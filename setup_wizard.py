#!/usr/bin/env python3
"""autoclip setup wizard: walks through each account/key, validates it with a
real API call, and saves it to .env next to this script.

Usage:
    py -3.12 setup_wizard.py              # run all steps
    py -3.12 setup_wizard.py --only 2 3   # run selected steps (1-5)
    py -3.12 setup_wizard.py --show       # echo pasted secrets (if hidden paste misbehaves)

Standard library only. Secrets are never printed.
"""
import argparse
import base64
import getpass
import hashlib
import http.server
import json
import os
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
SECRETS_DIR = HERE / "secrets"
SHOW = False
REMAINING = []  # things only the user can do, printed at the end


# ---------------------------------------------------------------- helpers
def say(msg=""):
    print(msg, flush=True)


def header(n, title):
    say("\n" + "=" * 70)
    say(f" STEP {n}: {title}")
    say("=" * 70)


def ask(prompt, secret=False, default=None):
    suffix = f" [{default}]" if default else ""
    full = f"{prompt}{suffix}: "
    val = (input(full) if (SHOW or not secret) else getpass.getpass(full)).strip()
    return val or (default or "")


def confirm(prompt, default=True):
    d = "Y/n" if default else "y/N"
    v = input(f"{prompt} [{d}]: ").strip().lower()
    return default if not v else v.startswith("y")


def pause(msg="Press Enter when done"):
    input(f"  -> {msg}...")


def open_url(url):
    say(f"  Opening: {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass


def read_env():
    out = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def save_env(**pairs):
    """Update-or-append keys in .env, preserving everything else."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(pairs)
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in remaining:
                lines[i] = f"{k}={remaining.pop(k)}"
    lines += [f"{k}={v}" for k, v in remaining.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"  Saved to .env: {', '.join(pairs)}")


def api_call(method, url, headers=None, data=None, form=None, timeout=30):
    """Returns (status, parsed_json_or_text). Never raises on HTTP errors."""
    headers = dict(headers or {})
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif data is not None:
        body = json.dumps(data).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:  # network down, DNS, etc.
        return 0, str(e)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def already_set(*keys):
    env = read_env()
    if all(env.get(k) for k in keys):
        return not confirm(f"  {', '.join(keys)} already in .env. Redo this step?", default=False)
    return False


def retry(fn):
    """Run fn() until it returns truthy or the user gives up."""
    while True:
        if fn():
            return True
        if not confirm("  That failed. Try again?", default=True):
            REMAINING.append("A step was skipped after failing; re-run with --only <n>.")
            return False


# ------------------------------------------------- 1. Google / YouTube
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def _oauth_loopback(client_id, client_secret, auth_uri, token_uri):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in q or "error" in q:
                result["q"] = q
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h3>autoclip: you can close this tab and return to the terminal.</h3>")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    params = {
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES), "access_type": "offline", "prompt": "consent",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    say("  A browser tab will open. Pick your YouTube account, click Continue past the")
    say("  'Google hasn't verified this app' warning (Advanced -> Go to ... (unsafe)),")
    say("  tick ALL permission boxes, then click Continue.")
    open_url(auth_uri + "?" + urllib.parse.urlencode(params))
    t0 = time.time()
    while "q" not in result and time.time() - t0 < 300:
        time.sleep(0.5)
    srv.shutdown()
    q = result.get("q")
    if not q:
        say("  Timed out waiting for the browser.")
        return None
    if q.get("state", [""])[0] != state or "code" not in q:
        say(f"  Authorization failed: {q.get('error', ['state mismatch'])[0]}")
        return None
    st, tok = api_call("POST", token_uri, form={
        "code": q["code"][0], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    })
    if st != 200 or not isinstance(tok, dict):
        say(f"  Token exchange failed ({st}): {tok}")
        return None
    return tok


def step_google():
    header(1, "Google Cloud: YouTube Data API v3 + YouTube Analytics API + OAuth desktop client")
    if already_set("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
        return
    say("""
 A. Create the project
    1. Open https://console.cloud.google.com/projectcreate
    2. Project name: autoclip  -> click CREATE. Wait for the bell icon to say it's done,
       then make sure 'autoclip' is the project shown in the top bar.
 B. Enable both APIs (open each link, click the blue ENABLE button)
    - https://console.cloud.google.com/apis/library/youtube.googleapis.com
    - https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com
 C. Consent screen
    1. Open https://console.cloud.google.com/auth/overview  -> GET STARTED
    2. App name: autoclip, support email: yours -> Next -> Audience: External -> Next
    3. Contact email: yours -> Next -> tick the agree box -> Continue -> Create
    4. Left menu 'Audience': click PUBLISH APP -> Confirm. (Important: apps left in
       'Testing' have refresh tokens that expire after 7 days.)
    5. Left menu 'Data Access' -> Add or remove scopes: add the 3 YouTube ones
       (youtube.upload, youtube.readonly, yt-analytics.readonly) -> Update -> Save.
 D. Create the OAuth client
    1. Open https://console.cloud.google.com/auth/clients -> CREATE CLIENT
    2. Application type: Desktop app, name: autoclip-desktop -> Create
    3. In the dialog click DOWNLOAD JSON. Note where it saves (usually Downloads).
""")
    open_url("https://console.cloud.google.com/projectcreate")
    pause("Do A-D, then press Enter")

    def attempt():
        p = ask("  Paste the full path to the downloaded client_secret_*.json (drag the file into this window)")
        p = p.strip('"').strip("'")
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            cfg = data.get("installed") or data.get("web")
            if "installed" not in data:
                say("  This is not a Desktop-app client (expected an 'installed' section). "
                    "Create the client again with type 'Desktop app'.")
                return False
            cid, csec = cfg["client_id"], cfg["client_secret"]
        except Exception as e:
            say(f"  Could not read that file: {e}")
            return False
        tok = _oauth_loopback(cid, csec, cfg["auth_uri"], cfg["token_uri"])
        if not tok or "refresh_token" not in tok:
            say("  No refresh token returned. Revoke autoclip at "
                "https://myaccount.google.com/permissions and retry.")
            return False
        granted = tok.get("scope", "")
        missing = [s for s in GOOGLE_SCOPES if s not in granted]
        if missing:
            say(f"  You didn't grant: {missing}. Retry and tick every box.")
            return False
        auth = {"Authorization": f"Bearer {tok['access_token']}"}
        st, ch = api_call("GET", "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true", auth)
        if st != 200:
            reason = json.dumps(ch)[:300]
            say(f"  YouTube Data API call failed ({st}): {reason}")
            if "accessNotConfigured" in reason or "has not been used" in reason:
                say("  -> The API isn't enabled yet (step B).")
            return False
        items = ch.get("items", [])
        if not items:
            say("  Authorized, but that Google account has no YouTube channel. "
                "Create one at https://www.youtube.com/create_channel and retry.")
            return False
        say(f"  YouTube Data API OK. Channel: {items[0]['snippet']['title']}")
        end, start = date.today(), date.today() - timedelta(days=7)
        st, an = api_call("GET", "https://youtubeanalytics.googleapis.com/v2/reports?" + urllib.parse.urlencode(
            {"ids": "channel==MINE", "metrics": "views", "startDate": start, "endDate": end}), auth)
        if st != 200:
            say(f"  YouTube Analytics API call failed ({st}): {json.dumps(an)[:300]}")
            say("  -> Enable 'YouTube Analytics API' (step B) and retry.")
            return False
        say("  YouTube Analytics API OK.")
        SECRETS_DIR.mkdir(exist_ok=True)
        dest = SECRETS_DIR / "youtube_client_secret.json"
        shutil.copy(p, dest)
        save_env(YOUTUBE_CLIENT_ID=cid, YOUTUBE_CLIENT_SECRET=csec,
                 YOUTUBE_REFRESH_TOKEN=tok["refresh_token"],
                 YOUTUBE_CHANNEL_ID=items[0]["id"],
                 YOUTUBE_CLIENT_SECRET_FILE=str(dest.relative_to(HERE)).replace("\\", "/"))
        return True

    retry(attempt)
    REMAINING.append("YouTube: API upload quota is 10,000 units/day (a video upload costs ~1,600), "
                     "and uploads from API projects created after 2020-07-28 start as PRIVATE until "
                     "you pass YouTube's API compliance audit: https://support.google.com/youtube/contact/yt_api_form")


# ------------------------------------------------------------ 2. Gemini
def step_gemini():
    header(2, "Gemini API key (Google AI Studio)")
    if already_set("GEMINI_API_KEY"):
        return
    say("""
    1. Open https://aistudio.google.com/apikey and sign in with Google.
    2. Click 'Create API key'. Pick the 'autoclip' project (or 'Create API key in
       new project').
    3. Click the copy icon next to the key.
""")
    open_url("https://aistudio.google.com/apikey")

    def attempt():
        key = ask("  Paste the Gemini API key", secret=True)
        st, r = api_call("GET", "https://generativelanguage.googleapis.com/v1beta/models?pageSize=50",
                     {"x-goog-api-key": key})
        if st != 200:
            say(f"  Rejected ({st}): {json.dumps(r)[:200]}")
            return False
        names = [m["name"] for m in r.get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        # The model list can include models closed to new users (404), so try several.
        text_only = [n for n in names if not any(x in n for x in ("tts", "image", "audio", "live", "embed",
                                                                   "native", "omni"))]
        # Flash-Lite has the largest free daily allowance; the "-latest" alias keeps tracking new versions.
        cands = ([n for n in text_only if n.endswith("flash-lite-latest")] +
                 [n for n in text_only if "flash" in n][::-1])[:8] or text_only[:8]
        working, quota_hit = None, False
        for m in cands:
            st, g = api_call("POST", f"https://generativelanguage.googleapis.com/v1beta/{m}:generateContent",
                             {"x-goog-api-key": key},
                             data={"contents": [{"parts": [{"text": "Reply with the single word OK"}]}]})
            if st == 200:
                working = m
                break
            if st == 429:
                quota_hit = True
            say(f"  {m}: skipped ({st})")
        if not working and not quota_hit:
            say("  Key is accepted but no listed model could generate. Check the key's project in AI Studio.")
            return False
        if working:
            say(f"  Gemini OK; generated text with {working}.")
            save_env(GEMINI_API_KEY=key, GEMINI_MODEL=working.split("/", 1)[-1])
        else:
            say("  Key is valid but all tried models are rate limited (429); set GEMINI_MODEL yourself later.")
            save_env(GEMINI_API_KEY=key)
        return True

    retry(attempt)


# ------------------------------------------------------------ 3. Telegram
def step_telegram():
    header(3, "Telegram bot token + chat ID")
    if already_set("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        return
    say("""
    1. In Telegram, open a chat with @BotFather (blue check mark, https://t.me/BotFather).
    2. Send:  /newbot
    3. Send a display name (e.g. autoclip), then a username ending in 'bot'
       (e.g. autoclip_yourname_bot).
    4. BotFather replies with 'Use this token to access the HTTP API:' -> copy the token
       (looks like 123456789:AA...).
""")
    open_url("https://t.me/BotFather")
    state = {}

    def get_token():
        tok = ask("  Paste the bot token", secret=True)
        st, r = api_call("GET", f"https://api.telegram.org/bot{tok}/getMe")
        if st != 200 or not r.get("ok"):
            say(f"  Rejected ({st}): {r if isinstance(r, str) else r.get('description')}")
            return False
        state["token"], state["user"] = tok, r["result"]["username"]
        say(f"  Token OK. Bot: @{state['user']}")
        return True

    if not retry(get_token):
        return
    say(f"""
    5. Open https://t.me/{state['user']}  -> press START (or send any message like 'hi').
       I'll wait up to 3 minutes and detect your chat ID automatically.
""")
    open_url(f"https://t.me/{state['user']}")
    api_call("GET", f"https://api.telegram.org/bot{state['token']}/deleteWebhook")  # getUpdates fails if a webhook is set
    t0, chat = time.time(), None
    offset = 0
    while time.time() - t0 < 180 and not chat:
        st, r = api_call("GET", f"https://api.telegram.org/bot{state['token']}/getUpdates?timeout=10&offset={offset}",
                     timeout=20)
        if st == 200 and r.get("ok"):
            for u in r["result"]:
                offset = u["update_id"] + 1
                m = u.get("message") or u.get("channel_post")
                if m and m["chat"].get("type") == "private":
                    chat = m["chat"]
        else:
            time.sleep(2)
    if not chat:
        say("  Didn't see a message. Send one to the bot and re-run with --only 3.")
        REMAINING.append("Telegram: message your bot, then re-run `setup_wizard.py --only 3`.")
        return
    who = chat.get("first_name") or chat.get("username") or "you"
    say(f"  Detected chat ID {chat['id']} ({who}).")
    st, r = api_call("POST", f"https://api.telegram.org/bot{state['token']}/sendMessage",
                 data={"chat_id": chat["id"], "text": "autoclip setup: Telegram is connected."})
    if st != 200:
        say(f"  Could not send a test message ({st}): {r}")
        return
    say("  Test message sent; check Telegram.")
    save_env(TELEGRAM_BOT_TOKEN=state["token"], TELEGRAM_CHAT_ID=chat["id"],
             TELEGRAM_BOT_USERNAME=state["user"])


# ----------------------------------------------------------- 4. Instagram
def step_instagram():
    header(4, "Meta developer app + Instagram API (unified / Facebook Login for Business)")
    if already_set("IG_APP_ID", "IG_APP_SECRET", "IG_ACCESS_TOKEN", "IG_USER_ID"):
        return
    say("""
 Can your own account publish without App Review?  YES. Meta's docs say an app that
 only serves Instagram professional accounts you own/manage needs only Standard
 Access ('App Review: Not required'). Keep the app in DEVELOPMENT mode, and you
 (the app admin) must own the Instagram account and the linked Facebook Page.
   https://developers.facebook.com/docs/instagram-platform/app-review

 A. Instagram must be Professional (Creator or Business) - phone app:
    Profile -> menu -> Settings and privacy -> Account type and tools ->
    Switch to professional account.
 B. Unified login needs a Facebook PAGE linked to that Instagram account:
    1. Create a Page if you have none: https://www.facebook.com/pages/create
       (any name, e.g. 'autoclip'; category any).
    2. Link it: Instagram app -> Profile -> Edit profile -> Page (under Public
       business information) -> Connect or create -> choose that Page.
       (Or on the Page: Settings -> Linked accounts -> Instagram -> Connect.)
 C. Meta app (you already created it). On its dashboard:
    App settings -> Basic: copy 'App ID', click Show on 'App secret' and copy it.
 D. Get a token with Graph API Explorer:
    1. Open https://developers.facebook.com/tools/explorer/
    2. Right panel 'Meta App': choose your autoclip app.
    3. 'User or Page': User Token. Under Permissions add:
         instagram_basic, instagram_content_publish, instagram_manage_insights, pages_show_list,
         pages_read_engagement, business_management
    4. Click 'Generate Access Token' -> continue as yourself -> when asked which
       Pages/Instagram accounts, select the linked Page + Instagram -> Done.
    5. Copy the token from the 'Access Token' box (short-lived, 1 hour; this wizard
       exchanges it for a ~60-day one).
""")
    open_url("https://developers.facebook.com/tools/explorer/")
    pause("Do A-D, then press Enter")
    G = "https://graph.facebook.com"

    def attempt():
        env = read_env()
        app_id, app_secret = env.get("IG_APP_ID", ""), env.get("IG_APP_SECRET", "")
        if app_id and app_secret:
            say("  Using the app ID + secret already verified and saved in .env.")
        else:
            app_id = ask("  Paste the Meta App ID (App settings -> Basic)")
            app_secret = ask("  Paste the App secret", secret=True)
        short = ask("  Paste the access token", secret=True)
        if not app_id.isdigit():
            say("  App ID should be digits only.")
            return False
        if short.startswith("IG"):  # Instagram-Login style token: validated on graph.instagram.com
            GI = "https://graph.instagram.com"
            st, me = api_call("GET", GI + "/me?" + urllib.parse.urlencode({
                "fields": "user_id,username,account_type", "access_token": short}))
            if st != 200:
                say(f"  Token rejected ({st}): {json.dumps(me)[:250]}")
                return False
            uid = str(me.get("user_id") or me.get("id"))
            say(f"  Token OK. @{me.get('username')} ({me.get('account_type')})")
            st, lim = api_call("GET", f"{GI}/{uid}/content_publishing_limit?" + urllib.parse.urlencode({
                "fields": "quota_usage,config", "access_token": short}))
            if st != 200:
                say(f"  Publishing check failed ({st}): {json.dumps(lim)[:250]}")
                say("  -> Ensure instagram_business_content_publish / instagram_content_publish is granted.")
                return False
            q = ((lim.get("data") or [{}])[0])
            say(f"  Publishing permission OK. Quota used: {q.get('quota_usage')}/"
                f"{(q.get('config') or {}).get('quota_total')} per 24h.")
            say("  (App ID/secret were not checked by a call for this token type.)")
            save_env(IG_APP_ID=app_id, IG_APP_SECRET=app_secret, IG_ACCESS_TOKEN=short, IG_USER_ID=uid,
                     IG_USERNAME=me.get("username", ""), IG_GRAPH_HOST="graph.instagram.com",
                     IG_TOKEN_TYPE="instagram_login")
            return True
        st, ex = api_call("GET", G + "/oauth/access_token?" + urllib.parse.urlencode({
            "grant_type": "fb_exchange_token", "client_id": app_id,
            "client_secret": app_secret, "fb_exchange_token": short}))
        if st != 200:
            # System-user tokens (from the app's login configuration) can't be exchanged; they
            # never expire. Validate them with debug_token using the app's own credentials.
            st2, dbg = api_call("GET", G + "/debug_token?" + urllib.parse.urlencode({
                "input_token": short, "access_token": f"{app_id}|{app_secret}"}))
            d = dbg.get("data", {}) if isinstance(dbg, dict) else {}
            if st2 != 200 or not d.get("is_valid") or str(d.get("app_id")) != app_id:
                say(f"  Exchange failed ({st}): {json.dumps(ex)[:200]}")
                say(f"  debug_token ({st2}): {json.dumps(dbg)[:200]}")
                say("  -> Wrong app ID/secret, or the token belongs to a different app / has expired.")
                return False
            ids = [t for g in d.get("granular_scopes", []) if g.get("scope") == "instagram_content_publish"
                   for t in g.get("target_ids", [])]
            if not ids:
                say("  Token is valid but has no Instagram account with instagram_content_publish. "
                    "Generate it again and select your Instagram account.")
                return False
            token, uid = short, ids[0]
            say(f"  App ID + secret OK; system-user token (type {d.get('type')}, "
                f"expires: {d.get('expires_at') or 'never'}).")
            st, lim = api_call("GET", f"{G}/{uid}/content_publishing_limit?" + urllib.parse.urlencode({
                "fields": "quota_usage,config", "access_token": token}))
            if st != 200:
                say(f"  Publishing check failed ({st}): {json.dumps(lim)[:250]}")
                return False
            q = ((lim.get("data") or [{}])[0])
            say(f"  Publishing permission OK. Quota used: {q.get('quota_usage')}/"
                f"{(q.get('config') or {}).get('quota_total')} per 24h.")
            st, me = api_call("GET", f"{G}/{uid}?fields=username&access_token={token}")
            uname = me.get("username", "") if isinstance(me, dict) else ""
            save_env(IG_APP_ID=app_id, IG_APP_SECRET=app_secret, IG_ACCESS_TOKEN=token, IG_USER_ID=uid,
                     IG_USERNAME=uname, IG_GRAPH_HOST="graph.facebook.com", IG_TOKEN_TYPE="system_user")
            return True
        token = ex["access_token"]
        say("  App ID + secret OK; got a long-lived token (~60 days).")
        save_env(IG_APP_ID=app_id, IG_APP_SECRET=app_secret)  # verified; no need to paste again
        st, pg = api_call("GET", G + "/me/accounts?" + urllib.parse.urlencode({
            "fields": "name,instagram_business_account{id,username}", "access_token": token}))
        if st != 200:
            say(f"  Could not list Pages ({st}): {json.dumps(pg)[:250]}")
            return False
        linked = [(p["name"], p["id"], p["instagram_business_account"])
                  for p in pg.get("data", []) if p.get("instagram_business_account")]
        if not linked:
            # Unified login can grant an Instagram account directly, with no Facebook Page.
            # Read the granted Instagram account IDs from the token itself.
            st2, dbg = api_call("GET", G + "/debug_token?" + urllib.parse.urlencode({
                "input_token": token, "access_token": f"{app_id}|{app_secret}"}))
            d = dbg.get("data", {}) if isinstance(dbg, dict) else {}
            ids = []
            for g in d.get("granular_scopes", []):
                if g.get("scope") in ("instagram_content_publish", "instagram_basic"):
                    ids += [t for t in g.get("target_ids", []) if t not in ids]
            # A scope with no target_ids applies to every account the user granted, so also
            # try the account ID shown on the app dashboard (IG_USER_ID_HINT in .env).
            hint = read_env().get("IG_USER_ID_HINT", "")
            if hint and hint not in ids:
                ids.append(hint)
            for uid in ids:
                for host in (G, "https://graph.instagram.com"):
                    st, lim = api_call("GET", f"{host}/{uid}/content_publishing_limit?" + urllib.parse.urlencode({
                        "fields": "quota_usage,config", "access_token": token}))
                    if st != 200:
                        say(f"  {host.split('//')[1]} account {uid}: check failed ({st}): {json.dumps(lim)[:200]}")
                        continue
                    q = ((lim.get("data") or [{}])[0])
                    st, me = api_call("GET", f"{host}/{uid}?fields=username&access_token={token}")
                    uname = me.get("username", "") if isinstance(me, dict) else ""
                    say(f"  Publishing permission OK for @{uname or uid}. Quota used: {q.get('quota_usage')}/"
                        f"{(q.get('config') or {}).get('quota_total')} per 24h.")
                    save_env(IG_ACCESS_TOKEN=token, IG_USER_ID=uid, IG_USERNAME=uname,
                             IG_GRAPH_HOST=host.split("//")[1], IG_TOKEN_TYPE="user_long_lived")
                    return True
            # Keep the (valid) token so it can be diagnosed without pasting again.
            save_env(IG_ACCESS_TOKEN_UNVERIFIED=token)
            say("  Token is valid for the app but the Instagram account check did not pass yet.")
            say("  It is saved as IG_ACCESS_TOKEN_UNVERIFIED - answer 'n' below and tell Claude; no more pasting needed.")
            return False
        for i, (n, pid, ig) in enumerate(linked, 1):
            say(f"   {i}. Page '{n}' -> Instagram @{ig.get('username')}")
        idx = 1
        if len(linked) > 1:
            idx = int(ask("  Which one posts for autoclip? number", default="1") or 1)
        page_name, page_id, ig = linked[idx - 1]
        uid = ig["id"]
        st, lim = api_call("GET", f"{G}/{uid}/content_publishing_limit?" + urllib.parse.urlencode({
            "fields": "quota_usage,config", "access_token": token}))
        if st != 200:
            say(f"  Publishing check failed ({st}): {json.dumps(lim)[:250]}")
            say("  -> Make sure instagram_content_publish was ticked in Explorer; then generate a NEW token.")
            return False
        d = (lim.get("data") or [{}])[0]
        say(f"  Publishing permission OK for @{ig.get('username')}. Quota used: "
            f"{d.get('quota_usage')}/{(d.get('config') or {}).get('quota_total')} per 24h.")
        save_env(IG_APP_ID=app_id, IG_APP_SECRET=app_secret, IG_ACCESS_TOKEN=token, IG_USER_ID=uid,
                 IG_USERNAME=ig.get("username", ""), IG_PAGE_ID=page_id, IG_GRAPH_HOST="graph.facebook.com")
        return True

    retry(attempt)
    REMAINING.append("Instagram: the saved user token lasts ~60 days. Re-do step D (Explorer) and re-run "
                     "`--only 4` before it expires, or build a refresh using the same fb_exchange_token call.")
    REMAINING.append("Instagram: publishing goes through graph.facebook.com (/{IG_USER_ID}/media then "
                     "/media_publish). Keep the app in Development mode with you as admin; no App Review needed.")
    REMAINING.append("Instagram: videos/Reels must be reachable via a public HTTPS URL for the API to "
                     "fetch (or use resumable upload); local file paths won't work.")


# ------------------------------------------------------------- 5. Twitch
def step_twitch():
    header(5, "Twitch developer app (client ID + secret)")
    if already_set("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"):
        return
    say("""
    1. Twitch requires 2FA on your account: https://www.twitch.tv/settings/security
       -> enable Two-Factor Authentication if it isn't already.
    2. Open https://dev.twitch.tv/console/apps -> Register Your Application.
    3. Name: autoclip-<something unique>. OAuth Redirect URL: http://localhost
       Category: Application Integration. Client Type: Confidential. -> Create.
    4. Click 'Manage' on the new app. Copy the Client ID.
    5. Click 'New Secret' -> Confirm -> copy the secret (shown only once).
""")
    open_url("https://dev.twitch.tv/console/apps")

    def attempt():
        cid = ask("  Paste the Twitch Client ID")
        sec = ask("  Paste the Twitch Client Secret", secret=True)
        st, t = api_call("POST", "https://id.twitch.tv/oauth2/token",
                     form={"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"})
        if st != 200:
            say(f"  Rejected ({st}): {t if isinstance(t, str) else t.get('message')}")
            return False
        st, g = api_call("GET", "https://api.twitch.tv/helix/games/top?first=1",
                     {"Client-Id": cid, "Authorization": f"Bearer {t['access_token']}"})
        if st != 200:
            say(f"  Token issued but Helix call failed ({st}): {g}")
            return False
        say(f"  Twitch OK. Helix top game: {g['data'][0]['name']}")
        save_env(TWITCH_CLIENT_ID=cid, TWITCH_CLIENT_SECRET=sec)
        return True

    retry(attempt)
    REMAINING.append("Twitch: this validates an APP token only. Anything acting as you (e.g. creating "
                     "clips on your channel) needs a one-time user OAuth consent with the right scopes.")


STEPS = {1: step_google, 2: step_gemini, 3: step_telegram, 4: step_instagram, 5: step_twitch}


def main():
    global SHOW
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, nargs="+", choices=STEPS)
    ap.add_argument("--show", action="store_true", help="echo pasted secrets instead of hiding them")
    args = ap.parse_args()
    SHOW = args.show
    say(f"autoclip setup wizard. Keys will be saved to {ENV_PATH}")
    say("Ctrl+C at any time to stop; finished steps stay saved and are skippable on re-run.")
    try:
        for n in args.only or STEPS:
            STEPS[n]()
    except KeyboardInterrupt:
        say("\nStopped.")
    env = read_env()
    say("\n" + "=" * 70 + "\n SUMMARY\n" + "=" * 70)
    for label, keys in [("YouTube", ["YOUTUBE_REFRESH_TOKEN"]), ("Gemini", ["GEMINI_API_KEY"]),
                        ("Telegram", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]),
                        ("Instagram", ["IG_ACCESS_TOKEN", "IG_USER_ID"]),
                        ("Twitch", ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"])]:
        say(f"  {label:<10} {'saved' if all(env.get(k) for k in keys) else 'NOT SET'}")
    say("\n WHAT'S LEFT THAT ONLY YOU CAN DO")
    say("  - Keep .env and secrets/ out of git and cloud-sync folders; never share them.")
    for r in REMAINING:
        say(f"  - {r}")
    say("  - Rotate any key you pasted into a chat or screenshot.")


if __name__ == "__main__":
    main()
