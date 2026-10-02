"""Tests for WP11's corpus assembly (``src/claudeglass/corpus.py``):
a tmp project tree with two sessions and one subagent, ``jobs=1`` vs
``jobs=2`` producing equal ``to_jsonable`` output, cold-then-warm cache
behaviour, ``exclude_projects``, and window filters.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from claudeglass.cache import DigestCache
from claudeglass.corpus import Corpus, SessionBundle, load_corpus
from claudeglass.pricing import load_pricing
from claudeglass.render.json_out import to_jsonable

from helpers import turn_line, write_jsonl

_OLD_TS = time.time() - 3600  # one hour ago: well past the 60s live window


def _write_top(project_dir: Path, session_id: str, n_turns: int = 2, mtime: float | None = None) -> Path:
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(path, [turn_line(input_tokens=100 + i, output_tokens=20 + i) for i in range(n_turns)])
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_subagent(
    project_dir: Path,
    session_id: str,
    agent_id: str,
    n_turns: int = 1,
    meta: dict | None = None,
    workflow_run_id: str | None = None,
) -> Path:
    if workflow_run_id is not None:
        agent_dir = project_dir / session_id / "subagents" / "workflows" / workflow_run_id
    else:
        agent_dir = project_dir / session_id / "subagents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = agent_dir / f"{agent_id}.jsonl"
    write_jsonl(jsonl_path, [turn_line(input_tokens=50 + i, output_tokens=10 + i) for i in range(n_turns)])
    meta_path = agent_dir / f"{agent_id}.meta.json"
    meta_payload = {"agentType": "claude-implementer", "model": "claude-sonnet-5"}
    if meta:
        meta_payload.update(meta)
    meta_path.write_text(json.dumps(meta_payload), encoding="utf-8")
    return jsonl_path


def _write_workflow(project_dir: Path, session_id: str, run_id: str, agent_count: int = 1) -> Path:
    workflows_dir = project_dir / session_id / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    path = workflows_dir / f"{run_id}.json"
    path.write_text(json.dumps({"runId": run_id, "agentCount": agent_count, "phases": []}), encoding="utf-8")
    return path


# -- basic assembly: two sessions, one subagent ------------------------------


def test_two_sessions_one_subagent(tmp_path):
    project_dir = tmp_path / "proj-a"
    project_dir.mkdir()
    _write_top(project_dir, "session-001")
    _write_top(project_dir, "session-002")
    _write_subagent(project_dir, "session-002", "agent-aaa111")

    corpus = load_corpus([project_dir])

    assert isinstance(corpus, Corpus)
    assert len(corpus.sessions) == 2
    by_id = {s.session_id: s for s in corpus.sessions}
    assert set(by_id) == {"session-001", "session-002"}

    session_1 = by_id["session-001"]
    assert isinstance(session_1, SessionBundle)
    assert session_1.top is not None
    assert len(session_1.top.turns) == 2
    assert session_1.subs == []
    assert session_1.workflows == []
    assert session_1.slug == "proj-a"
    assert session_1.project_dir == str(project_dir)

    session_2 = by_id["session-002"]
    assert len(session_2.subs) == 1
    assert len(session_2.subs[0].turns) == 1

    assert corpus.total_files == 3  # 2 top-level + 1 subagent
    assert corpus.cache_hits == 0
    assert corpus.cache_misses == 0  # no cache was supplied
    assert corpus.total_bytes > 0
    assert corpus.elapsed_s >= 0.0


def test_empty_project_dir_yields_empty_corpus(tmp_path):
    project_dir = tmp_path / "empty-proj"
    project_dir.mkdir()
    corpus = load_corpus([project_dir])
    assert corpus.sessions == []
    assert corpus.total_files == 0
    assert corpus.cache_hits == 0
    assert corpus.cache_misses == 0


def test_nonexistent_project_dir_is_tolerated(tmp_path):
    corpus = load_corpus([tmp_path / "does-not-exist"])
    assert corpus.sessions == []
    assert corpus.total_files == 0


# -- deterministic ordering ---------------------------------------------------


def test_sessions_ordered_by_first_ts_then_session_id(tmp_path):
    project_dir = tmp_path / "proj-order"
    project_dir.mkdir()
    # session-b's turns start earlier than session-a's.
    write_jsonl(
        project_dir / "session-a.jsonl",
        [turn_line(timestamp="2026-09-18T14:00:00.000Z")],
    )
    write_jsonl(
        project_dir / "session-b.jsonl",
        [turn_line(timestamp="2026-09-18T10:00:00.000Z")],
    )
    write_jsonl(
        project_dir / "session-c.jsonl",
        [turn_line(timestamp="2026-09-18T10:00:00.000Z")],  # ties with session-b
    )

    corpus = load_corpus([project_dir])
    ordered_ids = [s.session_id for s in corpus.sessions]
    assert ordered_ids == ["session-b", "session-c", "session-a"]


# -- jobs=1 vs jobs=2 determinism --------------------------------------------


def test_jobs_1_and_jobs_2_produce_equal_output(tmp_path):
    project_dir = tmp_path / "proj-jobs"
    project_dir.mkdir()
    for i in range(4):
        session_id = f"session-{i:03d}"
        _write_top(project_dir, session_id, n_turns=2)
        if i % 2 == 0:
            _write_subagent(project_dir, session_id, f"agent-{i:03d}")

    corpus_serial = load_corpus([project_dir], jobs=1)
    corpus_parallel = load_corpus([project_dir], jobs=2)

    serial_jsonable = [to_jsonable(s) for s in corpus_serial.sessions]
    parallel_jsonable = [to_jsonable(s) for s in corpus_parallel.sessions]
    assert serial_jsonable == parallel_jsonable
    assert corpus_serial.total_files == corpus_parallel.total_files
    assert corpus_serial.total_bytes == corpus_parallel.total_bytes


# -- cache: cold then warm ----------------------------------------------------


def test_cold_then_warm_uses_cache_on_second_call(tmp_path):
    project_dir = tmp_path / "proj-cache"
    project_dir.mkdir()
    _write_top(project_dir, "session-001", mtime=_OLD_TS)
    sub_path = _write_subagent(project_dir, "session-001", "agent-001")
    os.utime(sub_path, (_OLD_TS, _OLD_TS))

    cache = DigestCache(tmp_path / "cache-config")

    cold = load_corpus([project_dir], cache=cache)
    assert cold.cache_misses == 2
    assert cold.cache_hits == 0

    warm = load_corpus([project_dir], cache=cache)
    assert warm.cache_hits == 2
    assert warm.cache_misses == 0

    cold_jsonable = [to_jsonable(s) for s in cold.sessions]
    warm_jsonable = [to_jsonable(s) for s in warm.sessions]
    assert cold_jsonable == warm_jsonable


def test_cache_miss_again_after_file_is_modified(tmp_path):
    project_dir = tmp_path / "proj-cache-invalidate"
    project_dir.mkdir()
    top_path = _write_top(project_dir, "session-001", mtime=_OLD_TS)
    cache = DigestCache(tmp_path / "cache-config")

    first = load_corpus([project_dir], cache=cache)
    assert first.cache_misses == 1

    # Rewrite with different content/size, still backdated past the live
    # window, so the next call is a fresh miss rather than a stale hit.
    write_jsonl(top_path, [turn_line(input_tokens=999, output_tokens=999) for _ in range(3)])
    os.utime(top_path, (_OLD_TS, _OLD_TS))

    second = load_corpus([project_dir], cache=cache)
    assert second.cache_misses == 1
    assert second.sessions[0].top.turns[0].input_tokens == 999


# -- exclude_projects ----------------------------------------------------


def test_exclude_projects_is_honoured(tmp_path):
    keep_dir = tmp_path / "proj-keep"
    keep_dir.mkdir()
    _write_top(keep_dir, "session-keep")

    excluded_dir = tmp_path / "proj-exclude-me"
    excluded_dir.mkdir()
    _write_top(excluded_dir, "session-excluded")

    corpus = load_corpus([keep_dir, excluded_dir], exclude_projects=["exclude"])

    assert len(corpus.sessions) == 1
    assert corpus.sessions[0].session_id == "session-keep"
    assert corpus.sessions[0].slug == "proj-keep"


def test_exclude_projects_malformed_regex_is_skipped_not_fatal(tmp_path):
    project_dir = tmp_path / "proj-a"
    project_dir.mkdir()
    _write_top(project_dir, "session-001")

    corpus = load_corpus([project_dir], exclude_projects=["[unterminated"])
    assert len(corpus.sessions) == 1


# -- window filters ------------------------------------------------------


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def _write_top_replied(project_dir: Path, session_id: str, *, last_reply: float, mtime: float) -> Path:
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(path, [turn_line(timestamp=_iso(last_reply - 600)), turn_line(timestamp=_iso(last_reply))])
    os.utime(path, (mtime, mtime))
    return path


def _three_sessions(project_dir: Path) -> None:
    """A recent session; one whose replies are ten days old but whose file
    changed an hour ago, as when Claude Code appends a title to an old
    transcript; and one old in both."""
    now = time.time()
    _write_top_replied(project_dir, "session-recent", last_reply=now - 3600, mtime=now - 3600)
    _write_top_replied(project_dir, "session-touched", last_reply=now - 10 * 86400, mtime=now - 3600)
    _write_top_replied(project_dir, "session-old", last_reply=now - 10 * 86400, mtime=now - 10 * 86400)


def test_days_window_counts_sessions_by_their_last_reply(tmp_path):
    project_dir = tmp_path / "proj-window"
    project_dir.mkdir()
    _three_sessions(project_dir)

    corpus = load_corpus([project_dir], days=5)

    assert {s.session_id for s in corpus.sessions} == {"session-recent"}
    assert corpus.total_files == 1


def test_mtime_window_counts_sessions_by_their_file(tmp_path):
    project_dir = tmp_path / "proj-window"
    project_dir.mkdir()
    _three_sessions(project_dir)

    corpus = load_corpus([project_dir], days=5, window_by="mtime")

    assert {s.session_id for s in corpus.sessions} == {"session-recent", "session-touched"}


def test_until_keeps_a_session_whose_file_changed_after_the_window(tmp_path):
    project_dir = tmp_path / "proj-until"
    project_dir.mkdir()
    now = time.time()
    _write_top_replied(project_dir, "session-in", last_reply=now - 3 * 86400, mtime=now - 3600)
    _write_top_replied(project_dir, "session-after", last_reply=now - 3600, mtime=now - 3600)

    corpus = load_corpus([project_dir], since=_iso(now - 5 * 86400), until=_iso(now - 2 * 86400))

    assert {s.session_id for s in corpus.sessions} == {"session-in"}


def test_limit_caps_number_of_sessions(tmp_path):
    project_dir = tmp_path / "proj-limit"
    project_dir.mkdir()
    for i in range(5):
        _write_top(project_dir, f"session-{i:03d}", mtime=time.time() - i)

    corpus = load_corpus([project_dir], limit=2)
    assert len(corpus.sessions) == 2


# -- progress callback ------------------------------------------------------


def test_progress_callback_reaches_total(tmp_path):
    project_dir = tmp_path / "proj-progress"
    project_dir.mkdir()
    _write_top(project_dir, "session-001")
    _write_subagent(project_dir, "session-001", "agent-001")
    _write_subagent(project_dir, "session-001", "agent-002")

    calls: list[tuple[int, int]] = []
    corpus = load_corpus([project_dir], progress=lambda done, total: calls.append((done, total)))

    assert calls, "progress should have been called at least once"
    assert calls[-1] == (corpus.total_files, corpus.total_files)
    # done strictly increases and total is fixed across every call.
    totals = {total for _done, total in calls}
    assert totals == {corpus.total_files}
    dones = [done for done, _total in calls]
    assert dones == sorted(dones)
    assert len(dones) == len(set(dones))


# -- workflow linking ------------------------------------------------------


def test_workflow_runs_are_linked_and_costed(tmp_path):
    project_dir = tmp_path / "proj-workflow"
    project_dir.mkdir()
    _write_top(project_dir, "session-wf")
    _write_workflow(project_dir, "session-wf", "wf_test-000", agent_count=1)
    _write_subagent(
        project_dir,
        "session-wf",
        "agent-work1",
        n_turns=1,
        workflow_run_id="wf_test-000",
    )

    corpus = load_corpus([project_dir])
    bundle = corpus.sessions[0]

    assert len(bundle.workflows) == 1
    run = bundle.workflows[0]
    assert run.run_id == "wf_test-000"
    assert run.agent_count == 1
    # The workflow-nested subagent is still surfaced in `subs` (discovery
    # globs both shapes) and its cost rolled into the run via
    # workflows.link_workflow_agents.
    assert len(bundle.subs) == 1
    assert run.cost > 0.0


def _write_card(path: Path, input_rate: float) -> Path:
    """A rate card pricing ``claude-sonnet-5`` input only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'version = "test"\n\n[models."claude-sonnet-5"]\n'
        f"input = {input_rate}\noutput = 0.0\ncache_write_5m = 0.0\ncache_write_1h = 0.0\ncache_read = 0.0\n",
        encoding="utf-8",
    )
    return path


def _workflow_corpus(tmp_path: Path) -> Path:
    project_dir = tmp_path / "proj-workflow"
    project_dir.mkdir()
    _write_top(project_dir, "session-wf")
    _write_workflow(project_dir, "session-wf", "wf_test-000", agent_count=1)
    _write_subagent(project_dir, "session-wf", "agent-work1", n_turns=1, workflow_run_id="wf_test-000")
    return project_dir


def test_workflow_runs_are_costed_at_the_config_pricing_path(tmp_path):
    """The card config.toml names, as the report and the service use, ahead
    of the config-dir one. The subagent's one reply reads 50 input tokens."""
    project_dir = _workflow_corpus(tmp_path)
    config_dir = tmp_path / "config"
    _write_card(config_dir / "pricing.toml", 1_000.0)
    custom = _write_card(tmp_path / "elsewhere" / "rates.toml", 1_000_000.0)
    (config_dir / "config.toml").write_text(f"pricing_path = {json.dumps(str(custom))}\n", encoding="utf-8")

    run = load_corpus([project_dir], config_dir=config_dir).sessions[0].workflows[0]

    assert run.cost == pytest.approx(50.0)


def test_a_card_passed_in_wins_over_the_config_pricing_path(tmp_path):
    """The CLI's --pricing card arrives as ``rates`` and prices workflow runs
    too, as it prices the rest of the report."""
    project_dir = _workflow_corpus(tmp_path)
    config_dir = tmp_path / "config"
    custom = _write_card(tmp_path / "elsewhere" / "rates.toml", 1_000.0)
    (config_dir / "config.toml").parent.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(f"pricing_path = {json.dumps(str(custom))}\n", encoding="utf-8")
    passed = load_pricing(path=_write_card(tmp_path / "flag" / "rates.toml", 1_000_000.0))

    run = load_corpus([project_dir], config_dir=config_dir, rates=passed).sessions[0].workflows[0]

    assert run.cost == pytest.approx(50.0)


def test_an_unreadable_pricing_path_falls_back_to_the_config_dir_card(tmp_path):
    project_dir = _workflow_corpus(tmp_path)
    config_dir = tmp_path / "config"
    _write_card(config_dir / "pricing.toml", 1_000_000.0)
    (config_dir / "config.toml").write_text(
        f"pricing_path = {json.dumps(str(tmp_path / 'no-such-card.toml'))}\n", encoding="utf-8"
    )

    run = load_corpus([project_dir], config_dir=config_dir).sessions[0].workflows[0]

    assert run.cost == pytest.approx(50.0)
