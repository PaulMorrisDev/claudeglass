"""What the dashboard's rating asks of a session, and which sessions are
waiting for one.

The Sessions tab asks the same questions /cg-feedback does
(``capture_catalogue.FEEDBACK_QUESTIONS``), from the same catalogue and
under the same rules: each is asked only when the facts about the session
say it applies. The skill reads those facts from a line the capture hook
writes from the end of a transcript (``cg-fb-facts``); here they are worked
out from the stored transcripts instead, with the same keys
(``capture_catalogue.FEEDBACK_FACT_KEYS``) and the same reading of each:

- ``tokens`` is what the main transcript used (``coaching.session_tokens``)
  and ``typical`` the median piece of work in ``coaching.json``
  (``pieces.corpus_pieces``). The rating is of the whole session, which
  may hold more than one piece of work.
- ``followups`` counts the messages after the first that are not a
  go-ahead or a status check, those you typed while Claude was working
  included (``queued``). A /cg-feedback run is no message.
- ``plan`` is ``approved`` when a plan was approved, by the dialog or by a
  typed go-ahead; ``plan_followups`` counts the follow-ups after that
  approval, ``plan_asked`` is 1 once the plan question was answered after
  it, and ``build`` is ``same`` when files changed after it.
- ``tips`` and ``tip`` name the coaching tips Claude showed in a reply.

A session with two or more approved plans has a set of those plan facts
for each (:func:`session_builds`), and the plan and handoff questions are
asked once for each plan build.

Only counts, flags and ids are worked out: nothing a transcript said is
kept. :func:`unrated_pieces` says which pieces of work in a session the
banner lists: those the capture hook's rating reminder would have spoken
up for (at least the reminder's size) that no /cg-feedback answer covers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime

from . import capture as capture_mod
from . import capture_catalogue as catalogue
from . import coaching, pieces as pieces_mod, prompting
from .model import EventKind

#: The most unrated sessions the banner names.
BANNER_LIMIT = 5


def tokens_text(tokens: int) -> str:
    """``tokens`` as a question or note says it: ``1.3M``, ``420k`` (the
    capture hook's ``_tokens_text``), and under 10,000 a figure that keeps
    a digit: ``2.4k``, ``456``."""
    if tokens >= 999_500:
        return f"{tokens / 1_000_000:.1f}".removesuffix(".0") + "M"
    if tokens >= 10_000:
        return f"{round(tokens / 1000)}k"
    if tokens >= 1000:
        return f"{tokens / 1000:.1f}".removesuffix(".0") + "k"
    return str(tokens)


def _moment(ts: str | None) -> datetime | None:
    return capture_mod._moment(ts)


def _typed_followups(work: list) -> list:
    """The messages after the first that ask for something: no go-ahead,
    no status check."""
    return [c for c in work[1:] if not (c.turns[0].human_go or c.turns[0].human_status)]


def _queued_followups(top, *, dups: bool = False) -> list[datetime | None]:
    """When each message you typed while Claude was working was typed,
    leaving out a go-ahead or a status check and, unless ``dups``, a copy
    of a message also written as a user line (that line counts as a typed
    follow-up; the hook's ``queued`` still counts it as typed while Claude
    worked)."""
    out = []
    for event in top.events:
        if event.kind != EventKind.QUEUE_OPERATION:
            continue
        detail = event.detail
        if detail.get("origin") != "human" or (detail.get("dup") and not dups) or detail.get("go") or detail.get("status"):
            continue
        out.append(_moment(event.ts))
    return out


def session_builds(bundle) -> list[dict]:
    """One dict for each plan approved in the session, in order: the facts
    the plan and handoff questions need, counted between that approval and
    the next (``{"build", "plan_followups", "plan_asked", "built"}``). Empty
    when no plan was approved."""
    top = bundle.top
    if top is None:
        return []
    turns = capture_mod._priced(top)
    approvals = [i for i, turn in enumerate(turns) if capture_mod._plan_approved(turn)]
    if not approvals:
        return []
    cycles = capture_mod.prompt_cycles(top, bundle.subs, getattr(bundle, "workflows", ()))
    typed = _typed_followups([c for c in cycles if not capture_mod.is_feedback_run(c)])
    queued = _queued_followups(top)
    out = []
    for n, at in enumerate(approvals):
        end = approvals[n + 1] if n + 1 < len(approvals) else len(turns)
        after = _moment(turns[at].ts)
        until = _moment(turns[end].ts) if end < len(turns) else None
        follow = sum(1 for c in typed if at < c.start <= end)
        follow += sum(
            1 for when in queued
            if when is not None and after is not None and when > after and (until is None or when <= until)
        )
        window = turns[at + 1:end]
        out.append(
            {
                "build": n + 1,
                "plan_followups": follow,
                "plan_asked": int(any(t.plan_check is not None and t.plan_check.word for t in window)),
                "built": prompting._edited(window),
            }
        )
    return out


def session_facts(bundle, typical: int = 0) -> dict:
    """The facts line's values for one session
    (``capture_catalogue.FEEDBACK_FACT_KEYS``), counts and ids only. The
    plan facts are those of the latest approved plan, as the line has
    them."""
    top = bundle.top
    turns = capture_mod._priced(top)
    cycles = capture_mod.prompt_cycles(top, bundle.subs, getattr(bundle, "workflows", ()))
    work = [c for c in cycles if not capture_mod.is_feedback_run(c)]
    typed = _typed_followups(work)
    queued = _queued_followups(top)
    builds = session_builds(bundle)
    latest = builds[-1] if builds else None
    _notes, shown, _misfires = prompting._tips(top)
    counts = {hint: shown[hint] for hint in catalogue.TIP_HINT_TITLES if shown.get(hint)}
    return {
        "tokens": coaching.session_tokens(top),
        "typical": max(0, int(typical)),
        "followups": len(typed) + len(queued),
        "queued": len(_queued_followups(top, dups=True)),
        "plan": "approved" if latest else "pending" if any(t.plan_stats is not None for t in turns) else "none",
        "plan_followups": latest["plan_followups"] if latest else 0,
        "plan_asked": latest["plan_asked"] if latest else 0,
        "build": "same" if latest and latest["built"] else "none",
        "tips": ",".join(f"{hint}:{n}" for hint, n in counts.items()) or "none",
        "tip": max(counts, key=counts.get) if counts else "none",
        "admits": sum(1 for turn in turns if turn.admit_candidate),
    }


def _count(facts: Mapping[str, object], key: str) -> int:
    value = facts.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def fill_question(q: catalogue.FeedbackQuestion, facts: Mapping[str, object] | None = None) -> str:
    """The question as the skill words it for these facts: ``{n}`` and
    ``{q}`` from the follow-ups, ``{tokens}`` and ``{x}`` from the tokens
    against the typical piece, ``{hint title}`` from the tip. Without the
    numbers a question needs, its ``plain`` wording; a question with
    neither gets its placeholders left out."""
    text = q.question
    if "{" not in text:
        return text
    if q.key == "why" and facts is not None and _count(facts, "followups") >= 1:
        n, queued = _count(facts, "followups"), _count(facts, "queued")
        text = text.replace("{n}", str(n))
        text = text.replace("{q}", str(queued)) if queued else re.sub(r" \([^()]*\{q\}[^()]*\)", "", text)
        return text.replace(" more messages", " more message") if n == 1 else text
    if q.key == "worth" and facts is not None and _count(facts, "tokens") >= 1:
        tokens, typical = _count(facts, "tokens"), _count(facts, "typical")
        text = text.replace("{tokens}", tokens_text(tokens))
        if typical > 0:
            return text.replace("{x}", f"{tokens / typical:.1f}")
        return re.sub(r", about \{x\}\S* your usual piece", "", text)
    if q.key == "tip" and facts is not None and facts.get("tip") in catalogue.TIP_HINT_TITLES:
        return text.replace("{hint title}", catalogue.TIP_HINT_TITLES[str(facts["tip"])])
    if q.plain:
        return q.plain
    return re.sub(r" about \{[^{}]*\}", "", text)


def _option_rows(q: catalogue.FeedbackQuestion) -> list[dict]:
    return [{"word": word, "label": label, "description": text} for word, label, text in q.options]


def _asked(facts: Mapping[str, object] | None, *, plan_approved: bool = False) -> set[str]:
    """The keys of the questions asked for these facts, the missed question
    among them when the follow-ups question is (it waits for a ``missed``
    answer to that one: the page shows it only then)."""
    first, second = catalogue.feedback_questions(facts, ("missed",), plan_approved=plan_approved)
    keys = {q.key for q in (*first, *second)}
    if "why" not in keys:
        keys.discard("missed_in")
    return keys


def question_rows(facts: Mapping[str, object] | None, builds: list[dict] | None = None) -> list[dict]:
    """The questions the Sessions tab's rating shows for a session, in the
    catalogue's order, left out when the facts say they don't apply:
    ``{key, question, multi, options, needs, tip_hint, builds}``.

    ``needs`` is a word the follow-ups answer must hold for the question to
    show (the missed question: ``missed``), else ``""``. ``tip_hint`` is the
    tip the tip question is about. ``builds`` is empty for an ordinary
    question; for the plan and handoff questions of a session with two or
    more approved plans it holds one ``{build, label, question}`` for each
    plan build the question applies to, and the answers are kept for each.
    ``facts`` is ``None`` for a session whose transcript is not stored: the
    plain wording, and only the questions that need no facts."""
    builds = builds or []
    many = len(builds) >= 2
    asked = _asked(facts, plan_approved=bool(builds))
    per_build: dict[str, list[dict]] = {key: [] for key in catalogue.PER_BUILD_KEYS}
    if many and facts is not None:
        for build in builds:
            held = {
                **facts,
                "plan": "approved",
                "plan_followups": build["plan_followups"],
                "plan_asked": build["plan_asked"],
                "build": "same" if build["built"] else "none",
            }
            for key in _asked(held):
                if key in per_build:
                    per_build[key].append(
                        {"build": build["build"], "label": f"Plan {build['build']} of {len(builds)}", "key": key}
                    )
    rows = []
    for q in catalogue.RATING_QUESTIONS:
        row = {
            "key": q.key,
            "question": fill_question(q, facts),
            "multi": q.multi,
            "options": _option_rows(q),
            "needs": "missed" if q.key == "missed_in" else "",
            "tip_hint": str(facts["tip"]) if q.key == "tip" and facts is not None else "",
            "builds": [],
        }
        if q.key in per_build and many and facts is not None:
            if not per_build[q.key]:
                continue
            row["builds"] = [
                {"build": item["build"], "label": item["label"], "question": row["question"]}
                for item in per_build[q.key]
            ]
        elif q.key not in asked:
            continue
        rows.append(row)
    return rows


# -- the sessions the banner lists --------------------------------------------


def coaching_thresholds(config_thresholds: Mapping[str, object] | None, personal: Mapping[str, object]) -> dict:
    """The catalogue's coaching thresholds, then yours from
    ``coaching.json``, then any ``coaching_<key>`` in ``config.toml``'s
    ``[thresholds]``: the hook's own reading, so the banner and the
    reminder agree."""
    out = dict(catalogue.COACHING_THRESHOLDS)
    for source, prefix in ((personal.get("thresholds"), ""), (config_thresholds, "coaching_")):
        if not isinstance(source, Mapping):
            continue
        for key in out:
            value = source.get(prefix + key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                out[key] = value
    return out


def reminder_tokens(thresholds: Mapping[str, float], typical: int) -> int:
    """How many tokens a piece of work needs before it is worth a
    reminder: at least ``rating_min_tokens``, and ``rating_typical_factor``
    times your typical piece when that is more."""
    return int(max(thresholds["rating_min_tokens"], thresholds["rating_typical_factor"] * max(0, typical)))


def piece_label(task: str, messages: int, part: int, parts: int) -> str:
    """How the banner names a piece of work in a session: ``piece 2 of
    3`` when the session holds several, the task word its messages mostly
    had, and how many messages asked for something."""
    words = [f"piece {part} of {parts}"] if parts > 1 else []
    if task:
        words.append(task)
    if messages:
        words.append(f"{messages} message{'' if messages == 1 else 's'}")
    return ", ".join(words)


def unrated_pieces(bundle, threshold: int) -> list[dict]:
    """The pieces of work in a session that the rating reminder would have
    spoken up for and you have not rated: each at least ``threshold`` tokens
    (the main transcript's own, as the hook counts them) and covered by no
    /cg-feedback answer or run (``pieces.WorkPiece``). One dict each, oldest
    first, of counts and words only: ``tokens``, ``end_ts``, ``part``
    (its place in the session, from 1) and ``label``. A piece that carries
    on from an earlier session counts only what ran in this one. A
    transcript with no message of yours in it (a resumed session's
    leftovers) has no piece to draw, so it is one piece whole, as the
    banner always listed such a session."""
    if bundle.top is None:
        return []
    cycles = capture_mod.prompt_cycles(bundle.top, bundle.subs, getattr(bundle, "workflows", ()))
    if not cycles:
        tokens = coaching.session_tokens(bundle.top)
        return [{"tokens": tokens, "end_ts": "", "part": 1, "label": ""}] if tokens >= threshold else []
    found = pieces_mod.pieces_of(cycles, session_id=bundle.session_id)
    return [
        {
            "tokens": piece.tokens,
            "end_ts": piece.end_ts,
            "part": part,
            "label": piece_label(piece.task, piece.substantive + piece.aside_cycles, part, len(found)),
        }
        for part, piece in enumerate(found, start=1)
        if piece.tokens >= threshold and not piece.rated
    ]


__all__ = [
    "BANNER_LIMIT",
    "coaching_thresholds",
    "fill_question",
    "piece_label",
    "question_rows",
    "reminder_tokens",
    "session_builds",
    "session_facts",
    "tokens_text",
    "unrated_pieces",
]
