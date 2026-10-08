"""The eight shipped starting-point profiles (plan "Milestone v0.3":
"Catalogue shipped as starting points, each with the metrics that
justify it"), plus :func:`suggest`, the deterministic archetype/purpose
-> catalogue-id mapping the plan's baseline/``init`` (v0.3, out of this
work package's writable paths) will call once it detects a corpus's
archetype and dominant purposes.

Each ``catalogue/<id>.toml`` is a normal profile document (loaded
through ``schema.load_profile``, so it is validated the same way any
other profile is) whose ``notes`` field names the actual report
table/column this project already computes that justifies the profile's
settings -- never an invented number (see each file's own ``notes``).

Deviation from the plan, reported rather than made silently (see
``model.py``'s module docstring for this project's convention): the
plan's `suggest(archetype, purposes)` signature (Milestone v0.3 bullet)
takes no session *mode* (``classify.classify_mode``'s
overnight/long-agentic/interactive/one-shot/mixed), only archetype and
purpose -- but the ``overnight-batch`` catalogue entry is justified
entirely by *mode* evidence (``classify.classify_mode``'s overnight rule:
Claude worked two hours or more at night while you were away), which has
no archetype or purpose signal of
its own (an overnight session can be any archetype). With the signature
fixed as given, ``suggest`` cannot deterministically reach
``"overnight-batch"`` -- it is reachable only via direct
``catalogue.get("overnight-batch")`` (or a future ``suggest`` revision
that also takes the corpus's mode mix, once that lands as an explicit,
reviewed contract change rather than a guess baked in here). This is
disclosed rather than papered over with a fabricated purpose->id
mapping that would never actually fire for a genuinely overnight corpus.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .. import capture_catalogue
from .schema import Profile, load_profile

__all__ = ["CATALOGUE_IDS", "FOR_TASKS", "list_profiles", "get", "suggest", "task_profile", "tasks_for"]

#: The seven ids the plan names (Milestone v0.3's catalogue bullet), in
#: the order the plan lists them, then ``plan-then-build`` (a way of
#: working :func:`suggest` reaches through its ``shape`` input).
CATALOGUE_IDS: tuple[str, ...] = (
    "interactive-chat",
    "discovery-scrape",
    "planning-requirements",
    "implementation-heavy",
    "overseer-fanout",
    "overnight-batch",
    "workflow-ultracode",
    "plan-then-build",
)


def _catalogue_dir() -> Path:
    return Path(__file__).parent / "catalogue"


def list_profiles() -> list[Profile]:
    """Every shipped catalogue profile, in :data:`CATALOGUE_IDS` order."""
    return [load_profile(_catalogue_dir() / f"{profile_id}.toml") for profile_id in CATALOGUE_IDS]


def get(profile_id: str) -> Profile | None:
    """The shipped catalogue profile named ``profile_id``, or ``None`` if
    ``profile_id`` is not one of :data:`CATALOGUE_IDS`."""
    if profile_id not in CATALOGUE_IDS:
        return None
    return load_profile(_catalogue_dir() / f"{profile_id}.toml")


# -- suggest(): deterministic archetype/purpose -> catalogue id ------------
#
# Printed verbatim (as a table) in docs/profiles.md's "suggest() mapping"
# section -- keep the two in sync by hand when either changes, the same
# convention docs/config-layers.md already follows for snapshots.py.

#: Checked first, in ``purposes`` list order (the caller's own dominant-
#: purpose ordering) -- a purpose is a more specific signal than the bare
#: archetype, so it wins when present. Every entry here is one of the
#: nine purposes ``classify.classify_purpose`` can return.
_PURPOSE_OVERRIDE: dict[str, str] = {
    "local-llm-pipeline": "discovery-scrape",
    "workflow-run": "workflow-ultracode",
    "agent-fanout": "overseer-fanout",
    "refactor": "implementation-heavy",
    "test-triage": "implementation-heavy",
    "review": "implementation-heavy",
    "planning": "planning-requirements",
    "docs-or-light-edit": "interactive-chat",
    "general-dev": "planning-requirements",
}

#: Falls back to this when no purpose in the caller's list matches
#: anything in ``_PURPOSE_OVERRIDE`` above. Every key is one of
#: ``schema.ARCHETYPES``.
_ARCHETYPE_DEFAULT: dict[str, str] = {
    "chat-only": "interactive-chat",
    "single-model": "planning-requirements",
    "effort-varied": "planning-requirements",
    "plan-high-implement-low": "implementation-heavy",
    "overseer-fanout": "overseer-fanout",
    "workflow-heavy": "workflow-ultracode",
    "mixed": "interactive-chat",
}

#: The catalogue id neither table above ever reaches (see module
#: docstring's deviation note) -- ``suggest`` never returns this.
UNREACHABLE_BY_SUGGEST: str = "overnight-batch"

#: Each catalogue profile's ``for`` words, normalised to the kinds of
#: task metrics capture reports (``capture_catalogue.TAG_VOCAB["task"]``).
#: A word missing here (``fanout``, ``overnight-run``, ...) names a way of
#: running, not a kind of task.
FOR_TASKS: dict[str, tuple[str, ...]] = {
    "implementation": ("feature", "bugfix", "debug"),
    "refactor": ("refactor",),
    "test-triage": ("test",),
    "review": ("review",),
    "planning": ("plan",),
    "requirements": ("plan",),
    "architecture": ("plan",),
    "data-exploration": ("research",),
    "web-research": ("research",),
    "database-exploration": ("research",),
    "chat": ("chat",),
    "quick-question": ("chat",),
    "pairing": ("chat",),
    "docs": ("docs",),
    # PROF-11/F11: workflow-ultracode's own "ops" for-word (see its
    # notes) -- the one task word this dict used to leave unmapped.
    "ops": ("ops",),
}

#: Purposes the transcript's own structure decides (a local-LLM pipeline,
#: a workflow run, a fan-out of agents): they win over a reported task,
#: as they do in ``classify.classify_session``.
_STRUCTURAL_PURPOSES = ("local-llm-pipeline", "workflow-run", "agent-fanout")

#: A way of working measured from the sessions themselves -> catalogue
#: id. ``plan-then-build``: at least half the main sessions approved a
#: plan and built it in the same session (``baseline`` reads
#: ``habits.habits_by_shape``).
SHAPE_PROFILES: dict[str, str] = {"plan-then-build": "plan-then-build"}


def tasks_for(profile: Profile) -> tuple[str, ...]:
    """The kinds of task ``profile``'s ``for`` words cover, in order. A
    word that already is a task (a saved task profile's ``for=[task]``)
    stands for itself; a way of running (``fanout``, ...) covers none."""
    vocab = capture_catalogue.TAG_VOCAB["task"]
    return tuple(
        dict.fromkeys(
            task for word in profile.for_ for task in ((word,) if word in vocab else FOR_TASKS.get(word, ()))
        )
    )


@lru_cache(maxsize=1)
def _task_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for profile in list_profiles():
        for task in tasks_for(profile):
            index.setdefault(task, profile.id)
    return index


def task_profile(task: str) -> str | None:
    """The catalogue id whose ``for`` list covers ``task``, or ``None``."""
    return _task_index().get(task)


def suggest(
    archetype: str | None,
    purposes: list[str],
    tasks: list[str] | tuple[str, ...] = (),
    shape: str | None = None,
) -> str:
    """The catalogue id ``id`` (see :data:`CATALOGUE_IDS`) that best
    starts a corpus with workstyle ``archetype`` (one of
    ``schema.ARCHETYPES``, or ``None``/unclassified) whose dominant
    purposes are ``purposes`` (``classify.classify_purpose``'s values,
    most-dominant first). Deterministic: the same ``(archetype,
    purposes)`` pair always returns the same id. Never returns
    :data:`UNREACHABLE_BY_SUGGEST` -- see the module docstring.

    ``tasks``: the kinds of task metrics capture reported, most-dominant
    first. A structural purpose anywhere in ``purposes`` still wins;
    otherwise the first task a catalogue profile's ``for`` list covers
    (:data:`FOR_TASKS`) comes before the purposes, since Claude reported
    it rather than it being guessed. Without ``tasks`` the purposes are
    read in the caller's order, as before.

    ``shape``: a way of working measured from the sessions
    (:data:`SHAPE_PROFILES`). It comes after a structural purpose and
    before the tasks: a plan-then-build corpus's tasks (feature, bugfix)
    would otherwise lead to ``implementation-heavy``, which hands the
    build to a cheaper model."""
    if tasks or shape in SHAPE_PROFILES:
        for purpose in purposes:
            if purpose in _STRUCTURAL_PURPOSES:
                return _PURPOSE_OVERRIDE[purpose]
    if shape in SHAPE_PROFILES:
        return SHAPE_PROFILES[shape]
    if tasks:
        for task in tasks:
            profile_id = task_profile(task)
            if profile_id is not None:
                return profile_id
    for purpose in purposes:
        if purpose in _PURPOSE_OVERRIDE:
            return _PURPOSE_OVERRIDE[purpose]
    return _ARCHETYPE_DEFAULT.get(archetype or "", "interactive-chat")
