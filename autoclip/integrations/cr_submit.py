"""Submits a posted clip's link on its Content Rewards campaign page, the way the owner does by hand.

Used only while the owner is asleep, only for campaigns listed in AUTO_SUBMIT_CAMPAIGNS. It drives the
normal website (campaign page -> "Submit clip" -> paste link -> tick the requirements box -> "Submit
clip"), never /api/, with human-like pauses. The login is the owner's own: they sign in once with
scripts/cr_login.py, which saves the browser session to CR_STATE_PATH. No password is ever stored here.

Creator Terms (read 16 Nov 2026) do not address automated submission; section 7.12 requires submitting
within 30 minutes of posting, which is why this exists.
"""
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class SubmitResult:
    ok: bool
    reason: str
    logged_out: bool = False
    notice: str | None = None  # text of a "Before you submit" notice, when the campaign showed one


class ClipSubmitter(Protocol):
    def submit(self, campaign_url: str, post_url: str) -> SubmitResult: ...


class MockSubmitter:
    def __init__(self, result: SubmitResult | None = None):
        self.result = result or SubmitResult(True, "submitted (mock)")
        self.calls: list[tuple[str, str]] = []

    def submit(self, campaign_url: str, post_url: str) -> SubmitResult:
        self.calls.append((campaign_url, post_url))
        return self.result


SUCCESS_WORDS = ("submitted", "under review", "pending review", "in review")
ERROR_WORDS = ("can't be submitted", "cannot be submitted", "invalid", "too old",
               "not eligible", "error", "failed")


def run_submit(page, campaign_url: str, post_url: str, *, pause=None, dry_run: bool = False) -> SubmitResult:
    """The click path, written against Playwright's page API so a fake page can test it."""
    pause = pause or (lambda: time.sleep(random.uniform(1.2, 3.5)))
    if "/c/campaigns/" not in campaign_url:
        return SubmitResult(False, "no exact campaign page known for this campaign")
    page.goto(campaign_url, wait_until="domcontentloaded")
    pause()
    if any(w in page.url for w in ("/login", "/sign-in", "/signin", "/auth")):
        return SubmitResult(False, "the saved Content Rewards login has expired", logged_out=True)
    not_now = page.get_by_role("button", name="Not now")
    if not_now.count():
        not_now.first.click()
        pause()
    opener = page.get_by_role("button", name="Submit clip")
    _wait_visible(opener, 20000)  # the page draws this button late; a headless check can come too early
    if not opener.count():
        return SubmitResult(False, "no 'Submit clip' button on the campaign page (logged out or not joined?)",
                            logged_out=True)
    opener.first.click()
    pause()
    notice = _confirm_notice(page, pause)
    dialog = page.get_by_role("dialog", name="Submit clip")
    box = dialog.get_by_role("textbox", name="Clip link")
    _wait_visible(box, 10000)
    if not box.count():
        return SubmitResult(False, "submit dialog changed: no 'Clip link' box", notice=notice)
    box.first.fill(post_url)
    pause()
    tick = dialog.get_by_role("checkbox")
    if tick.count():
        # a styled box drawn over a hidden input: a normal check() waits forever for it to be visible
        tick.first.check(force=True, timeout=5000)
        pause()
    final = dialog.get_by_role("button", name="Submit clip").first
    if _disabled(final):
        page.wait_for_timeout(3000)  # the link is checked as it is typed; give it a moment before judging
    if _disabled(final):
        text = _verdict_text(page)
        if ALREADY in text:
            return SubmitResult(True, "already submitted", notice=notice)
        snippet = next((w for w in ERROR_WORDS if w in text), "the form would not accept the link")
        return SubmitResult(False, f"Content Rewards refused it ('{snippet}')", notice=notice)
    if dry_run:
        return SubmitResult(True, "dry run: form filled, not sent", notice=notice)
    final.click()
    page.wait_for_timeout(5000)
    text = _verdict_text(page)
    if ALREADY in text:  # the form re-checks the link after sending: this is the success case
        return SubmitResult(True, "submitted (Content Rewards: already submitted)", notice=notice)
    if any(w in text for w in ERROR_WORDS) and dialog.count():
        snippet = next((w for w in ERROR_WORDS if w in text), "error")
        return SubmitResult(False, f"Content Rewards refused it ('{snippet}')", notice=notice)
    if not dialog.count() or any(w in text for w in SUCCESS_WORDS):
        return SubmitResult(True, "submitted", notice=notice)
    return SubmitResult(False, "no confirmation seen after clicking Submit clip", notice=notice)


HOLD_MS = 3000


# Any "already submitted" wording means the link is in: after sending, the form re-checks it and says so.
ALREADY = "already submitted"
# The dialog always shows a heading like "11 posts can't be submitted" (older posts): never a verdict.
COUNT_HEADING = re.compile(r"\d+\s+posts?\s+can.t be submitted")


def _verdict_text(page) -> str:
    return COUNT_HEADING.sub("", (page.inner_text("body") or "").lower())


def _disabled(button) -> bool:
    """True when the final Submit button is greyed out (the form rejected the link)."""
    get = getattr(button, "get_attribute", None)
    try:
        return bool(get) and get("aria-disabled") == "true"
    except Exception:  # noqa: BLE001 - a detached element: let the click path report it
        return False


def _wait_visible(locator, timeout_ms: int) -> None:
    """Waits for an element to appear; a fake page in the tests has no wait_for and is ready at once."""
    wait = getattr(getattr(locator, "first", locator), "wait_for", None)
    if wait:
        try:
            wait(state="visible", timeout=timeout_ms)
        except Exception:  # noqa: BLE001 - not there in time: the caller's count() check reports it
            pass


def _confirm_notice(page, pause) -> str | None:
    """Some campaigns show a "Before you submit" notice (shown once, and again after the campaign updates
    it) that is accepted by pressing and holding "Hold to confirm". Returns the notice text, or None."""
    hold = page.get_by_role("button", name="Hold to confirm")
    _wait_visible(hold, 3000)
    if not hold.count():
        return None
    texts = [d.inner_text() for d in _all(page.get_by_role("dialog")) if hasattr(d, "inner_text")]
    notice = next((t for t in texts if "hold to confirm" in t.lower()), texts[0] if texts else "")
    notice = notice.replace("Hold to confirm", "").strip() or None
    box = hold.first.bounding_box() if hasattr(hold.first, "bounding_box") else None
    if box:  # a real press and hold: the button reacts to a held mouse button, not to a click
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.wait_for_timeout(HOLD_MS)
        page.mouse.up()
    else:
        hold.first.hold(HOLD_MS)
    pause()
    return notice


def _all(locator) -> list:
    getter = getattr(locator, "all", None)
    return getter() if getter else [locator]


class BrowserSubmitter:
    """Headless Chromium with the owner's saved session (Playwright storage state)."""

    def __init__(self, state_path: str | Path, *, dry_run: bool = False):
        self.state_path, self.dry_run = Path(state_path), dry_run

    def submit(self, campaign_url: str, post_url: str) -> SubmitResult:
        if not self.state_path.exists():
            return SubmitResult(False, "no saved Content Rewards login: run scripts/cr_login.py once",
                                logged_out=True)
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(storage_state=str(self.state_path), viewport={"width": 1396, "height": 800})
                page = ctx.new_page()
                result = run_submit(page, campaign_url, post_url, dry_run=self.dry_run)
                if not result.logged_out:
                    ctx.storage_state(path=str(self.state_path))  # keep refreshed cookies
                browser.close()
                return result
        except PlaywrightError as e:
            return SubmitResult(False, f"browser error ({str(e).splitlines()[0][:150]})")
