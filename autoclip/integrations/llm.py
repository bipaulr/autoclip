"""LLM providers behind one interface: Gemini (free tier), Ollama (local fallback), mock."""
import hashlib
import time
from datetime import date, datetime, timezone
from typing import Callable, Protocol

import httpx
from sqlalchemy.orm import Session

from autoclip.models import LLMCache

PROMPT_OVERHEAD = 2000  # instruction text around the transcript


class LLMError(Exception):
    """Message never contains an API key. retryable = a temporary problem (overload, network)."""

    def __init__(self, message: str = "", retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class LLMQuotaError(LLMError):
    def __init__(self, message: str, daily: bool = False):
        super().__init__(message, retryable=not daily)
        self.daily = daily


class LLMSwitched(LLMError):
    """The provider changed and the prompt is too long for the new one: re-chunk and retry."""


class LLM(Protocol):
    name: str
    max_chars: int  # how much transcript text fits in one request

    def complete(self, prompt: str) -> str: ...


class MockLLM:
    def __init__(self, responses: list[str] | Callable[[str], str] | None = None,
                 name: str = "mock", max_chars: int = 60000, error: Exception | None = None):
        self.responses, self.name, self.max_chars, self.error = responses or [], name, max_chars, error
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        if callable(self.responses):
            return self.responses(prompt)
        return self.responses[min(len(self.prompts), len(self.responses)) - 1]


class GeminiLLM:
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None, max_chars: int = 60000):
        self.api_key, self.model, self.max_chars = api_key, model, max_chars
        self.client = client or httpx.Client(timeout=120)
        self.name = f"gemini:{model}"

    def complete(self, prompt: str) -> str:
        if not (self.api_key and self.model):
            raise LLMError("Gemini is not configured (GEMINI_API_KEY / GEMINI_MODEL)")
        try:
            r = self.client.post(
                f"{self.BASE}/models/{self.model}:generateContent",
                headers={"x-goog-api-key": self.api_key},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}})
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(f"Gemini request failed ({type(e).__name__})", retryable=True) from None
        if r.status_code == 429:
            text = str(body)
            raise LLMQuotaError("Gemini quota reached", daily="PerDay" in text or "per day" in text.lower())
        if r.status_code != 200:
            raise LLMError(f"Gemini HTTP {r.status_code}: {(body.get('error') or {}).get('message', '')[:200]}",
                           retryable=r.status_code >= 500)
        try:
            parts = body["candidates"][0]["content"]["parts"]
            # thinking models (Gemma 4) can put their reasoning in "thought" parts before the answer
            return next(p["text"] for p in parts if p.get("text") and not p.get("thought"))
        except (KeyError, IndexError, TypeError, StopIteration):
            raise LLMError("Gemini returned no text") from None


class OllamaLLM:
    def __init__(self, host: str, model: str, client: httpx.Client | None = None, max_chars: int = 6000):
        self.host, self.model, self.max_chars = host.rstrip("/"), model, max_chars
        self.client = client or httpx.Client(timeout=600)
        self.name = f"ollama:{model}"

    def complete(self, prompt: str) -> str:
        if not self.model:
            raise LLMError("Ollama is not configured (OLLAMA_MODEL)")
        try:
            r = self.client.post(f"{self.host}/api/generate", json={
                "model": self.model, "prompt": prompt, "stream": False, "format": "json",
                # 4096 tokens holds a 6000-character batch and keeps llama3.2:3b fully on a 4 GB GPU
                # (8192 spilled part of it to the CPU). keep_alive avoids a cold reload per batch.
                "keep_alive": "30m", "options": {"num_ctx": 4096, "temperature": 0.4}})
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(f"Ollama request failed ({type(e).__name__})") from None
        if r.status_code != 200 or "response" not in body:
            raise LLMError(f"Ollama HTTP {r.status_code}: {str(body.get('error', ''))[:200]}")
        return body["response"]


class FallbackLLM:
    """Primary with back-off on rate limits; switches to the fallback when the primary's
    daily quota is used up (until the date changes) or it keeps failing."""

    def __init__(self, primary: LLM, fallback: LLM, *, retry_delays: tuple[float, ...] = (5, 20),
                 sleep: Callable[[float], None] = time.sleep, today: Callable[[], date] = date.today):
        self.primary, self.fallback = primary, fallback
        self.retry_delays, self.sleep, self.today = retry_delays, sleep, today
        self.exhausted_on: date | None = None
        self.events: list[str] = []  # human-readable reasons, for the decision log

    @property
    def _use_fallback(self) -> bool:
        return self.exhausted_on == self.today()

    @property
    def name(self) -> str:
        return self.fallback.name if self._use_fallback else self.primary.name

    @property
    def max_chars(self) -> int:
        return self.fallback.max_chars if self._use_fallback else self.primary.max_chars

    def complete(self, prompt: str) -> str:
        if not self._use_fallback:
            reason, for_the_day = None, False
            for delay in (*self.retry_delays, None):
                try:
                    return self.primary.complete(prompt)
                except LLMQuotaError as e:
                    reason = f"{self.primary.name} quota reached" + (" for the day" if e.daily else "")
                    for_the_day = e.daily
                    if e.daily or delay is None:
                        break
                    self.sleep(delay)  # per-minute limit: back off and retry
                except LLMError as e:
                    reason = f"{self.primary.name} failed: {e}"
                    if not e.retryable or delay is None:
                        for_the_day = not e.retryable  # e.g. bad model name: stop trying today
                        break
                    self.sleep(delay)  # overload or network blip: back off and retry
            if for_the_day:
                self.exhausted_on = self.today()
            self.events.append(f"{reason}; falling back to {self.fallback.name}"
                               + (" for the rest of the day" if for_the_day else " for this request"))
            if len(prompt) > self.fallback.max_chars + PROMPT_OVERHEAD:
                if not for_the_day:
                    raise LLMError(self.events[-1], retryable=True)  # too long for the fallback: retry later
                raise LLMSwitched(self.events[-1])
        return self.fallback.complete(prompt)


class CachedLLM:
    """Stores every response in the DB so the same prompt never costs quota twice."""

    def __init__(self, inner: LLM, session: Session):
        self.inner, self.session = inner, session
        self.hits = 0

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def max_chars(self) -> int:
        return self.inner.max_chars

    def complete(self, prompt: str) -> str:
        key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        row = self.session.get(LLMCache, key)
        if row:
            self.hits += 1
            return row.response
        response = self.inner.complete(prompt)
        if is_local(self.inner.name):
            return response  # free to ask again, and a weaker answer must not stand once Gemini is back
        self.session.add(LLMCache(key=key, llm=self.inner.name, response=response,
                                  created_at=datetime.now(timezone.utc)))
        self.session.flush()
        return response


def is_local(name: str) -> bool:
    """The local Ollama model (free, unlimited, but much weaker than Gemini)."""
    return str(name or "").startswith("ollama")
