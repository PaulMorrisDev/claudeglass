"""The tuning export's ``figures`` block (K1 group 3): the figures some advice rests on, which no page
shows. Cost per reply, context growth, what a summary cost and saved, corrections after a summary, spend
by model and week, and the cold returns by context.

The shared helpers and worlds are ``test_tuning``'s; the in-memory worlds are built from ``model`` turns
as ``test_measured`` builds them.
"""

from __future__ import annotations

from claudeglass import measured, tuning
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.model import TranscriptResult

from helpers import write_jsonl
from test_tuning import DAYS, TODAY, _big_reply, _human, _note, _notice, _say

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


def _ctx_of(tmp_path, lines) -> tuning._Ctx:
    project = tmp_path / "mini"
    project.mkdir(exist_ok=True)
    write_jsonl(project / "m1.jsonl", lines)
    return tuning._Ctx(load_corpus([project]), Config(tz="UTC"), None, None, DAYS, TODAY, tmp_path)


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
