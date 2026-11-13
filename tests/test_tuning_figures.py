"""The tuning export's ``figures`` block (K1 group 3): the figures some advice rests on, which no page
shows. Cost per reply, context growth, what a summary cost and saved, corrections after a summary, spend
by model and week, and the cold returns by context.

The shared helpers and worlds are ``test_tuning``'s. The in-memory worlds are built from ``model`` turns,
as ``test_measured`` builds them, so each figure's rule is tried on numbers made to show it.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from claudeglass import measured, pricing as pricing_mod, recache, tuning, usage
from claudeglass.config import Config
from claudeglass.corpus import SessionBundle, load_corpus
from claudeglass.model import CaptureTag, Event, EventKind, TranscriptMeta, TranscriptResult, Turn
from claudeglass.pricing import load_pricing, price_turn

from helpers import assert_privacy_deep, system_line, write_jsonl
from test_tuning import (
    DAYS,
    PRIVATE,
    TODAY,
    _at,
    _big_reply,
    _breaks,
    _build,
    _ctx,
    _human,
    _make_world,
    _mini_world,
    _note,
    _notice,
    _plain,
    _put,
    _sample,
    _say,
)

PRICING = load_pricing()

# -- measured: the two rows figures gate on --------------------------------------------------------


def test_the_field_versions_hold_the_two_rows_the_summary_and_correction_figures_gate_on():
    assert measured.FIELD_VERSIONS["estimated"] == 45
    assert measured.FIELD_VERSIONS["queued_correction"] == 37
    assert measured.NEWEST == 51
    assert not measured.measures(TranscriptResult(parser_version=44), "estimated")
    assert measured.measures(TranscriptResult(parser_version=45), "estimated")
    assert not measured.measures(TranscriptResult(parser_version=36), "queued_correction")
    assert measured.measures(TranscriptResult(parser_version=37), "queued_correction")


# -- the cold returns and wake-ups, one loop ----------------------------------------------------------


def _ctx_of(tmp_path, lines, pricing=None) -> tuning._Ctx:
    """A ctx over one main transcript written from ``lines``."""
    project = tmp_path / "mini"
    project.mkdir(parents=True, exist_ok=True)
    write_jsonl(project / "m1.jsonl", lines)
    return tuning._Ctx(load_corpus([project]), Config(tz="UTC"), pricing, None, DAYS, TODAY, tmp_path)


def test_break_turns_yield_each_cold_return_and_wake_up_and_breaks_sums_them(tmp_path):
    ctx = _ctx_of(
        tmp_path,
        [
            _note(0),
            _human(5, "add the login form to src/app.py"),
            _big_reply(),
            _human(947, "keep going"),
            _say(948, "Done.", cache_creation_input_tokens=7000),
            _notice(4548),
            _say(4549, "They passed.", cache_creation_input_tokens=9000),
        ],
    )
    found = list(tuning._break_turns(ctx))
    assert [(kind, turn.cache_creation_tokens) for _s, kind, _last, turn in found] == [("cold", 7000), ("wake", 9000)]
    assert all(s is ctx.sessions[0] and last.ctx > 0 for s, _kind, last, _turn in found)
    assert tuning._breaks(ctx) == ([1, 7000], [1, 9000])


# -- in-memory worlds -----------------------------------------------------------------------------------

#: A Monday, so ISO week 38.
_BASE = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
_ids = itertools.count(1)
_PROBE = {"Read": 1}


def _iso(second: int) -> str:
    return (_BASE + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _turn(ctx: int, at: int, model: str = "claude-opus-5-5", output: int = 100, **kw) -> Turn:
    """A reply that read ``ctx`` tokens from the cache and wrote ``output``."""
    return Turn(
        message_id=f"msg_{next(_ids)}", turn_index=1, ts=_iso(at), model=model, cache_read_tokens=ctx,
        output_tokens=output, ctx=ctx, **kw,
    )  # fmt: skip


def _request(at: int, model: str = "claude-opus-5-5", output: int = 20_000) -> Turn:
    """The estimated request that wrote a summary: priced, not a reply."""
    return Turn(
        message_id=f"compaction-{next(_ids)}", turn_index=1, ts=_iso(at), model=model, output_tokens=output,
        ctx=50_000, is_synthetic=True, estimated="compaction",
    )  # fmt: skip


def _result(turns, kind="top-level", session="s1", events=(), version=None) -> TranscriptResult:
    for i, turn in enumerate(turns, 1):
        turn.turn_index = i
    meta = TranscriptMeta(kind=kind, session_id=session, agent_id=None if kind == "top-level" else f"agent-{next(_ids)}")
    return TranscriptResult(meta=meta, turns=list(turns), events=list(events), parser_version=version)


def _boundary(at: int, trigger: str = "auto", pre: int = 200_000, post: int = 20_000) -> Event:
    return Event(kind=EventKind.COMPACT_BOUNDARY, ts=_iso(at), pre_tokens=pre, post_tokens=post, trigger=trigger)


def _ctx_in(tmp_path, *bundles, pricing=PRICING) -> tuning._Ctx:
    return tuning._Ctx(NS(sessions=list(bundles)), Config(tz="UTC"), pricing, tmp_path, DAYS, TODAY, None)


def _bundle(top, *subs, session="s1") -> SessionBundle:
    return SessionBundle(session_id=session, top=top, subs=list(subs))


def _cost(turn: Turn, pricing=PRICING) -> float:
    return price_turn(turn, pricing.resolve_model(turn.model)).total


def _close(actual, expected, where="figures"):
    """Equal as the build writes them: the same keys, amounts to 1e-6."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict) and set(actual) == set(expected), (where, sorted(actual), sorted(expected))
        for key, item in expected.items():
            _close(actual[key], item, f"{where}.{key}")
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, abs=1e-6), (where, actual, expected)
    else:
        assert actual == expected, (where, actual, expected)


def _valid(block: dict) -> None:
    assert tuning.validate(_put(_sample(), "figures", block)) == []


# -- the spec node and the sample -----------------------------------------------------------------------


def test_a_sample_with_the_figures_block_passes_and_a_file_without_it_still_does():
    doc = _put(_sample(), "figures", _sample()["figures"])
    assert tuning.validate(doc) == []
    assert tuning.loads(tuning.dumps(doc)) == doc
    without = _sample()
    del without["figures"]
    assert tuning.validate(without) == []
    assert "figures" in tuning.spec().fields and "figures" not in tuning.spec().required


_WEEKS_9 = {f"2026-W{week:02d}": 1.0 for week in range(30, 39)}


@pytest.mark.parametrize(
    "path, value, check, leak",
    [
        ("figures.models", {"claude-opus-5-5-mine": {"replies": 1}}, "figures.models: has a key that is not in the spec", "mine"),
        ("figures.models", {"my-fine-tune": {"replies": 1}}, "figures.models: has a key that is not in the spec", "tune"),
        ("figures.models", {"Opus 5.5": {"replies": 1}}, "figures.models: has a key that is not in the spec", "Opus"),
        ("figures.reply_cost", {"Explore": {}}, "figures.reply_cost: has a key that is not in the spec", "Explore"),
        ("figures.reply_cost.main", {"300k": {"replies": 1}}, "figures.reply_cost.main: has a key", "300k"),
        ("figures.after_summary.stretches", {"long": {}}, "figures.after_summary.stretches: has a key", "long"),
        (
            "figures.returns.to_200k",
            {"returns": 1, "context_tokens": 1, "rewritten_tokens": 1},
            "figures.returns.to_200k.rebuilt: is missing",
            "",
        ),
        (
            "figures.models.other.weekly_usd",
            {"week of the move": 1.0},
            "figures.models.other.weekly_usd: has a key that is not in the spec",
            "move",
        ),
        ("figures.models.other.weekly_usd", _WEEKS_9, "figures.models.other.weekly_usd: has more entries", ""),
        (
            "figures.models",
            {word: {"replies": 1} for word in sorted(pricing_mod.load_packaged_pricing().models)[:9]},
            "figures.models: has more entries than the spec allows",
            "",
        ),
        ("figures.summaries.paid_back", -1, "figures.summaries.paid_back: must be 0 or more", ""),
        ("figures.growth.shrinks", "3", "figures.growth.shrinks: must be a whole number", "3"),
    ],
)
def test_the_figures_block_refuses_a_name_a_model_id_and_a_wrong_kind(path, value, check, leak):
    problems = tuning.validate(_put(_sample(), path, value))
    assert any(check in problem for problem in problems), problems
    # A line says where and which check, never what was found there.
    if leak:
        assert not any(leak in problem for problem in problems), problems
    with pytest.raises(tuning.TuningError) as raised:
        tuning.dumps(_put(_sample(), path, value))
    assert raised.value.problems == problems
    if leak:
        assert leak not in str(raised.value)


# -- the model word -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, word",
    [
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("claude-opus-5-5[1m]", "claude-opus-5-5"),
        ("us.anthropic.claude-sonnet-5-5-v1:0", "claude-sonnet-5-5"),
        ("opus", "claude-opus-5-5"),
        ("claude-haiku-4-5", "claude-haiku-4-5-20251001"),
        # A newer release the card prices as an older one, a name of nobody's, and no model at all.
        ("claude-opus-5-9", "other"),
        ("my-fine-tune", "other"),
        (None, "other"),
        ("", "other"),
        ("<synthetic>", "other"),
    ],
)
def test_a_model_id_becomes_a_packaged_rate_card_id_or_other(model, word):
    assert tuning._model_word(model) == word
    assert word in tuning.MODEL_WORDS


def test_the_model_words_are_the_packaged_cards_ids_and_a_users_own_row_never_becomes_one(tmp_path):
    assert tuning.MODEL_WORDS == (*sorted(pricing_mod.load_packaged_pricing().models), "other")
    assert len(tuning.MODEL_WORDS) == 26
    card = tmp_path / "pricing.toml"
    card.write_text(
        '''version = "2099-01-01-test"
currency = "USD"

[models."acme-model"]
aliases = []
input = 1.0
output = 2.0
cache_write_5m = 0.5
cache_write_1h = 0.8
cache_read = 0.1
''',
        encoding="utf-8",
    )
    mine = load_pricing(path=card)
    turn = _turn(10_000, 0, model="acme-model", output=1_000_000, human_prompt_chars=5)
    assert _cost(turn, mine) > 0
    block = tuning._figures(_ctx_in(tmp_path, _bundle(_result([turn])), pricing=mine))
    # Its replies are "other", its cost is the user's own card's.
    assert set(block["models"]) == {"other"}
    assert block["models"]["other"]["replies"] == 1
    assert block["models"]["other"]["cost_usd"] == pytest.approx(_cost(turn, mine), abs=1e-6)
    assert "acme-model" not in tuning.dumps(_put(_sample(), "figures", block))


# -- 1. reply cost --------------------------------------------------------------------------------------


def _cell(replies, lookups=(), measured_replies=None, pricing=PRICING):
    cell = {
        "replies": len(replies),
        "measured_replies": len(replies) if measured_replies is None else measured_replies,
        "lookups": len(lookups),
    }
    if pricing is not None:
        sonnet = pricing.models[pricing.aliases["sonnet"]]
        cell |= {
            "cost_usd": tuning._num(sum(_cost(t, pricing) for t in replies)),
            "lookup_usd": tuning._num(sum(_cost(t, pricing) for t in lookups)),
            "sonnet_usd": tuning._num(sum(price_turn(t, sonnet).total for t in replies)),
            "lookup_sonnet_usd": tuning._num(sum(price_turn(t, sonnet).total for t in lookups)),
        }
    return cell


def test_reply_cost_splits_the_replies_by_where_they_ran_and_the_context_they_read(tmp_path):
    small, probe, big = _turn(50_000, 0, human_prompt_chars=5), _turn(150_000, 10, tool_calls_by_tool=_PROBE), _turn(250_000, 20)
    request, empty = _request(30), _turn(0, 40)
    agent_probe = _turn(120_000, 50, tool_calls_by_tool=_PROBE)
    workflow = _turn(90_000, 60)
    old_probe = _turn(130_000, 70, tool_calls_by_tool=_PROBE)
    bundle = _bundle(
        _result([small, probe, big, request, empty]),
        _result([agent_probe], kind="subagent"),
        _result([workflow], kind="workflow-agent"),
        _result([old_probe], kind="subagent", version=measured.BEFORE_STAMP),
    )
    block = tuning._figures(_ctx_in(tmp_path, bundle))
    _close(
        block["reply_cost"],
        {
            "main": {
                "under_100k": _cell([small]),
                "to_200k": _cell([probe], [probe]),
                "over_200k": _cell([big]),
            },
            # The older digest's reply is a reply and not a measured one, and its lookup is not counted.
            "direct": {"to_200k": _cell([agent_probe, old_probe], [agent_probe], measured_replies=1)},
            "workflow": {"under_100k": _cell([workflow])},
        },
    )
    # The summary request and the reply that read nothing are in no cell.
    counted = sum(cell["replies"] for place in block["reply_cost"].values() for cell in place.values())
    assert counted == 6
    # The Sonnet figure is the same replies at the row the "sonnet" alias names.
    cell = block["reply_cost"]["main"]["to_200k"]
    assert cell["sonnet_usd"] == pytest.approx(
        price_turn(probe, PRICING.models[PRICING.aliases["sonnet"]]).total, abs=1e-6
    )
    assert cell["sonnet_usd"] != cell["cost_usd"]
    _valid(block)


def test_reply_cost_holds_counts_alone_without_a_rate_card(tmp_path):
    only = _turn(150_000, 0, human_prompt_chars=5, tool_calls_by_tool=_PROBE)
    block = tuning._figures(_ctx_in(tmp_path, _bundle(_result([only])), pricing=None))
    assert block["reply_cost"] == {"main": {"to_200k": {"replies": 1, "measured_replies": 1, "lookups": 1}}}
    assert block["models"] == {"claude-opus-5-5": {"replies": 1}}
    _valid(block)


# -- 2. growth ------------------------------------------------------------------------------------------


def test_growth_counts_each_rise_between_replies_and_not_the_reply_after_a_summary(tmp_path):
    main = _result(
        [
            _turn(100_000, 0, human_prompt_chars=5),
            _turn(101_000, 10),
            _turn(103_000, 20),
            _request(30),
            _turn(30_000, 40, preceding_event_kinds=(EventKind.COMPACT_BOUNDARY,)),
            _turn(31_000, 50),
            _turn(20_000, 60),
            _turn(25_000, 70),
        ]
    )
    scheduled = _result(
        [_turn(1_000, 0), _turn(60_000, 10), _turn(61_000, 20)], session="s2", events=[Event(kind=EventKind.SCHEDULED_TASK)]
    )
    block = tuning._figures(_ctx_in(tmp_path, _bundle(main), _bundle(scheduled, session="s2")))
    assert block["growth"] == {
        "by_reply": {
            "to_1k": {"replies": 2, "tokens": 2000},
            "to_2k": {"replies": 1, "tokens": 2000},
            "to_5k": {"replies": 1, "tokens": 5000},
        },
        # The mean rise of the one main session: 9,000 over 4 replies.
        "by_session": {"to_5k": {"sessions": 1, "replies": 4}},
        "shrinks": 1,
    }
    _valid(block)


def test_a_reply_after_a_boundary_with_no_request_logged_before_it_has_no_rise_either(tmp_path):
    """A session whose digest holds no estimated request for the summary (none could be sized) is still
    cut at the boundary: the fall to the new context is neither a rise nor a shrink."""
    main = _result(
        [
            _turn(100_000, 0, human_prompt_chars=5),
            _turn(101_000, 10),
            _turn(30_000, 20, preceding_event_kinds=(EventKind.COMPACT_BOUNDARY,)),
            _turn(31_000, 30),
        ]
    )
    block = tuning._figures(_ctx_in(tmp_path, _bundle(main)))
    assert block["growth"] == {
        "by_reply": {"to_1k": {"replies": 2, "tokens": 2000}},
        "by_session": {"to_1k": {"sessions": 1, "replies": 2}},
        "shrinks": 0,
    }


# -- 3. summaries ---------------------------------------------------------------------------------------


def test_summaries_say_what_each_cost_and_saved_and_how_many_replies_followed(tmp_path):
    w = _mini_world(tmp_path)
    ctx = _ctx(w)
    block = tuning._figures(ctx)["summaries"]
    doc = tuning.build(w.corpus, w.config, w.pricing, config_dir=w.config_dir, days=DAYS, today=TODAY, claude_root=w.claude_root)
    top = ctx.sessions[0].top
    assert (block["summaries"], block["auto"], block["sized"]) == (3, 1, 3)
    assert (block["pre_tokens"], block["post_tokens"]) == (300_000, 60_000)
    assert block["followed_to_next"] == {"to_10": 2} and block["followed_to_end"] == {"to_10": 1}
    # The records are the compactions block's own: the Explore run's summary is an agent's, not counted here.
    assert block["summaries"] + block["unmeasured"] == doc["compactions"]["summaries"] == 3
    records = [r for r in ctx.compactions[0].records if r.kind == "top-level"]
    assert len(records) == 3 and len(ctx.compactions[0].records) == 4
    write = sum(r.next_turn_write_cost for r in records if tuning.compaction_mod._join_is_tight(r))
    assert block["write_usd"] == pytest.approx(write, abs=1e-6) and write > 0
    requests = [t for t in top.turns if t.estimated == "compaction"]
    assert len(requests) == 3
    assert block["request_usd"] == pytest.approx(sum(_cost(t, w.pricing) for t in requests), abs=1e-6)
    rate = w.pricing.resolve_model(tuning._dominant_transcript_model(top)).rates.cache_read / 1_000_000
    assert block["read_saving_usd"] == pytest.approx(3 * 80_000 * rate, abs=1e-6)
    even = ((block["request_usd"] + block["write_usd"]) / 3) / (block["read_saving_usd"] / 3)
    assert block["paid_back"] == sum(1 for followed in (1, 1, 1) if followed >= even)


def test_two_bundles_with_one_session_id_each_count_their_own_summaries(tmp_path):
    """A project folder copied to keep its history, or two projects roots holding one session, make two
    bundles with one session id. Each takes its own records off the front of that id's list, so the
    export is built and each summary is counted once."""
    w = _mini_world(tmp_path)
    project = tmp_path / "projects" / "proj-mini"
    twin = project.parent / "proj-mini-copy"
    shutil.copytree(project, twin)
    corpus = load_corpus([project, twin])
    assert len(corpus.sessions) == 2 and len({b.session_id for b in corpus.sessions}) == 1
    doc = tuning.build(corpus, w.config, w.pricing, config_dir=w.config_dir, days=DAYS, today=TODAY, claude_root=w.claude_root)
    block = doc["figures"]["summaries"]
    assert block["summaries"] == doc["compactions"]["summaries"] == 6
    assert (block["auto"], block["sized"]) == (2, 6)
    assert block["followed_to_next"] == {"to_10": 4} and block["followed_to_end"] == {"to_10": 2}
    once = tuning._figures(_ctx(w))["summaries"]
    assert block["request_usd"] == pytest.approx(2 * once["request_usd"], abs=1e-6)
    assert block["write_usd"] == pytest.approx(2 * once["write_usd"], abs=1e-6)


def test_a_summary_pays_back_when_the_replies_after_it_reach_the_mean_break_even(tmp_path):
    replies = [_turn(20_000 + 100 * i, 20 + i) for i in range(40)]
    replies[0].preceding_event_kinds = (EventKind.COMPACT_BOUNDARY,)
    last = _turn(20_000, 110)
    last.preceding_event_kinds = (EventKind.COMPACT_BOUNDARY,)
    main = _result(
        [_turn(200_000, 0, human_prompt_chars=5), _request(10), *replies, _request(100), last],
        events=[_boundary(11), _boundary(101, "manual")],
    )
    block = tuning._figures(_ctx_in(tmp_path, _bundle(main)))["summaries"]
    assert (block["summaries"], block["auto"], block["sized"]) == (2, 1, 2)
    assert block["followed_to_next"] == {"to_50": 1} and block["followed_to_end"] == {"to_10": 1}
    assert block["request_usd"] == pytest.approx(2 * _cost(_request(0)), abs=1e-6)
    saving = 2 * 180_000 * PRICING.resolve_model("claude-opus-5-5").rates.cache_read / 1_000_000
    assert block["read_saving_usd"] == pytest.approx(saving, abs=1e-6)
    even = (block["request_usd"] / 2) / (block["read_saving_usd"] / 2)
    # One summary was followed by 40 replies and the other by 1; the mean break-even sits between them.
    assert 1 < even < 40
    assert block["paid_back"] == 1
    _valid({"summaries": block})


def test_a_session_an_older_version_read_is_left_out_of_summaries_and_corrections_never_read_as_zero(tmp_path):
    w = _mini_world(tmp_path)
    corpus = copy.deepcopy(w.corpus)
    corpus.sessions[0].top.parser_version = measured.BEFORE_STAMP
    doc = tuning.build(corpus, w.config, w.pricing, config_dir=w.config_dir, days=DAYS, today=TODAY, claude_root=w.claude_root)
    figures = doc["figures"]
    assert figures["summaries"] == {
        "summaries": 0, "auto": 0, "sized": 0, "pre_tokens": 0, "post_tokens": 0, "unmeasured": 3,
        "followed_to_next": {}, "followed_to_end": {}, "request_usd": 0.0, "write_usd": 0.0, "read_saving_usd": 0.0,
    }  # fmt: skip
    assert figures["after_summary"] == {"sessions": 0, "unmeasured": 1, "stretches": {}}
    # The same replies are still replies; the older digest did not measure what a lookup is.
    assert figures["reply_cost"]["main"]["under_100k"]["measured_replies"] == 0
    assert "3 summaries left out of these figures: an older version read their sessions." in tuning.summary_text(doc).splitlines()
    assert measured.FIELD_VERSIONS["estimated"] == 45 and measured.FIELD_VERSIONS["queued_correction"] == 37


# -- 4. after a summary ---------------------------------------------------------------------------------


def _cycle(start: int, replies: int, **opening) -> list[Turn]:
    """A typed message and ``replies - 1`` more replies after it, a second apart from ``start``."""
    return [_turn(20_000 + 100 * i, start + i, human_prompt_chars=5 if i == 0 else None, **(opening if i == 0 else {})) for i in range(replies)]


def _after_world(tag: CaptureTag | None = None):
    first = _cycle(0, 12)
    # A summary fell in the middle of the first stretch.
    first[6:6] = [_request(5)]
    first[7].preceding_event_kinds = (EventKind.COMPACT_BOUNDARY,)
    second = _cycle(100, 12, human_correction=True)
    if tag is not None:
        second[3].cap = tag
    third = _cycle(200, 3)
    fourth = _cycle(300, 2)
    fourth[1].queued_correction = True
    summarised = _result([*first, *second, *third, *fourth], events=[_boundary(6)])
    plain = _result([*_cycle(0, 5), *_cycle(100, 5, human_correction=True)], session="s2")
    return _bundle(summarised), _bundle(plain, session="s2")


def _side(messages, corrections, tagged=0, fixes=0):
    return {"messages": messages, "corrections": corrections, "tagged": tagged, "fixes": fixes}


def test_after_summary_matches_the_next_message_to_one_after_as_long_a_stretch_without_a_summary(tmp_path):
    block = tuning._figures(_ctx_in(tmp_path, *_after_world()))["after_summary"]
    assert block == {
        "sessions": 1,
        "unmeasured": 0,
        "stretches": {
            "to_30": {"summarised": _side(1, 1), "plain": _side(1, 0)},
            "under_10": {"plain": _side(1, 1)},
        },
    }
    # With a shift word Claude wrote on the flagged message, that message is tagged, and a fix.
    tagged = tuning._figures(_ctx_in(tmp_path, *_after_world(CaptureTag(has_tl=True, shift="fix"))))["after_summary"]
    assert tagged["stretches"]["to_30"]["summarised"] == _side(1, 1, tagged=1, fixes=1)
    assert tagged["stretches"]["to_30"]["plain"] == _side(1, 0)
    _valid({"after_summary": tagged})


# -- 5. models ------------------------------------------------------------------------------------------

_NINE = (
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5-5", "claude-sonnet-5", "claude-fable-5-1",
    "claude-fable-5", "claude-haiku-5-5", "claude-haiku-4-5-20251001",
)  # fmt: skip


def _models_world(tmp_path):
    """Nine packaged ids and a name of nobody's, over ten weeks. Opus 4.8 ran in the first two only, and dear."""
    week = 7 * 24 * 3600
    main, agent = [_turn(10_000, 0, human_prompt_chars=5)], []
    for n, model in enumerate(_NINE):
        for w in range(10):
            if (model == "claude-opus-4-8") != (w < 2):
                continue
            target = main if (n + w) % 2 else agent
            target.append(_turn(10_000, w * week + 60 * n, model=model, output=2_000_000 if model == "claude-opus-4-8" else 1_000 * (n + 1)))
    for w in range(2, 10):
        agent.append(_turn(10_000, w * week + 3000, model="my-fine-tune", output=5_000))
    main.append(_request(9 * week + 4000))
    main.sort(key=lambda turn: turn.ts)
    agent.sort(key=lambda turn: turn.ts)
    bundle = _bundle(_result(main), _result(agent, kind="subagent"))
    return bundle, _ctx_in(tmp_path, bundle)


def test_models_keep_the_dearest_seven_and_fold_the_rest_into_other_over_the_latest_eight_weeks(tmp_path):
    bundle, ctx = _models_world(tmp_path)
    models = tuning._figures(ctx)["models"]
    spent: dict = {}
    for tr in (bundle.top, *bundle.subs):
        for turn in tr.turns:
            word = tuning._model_word(turn.model)
            entry = spent.setdefault(word, {"replies": 0, "cost": 0.0})
            entry["replies"] += not turn.is_synthetic
            entry["cost"] += _cost(turn)
    ranked = sorted((w for w in spent if w != "other"), key=lambda w: (-spent[w]["cost"], -spent[w]["replies"], w))
    assert len(models) == 8 and len(ranked) == 9
    assert set(models) == {*ranked[:7], "other"}
    assert list(models) == [w for w in tuning.MODEL_WORDS if w in models]
    folded = [*ranked[7:], "other"]
    assert models["other"]["replies"] == sum(spent[w]["replies"] for w in folded) > spent["other"]["replies"]
    assert models["other"]["cost_usd"] == pytest.approx(sum(spent[w]["cost"] for w in folded), abs=1e-6)
    # The same totals as the report's usage-by-week table, summed by model word.
    table = next(t for t in usage.build_section(NS(sessions=[bundle]), PRICING, Config(tz="UTC")).tables if t.name == "by_week")
    by_week: dict = {}
    for period, model, turns, _tokens, cost in table.rows:
        word = tuning._model_word(model)
        word = word if word in ranked[:7] else "other"
        cell = by_week.setdefault(word, {})
        cell[period] = cell.get(period, 0.0) + cost
    latest = sorted({week for cells in by_week.values() for week, cost in cells.items() if cost > 0})[-8:]
    assert len(latest) == 8
    for word, entry in models.items():
        assert entry["weekly_usd"] == {w: pytest.approx(c, abs=1e-6) for w, c in sorted(by_week[word].items()) if w in latest and c > 0}
        assert set(entry["weekly_usd"]) <= set(latest)
    # Opus 4.8 ran only before those weeks: named, dear, and with no week.
    assert models["claude-opus-4-8"]["weekly_usd"] == {} and models["claude-opus-4-8"]["cost_usd"] > 0
    doc = _put(_sample(), "figures", {"models": models})
    assert "Not used in the file's last 8 weeks: Opus 4.8." in tuning.summary_text(doc).splitlines()
    _valid({"models": models})


# -- 6. returns -----------------------------------------------------------------------------------------


def test_returns_break_down_the_cold_returns_the_pieces_block_counts(tmp_path):
    lines = [
        _note(0),
        _human(5, "add the login form to src/app.py"),
        _big_reply(context=400_000),
        _human(947, "keep going"),
        _say(948, "Done.", cache_creation_input_tokens=7000),
        _say(950, "More.", cache_read_input_tokens=120_000),
        _human(1888, "continue"),
        _say(1889, "Done.", cache_creation_input_tokens=130_000),
    ]
    ctx = _ctx_of(tmp_path, lines, pricing=PRICING)
    returns = tuning._figures(ctx)["returns"]
    cold = [(last, turn) for _s, kind, last, turn in tuning._break_turns(ctx) if kind == "cold"]
    assert [(last.ctx + last.output_tokens > 300_000) for last, _turn_ in cold] == [True, False]
    flagged = {t.message_id for t in recache.detect(ctx.sessions[0].top.turns, recache.RecacheThresholds.from_config(ctx.config.thresholds))}
    assert [turn.message_id in flagged for _last, turn in cold] == [False, True]
    assert set(returns) == {"to_500k", "under_150k"}
    big, small = (last.ctx + last.output_tokens for last, _turn_ in cold)
    first, second = (turn for _last, turn in cold)
    _close(
        returns,
        {
            "to_500k": {
                "returns": 1, "context_tokens": big, "rewritten_tokens": 7000, "rebuilt": 0,
                "rewrite_usd": tuning._num(price_turn(first, PRICING.resolve_model(first.model)).cache_write_cost),
            },
            "under_150k": {
                "returns": 1, "context_tokens": small, "rewritten_tokens": 130_000, "rebuilt": 1,
                "rewrite_usd": tuning._num(price_turn(second, PRICING.resolve_model(second.model)).cache_write_cost),
            },
        },
    )  # fmt: skip
    # The pieces block's own count, to the token.
    pieces_cold, _wake = _breaks(tmp_path, lines)
    assert sum(item["returns"] for item in returns.values()) == pieces_cold["count"] == 2
    assert sum(item["rewritten_tokens"] for item in returns.values()) == pieces_cold["cache_write_tokens"]


def test_a_gap_over_a_summary_or_a_limit_adds_no_return(tmp_path):
    base = [_note(0), _human(5, "add the login form to src/app.py"), _big_reply()]
    after = [_human(1000, "keep going"), _say(1001, "Done.", cache_creation_input_tokens=7000)]
    assert tuning._figures(_ctx_of(tmp_path / "a", [*base, *after]))["returns"]["under_150k"]["returns"] == 1
    compacted = [system_line("compact_boundary", timestamp=_at(900)), *after]
    assert "returns" not in tuning._figures(_ctx_of(tmp_path / "b", [*base, *compacted]))
    notified = [_notice(1000), _say(1001, "Done.", cache_creation_input_tokens=7000)]
    assert "returns" not in tuning._figures(_ctx_of(tmp_path / "c", [*base, *notified]))


# -- the built document ---------------------------------------------------------------------------------

#: The ids a document may name: the packaged rate card's, as keys of ``figures.models``.
_ID = re.compile(r"claude-(?:opus|sonnet|haiku|fable)[A-Za-z0-9._-]*")


def _check_built(doc: dict, names) -> None:
    keys = list(doc)
    assert keys.index("figures") > keys.index("compactions")
    assert_privacy_deep(doc)
    text = tuning.dumps(doc)
    for name in names:
        assert name not in text, name
    assert set(_ID.findall(text)) <= set(doc["figures"]["models"]) <= set(tuning.MODEL_WORDS)
    assert tuning.validate(json.loads(text)) == []


def test_the_built_document_has_the_figures_block_and_holds_no_id_but_the_packaged_cards(tmp_path):
    w = _make_world(tmp_path / "a")
    doc = _build(w)
    _check_built(doc, [name for name in PRIVATE if not name.startswith("claude-")] + [str(w.root)])
    assert set(doc["figures"]["models"]) == {"claude-sonnet-5", "claude-haiku-4-5-20251001"}
    mini = _mini_world(tmp_path / "b")
    built = tuning.build(mini.corpus, mini.config, mini.pricing, config_dir=mini.config_dir, days=DAYS, today=TODAY, claude_root=mini.claude_root)
    _check_built(built, ["my-private-reviewer", "proj-mini", "mystery-trigger", "agent-fi", str(mini.root)])
    assert set(built["figures"]["models"]) == {"claude-opus-4-1", "claude-sonnet-5"}


_MAKE_WORLD = {
    "reply_cost": {
        "main": {
            "under_100k": {"replies": 67, "measured_replies": 67, "lookups": 0, "cost_usd": 0.429, "lookup_usd": 0.0, "sonnet_usd": 0.429, "lookup_sonnet_usd": 0.0},
            "to_200k": {"replies": 1, "measured_replies": 1, "lookups": 0, "cost_usd": 0.0247, "lookup_usd": 0.0, "sonnet_usd": 0.0127, "lookup_sonnet_usd": 0.0},
        },
        "direct": {
            "under_100k": {"replies": 4, "measured_replies": 4, "lookups": 0, "cost_usd": 0.0021, "lookup_usd": 0.0, "sonnet_usd": 0.0028, "lookup_sonnet_usd": 0.0},
        },
        "workflow": {
            "under_100k": {"replies": 1, "measured_replies": 1, "lookups": 0, "cost_usd": 0.0007, "lookup_usd": 0.0, "sonnet_usd": 0.0007, "lookup_sonnet_usd": 0.0},
        },
    },
    "growth": {
        "by_reply": {
            "to_500": {"replies": 58, "tokens": 0},
            "to_50k": {"replies": 2, "tokens": 55000},
            "over_50k": {"replies": 1, "tokens": 120000},
        },
        "by_session": {"to_500": {"sessions": 3, "replies": 56}, "to_50k": {"sessions": 2, "replies": 5}},
        "shrinks": 2,
    },
    "models": {
        "claude-haiku-4-5-20251001": {"replies": 2, "cost_usd": 0.0007, "weekly_usd": {"2026-W38": 0.0007}},
        "claude-sonnet-5": {"replies": 71, "cost_usd": 0.4558, "weekly_usd": {"2026-W38": 0.4558}},
    },
    "returns": {
        "under_150k": {"returns": 1, "context_tokens": 120150, "rewritten_tokens": 50000, "rebuilt": 1, "rewrite_usd": 0.125},
    },
}  # fmt: skip

_MINI_WORLD = {
    "reply_cost": {
        "main": {
            "under_100k": {"replies": 4, "measured_replies": 4, "lookups": 0, "cost_usd": 0.3061, "lookup_usd": 0.0, "sonnet_usd": 0.3061, "lookup_sonnet_usd": 0.0},
        },
        "direct": {
            "under_100k": {"replies": 4, "measured_replies": 4, "lookups": 0, "cost_usd": 0.77025, "lookup_usd": 0.0, "sonnet_usd": 0.1027, "lookup_sonnet_usd": 0.0},
        },
        "workflow": {
            "under_100k": {"replies": 1, "measured_replies": 1, "lookups": 0, "cost_usd": 0.0007, "lookup_usd": 0.0, "sonnet_usd": 0.0007, "lookup_sonnet_usd": 0.0},
        },
    },
    "summaries": {
        "summaries": 3, "auto": 1, "sized": 3, "pre_tokens": 300000, "post_tokens": 60000, "unmeasured": 0,
        "followed_to_next": {"to_10": 2}, "followed_to_end": {"to_10": 1},
        "request_usd": 1.11, "write_usd": 0.3, "read_saving_usd": 0.048, "paid_back": 0,
    },
    "after_summary": {"sessions": 1, "unmeasured": 0, "stretches": {}},
    "models": {
        "claude-opus-4-1": {"replies": 4, "cost_usd": 3.77025, "weekly_usd": {"2026-W38": 3.77025}},
        "claude-sonnet-5": {"replies": 5, "cost_usd": 1.4168, "weekly_usd": {"2026-W38": 1.4168}},
    },
}  # fmt: skip


def test_the_block_gives_the_reference_figures_on_the_fixture(tmp_path):
    """The reference is the designer's script (``expect/figures_block.py``) run on these two worlds; the
    expected dicts are its output, written here. The plan's own hand scripts are not the reference: they
    price no summary request, cap a rise at 60,000 and group projects by name."""
    _close(tuning._figures(_ctx(_make_world(tmp_path / "a"))), _MAKE_WORLD)
    mini = _mini_world(tmp_path / "b")
    _close(tuning._figures(_ctx(mini)), _MINI_WORLD)
    # Without a rate card the same counts, and no amount.
    bare = tuning._figures(_ctx(mini, pricing=False))
    _close(bare["summaries"], {k: v for k, v in _MINI_WORLD["summaries"].items() if not k.endswith("_usd") and k != "paid_back"})
    assert bare["models"] == {"claude-opus-4-1": {"replies": 4}, "claude-sonnet-5": {"replies": 5}}
    _valid(bare)


# -- the summary lines ----------------------------------------------------------------------------------

_SAMPLE_LINES = [
    "Cost per reply, main session: $0.10 from 100K to 200K and $0.15 over 200K.",
    "Cost per reply, direct agents: $0.04 under 100K tokens of context.",
    "Cost per reply, workflow agents: $0.05 from 100K to 200K.",
    "One read-only lookup cost $0.09 a reply in the main session and $0.04 in an agent, or $0.04 and $0.03 at Sonnet's rates.",
    "Context grew by 1,196 tokens a reply on average in the main session, over 460 replies.",
    "1 session grew by over 10,000 tokens a reply on average.",
    "Each summary cost about $1.00 and saved $0.10 on every later reply, so it paid back after about 10 replies.",
    "5 of 6 summaries were followed by that many replies.",
    "1 summary left out of these figures: an older version read their sessions.",
    "After a summary, 1 of 4 next messages read as a correction, against 1 of 20 after a stretch as long without one.",
    "Spend by model: Opus 5.5 $72.25, Opus 5 $9.50 and other models $0.75.",
    "Not used in the file's last 2 weeks: Opus 5.",
    "Cold returns by context: 2 under 150K and 4 to 300K.",
]
_OPENINGS = (
    "Cost per reply", "One read-only lookup", "Context grew", "Each summary cost", "After a summary", "Spend by model",
    "Cold returns by context",
)  # fmt: skip


def _zeros(node):
    """The fullest block the node allows, every count and amount 0."""
    if isinstance(node, (tuning.Count, tuning.Num)):
        return 0
    if isinstance(node, tuning.Obj):
        return {key: _zeros(child) for key, child in node.fields.items()}
    keys = node.keys
    names = sorted(keys.words) if isinstance(keys, tuning.Closed) else [f"2026-W{i:02d}" for i in range(1, 60)]
    return {key: _zeros(node.item) for key in names[: node.limit]}


def test_the_figures_lines_keep_to_the_copy_rules_and_say_nothing_without_the_block():
    lines = tuning.summary_text(_sample()).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Cost per reply"))
    assert lines[start : start + len(_SAMPLE_LINES)] == _SAMPLE_LINES
    assert start > next(i for i, line in enumerate(lines) if line.startswith("Summaries:"))
    for line in _SAMPLE_LINES:
        _plain(line)
    without = _sample()
    del without["figures"]
    text = tuning.summary_text(without)
    assert not any(opening in text for opening in _OPENINGS)
    # A block with growth alone says the one growth line.
    growth = {"by_reply": _sample()["figures"]["growth"]["by_reply"], "by_session": {}, "shrinks": 0}
    alone = tuning.summary_text(_put(_sample(), "figures", {"growth": growth})).splitlines()
    assert [line for line in alone if line.startswith(_OPENINGS)] == [_SAMPLE_LINES[4]]
    # A valid block of zeros, every map full, prints without an error, and only the lines that need no division.
    zeros = _put(_sample(), "figures", _zeros(tuning._FIGURES))
    assert tuning.validate(zeros) == []
    said = [line for line in tuning.summary_text(zeros).splitlines() if line.startswith(_OPENINGS)]
    assert {line.split(":")[0] for line in said} == {"Spend by model", "Cold returns by context"}
    # An older file, a block with a part missing: the part's lines are not printed.
    no_rates = copy.deepcopy(_sample())
    for part in ("reply_cost", "summaries", "models"):
        del no_rates["figures"][part]
    kept = [line for line in tuning.summary_text(no_rates).splitlines() if line.startswith(_OPENINGS)]
    assert [line.split(" ")[0] for line in kept] == ["Context", "After", "Cold"]
