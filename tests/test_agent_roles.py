"""The role word an agent's name carries (``agent_roles``): whole tokens from a
closed list, phase before agent type before description, and only the
canonical word comes back."""

from __future__ import annotations

import ast

import pytest

from claudeglass import agent_roles
from claudeglass.agent_roles import DECIDER, FORMS, INTEGRATES, WRITER, role_class, role_word

# The closed list, written out here so a change to it has to change this too.
EXPECTED = {
    "writer": {
        "implement": "implement implements implemented implementing implementation implementations"
                     " implementer implementers implementor impl",
        "fix": "fix fixes fixed fixing fixer fixers fixup",
        "apply": "apply applies applied applying applier",
        "test": "test tests tested testing tester testers",
        "build": "build builds building builder built",
        "write": "write writes writing writer writers wrote",
        "migrate": "migrate migrates migrated migrating migration migrations",
        "refactor": "refactor refactors refactored refactoring",
    },
    "integrates": {
        "integrate": "integrate integrates integrated integrating integration integrator",
    },
    "decider": {
        "review": "review reviews reviewed reviewing reviewer reviewers",
        "verify": "verify verifies verified verifying verification verifier verifiers",
        "refute": "refute refutes refuting refutation",
        "judge": "judge judges judging judgement judgment",
        "decide": "decide decides deciding decision",
        "audit": "audit audits audited auditing auditor",
        "research": "research researcher researching",
        "design": "design designs designing designer",
        "challenge": "challenge challenger challenging",
        "critique": "critique critic critics",
        "synthesise": "synthesise synthesize synthesis synthesising synthesizing synthesiser synthesizer",
        "plan": "plan plans planning planner",
        "find": "find finds finder finders",
        "map": "map maps mapper",
        "assess": "assess assesses assessing assessment",
        "check": "check checks checking checker",
        "completeness": "completeness",
        "explore": "explore explores exploring exploration explorer",
        "investigate": "investigate investigates investigating investigation investigator",
        "inventory": "inventory",
        "baseline": "baseline",
        "adversarial": "adversarial",
        "analyse": "analyse analyze analysis analyst",
    },
}

ALL_FORMS = [
    (form, word, cls)
    for cls, words in EXPECTED.items()
    for word, forms in words.items()
    for form in forms.split()
]
ALL_WORDS = [(word, cls) for cls, words in EXPECTED.items() for word in words]


def test_forms_are_exactly_the_closed_list():
    listed = {form: word for form, word, _ in ALL_FORMS}
    assert len(listed) == len(ALL_FORMS)  # no form stands for two words
    assert FORMS == listed
    assert all(form == form.lower() and form.isascii() and form.isalpha() for form in FORMS)


def test_class_sets_are_the_canonical_words():
    assert WRITER == frozenset(EXPECTED["writer"])
    assert INTEGRATES == frozenset(EXPECTED["integrates"])
    assert DECIDER == frozenset(EXPECTED["decider"])
    assert not WRITER & INTEGRATES and not WRITER & DECIDER and not INTEGRATES & DECIDER
    assert WRITER | INTEGRATES | DECIDER == set(FORMS.values())


@pytest.mark.parametrize("word, cls", ALL_WORDS)
def test_every_canonical_word_maps_to_its_class(word, cls):
    assert role_class(word) == cls


@pytest.mark.parametrize("form, word, cls", ALL_FORMS)
def test_every_form_maps_to_its_canonical_word(form, word, cls):
    assert FORMS[form] == word
    assert role_word(form, None, None) == word
    assert role_word(None, form, None) == word
    assert role_word(None, None, form) == word
    assert role_word(None, None, form.upper()) == word
    assert role_class(role_word(None, None, form)) == cls


@pytest.mark.parametrize("phase, agent_type, description, expected", [
    # Whole tokens only: a longer word that starts with a form is not the form.
    (None, None, "Fixtures audit", "audit"),
    (None, None, "Fixtures", None),
    (None, None, "implications of mapping", None),
    (None, None, "Mapping the hooks", None),
    (None, None, "Testimonial reviewer", "review"),
    # Tokens are runs of letters, so punctuation and digits split them.
    (None, None, "re-fix things", "fix"),
    (None, None, "fix2 later", "fix"),
    (None, None, "impl:C:/Users/x/", "implement"),
    (None, None, "Step 2: Verify", "verify"),
    (None, None, "Adversarial verify", "adversarial"),
    # Within one source the first known token wins.
    (None, None, "review then fix", "review"),
    (None, None, "fix then review", "fix"),
    # Phase beats agent type beats description.
    ("review", "claude-implementer", "fix the thing", "review"),
    (None, "claude-implementer", "audit the thing", "implement"),
    (None, "claude-implementer", None, "implement"),
    ("Plan", "revixo-reviewer", None, "plan"),
    # A source with no known token falls through to the next.
    ("nothing here", "revixo-reviewer", "fix it", "review"),
    ("nothing here", "somebody", "fix it", "fix"),
    ("", "", "fix it", "fix"),
    # Agent types, named and not.
    (None, "claude-implementer", "x", "implement"),
    (None, "revixo-reviewer", "x", "review"),
    (None, "Explore", None, "explore"),
    (None, "Plan", None, "plan"),
    (None, "general-purpose", None, None),
    (None, "workflow-subagent", None, None),
    (None, "claude", None, None),
    (None, "fork", None, None),
    (None, "unknown", None, None),
    # The unnamed types are skipped, so the description still gets its say.
    (None, "workflow-subagent", "Implement the cache", "implement"),
    (None, "general-purpose", "Audit the cache", "audit"),
    # The skip compares the raw value, so another casing is read as a name.
    (None, "Fork", None, None),
    (None, "Fork-fixer", None, "fix"),
    # A description reads only its first four tokens.
    (None, None, "one two three four fix", None),
    (None, None, "one two three fix four", "fix"),
    (None, None, "one, two; three. four? fix", None),
    # A phase and an agent type read every token.
    ("one two three four five fix", None, None, "fix"),
    (None, "one-two-three-four-five-fix", None, "fix"),
    # Nothing at all.
    (None, None, None, None),
    ("", "", "", None),
    ("   ", None, "!!!", None),
])
def test_sources_and_tokens(phase, agent_type, description, expected):
    assert role_word(phase, agent_type, description) == expected


def test_only_the_canonical_word_comes_back():
    result = role_word(None, None, "impl:C:/Users/x/secret.py")
    assert result == "implement"
    assert "secret" not in result and "C:/" not in result
    assert role_word("Implementation of the secret plan", None, None) == "implement"
    assert role_word(None, "Fixer-of-secret-things", None) == "fix"


@pytest.mark.parametrize("value", [123, 1.5, True, b"fix", ["fix"], ("fix",), {"fix": 1}, {"fix"}, object()])
def test_non_string_inputs_count_as_absent(value):
    assert role_word(value, None, None) is None
    assert role_word(None, value, None) is None
    assert role_word(None, None, value) is None
    assert role_word(value, value, value) is None
    # An absent source falls through to the next one.
    assert role_word(value, value, "fix it") == "fix"
    assert role_word(value, "revixo-reviewer", value) == "review"


def test_role_class_of_nothing_and_of_a_non_word():
    assert role_class(None) is None
    assert role_class("nonsense") is None
    assert role_class("") is None
    # Only a canonical word has a class, not another form of it.
    assert role_class("fixes") is None
    assert role_class("Fix") is None
    assert role_class(["fix"]) is None
    assert role_class(5) is None


def test_the_module_imports_nothing_from_claudeglass():
    with open(agent_roles.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name.startswith("claudeglass") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and not (node.module or "").startswith("claudeglass")
