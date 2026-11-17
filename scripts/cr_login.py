"""One-time Content Rewards login for auto-submit.

Opens a normal browser window on contentrewards.com. Log in there yourself (your password never passes
through autoclip), then come back here and press Enter. The browser session is saved to
secrets/cr_state.json, which the worker uses to submit campaign links while you sleep.
Run it again whenever Telegram says the login needs renewing.

    py -3.12 -m pip install playwright==1.55.0
    py -3.12 -m playwright install chromium
    py -3.12 scripts/cr_login.py
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

STATE = Path(__file__).resolve().parent.parent / "secrets" / "cr_state.json"


def main() -> None:
    STATE.parent.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        try:  # your installed Google Chrome first (sign-in pages trust it more), else Playwright's own
            browser = pw.chromium.launch(channel="chrome", headless=False)
        except Exception:  # noqa: BLE001
            browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(storage_state=str(STATE) if STATE.exists() else None)
        page = ctx.new_page()
        page.goto("https://contentrewards.com/c/campaigns")
        input("Log in to Content Rewards in the browser window, open your Campaigns page, "
              "then press Enter here... ")
        if "/c/" not in page.url:
            print(f"Still not on a logged-in page ({page.url}). Nothing saved; run this again.")
            return
        ctx.storage_state(path=str(STATE))
        browser.close()
    print(f"Saved. Auto-submit can now use your login ({STATE.name}).")


if __name__ == "__main__":
    main()
