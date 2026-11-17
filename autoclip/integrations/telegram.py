import json
from typing import Protocol

import httpx

# buttons: rows of {"text": ..., "callback_data": ...} (tap -> sent back to the bot)
#          or {"text": ..., "url": ...} (opens a link)
Buttons = list[list[dict]]


class Notifier(Protocol):
    def send(self, text: str, buttons: Buttons | None = None) -> bool: ...


VIDEO_LIMIT = 50 * 10**6  # Telegram bots may upload files up to 50 MB


class MockNotifier:
    def __init__(self):
        self.sent: list[str] = []
        self.buttons: list[Buttons | None] = []
        self.answers: list[tuple[str, str]] = []
        self.edits: list[tuple[str, int, str]] = []
        self.inbox: list[dict] = []

    def send(self, text: str, buttons: Buttons | None = None) -> bool:
        self.sent.append(text)
        self.buttons.append(buttons)
        return True

    def updates(self, offset: int = 0) -> list[dict]:
        return [u for u in self.inbox if u["update_id"] >= offset]

    def send_video(self, path, caption: str, buttons: Buttons | None = None) -> bool:
        self.videos = getattr(self, "videos", []) + [(str(path), caption)]
        self.buttons.append(buttons)
        return getattr(self, "video_ok", True)

    def prompt(self, text: str) -> bool:
        self.sent.append(text)
        self.prompts = getattr(self, "prompts", []) + [text]
        return True

    def answer(self, callback_id: str, text: str) -> bool:
        self.answers.append((callback_id, text))
        return True

    def edit(self, chat_id: str, message_id: int, text: str) -> bool:
        self.edits.append((str(chat_id), message_id, text))
        return True


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, client: httpx.Client | None = None):
        self.bot_token, self.chat_id = bot_token, chat_id
        self.client = client or httpx.Client(timeout=20)

    def _call(self, method: str, payload: dict) -> dict | None:
        """Errors are swallowed without detail because the URL contains the bot token."""
        if not self.bot_token:
            return None
        try:
            r = self.client.post(f"https://api.telegram.org/bot{self.bot_token}/{method}", json=payload)
            data = r.json()
            return data if r.status_code == 200 and data.get("ok") else None
        except (httpx.HTTPError, ValueError):
            return None

    def send(self, text: str, buttons: Buttons | None = None) -> bool:
        """Returns False instead of raising: an alert failure must not break the caller."""
        if not self.chat_id:
            return False
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("sendMessage", payload) is not None

    def prompt(self, text: str) -> bool:
        """A message that opens the reply box, so the owner's answer comes straight back to the bot."""
        if not self.chat_id:
            return False
        return self._call("sendMessage", {"chat_id": self.chat_id, "text": text,
                                          "reply_markup": {"force_reply": True,
                                                           "input_field_placeholder": "https://youtube.com/shorts/..."}}) is not None

    def send_video(self, path, caption: str, buttons: Buttons | None = None) -> bool:
        """Uploads a clip file (for a manual post). False on any error or a file over 50 MB."""
        from pathlib import Path
        p = Path(path)
        if not (self.bot_token and self.chat_id) or not p.is_file() or p.stat().st_size > VIDEO_LIMIT:
            return False
        try:
            with open(p, "rb") as f:
                r = self.client.post(f"https://api.telegram.org/bot{self.bot_token}/sendVideo", timeout=180,
                                     data={"chat_id": self.chat_id, "caption": caption[:1024],
                                           "supports_streaming": "true",
                                           **({"reply_markup": json.dumps({"inline_keyboard": buttons})}
                                              if buttons else {})},
                                     files={"video": (p.name, f, "video/mp4")})
            return r.status_code == 200 and r.json().get("ok", False)
        except (httpx.HTTPError, ValueError, OSError):
            return False

    def updates(self, offset: int = 0) -> list[dict]:
        """New messages and button taps (no long-poll). Empty on any error."""
        data = self._call("getUpdates", {"offset": offset, "timeout": 0,
                                         "allowed_updates": ["message", "callback_query"]})
        return (data or {}).get("result", [])

    def answer(self, callback_id: str, text: str) -> bool:
        """The small toast Telegram shows after a button tap."""
        return self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]}) is not None

    def edit(self, chat_id: str, message_id: int, text: str) -> bool:
        """Rewrites a sent message and removes its buttons, so a tapped alert shows it is done."""
        return self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text,
                                              "disable_web_page_preview": True}) is not None
