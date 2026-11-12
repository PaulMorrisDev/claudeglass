"""R10: a field a newer parser added or redefined reads "not measured" in a
digest an older parser wrote (``src/claudeglass/measured.py``). The table of
fields, the decoder's marker for a digest from before the stamp, and the
Data quality sentence; later tests in this file build a store with one older
session and follow it through every figure that leaves it out.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from claudeglass import PARSER_VERSION
from claudeglass.cache import DigestCache, encode_result, result_from_jsonable
from claudeglass.corpus import SessionBundle
from claudeglass.measured import (
    BEFORE_STAMP,
    FIELD_VERSIONS,
    NEWEST,
    is_older,
    measures,
    older_sentence,
    older_sessions,
)
from claudeglass.model import TranscriptMeta, TranscriptResult, Turn
from claudeglass.parse import parse_transcript
from claudeglass.service.rebuild import corpus_from_store

from helpers import fallback_read_session, store_with_older_sessions, turn_line, write_jsonl


def _parsed(tmp_path):
    path = tmp_path / "session-1.jsonl"
    write_jsonl(path, [turn_line(input_tokens=100, output_tokens=20), turn_line(input_tokens=110, output_tokens=25)])
    meta = TranscriptMeta(
        path=str(path), kind="top-level", session_id=path.stem, mtime_ns=1_000_000, size_bytes=path.stat().st_size
    )
    return path, meta, parse_transcript(path, meta)


def test_every_field_in_the_table_is_a_turn_or_result_field_at_or_below_the_parser_version():
    known = {f.name for f in dataclasses.fields(Turn)} | {f.name for f in dataclasses.fields(TranscriptResult)}
    assert set(FIELD_VERSIONS) <= known
    assert all(version <= PARSER_VERSION for version in FIELD_VERSIONS.values())
    assert NEWEST == max(FIELD_VERSIONS.values())


def test_a_digest_without_the_stamp_decodes_as_written_before_it(tmp_path):
    _, _, result = _parsed(tmp_path)
    data = json.loads(json.dumps(encode_result(result)))
    assert result_from_jsonable(data).parser_version == PARSER_VERSION
    data["parser_version"] = None
    assert result_from_jsonable(data).parser_version is None
    del data["parser_version"]
    assert result_from_jsonable(data).parser_version == BEFORE_STAMP == 0


def test_a_fresh_parse_measures_every_field(tmp_path):
    path, meta, result = _parsed(tmp_path)
    results = [result]
    cache = DigestCache(tmp_path / "config")
    cache.put(path, meta, result)
    results.append(cache.get(path, meta))
    when = datetime.now(timezone.utc) - timedelta(days=2)
    store, _ = store_with_older_sessions(
        tmp_path / "store", {"sess-a": fallback_read_session("sess-a", when)}, older=set()
    )
    [bundle] = corpus_from_store(store).sessions
    results.append(bundle.top)
    assert len(results) == 3
    for each in results:
        assert each is not None and each.parser_version == PARSER_VERSION
        assert measures(each, *FIELD_VERSIONS)
        assert not is_older(each)


@pytest.mark.parametrize("field", sorted(FIELD_VERSIONS))
def test_an_unstamped_digest_measures_none_of_the_newer_fields(field):
    result = TranscriptResult(parser_version=BEFORE_STAMP)
    assert not measures(result, field)
    assert is_older(result)


def test_a_result_built_in_memory_reads_as_current():
    result = TranscriptResult()
    assert result.parser_version is None
    assert measures(result, *FIELD_VERSIONS)
    assert not is_older(result)


def test_older_sessions_counts_a_session_once_when_any_of_its_transcripts_is_older():
    fresh = TranscriptResult(parser_version=PARSER_VERSION)
    old = TranscriptResult(parser_version=BEFORE_STAMP)
    bundles = [
        SessionBundle(session_id="a", top=fresh, subs=[fresh]),
        SessionBundle(session_id="b", top=fresh, subs=[old]),
        SessionBundle(session_id="c", top=old, subs=[old, old]),
        SessionBundle(session_id="d", top=None, subs=[old]),
    ]
    assert older_sessions(bundles) == (2, 3)


def test_the_older_sentence_in_the_singular_the_plural_and_never_at_zero():
    from test_help_coverage import _plain

    assert older_sentence(1, 2) == (
        "1 of 2 sessions in this window was read by an older version and its transcripts are gone, "
        "so the newer figures leave it out."
    )
    assert older_sentence(73, 82) == (
        "73 of 82 sessions in this window were read by an older version and their transcripts are gone, "
        "so the newer figures leave them out."
    )
    assert older_sentence(1, 1) == (
        "1 of 1 session in this window was read by an older version and its transcripts are gone, "
        "so the newer figures leave it out."
    )
    assert older_sentence(0, 5) == ""
    for n, m in ((1, 2), (73, 82), (1, 1)):
        _plain(older_sentence(n, m), f"older_sentence({n}, {m})")


# -- a store with one session read by an older version ----------------------------------------------

from claudeglass import habits  # noqa: E402
from claudeglass.config import Config  # noqa: E402
from claudeglass.model import CaptureTag  # noqa: E402
from claudeglass.pricing import load_pricing  # noqa: E402
from claudeglass.report import build_report  # noqa: E402

PRICING = load_pricing()
FIELDS = sorted(FIELD_VERSIONS)


@pytest.fixture(scope="module")
def older_store(tmp_path_factory):
    """Sessions ``sess-new`` and ``sess-old``, identical, each a Bash
    ``cat a || cat b``; the second is a 0.16.0 digest of a deleted transcript."""
    when = datetime.now(timezone.utc) - timedelta(days=2)
    sessions = {sid: fallback_read_session(sid, when) for sid in ("sess-new", "sess-old")}
    store, _ = store_with_older_sessions(tmp_path_factory.mktemp("older"), sessions, older={"sess-old"})
    return store


def _table_rows(report, name: str) -> list[dict]:
    for section in report.sections:
        for table in section.tables:
            if table.name == name:
                keys = [column.key for column in table.columns]
                return [dict(zip(keys, row)) for row in table.rows]
    return []


def _report_of(corpus):
    return build_report(corpus, PRICING, Config(), projects=("proj-a",), window="test")


def test_each_new_count_is_not_measured_for_the_older_row(older_store):
    bundles = {b.session_id: b for b in corpus_from_store(older_store).sessions}
    assert bundles["sess-new"].top.parser_version == PARSER_VERSION
    assert bundles["sess-old"].top.parser_version == BEFORE_STAMP
    for name in FIELDS:
        assert measures(bundles["sess-new"].top, name)
        assert not measures(bundles["sess-old"].top, name)
    # The digest holds the pre-46 reading of the fallback (2 reads, not 1).
    reads = {sid: [t.shell_read_count for t in b.top.turns if t.shell_read_count] for sid, b in bundles.items()}
    assert reads == {"sess-new": [1], "sess-old": [2]}
    facts = {c.session_id: c for c in habits.collect(corpus_from_store(older_store), PRICING).cycles}
    assert (facts["sess-new"].calls, facts["sess-new"].probe_calls) == (2, 1)
    assert facts["sess-old"].probe_calls is None
    assert (facts["sess-old"].probe_shell_calls, facts["sess-old"].probe_runs, facts["sess-old"].probe_batch_cost) == (
        None,
        None,
        None,
    )


def test_a_redefined_count_is_never_summed_across_the_two_rows(older_store):
    mixed = _report_of(corpus_from_store(older_store))
    new_only = _report_of(corpus_from_store(older_store, session_ids=["sess-new"]))
    # The old digest reads `cat a || cat b` as two reads, so a sum over both rows
    # would count its batch as the other row's single lookup.
    assert _table_rows(mixed, "habits_probes") == _table_rows(new_only, "habits_probes")
    assert _table_rows(mixed, "habits_probes") == [
        {"agent_type": "top-level", "calls": 2, "probes": 1, "shell": 1, "runs": 0, "batch_cost": 0.0}
    ]


# -- every table answers as it would with the older row removed ------------------------------------


def _probing(probes: int | None, shell: int = 0) -> dict:
    """The four probe figures of a fact: ``probes`` single lookups, ``shell``
    of them by shell command; ``None`` is a digest an older parser wrote."""
    if probes is None:
        return dict(probe_calls=None, probe_shell_calls=None, probe_runs=None, probe_batch_cost=None)
    return dict(probe_calls=probes, probe_shell_calls=shell)


def _cycle_fact(*, probes: int | None, shell: int = 0, calls: int = 4, level: str = "easy") -> habits.CycleFact:
    return habits.CycleFact(
        session_id="s", ts=None, week="2026-08-03", cost=1.0, turns=calls, calls=calls, tag=CaptureTag(level=level),
        **_probing(probes, shell),
    )


def _agent_fact(*, probes: int | None, shell: int = 0, calls: int = 4) -> habits.AgentFact:
    return habits.AgentFact(
        "s", "Explore", "2026-08-03", 1.0, calls=calls, launch="background", task="explore", **_probing(probes, shell)
    )


def _habits_of(*, with_older: bool) -> habits.Habits:
    cycles = [_cycle_fact(probes=2, shell=1), _cycle_fact(probes=1, shell=1)]
    agents = [_agent_fact(probes=2, shell=1), _agent_fact(probes=1, shell=1)]
    if with_older:
        cycles.append(_cycle_fact(probes=None, calls=10))
        agents.append(_agent_fact(probes=None, calls=10))
    return habits.Habits(cycles=cycles, agents=agents)


def _builder_rows(table: str, h: habits.Habits) -> list[dict]:
    built = {
        "habits_probes": habits._probes_table,
        "habits_agents": habits._agents_table,
        "habits_agent_runs": habits._run_receipts_table,
        "habits_agents_by_task": habits._agents_by_task_table,
    }[table](h)
    keys = [column.key for column in built.columns]
    return [dict(zip(keys, row)) for row in built.rows]


@pytest.mark.parametrize(
    "table, columns",
    [
        ("habits_probes", ["calls", "probes", "shell", "runs", "batch_cost"]),
        ("habits_agents", ["probe_pct", "probe_shell_pct"]),
        ("habits_agent_runs", ["probe_pct"]),
        ("habits_agents_by_task", ["probe_pct", "probe_shell_pct"]),
    ],
)
def test_each_table_answers_as_with_the_older_row_removed(table, columns):
    mixed = _builder_rows(table, _habits_of(with_older=True))
    clean = _builder_rows(table, _habits_of(with_older=False))
    assert mixed and len(mixed) == len(clean)
    for with_older, without in zip(mixed, clean):
        for column in columns:
            assert with_older[column] is not None
            assert with_older[column] == pytest.approx(without[column]), (table, column)


def test_a_column_no_version_changed_still_counts_the_older_row():
    mixed = {r["agent_type"]: r for r in _builder_rows("habits_agents", _habits_of(with_older=True))}
    clean = {r["agent_type"]: r for r in _builder_rows("habits_agents", _habits_of(with_older=False))}
    assert (mixed["top-level"]["runs"], mixed["Explore"]["runs"]) == (3, 3)
    assert (clean["top-level"]["runs"], clean["Explore"]["runs"]) == (2, 2)
    assert mixed["top-level"]["cost"] == 3.0 and mixed["Explore"]["cost"] == 3.0
    receipts = _builder_rows("habits_agent_runs", _habits_of(with_older=True))
    assert [r["calls"] for r in receipts] == [18]


def test_a_table_with_no_measured_reply_leaves_the_row_out_of_the_probes_and_the_percentages():
    h = habits.Habits(cycles=[_cycle_fact(probes=None)], agents=[_agent_fact(probes=None)])
    assert _builder_rows("habits_probes", h) == []
    [top, agent] = _builder_rows("habits_agents", h)
    assert (top["probe_pct"], top["probe_shell_pct"], agent["probe_pct"], agent["probe_shell_pct"]) == (None,) * 4
    [group] = _builder_rows("habits_agents_by_task", h)
    assert group["probe_pct"] is None and group["runs"] == 1
    [receipt] = _builder_rows("habits_agent_runs", h)
    assert receipt["probe_pct"] is None and receipt["calls"] == 4


def test_the_playbook_clauses_on_lone_lookups_take_numerator_and_base_from_measured_cycles():
    from test_agent_runs import _looker

    named = [_looker(2, 4, 0, flags=("path",)) for _ in range(habits.MIN_GROUP)]
    unnamed = [_looker(8, 4, 3) for _ in range(habits.MIN_GROUP)]
    older = [_looker(8, 40, None, probe_shell_calls=None, probe_runs=None, probe_batch_cost=None) for _ in range(2)]
    older_named = [_looker(2, 40, None, flags=("path",), probe_shell_calls=None) for _ in range(2)]
    clean = habits.Habits(cycles=named + unnamed)
    mixed = habits.Habits(cycles=named + unnamed + older + older_named)
    n = habits.MIN_GROUP
    assert "75% of the replies to those that didn't made one lookup and nothing else, against 0%." in (
        habits._item_name_files(clean).evidence
    )
    # The older cycles still count in the reads the item's other clauses use.
    assert "75% of the replies to those that didn't" in habits._item_name_files(mixed).evidence
    assert f"{3 * n} of their {4 * n} replies made one lookup and nothing else" in (
        habits._item_explore_research(clean).evidence
    )
    assert f"{3 * n} of their {4 * n} replies made one lookup and nothing else" in (
        habits._item_explore_research(mixed).evidence
    )
    # With only older cycles the clause is left out, not read as 0 of a base.
    only_older = habits.Habits(cycles=[_looker(2, 4, None, flags=("path",), probe_shell_calls=None) for _ in range(n)]
                               + [_looker(8, 4, None, probe_shell_calls=None) for _ in range(n)])
    assert "one lookup" not in habits._item_name_files(only_older).evidence
    assert "one lookup" not in habits._item_explore_research(only_older).evidence


def test_the_tips_of_the_older_row_are_not_measured(older_store):
    from claudeglass import prompting

    by_id = {s.session_id: s for s in prompting.collect(corpus_from_store(older_store), PRICING)}
    assert set(by_id) == {"sess-new", "sess-old"}
    assert by_id["sess-new"].tips_measured is True
    assert by_id["sess-old"].tips_measured is False


# -- the Data quality line ------------------------------------------------------------------------


def test_a_store_with_one_older_session_says_1_of_2_on_data_quality(older_store):
    from claudeglass import helptext
    from claudeglass.model import Diagnostics
    from claudeglass.render.html import render_html
    from claudeglass.render.markdown import render_markdown

    model = _report_of(corpus_from_store(older_store))
    assert (model.meta.older_sessions, model.meta.sessions) == (1, 2)
    sentence = older_sentence(1, 2)
    table = helptext.diagnostics_table(Diagnostics(), older=(1, 2))
    assert table.rows[-1] == ["older_sessions", "1 of 2", sentence]
    assert table.value_labels["older_sessions"] == helptext.OLDER_SESSIONS_LABEL == "Sessions read by an older version"
    # With no older session the page has no such row and every other row is where it was.
    plain = helptext.diagnostics_table(Diagnostics())
    assert helptext.diagnostics_table(Diagnostics(), older=(0, 2)).rows == plain.rows
    assert helptext.diagnostics_table(Diagnostics(), older=None).rows == plain.rows
    assert table.rows[:-1] == plain.rows
    # The reports say it once, and only when a session was read by an older version.
    assert render_markdown(model).count(sentence) == 1
    assert render_html(model).count(sentence) == 1
    only_new = _report_of(corpus_from_store(older_store, session_ids=["sess-new"]))
    assert (only_new.meta.older_sessions, only_new.meta.sessions) == (0, 1)
    assert "read by an older version" not in render_markdown(only_new)
    assert "read by an older version" not in render_html(only_new)
