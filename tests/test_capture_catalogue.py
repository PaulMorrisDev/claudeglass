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
    # splits by it: about 10 tokens more.
    "essentials": (205, 95),
    "standard": (350, 205),
    "deep": (420, 205),
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
        elif m.id == "feedback_reminder":
            assert asks and m.hooks == ("SessionStart",)
        else:
            assert not asks, m.id
            assert m.out_chars == 0, m.id
        if m.id == "coaching_notes":
            # The one coaching toggle that runs through the capture hook.
            assert m.hooks == ("UserPromptSubmit", "PostToolUse")
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
            for key, words, or_none in re.findall(r"(?:^|; )([a-z]+): ([a-z|,-]+)( or none)?", line):
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
    assert cat.with_requirements(["fit", "task"]) == ("task", "result", "fit")
    assert cat.with_requirements(["coaching_line", "nope"]) == ()
    assert cat.active_metrics("custom", ["agent_brief"], ["feedback_reminder"]) == (
        "result", "agent_brief", "feedback_reminder"
    )
    # A retired metric in an older config.toml is dropped, not asked for.
    assert cat.active_metrics("custom", ["rules", "fit"]) == ("result", "fit")
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
        assert all(len(line) <= 160 for line in text.splitlines()), text


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
    reminder = cat.note_text(["feedback_reminder"], "main")
    assert "/cl-feedback" in reminder and "[tl:" not in reminder


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
        "capture-hook.py", "PostToolUse", "Bash|Read|Grep|Glob|WebFetch|WebSearch|mcp__.*", False,
    )
    assert cat.hook_specs(["web"]) == ()
    assert cat.hook_specs(["result"]) == (("capture-hook.py", "SubagentStop", "", False),)


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
        "Done.\n\n[tl: task=bugfix brief=clear level=hard shift=new size=m missing=files,repro plan=made "
        "skill=none found=yes prior=none detour=reread check=targeted out=part useful=no]"
    )
    assert (tag.task, tag.brief, tag.level, tag.shift, tag.size) == ("bugfix", "clear", "hard", "new", "m")
    assert tag.missing == ("files", "repro")
    assert (tag.plan, tag.skill, tag.found, tag.prior, tag.detour, tag.check) == (
        "made", "none", "yes", "none", "reread", "targeted",
    )
    assert (tag.out, tag.useful) == ("part", "no")


def test_a_report_tag_written_as_the_standard_note_asks_is_read_back_whole():
    tag, result = capture_tags.parse_reply_tags(
        "Report.\n[result: partial fit=larger rules=unused brief=vague missing=goal,done]"
    )
    assert result == "partial"
    assert (tag.fit, tag.rules, tag.brief, tag.missing) == ("larger", "unused", "vague", ("goal", "done"))


def test_the_session_note_is_recognised_in_a_transcript(tmp_path):
    note = cat.note_text(cat.level_metrics("essentials"), "main")
    wrapped = f"<system-reminder>\nSessionStart hook additional context: {note}\n</system-reminder>"
    path = tmp_path / "s.jsonl"
    write_jsonl(path, [
        attachment_line("hook_additional_context", rendered=wrapped, content=[note], hookName="SessionStart",
                        hookEvent="SessionStart", toolUseID="SessionStart"),
        user_str_line("fix it", origin={"kind": "human"}),
        turn_line(content=[{"type": "text", "text": "Fixed.\n[tl: task=bugfix brief=clear level=easy]"}]),
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
        "the scope grew; redoing earlier work; fixing a fault in it)"
    ) in note.splitlines()
    tag, _ = capture_tags.parse_reply_tags("Fixed the earlier change.\n\n[tl: task=bugfix shift=fix]")
    assert (tag.task, tag.shift) == ("bugfix", "fix")


# -- the /cl-brief skill -----------------------------------------------------


def test_the_brief_skill_is_user_invoked_names_no_model_and_holds_every_checklist():
    text = cat.brief_skill_text()
    front, body = text.split("---\n", 2)[1:]
    assert "name: cl-brief" in front and "disable-model-invocation: true" in front
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
