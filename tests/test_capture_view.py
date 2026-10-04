"""``capture_view``: what the Capture tab and the banner show, from
``[capture]``, the catalogue, a replayed history and measured usage."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from claudeglass import capture, capture_catalogue as catalogue, capture_view, habits
from claudeglass.config import CaptureConfig
from claudeglass.hook_health import CaptureHookHealth, HookSpec
from claudeglass.units import Units

from helpers import assert_privacy, elasticity_with_slope

API = Units(billing_mode="api")


def _past() -> capture.History:
    return capture.History(
        days=14, sessions=20, cycles=200, subagents=40, main_notes=24, sub_notes=40,
        main_note=2e-6, sub_note=1e-6, sub_note_no_rules=0.0, reply_tag=4e-6, report_tag=4e-6,
        brief_tag=4e-6, spend=50.0,
    )


def _rows(data) -> dict:
    return {row["id"]: row for section in data["sections"] for row in section["metrics"]}


def test_off_invites_with_the_essentials_estimate():
    data = capture_view.view(CaptureConfig(), past=_past(), units=API)
    assert_privacy(data)
    banner = data["banner"]
    assert banner["on"] is False
    assert banner["headline"].startswith("Metrics capture is off. At Essentials it would have cost about ")
    assert "USD a week" in banner["headline"] and "of what you spent" in banner["headline"]
    levels = {level["id"]: level for level in data["levels"]}
    assert levels["free"]["estimate"] is None  # no Claude tokens
    assert levels["essentials"]["estimate"]["usd"] < levels["deep"]["estimate"]["usd"]
    assert "Feedback reminder from Claude" in levels["deep"]["adds"]
    assert "Feedback reminder from Claude" not in levels["standard"]["adds"]
    assert levels["deep"]["metrics"][-4:] == list(catalogue.DEEP_FEEDBACK_IDS)
    assert levels["off"]["current"] is True
    rows = _rows(data)
    assert rows["task"]["on"] is False and rows["task"]["estimate"]["usd"] > 0
    assert rows["session_end"]["estimate"] is None and not rows["session_end"]["asks_claude"]
    assert rows["prompt_features"]["on"] is True and rows["prompt_features"]["toggle"] is False


def test_off_without_history_still_invites():
    data = capture_view.view(CaptureConfig())
    assert data["banner"]["headline"] == (
        "Metrics capture is off. Turn it on to get suggestions that fit how you work."
    )
    assert data["history"] is None and all(level["estimate"] is None for level in data["levels"])


def _on(**kw) -> CaptureConfig:
    return CaptureConfig(level="essentials", enabled_at="2026-09-20T10:00:00+00:00", **kw)


def _use(*, sessions: int = 4, **kw) -> capture.CaptureUsage:
    use = capture.CaptureUsage(since="2026-09-20T10:00:00+00:00", sessions=sessions, subagents=6, spend=10.0, **kw)
    use._add("main", note_chars=4000, note_cost=0.02, tag_chars=400, tag_cost=0.01)
    use.by_metric = {"task": 0.012, "result": 0.004}
    return use


def test_on_headline_shows_level_tokens_amount_and_coverage():
    data = capture_view.view(_on(), past=_past(), units=API, use=_use(cycles=10, tagged_cycles=9))
    headline = data["banner"]["headline"]
    assert headline.startswith("Metrics capture: Essentials · since 2026-09-20 · 1,100 tokens · 0.03 USD")
    assert "(0.3% of spend)" in headline and "tagged on 90.0% of messages" in headline
    rows = _rows(data)
    assert rows["task"]["actual"]["usd"] == 0.012
    assert rows["task"]["answers"] == 0 and rows["task"]["target"] == capture.enough_target("task")
    assert data["measured"]["scopes"]["main"]["note_tokens"] == 1000


def test_measured_splits_the_tags_haiku_filled_in_from_claudes_own():
    data = capture_view.view(_on(), past=_past(), units=API, use=_use(cycles=8, tagged_cycles=6, filled_cycles=2))
    measured = data["measured"]
    assert measured["coverage_text"] == "75.0%"
    assert (measured["own_coverage_text"], measured["filled_text"]) == ("50.0%", "25.0%")
    # Nothing filled in, or nothing measured: both stay empty.
    for use in (_use(cycles=8, tagged_cycles=6), _use(cycles=0, tagged_cycles=0, filled_cycles=0)):
        measured = capture_view.view(_on(), past=_past(), units=API, use=use)["measured"]
        assert (measured["own_coverage_text"], measured["filled_text"]) == ("", "")


def test_on_notes_low_coverage_enough_data_and_expiry():
    use = _use(cycles=30, tagged_cycles=6)
    use.answers = {m: 1000 for m in catalogue.level_metrics("essentials")}
    capture_config = _on(until="2026-09-21T00:00:00+00:00")
    data = capture_view.view(capture_config, units=API, use=use, now=datetime(2026, 9, 22, tzinfo=timezone.utc))
    notes = data["banner"]["notes"]
    assert data["config"]["expired"] is True and data["config"]["effective"] is False
    assert any(note.startswith("Its end time (2026-09-21 00:00) has passed") for note in notes)
    assert any("tagged only 20.0% of your messages" in note for note in notes)
    assert any(note.startswith("Enough collected for every metric on") for note in notes)


# -- CAP-7: a specific step-down command once its evidence is ready ---------


def test_step_down_note_names_the_specific_command_once_its_dropped_metrics_are_ready():
    dropped = [
        i for i in catalogue.level_metrics("standard")
        if i not in catalogue.level_metrics("essentials")
    ]
    assert dropped  # sanity: standard really does add something over essentials
    use = _use(cycles=30, tagged_cycles=30)
    use.answers = {i: capture.enough_target(i) for i in dropped}
    capture_config = CaptureConfig(level="standard", enabled_at="2026-09-20T10:00:00+00:00")
    data = capture_view.view(capture_config, units=API, use=use)
    notes = data["banner"]["notes"]
    step = [n for n in notes if n.startswith("Every metric Standard adds over Essentials has enough collected (")]
    assert len(step) == 1
    # What changes, where, the trade-off and the undo -- a command, never an apply.
    # The metrics it would drop, by their Capture page names, never their ids.
    shown = {r["id"]: r for s in data["sections"] for r in s["metrics"]}
    named = [m.id for m in catalogue.METRICS if m.id in dropped and shown[m.id]["asks_claude"]]
    assert habits.metric_list(named) in step[0]
    assert not any(f"{i}," in step[0] or f"{i})" in step[0] for i in dropped if "_" in i)
    assert "stops collecting them" in step[0]
    assert "[capture] level in ClaudeGlass's config.toml" in step[0] and "settings.json" in step[0]
    assert (
        "'claudeglass capture level essentials --dry-run' shows what stepping down would change and writes "
        "nothing; 'claudeglass capture level standard' undoes it."
    ) in step[0]
    # The specific command replaces the generic "lower the level" note, not both at once.
    assert not any(note.startswith("Enough collected for every metric on") for note in notes)


def test_step_down_note_is_none_below_essentials_or_when_not_every_dropped_metric_is_ready():
    dropped = [
        i for i in catalogue.level_metrics("standard")
        if i not in catalogue.level_metrics("essentials")
    ]
    use = _use(cycles=30, tagged_cycles=30)
    use.answers = {i: capture.enough_target(i) for i in dropped[:-1]}  # the last one is short
    data = capture_view.view(
        CaptureConfig(level="standard", enabled_at="2026-09-20T10:00:00+00:00"), units=API, use=use
    )
    assert not any("shows what stepping down would change" in n for n in data["banner"]["notes"])
    # essentials has no lower step in the ladder at all (free asks Claude nothing).
    essentials_use = _use(cycles=30, tagged_cycles=30)
    essentials_use.answers = {m: 1000 for m in catalogue.level_metrics("essentials")}
    essentials_data = capture_view.view(_on(), units=API, use=essentials_use)
    assert not any("shows what stepping down would change" in n for n in essentials_data["banner"]["notes"])
    assert any(
        note.startswith("Enough collected for every metric on") for note in essentials_data["banner"]["notes"]
    )


# -- CAP-5 (gap 4): what each metric is worth ------------------------------


def test_worth_table_prices_each_asked_metric_a_week_against_what_it_feeds():
    now = datetime(2026, 10, 4, 10, tzinfo=timezone.utc)  # 2 weeks after enabled_at
    data = capture_view.view(_on(), units=API, use=_use(sessions=habits.MIN_GROUP), now=now)
    worth = {row["id"]: row for row in data["worth"]}
    assert worth["task"]["usd"] == 0.012 / 2
    assert worth["task"]["feeds"] == [catalogue.THEMES.get(p, p) for p in catalogue.METRICS_BY_ID["task"].powers]
    assert worth["result"]["usd"] == 0.004 / 2
    # Sorted priciest first.
    assert [row["id"] for row in data["worth"]] == ["task", "result"]
    # A metric that's on but never measured a dollar (or off, or derived,
    # like "found") doesn't show up.
    assert "session_end" not in worth


def test_worth_table_is_empty_without_a_start_time_or_before_capture_is_on():
    assert capture_view.view(CaptureConfig(), units=API)["worth"] == []
    assert capture_view.view(_on(), units=API)["worth"] == []  # no `use` passed


def test_worth_table_is_empty_below_min_group_sessions_with_notes():
    """SURV-8: a metric-worth table built from a handful of sessions is
    noise, not a trend -- the same ``habits.MIN_GROUP`` gate other
    small-sample tables use. ``_use()``'s default ``sessions=4`` is one
    short of ``habits.MIN_GROUP`` (5)."""
    now = datetime(2026, 10, 4, 10, tzinfo=timezone.utc)
    data = capture_view.view(_on(), units=API, use=_use(), now=now)
    assert data["worth"] == []
    assert data["worth_min_sessions"] == habits.MIN_GROUP


# -- capture ROI: what it costs against what depends on it -----------------


def test_roi_is_none_without_a_weekly_cost_to_price():
    data = capture_view.view(_on(), units=API, use=_use())
    assert data["roi"] is None
    assert not any("Capture cost about" in note for note in data["banner"]["notes"])


def test_roi_prices_capture_against_what_depends_on_it():
    data = capture_view.view(_on(), units=API, use=_use(), weekly_cost=2.0, dependent_value=5.0)
    roi = data["roi"]
    assert roi["cost"]["usd"] == 2.0 and roi["value"]["usd"] == 5.0 and roi["measured"] is True
    assert (
        "Capture cost about 2.00 USD a week; suggestions that rely on it are worth about 5.00 USD a week."
        in data["banner"]["notes"]
    )


def test_roi_says_so_instead_of_a_zero_when_nothing_measured_depends_on_capture():
    data = capture_view.view(_on(), units=API, use=_use(), weekly_cost=2.0, dependent_value=None)
    roi = data["roi"]
    assert roi["cost"]["usd"] == 2.0 and roi["value"] is None and roi["measured"] is False
    assert "Capture cost about 2.00 USD a week; nothing measured yet relies on it." in data["banner"]["notes"]


def test_roi_adds_no_banner_note_when_nothing_was_spent():
    data = capture_view.view(_on(), units=API, use=_use(), weekly_cost=0.0, dependent_value=None)
    assert data["roi"]["cost"]["usd"] == 0.0
    assert not any("Capture cost about" in note for note in data["banner"]["notes"])


def test_roi_banner_has_no_bare_dollar_or_doubled_about_or_doubled_weekly_under_a_subscription():
    """UX-2 / finding F3: a subscription's ROI banner note must route
    through Units, never a bare "$", never double "about" (the "about"
    manually prepended in ``_banner`` used to collide with a subscription
    share's own "about X% of your weekly usage limit"), and never say
    "...weekly usage limit a week" (the roi cost/value used to keep the
    "a week" period suffix even once the primary text already read as a
    share of the *weekly* usage limit)."""
    subscription = Units(billing_mode="subscription", elasticity=elasticity_with_slope())
    data = capture_view.view(_on(), units=subscription, use=_use(), weekly_cost=2.0, dependent_value=5.0)
    note = next(n for n in data["banner"]["notes"] if n.startswith("Capture cost"))
    assert "$" not in note
    assert "about about" not in note.lower()
    assert "usage limit a week" not in note.lower()


def test_roi_in_list_price_dollars_under_a_subscription_is_still_a_week():
    """A subscription with no usage-limit readings (the status line never
    ran) phrases amounts in list-price dollars: without "a week" the ROI
    read as a total beside the banner's own total since capture began."""
    subscription = Units(billing_mode="subscription")
    data = capture_view.view(_on(), units=subscription, use=_use(), weekly_cost=2.0, dependent_value=5.0)
    assert (
        "Capture cost about 2.00 USD list-price equivalent a week; suggestions that rely on it are worth "
        "about 5.00 USD list-price equivalent a week." in data["banner"]["notes"]
    )


def test_on_with_no_notes_seen_says_the_hook_may_be_blocked():
    use = capture.CaptureUsage(since="2026-09-20T10:00:00+00:00")
    data = capture_view.view(_on(), units=API, use=use, started_since=5)
    assert "no captured sessions yet" in data["banner"]["headline"]
    assert any("No capture note seen in the 5 sessions" in note for note in data["banner"]["notes"])
    # Half the sessions sampled out: 5 started is not enough to say so.
    data = capture_view.view(_on(sample=50), units=API, use=use, started_since=5)
    assert not any("No capture note" in note for note in data["banner"]["notes"])


def test_hook_problems_are_counted_never_quoted():
    spec = HookSpec(catalogue.HOOK_SCRIPT, "SessionStart", catalogue.SESSION_START_MATCHER)
    health = CaptureHookHealth(
        settings_path=Path("C:/Users/someone-private/.claude/settings.json"),
        needed=(spec,),
        problems=["The command runs C:/Users/someone-private/hook.py, which does not exist."],
    )
    block = capture_view.hooks_block(health)
    assert block["ok"] is False and block["problems"] == 1
    assert "someone-private" not in str(block)
    assert "1 capture hook entry in settings.json can't run" in block["summary"]
    data = capture_view.view(_on(), units=API, hooks=health)
    assert block["summary"] in data["banner"]["notes"]


def test_missing_hook_marks_the_metrics_that_need_it():
    spec = HookSpec(catalogue.HOOK_SCRIPT, "SubagentStop")
    health = CaptureHookHealth(settings_path=Path("settings.json"), needed=(spec,), missing=(spec,))
    rows = _rows(capture_view.view(_on(), units=API, hooks=health))
    assert rows["result"]["needs_hook"] is True
    assert rows["task"]["needs_hook"] is False
    assert rows["size"]["needs_hook"] is False  # off
    # The chip comes with the exact command that adds the entry; a row that needs nothing names none.
    assert rows["result"]["hook_command"] == capture_view.CONNECT_COMMAND == "claudeglass capture connect"
    assert rows["task"]["hook_command"] is None and rows["size"]["hook_command"] is None


def test_the_background_stop_entry_missing_marks_coaching_notes_and_names_the_command():
    # An earlier version's connection has no Stop entry: the coaching metric shows the chip and the command.
    stop = HookSpec(catalogue.HOOK_SCRIPT, "Stop", "", True)
    health = CaptureHookHealth(settings_path=Path("settings.json"), needed=(stop,), missing=(stop,))
    config = CaptureConfig(coaching=["coaching_notes"])
    rows = _rows(capture_view.view(config, units=API, hooks=health))
    assert rows["coaching_notes"]["needs_hook"] is True
    assert rows["coaching_notes"]["hook_command"] == "claudeglass capture connect"
    assert "Run 'claudeglass capture connect' to fix it." in capture_view.hooks_block(health)["summary"]


def test_describe_and_config_block():
    assert capture_view.describe(CaptureConfig()) == "Off"
    config = _on(sample=25, until="2026-10-01T12:30:00+00:00")
    assert capture_view.describe(config) == "Essentials (since 2026-09-20, until 2026-10-01 12:30, 25% of sessions)"
    block = capture_view.config_block(CaptureConfig(level="free", projects=["secret-client"]))
    assert block["projects_limited"] is True and "secret-client" not in str(block)
    assert block["metrics"] == list(catalogue.level_metrics("free"))


def test_change_commands():
    before = _on()
    assert capture_view.change_commands(before, {"level": "off"}) == ["claudeglass capture off"]
    assert capture_view.change_commands(before, {"level": "deep"}) == ["claudeglass capture level deep"]
    assert capture_view.change_commands(before, {"metrics": ["task", "agent_brief"]}) == [
        "claudeglass capture enable agent_brief",
        "claudeglass capture disable brief level shift size retry session_end waits permissions turn_signals",
    ]
    assert capture_view.change_commands(before, {"feedback": ["feedback_note"], "sample": 50}) == [
        "claudeglass capture enable feedback_note",
        "claudeglass capture on --sample 50",
    ]


def test_amount_text_follows_the_billing_mode():
    assert capture_view.amount_text(API, 0) == "nothing"
    assert capture_view.amount_text(API, 0.001, "a week") == "under 0.01 USD a week"
    subscription = Units(billing_mode="subscription")
    assert capture_view.amount_text(subscription, 1.5) == "1.50 USD list-price equivalent"


def test_units_basis():
    assert API.basis() == "Amounts are what the tokens cost at list price."
    assert Units(billing_mode="subscription").basis().startswith(
        "Amounts are list-price equivalents, not what you are charged."
    )


def test_several_missing_hooks_make_one_sentence_and_a_list():
    specs = (
        HookSpec(catalogue.HOOK_SCRIPT, "SessionStart", catalogue.SESSION_START_MATCHER),
        HookSpec(catalogue.HOOK_SCRIPT, "SubagentStart"),
    )
    health = CaptureHookHealth(settings_path=Path("settings.json"), needed=specs, missing=specs)
    block = capture_view.hooks_block(health)
    assert block["summary"] == (
        "settings.json lacks 2 of the hook entries your metrics need, so they aren't captured. "
        "Run 'claudeglass capture connect' to fix it."
    )
    assert len(block["missing"]) == 2


def test_share_text_never_reads_as_zero():
    assert capture_view.share_text(None) == ""
    assert capture_view.share_text(0.0) == "0.0%"
    assert capture_view.share_text(0.01) == "under 0.1%"
    assert capture_view.share_text(0.3) == "0.3%"


# -- feedback --------------------------------------------------------------


def test_a_missing_feedback_skill_is_a_row_note_and_a_banner_note_even_with_capture_off():
    data = capture_view.view(CaptureConfig(feedback=["feedback_skill"]), skill="missing")
    row = _rows(data)["feedback_skill"]
    assert row["needs_install"] is True
    assert row["install_note"] == capture_view.SKILL_STATES["missing"]
    assert row["install_command"] == capture_view.FEEDBACK_COMMAND
    assert data["banner"]["on"] is False and data["banner"]["notes"] == [capture_view.SKILL_NOTES["missing"]]
    installed = capture_view.view(CaptureConfig(feedback=["feedback_skill"]), skill="installed")
    assert _rows(installed)["feedback_skill"]["needs_install"] is False and installed["banner"]["notes"] == []
    # Off, nothing is asked of the skill file at all.
    assert _rows(capture_view.view(CaptureConfig(), skill="missing"))["feedback_skill"]["needs_install"] is False


def test_a_missing_brief_skill_is_a_row_note_and_a_banner_note():
    config = CaptureConfig(coaching=["brief_templates"])
    data = capture_view.view(config, brief_skill="missing")
    row = _rows(data)["brief_templates"]
    assert row["needs_install"] is True
    assert row["install_note"] == capture_view.BRIEF_SKILL_STATES["missing"] == "The /cg-brief skill isn't installed"
    assert row["install_command"] == capture_view.BRIEF_COMMAND == "claudeglass capture brief on"
    assert data["banner"]["notes"] == [capture_view.BRIEF_SKILL_NOTES["missing"]]
    assert data["commands"]["brief"] == capture_view.BRIEF_COMMAND
    installed = capture_view.view(config, brief_skill="installed")
    assert _rows(installed)["brief_templates"]["needs_install"] is False and installed["banner"]["notes"] == []
    # The feedback skill's state never marks the brief row, and the other way round.
    both = capture_view.view(CaptureConfig(feedback=["feedback_skill"]), skill="installed", brief_skill="missing")
    assert _rows(both)["brief_templates"]["needs_install"] is False
    assert _rows(both)["feedback_skill"]["needs_install"] is False

def test_feedback_runs_are_priced_over_the_last_days_and_counted_toward_enough():
    use = capture.CaptureUsage(since="", feedback_runs=3, feedback_cost=0.05, feedback_answered=2)
    data = capture_view.view(
        CaptureConfig(feedback=["feedback_skill", "dashboard_rating"]), units=API, feedback_use=use,
        skill="installed", ratings=4,
    )
    rows = _rows(data)
    assert rows["feedback_skill"]["actual_label"] == f"Over the last {capture.HISTORY_DAYS} days"
    assert rows["feedback_skill"]["actual"]["usd"] == 0.05
    assert (rows["feedback_skill"]["answers"], rows["feedback_skill"]["target"]) == (2, capture.ENOUGH["feedback"])
    assert rows["dashboard_rating"]["answers"] == 4 and rows["dashboard_rating"]["actual"] is None
    assert data["feedback"]["runs"] == 3 and data["feedback"]["ratings"] == 4
    assert rows["task"]["actual_label"] == "Since it was turned on"


def _unrated(total: int = 2, pieces: int = 2) -> dict:
    return {
        "threshold": 1_000_000,
        "threshold_text": "1M",
        "total": total,
        "pieces": [
            {"session_id": f"s{n}", "slug": "shop", "last_ts": f"2026-09-2{n}T10:00:00Z", "tokens": 1_200_000 + n,
             "tokens_text": "1.2M"}
            for n in range(pieces)
        ],
    }


def test_the_banner_lists_the_sessions_waiting_for_a_rating_with_a_sentence_that_introduces_them():
    unrated = _unrated()
    for config in (CaptureConfig(feedback=["feedback_reminder"]), CaptureConfig(feedback=["dashboard_rating"])):
        banner = capture_view.view(config, unrated=unrated)["banner"]
        block = banner["unrated"]
        assert block["text"] == (
            "2 sessions used at least 1M tokens and have no rating yet. "
            "Rating them makes your savings tips fit how you work."
        )
        assert [p["session_id"] for p in block["pieces"]] == ["s0", "s1"]
        assert (block["threshold"], block["threshold_text"], block["total"]) == (1_000_000, "1M", 2)
    # The figures it was given stay as they were, with the sentence added.
    assert "text" not in unrated


def test_the_banner_counts_more_sessions_than_it_lists_and_says_one_in_the_singular():
    block = capture_view.view(CaptureConfig(), unrated=_unrated(total=7, pieces=5))["banner"]["unrated"]
    assert block["total"] == 7 and len(block["pieces"]) == 5
    assert block["text"].startswith("7 sessions used at least 1M tokens and have no rating yet.")
    one = capture_view.view(CaptureConfig(), unrated=_unrated(total=1, pieces=1))["banner"]["unrated"]
    assert one["text"].startswith("1 session used at least 1M tokens and has no rating yet.")


def test_the_banner_has_no_list_without_sessions_to_rate():
    for unrated in (None, {}, _unrated(total=0, pieces=0)):
        assert capture_view.view(CaptureConfig(), unrated=unrated)["banner"]["unrated"] is None
    assert capture_view.view(CaptureConfig())["banner"]["unrated"] is None


def test_the_banner_list_is_there_whether_capture_is_on_or_off_and_holds_no_money():
    on = capture_view.view(CaptureConfig(level="essentials"), units=API, unrated=_unrated())["banner"]
    off = capture_view.view(CaptureConfig(), units=API, unrated=_unrated())["banner"]
    assert on["on"] is True and off["on"] is False
    assert on["unrated"] == off["unrated"] and on["unrated"] is not None
    assert "$" not in str(on["unrated"]) and "USD" not in str(on["unrated"])
    assert_privacy(on)


def test_status_line_toggles_say_when_the_status_line_is_someone_elses():
    config = CaptureConfig(feedback=["feedback_note"], coaching=["coaching_line"])
    rows = _rows(capture_view.view(config, statusline=False))
    assert rows["feedback_note"]["statusline_note"] == capture_view.STATUSLINE_NOTES["feedback_note"]
    assert rows["coaching_line"]["statusline_note"] == capture_view.STATUSLINE_NOTES["coaching_line"]
    for statusline in (True, None):
        rows = _rows(capture_view.view(config, statusline=statusline))
        assert rows["feedback_note"]["statusline_note"] is None and rows["coaching_line"]["statusline_note"] is None


def test_coaching_notes_say_what_a_note_costs_not_no_tokens():
    """Coaching notes ask Claude for nothing, yet each note is read: the
    Capture page said "No tokens." beside what the notes had cost."""
    rows = _rows(capture_view.view(CaptureConfig(coaching=["coaching_notes", "coaching_line"])))
    assert rows["coaching_notes"]["asks_claude"] is False
    assert rows["coaching_notes"]["cost_note"] == "About 50 to 140 tokens a note, only when a hint applies."
    assert rows["coaching_line"]["cost_note"] is None
