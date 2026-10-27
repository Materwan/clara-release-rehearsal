"""Housekeeping and watching, on their own loops so one failing never stops the others.

* every 10 s: approval requests (push after a minute, expire after 24 h);
* every minute: the debug switch's hour; the disk (alert above CLARA_DISK_ALERT_PERCENT); the Discord bot's heartbeat
  (alert after 5 minutes of silence, when CLARA_DISCORD_EXPECTED or CLARA_DISCORD_AUTOSTART); the systemd watchdog,
  fed only while every loop has turned within the last two minutes;
* every 10 minutes: incognito conversations idle for CLARA_INCOGNITO_HOURS are deleted;
* every hour: events read by every listener, usage and audit rows older than CLARA_RETENTION_DAYS, expired web
  sessions, and the events of hooks older than 30 days (those a run still needs stay);
* every CLARA_MAIL_POLL_SECONDS (0: never): new mail in the connected mailboxes, when `mail_notify` is on. This loop
  waits for the mail providers, so the watchdog does not count it;
* every CLARA_GITHUB_POLL_SECONDS (0: never): pull requests and pushes in the repositories you added, while a hook waits
  for them. Same reason, same exception for the watchdog.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from ..events.delivery import DISCORD_SILENCE
from ..timeutil import iso, parse_iso, utcnow
from .status import disk_percent
from .systemd import notify, watchdog_seconds

if TYPE_CHECKING:
    from ..services import Services

log = logging.getLogger(__name__)

LOOP_STALL = 120.0


class Upkeep:
    def __init__(self, services: Services):
        self.s = services
        self.passes: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        for name, seconds, job in (
            ("approvals", 10.0, self.approvals),
            ("minute", 60.0, self.every_minute),
            ("incognito", 600.0, self.incognito),
            ("hourly", 3600.0, self.hourly),
        ):
            self._tasks.append(asyncio.create_task(self._loop(name, seconds, job), name=f"upkeep-{name}"))
        if self.s.settings.mail_poll_seconds > 0:
            self._tasks.append(
                asyncio.create_task(
                    self._loop("mail", float(self.s.settings.mail_poll_seconds), self.mail, watched=False),
                    name="upkeep-mail",
                )
            )
        if self.s.settings.github_poll_seconds > 0:
            self._tasks.append(
                asyncio.create_task(
                    self._loop("github", float(self.s.settings.github_poll_seconds), self.github, watched=False),
                    name="upkeep-github",
                )
            )
        interval = watchdog_seconds()
        if interval:
            self._tasks.append(asyncio.create_task(self._watchdog(interval), name="watchdog"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task

    async def _loop(self, name: str, seconds: float, job, *, watched: bool = True) -> None:
        while True:
            if watched:
                self.passes[name] = time.monotonic()
            try:
                result = job()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("upkeep %s failed", name)
            left = seconds
            while left > 0:  # in steps, so a loop that sleeps is not taken for one that is stuck
                await asyncio.sleep(min(left, LOOP_STALL / 4))
                left -= LOOP_STALL / 4
                if watched:
                    self.passes[name] = time.monotonic()

    # ------------------------------------------------------------------
    def approvals(self) -> None:
        self.s.approvals.sweep()

    async def mail(self) -> None:
        if self.s.connectors is not None:
            await self.s.connectors.watch.poll()

    async def github(self) -> None:
        if self.s.connectors is not None:
            await self.s.connectors.github_watch.poll()

    def every_minute(self) -> None:
        s = self.s
        until = s.runtime.get("debug_until")
        if until and parse_iso(until) <= utcnow():
            s.runtime.set("debug_until", "")
            s.traffic.end_debug()
        percent = disk_percent(s.settings.data_dir)
        if percent >= s.settings.disk_alert_percent:
            s.alerts.raise_(
                "disk", "The disk is nearly full", f"{percent}% of the disk holding {s.settings.data_dir} is used."
            )
        else:
            s.alerts.clear("disk")
        if (
            s.settings.discord_expected or s.settings.discord_autostart
        ) and time.monotonic() - s.started > DISCORD_SILENCE:
            seen = s.clients.get("discord")
            if seen is None or time.monotonic() - seen > DISCORD_SILENCE:
                s.alerts.raise_(
                    "discord",
                    "The Discord bot is gone",
                    "clara-discord has not checked in for 5 minutes (the server's log, or systemctl status clara-discord).",
                )

    def incognito(self) -> None:
        deleted = self.s.conversations.sweep_incognito(self.s.settings.incognito_hours)
        if deleted:
            log.info("deleted %d incognito conversation(s)", deleted)

    def hourly(self) -> None:
        s = self.s
        s.events.prune()
        s.usage.prune(s.settings.retention_days)
        limit = iso(utcnow() - timedelta(days=s.settings.retention_days))
        s.db.execute("DELETE FROM audit_log WHERE at < ?", (limit,))
        s.web_sessions.prune()
        if s.triggers is not None:
            s.triggers.prune(s.settings.retention_days)

    async def _watchdog(self, interval: float) -> None:
        while True:
            now = time.monotonic()
            loops = [loop.last_pass for loop in self.s.due_loops()] + list(self.passes.values())
            if all(now - last < LOOP_STALL for last in loops):
                notify("WATCHDOG=1")
            else:
                log.error("a loop has not turned for %g s: the watchdog is not fed", LOOP_STALL)
            await asyncio.sleep(interval)


def late_alert(services: Services):
    """What a due loop calls when something fired late: an alert, so you know it did not happen on time."""

    def late(what: str, seconds: float) -> None:
        minutes = int(seconds // 60)
        services.alerts.raise_(
            f"late:{what}",
            "Something ran late",
            f"{what} ran {minutes} min late (was the server stopped?).",
            cooldown=600,
        )

    return late
