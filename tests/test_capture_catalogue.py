"""``capture_catalogue``: the metric catalogue, its levels, the note the
capture hook adds, and the JSON the hook reads. The note and the parser
must agree word for word, or a tag Claude writes as asked would be
dropped.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

import pytest

from claudeglass import capture_catalogue as cat
from claudeglass import capture_tags
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import attachment_line, turn_line, user_str_line, write_jsonl

#: Rough token budgets per note (characters / 4), main and subagent.
#: Real costs are measured from transcripts; these stop a note growing
#: unnoticed.
_BUDGETS = {
    # Essentials carries size too, since the before-and-after comparison
    # splits by it: about 10 tokens more. why and admit (on the shift
    # switch), the plan and agent-report sentences and the longer shift,
    # missing, skill and check lines took the notes from about 177, 295
    # and 336 to about 334, 465 and 576.
    "essentials": (340, 95),
    "standard": (470, 205),
    "deep": (580, 205),
}


def test_every_metric_says_what_it_captures_why_and_what_it_feeds():
    ids = [m.id for m in cat.METRICS]
    assert len(ids) == len(set(ids))
    for m in cat.METRICS:
        assert re.fullmatch(r"[a-z_]{2,24}", m.id), m.id
        assert m.group in cat.GROUPS, m.id
        assert m.section in cat.SECTIONS, m.id
        assert m.title and m.what.endswith(".") and m.why.endswith("."), m.id
        assert m.powers and set(m.powers) <= set(cat.THEMES), m.id
        assert set(m.requires) <= set(ids), m.id


def test_metrics_claude_writes_have_a_tag_and_a_note_and_the_rest_have_neither():
    for m in cat.METRICS:
        asks = bool(m.main_line or m.sub_line or m.main_extra or m.sub_extra or m.tool_note)
        if m.id in cat.AGENT_JUDGE_KEYS:
            # Judged by Haiku once the run is done: nothing asked of the agent,
            # nothing it writes.
            assert m.sub_line and not (m.tag or m.main_line or m.main_extra or m.sub_extra), m.id
            assert m.hooks == ("SubagentStop",) and m.out_chars == 0, m.id
        elif m.group in ("essentials", "standard", "deep"):
            assert asks and m.tag and m.hooks and m.out_chars > 0, m.id
        elif m.id in cat.FEEDBACK_ASKS:
            # Asked for in a note on a message of yours (``FEEDBACK_NOTE_TEXT``), not at the session start.
            assert not asks and cat.asks_claude(m.id), m.id
            assert m.hooks == ("UserPromptSubmit",) and m.out_chars > 0, m.id
        else:
            assert not asks, m.id
            assert m.out_chars == 0, m.id
        if m.id == "coaching_notes":
            # The one coaching toggle that runs through the capture hook; Stop only keeps the
            # newest reply's time for the cold-return receipt and prints nothing.
            assert m.hooks == ("UserPromptSubmit", "PostToolUse", "Stop")
        elif m.group in ("derived", "coaching") or m.id in ("feedback_note", "dashboard_rating"):
            assert not m.hooks, m.id


def _words_in(line: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z-]*", line))


def test_each_note_line_lists_only_words_the_parser_keeps_and_main_lines_list_them_all():
    for m in cat.METRICS:
        for scope, line in (("main", m.main_line), ("sub", m.sub_line)):
            if not line:
                continue
            # An agent line is what Haiku reads; its words are the agent vocabulary.
            vocab = cat.AGENT_JUDGE_VOCAB if scope == "sub" else cat.TAG_VOCAB
            # A main line may carry the keys riding on its metric (why and admit on shift), one per line.
            for key, words, or_none in re.findall(
                r"(?:^|; )([a-z]+): ([a-z_|,-]+)( or none)?", line, flags=re.MULTILINE
            ):
                assert key in vocab, (m.id, key)
                listed = set(re.split(r"[|,]", words.strip(","))) | ({"none"} if or_none else set())
                assert listed == set(vocab[key]), (m.id, key)
    # Every key Haiku answers for an agent metric has its line, and every
    # word it may take is one the parser keeps.
    for metric_id, keys in cat.AGENT_JUDGE_KEYS.items():
        for key in keys:
            assert f"{key}: " in cat.METRICS_BY_ID[metric_id].sub_line, (metric_id, key)
    assert set(cat.AGENT_JUDGE_VOCAB["missing"]) <= set(cat.TAG_VOCAB["missing"])
    assert "out=" + "|".join(cat.TAG_VOCAB["out"]) in cat.METRICS_BY_ID["big_output"].tool_note


def test_levels_nest_and_deep_is_every_level_metric():
    sets = [set(cat.level_metrics(level)) for level in cat.LEVELS]
    assert sets[0] == set()
    for smaller, larger in zip(sets, sets[1:]):
        assert smaller < larger
    assert sets[-1] == set(cat.LEVEL_METRIC_IDS)
    assert set(cat.level_metrics("free")) == {m.id for m in cat.METRICS if m.group == "free"}


@pytest.mark.parametrize("level", cat.LEVELS)
def test_a_preset_is_recognised_from_its_metrics(level):
    assert cat.level_of(cat.level_metrics(level)) == level


@pytest.mark.parametrize("level", cat.LEVELS)
def test_only_deep_brings_the_feedback_survey_and_its_reminders(level):
    extra = cat.DEEP_FEEDBACK_IDS if level == "deep" else ()
    assert cat.level_includes(level) == cat.level_metrics(level) + extra
    assert set(cat.DEEP_FEEDBACK_IDS) == set(cat.FEEDBACK_IDS) - {"dashboard_rating"}


def test_essentials_tags_what_a_change_is_judged_like_for_like_on():
    """task, level and size stratify the before-and-after comparison
    (impact.stratum), so Essentials carries all three and says why."""
    essentials = cat.level_metrics("essentials")
    for metric_id in ("task", "level", "size"):
        assert metric_id in essentials
        assert "measuring" in next(m for m in cat.METRICS if m.id == metric_id).powers
    assert "size" not in set(cat.level_metrics("standard")) - set(essentials)
    assert cat.THEMES["measuring"] == "Measuring your changes"
    assert "how big" in cat.LEVEL_SUMMARIES["essentials"]
    assert "size" not in cat.LEVEL_SUMMARIES["standard"]


def test_a_subset_is_custom_and_a_subagent_extra_brings_result():
    assert cat.level_of(["task", "size"]) == "custom"
    assert cat.with_requirements(["agent_brief", "task"]) == ("task", "result", "agent_brief")
    assert cat.with_requirements(["coaching_line", "nope"]) == ()
    assert cat.active_metrics("custom", ["agent_brief"], ["feedback_reminder"]) == (
        "result", "agent_brief", "feedback_reminder"
    )
    # A retired metric in an older config.toml is dropped, not asked for.
    assert cat.active_metrics("custom", ["rules", "fit", "found"]) == ()
    assert cat.active_metrics("custom", ["rules", "fit", "agent_brief"]) == ("result", "agent_brief")
    assert cat.active_metrics("essentials", ["size"]) == cat.level_metrics("essentials")


@pytest.mark.parametrize("level", ["essentials", "standard", "deep"])
def test_notes_stay_within_their_token_budget(level):
    ids = cat.level_metrics(level)
    main, sub = (len(cat.note_text(ids, scope)) / 4 for scope in ("main", "subagent"))
    assert main <= _BUDGETS[level][0] and sub <= _BUDGETS[level][1], (main, sub)


def test_notes_are_worded_as_facts_and_requests_not_orders():
    texts = [cat.note_text(cat.level_metrics("deep") + cat.FEEDBACK_IDS, "main")]
    texts += [cat.tool_note_text(i) for i in ("big_output",)]
    for text in texts:
        assert not re.search(r"\b(must|IMPORTANT|ALWAYS|NEVER|CRITICAL)\b", text), text
        assert "the user turned on" in text.lower() or text.startswith(cat.NOTE_MARKER)
        # The longest are the key lines that define their words (check, shift, missing).
        assert all(len(line) <= 320 for line in text.splitlines()), text


def test_the_note_marker_names_exactly_the_metrics_it_asks_for():
    ids = cat.level_metrics("standard")
    main = cat.note_text(ids, "main")
    version, codes = capture_tags.parse_note_codes(main)
    assert version == cat.NOTE_VERSION
    assert set(codes) == {m.id for m in cat.METRICS if m.id in ids and (m.main_line or m.main_extra)}
    # An agent is asked for nothing.
    assert cat.note_text(ids, "subagent") == ""


def test_free_signals_and_feedback_toggles_alone_add_no_subagent_note():
    assert cat.note_text(cat.level_metrics("free"), "main") == ""
    assert cat.note_text(cat.level_metrics("free"), "subagent") == ""
    assert cat.note_text(["feedback_reminder"], "subagent") == ""
    assert cat.note_text(["feedback_reminder"], "main") == ""
    reminder = cat.FEEDBACK_NOTE_TEXT["rating_reminder"]
    assert "/cg-feedback" in reminder and "[cg:" not in reminder


def test_no_agent_gets_a_note_at_any_level():
    for level in cat.LEVELS:
        for agent_type in ("general-purpose", "Explore", "Plan", "statusline-setup", ""):
            assert cat.note_text(cat.level_includes(level), "subagent", agent_type) == ""


def test_hook_entries_follow_the_metrics():
    signals = (
        ("capture-hook.py", "SessionEnd", "", False),
        ("capture-hook.py", "Notification", "", True),
        ("capture-hook.py", "PermissionRequest", "", True),
        ("capture-hook.py", "Stop", "", True),
        ("capture-hook.py", "StopFailure", "", True),
    )
    assert cat.hook_specs(cat.level_metrics("free")) == signals
    assert cat.hook_specs(["waits"]) == (signals[1],)
    assert cat.hook_specs(["turn_signals"]) == signals[3:]
    assert cat.hook_specs(cat.level_metrics("essentials")) == (
        ("capture-hook.py", "SessionStart", "startup|clear|compact", False),
        ("capture-hook.py", "SubagentStop", "", False),
    ) + signals
    assert cat.hook_specs(cat.level_metrics("deep"))[2] == (
        "capture-hook.py", "PostToolUse", "Read|Grep|Glob|WebFetch|WebSearch", False,
    )
    assert cat.hook_specs(["web"]) == ()
    assert cat.hook_specs(["result"]) == (("capture-hook.py", "SubagentStop", "", False),)


def test_the_post_tool_use_matcher_leaves_out_the_shell_and_mcp_tools():
    """A 30-day replay found the large-output note after a shell or MCP
    result wasn't worth the wait, and those two were about two thirds of
    the hook's spawns. Whatever metrics are on, the matcher names only
    tools whose results the hook can use."""
    assert cat.BIG_OUTPUT_TOOLS == ("Read", "Grep", "Glob", "WebFetch", "WebSearch")
    assert cat.COACHING_TOOLS == (*cat.BIG_OUTPUT_TOOLS, "ExitPlanMode")
    for ids in (cat.level_metrics("deep"), ["coaching_notes"], [*cat.level_metrics("deep"), "coaching_notes"]):
        matchers = [spec[2] for spec in cat.hook_specs(ids) if spec[1] == "PostToolUse"]
        assert len(matchers) == 1
        names = matchers[0].split("|")
        assert not {"Bash", "PowerShell"} & set(names) and not any("mcp__" in name or "*" in name for name in names)
    exported = cat.export_json()
    assert exported["result_tools"] == list(cat.COACHING_TOOLS)


def test_the_size_measure_constants_are_exported_for_the_hook():
    exported = cat.export_json()
    assert exported["result_image_max_tokens"] == cat.RESULT_IMAGE_MAX_TOKENS == 1_600
    assert exported["result_image_patch_px"] == cat.RESULT_IMAGE_PATCH_PX == 28
    assert exported["result_persist_chars"] == cat.RESULT_PERSIST_CHARS
    assert exported["result_preview_chars"] == cat.RESULT_PREVIEW_CHARS
    # An image alone can never reach the large-output threshold; a saved result
    # counts for its small preview, far under it.
    threshold = cat.BIG_OUTPUT_TOKENS * 4
    assert cat.RESULT_IMAGE_MAX_TOKENS * 4 < threshold and cat.RESULT_PREVIEW_CHARS < threshold


def test_the_hook_is_a_launcher_beside_the_module_it_runs():
    assert (cat.HOOK_SCRIPT, cat.HOOK_MODULE) == ("capture-hook.py", "capture_hook.py")
    hooks = resources.files("claudeglass") / "hooks"
    assert (hooks / cat.HOOK_MODULE).is_file() and (hooks / cat.HOOK_SCRIPT).is_file()
    launcher = (hooks / cat.HOOK_SCRIPT).read_text(encoding="utf-8")
    assert len(launcher.splitlines()) < 50 and "import capture_hook" in launcher


def test_only_signals_the_transcripts_lack_get_a_hook():
    """The transcripts already record instruction files, commands and
    skills, task lists and API errors, so those are always measured; only
    why sessions end, waits, permission prompts and how a turn ends need
    a hook (the last one -- ``turn_signals`` -- an independent, hook-level
    cross-check next to the transcript's own API-error record, not a
    replacement for it: ``stop_failure`` stays hookless, derived)."""
    free = {m.id: m for m in cat.METRICS if m.group == "free"}
    assert set(free) == set(cat.SIGNAL_EVENTS.values())
    for event, metric_id in cat.SIGNAL_EVENTS.items():
        assert event in free[metric_id].hooks and not cat.asks_claude(metric_id)
    assert cat.METRICS_BY_ID["turn_signals"].hooks == ("Stop", "StopFailure")
    for metric_id in ("instructions_loaded", "prompt_expansion", "tasks", "stop_failure"):
        assert cat.METRICS_BY_ID[metric_id].group == "derived" and not cat.METRICS_BY_ID[metric_id].hooks


def test_every_hook_file_ships_in_the_package():
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(pyproject["tool"]["setuptools"]["package-data"]["claudeglass"])
    hooks = resources.files("claudeglass") / "hooks"
    shipped = {f"hooks/{p.name}" for p in hooks.iterdir() if p.is_file() and not p.name.startswith("__")}
    assert shipped <= declared, f"add {sorted(shipped - declared)} to package-data in pyproject.toml"


def test_the_packaged_json_is_the_catalogue_export():
    packaged = resources.files("claudeglass") / "hooks" / cat.CATALOGUE_FILE
    assert packaged.read_text(encoding="utf-8") == cat.catalogue_json_text(), (
        "regenerate src/claudeglass/hooks/capture-catalogue.json from capture_catalogue.catalogue_json_text()"
    )
    data = json.loads(cat.catalogue_json_text())
    known = {m["id"] for m in data["metrics"]}
    for ids in data["levels"].values():
        assert set(ids) <= known


def test_the_export_carries_what_the_agent_judge_waits_for_and_ranks_by():
    agent = json.loads(cat.catalogue_json_text())["judge"]["agent"]
    assert agent["answer_tools"] == list(cat.AGENT_ANSWER_TOOLS)
    assert {"StructuredOutput", "SubagentHandback"} <= set(agent["answer_tools"])
    assert agent["wait"] == cat.AGENT_JUDGE_WAIT
    # Polls, then quiet, then the cap, which lies well inside the worker's own time.
    wait = agent["wait"]
    assert 0 < wait["poll_s"] < wait["quiet_s"] < wait["cap_s"] < cat.JUDGE_TIMEOUT_S / 2
    assert agent["model_tiers"] == list(cat.AGENT_MODEL_TIERS)
    # The hook ranks models as the parser does.
    from claudeglass import workstyle

    assert tuple(agent["model_tiers"]) == tuple(workstyle._TIER_FAMILIES)
    assert agent["limits"]["relay"] < agent["limits"]["brief"]
    assert list(agent["keys"]) == list(cat.AGENT_JUDGE_KEYS)


def test_an_agent_with_no_answer_is_a_failure_the_status_names():
    from claudeglass import cli

    assert "no_answer" in cat.JUDGE_ERRORS
    assert set(cli._HAIKU_ERRORS) == set(cat.JUDGE_ERRORS)


def test_pyproject_ships_the_json():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    assert f"hooks/{cat.CATALOGUE_FILE}" in pyproject.read_text(encoding="utf-8")


def test_the_capture_doc_is_the_catalogue_markdown():
    """``docs/capture.md`` is checked in, not built at doc time, the same
    way ``hooks/capture-catalogue.json`` is (see
    :func:`test_the_packaged_json_is_the_catalogue_export` above): kept
    in step with :func:`capture_catalogue.render_markdown` by this test
    rather than a build step."""
    doc = Path(__file__).resolve().parent.parent / "docs" / "capture.md"
    assert doc.read_text(encoding="utf-8") == cat.render_markdown(), (
        "regenerate docs/capture.md from capture_catalogue.render_markdown()"
    )
    text = doc.read_text(encoding="utf-8")
    for m in cat.METRICS:
        assert f"(`{m.id}`)" in text, m.id


@pytest.mark.parametrize("level", ["essentials", "standard", "deep"])
def test_the_levels_table_note_sizes_match_rough_tokens(level):
    """D17: the checked-in doc's "Note at session start"/"Note per
    subagent start" columns come from :func:`capture_catalogue.rough_tokens`
    -- the same function the Capture page and CAP-7's step-down saving
    estimate read -- not a separate chars/4 calculation that silently
    drops the hook-wrapper overhead ``rough_tokens`` includes (an earlier
    audit caught the two having drifted apart, 201/107 measured against
    182/88 then checked in for Essentials). This pins the doc's own
    numbers to ``rough_tokens`` directly, so a future edit to either one
    without regenerating the other fails here, not just in the
    whole-document sync test above."""
    doc = Path(__file__).resolve().parent.parent / "docs" / "capture.md"
    text = doc.read_text(encoding="utf-8")
    sizes = cat.rough_tokens(cat.level_includes(level))
    row = next(line for line in text.splitlines() if line.startswith(f"| {cat.LEVEL_TITLES[level]} |"))
    assert f"~{sizes['session_note']} tokens" in row, row
    # No agent gets a note: the last column is Haiku's call per agent run.
    assert sizes["subagent_note"] == 0
    assert row.rstrip().endswith(f"~${cat.JUDGE_USD_PER_CALL:.3f} |"), row


# -- round trip: what the note asks for is what the parser reads ----------


def test_a_tag_written_as_the_deep_note_asks_is_read_back_whole():
    tag, _ = capture_tags.parse_reply_tags(
        "Done.\n\n[cg: task=bugfix brief=clear level=hard shift=redo why=left_out admit=claim size=m "
        "missing=files,repro plan=made skill=none prior=none check=targeted out=part useful=no]"
    )
    assert (tag.task, tag.brief, tag.level, tag.shift, tag.size) == ("bugfix", "clear", "hard", "redo", "m")
    assert (tag.why, tag.admit) == ("left_out", "claim")
    assert tag.missing == ("files", "repro")
    assert (tag.plan, tag.skill, tag.prior, tag.check) == ("made", "none", "none", "targeted")
    assert (tag.out, tag.useful) == ("part", "no")


def test_a_report_tag_written_as_the_standard_note_asks_is_read_back_whole():
    tag, result = capture_tags.parse_reply_tags("Report.\n[result: partial brief=vague missing=goal,done]")
    assert result == "partial"
    assert (tag.brief, tag.missing) == ("vague", ("goal", "done"))


def test_older_transcripts_with_found_fit_and_rules_are_still_read():
    """found, fit and rules are no longer asked for, but a transcript written
    while they were is read as before: their words stay in the vocabulary."""
    tag, result = capture_tags.parse_reply_tags(
        "Report.\n[result: partial fit=larger rules=unused brief=vague missing=goal,done]"
    )
    assert result == "partial"
    assert (tag.fit, tag.rules, tag.brief, tag.missing) == ("larger", "unused", "vague", ("goal", "done"))
    tag, _ = capture_tags.parse_reply_tags("Found it.\n\n[cg: task=research found=partial detour=reread]")
    assert (tag.task, tag.found, tag.detour) == ("research", "partial", "reread")
    for retired in ("found", "fit", "rules"):
        assert retired in cat.RETIRED_METRIC_IDS and retired not in cat.METRICS_BY_ID
        assert retired in cat.TAG_VOCAB


def test_the_session_note_is_recognised_in_a_transcript(tmp_path):
    note = cat.note_text(cat.level_metrics("essentials"), "main")
    wrapped = f"<system-reminder>\nSessionStart hook additional context: {note}\n</system-reminder>"
    path = tmp_path / "s.jsonl"
    write_jsonl(path, [
        attachment_line("hook_additional_context", rendered=wrapped, content=[note], hookName="SessionStart",
                        hookEvent="SessionStart", toolUseID="SessionStart"),
        user_str_line("fix it", origin={"kind": "human"}),
        turn_line(content=[{"type": "text", "text": "Fixed.\n[cg: task=bugfix brief=clear level=easy]"}]),
    ])
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.meta.cap_metrics == ("task", "brief", "level", "shift", "size")
    assert result.turns[0].cap_note_chars == len(wrapped)
    assert result.turns[0].cap.task == "bugfix"


def test_the_session_note_offers_fix_for_a_fault_in_earlier_work_and_the_parser_keeps_it():
    assert cat.TAG_VOCAB["shift"] == ("new", "build", "grew", "redo", "fix")
    note = cat.note_text(cat.level_metrics("essentials"), "main")
    assert (
        "shift: new|build|grew|redo|fix, only if it applies (a new unrelated task; building on the last one; "
        "the scope grew; redoing earlier work; changing what you just delivered because it was wrong or not "
        "what they wanted, a rename or tweak included = fix)"
    ) in note.splitlines()
    tag, _ = capture_tags.parse_reply_tags("Fixed the earlier change.\n\n[cg: task=bugfix shift=fix]")
    assert (tag.task, tag.shift) == ("bugfix", "fix")


def test_why_and_admit_ride_on_the_shift_switch():
    shift = cat.METRICS_BY_ID["shift"]
    assert shift.extra_keys == ("why", "admit")
    assert cat.TAG_VOCAB["why"] == ("left_out", "missed", "changed", "tools")
    assert cat.TAG_VOCAB["admit"] == ("claim", "change", "instruction")
    # They are keys, not metrics: shift on asks for all three, shift off for none of them.
    assert "why" not in cat.METRICS_BY_ID and "admit" not in cat.METRICS_BY_ID
    assert cat.tag_keys(["task", "shift", "size"]) == ("task", "shift", "why", "admit", "size")
    assert cat.tagged_keys(["task", "shift", "size"]) == ("task", "shift", "size")
    assert cat.tag_keys(["task", "size"]) == ("task", "size")
    plain = cat.note_text(["task", "size"], "main")
    assert "why:" not in plain and "admit:" not in plain
    lines = cat.note_text(cat.level_metrics("essentials"), "main").splitlines()
    assert (
        "why: left_out|missed|changed|tools, only with shift redo or fix (their earlier request or the plan "
        "left it out; you missed something their request or the plan said; they changed their mind; a tool or "
        "setup failure)"
    ) in lines
    assert (
        "admit: claim|change|instruction, only if it applies (this reply admits an earlier mistake of yours: a "
        "wrong statement; a wrong change; an instruction you were given and didn't follow)"
    ) in lines
    # Each rides on shift's line in the note: after it and before the next metric's.
    keys = [line.split(":")[0] for line in lines if re.match(r"[a-z]+: ", line)]
    assert keys[keys.index("shift"):][:3] == ["shift", "why", "admit"]
    # The hook reads the same list from the packaged catalogue, and the marker names shift once.
    assert [m["extra_keys"] for m in cat.export_json()["metrics"] if m["id"] == "shift"] == [["why", "admit"]]
    _, codes = capture_tags.parse_note_codes(cat.note_text(cat.level_metrics("essentials"), "main"))
    assert codes.count("shift") == 1 and "why" not in codes and "admit" not in codes


def test_the_note_ends_with_the_plan_and_agent_report_rules_and_defines_files_and_scope():
    note = cat.note_text(cat.level_metrics("deep"), "main")
    closing = note.splitlines()[-1]
    assert closing == (
        "Leave out a key you can't judge. When carrying out a plan, judge the plan, not the go-ahead. "
        "Tag your reply to an agent's report for the request that started the agent."
    )
    missing = next(line for line in note.splitlines() if line.startswith("missing: "))
    assert "files = you had to search for which files" in missing
    assert "scope = what to change and what to leave alone wasn't said" in missing
    assert "always none on the first message" in next(line for line in note.splitlines() if line.startswith("prior: "))
    skill = next(line for line in note.splitlines() if line.startswith("skill: "))
    assert skill.index("if you ran a skill") < skill.index("would-help:<name>")


def test_haiku_gets_the_same_meanings_for_every_key_including_why_and_admit():
    ids = cat.level_metrics("deep")
    lines = cat.judge_text(ids).splitlines()
    for key in cat.tag_keys(ids):
        assert any(line.startswith(f"{key}: ") for line in lines), key
    why = next(line for line in lines if line.startswith("why: "))
    assert all(f"{word} = " in why for word in cat.TAG_VOCAB["why"]), why
    admit = next(line for line in lines if line.startswith("admit: "))
    assert all(f"{word} = " in admit for word in cat.TAG_VOCAB["admit"]), admit
    shift = next(line for line in lines if line.startswith("shift: "))
    assert "A short message changing files Claude changed in its previous reply is fix, not build or grew." in shift
    assert lines[-1].endswith("When Claude carried out a plan, judge the plan, not the user's go-ahead.")
    # Haiku may leave why and admit out where they don't fit, never the retired keys in.
    assert not any(line.startswith(("found: ", "fit: ")) for line in lines)


# -- the /cg-brief skill -----------------------------------------------------


def test_the_brief_skill_is_user_invoked_names_no_model_and_holds_every_checklist():
    text = cat.brief_skill_text()
    front, body = text.split("---\n", 2)[1:]
    assert "name: cg-brief" in front and "disable-model-invocation: true" in front
    assert "ClaudeGlass" in front and "model:" not in front
    for task, keys in cat.BRIEF_CHECKLISTS.items():
        assert f"   - {task}: " + ", ".join(cat.BRIEF_LINES[k][0] for k in keys) in body
    for _label, template in cat.BRIEF_LINES.values():
        assert f"   {template}" in body
    # Every checklist line is a word Claude can write as ``missing=``, or
    # the research report line.
    assert set(cat.BRIEF_LINES) - {"report"} == set(cat.TAG_VOCAB["missing"]) - {"none"}
    assert set(cat.BRIEF_CHECKLISTS) == set(cat.TAG_VOCAB["task"])
    # About one short turn: the checklist costs little when it runs.
    assert len(text) / 4 < 500


# -- the survey items that answer a message of yours ---------------------------------------


def _events(ids) -> list[str]:
    return [spec[1] for spec in cat.hook_specs(ids)]


@pytest.mark.parametrize("metric_id", cat.FEEDBACK_MESSAGE_IDS)
def test_each_survey_item_on_your_messages_asks_for_the_prompt_hook_alone(metric_id):
    assert cat.hook_specs((metric_id,)) == ((cat.HOOK_SCRIPT, "UserPromptSubmit", "", False),)
    # Beside coaching notes, which use the same entry, there is still one.
    assert _events((metric_id, "coaching_notes")).count("UserPromptSubmit") == 1
    assert _events((metric_id, "task", "feedback_reminder", "plan_check")).count("UserPromptSubmit") == 1


def test_the_survey_note_and_the_dashboard_rating_need_no_hook_on_your_messages():
    assert set(cat.FEEDBACK_MESSAGE_IDS) == {"feedback_skill", "plan_check", "feedback_reminder"}
    for metric_id in set(cat.FEEDBACK_IDS) - set(cat.FEEDBACK_MESSAGE_IDS):
        assert "UserPromptSubmit" not in _events((metric_id,)), metric_id
    assert _events(()) == []


def test_the_plan_check_and_the_reminder_ask_claude_and_the_facts_line_does_not():
    assert cat.FEEDBACK_ASKS == ("plan_check", "feedback_reminder")
    for metric_id in cat.FEEDBACK_ASKS:
        assert cat.asks_claude(metric_id)
        assert cat.METRICS_BY_ID[metric_id].group == "feedback" and cat.METRICS_BY_ID[metric_id].out_chars > 0
    # The facts line is written by the hook: it costs Claude no output.
    assert not cat.asks_claude("feedback_skill") and cat.METRICS_BY_ID["feedback_skill"].out_chars == 0


def test_every_feedback_note_has_its_text_and_its_metric():
    assert set(cat.FEEDBACK_NOTE_TEXT) == set(cat.FEEDBACK_HINTS) == set(cat.FEEDBACK_NOTE_METRIC)
    assert set(cat.FEEDBACK_NOTE_METRIC.values()) == set(cat.FEEDBACK_ASKS)
    for hint, text in cat.FEEDBACK_NOTE_TEXT.items():
        # Our own words only: no fill left but the reminder's token count, and no reply tag in it.
        assert not set(re.findall(r"\{(\w+)\}", text)) - {"tokens"} and "[cg:" not in text, hint


def test_the_plan_check_note_names_the_question_and_its_four_labels_word_for_word():
    text = cat.FEEDBACK_NOTE_TEXT["plan_check"]
    assert "AskUserQuestion" in text and f'header "{cat.PLAN_CHECK_HEADER}"' in text and cat.PLAN_CHECK_QUESTION in text
    assert cat.PLAN_CHECK_HEADER.startswith("CG ") and cat.PLAN_CHECK_WORDS == ("covered", "gap", "new", "none")
    for word, label, description in cat.PLAN_CHECK_OPTIONS:
        assert f'"{label}" ({description})' in text and "," not in label, word
    # It does not hold up the message, and it keeps the user's words out of what it saves.
    assert "exactly as you would have without this note" in text and "never copy" in text
    assert cat.PLAN_CHECK_ASK_CHARS == cat.METRICS_BY_ID["plan_check"].out_chars


def test_the_rating_reminder_note_ends_on_the_line_for_claude_to_copy():
    text = cat.FEEDBACK_NOTE_TEXT["rating_reminder"]
    assert text.splitlines()[-1] == f"{cat.REMINDER_LABEL} {cat.FEEDBACK_REMINDER_LINE}"
    assert cat.REMINDER_REPLY_CHARS == len(f"\n\n{cat.REMINDER_LABEL} {cat.FEEDBACK_REMINDER_LINE}")
    assert cat.METRICS_BY_ID["feedback_reminder"].out_chars == cat.REMINDER_REPLY_CHARS
    assert "/cg-feedback" in cat.FEEDBACK_REMINDER_LINE


def test_the_facts_line_keys_are_the_closed_list_the_skill_reads():
    assert cat.FEEDBACK_FACTS_MARKER == "cg-fb-facts v1"
    assert cat.FEEDBACK_FACT_KEYS == (
        "tokens", "typical", "followups", "queued", "plan", "plan_followups", "plan_asked", "build", "tips", "tip",
        "admits",
    )
    assert len(set(cat.FEEDBACK_FACT_KEYS)) == len(cat.FEEDBACK_FACT_KEYS)


def _note_tokens(hint: str) -> int:
    """A feedback note as it reaches the context: its text and the wrapper Claude Code puts round a hook note."""
    return round((len(cat.FEEDBACK_NOTE_TEXT[hint]) + cat.NOTE_WRAP_CHARS + len("UserPromptSubmit")) / 4)


def test_rough_tokens_size_the_reminder_the_plan_check_and_the_note_that_asks_for_either():
    none = cat.rough_tokens(())
    assert none["reminder"] == 0 and none["plan_check"] == 0 and none["message_note"] == 0
    reminder = cat.rough_tokens(("feedback_reminder",))
    assert reminder["reminder"] == round(cat.REMINDER_REPLY_CHARS / 4) and reminder["plan_check"] == 0
    assert reminder["message_note"] == _note_tokens("rating_reminder")
    check = cat.rough_tokens(("plan_check",))
    assert check["plan_check"] == round(cat.PLAN_CHECK_ASK_CHARS / 4) and check["reminder"] == 0
    assert check["message_note"] == _note_tokens("plan_check")
    both = cat.rough_tokens(("feedback_reminder", "plan_check"))
    assert both["message_note"] == max(reminder["message_note"], check["message_note"])
    # The facts line costs nothing to ask for, and neither piece adds to the tag Claude writes for each reply.
    assert cat.rough_tokens(("feedback_skill",))["message_note"] == 0
    tagged = cat.rough_tokens(("task",))["reply_tag"]
    assert tagged > 0 and cat.rough_tokens(("task", "feedback_reminder", "plan_check"))["reply_tag"] == tagged


def test_the_hook_is_given_the_survey_items_words_and_labels_it_matches_on():
    feedback = cat.export_json()["coaching"]["feedback"]
    assert feedback["facts_marker"] == cat.FEEDBACK_FACTS_MARKER
    assert feedback["fact_keys"] == list(cat.FEEDBACK_FACT_KEYS)
    assert feedback["message_ids"] == list(cat.FEEDBACK_MESSAGE_IDS)
    assert feedback["plan_check_header"] == cat.PLAN_CHECK_HEADER and feedback["plan_check_words"] == list(cat.PLAN_CHECK_WORDS)
    assert feedback["plan_check_labels"] == [label for _word, label, _description in cat.PLAN_CHECK_OPTIONS]
    assert feedback["plan_headers"][-1] == cat.PLAN_CHECK_HEADER and feedback["text"] == cat.FEEDBACK_NOTE_TEXT
    assert feedback["reminder_line"] == cat.FEEDBACK_REMINDER_LINE and feedback["hints"] == list(cat.FEEDBACK_HINTS)


def test_the_hook_is_given_the_rating_reminder_and_plan_check_thresholds():
    thresholds = cat.export_json()["coaching"]["thresholds"]
    assert thresholds["rating_min_tokens"] == 1_000_000 and thresholds["rating_typical_factor"] == 2
    assert thresholds["rating_rest_days"] == 3
    assert thresholds["plan_check_off_days"] == 14 and thresholds["plan_check_declines"] == 2
    assert cat.COACHING_THRESHOLDS["rating_min_tokens"] == thresholds["rating_min_tokens"]
