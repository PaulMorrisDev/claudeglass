"""While the service runs, keep ``coaching.json`` (your own split points,
plan habit and typical piece of work, for the capture hook's coaching notes
and feedback items) up to date.

:class:`CoachingJob` checks once the watcher's first scan is done (so
a first run doesn't work from a half-filled store) and then every
:data:`CHECK_INTERVAL_S` seconds, on its own daemon thread. It does
nothing while coaching notes (``[capture] coaching`` lacks
``coaching_notes``) and the feedback items on your messages (the facts
line, the plan check and the rating reminder: ``[capture] feedback``) are
all off. Otherwise, once the file is missing or older than
``coaching.MAX_AGE_HOURS``, it builds a report of the last
``coaching.DAYS`` days across every project from the store (no
transcript read) and writes the file (``coaching.from_report``). A
failure is logged to stderr and retried at the next check -- it never
stops ``serve``. The clock is injectable (``now_fn``) so tests can move
it. Same shape as ``monthly_job.MonthlyReportJob``.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .contracts import ServeOptions

#: How often (seconds) the job checks the file's age: hourly, and a
#: check costs one small file read.
CHECK_INTERVAL_S = 3600.0
#: How often (seconds) it looks whether the first scan is done.
READY_POLL_S = 30.0

_LOG_PREFIX = "claudeglass serve: coaching split points"


class CoachingJob:
    """Rewrites ``coaching.json`` once a day while coaching notes or a feedback
    item on your messages are on.
    :meth:`run_once` is one check (tests and ``serve --once`` call it
    directly); :meth:`start`/:meth:`stop` run it on a background thread.
    ``build_report`` builds a report over ``days`` days from the store,
    ``build_typical`` the tokens of your typical piece of work over the same
    days (``coaching.typical_piece_tokens``; 0 without it); ``ready`` says
    whether the store is filled enough to start."""

    def __init__(
        self,
        options: ServeOptions,
        build_report: Callable[[int], object],
        *,
        build_typical: Callable[[int], int] | None = None,
        ready: Callable[[], bool] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        check_interval_s: float = CHECK_INTERVAL_S,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.options = options
        self._build_report = build_report
        self._build_typical = build_typical
        self._ready = ready or (lambda: True)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._check_interval_s = check_interval_s
        self._log = log or (lambda text: print(text, file=sys.stderr, flush=True))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> Path | None:
        """One check: rewrite the file when coaching notes are on and it is
        missing or stale. Returns the path written, else ``None``. Never
        raises."""
        from .. import coaching
        from ..config import load_config

        try:
            config_dir = self.options.config_dir
            config = load_config(config_dir)
            if not (config.capture.coaching_notes_on or config.capture.feedback_prompts_on):
                return None
            now = self._now_fn()
            age = coaching.age_hours(config_dir, now)
            if age is not None and 0 <= age < coaching.MAX_AGE_HOURS:
                return None
            report = self._build_report(coaching.DAYS)
            typical = self._build_typical(coaching.DAYS) if self._build_typical is not None else 0
            written = coaching.write(
                config_dir, coaching.from_report(report, config_dir, config.thresholds, now=now, typical=typical)
            )
        except Exception as exc:  # noqa: BLE001 -- a background job must never take serve down
            self._log(f"{_LOG_PREFIX} not written: {exc}")
            return None
        return written

    def _loop(self) -> None:
        while not self._ready():
            if self._stop.wait(READY_POLL_S):
                return
        self.run_once()
        while not self._stop.wait(self._check_interval_s):
            self.run_once()

    def start(self) -> None:
        """Check once ``ready`` and then every ``check_interval_s``
        seconds, on a daemon thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="coaching", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        """Stop checking. Idempotent."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None


__all__ = ["CoachingJob", "CHECK_INTERVAL_S", "READY_POLL_S"]
