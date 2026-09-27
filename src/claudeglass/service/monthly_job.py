"""``serve --monthly-report DIR``: while the service runs, write the
previous calendar month's report into ``DIR`` once, when it is missing.

The report is the same one ``claudeglass monthly-report --out DIR``
writes (both go through :func:`claudeglass.monthly.run_monthly_report`),
covering every project the watcher scans: all of ``--projects-root``
minus ``config.toml``'s ``exclude_projects`` and ``--exclude-project``.

:class:`MonthlyReportJob` checks once when ``serve`` starts and then every
:data:`CHECK_INTERVAL_S` seconds, on its own daemon thread, so building a
report never holds up a request or the watcher. A month is "done" when
both of its files (``claudeglass-YYYY-MM.md``/``.html``) exist in
``DIR``; a report you delete is written again at the next check. A
failure (an unreadable ``config.toml``, no sessions yet, a full disk) is
logged to stderr and retried at the next check -- it never stops
``serve``. The clock is injectable (``now_fn``) so tests can move it.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import haiku_tags
from .contracts import ServeOptions

#: How often (seconds) the job checks whether last month's report is
#: missing. Hourly is plenty for a once-a-month file and costs nothing
#: when the files already exist (two ``stat`` calls).
CHECK_INTERVAL_S = 3600.0

_LOG_PREFIX = "claudeglass serve: monthly report"


class MonthlyReportJob:
    """Writes the previous month's report into
    ``options.monthly_report_dir`` when it is missing. :meth:`run_once`
    is one check (tests and ``serve --once`` call it directly);
    :meth:`start`/:meth:`stop` run it on a background thread.
    """

    def __init__(
        self,
        options: ServeOptions,
        *,
        now_fn: Callable[[], datetime] | None = None,
        check_interval_s: float = CHECK_INTERVAL_S,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if options.monthly_report_dir is None:
            raise ValueError("MonthlyReportJob needs options.monthly_report_dir")
        self.options = options
        self.out_dir = Path(options.monthly_report_dir)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._check_interval_s = check_interval_s
        self._log = log or (lambda text: print(text, file=sys.stderr, flush=True))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list[Path]:
        """One check: write last month's report when either file is
        missing. Returns the paths written (empty when the report was
        already there or could not be written). Never raises."""
        from .. import discovery
        from .. import monthly as monthly_mod
        from ..cache import DigestCache
        from ..config import load_config
        from ..corpus import load_corpus
        from ..parse import load_or_create_salt
        from ..pricing import load_pricing

        month = "the previous month"
        try:
            config_dir = self.options.config_dir
            config = load_config(config_dir)
            month = monthly_mod.resolve_month(None, config.tz, now=self._now_fn())
            if all(path.is_file() for path in monthly_mod.report_paths(self.out_dir, month)):
                return []
            pricing = load_pricing(path=config.pricing_path, config_dir=config_dir)
            exclude = [*config.exclude_projects, *self.options.exclude_projects]
            roots = [self.options.projects_root, *self.options.extra_projects_roots]
            root = ", ".join(str(r) for r in roots)
            project_dirs = discovery.resolve_project_dirs(roots, all_projects=True, exclude_projects=exclude)
            salt = load_or_create_salt(config_dir)
            cache = DigestCache(config_dir, salt=salt)
            paths = monthly_mod.run_monthly_report(
                config=config,
                pricing=pricing,
                config_dir=config_dir,
                root=root,
                project_dirs=project_dirs,
                month=month,
                out_dir=self.out_dir,
                load_corpus=lambda dirs: _with_haiku_tags(
                    load_corpus(dirs, cache=cache, exclude_projects=exclude, salt=salt), config_dir
                ),
                note=lambda text: self._log(f"{_LOG_PREFIX}: {text}"),
            )
        except Exception as exc:  # noqa: BLE001 -- a background job must never take serve down
            self._log(f"{_LOG_PREFIX} for {month} not written: {exc}")
            return []
        self._log(f"{_LOG_PREFIX} for {month} written to {self.out_dir}")
        return paths

    def _loop(self) -> None:
        self.run_once()
        while not self._stop.wait(self._check_interval_s):
            self.run_once()

    def start(self) -> None:
        """Check now and then every ``check_interval_s`` seconds, on a
        daemon thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="monthly-report", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        """Stop checking. Waits up to ``timeout`` seconds for a report
        being written right now; the thread is a daemon, so a slow one
        never keeps the process alive. Idempotent."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None


def _with_haiku_tags(corpus, config_dir):
    """``corpus`` with the tags Claude Haiku wrote put on their replies."""
    haiku_tags.apply(corpus, config_dir)
    return corpus


__all__ = ["MonthlyReportJob", "CHECK_INTERVAL_S"]
