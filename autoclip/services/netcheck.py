"""Network speed check from inside Docker.

On 16 Nov 2026 Docker Desktop started capping every container connection at ~80 KB/s (the PC itself still
had 10 MB/s). Live recording fell behind and lost most of the stream, and a 2.5 GB VOD crawled for hours.
A Docker Desktop restart cleared it. This check measures a download every 15 minutes and alerts on
Telegram after two slow readings in a row, and again when the speed is back.
"""
import time
from datetime import datetime
from typing import Callable

import httpx
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.integrations.telegram import Notifier
from autoclip.services.settings_store import get_setting, set_setting

MODULE = "netcheck"
KEY = "netcheck.state"
PROBE_URL = "https://speed.cloudflare.com/__down?bytes=8000000"  # public speed-test file, no data sent
MIN_BPS = 1_000_000  # 1 MB/s: live 720p needs ~0.45 MB/s, and a VOD download is still running alongside
STRIKES = 2


def measure(client: httpx.Client | None = None, url: str = PROBE_URL, limit_s: float = 12.0,
            clock: Callable[[], float] = time.monotonic) -> float:
    """Bytes per second for one download (stops after limit_s). 0.0 when it fails."""
    client = client or httpx.Client(timeout=limit_s + 5)
    got, start = 0, clock()
    try:
        with client.stream("GET", url) as r:
            if r.status_code != 200:
                return 0.0
            for chunk in r.iter_bytes():
                got += len(chunk)
                if clock() - start > limit_s:
                    break
    except httpx.HTTPError:
        return 0.0
    return got / max(clock() - start, 1e-6)


def check(session: Session, notifier: Notifier, now: datetime, speed: float, *, min_bps: float = MIN_BPS,
          strikes: int = STRIKES) -> str:
    state = dict(get_setting(session, KEY, {}) or {})
    slow, alerted = int(state.get("slow", 0)), bool(state.get("alerted"))
    shown = f"{speed / 1e6:.2f} MB/s" if speed >= 1e5 else f"{speed / 1e3:.0f} KB/s"
    if speed < min_bps:
        slow += 1
        log_decision(session, MODULE, "slow", f"download speed inside Docker is {shown} "
                                              f"({slow} slow reading(s) in a row)")
        if slow >= strikes and not alerted:
            notifier.send(f"autoclip: the internet inside Docker is slow ({shown}; normal is ~9 MB/s), "
                          f"{slow} checks in a row. Live recording and VOD downloads fall behind. "
                          "Fix: restart Docker Desktop (PowerShell:  docker desktop restart ). "
                          "Wait until no upload is running.")
            alerted = True
    else:
        if alerted:
            notifier.send(f"autoclip: internet inside Docker is back to normal ({shown}).")
            log_decision(session, MODULE, "recovered", f"download speed inside Docker is back to {shown}")
        slow, alerted = 0, False
    set_setting(session, KEY, {"slow": slow, "alerted": alerted, "last_bps": round(speed),
                               "at": now.isoformat()})
    return shown
