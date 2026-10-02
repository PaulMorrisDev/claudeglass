"""``tools/scrub.py``: whitelist-rewrite of a session directory into a
privacy-safe fixture, HMAC id rehashing, and the independent ``--verify``
privacy scan (WP12a).

Every fixture built here is synthetic (``tests/helpers.py`` builders or
hand-built dicts) -- this file never touches a real transcript.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from claudeglass import agent_roles
from claudeglass.tools import scrub

from helpers import (
    attachment_line,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

_KEY = b"\x01" * 32


# -- scrub_line: allowlisted fields survive -------------------------------


def test_scrub_line_keeps_type_subtype_timestamp_verbatim():
    line = system_line("compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 1000, "postTokens": 200})
    out = scrub.scrub_line(line, _KEY)

    assert out["type"] == "system"
    assert out["subtype"] == "compact_boundary"
    assert out["timestamp"] == line["timestamp"]
    # trigger isn't on the plan's keep-list -> dropped, numerics kept.
    assert "trigger" not in out["compactMetadata"]
    assert out["compactMetadata"] == {"preTokens": 1000, "postTokens": 200}


def test_scrub_line_rehashes_ids_length_preserving():
    line = turn_line(message_id="msg_abcdef", uuid="uuid-1234-5678", request_id="req_000111")
    out = scrub.scrub_line(line, _KEY)

    assert out["message"]["id"] != "msg_abcdef"
    assert len(out["message"]["id"]) == len("msg_abcdef")
    assert all(c in "0123456789abcdef" for c in out["message"]["id"])

    assert out["uuid"] != "uuid-1234-5678"
    assert len(out["uuid"]) == len("uuid-1234-5678")

    assert out["requestId"] != "req_000111"
    assert len(out["requestId"]) == len("req_000111")


def test_rehash_id_is_deterministic_given_the_same_key():
    a = scrub._rehash_id(_KEY, "some-real-session-id")
    b = scrub._rehash_id(_KEY, "some-real-session-id")
    assert a == b
    assert a != "some-real-session-id"
    assert len(a) == len("some-real-session-id")


def test_rehash_id_differs_across_keys():
    other_key = b"\x02" * 32
    a = scrub._rehash_id(_KEY, "some-real-session-id")
    b = scrub._rehash_id(other_key, "some-real-session-id")
    assert a != b


def test_scrub_line_keeps_message_usage_numerics_and_model():
    line = turn_line(
        model="claude-sonnet-5",
        input_tokens=123,
        output_tokens=45,
        cache_creation_input_tokens=300,
        ephemeral_5m_input_tokens=100,
        ephemeral_1h_input_tokens=200,
    )
    out = scrub.scrub_line(line, _KEY)

    usage = out["message"]["usage"]
    assert usage["input_tokens"] == 123
    assert usage["output_tokens"] == 45
    assert usage["cache_creation_input_tokens"] == 300
    assert usage["cache_creation"] == {
        "ephemeral_5m_input_tokens": 100,
        "ephemeral_1h_input_tokens": 200,
    }
    assert out["message"]["model"] == "claude-sonnet-5"


def test_scrub_line_keeps_tool_use_name_and_bash_command_verb_only():
    line = turn_line(content=[tool_use_block("Bash", "toolu_001", input={"command": "git status -sb"})])
    out = scrub.scrub_line(line, _KEY)

    block = out["message"]["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "Bash"
    assert block["input"]["command"].startswith("git")
    assert block["input"]["command"] != "git status -sb"
    assert len(block["input"]["command"]) == len("git status -sb")
    # the id is rehashed, not dropped, so tool_result can still join it.
    assert block["id"] != "toolu_001"
    assert len(block["id"]) == len("toolu_001")


def test_scrub_command_falls_back_to_full_x_run_when_first_token_is_not_a_bare_word():
    """A leading shell-variable assignment embeds a path in what a naive
    "first whitespace token" split would treat as the verb (e.g.
    ``P="/c/Users/alice/secret.log" cat "$P"``) -- found via a real-corpus
    dry run while building the WP12a real fixture. The whole command
    must fall back to a full-length x run rather than leaking that path.
    """
    command = 'P="/c/Users/alice/secret.log" cat "$P"'
    scrubbed = scrub._scrub_command(command)
    assert scrubbed == "x" * len(command)
    assert "alice" not in scrubbed
    assert "/c/" not in scrubbed


def test_scrub_command_falls_back_for_drive_path_or_url_leading_token():
    for command in (
        'SP="C:/Users/alice/scratch";cd "$SP"',
        "B=http://127.0.0.1:5124/api curl $B",
    ):
        scrubbed = scrub._scrub_command(command)
        assert scrubbed == "x" * len(command)


def test_scrub_line_collapses_other_tool_input_to_length_only():
    line = turn_line(content=[tool_use_block("Read", "toolu_002", input={"file_path": "C:\\Dev\\secret\\file.py"})])
    out = scrub.scrub_line(line, _KEY)

    block = out["message"]["content"][0]
    assert block["name"] == "Read"
    assert set(block["input"]) == {"_len"}
    assert isinstance(block["input"]["_len"], int)
    assert block["input"]["_len"] > 0


def test_tool_use_and_tool_result_ids_still_join_after_scrubbing():
    """The plan names ``toolUseId``/``tool_use_id`` for rehashing but not
    a ``tool_use`` block's own ``id`` -- this project rehashes it too
    (with the same key) so the two blocks still reference the same
    (scrubbed) id after the rewrite -- a documented decision, not a gap.
    """
    use_line = turn_line(content=[tool_use_block("Grep", "toolu_003", input={"pattern": "x"})])
    result_line = user_block_line([tool_result_block("toolu_003", "some tool output text")])

    use_out = scrub.scrub_line(use_line, _KEY)
    result_out = scrub.scrub_line(result_line, _KEY)

    used_id = use_out["message"]["content"][0]["id"]
    result_id = result_out["message"]["content"][0]["tool_use_id"]
    assert used_id == result_id


def test_scrub_line_caps_tool_result_content_at_2000_chars():
    huge = "y" * 5000
    line = user_block_line([tool_result_block("toolu_004", huge)])
    out = scrub.scrub_line(line, _KEY)

    content = out["message"]["content"][0]["content"]
    assert content == "x" * 2000


def test_scrub_line_never_carries_raw_tool_result_text():
    line = user_block_line([tool_result_block("toolu_005", "THE SECRET TRANSCRIPT TEXT")])
    out = scrub.scrub_line(line, _KEY)
    dumped = json.dumps(out)
    assert "SECRET" not in dumped
    assert "TRANSCRIPT" not in dumped


# -- user-string A2 category preservation ---------------------------------


def test_scrub_user_string_preserves_task_notification_prefix():
    text = "<task-notification agentId=\"abc123\">did the thing</task-notification>"
    scrubbed = scrub._scrub_user_string(text)
    assert scrubbed.startswith("<task-notification")
    assert len(scrubbed) == len(text)
    assert "abc123" not in scrubbed
    assert "did the thing" not in scrubbed


def test_scrub_user_string_preserves_interrupt_marker_full_prefix():
    text = "[Request interrupted by user for tool use]"
    scrubbed = scrub._scrub_user_string(text)
    assert scrubbed.startswith("[Request interrupted")
    assert len(scrubbed) == len(text)


def test_scrub_user_string_redacts_ordinary_human_text():
    text = "please fix the login bug in auth.py"
    scrubbed = scrub._scrub_user_string(text)
    assert scrubbed == "x" * len(text)


def test_scrub_line_generic_string_field_becomes_x_run():
    line = {"type": "mode", "mode": "plan-mode", "timestamp": "2026-09-18T12:00:00.000Z"}
    out = scrub.scrub_line(line, _KEY)
    assert out["type"] == "mode"
    assert out["mode"] == "x" * len("plan-mode")


def test_scrub_line_unknown_dict_field_is_dropped():
    line = {"type": "assistant", "someUnknownStructuredField": {"nested": "value"}}
    out = scrub.scrub_line(line, _KEY)
    assert "someUnknownStructuredField" not in out


# -- attachment / cwd ------------------------------------------------------


def test_scrub_attachment_line_keeps_only_type_and_x_fills_rendered():
    line = attachment_line("skill_listing", rendered="a" * 42, addedNames=["secret-skill"])
    out = scrub.scrub_line(line, _KEY)

    assert out["attachment"] == {"type": "skill_listing"}
    assert out["rendered"] == [{"content": "x" * 42}]


def test_scrub_line_hashes_cwd_to_a_short_slug():
    line = {"type": "user", "cwd": "C:\\Dev\\SomeSecretProject"}
    out = scrub.scrub_line(line, _KEY)
    assert out["cwd"] != "C:\\Dev\\SomeSecretProject"
    assert out["cwd"].startswith("proj-")
    assert "SomeSecretProject" not in out["cwd"]


# -- meta.json ---------------------------------------------------------


def test_scrub_meta_json_whitelists_fields_and_x_fills_description():
    raw = {
        "agentType": "code-reviewer",
        "description": "summarise the auth module for a customer named Alice",
        "spawnDepth": 1,
        "model": "claude-sonnet-5",
        "requestShape": "task",
        "stoppedByUser": False,
        "toolUseId": "toolu_meta_1",
        "worktreeBranch": "feature/secret-branch-name",
    }
    out = scrub.scrub_meta_json(raw, _KEY)

    assert out["agentType"] == "code-reviewer"
    assert out["model"] == "claude-sonnet-5"
    assert out["requestShape"] == "task"
    assert out["spawnDepth"] == 1
    assert out["stoppedByUser"] is False
    assert out["description"] == "x" * len(raw["description"])
    assert out["worktreeBranch"] == "x" * len(raw["worktreeBranch"])
    assert out["toolUseId"] != "toolu_meta_1"
    assert len(out["toolUseId"]) == len("toolu_meta_1")


def test_scrub_meta_json_keeps_workflow_phase_only_as_its_role_word():
    out = scrub.scrub_meta_json({"workflowPhase": "Implement"}, _KEY)
    assert out["workflowPhase"] == "implement"

    # A form maps to its canonical word, never stays as written.
    out = scrub.scrub_meta_json({"workflowPhase": "Verifying the Acme billing"}, _KEY)
    assert out["workflowPhase"] == "verify"
    assert "Acme" not in json.dumps(out)


def test_scrub_meta_json_drops_a_workflow_phase_with_no_role_word():
    out = scrub.scrub_meta_json({"workflowPhase": "Pricing for Acme"}, _KEY)
    assert "workflowPhase" not in out
    assert "Acme" not in json.dumps(out)


def test_scrub_meta_json_drops_a_workflow_phase_that_is_not_a_string():
    for phase in (None, 7, True, ["implement"], {"phase": "implement"}):
        assert "workflowPhase" not in scrub.scrub_meta_json({"workflowPhase": phase}, _KEY)


def test_scrub_meta_json_description_with_a_role_word_keeps_the_word_and_the_length():
    description = "Fix the Acme parser in C:/x"
    out = scrub.scrub_meta_json({"description": description}, _KEY)

    assert out["description"] == "fix" + " " + "x" * (len(description) - len("fix") - 1)
    assert len(out["description"]) == len(description)
    assert "Acme" not in out["description"]
    assert "C:" not in out["description"]


def test_scrub_meta_json_description_keeps_the_canonical_word_not_the_form():
    description = "impl: the Acme importer"
    out = scrub.scrub_meta_json({"description": description}, _KEY)
    assert out["description"] == "implement " + "x" * (len(description) - len("implement") - 1)


def test_scrub_meta_json_description_shorter_than_its_word_is_just_the_word():
    out = scrub.scrub_meta_json({"description": "impl"}, _KEY)
    assert out["description"] == "implement"


def test_scrub_meta_json_description_with_no_role_word_is_all_x():
    description = "Pricing for the Acme parser"
    out = scrub.scrub_meta_json({"description": description}, _KEY)
    assert out["description"] == "x" * len(description)


def test_scrub_meta_json_description_role_word_past_the_fourth_token_is_ignored():
    description = "one two three four fix the Acme parser"
    out = scrub.scrub_meta_json({"description": description}, _KEY)
    assert out["description"] == "x" * len(description)


def test_scrub_meta_json_role_words_survive_a_second_read():
    # The scrubbed fixture has to tell the same agents apart as the original.
    raw = {"workflowPhase": "Reviewing", "description": "Implementation of the Acme importer"}
    out = scrub.scrub_meta_json(raw, _KEY)
    assert agent_roles.role_word(out["workflowPhase"], None, None) == "review"
    assert agent_roles.role_word(None, None, out["description"]) == "implement"
    assert agent_roles.role_word(None, None, out["description"]) == agent_roles.role_word(None, None, raw["description"])


# -- workflow json -------------------------------------------------------


def test_scrub_workflow_json_drops_script_logs_summary_result():
    raw = {
        "runId": "wf_abc123",
        "script": "const prompt = 'full task prompt text';",
        "logs": ["secret log line"],
        "summary": "secret summary",
        "result": {"anything": "secret"},
        "workflowProgress": [{"title": "secret phase title"}],
        "status": "completed",
        "agentCount": 3,
        "durationMs": 5000,
        "startTime": 1758000000000,
        "totalTokens": 999,
        "totalToolCalls": 7,
        "phases": [{"title": "Discovery", "detail": "secret detail text"}, {"title": "implement"}],
    }
    out = scrub.scrub_workflow_json(raw, _KEY)

    for forbidden in ("script", "logs", "summary", "result", "workflowProgress"):
        assert forbidden not in out
    dumped = json.dumps(out)
    assert "secret" not in dumped
    assert "prompt text" not in dumped

    assert out["status"] == "completed"
    assert out["agentCount"] == 3
    assert out["durationMs"] == 5000
    assert out["startTime"] == 1758000000000
    assert out["totalTokens"] == 999
    assert out["totalToolCalls"] == 7
    # "Discovery" is on the allowlist (case-insensitive) -> kept; a
    # non-allowlisted title would fall back to "phase-<index>".
    assert out["phases"][0]["title"] == "Discovery"
    assert out["phases"][1]["title"] == "implement"
    assert out["runId"] != "wf_abc123"


def test_scrub_workflow_json_non_allowlisted_title_becomes_phase_n():
    raw = {"runId": "wf_x", "phases": [{"title": "Fix the customer's billing issue"}]}
    out = scrub.scrub_workflow_json(raw, _KEY)
    assert out["phases"][0]["title"] == "phase-0"


def test_scrub_workflow_json_keeps_a_role_word_title_verbatim():
    raw = {"runId": "wf_x", "phases": [{"title": "Judge"}, {"title": " Reviewers "}, {"title": "Judge Acme"}]}
    out = scrub.scrub_workflow_json(raw, _KEY)
    assert [p["title"] for p in out["phases"]] == ["Judge", " Reviewers ", "phase-2"]
    assert "Acme" not in json.dumps(out)


# -- scrub_session end to end ---------------------------------------------


def _build_synthetic_session(root: Path) -> Path:
    """A tiny synthetic session directory in the exact layout
    ``discovery.py`` expects: ``<project_dir>/<session_id>.jsonl`` +
    ``<project_dir>/<session_id>/subagents/agent-*.jsonl`` (+meta) +
    ``<project_dir>/<session_id>/workflows/wf_*.json``.
    """
    project_dir = root / "proj"
    session_id = "session-real-0001"
    project_dir.mkdir(parents=True, exist_ok=True)

    write_jsonl(
        project_dir / f"{session_id}.jsonl",
        [
            turn_line(message_id="msg_1", content=[tool_use_block("Bash", "toolu_top_1", input={"command": "pytest -q"})]),
            user_block_line([tool_result_block("toolu_top_1", "1 passed")]),
            system_line("compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 5000, "postTokens": 100}),
            user_str_line("please review this for a user named Bob Smith"),
        ],
    )

    session_dir = project_dir / session_id
    subagents_dir = session_dir / "subagents"
    subagents_dir.mkdir(parents=True)
    for i in range(3):
        agent_path = subagents_dir / f"agent-{i:016x}.jsonl"
        write_jsonl(agent_path, [turn_line(message_id=f"agent{i}_msg", output_tokens=10)])
        agent_path.with_name(agent_path.stem + ".meta.json").write_text(
            json.dumps({"agentType": "implementer", "description": "secret task for Bob", "toolUseId": f"toolu_top_{i}"}),
            encoding="utf-8",
        )

    workflows_dir = session_dir / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "wf_run1.json").write_text(
        json.dumps({"runId": "wf_run1", "script": "secret script text", "status": "completed", "agentCount": 1}),
        encoding="utf-8",
    )

    return session_dir


def test_scrub_session_end_to_end_produces_a_privacy_clean_directory(tmp_path):
    session_dir = _build_synthetic_session(tmp_path / "in")
    out_dir = tmp_path / "out" / "session-a"

    manifest = scrub.scrub_session(session_dir, out_dir, _KEY)

    assert manifest["subagent_count"] == 3
    assert manifest["workflow_count"] == 1
    assert manifest["scrub_tool_version"] == scrub.SCRUB_TOOL_VERSION
    assert (out_dir / "manifest.json").exists()

    # Never leaks the human name / secret text anywhere in the output tree.
    for path in out_dir.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "Bob Smith" not in text
            assert "secret" not in text.lower() or "secret" not in text  # belt and braces

    ok, violations = scrub.verify_dir(out_dir)
    assert ok, violations

    # Round-trips through discovery/parse unchanged.
    from claudeglass import discovery
    from claudeglass.parse import parse_transcript

    sessions = discovery.find_sessions(out_dir)
    assert len(sessions) == 1
    top_meta = discovery.TranscriptMeta(path=str(sessions[0]), kind="top-level", session_id=sessions[0].stem)
    top_result = parse_transcript(sessions[0], top_meta)
    assert top_result.diagnostics.unparsable_lines == 0

    subs = discovery.find_subagents(out_dir, sessions[0].stem)
    assert len(subs) == 3


def test_scrub_session_keeps_role_words_in_workflow_sidecars_and_verifies_clean(tmp_path):
    session_dir = _build_synthetic_session(tmp_path / "in")
    run_dir = session_dir / "subagents" / "workflows" / "wf_run1"
    run_dir.mkdir(parents=True)
    agent_path = run_dir / "agent-00000000000000aa.jsonl"
    write_jsonl(agent_path, [turn_line(message_id="wf_agent_msg", output_tokens=10)])
    agent_path.with_name(agent_path.stem + ".meta.json").write_text(
        json.dumps(
            {
                "agentType": "workflow-subagent",
                "workflowPhase": "Implement",
                "description": "Fix the Acme parser in C:/x",
                "toolUseId": "toolu_wf_1",
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out" / "session-roles"

    scrub.scrub_session(session_dir, out_dir, _KEY)

    metas = [p for p in out_dir.rglob("*.meta.json") if "workflows" in p.parts]
    assert len(metas) == 1
    meta = json.loads(metas[0].read_text(encoding="utf-8"))
    assert meta["workflowPhase"] == "implement"
    assert meta["description"] == "fix" + " " + "x" * (len("Fix the Acme parser in C:/x") - len("fix") - 1)
    for path in out_dir.rglob("*"):
        if path.is_file():
            assert "Acme" not in path.read_text(encoding="utf-8")
    ok, violations = scrub.verify_dir(out_dir)
    assert ok, violations


def test_scrub_session_reproducible_with_key_seed(tmp_path):
    session_dir = _build_synthetic_session(tmp_path / "in")
    key_a = scrub._resolve_key("fixed-seed")
    key_b = scrub._resolve_key("fixed-seed")
    assert key_a == key_b

    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    scrub.scrub_session(session_dir, out_a, key_a)
    scrub.scrub_session(session_dir, out_b, key_b)

    files_a = sorted(p.name for p in out_a.rglob("*") if p.is_file())
    files_b = sorted(p.name for p in out_b.rglob("*") if p.is_file())
    assert files_a == files_b


# -- verify_dir --------------------------------------------------------


def test_verify_dir_fails_on_injected_alphanumeric_leak(tmp_path):
    out_dir = tmp_path / "leaky"
    out_dir.mkdir()
    (out_dir / "sess.jsonl").write_text(
        json.dumps({"type": "user", "someField": "leakedsecretword"}) + "\n", encoding="utf-8"
    )
    ok, violations = scrub.verify_dir(out_dir)
    assert not ok
    assert any("leakedsecretword" in v for v in violations)


def test_verify_dir_fails_on_windows_drive_path(tmp_path):
    out_dir = tmp_path / "leaky2"
    out_dir.mkdir()
    (out_dir / "sess.jsonl").write_text(
        json.dumps({"type": "user", "someField": "C:\\Users\\alice\\secret"}) + "\n", encoding="utf-8"
    )
    ok, violations = scrub.verify_dir(out_dir)
    assert not ok


def test_verify_dir_passes_on_scrubbed_role_words(tmp_path):
    out_dir = tmp_path / "roles"
    out_dir.mkdir()
    meta = scrub.scrub_meta_json(
        {"workflowPhase": "Implement", "description": "Fix the Acme parser in the billing service"}, _KEY
    )
    assert meta["workflowPhase"] == "implement" and meta["description"].startswith("fix x")
    (out_dir / "agent-0.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    ok, violations = scrub.verify_dir(out_dir)
    assert ok, violations


def test_verify_dir_fails_on_role_word_keys_carrying_other_text(tmp_path):
    out_dir = tmp_path / "leaky3"
    out_dir.mkdir()
    (out_dir / "agent-0.meta.json").write_text(
        json.dumps({"workflowPhase": "Pricing for Acme", "description": "fix the Acme parser"}), encoding="utf-8"
    )
    ok, violations = scrub.verify_dir(out_dir)
    assert not ok
    assert any("workflowPhase" in v and "Pricing" in v for v in violations)
    assert any("description" in v and "parser" in v for v in violations)


def test_verify_dir_passes_on_clean_x_runs(tmp_path):
    out_dir = tmp_path / "clean"
    out_dir.mkdir()
    (out_dir / "sess.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "xxxxxxxxxxxx"}}) + "\n", encoding="utf-8"
    )
    ok, violations = scrub.verify_dir(out_dir)
    assert ok, violations


# -- CLI (main) ----------------------------------------------------------


def test_main_scrubs_and_then_verifies_via_cli(tmp_path, capsys):
    session_dir = _build_synthetic_session(tmp_path / "in")
    out_dir = tmp_path / "out"

    rc = scrub.main(["--session-dir", str(session_dir), "--out", str(out_dir), "--key-seed", "test-seed"])
    assert rc == 0
    assert (out_dir / "manifest.json").exists()

    rc_verify = scrub.main(["--verify", str(out_dir)])
    assert rc_verify == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_main_requires_session_dir_and_out(capsys):
    rc = scrub.main([])
    assert rc == 2
