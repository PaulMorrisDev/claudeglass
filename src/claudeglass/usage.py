"""Usage section (WP10a): finance/enterprise-facing breakdowns of a
corpus by calendar period, project, entrypoint and (subscription billing
only) 5-hour session block.

Where the rest of the report answers "how is caching/agentic work
behaving", this section answers the plainer question a finance or
platform-team reader actually has: how much was used, and when. Every
period bucket (:func:`_day_key`/:func:`_week_key`/:func:`_month_key`) is
computed in ``config.tz`` (the same "fall back to the machine's own local
zone on an unresolvable/absent name" convention ``classify._to_local``
uses — duplicated here rather than imported, per this project's
established convention for small cross-module helpers, see
``workflows.py``'s/``phases.py``'s module docstrings).

In subscription billing mode (``config.billing == "subscription"``),
every money column in this section is what the tokens *would* have cost
at the resolved rate card, not a real invoice line — the report's own
column labels say so explicitly ("list-price equivalent") so a
subscription-billed reader never mistakes it for an actual charge.

Five-hour blocks: a genuine Claude subscription usage window is a
rolling 5-hour period starting from a user's first message in it, reset
per-account — this module cannot observe account-wide reset timing from
transcripts alone, so it uses a fixed grid instead (each local calendar
day cut into 00:00/05:00/10:00/15:00/20:00-local blocks) as a documented,
deterministic proxy. Each priced turn (top-level and subagent alike) is
assigned to the block containing *its own* local timestamp — not the
block its session happened to start in — so a long-running session that
spans several of these fixed blocks (e.g. a 12-hour session) has its
turns split across all of them rather than stamped wholesale onto its
first block.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config
from .corpus import Corpus, SessionBundle
from .model import Column, Section, Table, TranscriptResult, Turn
from .pricing import Pricing, price_turn

#: Width of one usage block, in hours (see module docstring's proxy note).
_BLOCK_HOURS = 5


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_local(dt: datetime, tz: str | None) -> datetime:
    """Same convention as ``classify._to_local``: convert to ``tz`` (an
    IANA name), falling back to the machine's own local zone when ``tz``
    is falsy or can't be resolved."""
    if tz:
        try:
            return dt.astimezone(ZoneInfo(tz))
        except (ZoneInfoNotFoundError, ValueError):
            return dt.astimezone()
    return dt.astimezone()


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    return [t for t in result.turns if t.turn_index > 0]


def _transcripts_of(bundle: SessionBundle) -> list[TranscriptResult]:
    transcripts: list[TranscriptResult] = []
    if bundle.top is not None:
        transcripts.append(bundle.top)
    transcripts.extend(bundle.subs)
    return transcripts


def _usage_tokens(turn: Turn) -> int:
    return turn.input_tokens + turn.cache_creation_tokens + turn.cache_read_tokens + turn.output_tokens


def _day_key(local_dt: datetime) -> str:
    return local_dt.strftime("%Y-%m-%d")


def _week_key(local_dt: datetime) -> str:
    iso_year, iso_week, _ = local_dt.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def _month_key(local_dt: datetime) -> str:
    return local_dt.strftime("%Y-%m")


def _block_key(local_dt: datetime) -> str:
    block_start_hour = (local_dt.hour // _BLOCK_HOURS) * _BLOCK_HOURS
    block_start = local_dt.replace(hour=block_start_hour, minute=0, second=0, microsecond=0)
    return block_start.strftime("%Y-%m-%d %H:%M") + f" ({local_dt.tzname() or 'local'})"


@dataclass(slots=True)
class _PeriodModelCell:
    turns: int = 0
    tokens: int = 0
    cost: float = 0.0


def _period_table(name: str, title: str, cells: dict[tuple[str, str], _PeriodModelCell]) -> Table:
    rows = [
        [period, model, cell.turns, cell.tokens, cell.cost]
        for (period, model), cell in sorted(cells.items(), key=lambda kv: kv[0])
    ]
    return Table(
        name=name,
        title=title,
        columns=[
            Column(key="period", label="Period", kind="str"),
            Column(key="model", label="Model", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="tokens", label="Tokens", kind="tokens"),
            Column(key="cost", label="Cost", kind="money"),
        ],
        rows=rows,
    )


def build_section(corpus: Corpus, pricing: Pricing, config: Config) -> Section:
    """Build the "Usage" report section (key ``"usage"``): ``by_day``,
    ``by_week``, ``by_month`` (period x model), ``by_project``,
    ``by_entrypoint``, and (subscription billing only) ``five_hour_blocks``.
    """
    by_day: dict[tuple[str, str], _PeriodModelCell] = {}
    by_week: dict[tuple[str, str], _PeriodModelCell] = {}
    by_month: dict[tuple[str, str], _PeriodModelCell] = {}
    by_project: dict[str, dict] = {}
    by_entrypoint: dict[str, dict] = {}
    by_block: dict[str, dict] = {}

    is_subscription = config.billing == "subscription"
    money_label = "Cost (list-price equivalent)" if is_subscription else "Cost"

    for bundle in corpus.sessions:
        # A bundle with no top-level transcript is an orphaned subagent
        # (its parent session was never discovered) -- report.py's own
        # main loop skips these entirely (see build_report's ``if
        # bundle.top is None: continue``), so this module must too (R23
        # fix) or the two sections' session/turn counts disagree on a
        # corpus containing one.
        if bundle.top is None:
            continue

        project_bucket = by_project.setdefault(
            bundle.slug, {"sessions": 0, "cost": 0.0}
        )
        project_bucket["sessions"] += 1

        for tr in _transcripts_of(bundle):
            entrypoint = tr.meta.entrypoint or "unknown"
            entry_bucket = by_entrypoint.setdefault(
                entrypoint, {"transcripts": 0, "turns": 0, "tokens": 0, "cost": 0.0}
            )
            entry_bucket["transcripts"] += 1

            for turn in _priced_turns(tr):
                resolved = pricing.resolve_model(turn.model)
                breakdown = price_turn(turn, resolved)
                tokens = _usage_tokens(turn)
                model = turn.model or "<unknown>"
                # Replies are counted without an estimated compaction
                # call (``is_synthetic``); its tokens and cost are spend.

                local_dt = None
                parsed = _parse_ts(turn.ts)
                if parsed is not None:
                    local_dt = _to_local(parsed, config.tz)

                if local_dt is not None:
                    day_cell = by_day.setdefault((_day_key(local_dt), model), _PeriodModelCell())
                    day_cell.turns += not turn.is_synthetic
                    day_cell.tokens += tokens
                    day_cell.cost += breakdown.total

                    week_cell = by_week.setdefault((_week_key(local_dt), model), _PeriodModelCell())
                    week_cell.turns += not turn.is_synthetic
                    week_cell.tokens += tokens
                    week_cell.cost += breakdown.total

                    month_cell = by_month.setdefault((_month_key(local_dt), model), _PeriodModelCell())
                    month_cell.turns += not turn.is_synthetic
                    month_cell.tokens += tokens
                    month_cell.cost += breakdown.total

                project_bucket["cost"] += breakdown.total

                entry_bucket["turns"] += not turn.is_synthetic
                entry_bucket["tokens"] += tokens
                entry_bucket["cost"] += breakdown.total

                # R15 fix: the block a turn belongs to is computed from
                # *that turn's own* local timestamp, not the session's
                # first turn's -- a session spanning several blocks (e.g.
                # a 12-hour session covers 2-3 of these 5-hour blocks) had
                # every one of its turns stamped onto whichever block its
                # first turn happened to land in.
                if is_subscription and local_dt is not None:
                    block_key = _block_key(local_dt)
                    block_bucket = by_block.setdefault(
                        block_key, {"sessions": set(), "turns": 0, "tokens": 0, "cost": 0.0}
                    )
                    block_bucket["sessions"].add(bundle.session_id)
                    block_bucket["turns"] += not turn.is_synthetic
                    block_bucket["tokens"] += tokens
                    block_bucket["cost"] += breakdown.total

    project_table = Table(
        name="by_project",
        title="Usage by project",
        columns=[
            Column(key="slug", label="Project", kind="str"),
            Column(key="sessions", label="Sessions", kind="int"),
            Column(key="cost", label=money_label, kind="money"),
        ],
        rows=[
            [slug, bucket["sessions"], bucket["cost"]]
            for slug, bucket in sorted(by_project.items(), key=lambda kv: (-kv[1]["cost"], kv[0]))
        ],
    )

    entrypoint_table = Table(
        name="by_entrypoint",
        title="Usage by entrypoint",
        columns=[
            Column(key="entrypoint", label="Entrypoint", kind="str"),
            Column(key="transcripts", label="Transcripts", kind="int"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="tokens", label="Tokens", kind="tokens"),
            Column(key="cost", label=money_label, kind="money"),
        ],
        rows=[
            [entrypoint, bucket["transcripts"], bucket["turns"], bucket["tokens"], bucket["cost"]]
            for entrypoint, bucket in sorted(by_entrypoint.items(), key=lambda kv: (-kv[1]["cost"], kv[0]))
        ],
    )

    tables = [
        _period_table("by_day", "Usage by day", by_day),
        _period_table("by_week", "Usage by week", by_week),
        _period_table("by_month", "Usage by month", by_month),
        project_table,
        entrypoint_table,
    ]
    for table in tables[:3]:
        for column in table.columns:
            if column.key == "cost":
                column.label = money_label

    notes: list[str] = []
    if is_subscription:
        block_rows = [
            [block_key, len(bucket["sessions"]), bucket["turns"], bucket["tokens"], bucket["cost"]]
            for block_key, bucket in sorted(by_block.items())
        ]
        blocks_table = Table(
            name="five_hour_blocks",
            title="Five-hour usage blocks",
            columns=[
                Column(key="block_start", label="Block start (local)", kind="str"),
                Column(key="sessions", label="Sessions", kind="int"),
                Column(key="turns", label="Turns", kind="int"),
                Column(key="tokens", label="Tokens", kind="tokens"),
                Column(key="cost", label=money_label, kind="money"),
            ],
            rows=block_rows,
            notes=[
                "Blocks start at 00:00, 05:00, 10:00, 15:00 and 20:00 local time. "
                "Your plan's real five-hour window doesn't follow this grid, and "
                "transcripts can't show where it starts, so treat these blocks as "
                "close, not exact.",
            ],
        )
        tables.append(blocks_table)
        notes.append(
            "Billing mode is subscription: every cost column above is a list-price "
            "equivalent, not a real charge."
        )
    else:
        tables.append(
            Table(
                name="five_hour_blocks",
                title="Five-hour usage blocks",
                columns=[
                    Column(key="block_start", label="Block start (local)", kind="str"),
                    Column(key="sessions", label="Sessions", kind="int"),
                    Column(key="turns", label="Turns", kind="int"),
                    Column(key="tokens", label="Tokens", kind="tokens"),
                    Column(key="cost", label="Cost", kind="money"),
                ],
                rows=[],
                notes=[
                    "Skipped: five-hour blocks exist only on a Pro or Max plan, "
                    "and your billing is set to the API."
                ],
            )
        )

    return Section(key="usage", title="Usage", tables=tables, notes=notes)


__all__ = ["build_section"]
