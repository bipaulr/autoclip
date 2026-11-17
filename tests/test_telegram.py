import json

import httpx

from autoclip.integrations.telegram import MockNotifier, TelegramNotifier


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sends_message_to_configured_chat():
    seen = {}

    def handler(request):
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    assert TelegramNotifier("TOK", "42", client(handler)).send("hello") is True
    assert seen["url"] == "https://api.telegram.org/botTOK/sendMessage"
    assert seen["body"] == {"chat_id": "42", "text": "hello", "disable_web_page_preview": True}


def test_api_error_returns_false():
    n = TelegramNotifier("TOK", "42", client(lambda r: httpx.Response(401, json={"ok": False})))
    assert n.send("x") is False


def test_network_error_returns_false():
    def handler(request):
        raise httpx.ConnectError("down")

    assert TelegramNotifier("TOK", "42", client(handler)).send("x") is False


def test_unconfigured_does_not_call_network():
    def handler(request):
        raise AssertionError("must not be called")

    assert TelegramNotifier("", "", client(handler)).send("x") is False


def test_mock_notifier_records():
    m = MockNotifier()
    assert m.send("a") and m.sent == ["a"]


def test_buttons_taps_and_edits():
    calls = []

    def handler(request):
        calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}]})
        return httpx.Response(200, json={"ok": True, "result": {}})

    n = TelegramNotifier("TOK", "42", client(handler))
    assert n.send("x", [[{"text": "Submitted", "callback_data": "sub:3"}]])
    assert calls[0][1]["reply_markup"] == {"inline_keyboard": [[{"text": "Submitted", "callback_data": "sub:3"}]]}
    assert n.updates(5) == [{"update_id": 1}] and calls[1][1]["allowed_updates"] == ["message", "callback_query"]
    assert n.answer("cb", "done") and n.edit("42", 7, "new text")
    assert [c[0] for c in calls] == ["sendMessage", "getUpdates", "answerCallbackQuery", "editMessageText"]
