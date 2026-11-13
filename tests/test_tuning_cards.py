"""The ``cards`` block of the tuning export (``tuning.py``): how the cards the
dashboard showed were used. It counts the dashboard's ``card_marks`` rows by
rule and by the class of agent, and reads which are ignored from the ignores
file and which were rated from the tip-card answers. Only counts and words
from closed sets reach the document.

These cases sit beside ``test_tuning.py`` and use its sample document, its
world on disk and its copy rules.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from claudeglass import ignores, tuning
from claudeglass.model import Recommendation

from helpers import assert_privacy_deep, recommendation_rule_ids
from test_tuning import _build, _plain, _put, _salt, _sample, world  # noqa: F401  (fixtures)

#: In the world's window (30 days to 2026-09-19).
SHOWN = datetime(2026, 9, 10, 9, 0, 0, tzinfo=timezone.utc)


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(
    key,
    rule="model-tier",
    agent_type=None,
    *,
    project="*",
    folders=(),
    changes=(),
    shown=SHOWN,
    copied=False,
    bundled=False,
    landed_days=None,
    exact=False,
):
    """One ``Store.card_marks`` row."""
    landed = _stamp(shown + timedelta(days=landed_days, hours=3)) if landed_days is not None else None
    return {
        "card_key": key,
        "project": project,
        "folders": list(folders),
        "rule_id": rule,
        "agent_type": agent_type,
        "changes": list(changes),
        "saving_usd": 1.0,
        "first_shown_at": _stamp(shown),
        "copied_at": _stamp(shown + timedelta(minutes=5)) if copied else None,
        "bundled_at": _stamp(shown + timedelta(minutes=9)) if bundled else None,
        "landed_at": landed,
        "landed_exact": (1 if exact else 0) if landed else None,
        "updated_at": _stamp(shown + timedelta(minutes=9)),
    }


def _change(value="sonnet"):
    return {"target": "settings", "key": "model", "agent": None, "value": value}


def _marks(rows, folders=(), every_project=True):
    return tuning.CardMarks(list(rows), frozenset(folders), every_project)


def _by_pair(doc):
    return {(r["rule"], r["agent"]): r for r in doc["cards"]["rows"]}


# -- the closed sets -----------------------------------------------------------------------


def test_the_card_rules_are_every_rule_the_service_can_send():
    assert tuning.CARD_RULES == (*sorted(recommendation_rule_ids()), "other")
    assert tuning.CARD_AGENTS == ("none", "main", *tuning.AGENT_TYPES)
    assert tuning.CARD_ROWS_KEPT == 40


# -- the spec and the older files ----------------------------------------------------------


def test_a_file_without_a_cards_block_still_passes_and_says_nothing_of_it():
    doc = _sample()
    assert "cards" in doc
    del doc["cards"]
    assert tuning.validate(doc) == []
    assert not any(line.startswith("Dashboard cards") for line in tuning.summary_text(doc).splitlines())
    # A 0.15.0 file has neither the coverage block nor this one.
    del doc["coverage"]
    assert tuning.validate(doc) == []
    assert "Dashboard cards" not in tuning.summary_text(doc)
    assert any(line.startswith("Dashboard cards") for line in tuning.summary_text(_sample()).splitlines())


def test_a_cards_block_fails_the_checks_like_any_block():
    def problems_of(path, value):
        return tuning.validate(_put(_sample(), path, value))

    for path, value, start in (
        ("cards.rows.0.rule", "my-rule", "cards.rows[0].rule:"),
        ("cards.rows.0.agent", "my-agent", "cards.rows[0].agent:"),
        ("cards.rows.0.extra", 1, "cards.rows[0]:"),
        ("cards.shown", -1, "cards.shown: must be 0 or more"),
        ("cards.rows.0.answers.fine", 1, "cards.rows[0].answers:"),
    ):
        found = problems_of(path, value)
        assert any(problem.startswith(start) for problem in found), (path, found)
        # A problem line names the path and the check, never the value.
        assert not any(text in problem for problem in found for text in ("my-rule", "my-agent", "fine")), found
    doc = _sample()
    doc["cards"]["rows"] = [doc["cards"]["rows"][0]] * 41
    assert any(problem.startswith("cards.rows: has more items") for problem in tuning.validate(doc))
    doc = _sample()
    del doc["cards"]["rows"][0]["exact"]
    assert "cards.rows[0].exact: is missing" in tuning.validate(doc)
    doc = _sample()
    del doc["cards"]["shown"]
    assert "cards.shown: is missing" in tuning.validate(doc)
    # median_days and answers are optional.
    doc = _sample()
    for row in doc["cards"]["rows"]:
        row.pop("median_days", None)
        row.pop("answers", None)
    assert tuning.validate(doc) == []


# -- build ---------------------------------------------------------------------------------


def test_build_leaves_the_cards_block_out_without_a_dashboard_store(world):
    assert "cards" not in _build(world)
    assert "cards" not in _build(world, card_marks=None)
    doc = _build(world, card_marks=_marks([]))
    # A dashboard that showed no card is not one that isn't there.
    assert doc["cards"] == {"shown": 0, "rows": []}
    assert list(doc)[-1] == "cards"
    assert tuning.validate(doc) == []


def test_build_counts_cards_shown_copied_bundled_ignored_landed_and_rated(world):
    secret_change = _change("C:\\Users\\me\\x")
    rows = [
        # A custom agent's card, copied, landed three days on to its own value.
        _row("spawn-tools-list:my-secret-agent", "spawn-tools-list", "my-secret-agent", project="proj-secret",
             folders=["proj-secret"], changes=[secret_change], copied=True, landed_days=3, exact=True),
        # A built-in agent's card: nothing copied (a refused copy is never posted).
        _row("spawn-tools-list:Explore", "spawn-tools-list", "Explore", changes=[_change()]),
        # A card about no agent, copied only in the bundle, and ignored.
        _row("model-tier-main", "model-tier-main", None, bundled=True),
        # A card with no setting to change.
        _row("limit-pressure", "limit-pressure", None),
        # A rule this build does not know, about the main session.
        _row("made-up-rule", "made-up-rule", "top-level"),
    ]
    rec = Recommendation(id="model-tier-main", key="model-tier-main")
    ignores.set_ignored(world.config_dir, [rec], ignored=True, project=None)
    tip_feedback = {("recommendation", "model-tier-main"): {"answer": "wrong", "set_at": "2026-09-10T10:00:00Z"}}
    doc = _build(world, card_marks=_marks(rows, ["proj-secret"]), tip_feedback=tip_feedback)

    assert doc["cards"]["shown"] == 5
    pairs = _by_pair(doc)
    assert pairs[("spawn-tools-list", "custom")] == {
        "rule": "spawn-tools-list", "agent": "custom", "shown": 1, "copied": 1, "bundled": 0, "ignored": 0,
        "landable": 1, "landed": 1, "exact": 1, "median_days": 3,
    }
    assert pairs[("spawn-tools-list", "Explore")]["copied"] == 0
    assert pairs[("spawn-tools-list", "Explore")]["landable"] == 1 and pairs[("spawn-tools-list", "Explore")]["landed"] == 0
    assert "median_days" not in pairs[("spawn-tools-list", "Explore")]
    assert pairs[("model-tier-main", "none")] == {
        "rule": "model-tier-main", "agent": "none", "shown": 1, "copied": 1, "bundled": 1, "ignored": 1,
        "landable": 0, "landed": 0, "exact": 0, "answers": {"wrong": 1},
    }
    assert pairs[("limit-pressure", "none")]["landable"] == 0
    assert ("other", "main") in pairs

    assert tuning.validate(doc) == []
    assert_privacy_deep(doc)
    text = tuning.dumps(doc)
    for planted in ("my-secret-agent", "proj-secret", "made-up-rule", "Users", "spawn-tools-list:", "model-tier-main:"):
        assert planted not in text, planted


def test_median_days_is_the_middle_of_the_days_to_landing_not_the_longest(world):
    rows = [_row(f"c{n}", "model-tier", None, changes=[_change()], landed_days=days) for n, days in enumerate((20, 1, 3))]
    [row] = _build(world, card_marks=_marks(rows))["cards"]["rows"]
    assert (row["landed"], row["median_days"]) == (3, 3)
    # With an even number of cards it is the lower of the two in the middle.
    rows = [_row(f"d{n}", "model-tier", None, changes=[_change()], landed_days=days) for n, days in enumerate((9, 2, 4, 30))]
    [row] = _build(world, card_marks=_marks(rows))["cards"]["rows"]
    assert row["median_days"] == 4


def test_a_row_whose_times_do_not_read_is_left_out_or_not_counted_landed(world):
    odd_start = _row("a", "model-tier", None, changes=[_change()])
    odd_start["first_shown_at"] = "not a time"
    odd_landing = _row("b", "model-tier", None, changes=[_change()], landed_days=2)
    odd_landing["landed_at"] = "not a time either"
    doc = _build(world, card_marks=_marks([odd_start, odd_landing]))
    assert doc["cards"]["shown"] == 1
    [row] = doc["cards"]["rows"]
    assert (row["shown"], row["landable"], row["landed"], row["exact"]) == (1, 1, 0, 0)
    assert "median_days" not in row


def test_the_cards_block_follows_the_exports_projects_and_window(world):
    rows = [
        _row("every", project="*"),
        _row("early", project="*", shown=datetime(2026, 7, 1, tzinfo=timezone.utc)),
        _row("one", project="p1", folders=["p1"]),
        _row("two", project="p2", folders=["p2"]),
        _row("both", project="p12", folders=["p1", "p2"]),
    ]
    # One project selected: its rows and the rows of views that include it; the every-project rows are left out.
    narrow = _build(world, card_marks=_marks(rows, ["p1"], every_project=False))
    assert narrow["cards"]["shown"] == 2
    # Every project: the every-project rows and every project row of a selected folder.
    wide = _build(world, card_marks=_marks(rows, ["p1", "p2"], every_project=True))
    assert wide["cards"]["shown"] == 4
    # Before the window starts counts for nothing, and nothing selected keeps no project rows.
    none = _build(world, card_marks=_marks(rows, [], every_project=False))
    assert none["cards"] == {"shown": 0, "rows": []}


def test_the_card_rows_keep_the_most_shown_and_the_total_counts_them_all(world):
    rules = tuning.CARD_RULES[:45]
    rows = []
    for index, rule in enumerate(rules):
        rows += [_row(f"{rule}:{n}", rule, None) for n in range(index + 1)]
    doc = _build(world, card_marks=_marks(rows))
    kept = doc["cards"]["rows"]
    assert len(kept) == tuning.CARD_ROWS_KEPT == 40
    assert doc["cards"]["shown"] == sum(range(1, 46)) == len(rows)
    assert [r["shown"] for r in kept] == list(range(45, 5, -1))
    assert tuning.validate(doc) == []


def test_ties_are_ordered_by_rule_then_agent(world):
    rows = [_row("k1", "spawn-tools-list", "custom"), _row("k2", "spawn-tools-list", "Explore"), _row("k3", "limit-pressure")]
    doc = _build(world, card_marks=_marks(rows))
    assert [(r["rule"], r["agent"]) for r in doc["cards"]["rows"]] == [
        ("limit-pressure", "none"), ("spawn-tools-list", "Explore"), ("spawn-tools-list", "custom"),
    ]


# -- the plain-words summary ---------------------------------------------------------------


def test_the_summary_says_how_the_cards_were_used():
    block = {
        "shown": 14,
        "rows": [
            {"rule": "spawn-tools-list", "agent": "custom", "shown": 8, "copied": 4, "bundled": 3, "ignored": 0,
             "landable": 8, "landed": 8, "exact": 0, "median_days": 5},
            {"rule": "model-tier-main", "agent": "none", "shown": 3, "copied": 0, "bundled": 0, "ignored": 3,
             "landable": 3, "landed": 0, "exact": 0},
            {"rule": "spawn-tools-list", "agent": "workflow-subagent", "shown": 3, "copied": 3, "bundled": 1,
             "ignored": 0, "landable": 0, "landed": 0, "exact": 0, "answers": {"useful": 1}},
        ],
    }
    lines = tuning._cards_lines(block)
    assert lines == [
        "Dashboard cards: 14 cards shown, 7 copied and 3 ignored. 4 cards were copied only in the prompt for every open fix.",
        "Of 11 cards that change a setting, 8 left the list after you changed it, 0 to the card's own value.",
        "Most shown: spawn-tools-list 11 and model-tier-main 3.",
        "Your ratings of them: useful 1.",
    ]
    for line in lines:
        _plain(line)
    # One card copied only in the bundle is "was"; no landable card, no bundle, no rating: fewer lines.
    small = {
        "shown": 1,
        "rows": [{"rule": "model-tier", "agent": "none", "shown": 1, "copied": 1, "bundled": 1, "ignored": 0,
                  "landable": 0, "landed": 0, "exact": 0}],
    }
    assert tuning._cards_lines(small) == [
        "Dashboard cards: 1 card shown, 1 copied and 0 ignored. 1 card was copied only in the prompt for every open fix.",
        "Most shown: model-tier 1.",
    ]
    small["rows"][0]["bundled"] = 0
    assert tuning._cards_lines(small)[0] == "Dashboard cards: 1 card shown, 1 copied and 0 ignored."
    assert tuning._cards_lines({"shown": 0, "rows": []}) == ["Dashboard cards: none shown in this window."]
    _plain(tuning._cards_lines({"shown": 0, "rows": []})[0])
    # In the whole summary, after the compactions lines, for the sample file too.
    doc = _sample()
    text = tuning.summary_text(doc)
    assert text.splitlines()[-len(tuning._cards_lines(doc["cards"])) :] == tuning._cards_lines(doc["cards"])


# -- the design's reference figures --------------------------------------------------------

#: The block the design worked out for a synthetic dashboard store over the
#: frozen data (every project, 30 days): (rule, agent, shown, copied, bundled,
#: ignored, landable, landed, median days, answers).
_REFERENCE = [
    ("spawn-claude-md", "custom", 12, 4, 4, 0, 12, 0, None, None),
    ("spawn-tools-list", "custom", 8, 4, 3, 0, 8, 8, 5, None),
    ("spawn-unused-skills", "custom", 6, 0, 0, 0, 6, 0, None, None),
    ("limit-pressure", "none", 3, 1, 1, 0, 0, 0, None, None),
    ("long-context-share", "none", 3, 1, 1, 0, 0, 0, None, None),
    ("model-tier-main", "none", 3, 0, 0, 3, 3, 0, None, None),
    ("spawn-shared-claude-md", "none", 3, 2, 2, 0, 0, 0, None, None),
    ("spawn-tools-list", "workflow-subagent", 3, 3, 1, 0, 0, 0, None, {"useful": 1}),
    ("spawn-unused-skills", "Explore", 3, 0, 0, 0, 3, 0, None, None),
    ("agent-model-asked", "custom", 2, 0, 0, 0, 0, 0, None, None),
    ("agent-model-asked", "general-purpose", 2, 0, 0, 0, 0, 0, None, None),
    ("agent-model-asked", "workflow-subagent", 2, 0, 0, 0, 0, 0, None, None),
    ("agent-model-inherited", "workflow-subagent", 2, 0, 0, 0, 0, 0, None, None),
    ("baseline-bloat", "none", 2, 1, 1, 0, 0, 0, None, None),
    ("data-quality", "none", 2, 0, 0, 0, 0, 0, None, None),
    ("effort-mismatch", "none", 2, 0, 0, 0, 2, 0, None, None),
    ("hook-block-resent", "none", 2, 1, 1, 0, 0, 0, None, None),
    ("run-split", "custom", 2, 1, 1, 0, 0, 0, None, None),
    ("spawn-claude-md", "general-purpose", 2, 0, 0, 0, 2, 0, None, None),
    ("spawn-task-prompt", "custom", 2, 0, 0, 0, 0, 0, None, None),
    ("spawn-tools-list", "general-purpose", 2, 0, 0, 0, 2, 2, 4, None),
    ("spawn-unused-mcp", "custom", 2, 0, 0, 0, 2, 0, None, None),
    ("subagent-volume", "workflow-subagent", 2, 0, 0, 0, 0, 0, None, None),
    ("batch-instructions", "none", 1, 0, 0, 0, 0, 0, None, None),
    ("compaction-window", "main", 1, 1, 1, 0, 1, 0, None, None),
    ("mcp-unused-server", "none", 1, 0, 0, 0, 0, 0, None, None),
    ("plan-rounds", "none", 1, 0, 0, 0, 0, 0, None, None),
    ("run-split", "Explore", 1, 1, 1, 0, 0, 0, None, None),
    ("run-split", "Plan", 1, 0, 0, 0, 0, 0, None, None),
    ("spawn-task-prompt", "workflow-subagent", 1, 0, 0, 0, 0, 0, None, None),
    ("spawn-unused-skills", "general-purpose", 1, 0, 0, 0, 1, 0, None, None),
]


def test_the_cards_block_matches_the_designs_reference_figures(world):
    """The 80 rows of the design's synthetic store, made again from its 31
    pairs (which row of a pair was copied, bundled, landed and so on is made
    up: the block counts rows, so only the numbers matter), come out as the
    design's block: 80 shown in 31 rows, 20 copied, 17 of them only in the
    bundle, 3 ignored, 42 landable, 10 landed, none exactly."""
    rows, wanted, rated = [], [], {}
    for rule, agent, shown, copied, bundled, ignored, landable, landed, median, answers in _REFERENCE:
        own = copied - bundled
        for n in range(shown):
            key = f"{rule}:{agent}:{n}"
            project, folders = "*", []
            if rule == "model-tier-main":
                # One card in three views; the ignore is every project's.
                key = rule
                project, folders = ("*", []) if n == 0 else (f"p{n}", [f"p{n}"])
            agent_type = {"none": None, "main": "top-level", "custom": f"private-agent-{n}"}.get(agent, agent)
            rows.append(
                _row(
                    key, rule, agent_type, project=project, folders=folders,
                    changes=[_change()] if n < landable else [],
                    copied=n < own,
                    # One copied card of a pair that also has bundle-only cards was bundled as well:
                    # it counts as copied, not as copied only in the bundle.
                    bundled=own <= n < own + bundled or (n == 0 and own > 0 and bundled > 0),
                    landed_days=median if n < landed else None,
                )
            )
        if answers:
            rated[("recommendation", f"{rule}:{agent}:0")] = {"answer": next(iter(answers)), "set_at": "2026-09-10T10:00:00Z"}
        item = {"rule": rule, "agent": agent, "shown": shown, "copied": copied, "bundled": bundled, "ignored": ignored,
                "landable": landable, "landed": landed, "exact": 0}
        if median is not None:
            item["median_days"] = median
        if answers:
            item["answers"] = answers
        wanted.append(item)
    ignores.set_ignored(world.config_dir, [Recommendation(id="model-tier-main", key="model-tier-main")], ignored=True, project=None)
    doc = _build(world, card_marks=_marks(rows, ["p1", "p2"]), tip_feedback=rated)
    assert doc["cards"]["shown"] == len(rows) == 80
    assert doc["cards"]["rows"] == wanted
    totals = {k: sum(r[k] for r in doc["cards"]["rows"]) for k in ("copied", "bundled", "ignored", "landable", "landed", "exact")}
    assert totals == {"copied": 20, "bundled": 17, "ignored": 3, "landable": 42, "landed": 10, "exact": 0}
    assert len(doc["cards"]["rows"]) == 31
    assert len(json.dumps({"cards": doc["cards"]}, indent=2).encode("utf-8")) < 8_000
