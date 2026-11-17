import json
from datetime import date

import httpx
import pytest

from autoclip.integrations.llm import (CachedLLM, FallbackLLM, GeminiLLM, LLMError, LLMQuotaError, LLMSwitched,
                                       MockLLM, OllamaLLM)


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_gemini_sends_key_in_header_and_returns_text():
    def handler(request):
        assert request.url.path.endswith("/models/my-model:generateContent")
        assert request.headers["x-goog-api-key"] == "KEY" and "KEY" not in str(request.url)
        assert json.loads(request.content)["contents"][0]["parts"][0]["text"] == "hi"
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "OUT"}]}}]})

    llm = GeminiLLM("KEY", "my-model", client(handler))
    assert llm.complete("hi") == "OUT" and llm.name == "gemini:my-model"


def test_gemini_quota_errors_distinguish_daily_from_per_minute():
    daily = {"error": {"message": "quota", "details": [{"violations": [
        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    with pytest.raises(LLMQuotaError) as e:
        GeminiLLM("K", "m", client(lambda r: httpx.Response(429, json=daily))).complete("x")
    assert e.value.daily is True
    minute = {"error": {"message": "quota", "details": [{"violations": [{"quotaId": "PerMinute"}]}]}}
    with pytest.raises(LLMQuotaError) as e:
        GeminiLLM("K", "m", client(lambda r: httpx.Response(429, json=minute))).complete("x")
    assert e.value.daily is False


def test_gemini_errors_do_not_leak_the_key():
    def boom(request):
        raise httpx.ConnectError(f"failed {request.headers['x-goog-api-key']}")

    with pytest.raises(LLMError) as e:
        GeminiLLM("SECRETKEY", "m", client(boom)).complete("x")
    assert "SECRETKEY" not in str(e.value)
    with pytest.raises(LLMError, match="not configured"):
        GeminiLLM("", "", client(boom)).complete("x")


def test_ollama_posts_to_local_host():
    def handler(request):
        body = json.loads(request.content)
        assert str(request.url) == "http://ollama:11434/api/generate"
        assert (body["model"], body["stream"], body["format"]) == ("tiny", False, "json")
        assert body["options"]["num_ctx"] == 4096 and body["keep_alive"] == "30m"
        return httpx.Response(200, json={"response": "LOCAL"})

    llm = OllamaLLM("http://ollama:11434/", "tiny", client(handler))
    assert llm.complete("p") == "LOCAL" and llm.name == "ollama:tiny"
    with pytest.raises(LLMError):
        OllamaLLM("http://o", "tiny", client(lambda r: httpx.Response(500, json={"error": "x"}))).complete("p")


def test_fallback_backs_off_on_per_minute_limit_then_succeeds():
    calls, sleeps = [], []

    def flaky(prompt):
        calls.append(prompt)
        if len(calls) < 3:
            raise LLMQuotaError("per minute")
        return "PRIMARY"

    fb = FallbackLLM(MockLLM(flaky, name="gemini:m"), MockLLM(["LOCAL"], name="ollama:t"),
                     retry_delays=(5, 20), sleep=sleeps.append)
    assert fb.complete("p") == "PRIMARY" and sleeps == [5, 20] and fb.events == []


def test_daily_quota_switches_to_ollama_for_the_rest_of_the_day():
    sleeps, day = [], [date(2026, 11, 13)]
    primary = MockLLM(error=LLMQuotaError("daily", daily=True), name="gemini:m")
    fb = FallbackLLM(primary, MockLLM(["LOCAL"], name="ollama:t", max_chars=6000),
                     sleep=sleeps.append, today=lambda: day[0])
    assert fb.complete("p") == "LOCAL" and sleeps == []
    assert "quota reached for the day" in fb.events[0] and "ollama:t" in fb.events[0]
    assert (fb.name, fb.max_chars) == ("ollama:t", 6000)
    fb.complete("again")
    assert len(primary.prompts) == 1

    day[0] = date(2026, 11, 14)
    primary.error, primary.responses = None, ["PRIMARY"]
    assert fb.complete("p") == "PRIMARY"


def test_other_primary_failure_also_falls_back():
    fb = FallbackLLM(MockLLM(error=LLMError("HTTP 500"), name="gemini:m"), MockLLM(["LOCAL"], name="ollama:t"))
    assert fb.complete("p") == "LOCAL" and "HTTP 500" in fb.events[0]


def test_prompt_too_long_for_fallback_asks_caller_to_rechunk():
    fb = FallbackLLM(MockLLM(error=LLMQuotaError("d", daily=True), name="g"),
                     MockLLM(["LOCAL"], name="o", max_chars=100))
    with pytest.raises(LLMSwitched):
        fb.complete("x" * 5000)
    assert fb.complete("short") == "LOCAL"


def test_cache_avoids_second_request(session):
    inner = MockLLM(["FIRST", "SECOND"], name="gemini:m")
    cached = CachedLLM(inner, session)
    assert cached.complete("same") == "FIRST" and cached.complete("same") == "FIRST"
    assert len(inner.prompts) == 1 and cached.hits == 1
    assert cached.complete("other") == "SECOND"


def test_overload_is_retried_and_only_this_request_falls_back():
    sleeps, calls = [], []

    def overloaded(prompt):
        calls.append(prompt)
        raise LLMError("Gemini HTTP 503: high demand", retryable=True)

    primary = MockLLM(overloaded, name="gemini:m")
    fb = FallbackLLM(primary, MockLLM(["LOCAL"], name="ollama:t"), retry_delays=(5, 20), sleep=sleeps.append)
    assert fb.complete("p") == "LOCAL" and sleeps == [5, 20] and len(calls) == 3
    assert "for this request" in fb.events[0] and fb.name == "gemini:m"  # Gemini is tried again next time
    primary.responses = lambda p: "PRIMARY"
    assert fb.complete("q") == "PRIMARY"


def test_gemini_503_is_retryable_and_400_is_not():
    e503 = GeminiLLM("K", "m", client(lambda r: httpx.Response(503, json={"error": {"message": "busy"}})))
    with pytest.raises(LLMError) as e:
        e503.complete("x")
    assert e.value.retryable
    e400 = GeminiLLM("K", "m", client(lambda r: httpx.Response(400, json={"error": {"message": "bad model"}})))
    with pytest.raises(LLMError) as e:
        e400.complete("x")
    assert not e.value.retryable


def test_thinking_parts_are_skipped():
    parts = [{"text": "let me think...", "thought": True}, {"text": '{"ok": true}'}]
    llm = GeminiLLM("KEY", "gemma-4-26b-a4b-it",
                    client(lambda r: httpx.Response(200, json={"candidates": [{"content": {"parts": parts}}]})))
    assert llm.complete("x") == '{"ok": true}'


def test_the_chain_is_flash_then_lite_then_gemma_then_ollama():
    from autoclip.config import Settings
    from autoclip.tasks import build_llm
    s = Settings(_env_file=None, gemini_api_key="k", gemini_model="flash", gemini_fallback_model="lite",
                 gemini_extra_models="gemma-a, gemma-b", ollama_model="llama")
    names, node = [], build_llm(s)
    while isinstance(node, FallbackLLM):
        names.append(node.primary.name)
        node = node.fallback
    names.append(node.name)
    assert names == ["gemini:flash", "gemini:lite", "gemini:gemma-a", "gemini:gemma-b", "ollama:llama"]
