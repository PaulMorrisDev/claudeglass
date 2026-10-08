"""Characters per token, measured on your own sessions.

Nothing here runs a tokenizer over a transcript (the privacy rule). It
does the next best thing: a subagent's first request is billed in tokens
and its recorded content is measured in characters, so the ratio of the
two, taken over many first calls, says how many characters one token
covers on your sessions. It differs by model (a tool definition is about 4.3
characters per token on Sonnet 5 and 5.8 on Haiku 4.5), and a fixed
``chars / 4`` was wrong by that much in every table that used it.

Two numbers per model family, and nothing else is kept:

- **tool definitions**: the characters of the tool definitions in the
  first tools-bearing prompt snapshot, over the first call's cache-read
  tokens. A subagent's first call reads exactly that shared tool prefix
  from cache, so the two describe the same text.
- **text**: the characters of everything else the transcript records
  before that call (task prompt, CLAUDE.md, skills list, environment
  notes, system prompt, tool lists), over its cache-write plus uncached
  input tokens. Only calls that read a tool prefix count, so the tool
  definitions are not in those tokens.

A family's number is the median over its calls, and only once it has
:data:`MIN_CALLS` of them. With fewer it stays out and :data:`FALLBACK`
(4.0) stands in, so a thin history never invents a precise-looking
figure. A model family is the model id with its date suffix and
``[1m]`` removed (:func:`model_family`); it is not
``habits.family``'s three-way split, because Sonnet 5 and Sonnet 5.5 do
not tokenize alike.

:func:`Calibration.from_calls` is fed one :class:`FirstCall` per
transcript by ``report.py`` before any accumulator runs, and the result
is passed to the four modules that turn characters into tokens
(``context_budget``, ``context_files``, ``topology``, ``tool_search``).
Every one of them starts from an all-fallback ``Calibration()`` (equal to
:data:`DEFAULT`), so a hand-built accumulator behaves as ``chars / 4``
did.

Privacy: a :class:`FirstCall` is five numbers and a model id; a
:class:`Calibration` is two floats per model family.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable

#: The ratio used until a family has :data:`MIN_CALLS` first calls.
FALLBACK = 4.0

#: First calls a family needs before its own ratio replaces :data:`FALLBACK`.
MIN_CALLS = 10

#: What a table's note says about its character-to-token figures.
BASIS = "calibrated on your sessions"
FALLBACK_BASIS = f"assumed at {FALLBACK:g} characters per token, as too few first calls to calibrate on"

_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def model_family(model: str | None) -> str:
    """``model`` less its ``[1m]`` tag and date suffix (``claude-haiku-
    4-5-20251001`` -> ``claude-haiku-4-5``), lower-cased; ``""`` for no
    model or a synthetic one (``<synthetic>``)."""
    if not isinstance(model, str):
        return ""
    name = model.strip().lower()
    if not name or name.startswith("<"):
        return ""
    if name.endswith("[1m]"):
        name = name[:-4]
    return _DATE_SUFFIX_RE.sub("", name)


@dataclass(slots=True)
class FirstCall:
    """What one transcript's first call and its recorded start say."""

    model: str
    #: Characters of the tool definitions in the first snapshot that lists
    #: tools (0 when none was recorded).
    tool_chars: int = 0
    #: Characters of all the other text recorded for the call's start.
    text_chars: int = 0
    #: The call's cache-read tokens: the shared prefix.
    shared_prefix_tokens: int = 0
    #: Its uncached input plus cache-write tokens.
    own_tokens: int = 0


def _ratios(values: list[float]) -> float | None:
    return median(values) if len(values) >= MIN_CALLS else None


@dataclass(slots=True)
class Calibration:
    """Characters per token by model family: the median of the first
    calls it was taken from, for families that had :data:`MIN_CALLS`."""

    #: Family -> characters per token of tool definitions.
    tool: dict[str, float] = field(default_factory=dict)
    #: Family -> characters per token of everything else.
    text: dict[str, float] = field(default_factory=dict)
    #: The family most first calls ran on: stands in for a model that a
    #: caller doesn't know.
    default_family: str = ""

    @classmethod
    def from_calls(cls, calls: Iterable[FirstCall]) -> "Calibration":
        tool: dict[str, list[float]] = {}
        text: dict[str, list[float]] = {}
        seen: dict[str, int] = {}
        for call in calls:
            family = model_family(call.model)
            if not family:
                continue
            seen[family] = seen.get(family, 0) + 1
            if call.shared_prefix_tokens <= 0:
                # A cold start wrote the tool prefix too, so neither
                # ratio can be told apart in its tokens.
                continue
            if call.tool_chars > 0:
                tool.setdefault(family, []).append(call.tool_chars / call.shared_prefix_tokens)
            if call.text_chars > 0 and call.own_tokens > 0:
                text.setdefault(family, []).append(call.text_chars / call.own_tokens)
        found = cls()
        for family, values in tool.items():
            value = _ratios(values)
            if value is not None:
                found.tool[family] = value
        for family, values in text.items():
            value = _ratios(values)
            if value is not None:
                found.text[family] = value
        if seen:
            found.default_family = max(sorted(seen), key=lambda family: seen[family])
        return found

    @property
    def calibrated(self) -> bool:
        """Whether any family has a measured ratio."""
        return bool(self.tool or self.text)

    def tool_chars_per_token(self, model: str | None = None) -> float:
        """Tool definitions' characters per token on ``model`` (the most
        common family when it is ``None``)."""
        return self.tool.get(model_family(model) or self.default_family, FALLBACK)

    def text_chars_per_token(self, model: str | None = None) -> float:
        """Everything else's characters per token on ``model``."""
        return self.text.get(model_family(model) or self.default_family, FALLBACK)

    def tool_tokens(self, chars: float, model: str | None = None) -> float:
        return chars / self.tool_chars_per_token(model)

    def text_tokens(self, chars: float, model: str | None = None) -> float:
        return chars / self.text_chars_per_token(model)

    def basis(self) -> str:
        """The phrase a table note uses for where its token figures
        come from."""
        return BASIS if self.calibrated else FALLBACK_BASIS

    def rows(self) -> list[list]:
        """``[family, tool chars per token, text chars per token]`` per
        calibrated family, most common first; ``None`` where only the
        other is known."""
        families = sorted(
            set(self.tool) | set(self.text),
            key=lambda family: (family != self.default_family, family),
        )
        return [[family, self.tool.get(family), self.text.get(family)] for family in families]

    def to_dict(self) -> dict:
        return {"tool": dict(self.tool), "text": dict(self.text), "default_family": self.default_family}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Calibration":
        found = cls()
        if not isinstance(data, dict):
            return found
        for key, target in (("tool", found.tool), ("text", found.text)):
            values = data.get(key)
            if isinstance(values, dict):
                for family, value in values.items():
                    if isinstance(family, str) and isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                        target[family] = float(value)
        family = data.get("default_family")
        found.default_family = family if isinstance(family, str) else ""
        return found


#: The all-fallback calibration every accumulator starts from.
DEFAULT = Calibration()

__all__ = [
    "BASIS",
    "DEFAULT",
    "FALLBACK",
    "FALLBACK_BASIS",
    "MIN_CALLS",
    "Calibration",
    "FirstCall",
    "model_family",
]
