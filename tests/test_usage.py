"""``usage.py``: period bucketing (day/week/month), by-project/by-entrypoint
breakdowns, and the subscription-only five-hour-block table. Built on
synthetic corpora via ``tests/helpers``/``corpus.load_corpus`` (per the
plan's test list), the same construction pattern ``test_corpus.py`` uses.
"""

from __future__ import annotations

from pathlib import Path

from claudeglass.config import Config
from claudeglass.corpus import SessionBundle, load_corpus
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing
from claudeglass.report import build_report
from claudeglass.usage import build_section

from helpers import assert_privacy, turn_line, write_jsonl

PRICING = load_pricing()


def _write_top(project_dir: Path, session_id: str, timestamps: list[str], **overrides) -> None:
    write_jsonl(
        project_dir / f"{session_id}.jsonl",
        [turn_line(timestamp=ts, **overrides) for ts in timestamps],
    )


def test_empty_corpus_renders_usage_section_without_exceptions(tmp_path):
    project_dir = tmp_path / "proj-empty"
    project_dir.mkdir()
    corpus = load_corpus([project_dir])
    section = build_section(corpus, PRICING, Config())
    assert section.key == "usage"
    table_names = {t.name for t in section.tables}
    assert table_names == {"by_day", "by_week", "by_month", "by_project", "by_entrypoint", "five_hour_blocks"}
    for table in section.tables:
        assert table.rows == []
    assert_privacy(section)


def test_period_bucketing_across_a_month_boundary(tmp_path):
    # Config.tz=None: usage.py's own _to_local falls back to the
    # machine's own local zone (dt.astimezone()) without ever
    # attempting a named zoneinfo lookup, so this test's expectations
    # (built the same way) hold regardless of whether an IANA tzdata
    # source is installed (e.g. a bare Windows install without the
    # tzdata package never resolves a named zone -- see test_classify.py's
    # _tzdata_has convention for the same issue).
    # Wide margins from midnight UTC (06:00/18:00) so the assigned
    # calendar day/month survives any realistic local UTC offset
    # (-12..+14) without actually crossing into the other bucket --
    # only the deliberate 31-Aug/1-Sep gap between them does that.
    ts_aug = "2026-08-31T06:00:00.000Z"
    ts_sep = "2026-09-01T18:00:00.000Z"
    project_dir = tmp_path / "proj-month"
    project_dir.mkdir()
    _write_top(project_dir, "session-boundary", [ts_aug, ts_sep], input_tokens=100, output_tokens=20)
    corpus = load_corpus([project_dir])
    section = build_section(corpus, PRICING, Config(tz=None))

    from datetime import datetime

    local_aug = datetime.fromisoformat(ts_aug.replace("Z", "+00:00")).astimezone()
    local_sep = datetime.fromisoformat(ts_sep.replace("Z", "+00:00")).astimezone()
    expected_months = {local_aug.strftime("%Y-%m"), local_sep.strftime("%Y-%m")}
    expected_days = {local_aug.strftime("%Y-%m-%d"), local_sep.strftime("%Y-%m-%d")}

    by_month = next(t for t in section.tables if t.name == "by_month")
    months = {row[0] for row in by_month.rows}
    assert months == expected_months

    by_day = next(t for t in section.tables if t.name == "by_day")
    days = {row[0] for row in by_day.rows}
    assert days == expected_days

    # Every period table's turns sum to the corpus's total priced turns (2).
    assert sum(row[2] for row in by_month.rows) == 2
    assert sum(row[2] for row in by_day.rows) == 2


def test_by_project_and_by_entrypoint_totals(tmp_path):
    project_dir = tmp_path / "proj-multi"
    project_dir.mkdir()
    _write_top(project_dir, "session-a", ["2026-09-18T10:00:00.000Z"], input_tokens=100, output_tokens=20)
    _write_top(project_dir, "session-b", ["2026-09-18T11:00:00.000Z"], input_tokens=200, output_tokens=40)

    corpus = load_corpus([project_dir])
    section = build_section(corpus, PRICING, Config())

    by_project = next(t for t in section.tables if t.name == "by_project")
    assert len(by_project.rows) == 1  # both sessions share one project dir/slug
    assert by_project.rows[0][1] == 2  # sessions count

    by_entrypoint = next(t for t in section.tables if t.name == "by_entrypoint")
    assert len(by_entrypoint.rows) == 1
    assert by_entrypoint.rows[0][0] == "unknown"
    assert by_entrypoint.rows[0][2] == 2  # turns


def test_api_billing_skips_five_hour_blocks_with_a_note(tmp_path):
    project_dir = tmp_path / "proj-api"
    project_dir.mkdir()
    _write_top(project_dir, "session-a", ["2026-09-18T10:00:00.000Z"])
    corpus = load_corpus([project_dir])

    section = build_section(corpus, PRICING, Config(billing="api"))
    blocks = next(t for t in section.tables if t.name == "five_hour_blocks")
    assert blocks.rows == []
    assert blocks.notes and "Skipped" in blocks.notes[0]

    for table in section.tables[:3]:
        for column in table.columns:
            if column.key == "cost":
                assert column.label == "Cost"


def test_subscription_billing_populates_five_hour_blocks_and_relabels_cost(tmp_path):
    project_dir = tmp_path / "proj-sub"
    project_dir.mkdir()
    _write_top(project_dir, "session-a", ["2026-09-18T10:00:00.000Z"], input_tokens=100, output_tokens=20)
    corpus = load_corpus([project_dir])

    section = build_section(corpus, PRICING, Config(billing="subscription"))
    blocks = next(t for t in section.tables if t.name == "five_hour_blocks")
    assert len(blocks.rows) == 1
    assert blocks.rows[0][1] == 1  # one session

    for table in section.tables:
        for column in table.columns:
            if column.key == "cost":
                assert column.label == "Cost (list-price equivalent)"
    assert any("subscription" in note for note in section.notes)
    assert_privacy(section)


def test_orphaned_subagent_bundle_session_count_matches_report(tmp_path):
    # R23: a bundle with ``top is None`` is an orphaned subagent -- its
    # parent top-level session was never discovered. ``load_corpus``
    # never produces this through normal discovery (a subagent transcript
    # is only ever attached to a bundle whose top-level session was also
    # found), so it's constructed by hand here, the same way report.py's
    # own build_report loop is exercised against one. Before the fix,
    # usage.py counted this bundle's session (report.py has always
    # skipped it -- see build_report's ``if bundle.top is None:
    # continue``), so the two sections' session totals disagreed on any
    # corpus containing one.
    project_dir = tmp_path / "proj-orphan"
    project_dir.mkdir()
    _write_top(project_dir, "session-normal", ["2026-09-18T10:00:00.000Z"], input_tokens=100, output_tokens=20)
    corpus = load_corpus([project_dir])
    assert len(corpus.sessions) == 1

    orphan_path = project_dir / "orphan-sub.jsonl"
    write_jsonl(
        orphan_path,
        [turn_line(timestamp="2026-09-18T11:00:00.000Z", input_tokens=50, output_tokens=10)],
    )
    orphan_meta = TranscriptMeta(
        path=str(orphan_path),
        kind="subagent",
        session_id="session-orphan",
        agent_type="explore",
        project_slug="proj-orphan",
    )
    orphan_transcript = parse_transcript(orphan_path, orphan_meta)
    corpus.sessions.append(
        SessionBundle(
            session_id="session-orphan",
            slug="proj-orphan",
            top=None,
            subs=[orphan_transcript],
        )
    )
    assert len(corpus.sessions) == 2

    section = build_section(corpus, PRICING, Config())
    by_project = next(t for t in section.tables if t.name == "by_project")
    usage_session_total = sum(row[1] for row in by_project.rows)
    assert usage_session_total == 1  # the orphaned bundle must not be counted

    report_model = build_report(corpus, PRICING, Config(), projects=(), window="all")
    overview = next(s for s in report_model.sections if s.key == "overview")
    totals_table = next(t for t in overview.tables if t.name == "totals")
    report_sessions = next(row[1] for row in totals_table.rows if row[0] == "sessions")

    assert report_sessions == usage_session_total


def test_twelve_hour_session_spans_three_five_hour_blocks(tmp_path):
    # R15: a session's turns must be assigned to five-hour blocks by
    # *each turn's own* local timestamp, not stamped wholesale onto the
    # block its first turn landed in. A 12-hour session (00:30 -> 12:30
    # local, one turn per hour) starting at the very beginning of a
    # fixed block (00:00 local) crosses three block boundaries (00:00,
    # 05:00, 10:00), so before the fix every one of these turns would
    # have landed in the single 00:00 block instead of being split
    # across three.
    #
    # The UTC timestamps below are built from fixed LOCAL wall-clock
    # times (via this machine's UTC offset on that date) rather than a named
    # zone passed through Config -- this test suite's own convention
    # elsewhere (see _tzdata_has in test_classify.py) is that a resolvable
    # IANA zone isn't guaranteed on every machine (e.g. bare Windows
    # without the tzdata package), and Config(tz=None) always falls back
    # to the machine's own local zone regardless.
    from datetime import datetime, timedelta, timezone

    local_base = datetime(2026, 9, 18, 0, 30)  # 00:30 local
    # The offset on that date, not today's: a test run after the clocks
    # change would otherwise put every turn an hour off.
    offset = local_base.astimezone().utcoffset() or timedelta(0)
    local_times = [local_base + timedelta(hours=h) for h in range(13)]  # 00:30 .. 12:30 local
    timestamps = [(lt - offset).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z") for lt in local_times]

    project_dir = tmp_path / "proj-long-session"
    project_dir.mkdir()
    _write_top(project_dir, "session-long", timestamps, input_tokens=100, output_tokens=20)
    corpus = load_corpus([project_dir])

    section = build_section(corpus, PRICING, Config(billing="subscription"))
    blocks = next(t for t in section.tables if t.name == "five_hour_blocks")

    assert len(blocks.rows) == 3
    assert sum(row[2] for row in blocks.rows) == len(timestamps)  # every turn accounted for
    # Every block has the same single session, but split turn counts.
    assert all(row[1] == 1 for row in blocks.rows)
    turns_per_block = sorted(row[2] for row in blocks.rows)
    # 00:00 block: 00:30..04:30 (5 turns); 05:00 block: 05:30..09:30 (5
    # turns); 10:00 block: 10:30..12:30 (3 turns).
    assert turns_per_block == [3, 5, 5]
