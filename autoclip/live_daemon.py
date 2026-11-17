"""Long-running process (docker compose service 'live'): watches the allowlisted channels, records each
one that goes live, reads its chat, and queues finished chunks for the pipeline within minutes.

    python -m autoclip.live_daemon

Rights gate: only channels from sources.yaml / joined campaigns are ever recorded (services.live)."""
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from autoclip.config import Settings, get_settings
from autoclip.db import get_session_factory
from autoclip.decisions import log_decision
from autoclip.integrations.downloader import probe_duration, run_cmd
from autoclip.integrations.feeds import FeedError, KickPublic, TwitchHelix
from autoclip.integrations.telegram import TelegramNotifier
from autoclip.integrations.live import (ChatReader, LiveError, Recorder, StreamlinkRecorder, TwitchAnonChat)
from autoclip.services import cleanup, focus
from autoclip.services.live import (LiveJob, allowed_channels, finalize_chunks, mark_recorded)
from autoclip.sources import load_sources

MODULE = "live"
ALERT_AFTER = 10  # consecutive failed live checks (~5 min) before a Telegram alert
log = logging.getLogger("autoclip.live")


class LiveManager:
    def __init__(self, settings: Settings, session_factory, *, twitch=None, kick=None,
                 recorder_factory: Callable[[], Recorder] = StreamlinkRecorder,
                 chat_factory: Callable[[], ChatReader] = TwitchAnonChat,
                 trigger: Callable[[list[str]], None] = lambda ids: None, sources=None,
                 clock: Callable[[], float] = time.time, run: Callable = run_cmd,
                 probe: Callable = probe_duration, notify: Callable[[str], None] = lambda text: None):
        self.s, self.session_factory = settings, session_factory
        self.twitch, self.kick = twitch, kick
        self.recorder_factory, self.chat_factory, self.trigger = recorder_factory, chat_factory, trigger
        self.sources, self.clock, self.run, self.probe = sources, clock, run, probe
        self.media = Path(settings.media_dir)
        self.jobs: dict[tuple[str, str], tuple[LiveJob, Recorder, ChatReader | None]] = {}
        self._last_cleanup = 0.0
        self.notify = notify
        self._failures: dict[str, int] = {}  # consecutive live-check failures per platform
        self._adopted = False
        self.keywords = [k.strip() for k in (getattr(settings, "priority_keywords", "") or "").split(",")
                         if k.strip()] or list(focus.DEFAULT_KEYWORDS)

    # ------------------------------------------------------------ one pass
    def tick(self) -> None:
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        with self.session_factory() as session:
            sources = self.sources or load_sources(self.s.sources_file)
            channels = allowed_channels(sources, session, now.date())
            created: list[str] = []
            if not self._adopted:
                self._adopted = True
                created += self._adopt(session, channels, now)
            live = self._live_now(session, channels)
            gta = {k for k, info in live.items() if focus.is_gta_stream(info, self.keywords)}
            for k in gta:
                focus.note_gta_stream(session, f"{k[0]}:{k[1]}", now)
            for k, (job, _, _) in self.jobs.items():
                job.gta = k in gta  # a streamer may switch games mid-stream
            focus.refresh(session, now, self.notify, self.keywords)
            # GTA streams first, so they get the recording slots
            for ch in sorted(channels, key=lambda c: (c.platform, c.channel_id.lower()) not in gta):
                key = (ch.platform, ch.channel_id.lower())
                if key in live and key not in self.jobs:
                    created += self._start(session, ch, key, now, gta=key in gta)
            for key in list(self.jobs):
                created += self._service(session, key, now)
            if self.clock() - self._last_cleanup > 600:
                self._last_cleanup = self.clock()
                cleanup.run_cleanup(session, self.media, self.min_free)
            session.commit()
        if created:
            self.trigger(created)

    @property
    def min_free(self) -> int:
        return int(self.s.live_min_free_gb * 10**9)

    def _live_now(self, session, channels) -> dict[tuple[str, str], dict]:
        """(platform, login) -> {"game", "title"} for every allowlisted channel live now."""
        live: dict[tuple[str, str], dict] = {}
        for platform, client in (("twitch", self.twitch), ("kick", self.kick)):
            logins = [c.channel_id for c in channels if c.platform == platform]
            if client is None or not logins:
                continue
            try:
                if hasattr(client, "live_streams"):
                    live.update({(platform, x.lower()): info for x, info in client.live_streams(logins).items()})
                else:  # Kick: no game data, the title still counts
                    live.update({(platform, x.lower()): {"game": "", "title": ""}
                                 for x in client.live_logins(logins)})
            except FeedError as e:
                n = self._failures[platform] = self._failures.get(platform, 0) + 1
                if n == 1 or n % 10 == 0:  # first failure and every 10th: no log spam every 30 s
                    log_decision(session, MODULE, "paused", f"{platform} live check failed ({n} in a row): {e}")
                if n == ALERT_AFTER:
                    self.notify(f"autoclip live: {platform} live check has failed {n} times in a row "
                                f"(~{n // 2} min). Streams on {platform} are NOT being recorded. Last error: {e}")
                continue
            if self._failures.get(platform, 0) >= ALERT_AFTER:
                self.notify(f"autoclip live: {platform} live check works again")
            if self._failures.get(platform):
                log_decision(session, MODULE, "resumed", f"{platform} live check works again "
                                                         f"after {self._failures[platform]} failure(s)")
            self._failures[platform] = 0
        return live

    def _adopt(self, session, channels, now) -> list[str]:
        """Recordings left on disk by a previous run (daemon or Docker restart): their recorder is gone,
        so every chunk in them is finished. Queue them instead of letting cleanup delete them unclipped.
        Their chat was held in memory and is lost, so these chunks rely on loudness and the transcript."""
        root, created = self.media / "live", []
        if not root.is_dir():
            return created
        by_id = {c.channel_id.lower(): c for c in channels}
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            login, _, ts = d.name.rpartition("_")
            ch = by_id.get(login.lower())
            if ch is None or not ts.isdigit():
                log_decision(session, MODULE, "skipped", f"leftover recording '{d.name}' is not an allowlisted "
                                                         "channel; left for cleanup")
                continue
            job = LiveJob(ch, d, float(ts), self.s.live_chunk_seconds, chat_lag_s=self.s.live_chat_lag_s)
            ids = finalize_chunks(session, job, self.media, now, recording=False, run=self.run, probe=self.probe)
            log_decision(session, MODULE, "recovered", f"picked up {len(ids)} chunk(s) of '{ch.name}' left by "
                                                       "a previous run (no chat signal for them)",
                         subject=f"{ch.platform}:{ch.channel_id}")
            created += ids
        return created

    def _start(self, session, ch, key, now, gta: bool = False) -> list[str]:
        subject, created = f"{ch.platform}:{ch.channel_id}", []
        if len(self.jobs) >= self.s.live_max_streams and gta:
            victim = next((k for k, (j, _, _) in self.jobs.items() if not j.gta), None)
            if victim is not None:  # GTA focus: a non-GTA recording makes room
                vjob, vrec, _ = self.jobs[victim]
                vrec.stop()
                created += self._service(session, victim, now)
                log_decision(session, MODULE, "preempted", f"stopped recording '{vjob.channel.name}' (not GTA) "
                                                           f"to record '{ch.name}' (GTA 6)", subject=subject)
        if len(self.jobs) >= self.s.live_max_streams:
            log_decision(session, MODULE, "skipped", f"'{ch.name}' is live but {self.s.live_max_streams} "
                                                     "streams are already being recorded", subject=subject)
            return created
        free = cleanup.free_bytes(self.media)
        if free is not None and free < self.min_free:
            cleanup.run_cleanup(session, self.media, self.min_free)
            free = cleanup.free_bytes(self.media)
        if free is not None and free < self.min_free:
            log_decision(session, MODULE, "paused", f"'{ch.name}' is live but only {free / 1e9:.1f} GB is free "
                                                    f"(needs {self.s.live_min_free_gb} GB)", subject=subject)
            return created
        folder = self.media / "live" / f"{ch.channel_id}_{int(self.clock())}"
        rec, chat = self.recorder_factory(), None
        try:
            rec.start(ch.platform, ch.channel_id, folder, self.s.live_chunk_seconds)
        except LiveError as e:
            log_decision(session, MODULE, "failed", f"could not record '{ch.name}': {e}", subject=subject)
            return created
        if ch.platform == "twitch":  # anonymous chat reading exists for Twitch only
            chat = self.chat_factory()
            chat.start(ch.channel_id)
        self.jobs[key] = (LiveJob(ch, folder, self.clock(), self.s.live_chunk_seconds,
                                     chat_lag_s=self.s.live_chat_lag_s, gta=gta), rec, chat)
        mark_recorded(session, ch, now)
        log_decision(session, MODULE, "live_started",
                     f"'{ch.name}' went live: recording in {self.s.live_chunk_seconds // 60}-minute chunks"
                     + (" with chat" if chat else " (no chat signal on this platform)")
                     + (" [GTA 6]" if gta else ""), subject=subject)
        return created

    def _service(self, session, key, now) -> list[str]:
        job, rec, chat = self.jobs[key]
        if chat is not None:
            job.chat += chat.drain()
        running = rec.is_running()
        ids = finalize_chunks(session, job, self.media, now, recording=running, run=self.run,
                              probe=self.probe)
        if not running:
            if chat is not None:
                chat.stop()
            del self.jobs[key]
            log_decision(session, MODULE, "live_ended", f"'{job.channel.name}' stopped; recording closed",
                         subject=f"{job.channel.platform}:{job.login}")
        return ids

    def shutdown(self) -> None:
        for job, rec, chat in self.jobs.values():
            rec.stop()
            if chat is not None:
                chat.stop()

    def run_forever(self, every_s: int = 30) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive any single failure
                log.exception("live tick failed")
            time.sleep(every_s)


def trigger_processing(ids: list[str]) -> None:
    from autoclip.tasks import process_live_task
    process_live_task.delay()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs can carry tokens
    s = get_settings()
    if not s.live_enabled:
        log.info("LIVE_ENABLED is false; idle")
        while True:
            time.sleep(3600)
    twitch = TwitchHelix(s.twitch_client_id, s.twitch_client_secret) if s.twitch_client_id else None
    notifier = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)
    mgr = LiveManager(s, get_session_factory(), twitch=twitch, kick=KickPublic(),
                      recorder_factory=lambda: StreamlinkRecorder(s.live_quality, segment_threads=s.live_segment_threads), trigger=trigger_processing,
                      notify=notifier.send)
    try:
        mgr.run_forever()
    finally:
        mgr.shutdown()


if __name__ == "__main__":
    main()
