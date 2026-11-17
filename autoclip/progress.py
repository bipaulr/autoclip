"""Live progress of the video being processed, for the dashboard's Live tab.

The pipeline calls report(stage, detail, pct); the worker stores it in Redis, the API reads it.
"""
import json
import time
from typing import Callable

KEY = "autoclip:progress"
Reporter = Callable[..., None]


def noop(*_, **__) -> None:
    pass


class RedisProgress:
    """Remembers which video is running and when each stage started; never raises."""

    def __init__(self, redis_url: str, video: dict, client=None):
        if client is None:
            import redis
            client = redis.Redis.from_url(redis_url)
        self.r, self.video, self.started = client, video, time.time()
        self.stages: list[dict] = []

    def __call__(self, stage: str, detail: str = "", pct: float | None = None) -> None:
        now = time.time()
        if not self.stages or self.stages[-1]["stage"] != stage:
            if self.stages:
                self.stages[-1]["ended"] = now
            self.stages.append({"stage": stage, "started": now})
        state = {"video": self.video, "stage": stage, "detail": detail,
                 "pct": None if pct is None else round(max(0.0, min(100.0, pct)), 1),
                 "started": self.started, "updated": now, "stages": self.stages, "running": True}
        try:
            self.r.set(KEY, json.dumps(state), ex=6 * 3600)
        except Exception:  # noqa: BLE001 - progress is cosmetic; never break the pipeline
            pass

    def finish(self, result: str) -> None:
        now = time.time()
        if self.stages:
            self.stages[-1].setdefault("ended", now)
        try:
            self.r.set(KEY, json.dumps({"video": self.video, "stage": "finished", "detail": result, "pct": 100,
                                        "started": self.started, "updated": now, "stages": self.stages,
                                        "running": False}), ex=24 * 3600)
        except Exception:  # noqa: BLE001
            pass


def read(client) -> dict | None:
    try:
        raw = client.get(KEY)
    except Exception:  # noqa: BLE001
        return None
    return json.loads(raw) if raw else None
