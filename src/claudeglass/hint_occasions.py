"""Whether a coaching hint could have fired: the moments the capture hook's
own triggers held, and how many of them got a tip.

An *occasion* is a message of yours where the trigger of a live hint held
(the hook's ``_drip_hint``, ``_paste_hint`` and ``_poll_hint``), worked out
from the parsed transcript before the hook's rests: the hook gives one hint
a message and rests each kind for a while, so an occasion is not a promise of
a note. The unit that answers "does the hook work" is the session: its first
occasion always passes the rest, so ``sessions`` with a moment and
``noted`` of them with a note say it, not ``notes`` over ``occasions``.

Which sessions and which time count. The hints are the three that can speak
(``tests/agreement.live_hints``); a hint switched off for everyone has no
count. A session counts when the hook would have run in it: not a headless
run (``CLAUDE_CODE_ENTRYPOINT`` starting ``sdk``), in a project ``[capture]
projects`` and ``exclude_projects`` leave in, with a message of yours. A
message counts when coaching notes were on at the time it was typed
(``coaching_spans``, from the capture log) and it falls in the window.
Notes are cut the same way, by their own time. A message sent while Claude
worked, a /cg-feedback run and a message with a picture in it (for
``big_paste``) are none, as the hook gives them no hint.

Everything here is a count: no word of a message, a note or a mod event
reaches a result but as a number. ``collect`` always holds all three hints,
zeros included, so a missing hint cannot read as zero; this differs from
``capture.hints`` in the tuning export, which leaves out an all-zero row.
"""

from __future__ import annotations

import bisect
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import capture as capture_mod
from . import capture_catalogue
from . import coaching as coaching_mod
from . import config as config_mod
from . import hook_health, measured, prompting
from .hooks import capture_hook
from .model import EventKind

#: The coaching hints that can speak, in the order the catalogue lists them
#: (``tests/agreement.live_hints``, held to it by a test).
HINTS = ("drip_feed", "big_paste", "status_poll")
#: How the Capture page line names each, short enough for one sentence.
LABELS = {"drip_feed": "small requests", "big_paste": "a big paste", "status_poll": "a status check"}
#: ``CLAUDE_CODE_ENTRYPOINT`` of a run with nobody at the screen
#: (``capture_hook._HEADLESS_ENTRYPOINT``; a test holds them equal).
HEADLESS_PREFIX = "sdk"
#: The mod's present surfaces where it may draw the hint line itself.
MOD_HINT_SURFACES = tuple(s for s in capture_catalogue.SENSOR_PRESENT_SURFACES if s != "desktop")
#: Sessions with an occasion and no tip before the Capture page says so.
#: One session can be silent for a reason that is no fault (the hook gives
#: one hint a message, a parallel hook call can lose the claim, a background
#: launch can sit beyond the hook's tail), and none of those repeats by chance.
SILENT_MIN_SESSIONS = 2
#: The cache lifetimes the hook works out for a reply: five minutes, or an
#: hour when a recent reply wrote to the 1-hour cache.
CACHE_TTL_S = capture_hook._CACHE_TTL_S
CACHE_TTL_1H_S = capture_hook._CACHE_TTL_1H_S
#: The replies before a message the hook looks at for a 1-hour write.
_TTL_LOOKBACK = 5

_FLOOR = datetime.min.replace(tzinfo=timezone.utc)


def coaching_spans(config_dir, end: datetime) -> list[tuple[datetime, datetime]]:
    """The spans when ``coaching_notes`` was on, from the capture log
    (``config.load_capture_log``): a record that adds it opens a span at its
    ``ts``, one that removes it closes the open one, and a span still open
    runs to ``end``. A log with no ``coaching`` change at all, or no
    ``config_dir``, gives one span from the earliest time to ``end``: on since
    before the log began (the callers ask only when it is on now). A ``ts``
    that does not parse is skipped. A ``retention_days`` shorter than the
    window can prune the switch-on record, and the spans then cover the whole
    window: it is the user's own setting."""
    records = config_mod.load_capture_log(config_dir) if config_dir else []
    spans: list[tuple[datetime, datetime]] = []
    opened: datetime | None = None
    seen = False
    for record in records:
        change = (record.get("changed") or {}).get("coaching") if isinstance(record.get("changed"), dict) else None
        if not isinstance(change, dict):
            continue
        before, after = change.get("from"), change.get("to")
        if not isinstance(before, list) or not isinstance(after, list):
            continue
        at = capture_mod._moment(record.get("ts"))
        if at is None:
            continue
        seen = True
        if "coaching_notes" in after and "coaching_notes" not in before:
            opened = opened or at
        elif "coaching_notes" in before and "coaching_notes" not in after and opened is not None:
            spans.append((opened, at))
            opened = None
    if not seen:
        return [(_FLOOR, end)]
    if opened is not None:
        spans.append((opened, end))
    return spans


def setup_changed_at(claude_root, config_dir) -> datetime | None:
    """The newest time (UTC) Claude Code's ``settings.json`` or one of the
    capture hook's files under ``<config_dir>/hooks/`` was changed; a file
    that is missing or cannot be read is skipped, and ``None`` when none can
    be read."""
    paths = [hook_health.settings_path(claude_root)]
    if config_dir:
        paths += [Path(config_dir) / "hooks" / name for name in hook_health.CAPTURE_FILES[capture_catalogue.HOOK_SCRIPT]]
    newest: float | None = None
    for path in paths:
        try:
            stamp = os.stat(path).st_mtime
        except OSError:
            continue
        newest = stamp if newest is None or stamp > newest else newest
    return datetime.fromtimestamp(newest, tz=timezone.utc) if newest is not None else None


def _typed_events(top, cycles) -> list:
    """For each cycle, the ``HUMAN_TEXT`` event of the message that opened it
    (``None`` when the transcript holds none): the last one not sent again
    before any reply (``detail["replaced"]``), at or before the cycle's first
    turn and after the previous cycle's first turn."""
    typed = []
    for event in top.events:
        if event.kind == EventKind.HUMAN_TEXT and not event.detail.get("replaced"):
            at = capture_mod._moment(event.ts)
            if at is not None:
                typed.append((at, event))
    typed.sort(key=lambda pair: pair[0])
    stamps = [at for at, _event in typed]
    out = []
    low = _FLOOR
    for cycle in cycles:
        high = capture_mod._moment(cycle.turns[0].ts)
        found = None
        if high is not None:
            i = bisect.bisect_right(stamps, high) - 1
            if i >= 0 and stamps[i] > low:
                found = typed[i][1]
            low = high
        out.append(found)
    return out


def typed_times(top, cycles) -> list[datetime | None]:
    """For each cycle, the time of the message that opened it: the ``ts`` of
    its ``HUMAN_TEXT`` event (:func:`_typed_events`), else the first turn's
    ``ts``, as ``capture._moment`` reads them."""
    return _times_of(cycles, _typed_events(top, cycles))


def _times_of(cycles, events) -> list[datetime | None]:
    out = []
    for cycle, event in zip(cycles, events):
        at = capture_mod._moment(event.ts) if event is not None else None
        out.append(at or capture_mod._moment(cycle.turns[0].ts))
    return out


def _cache_ttl(priced: list, start: int) -> int:
    """The cache lifetime of the replies before the cycle that starts at
    ``start``: an hour when any of the five before it wrote to the 1-hour
    cache, else five minutes (the hook's ``_cache_ttl_s``)."""
    before = priced[max(0, start - _TTL_LOOKBACK):start]
    return CACHE_TTL_1H_S if any(turn.cc_1h > 0 for turn in before) else CACHE_TTL_S


def session_occasions(bundle, messages, cycles, th) -> dict[str, list[int]]:
    """The indexes of ``messages`` (``prompting.messages_of`` over ``cycles =
    capture.prompt_cycles(bundle.top)``, aligned) where the hook's trigger
    holds, by hint. ``th`` is the thresholds in force
    (``ratings.coaching_thresholds``). A message counts for no hint when it is
    a /cg-feedback run, or was sent while Claude worked: the reply before it
    ended on a tool call, no stop came between, and it was typed under
    ``queued_minutes`` after it."""
    top = bundle.top
    found: dict[str, list[int]] = {hint: [] for hint in HINTS}
    events = _typed_events(top, cycles)
    times = _times_of(cycles, events)
    priced = capture_mod._priced(top)
    window = th["drip_window_minutes"] * 60

    def small_edit(m) -> bool:
        return m.chars <= th["drip_chars"] and m.edited and m.since is not None and m.since <= window

    for i, (m, cycle) in enumerate(zip(messages, cycles)):
        if capture_mod.is_feedback_run(cycle):
            continue
        typed, last = times[i], cycles[i - 1].turns[-1] if i else None
        replied = capture_mod._moment(last.ts) if last is not None else None
        if (
            last is not None
            and last.stop_reason == "tool_use"
            and EventKind.INTERRUPT not in cycle.turns[0].preceding_event_kinds
            and typed is not None
            and replied is not None
            and (typed - replied).total_seconds() < th["queued_minutes"] * 60
        ):
            continue
        event = events[i]
        if m.chars / 4 >= th["big_paste_tokens"] and not (event is not None and event.detail.get("has_image")):
            found["big_paste"].append(i)
        if m.chars <= th["drip_chars"] and m.change and not m.answers and m.since is not None and m.since <= window:
            count = 1
            for j in range(i - 1, -1, -1):
                if messages[j].answers or not messages[j].change:
                    continue
                if not small_edit(messages[j]):
                    break
                count += 1
            if count >= th["drip_count"]:
                found["drip_feed"].append(i)
        if (
            m.status
            and last is not None
            and typed is not None
            and replied is not None
            and (typed - replied).total_seconds() <= _cache_ttl(priced, cycle.start)
            and cycle.turns[0].background_waiting
        ):
            found["status_poll"].append(i)
    return found


@dataclass(slots=True)
class HintCount:
    """What one hint's moments came to. ``occasions`` is the messages where
    the trigger held; ``sessions`` the sessions with at least one, where the
    hook is the one to speak; ``noted`` of those, the ones with at least one
    hook note for the hint; ``notes`` the hook's notes for it in every
    session read; ``glass`` the mod's ``note`` events for it; ``mod_sessions``
    the sessions with an occasion where the mod ran on a surface of
    :data:`MOD_HINT_SURFACES` (left out of ``sessions``); ``older``, for
    ``status_poll`` only, the sessions left out because their digest does not
    measure ``background_waiting``."""

    occasions: int = 0
    sessions: int = 0
    noted: int = 0
    notes: int = 0
    glass: int = 0
    mod_sessions: int = 0
    older: int = 0


@dataclass
class Occasions:
    """The moments of a window, by hint. ``silent_since`` is where the
    evidence for the Capture page line starts (:func:`collect`);
    ``silent_sessions[h]`` holds the sessions that count in ``sessions`` and
    have an occasion at or after it, ``silent_noted[h]`` those of them with
    a hook note for ``h`` at or after it."""

    hints: dict[str, HintCount]
    on_hours: int = 0
    muted: tuple[str, ...] = ()
    silent_since: datetime | None = None
    silent_sessions: dict[str, set[str]] = field(default_factory=lambda: {hint: set() for hint in HINTS})
    silent_noted: dict[str, set[str]] = field(default_factory=lambda: {hint: set() for hint in HINTS})

    def silent_distinct(self, found) -> int:
        """How many sessions the hints in ``found`` name together."""
        return len(set().union(*(self.silent_sessions[hint] for hint in found))) if found else 0


def _epoch(value) -> datetime | None:
    """A sensor event's ``ts`` as an aware time. The store keeps the UTC ISO
    text ``mod_sensor.clean_event`` writes; epoch seconds, as a number or as
    text, are read too."""
    if isinstance(value, str):
        moment = capture_mod._moment(value)
        if moment is not None:
            return moment
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _note_kind(event) -> str | None:
    """The hint a hook note was for, as ``prompting._tips`` reads it."""
    if event.subkind == "coaching_note":
        return event.detail.get("kind")
    if event.subkind == "capture_note":
        return event.detail.get("coach")
    return None


def collect(
    corpus,
    config,
    *,
    config_dir,
    thresholds,
    since: datetime,
    until: datetime,
    mod_events=None,
    claude_root=None,
) -> Occasions | None:
    """The moments of ``corpus`` between ``since`` and ``until`` (aware
    times), or ``None`` unless ``config.capture.coaching_notes_on``.
    ``mod_events`` are the store's sensor rows (``Store.read_mod_events``);
    only counts of them are kept."""
    if not config.capture.coaching_notes_on:
        return None
    spans = coaching_spans(config_dir, until)

    def inside(at: datetime | None) -> bool:
        return at is not None and since <= at < until and any(start <= at < end for start, end in spans)

    covered = sum(
        max(0.0, (min(end, until) - max(start, since)).total_seconds()) for start, end in spans
    )
    personal = coaching_mod.read(config_dir) if config_dir else {}
    muted_words = personal.get("muted")
    muted = tuple(hint for hint in HINTS if isinstance(muted_words, list) and hint in muted_words)
    present: dict[str, set[str]] = {}
    shown: dict[tuple[str, str], int] = {}
    for row in mod_events or ():
        sid, kind = row.get("session_id"), row.get("event")
        if kind == "present":
            present.setdefault(sid, set()).add(row.get("surface"))
        elif kind == "note" and row.get("kind") in HINTS:
            if inside(_epoch(row.get("ts"))):
                shown[(sid, row["kind"])] = shown.get((sid, row["kind"]), 0) + 1
    changed = setup_changed_at(claude_root, config_dir)
    bounds = [since, spans[-1][0] if spans else None, changed]
    silent_since = max(b for b in bounds if b is not None and b > _FLOOR)
    occ = Occasions(
        hints={hint: HintCount() for hint in HINTS},
        on_hours=int(covered // 3600),
        muted=muted,
        silent_since=silent_since,
    )
    for bundle in corpus.sessions:
        top = bundle.top
        if top is None or (top.meta.entrypoint or "").startswith(HEADLESS_PREFIX):
            continue
        if not capture_hook.project_allowed(bundle.slug or "", config.capture.projects, config.exclude_projects):
            continue
        cycles = capture_mod.prompt_cycles(top)
        if not cycles:
            continue
        messages = prompting.messages_of(top, prompting._Prices(None), cycles)
        times = typed_times(top, cycles)
        found = session_occasions(bundle, messages, cycles, thresholds)
        notes: dict[str, list[datetime]] = {hint: [] for hint in HINTS}
        for event in top.events:
            kind = _note_kind(event)
            at = capture_mod._moment(event.ts)
            if kind in notes and inside(at):
                notes[kind].append(at)
        ran_on = present.get(bundle.session_id, set()) & set(MOD_HINT_SURFACES)
        for hint in HINTS:
            count = occ.hints[hint]
            indexes = [i for i in found[hint] if inside(times[i])]
            count.notes += len(notes[hint])
            count.glass += shown.get((bundle.session_id, hint), 0)
            if hint == "status_poll" and not measured.measures(top, *measured.OCCASION_FIELDS):
                if any(inside(times[i]) for i, m in enumerate(messages) if m.status):
                    count.older += 1
                continue
            if not indexes:
                continue
            count.occasions += len(indexes)
            if ran_on:
                count.mod_sessions += 1
                continue
            count.sessions += 1
            if notes[hint]:
                count.noted += 1
            if any(times[i] >= silent_since for i in indexes):
                occ.silent_sessions[hint].add(bundle.session_id)
                if any(at >= silent_since for at in notes[hint]):
                    occ.silent_noted[hint].add(bundle.session_id)
    return occ


def silent(occ: Occasions) -> dict[str, int]:
    """The live hints that had moments and gave no tip, with the sessions of
    each, in :data:`HINTS` order: a hint the user has not muted whose silent
    sessions number :data:`SILENT_MIN_SESSIONS` or more, none of which got a
    hook note for it, and which the mod showed none of."""
    return {
        hint: len(occ.silent_sessions[hint])
        for hint in HINTS
        if hint not in occ.muted
        and len(occ.silent_sessions[hint]) >= SILENT_MIN_SESSIONS
        and not occ.silent_noted[hint]
        and occ.hints[hint].glass == 0
    }


def silent_sentence(found: dict[str, int], distinct: int) -> str | None:
    """The Capture page's line for :func:`silent`'s ``found``, where
    ``distinct`` is ``Occasions.silent_distinct(found)``; ``None`` when
    nothing is silent."""
    if not found:
        return None
    # capture_view imports this module, so the page's own wording is read here.
    from . import capture_view

    parts = ", ".join(f"{LABELS[hint]} {n}" for hint, n in found.items())
    return (
        f"{capture_view._plural(distinct, 'session')} had a moment for a coaching tip and none came: {parts}. "
        f"'{capture_view.STATUS_COMMAND}' checks the capture hook."
    )


__all__ = [
    "CACHE_TTL_1H_S",
    "CACHE_TTL_S",
    "HEADLESS_PREFIX",
    "HINTS",
    "HintCount",
    "LABELS",
    "MOD_HINT_SURFACES",
    "Occasions",
    "SILENT_MIN_SESSIONS",
    "collect",
    "coaching_spans",
    "session_occasions",
    "setup_changed_at",
    "silent",
    "silent_sentence",
    "typed_times",
]
