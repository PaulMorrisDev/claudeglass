"""Corpus assembly (WP11): turn a list of project directories into every
top-level session's parsed transcripts, subagent transcripts and workflow
runs, optionally through the on-disk digest cache and/or a process pool.

:func:`load_corpus` is the one entry point every later report-assembly
package (and the CLI's ``cli.py``, via ``_load_corpus_for_args``) calls
instead of hand-rolling ``discovery``/``parse``/``workflows`` plumbing
itself. It:

- Enumerates sessions per project directory via ``discovery.find_sessions``,
  their subagent transcripts via ``discovery.find_subagents`` (both the
  ordinary and workflow-nested shapes — see ``discovery.py``'s and
  ``workflows.py``'s module docstrings), and their workflow run files via
  ``discovery.find_workflows``.
- Parses every transcript via ``parse.parse_transcript``, consulting
  ``cache`` (a ``cache.DigestCache``, optional) first: a hit is used as-is,
  a miss is parsed and, unless the file is "live" (see ``cache.py``'s
  module docstring), written back to the cache. The cache is only ever
  touched in this (the parent/caller's) process — see the ``jobs``
  paragraph below.
- Links each session's workflow runs to their own subagent transcripts via
  ``workflows.parse_workflow_file``/``workflows.link_workflow_agents``,
  which needs a resolved rate card purely to total each run's cost; this
  module loads the one ``config.toml``'s ``pricing_path`` names, else the
  config-dir or packaged ``pricing.toml``, for that purpose alone — see
  :func:`_default_rates`.

``jobs`` (``concurrent.futures.ProcessPoolExecutor`` when > 1): every
cache miss (top-level and subagent transcripts alike) is submitted to the
pool as an independent ``(path, meta)`` parse job; the cache itself is
never handed to a worker (workers only ever call ``parse_transcript`` —
see :func:`_parse_worker`, which must stay a plain module-level function,
never a lambda or closure, so it can be pickled and re-imported by a
spawned worker process under Windows' default ``spawn`` start method).
The parent process is solely responsible for consulting and updating the
cache, both before dispatching (a hit never gets submitted at all) and
after a worker returns (the result is written back). This keeps the same
corpus, run with ``jobs=1`` or ``jobs=4``, cache cold or warm, producing
byte-identical parsed output — the CLI's ``--jobs`` flag only changes how
fast that output is produced, never what it is.

Sessions in the returned ``Corpus`` are always sorted by (earliest
observed ``Turn.ts`` across the session's own top-level and subagent
transcripts, then ``session_id``) — computed from the parsed data itself,
never from job-completion order — so the assembled corpus is deterministic
regardless of ``jobs`` or how much of it came from the cache.

``exclude_projects`` (same convention as ``discovery.resolve_project_dirs``
and ``Config.exclude_projects``: a list of slug regexes, ``re.search``,
case-insensitive) is re-applied to ``project_dirs`` here as a second,
independent filter — a caller may hand this function directories it
resolved itself (or a fixed test list) without having run them through
``discovery.resolve_project_dirs`` first, and a "never touch this
project" list should hold regardless of how the caller got its directory
list. A malformed regex is skipped, not fatal (same posture
``resolve_project_dirs`` and ``config.py`` already take for foreign
shapes).
"""

from __future__ import annotations

import concurrent.futures
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config as config_mod
from . import discovery
from . import parse as parse_mod
from .cache import DigestCache
from .model import TranscriptMeta, TranscriptResult, WorkflowRun
from .parse import parse_transcript
from .pricing import Pricing, PricingError, load_pricing
from .workflows import link_workflow_agents, parse_workflow_file

# -- Corpus / SessionBundle -------------------------------------------------


@dataclass(slots=True)
class SessionBundle:
    """One top-level session's parsed transcripts and workflow runs."""

    session_id: str = ""
    slug: str = ""
    top: TranscriptResult | None = None
    subs: list[TranscriptResult] = field(default_factory=list)
    workflows: list[WorkflowRun] = field(default_factory=list)
    project_dir: str = ""


@dataclass(slots=True)
class Corpus:
    """Every session ``load_corpus`` assembled, plus run totals."""

    sessions: list[SessionBundle] = field(default_factory=list)
    total_files: int = 0
    total_bytes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    elapsed_s: float = 0.0


# -- internal plumbing -------------------------------------------------------


@dataclass(slots=True)
class _SessionSpec:
    session_id: str
    slug: str
    project_dir: str
    top_path: Path
    top_meta: TranscriptMeta
    #: (jsonl_path, meta) pairs, in ``discovery.find_subagents`` order.
    sub_specs: list[tuple[Path, TranscriptMeta]]
    workflow_paths: list[Path]


def _filter_excluded_dirs(project_dirs, exclude_projects) -> list[Path]:
    dirs = [Path(p) for p in project_dirs]
    if not exclude_projects:
        return dirs
    patterns = []
    for raw_pattern in exclude_projects:
        try:
            patterns.append(re.compile(raw_pattern, re.IGNORECASE))
        except re.error:
            continue
    if not patterns:
        return dirs
    return [d for d in dirs if not any(p.search(d.name) for p in patterns)]


def _build_top_meta(top_path: Path, session_id: str, project_slug: str) -> TranscriptMeta:
    meta = TranscriptMeta(
        path=str(top_path),
        kind="top-level",
        session_id=session_id,
        project_slug=project_slug,
    )
    try:
        stat = top_path.stat()
    except OSError:
        return meta
    meta.mtime_ns = stat.st_mtime_ns
    meta.size_bytes = stat.st_size
    return meta


def _collect_specs(
    project_dirs: list[Path],
    days: int | None,
    since: str | None,
    until: str | None,
    limit: int | None,
    window_by: str,
    subagent_window: str,
) -> list[_SessionSpec]:
    specs: list[_SessionSpec] = []
    for project_dir in project_dirs:
        slug = project_dir.name
        for top_path in discovery.find_sessions(project_dir, days, since, until, limit, window_by):
            session_id = top_path.stem
            top_meta = _build_top_meta(top_path, session_id, slug)

            sub_specs: list[tuple[Path, TranscriptMeta]] = []
            for jsonl_path in discovery.find_subagent_paths(project_dir, session_id, since, until, subagent_window):
                meta_path = jsonl_path.with_name(jsonl_path.stem + ".meta.json")
                sub_specs.append((jsonl_path, discovery.load_meta(meta_path)))

            workflow_paths = discovery.find_workflows(project_dir, session_id)

            specs.append(
                _SessionSpec(
                    session_id=session_id,
                    slug=slug,
                    project_dir=str(project_dir),
                    top_path=top_path,
                    top_meta=top_meta,
                    sub_specs=sub_specs,
                    workflow_paths=workflow_paths,
                )
            )
    return specs


def _parse_worker(path: str | Path, meta: TranscriptMeta) -> TranscriptResult:
    """Module-level ``ProcessPoolExecutor`` worker (``jobs > 1``): parses
    one transcript and nothing else — no cache access happens in a worker
    process (see the module docstring). Must stay a plain top-level
    function (no lambda/closure) so it can be pickled and re-imported by
    a spawned worker under Windows' ``spawn`` start method.
    """
    return parse_transcript(path, meta)


def _default_rates(config_dir: str | Path | None = None) -> Pricing:
    """The rate card ``config.toml``'s ``pricing_path`` names, else
    ``<config_dir>/pricing.toml``, else the packaged default: the card the
    report and the service read (``config_dir`` defaults as
    ``load_pricing``'s does; the CLI's own ``--pricing`` card arrives as
    ``load_corpus``'s ``rates`` instead). Used only to total each
    ``WorkflowRun.cost`` via ``workflows.link_workflow_agents``. A
    ``pricing_path`` that can't be read falls back to the other two, and
    when that card can't be read either (a malformed ``pricing.toml``, or
    a corrupted install) this degrades to an all-unknown rate card rather
    than failing corpus assembly outright — every ``price_turn`` call
    against it simply prices at zero with ``model_known=False`` (see
    ``pricing.price_turn``'s own docstring), so a workflow's ``cost``
    becomes 0.0 instead of crashing the load.
    """
    try:
        return load_pricing(path=config_mod.saved_pricing_path(config_dir), config_dir=config_dir)
    except (config_mod.ConfigError, PricingError):
        pass
    try:
        return load_pricing(config_dir=config_dir)
    except PricingError:
        return Pricing(
            path="none",
            version="none",
            currency="USD",
            source_url=None,
            retrieved=None,
            notes=None,
            sha256="",
        )


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _session_first_ts(bundle: SessionBundle) -> str | None:
    """Earliest ``Turn.ts`` across the session's top-level and subagent
    transcripts, as the original ISO string — the sort key
    :func:`_session_sort_key` uses so corpus assembly order never depends
    on ``jobs`` or cache hit/miss timing (see the module docstring).

    Only successfully-parsed timestamps are compared against each other
    (same "skip what fails to parse" posture as ``classify._ts_range``),
    so this never mixes naive and timezone-aware ``datetime`` values in
    one comparison; an unparseable-but-present ``ts`` falls back to
    "first one seen" only when nothing at all parsed.
    """
    parsed: list[tuple[datetime, str]] = []
    first_raw: str | None = None
    transcripts = ([bundle.top] if bundle.top is not None else []) + bundle.subs
    for result in transcripts:
        for turn in result.turns:
            if not turn.ts:
                continue
            if first_raw is None:
                first_raw = turn.ts
            dt = _parse_ts(turn.ts)
            if dt is not None:
                parsed.append((dt, turn.ts))
    if parsed:
        return min(parsed, key=lambda pair: pair[0])[1]
    return first_raw


def _session_last_ts(bundle: SessionBundle) -> str | None:
    """Latest ``Turn.ts`` across the session's top-level and subagent
    transcripts (the session's last reply), as the original ISO string;
    ``None`` when no turn has a readable time."""
    latest: tuple[datetime, str] | None = None
    transcripts = ([bundle.top] if bundle.top is not None else []) + bundle.subs
    for result in transcripts:
        for turn in result.turns:
            dt = _parse_ts(turn.ts)
            if dt is not None and (latest is None or dt > latest[0]):
                latest = (dt, turn.ts)
    return latest[1] if latest else None


def _session_sort_key(bundle: SessionBundle) -> tuple:
    first_ts = _session_first_ts(bundle)
    # ``None`` first_ts sorts after every known one, but still
    # deterministically (by session_id) among themselves.
    return (first_ts is None, first_ts or "", bundle.session_id)


# -- load_corpus --------------------------------------------------------


def load_corpus(
    project_dirs,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    window_by: str = "last-reply",
    subagent_window: str = "parent",
    cache: DigestCache | None = None,
    jobs: int = 1,
    exclude_projects=(),
    progress: Callable[[int, int], None] | None = None,
    salt: bytes | None = None,
    config_dir: str | Path | None = None,
    rates: Pricing | None = None,
) -> Corpus:
    """Assemble a :class:`Corpus` from ``project_dirs``. See the module
    docstring for the full algorithm; parameters mirror
    ``discovery.find_sessions``/``find_subagents`` (``days``/``since``/
    ``until``/``limit``/``window_by``/``subagent_window``) plus the cache
    and parallelism knobs (``cache``/``jobs``) and a standing exclusion
    list (``exclude_projects``). ``config_dir`` is the folder whose
    ``config.toml``/``pricing.toml`` choose the rate card workflow runs
    are costed with (see :func:`_default_rates`); ``None`` means the
    default one, and ``rates``, when given, is that card already loaded
    (the CLI's ``--pricing``), so it wins over ``config_dir``'s. ``progress``, when given, is called as
    ``progress(done, total)`` once per transcript file as it's resolved
    (cache hit or freshly parsed) — ``total`` is fixed for the whole call,
    ``done`` only ever increases.

    ``salt`` (fix #8), when given, wires up ``Turn.read_target_hashes``:
    with ``jobs == 1`` this process calls ``parse.set_salt(salt)`` directly
    before parsing any pending transcript; with ``jobs > 1`` it is instead
    passed as the ``ProcessPoolExecutor``'s own ``initializer``/``initargs``,
    so every spawned worker calls ``parse.set_salt(salt)`` exactly once
    before parsing its first job — without this, a ``jobs > 1`` run would
    silently produce empty ``read_target_hashes`` for every transcript
    while a ``jobs == 1`` run on the same corpus produced full ones (the
    exact defect fix #8 describes). ``None`` (the default) leaves hashing
    off entirely, matching ``parse.set_salt``'s own "never called" contract.
    """
    start = time.monotonic()

    filtered_dirs = _filter_excluded_dirs(project_dirs, exclude_projects)
    specs = _collect_specs(filtered_dirs, days, since, until, limit, window_by, subagent_window)

    jobs_list: list[tuple[Path, TranscriptMeta]] = []
    for spec in specs:
        jobs_list.append((spec.top_path, spec.top_meta))
        jobs_list.extend(spec.sub_specs)

    total = len(jobs_list)
    done = 0

    def _tick() -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total)

    results: dict[str, TranscriptResult] = {}
    pending: list[tuple[Path, TranscriptMeta]] = []
    total_bytes = 0
    cache_hits = 0
    cache_misses = 0

    for path, meta in jobs_list:
        total_bytes += meta.size_bytes
        hit = cache.get(path, meta) if cache is not None else None
        if hit is not None:
            results[str(path)] = hit
            if cache is not None:
                cache_hits += 1
            _tick()
        else:
            if cache is not None:
                cache_misses += 1
            pending.append((path, meta))

    if pending:
        if jobs > 1:
            pool_kwargs = {"max_workers": jobs}
            if salt is not None:
                pool_kwargs["initializer"] = parse_mod.set_salt
                pool_kwargs["initargs"] = (salt,)
            with concurrent.futures.ProcessPoolExecutor(**pool_kwargs) as executor:
                future_map = {
                    executor.submit(_parse_worker, path, meta): (path, meta) for path, meta in pending
                }
                for future in concurrent.futures.as_completed(future_map):
                    path, meta = future_map[future]
                    result = future.result()
                    results[str(path)] = result
                    if cache is not None:
                        cache.put(path, meta, result)
                    _tick()
        else:
            if salt is not None:
                parse_mod.set_salt(salt)
            for path, meta in pending:
                result = parse_transcript(path, meta)
                results[str(path)] = result
                if cache is not None:
                    cache.put(path, meta, result)
                _tick()

    rates_lookup = rates if rates is not None else _default_rates(config_dir)
    bundles: list[SessionBundle] = []
    for spec in specs:
        top_result = results[str(spec.top_path)]
        sub_results = [results[str(path)] for path, _meta in spec.sub_specs]

        workflow_runs: list[WorkflowRun] = []
        for workflow_path in spec.workflow_paths:
            run = parse_workflow_file(workflow_path)
            link_workflow_agents(run, sub_results, rates_lookup)
            workflow_runs.append(run)

        bundles.append(
            SessionBundle(
                session_id=spec.session_id,
                slug=spec.slug,
                top=top_result,
                subs=sub_results,
                workflows=workflow_runs,
                project_dir=spec.project_dir,
            )
        )

    if window_by == "last-reply":
        # find_sessions kept every file written since the window opened;
        # keep only the sessions whose last reply is in the window.
        since_dt, until_dt = discovery._resolve_window(days, since, until)
        kept = [
            (spec, bundle)
            for spec, bundle in zip(specs, bundles)
            if discovery.ts_in_window(_session_last_ts(bundle), since_dt, until_dt)
        ]
        if len(kept) != len(bundles):
            bundles = [bundle for _spec, bundle in kept]
            total = sum(1 + len(spec.sub_specs) for spec, _bundle in kept)
            total_bytes = sum(
                spec.top_meta.size_bytes + sum(meta.size_bytes for _path, meta in spec.sub_specs)
                for spec, _bundle in kept
            )

    bundles.sort(key=_session_sort_key)

    return Corpus(
        sessions=bundles,
        total_files=total,
        total_bytes=total_bytes,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        elapsed_s=time.monotonic() - start,
    )


__all__ = ["SessionBundle", "Corpus", "load_corpus"]
