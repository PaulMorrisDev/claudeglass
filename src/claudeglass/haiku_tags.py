"""The reply tags Claude Haiku writes, read back.

While ``[capture] tagger`` is ``"haiku"``, the session note asks Claude for
no ``[cg: ...]`` tag. Instead, when a turn of the main session ends, the
capture hook's ``Stop`` entry hands a short excerpt of it to a worker of
its own, which asks Claude Haiku (``claude -p --model haiku``, with the
user's own login) for the tag, and appends one line to
``<config-dir>/tags/YYYY-MM.jsonl``::

    {"ts":"2026-09-27T10:02:11Z","reply":"msg_01...","tl":"task=bugfix brief=clear","usd":0.0011,"in":1204,"out":21,"model":"claude-haiku-4-5-20251001"}

``reply`` is the id of the reply tagged (``Turn.message_id``); ``tl`` the
tag's words, already checked against the closed vocabularies; ``usd``,
``in`` and ``out`` what the call cost and read and wrote. An optional ``w``
says who asked: ``"haiku-fallback"`` when ``[capture] tagger`` is
``"claude"`` and the hook asked Haiku for a reply Claude left without a
tag (``capture_catalogue.JUDGE_WRITERS``); a line without one, and any
other value, reads as ``"haiku"``, the tagger. An optional ``g``
lists what the hook's grounding put right in Haiku's words, as
``key:from>to`` items (``to`` empty for a word dropped), for example
``"g":"check:none>full shift:build>fix"``; a line without one, written
before grounding was recorded, reads the same. A line with
``err`` instead of ``tl`` says why that turn got none
(``capture_catalogue.JUDGE_ERRORS``). The excerpt itself, and anything
else Haiku wrote, is never kept.

A finished agent run is judged the same way, whichever writes the main
session's tags (the hook's ``SubagentStop`` entry): its line carries
``agent`` in place of ``tl`` (``brief=clear missing=none result=done``,
and ``retry=brief`` when it redid an earlier run, ``retry=model`` when
the hook saw the same brief rerun on a higher model tier; a line written
before ``fit`` was dropped also holds ``fit=right``), and ``reply`` is the
id of the agent's last reply. A run is judged at each stop, so one that
carried on after a stop hook asked it to has a line for each, each on
the reply the run ended on then: the newest verdict is the run's. A line
with ``err`` ``no_answer`` is a run whose answer never reached its
transcript, which Haiku wasn't asked about.

:func:`apply` puts each tag on its reply's turn in a loaded corpus, as if
the parser had found it at the end of the reply (``CaptureTag.judged``
set, ``chars`` 0), so every view that reads tags reads these the same
way: an agent run's ``result`` as the run's ``result_marker``, and its
``retry`` as the ``retry_marker`` of its first turn, where a brief's
marker used to put it. :func:`load` checks every line again: a hand-edited or foreign line
can't carry anything else into a report. :func:`prune` deletes month
files older than ``retention_days``, as ``signals.prune`` does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .capture_catalogue import (
    AGENT_JUDGE_VOCAB,
    JUDGE_DIR,
    JUDGE_ERRORS,
    JUDGE_WRITER,
    JUDGE_WRITERS,
    RESULT_WORDS,
    RETIRED_AGENT_KEYS,
    RETRY_REASONS,
)
from .capture_catalogue import TAG_VOCAB
from .capture_tags import CHANGE_PATTERN, GROUNDED_KEYS, MAIN_TAG_FIELDS, _apply_word, parse_reply_tags
from .model import CaptureTag

_MONTH_FILE_RE = re.compile(r"(\d{4})-(\d{2})\.jsonl")
#: A reply id as the parser keeps it: an API message id, a request id or
#: a line's uuid.
_REPLY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_WORDS_RE = re.compile(r"[a-z]{1,16}=[a-z,_-]{1,120}(?: [a-z]{1,16}=[a-z,_-]{1,120}){0,15}")
_MODEL_RE = re.compile(r"[a-z0-9.-]{1,64}")
#: The ``g`` field: up to eight ``key:from>to`` items, one per word
#: grounding can change.
_GROUNDED_RE = re.compile(rf"{CHANGE_PATTERN}(?: {CHANGE_PATTERN}){{0,7}}")
#: A call's cost or token count this large is implausible and dropped.
_MAX_USD = 10.0
_MAX_TOKENS = 10_000_000


@dataclass(frozen=True, slots=True)
class Judged:
    """One line of a tag file: a turn, or an agent run, Haiku was asked
    about."""

    at: datetime
    #: The reply's id (``Turn.message_id``): the agent's last reply for a
    #: run.
    reply: str
    #: The tag, or ``None`` when the call failed (``error`` says why).
    tag: CaptureTag | None
    error: str = ""
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    #: ``"agent"`` for an agent run, ``"main"`` for a turn of the main
    #: session.
    kind: str = "main"
    #: An agent run's ``result`` and ``retry`` words (``""`` when none).
    result: str = ""
    retry: str = ""
    #: What grounding put right in the words, ``key:from>to`` (the line's
    #: ``g`` field); empty for a line without one.
    grounded: tuple[str, ...] = ()
    #: Who asked Haiku: a word of ``capture_catalogue.JUDGE_WRITERS`` (the
    #: line's ``w`` field; ``"haiku"`` without one).
    writer: str = JUDGE_WRITER


def tags_dir(config_dir: str | Path) -> Path:
    return Path(config_dir) / JUDGE_DIR


def _month_files(config_dir: str | Path) -> list[tuple[datetime, Path]]:
    try:
        entries = list(tags_dir(config_dir).iterdir())
    except OSError:
        return []
    months = []
    for path in entries:
        match = _MONTH_FILE_RE.fullmatch(path.name)
        if match and 1 <= int(match.group(2)) <= 12:
            months.append((datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=timezone.utc), path))
    return sorted(months)


def _next_month(start: datetime) -> datetime:
    return start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)


def _number(value, ceiling: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= ceiling:
        return 0.0
    return float(value)


def _judged_from(record) -> Judged | None:
    if not isinstance(record, dict):
        return None
    ts, reply = record.get("ts"), record.get("reply")
    if not isinstance(ts, str) or not isinstance(reply, str) or not _REPLY_RE.fullmatch(reply):
        return None
    try:
        at = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    tag = None
    error = ""
    kind = "agent" if "agent" in record else "main"
    result = retry = ""
    grounded: tuple[str, ...] = ()
    if kind == "agent":
        tag, result, retry = _agent_tag(record.get("agent"))
    else:
        words = record.get("tl")
        if isinstance(words, str) and _WORDS_RE.fullmatch(words):
            # The same reading as a tag at the end of a reply: unknown keys
            # and words are dropped, and a skill name never survives.
            tag, _ = parse_reply_tags(f"[cg: {words}]")
        if tag is not None and not any(getattr(tag, name) not in (None, ()) for name in MAIN_TAG_FIELDS):
            tag = None  # no word it holds is a known one
        if tag is not None and not tag.has_tl:
            tag = None
        if tag is not None:
            grounded = _grounded_from(record.get("g"))
    if tag is None:
        error = record.get("err") if record.get("err") in JUDGE_ERRORS else "no_tag"
    model = record.get("model")
    writer = record.get("w")
    return Judged(
        at=at,
        reply=reply,
        tag=tag,
        error=error,
        usd=_number(record.get("usd"), _MAX_USD),
        tokens_in=int(_number(record.get("in"), _MAX_TOKENS)),
        tokens_out=int(_number(record.get("out"), _MAX_TOKENS)),
        model=model if isinstance(model, str) and _MODEL_RE.fullmatch(model) else "",
        kind=kind,
        result=result,
        retry=retry,
        grounded=grounded,
        writer=writer if writer in JUDGE_WRITERS and kind == "main" else JUDGE_WRITER,
    )


def _grounded_from(value) -> tuple[str, ...]:
    """The items of a line's ``g`` field that name a word grounding can
    change, from and to words of the closed vocabularies (``to`` may be
    empty: dropped). Anything else in it is dropped."""
    if not isinstance(value, str) or not _GROUNDED_RE.fullmatch(value):
        return ()
    items = []
    for item in value.split():
        key, _, change = item.partition(":")
        old, _, new = change.partition(">")
        vocab = TAG_VOCAB.get(key, ())
        if key in GROUNDED_KEYS and old in vocab and (not new or new in vocab):
            items.append(item)
    return tuple(dict.fromkeys(items))


def _agent_tag(words) -> tuple[CaptureTag | None, str, str]:
    """An agent run's words as ``(tag, result, retry)``: the tag holds
    ``brief`` and ``missing``, and ``fit`` from a line written before it
    was dropped; ``None`` when no word is a known one."""
    if not isinstance(words, str) or not _WORDS_RE.fullmatch(words):
        return None, "", ""
    values: dict = {}
    result = retry = ""
    for word in words.split():
        key, _, value = word.partition("=")
        if key == "result" and value in RESULT_WORDS:
            result = value
        elif key == "retry" and value in RETRY_REASONS:
            retry = value
        elif key in RETIRED_AGENT_KEYS or (key in AGENT_JUDGE_VOCAB and key not in ("result", "retry")):
            if key == "missing":
                value = ",".join(w for w in value.split(",") if w in AGENT_JUDGE_VOCAB["missing"])
            _apply_word(values, key, value, ())
    if not (values or result or retry):
        return None, "", ""
    return CaptureTag(has_tl=False, chars=0, **values), result, retry


def load(config_dir: str | Path, *, since: datetime | None = None) -> list[Judged]:
    """Every well-formed line, oldest first (from ``since`` on, when
    given). Unreadable files and malformed lines are skipped."""
    out: list[Judged] = []
    for start, path in _month_files(config_dir):
        if since is not None and _next_month(start) <= since:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                judged = _judged_from(json.loads(line))
            except ValueError:
                continue
            if judged is not None and (since is None or judged.at >= since):
                out.append(judged)
    out.sort(key=lambda j: j.at)
    return out


#: The last :func:`_by_reply` answer, keyed by the tag files' names,
#: sizes and times: the dashboard's service builds a corpus for many
#: requests, and the files only grow a line per turn.
_CACHE: dict = {}


def _by_reply(config_dir: str | Path) -> dict[str, Judged]:
    """The tagged lines by reply id, the last line for a reply winning."""
    try:
        key = tuple(
            (path.name, stat.st_size, stat.st_mtime_ns)
            for _start, path in _month_files(config_dir)
            for stat in (path.stat(),)
        )
    except OSError:
        key = None
    cached = _CACHE.get(str(config_dir))
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    by_reply = {j.reply: j for j in load(config_dir) if j.tag is not None}
    if key is not None:
        _CACHE[str(config_dir)] = (key, by_reply)
    return by_reply


def apply(corpus, config_dir: str | Path | None) -> int:
    """Put each of Haiku's tags on its reply's turn in ``corpus``: a main
    session's turn, unless it already ends in a ``[cg: ...]`` tag of
    Claude's own, and an agent run's last reply, unless the agent wrote a
    ``[result: ...]`` of its own (an older transcript); returns how many
    were put. The last line for a reply wins, and a run judged at more than
    one stop takes the verdict of the newest reply it was judged on.
    Nothing happens without a ``config_dir`` or a tag folder."""
    if config_dir is None or not tags_dir(config_dir).is_dir():
        return 0
    by_reply = _by_reply(config_dir)
    if not by_reply:
        return 0
    applied = 0
    for bundle in corpus.sessions:
        top = bundle.top
        if top is not None:
            for turn in top.turns:
                judged = by_reply.get(turn.message_id)
                if judged is None or judged.kind != "main" or (turn.cap is not None and turn.cap.has_tl):
                    continue
                turn.cap = replace(
                    judged.tag, chars=0, judged=True, judge_usd=judged.usd, grounded=judged.grounded
                )
                applied += 1
        for sub in bundle.subs:
            applied += _apply_run(sub, by_reply)
    return applied


def _apply_run(sub, by_reply: dict[str, Judged]) -> int:
    """Put an agent run's judged words on its turns; 1 when they went on.
    A run judged at more than one stop (it carried on after a stop hook
    asked it to) has a verdict on each reply it ended on: the newest turn's
    wins, and what the earlier calls cost is added to its cost."""
    verdicts = [
        (turn, by_reply[turn.message_id])
        for turn in reversed(sub.turns)
        if turn.message_id in by_reply and by_reply[turn.message_id].kind == "agent"
    ]
    if not verdicts:
        return 0
    turn, judged = verdicts[0]
    if turn.result_marker or (turn.cap is not None and turn.cap.chars):
        return 0  # the agent tagged its own report
    turn.cap = replace(judged.tag, chars=0, judged=True, judge_usd=sum(j.usd for _t, j in verdicts))
    turn.result_marker = judged.result or None
    first = sub.turns[0] if sub.turns else None
    if judged.retry and first is not None and not first.retry_marker:
        first.retry_marker = judged.retry
    return 1


@dataclass(slots=True)
class Summary:
    """What Haiku did over a stretch of time: calls, tags, failures and
    cost."""

    calls: int = 0
    tagged: int = 0
    #: Of ``calls``, the agent runs judged.
    agent_calls: int = 0
    #: ``capture_catalogue.JUDGE_ERRORS`` word -> how many turns.
    errors: dict[str, int] = field(default_factory=dict)
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def usd_per_call(self) -> float | None:
        costed = self.calls - sum(self.errors.get(e, 0) for e in ("no_cli", "no_login", "timeout", "no_answer"))
        return self.usd / costed if costed > 0 else None


def summary(
    config_dir: str | Path | None, *, since: datetime | None = None, kind: str | None = None, writer: str | None = None
) -> Summary:
    """:class:`Summary` of the tag files from ``since`` on: the main
    session's turns (``kind="main"``), agent runs (``"agent"``) or both,
    and, given a ``writer`` (a word of ``capture_catalogue.JUDGE_WRITERS``),
    only the lines it asked for."""
    out = Summary()
    if config_dir is None:
        return out
    for judged in load(config_dir, since=since):
        if (kind is not None and judged.kind != kind) or (writer is not None and judged.writer != writer):
            continue
        out.calls += 1
        out.agent_calls += judged.kind == "agent"
        out.usd += judged.usd
        out.tokens_in += judged.tokens_in
        out.tokens_out += judged.tokens_out
        if judged.tag is not None:
            out.tagged += 1
        else:
            out.errors[judged.error] = out.errors.get(judged.error, 0) + 1
    return out


def prune(config_dir: str | Path, retention_days: int, now: datetime | None = None) -> int:
    """Delete the month files wholly older than ``retention_days``;
    returns how many went."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    removed = 0
    for start, path in _month_files(config_dir):
        if _next_month(start) <= cutoff:
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
    return removed


__all__ = ["Judged", "Summary", "apply", "load", "prune", "summary", "tags_dir"]
