"""Tests for V3-profiles' ``profiles/schema.py``: the allowlist,
``validate``, the load/dump round trip, and the ``recommend.py``/``ttl.py``
lever-coverage regression (deliverable 1's "add a test that inspects
recommend.py's lever literals and asserts each is representable in a
profile").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from claudeglass import ttl
from claudeglass.profiles.schema import (
    AGENT_ALLOWLIST,
    ARCHETYPES,
    ENV_ALLOWLIST,
    SETTINGS_ALLOWLIST,
    Profile,
    ProfileError,
    dump_profile,
    load_dict,
    load_profile,
    loads_profile,
    recommend_lever_key,
    validate,
)

RECOMMEND_PY = Path(__file__).resolve().parent.parent / "src" / "claudeglass" / "recommend.py"


def _valid_doc(**overrides) -> dict:
    doc = {"id": "sample-profile", "name": "Sample", "for": ["chat"], "archetype": "chat-only"}
    doc.update(overrides)
    return doc


# --------------------------------------------------------------------
# validate(): accept matrix -- one case per allowlisted key
# --------------------------------------------------------------------


def test_validate_accepts_minimal_profile():
    assert validate({"id": "a"}) == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("model", "sonnet"),
        ("effortLevel", "high"),
        ("autoCompactWindow", 150000),
        ("outputStyle", "concise"),
        ("promptCacheTtl", "1h"),
        ("subagentPromptCacheTtl", "5m"),
        ("enabledPlugins", {"my-plugin@market": False}),
        ("skillOverrides", {"pdf": "name-only"}),
        ("disabledMcpjsonServers", ["some-server"]),
        ("enabledMcpjsonServers", ["some-server"]),
        ("alwaysThinkingEnabled", True),
        ("autoCompactEnabled", False),
        ("cleanupPeriodDays", 30),
        ("fastMode", False),  # PROF-08
    ],
)
def test_validate_accepts_every_settings_key(key, value):
    assert set(SETTINGS_ALLOWLIST) >= {key}
    assert validate(_valid_doc(settings={key: value})) == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("model", "sonnet"),
        ("effort", "medium"),
        ("maxTurns", 60),
        ("omitClaudeMd", True),
        ("memory", "project"),
        ("tools", ["Read", "Grep"]),
        ("disallowedTools", ["Bash"]),
        ("skills", ["dataviz"]),
        ("experimental.cacheTtl", "1h"),
    ],
)
def test_validate_accepts_every_agent_key(key, value):
    assert set(AGENT_ALLOWLIST) >= {key}
    assert validate(_valid_doc(agents={"claude-implementer": {key: value}})) == []


@pytest.mark.parametrize("name", sorted(ENV_ALLOWLIST))
def test_validate_accepts_every_env_name(name):
    assert validate(_valid_doc(env={name: "some-value"})) == []


def test_validate_accepts_every_archetype():
    for archetype in ARCHETYPES:
        assert validate(_valid_doc(archetype=archetype)) == []


# --------------------------------------------------------------------
# validate(): reject matrix -- one case per rejection reason
# --------------------------------------------------------------------


def test_validate_rejects_non_dict():
    problems = validate("not-a-dict")
    assert problems and "must be a table/object" in problems[0]


def test_validate_rejects_missing_id():
    problems = validate({})
    assert any(p.startswith("id:") for p in problems)


@pytest.mark.parametrize("bad_id", ["Foo", "has space", "under_score", "a" * 41, ""])
def test_validate_rejects_malformed_id(bad_id):
    problems = validate({"id": bad_id})
    assert any(p.startswith("id:") for p in problems)


def test_validate_rejects_unknown_top_level_key():
    problems = validate(_valid_doc(nonsense="x"))
    assert any(p == "nonsense: unknown top-level key" for p in problems)


def test_validate_rejects_unknown_settings_key():
    problems = validate(_valid_doc(settings={"notARealKey": 1}))
    assert any(p == "settings.notARealKey: unknown key" for p in problems)


def test_validate_accepts_xhigh_effort_level():
    # C3/COV-08: recommend.py, habits.py and helptext.py already use
    # "xhigh" as an effort level (Opus 5.5 supports low/medium/high/
    # xhigh/max, V25); the schema used to reject it.
    assert validate(_valid_doc(settings={"effortLevel": "xhigh"})) == []


def test_validate_accepts_xhigh_agent_effort():
    assert validate(_valid_doc(agents={"claude-implementer": {"effort": "xhigh"}})) == []


def test_validate_rejects_unknown_agent_key():
    problems = validate(_valid_doc(agents={"claude-implementer": {"notARealKey": 1}}))
    assert any(p == "agents.claude-implementer.notARealKey: unknown key" for p in problems)


def test_validate_rejects_unknown_env_name():
    problems = validate(_valid_doc(env={"RANDOM_VAR": "x"}))
    assert any(p == "env.RANDOM_VAR: not an allowed environment variable name" for p in problems)


def test_validate_rejects_unknown_archetype():
    problems = validate(_valid_doc(archetype="not-a-real-archetype"))
    assert any("unknown archetype" in p for p in problems)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("effortLevel", "extreme"),  # not in enum
        ("autoCompactWindow", "not-an-int"),  # wrong type
        ("autoCompactWindow", -1),  # out of range (min 0)
        ("autoCompactWindow", 2_000_000),  # out of range (max 1_000_000)
        ("autoCompactWindow", True),  # bool must not pass as int
        ("promptCacheTtl", "15m"),  # not in enum
        ("enabledPlugins", ["a-list"]),  # wrong type: an object of true/false
        ("skillOverrides", {"pdf": "hidden"}),  # not a visibility state
        ("enabledPlugins", {"p": "yes"}),  # non-bool value
        ("alwaysThinkingEnabled", "yes"),  # wrong type
        ("cleanupPeriodDays", 99999),  # out of range
    ],
)
def test_validate_rejects_bad_settings_values(key, value):
    problems = validate(_valid_doc(settings={key: value}))
    assert any(p.startswith(f"settings.{key}:") for p in problems), problems


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("effort", "extreme"),
        ("maxTurns", 0),  # min is 1
        ("maxTurns", "sixty"),
        ("omitClaudeMd", "true"),  # wrong type
        ("tools", "Read"),  # wrong type
        ("experimental.cacheTtl", "15m"),
    ],
)
def test_validate_rejects_bad_agent_values(key, value):
    problems = validate(_valid_doc(agents={"claude-implementer": {key: value}}))
    assert any(p.startswith(f"agents.claude-implementer.{key}:") for p in problems), problems


def test_validate_rejects_non_string_env_value():
    problems = validate(_valid_doc(env={"MAX_THINKING_TOKENS": 4096}))
    assert any(p == "env.MAX_THINKING_TOKENS: value must be a string, got int" for p in problems)


def test_validate_rejects_wrong_shaped_settings_agents_env():
    problems = validate(_valid_doc(settings=["not", "a", "dict"], agents="nope", env=1))
    assert any(p.startswith("settings:") for p in problems)
    assert any(p.startswith("agents:") for p in problems)
    assert any(p.startswith("env:") for p in problems)


def test_validate_rejects_non_dict_agent_entry():
    problems = validate(_valid_doc(agents={"claude-implementer": "not-a-dict"}))
    assert any(p.startswith("agents.claude-implementer:") for p in problems)


def test_validate_rejects_bad_for_and_name_types():
    problems = validate(_valid_doc(name=123, **{"for": "not-a-list"}))
    assert any(p.startswith("name:") for p in problems)
    assert any(p.startswith("for:") for p in problems)


# --------------------------------------------------------------------
# experimental.cacheTtl: dotted vs nested normalisation
# --------------------------------------------------------------------


def test_dotted_experimental_cache_ttl_is_accepted():
    doc = _valid_doc(agents={"claude-implementer": {"experimental.cacheTtl": "1h"}})
    profile = load_dict(doc)
    assert profile.agents["claude-implementer"]["experimental.cacheTtl"] == "1h"


def test_nested_experimental_table_is_normalised_to_dotted():
    doc = _valid_doc(agents={"claude-implementer": {"experimental": {"cacheTtl": "1h"}}})
    profile = load_dict(doc)
    assert profile.agents["claude-implementer"] == {"experimental.cacheTtl": "1h"}


def test_nested_and_dotted_agree_is_accepted():
    doc = _valid_doc(
        agents={"claude-implementer": {"experimental.cacheTtl": "1h", "experimental": {"cacheTtl": "1h"}}}
    )
    profile = load_dict(doc)
    assert profile.agents["claude-implementer"] == {"experimental.cacheTtl": "1h"}


def test_nested_and_dotted_disagree_is_rejected():
    doc = _valid_doc(
        agents={"claude-implementer": {"experimental.cacheTtl": "1h", "experimental": {"cacheTtl": "5m"}}}
    )
    problems = validate(doc)
    assert any("both" in p and "experimental.cacheTtl" in p for p in problems)


def test_nested_experimental_table_rejects_unknown_key():
    doc = _valid_doc(agents={"claude-implementer": {"experimental": {"notCacheTtl": "1h"}}})
    problems = validate(doc)
    assert any("experimental" in p and "unknown key" in p for p in problems)


def test_nested_experimental_table_wrong_type_is_rejected():
    doc = _valid_doc(agents={"claude-implementer": {"experimental": "not-a-table"}})
    problems = validate(doc)
    assert any(p.startswith("agents.claude-implementer.experimental:") for p in problems)


# --------------------------------------------------------------------
# load_dict / loads_profile / load_profile / ProfileError
# --------------------------------------------------------------------


def test_load_dict_raises_profile_error_with_every_problem():
    with pytest.raises(ProfileError) as exc_info:
        load_dict({"id": "Bad Id", "settings": {"nope": 1}, "env": {"NOPE": "x"}})
    problems = exc_info.value.problems
    assert any(p.startswith("id:") for p in problems)
    assert any(p.startswith("settings.nope:") for p in problems)
    assert any(p.startswith("env.NOPE:") for p in problems)


def test_load_dict_builds_expected_profile():
    doc = _valid_doc(settings={"effortLevel": "high"}, notes="why")
    profile = load_dict(doc)
    assert profile == Profile(
        id="sample-profile",
        name="Sample",
        for_=("chat",),
        archetype="chat-only",
        settings={"effortLevel": "high"},
        agents={},
        env={},
        notes="why",
    )
    assert profile.source_path is None


def test_loads_profile_parses_toml_text():
    text = 'id = "from-text"\n[settings]\neffortLevel = "low"\n'
    profile = loads_profile(text)
    assert profile.id == "from-text"
    assert profile.settings == {"effortLevel": "low"}


def test_load_profile_sets_source_path(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text('id = "from-file"\n', encoding="utf-8")
    profile = load_profile(path)
    assert profile.id == "from-file"
    assert profile.source_path == str(path)


def test_source_path_is_excluded_from_equality(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text('id = "x"\n', encoding="utf-8")
    from_file = load_profile(path)
    from_dict = load_dict({"id": "x"})
    assert from_file.source_path == str(path)
    assert from_dict.source_path is None
    assert from_file == from_dict


# --------------------------------------------------------------------
# dump_profile: never exports source_path; round-trips
# --------------------------------------------------------------------


def test_dump_profile_never_writes_source_path(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text('id = "x"\nnotes = "n"\n', encoding="utf-8")
    profile = load_profile(path)
    text = dump_profile(profile)
    assert str(path) not in text
    assert "source_path" not in text


def test_dump_profile_round_trips_every_field_kind():
    doc = {
        "id": "full-profile",
        "name": "Full profile",
        "for": ["implementation", "refactor"],
        "archetype": "plan-high-implement-low",
        "notes": 'multi\nline "quoted" notes',
        "settings": {
            "model": "sonnet",
            "effortLevel": "medium",
            "autoCompactWindow": 150000,
            "outputStyle": "concise",
            "promptCacheTtl": "5m",
            "subagentPromptCacheTtl": "1h",
            "enabledPlugins": {"a@m": True, "b@m": False},
            "disabledMcpjsonServers": ["srv"],
            "enabledMcpjsonServers": ["srv2"],
            "alwaysThinkingEnabled": True,
            "autoCompactEnabled": False,
            "cleanupPeriodDays": 14,
        },
        "agents": {
            "claude-implementer": {
                "model": "sonnet",
                "effort": "medium",
                "maxTurns": 60,
                "omitClaudeMd": False,
                "memory": "project",
                "tools": ["Read", "Edit"],
                "disallowedTools": ["Bash"],
                "skills": ["dataviz"],
                "experimental.cacheTtl": "5m",
            }
        },
        "env": {
            "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "5m",
            "MAX_THINKING_TOKENS": "4096",
        },
    }
    profile = load_dict(doc)
    dumped = dump_profile(profile)
    reloaded = loads_profile(dumped)
    assert reloaded == profile


def test_dump_profile_omits_empty_sections():
    profile = load_dict({"id": "bare"})
    text = dump_profile(profile)
    assert "[settings]" not in text
    assert "[agents" not in text
    assert "[env]" not in text
    assert text == 'id = "bare"\n'


def test_dump_profile_is_deterministic_regardless_of_dict_insertion_order():
    doc_a = _valid_doc(settings={"model": "sonnet", "effortLevel": "high"})
    doc_b = _valid_doc(settings={"effortLevel": "high", "model": "sonnet"})
    assert dump_profile(load_dict(doc_a)) == dump_profile(load_dict(doc_b))


# --------------------------------------------------------------------
# recommend.py / ttl.py lever coverage
# --------------------------------------------------------------------

#: Literal ``lever="..."``/``lever = "..."`` string constants passed to a
#: ``Recommendation(...)`` call inside recommend.py -- a plain regex scan
#: of the source, not an AST walk, matching this file's own preference
#: for a test that fails loudly (a new lever literal appearing in
#: recommend.py that this regex somehow can't see would still be caught
#: by ``test_every_settings_and_agent_lever_key_is_reachable`` below,
#: which walks the allowlist the other way).
_LEVER_LITERAL_RE = re.compile(r'lever\s*=\s*"([^"]+)"')


def _recommend_py_lever_literals() -> set[str]:
    source = RECOMMEND_PY.read_text(encoding="utf-8")
    return set(_LEVER_LITERAL_RE.findall(source))


def test_recommend_py_lever_literals_are_the_expected_set():
    """Pins the exact set of bare ``lever="..."`` literals recommend.py's
    source currently contains, so a newly added rule with a lever this
    test (and ``RECOMMEND_LEVER_MAP``) doesn't yet know about fails here
    first, loudly, instead of silently shipping an unrepresentable lever.

    COV-09 added four of these (recommend.py's env-lever rules): three
    ``"env:NAME"``-prefixed literals the regex can see directly, plus
    ``"includeCoAuthoredBy"``. A fifth env-lever rule
    (``env-disable-prompt-caching``) picks its own lever name dynamically
    at runtime (``f"env:{name}"`` for whichever ``DISABLE_PROMPT_CACHING*``
    variant is actually set) and so has no fixed literal for this
    source-regex scan to find at all -- see
    ``test_every_recommend_py_lever_literal_is_representable`` below,
    which is parametrized over this same set and would need a direct
    call to catch that one, exercised instead by
    ``tests/test_recommend.py``'s own env-disable-prompt-caching tests.

    COV-01 removed ``"effortLevel"`` from this set: the two effort-
    mismatch rules used to construct ``Recommendation(lever="effortLevel",
    ...)`` directly, a bare literal this scan could see, but now resolve
    their scope first via ``_lever_scope("effortLevel", snapshot)`` and
    pass the result along as a variable (``lever=lever``) -- same
    "evades the static scan" situation as the dynamic env-lever rule
    above, so it gets the same direct-call treatment in
    ``test_effort_level_lever_is_representable`` below instead.
    """
    assert _recommend_py_lever_literals() == {
        "omitClaudeMd",
        "env:ENABLE_TOOL_SEARCH",
        "env:CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "env:CLAUDE_CODE_SUBAGENT_MODEL",
        "includeCoAuthoredBy",
    }


@pytest.mark.parametrize("lever", sorted(_recommend_py_lever_literals()))
def test_every_recommend_py_lever_literal_is_representable(lever):
    resolved = recommend_lever_key(lever)
    assert resolved is not None, f"lever {lever!r} has no profile representation"
    scope_kind, key = resolved
    if scope_kind == "settings":
        assert key in SETTINGS_ALLOWLIST, f"lever {lever!r} maps to unknown settings key {key!r}"
    elif scope_kind == "agent frontmatter":
        assert key in AGENT_ALLOWLIST, f"lever {lever!r} maps to unknown agent key {key!r}"
    else:
        # COV-09: an "env:NAME"-prefixed lever.
        assert scope_kind == "env" and key in ENV_ALLOWLIST


def test_env_disable_prompt_caching_dynamic_lever_is_representable():
    """The one env-lever rule this file's regex-based scan can't see (its
    lever name is chosen at runtime, not a fixed literal -- see the
    docstring above) -- checked directly against every name it could
    possibly emit, so it isn't silently unrepresentable just because it
    evades the static scan.
    """
    for name in (
        "DISABLE_PROMPT_CACHING",
        "DISABLE_PROMPT_CACHING_SONNET",
        "DISABLE_PROMPT_CACHING_OPUS",
        "DISABLE_PROMPT_CACHING_HAIKU",
        "DISABLE_PROMPT_CACHING_FABLE",
    ):
        resolved = recommend_lever_key(f"env:{name}")
        assert resolved == ("env", name)
        assert resolved[1] in ENV_ALLOWLIST


def test_effort_level_lever_is_representable():
    """COV-01: ``"effortLevel"`` no longer appears as a bare ``lever="..."``
    literal in recommend.py (see the docstring above), so it evades
    ``_recommend_py_lever_literals``'s static scan -- checked directly
    instead, same as the dynamic env-lever rule just above."""
    resolved = recommend_lever_key("effortLevel")
    assert resolved == ("settings", "effortLevel")
    assert resolved[1] in SETTINGS_ALLOWLIST


def test_ttl_top_level_lever_is_representable():
    """``ttl.TtlTypeStats.lever`` for the ``"top-level"`` row is the other
    lever ``recommend.py``'s ttl-switch rule can emit (via
    ``row[lever_idx]``, not a literal in recommend.py's own source --
    see that rule's docstring) -- exercised here against the real
    property rather than a hand-copied string, so a wording change in
    ttl.py is caught immediately.
    """
    stats = _minimal_ttl_stats(key="top-level")
    lever = stats.lever
    assert lever == "promptCacheTtl"
    resolved = recommend_lever_key(lever)
    assert resolved == ("settings", "promptCacheTtl")
    assert resolved[1] in SETTINGS_ALLOWLIST


def test_ttl_per_agent_lever_is_representable():
    stats = _minimal_ttl_stats(key="claude-implementer")
    lever = stats.lever
    assert "experimental.cacheTtl in claude-implementer.md" in lever
    resolved = recommend_lever_key(lever)
    assert resolved == ("agent frontmatter", "experimental.cacheTtl")
    assert resolved[1] in AGENT_ALLOWLIST


def test_lever_none_is_not_representable_by_design():
    # A `lever=None` Recommendation carries pure workflow advice with no
    # settings/frontmatter lever to name -- recommend_lever_key has
    # nothing to resolve for it, by design (see recommend.py's own
    # category="workflow" rules, e.g. agent-report-size/discovery-share).
    assert recommend_lever_key("this is not a real lever string") is None


def _minimal_ttl_stats(*, key: str) -> "ttl.TtlTypeStats":
    return ttl.TtlTypeStats(
        key=key,
        spawns=1,
        priced_turns=1,
        observed_5m_pct=100.0,
        observed_1h_pct=0.0,
        gaps_over_5m=0,
        gaps_over_1h=0,
        gap_p50_s=1.0,
        gap_p90_s=1.0,
        cost_observed=1.0,
        cost_all_5m=1.0,
        cost_all_1h=1.0,
        unsimulatable=0,
        fidelity_pct=0.0,
        gap_buckets={},
    )
