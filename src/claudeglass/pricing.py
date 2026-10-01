"""Rate-card loading, model-id resolution and per-turn pricing.

This is WP2 of the project plan (see plan Appendix A3 for the
``pricing.toml`` shape and the "Pricing" section for the resolution and
costing rules). It depends only on ``model.py`` (frozen contract) and the
standard library.

Three things live here:

- :func:`load_pricing` reads a rate card (explicit path, user config dir,
  or the packaged default) into a :class:`Pricing` instance.
- :meth:`Pricing.resolve_model` maps an observed ``Turn.model`` string to
  a registered rate card entry, handling aliases, the ``[1m]`` context-
  window suffix, and cloud-provider prefix/suffix forms.
- :func:`price_turn` prices one turn (or a simulated variant of one, for
  the TTL package) against a resolved rate, producing a
  :class:`~claudeglass.model.CostBreakdown`.

:class:`PricingCoverage` is a small accumulator later report code uses to
track how much of the corpus was actually priced, for the "unknown
model" table and the ``pricing-coverage`` recommendation. It also tracks
three narrower cases of "priced, but only approximately" or "priced,
worth a second look": turns priced by closest (prefix) match
(``ResolvedRates.approximate`` — see ``Pricing.resolve_model``) rather
than their own rate-card row, fast-flagged turns priced at a model's
standard rate for lack of a ``[.fast]`` table (see :class:`FastRule` and
``price_turn``), and — PROF-08, the mirror image of that last one —
turns actually priced at a fast-mode rate, alongside what they'd have
cost standard: ``whatif._fast_mode`` reads this last one to price
turning ``fastMode`` off.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .model import Column, CostBreakdown, Table, Turn

#: Directory name under the resolved Claude config root that holds
#: claudeglass's own files (pricing overrides, snapshots, profiles,
#: usage log). Mirrors the plan's ``--config-dir`` default.
CLAUDEGLASS_DIRNAME = "claudeglass"

#: Model ids that never carry a real price and must resolve to ``None``
#: without being recorded as a pricing-coverage warning: synthetic error
#: lines carry no real usage, and an empty/missing model string means the
#: line never reached a model at all.
_NO_WARNING_MODEL_IDS = frozenset({"", "<synthetic>"})

_RATE_FIELDS: tuple[str, ...] = (
    "input",
    "output",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
)

#: Cloud-provider id prefixes stripped before a longest-prefix match,
#: e.g. Bedrock's ``us.anthropic.claude-opus-4-8`` -> ``claude-opus-4-8``.
_CLOUD_PREFIXES: tuple[str, ...] = ("us.anthropic.", "eu.anthropic.", "anthropic.")

#: Bedrock's trailing version suffix, e.g. ``...-v1:0``.
_BEDROCK_SUFFIX = "-v1:0"

#: Vertex's trailing ``@YYYYMMDD`` date suffix, e.g.
#: ``claude-sonnet-4-5@20250929``.
_VERTEX_DATE_SUFFIX_RE = re.compile(r"@\d{8}$")

#: The ``[1m]`` extended-context-window alias suffix, e.g. ``fable[1m]``.
_CONTEXT_WINDOW_SUFFIX = "[1m]"

#: A model's context window when its TOML entry sets no
#: ``context_window_tokens`` -- the pre-Claude-5 standard (D2/COV-12).
_DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000


class PricingError(Exception):
    """A pricing file could not be read or its structure is invalid.

    The CLI maps this to exit code 2 with the message unchanged, so every
    message here is written to stand alone as user-facing output.
    """


@dataclass(slots=True)
class LongContextRule:
    """A model's optional long-context pricing tier: absent unless a
    model's pricing page documents a surcharge above ``threshold_tokens``
    (the docs currently say 4.6+ models do not, so this is normally
    unset). ``overrides`` (explicit per-rate values) wins over
    ``multiplier`` when both are present.
    """

    threshold_tokens: int
    multiplier: float | None = None
    #: subset of {"input", "output", "cache_write_5m", "cache_write_1h",
    #: "cache_read"} -> explicit replacement rate.
    overrides: dict[str, float] | None = None


@dataclass(slots=True)
class FastRule:
    """A model's optional fast-mode rate: a flat multiplier over every
    one of the five standard rates, applied when a turn's own
    ``usage.speed == "fast"``. Absent unless a model's pricing page
    documents a fast tier — a fast-flagged turn on a model with no
    ``FastRule`` is priced at its standard rate (see ``price_turn`` and
    ``PricingCoverage.fast_priced_as_standard``).
    """

    multiplier: float


@dataclass(slots=True)
class ModelRates:
    """One ``[models."<id>"]`` entry: per-million-token USD rates plus
    optional geo multipliers, a long-context rule, and a fast-mode rule.
    """

    canonical_id: str
    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    #: geo string (e.g. "us") -> multiplier applied to all four cost
    #: components when ``price_turn``'s ``geo`` argument matches.
    geo_multipliers: dict[str, float] = field(default_factory=dict)
    long_context: LongContextRule | None = None
    fast: FastRule | None = None
    #: This model's native context-window size (D2/D4/COV-12): the
    #: single source of truth every "is this turn near the window"
    #: check should resolve against, instead of assuming the pre-Claude-5
    #: 200,000-token window applies everywhere. Defaults to
    #: ``_DEFAULT_CONTEXT_WINDOW_TOKENS`` (200,000) when the TOML entry
    #: doesn't set ``context_window_tokens``; the packaged file sets
    #: 1,000,000 explicitly for the models the docs list as natively 1M
    #: (Fable 5.1, Fable 5, Sonnet 5.5, Sonnet 5, Sonnet 4.6, and Opus
    #: 4.6 and later -- V24). Claude Code itself compacts a 1M-window
    #: session before the window fills, at about 967K tokens by default
    #: (V24) -- this field is the raw window, not that trigger point; a
    #: caller wanting the trigger should treat it as roughly 0.967x this
    #: value when it matters.
    context_window_tokens: int = _DEFAULT_CONTEXT_WINDOW_TOKENS


@dataclass(slots=True)
class ResolvedRates:
    """The result of resolving an observed ``Turn.model`` string against
    a :class:`Pricing` rate card.
    """

    canonical_id: str
    rates: ModelRates
    #: "exact" | "alias" | "strip_1m" | "cloud_strip" | "prefix"
    matched_via: str = "exact"
    #: True when this is only an approximation of the observed model's
    #: real price: resolution bottomed out at the longest-registered-id
    #: *prefix* match (``matched_via in {"prefix", "cloud_strip"}`` when
    #: the cloud strip itself didn't land on an exact/alias id), not the
    #: model's own rate-card row. False for "exact"/"alias"/"strip_1m"
    #: and for a "cloud_strip" that resolved straight to an exact/alias
    #: id — ``matched_via`` alone can't tell those two "cloud_strip"
    #: cases apart, which is why this is a separate field rather than a
    #: new ``matched_via`` value (see ``resolve_model``).
    approximate: bool = False
    #: The rate card's ``[server_tools].web_search_per_1000`` at
    #: resolution time (0.0 if absent). Carried here — rather than
    #: threaded as a separate argument through every ``price_turn``
    #: call site — because it isn't model-specific: every resolution
    #: from the same :class:`Pricing` gets the same value, and
    #: ``price_turn`` already receives this object.
    web_search_per_1000: float = 0.0
    #: True only on a prefix (``approximate``) match whose leftover is a
    #: bare minor version (``claude-sonnet-5-5`` priced as
    #: ``claude-sonnet-5``): a newer release of the matched model that
    #: the rate card has no row for yet. False for a dated, ``-preview``
    #: or ``-0`` leftover (see :func:`newer_version_of`, which applies
    #: the same test to two ids after the fact).
    newer_version: bool = False


@dataclass(slots=True)
class Pricing:
    """A loaded rate card: provenance plus the resolved model table."""

    path: str
    version: str
    currency: str
    source_url: str | None
    retrieved: str | None
    notes: str | None
    sha256: str
    models: dict[str, ModelRates] = field(default_factory=dict)
    #: alias string (including "[1m]" forms shipped as explicit aliases,
    #: e.g. "fable[1m]") -> canonical model id.
    aliases: dict[str, str] = field(default_factory=dict)
    #: optional [server_tools] rates, e.g. "web_search_per_1000".
    server_tools: dict[str, float] = field(default_factory=dict)
    #: :meth:`resolve_model` results by model string: one report asks
    #: about the same handful of strings millions of times. A rate card
    #: is never changed once loaded; ``dataclasses.replace`` of one
    #: starts an empty cache.
    _resolved: dict[str | None, ResolvedRates | None] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    @property
    def sha8(self) -> str:
        """First 8 hex characters of the file's sha256, for compact
        provenance display (report header, ``pricing-check``)."""
        return self.sha256[:8]

    def resolve_model(self, model_id: str | None) -> ResolvedRates | None:
        """Resolve an observed ``Turn.model`` string to a rate-card
        entry.

        Order: exact id -> alias -> strip a trailing ``[1m]`` -> strip
        cloud-provider prefixes/suffixes -> longest registered-id prefix
        match -> ``None``. ``<synthetic>`` and empty/``None`` return
        ``None`` immediately and are never treated as an unknown-model
        warning by callers (synthetic lines carry no billable usage).

        The prefix-match step requires a token boundary right after the
        matched prefix (the next character must be ``-``, ``@``, or end
        of string) so ``claude-opus-4-10-x`` — a real (hypothetical)
        model whose id happens to start with the characters of
        ``claude-opus-4-1`` — never falsely resolves to that shorter,
        unrelated registered id.

        When the cloud-provider strip actually changed the candidate
        (Bedrock/Vertex wrapping was present) and resolution only
        succeeds via the prefix step afterwards, ``matched_via`` is still
        reported as ``"cloud_strip"`` — the cloud wrapping is the
        substantive fact about how this id was resolved, and a caller
        reading ``matched_via`` shouldn't have to also inspect the raw id
        to notice a Bedrock/Vertex form was involved.
        """
        try:
            return self._resolved[model_id]
        except KeyError:
            resolved = self._resolved[model_id] = self._resolve_uncached(model_id)
            return resolved

    def _resolve_uncached(self, model_id: str | None) -> ResolvedRates | None:
        if not model_id or model_id in _NO_WARNING_MODEL_IDS:
            return None

        web_search_per_1000 = self.server_tools.get("web_search_per_1000", 0.0)

        exact = self._lookup_exact_or_alias(model_id)
        if exact is not None:
            canonical, matched_via = exact
            return ResolvedRates(
                canonical, self.models[canonical], matched_via,
                web_search_per_1000=web_search_per_1000,
            )

        candidate = model_id
        if candidate.endswith(_CONTEXT_WINDOW_SUFFIX):
            stripped = candidate[: -len(_CONTEXT_WINDOW_SUFFIX)]
            hit = self._lookup_exact_or_alias(stripped)
            if hit is not None:
                canonical, _ = hit
                return ResolvedRates(
                    canonical, self.models[canonical], "strip_1m",
                    web_search_per_1000=web_search_per_1000,
                )
            candidate = stripped

        cleaned = _strip_cloud_provider(candidate)
        cloud_stripped = cleaned != candidate
        if cloud_stripped:
            hit = self._lookup_exact_or_alias(cleaned)
            if hit is not None:
                canonical, _ = hit
                return ResolvedRates(
                    canonical, self.models[canonical], "cloud_strip",
                    web_search_per_1000=web_search_per_1000,
                )

        best_id: str | None = None
        for canonical_id in self.models:
            if _prefix_boundary_match(cleaned, canonical_id):
                if best_id is None or len(canonical_id) > len(best_id):
                    best_id = canonical_id
        if best_id is not None:
            matched_via = "cloud_strip" if cloud_stripped else "prefix"
            return ResolvedRates(
                best_id, self.models[best_id], matched_via, approximate=True,
                web_search_per_1000=web_search_per_1000,
                newer_version=bool(_NEWER_VERSION_RE.match(cleaned[len(best_id) :])),
            )

        return None

    def _lookup_exact_or_alias(self, model_id: str) -> tuple[str, str] | None:
        if model_id in self.models:
            return model_id, "exact"
        canonical = self.aliases.get(model_id)
        if canonical is not None:
            return canonical, "alias"
        return None

    def describe(self) -> Table:
        """A ``Table`` listing every registered model's resolved rates,
        for the report header and the ``pricing-check`` subcommand."""
        columns = [
            Column(key="model_id", label="Model", kind="str"),
            Column(key="aliases", label="Aliases", kind="str"),
            Column(key="input", label="Input", kind="money"),
            Column(key="output", label="Output", kind="money"),
            Column(key="cache_write_5m", label="Cache write (5m)", kind="money"),
            Column(key="cache_write_1h", label="Cache write (1h)", kind="money"),
            Column(key="cache_read", label="Cache read", kind="money"),
        ]
        aliases_by_model: dict[str, list[str]] = {}
        for alias, canonical in self.aliases.items():
            aliases_by_model.setdefault(canonical, []).append(alias)
        rows = []
        for model_id in sorted(self.models):
            rates = self.models[model_id]
            aliases = ", ".join(sorted(aliases_by_model.get(model_id, [])))
            rows.append(
                [
                    model_id,
                    aliases,
                    rates.input,
                    rates.output,
                    rates.cache_write_5m,
                    rates.cache_write_1h,
                    rates.cache_read,
                ]
            )
        return Table(
            name="pricing_rates",
            title="Resolved model rates (USD per million tokens)",
            columns=columns,
            rows=rows,
        )

    def rates_meta(self) -> dict:
        """Every priced model's own rates plus derived ratios, for
        ``report.json``'s additive ``meta.rates`` -- the dashboard's own
        rate card, without a second round trip to read ``pricing.toml``
        itself. Keyed by canonical model id; only models this rate card
        prices (an observed-but-unregistered model, per
        :meth:`resolve_model`'s "unknown model" case, is never a key
        here).

        Each entry carries the five per-million-token rates
        ``pricing.toml`` itself names (``input``, ``output``,
        ``cache_write_5m``, ``cache_write_1h``, ``cache_read``), three
        ratios against that model's own ``input`` rate
        (``cache_read_ratio``, ``cache_write_5m_ratio``,
        ``cache_write_1h_ratio`` -- ``None`` for the pathological case of
        a model priced at zero input), and ``input_ratio_to``: this
        model's ``input`` rate as a ratio of every other priced model's
        (cheap at the model counts a rate card actually has, so always
        included rather than gated behind a size check).
        """
        out: dict[str, dict] = {}
        for model_id, rates in self.models.items():
            entry = {
                "input": rates.input,
                "output": rates.output,
                "cache_write_5m": rates.cache_write_5m,
                "cache_write_1h": rates.cache_write_1h,
                "cache_read": rates.cache_read,
                "cache_read_ratio": (rates.cache_read / rates.input) if rates.input else None,
                "cache_write_5m_ratio": (rates.cache_write_5m / rates.input) if rates.input else None,
                "cache_write_1h_ratio": (rates.cache_write_1h / rates.input) if rates.input else None,
                "input_ratio_to": {
                    other_id: rates.input / other_rates.input
                    for other_id, other_rates in self.models.items()
                    if other_id != model_id and other_rates.input
                },
            }
            out[model_id] = entry
        return out

    def model_ids_meta(self, observed: Iterable[str] = ()) -> dict[str, str]:
        """Which canonical id each other name for a model is priced as,
        for ``report.json``'s additive ``meta.model_ids``: every alias
        this rate card ships (``"sonnet"`` -> ``"claude-sonnet-5-5"``),
        plus every ``observed`` id (the report window's ``by_model``
        keys) that :meth:`resolve_model` lands on a different canonical
        id (a dated, ``[1m]``, Bedrock/Vertex or newer-version id). The
        dashboard reads it to find an observed id's own entry in
        ``meta.rates``, which is keyed by canonical id only. A canonical
        id maps to nothing (it is already a ``meta.rates`` key), and an
        id this rate card can't price (``"<unknown>"``) is left out.
        """
        out = dict(self.aliases)
        for model_id in observed:
            if not isinstance(model_id, str) or not model_id or model_id in self.models:
                continue
            resolved = self.resolve_model(model_id)
            if resolved is not None:
                out[model_id] = resolved.canonical_id
        return out


#: Characters allowed to immediately follow a matched registered-id
#: prefix for the match to count (see ``_prefix_boundary_match``).
_PREFIX_BOUNDARY_CHARS = ("-", "@")


def _prefix_boundary_match(cleaned: str, canonical_id: str) -> bool:
    """Does ``cleaned`` start with ``canonical_id`` at a real token
    boundary? The character immediately after the matched prefix must be
    ``-``, ``@``, or nothing (the strings are equal) — otherwise
    ``cleaned`` merely shares a numeric run with ``canonical_id``
    (``claude-opus-4-10`` sharing ``claude-opus-4-1``'s digits) rather
    than actually being a dated/suffixed variant of it.
    """
    if not cleaned.startswith(canonical_id):
        return False
    rest = cleaned[len(canonical_id) :]
    return rest == "" or rest[0] in _PREFIX_BOUNDARY_CHARS


#: The leftover after a prefix match that marks a newer release of the
#: matched model: a bare 1-2 digit minor version (``-5`` in
#: ``claude-sonnet-5-5`` on ``claude-sonnet-5``, ``-10`` in
#: ``claude-opus-4-10``), alone or before a further ``-``/``@`` suffix.
#: Never ``-0`` (``claude-opus-4-0`` is an alias of ``claude-opus-4``),
#: an 8-digit date or ``-preview``.
_NEWER_VERSION_RE = re.compile(r"-(?:[1-9]\d?)(?=$|[-@])")


def newer_version_of(model_id: str, priced_as: str) -> bool:
    """Is ``model_id`` a newer release of ``priced_as`` that the rate
    card has no row for yet, so it was priced at that older model's
    rate? The same test :meth:`Pricing.resolve_model` applies for
    ``ResolvedRates.newer_version``, on two plain strings, so a reader
    of the ``pricing_closest_match`` table (``model_id``, ``priced_as``)
    can tell the two kinds of closest match apart: ``[1m]`` and
    Bedrock/Vertex wrapping are stripped first, then ``priced_as`` must
    be a token-boundary prefix and the rest must start with a bare minor
    version. True for ``claude-sonnet-5-5`` (or its dated and cloud
    forms) on ``claude-sonnet-5``; False for ``claude-sonnet-5-20261001``,
    ``claude-opus-4-1-preview`` on ``claude-opus-4-1``,
    ``claude-opus-4-0`` and an exact id.
    """
    return newer_version_id(model_id, priced_as) is not None


def newer_version_id(model_id: str, priced_as: str) -> str | None:
    """The newer release ``model_id`` names when :func:`newer_version_of`
    holds: ``priced_as`` plus that minor version, with ``[1m]``,
    Bedrock/Vertex wrapping and any later suffix dropped
    (``claude-opus-5-7`` for ``claude-opus-5-7-20261101`` or
    ``us.anthropic.claude-opus-5-7-v1:0`` priced as ``claude-opus-5``).
    ``None`` otherwise. Lets a caller that compares ids by canonical id
    keep apart two releases the rate card prices alike for now."""
    cleaned = str(model_id or "")
    if cleaned.endswith(_CONTEXT_WINDOW_SUFFIX):
        cleaned = cleaned[: -len(_CONTEXT_WINDOW_SUFFIX)]
    cleaned = _strip_cloud_provider(cleaned)
    priced_as = str(priced_as or "")
    if not priced_as or not _prefix_boundary_match(cleaned, priced_as):
        return None
    match = _NEWER_VERSION_RE.match(cleaned[len(priced_as) :])
    return priced_as + match.group(0) if match else None


def _strip_cloud_provider(model_id: str) -> str:
    """Strip Bedrock/Vertex wrapping around an Anthropic model id:
    ``us.anthropic.``/``eu.anthropic.``/``anthropic.`` prefixes, a
    trailing ``-v1:0`` Bedrock version suffix, and a trailing
    ``@YYYYMMDD`` Vertex date suffix.
    """
    stripped = model_id
    for prefix in _CLOUD_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            break
    if stripped.endswith(_BEDROCK_SUFFIX):
        stripped = stripped[: -len(_BEDROCK_SUFFIX)]
    match = _VERTEX_DATE_SUFFIX_RE.search(stripped)
    if match:
        stripped = stripped[: match.start()]
    return stripped


def _default_claudeglass_dir() -> Path:
    """``~/.claude/claudeglass``, or ``$CLAUDE_CONFIG_DIR/claudeglass`` when
    ``CLAUDE_CONFIG_DIR`` moves the whole config tree elsewhere."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else (Path.home() / ".claude")
    return root / CLAUDEGLASS_DIRNAME


def _locate_pricing_source(
    path: str | Path | None, config_dir: str | Path | None
) -> tuple[bytes, str]:
    """Resolve which pricing file to read, per the plan's order: explicit
    path -> user config dir -> packaged default. Returns the raw file
    bytes and a human-readable path/label for provenance display.
    """
    if path is not None:
        resolved = Path(path)
        try:
            return resolved.read_bytes(), str(resolved)
        except OSError as exc:
            raise PricingError(f"cannot read pricing file: {resolved} ({exc})") from exc

    claudeglass_dir = Path(config_dir) if config_dir is not None else _default_claudeglass_dir()
    candidate = claudeglass_dir / "pricing.toml"
    if candidate.exists():
        try:
            return candidate.read_bytes(), str(candidate)
        except OSError as exc:
            raise PricingError(f"cannot read pricing file: {candidate} ({exc})") from exc

    try:
        packaged = importlib.resources.files("claudeglass").joinpath("pricing.toml")
        return packaged.read_bytes(), "claudeglass/pricing.toml (packaged default)"
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise PricingError(f"packaged default pricing.toml is missing: {exc}") from exc


def _parse_model_entry(model_id: str, entry: object) -> tuple[ModelRates, list[str]]:
    if not isinstance(entry, dict):
        raise PricingError(f"models.\"{model_id}\" must be a table, got {type(entry).__name__}")

    missing = [f for f in _RATE_FIELDS if f not in entry]
    if missing:
        raise PricingError(
            f"models.\"{model_id}\" is missing required rate field(s): {', '.join(missing)}"
        )
    try:
        rate_values = {f: float(entry[f]) for f in _RATE_FIELDS}
    except (TypeError, ValueError) as exc:
        raise PricingError(f"models.\"{model_id}\" has a non-numeric rate value") from exc

    raw_geo = entry.get("geo_multipliers", {}) or {}
    if not isinstance(raw_geo, dict):
        raise PricingError(f"models.\"{model_id}\".geo_multipliers must be a table")
    try:
        geo_multipliers = {str(k): float(v) for k, v in raw_geo.items()}
    except (TypeError, ValueError) as exc:
        raise PricingError(
            f"models.\"{model_id}\".geo_multipliers has a non-numeric value"
        ) from exc

    long_context: LongContextRule | None = None
    raw_lc = entry.get("long_context")
    if raw_lc is not None:
        if not isinstance(raw_lc, dict) or "threshold_tokens" not in raw_lc:
            raise PricingError(
                f"models.\"{model_id}\".long_context requires threshold_tokens"
            )
        try:
            threshold_tokens = int(raw_lc["threshold_tokens"])
        except (TypeError, ValueError) as exc:
            raise PricingError(
                f"models.\"{model_id}\".long_context.threshold_tokens must be an integer"
            ) from exc
        multiplier_raw = raw_lc.get("multiplier")
        multiplier = float(multiplier_raw) if multiplier_raw is not None else None
        overrides = {k: float(v) for k, v in raw_lc.items() if k in _RATE_FIELDS}
        long_context = LongContextRule(
            threshold_tokens=threshold_tokens,
            multiplier=multiplier,
            overrides=overrides or None,
        )

    fast: FastRule | None = None
    raw_fast = entry.get("fast")
    if raw_fast is not None:
        if not isinstance(raw_fast, dict) or "multiplier" not in raw_fast:
            raise PricingError(f"models.\"{model_id}\".fast requires multiplier")
        try:
            fast_multiplier = float(raw_fast["multiplier"])
        except (TypeError, ValueError) as exc:
            raise PricingError(
                f"models.\"{model_id}\".fast.multiplier must be a number"
            ) from exc
        fast = FastRule(multiplier=fast_multiplier)

    aliases_raw = entry.get("aliases", [])
    if not isinstance(aliases_raw, list) or not all(isinstance(a, str) for a in aliases_raw):
        raise PricingError(f"models.\"{model_id}\".aliases must be a list of strings")

    raw_context_window = entry.get("context_window_tokens")
    if raw_context_window is None:
        context_window_tokens = _DEFAULT_CONTEXT_WINDOW_TOKENS
    else:
        try:
            context_window_tokens = int(raw_context_window)
        except (TypeError, ValueError) as exc:
            raise PricingError(
                f"models.\"{model_id}\".context_window_tokens must be an integer"
            ) from exc

    rates = ModelRates(
        canonical_id=model_id,
        input=rate_values["input"],
        output=rate_values["output"],
        cache_write_5m=rate_values["cache_write_5m"],
        cache_write_1h=rate_values["cache_write_1h"],
        cache_read=rate_values["cache_read"],
        geo_multipliers=geo_multipliers,
        long_context=long_context,
        fast=fast,
        context_window_tokens=context_window_tokens,
    )
    return rates, list(aliases_raw)


def _build_pricing(data: dict, path_str: str, raw_bytes: bytes) -> Pricing:
    version = data.get("version")
    if not version:
        raise PricingError(f"pricing file is missing 'version': {path_str}")

    models_raw = data.get("models")
    if not isinstance(models_raw, dict) or not models_raw:
        raise PricingError(f"pricing file has no [models] table: {path_str}")

    models: dict[str, ModelRates] = {}
    aliases: dict[str, str] = {}
    for model_id, entry in models_raw.items():
        rates, alias_list = _parse_model_entry(model_id, entry)
        models[model_id] = rates
        for alias in alias_list:
            aliases[alias] = model_id

    server_tools_raw = data.get("server_tools", {}) or {}
    if not isinstance(server_tools_raw, dict):
        raise PricingError(f"pricing file's [server_tools] must be a table: {path_str}")
    try:
        server_tools = {str(k): float(v) for k, v in server_tools_raw.items()}
    except (TypeError, ValueError) as exc:
        raise PricingError(f"[server_tools] has a non-numeric value: {path_str}") from exc

    return Pricing(
        path=path_str,
        version=str(version),
        currency=str(data.get("currency", "USD")),
        source_url=data.get("source_url"),
        retrieved=data.get("retrieved"),
        notes=data.get("notes"),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        models=models,
        aliases=aliases,
        server_tools=server_tools,
    )


def load_pricing(
    path: str | Path | None = None, config_dir: str | Path | None = None
) -> Pricing:
    """Load a rate card.

    Resolution order: an explicit ``path`` (the CLI's ``--pricing``
    flag) -> ``<config_dir>/pricing.toml`` (``config_dir`` defaults to
    ``~/.claude/claudeglass``, or ``$CLAUDE_CONFIG_DIR/claudeglass`` when
    that env var moves the whole config tree; the CLI's ``--config-dir``
    flag feeds this) -> the packaged default shipped inside
    ``claudeglass/pricing.toml``.

    Raises :class:`PricingError` if the file cannot be read, is not
    valid TOML, or is missing required structure (no ``[models]``
    table, a model missing a rate field, a non-numeric rate).
    """
    raw_bytes, path_str = _locate_pricing_source(path, config_dir)
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PricingError(f"pricing file is not valid UTF-8: {path_str}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PricingError(f"malformed pricing file ({path_str}): {exc}") from exc
    return _build_pricing(data, path_str, raw_bytes)


def _turn_token_total(turn: Turn) -> int:
    return (
        turn.input_tokens
        + turn.cache_creation_tokens
        + turn.cache_read_tokens
        + turn.output_tokens
    )


#: ``write_split`` keys accepted for the 5-minute and 1-hour TTL buckets.
#: The TTL simulation (plan Appendix A4) calls ``price_turn`` with
#: ``write_split={T: write}`` where ``T`` is the *seconds* form (300 /
#: 3600) since that's the policy identifier the simulation loop iterates
#: over; other callers (existing tests, report code) pass the friendlier
#: "5m"/"1h" string form. Both, plus the stringified-int form a caller
#: might produce by accident (``"300"``), are accepted; anything else is
#: a programming error, not a silently-ignored key, so it raises.
_WRITE_SPLIT_5M_KEYS = frozenset({300, "300", "5m"})
_WRITE_SPLIT_1H_KEYS = frozenset({3600, "3600", "1h"})


def _normalize_write_split(write_split: dict) -> tuple[int, int]:
    write_5m = 0
    write_1h = 0
    for key, value in write_split.items():
        if key in _WRITE_SPLIT_5M_KEYS:
            write_5m += int(value)
        elif key in _WRITE_SPLIT_1H_KEYS:
            write_1h += int(value)
        else:
            raise ValueError(
                f"unknown write_split key: {key!r} (expected one of 300/'300'/'5m' "
                "or 3600/'3600'/'1h')"
            )
    return write_5m, write_1h


#: Sentinel default for ``price_turn``'s ``geo`` parameter: "use
#: ``turn.inference_geo``" — distinct from an explicitly passed ``None``,
#: which means "disable the geo multiplier even if the turn observed one".
_GEO_FROM_TURN = object()

#: ``geo`` values that apply no multiplier even when a matching
#: ``geo_multipliers`` entry exists — "not_available" is the documented
#: literal ``usage.inference_geo`` carries when the field is present but
#: unresolved; empty string and ``None`` are the same "nothing observed"
#: case from an unset/blank field.
_NO_GEO_VALUES = frozenset({None, "", "not_available"})


def price_turn(
    turn: Turn,
    rates: ModelRates | ResolvedRates | None,
    write_split: dict | None = None,
    read_tokens: int | None = None,
    geo: str | None = _GEO_FROM_TURN,  # type: ignore[assignment]
) -> CostBreakdown:
    """Price one turn against a resolved rate.

    The default path (``write_split=None``, ``read_tokens=None``) uses
    the turn's own observed split: ``turn.cc_5m``/``turn.cc_1h`` for
    cache-write tokens and ``turn.cache_read_tokens`` for cache-read
    tokens. The simulation path (used by the TTL package) overrides one
    or both: ``write_split`` replaces the write split entirely (keys
    ``300``/``"300"``/``"5m"`` -> the 5-minute bucket, ``3600``/
    ``"3600"``/``"1h"`` -> the 1-hour bucket; any other key raises
    ``ValueError``), and ``read_tokens`` replaces the read count. Calling
    this with the turn's own observed values passed explicitly must equal
    the default-path result exactly — later packages rely on that
    invariant.

    ``geo`` defaults to a sentinel meaning "use ``turn.inference_geo``",
    so an ordinary call prices the turn's actual observed geo
    automatically. Passing ``geo=None`` explicitly (or ``""``/
    ``"not_available"``) overrides that and disables the multiplier even
    when the turn observed a geo. When the effective geo value matches an
    entry in the model's ``geo_multipliers``, it multiplies all four cost
    components (the documented data-residency uplift).

    When ``turn.speed == "fast"`` and the resolved model carries a
    ``[.fast]`` table, every one of the five standard rates is
    multiplied by that model's fast multiplier *before* the
    ``long_context``/geo rules below apply to them, so cache and geo
    multipliers stack on top of the fast rate rather than replacing it
    (the returned ``CostBreakdown.fast_applied`` records whether this
    happened). A fast-flagged turn on a model with no ``[.fast]`` table
    is priced at that model's standard rate.

    A model's ``long_context`` rule applies when ``turn.ctx`` is at or
    above its threshold: explicit ``overrides`` win over ``multiplier``
    when both are present.

    An unresolved model (``rates`` is ``None``) prices every component
    at zero with ``model_known=False``, so it is inert to sum but
    visible in coverage accounting.

    ``turn.web_search_requests`` is priced at ``rates``'s
    ``web_search_per_1000`` (the rate card's ``[server_tools]`` table,
    carried on :class:`ResolvedRates` by ``resolve_model`` — 0.0 when
    the rate card sets no rate, ``rates`` is a bare :class:`ModelRates`,
    or the model didn't resolve) and added into ``total`` as
    ``server_tool_cost``. It is a flat per-request fee, so it doesn't
    stack with the fast/long-context/geo multipliers above.
    ``web_fetch_requests`` has no documented per-request rate and is
    never priced.
    """
    if isinstance(rates, ResolvedRates):
        model_rates: ModelRates | None = rates.rates
        web_search_per_1000 = rates.web_search_per_1000
    else:
        model_rates = rates
        web_search_per_1000 = 0.0

    if model_rates is None:
        return CostBreakdown(model_known=False)

    if write_split is None:
        write_5m = turn.cc_5m
        write_1h = turn.cc_1h
    else:
        write_5m, write_1h = _normalize_write_split(write_split)

    read = turn.cache_read_tokens if read_tokens is None else read_tokens

    input_rate = model_rates.input
    output_rate = model_rates.output
    write_5m_rate = model_rates.cache_write_5m
    write_1h_rate = model_rates.cache_write_1h
    read_rate = model_rates.cache_read

    fast_applied = False
    if turn.speed == "fast" and model_rates.fast is not None:
        fast_applied = True
        fast_multiplier = model_rates.fast.multiplier
        input_rate *= fast_multiplier
        output_rate *= fast_multiplier
        write_5m_rate *= fast_multiplier
        write_1h_rate *= fast_multiplier
        read_rate *= fast_multiplier

    long_context_applied = False
    rule = model_rates.long_context
    if rule is not None and turn.ctx >= rule.threshold_tokens:
        long_context_applied = True
        if rule.overrides:
            input_rate = rule.overrides.get("input", input_rate)
            output_rate = rule.overrides.get("output", output_rate)
            write_5m_rate = rule.overrides.get("cache_write_5m", write_5m_rate)
            write_1h_rate = rule.overrides.get("cache_write_1h", write_1h_rate)
            read_rate = rule.overrides.get("cache_read", read_rate)
        elif rule.multiplier is not None:
            input_rate *= rule.multiplier
            output_rate *= rule.multiplier
            write_5m_rate *= rule.multiplier
            write_1h_rate *= rule.multiplier
            read_rate *= rule.multiplier

    input_cost = turn.input_tokens / 1_000_000 * input_rate
    output_cost = turn.output_tokens / 1_000_000 * output_rate
    cache_write_cost = (write_5m / 1_000_000 * write_5m_rate) + (
        write_1h / 1_000_000 * write_1h_rate
    )
    cache_read_cost = read / 1_000_000 * read_rate

    effective_geo = turn.inference_geo if geo is _GEO_FROM_TURN else geo
    if effective_geo in _NO_GEO_VALUES:
        effective_geo = None

    if effective_geo and model_rates.geo_multipliers:
        multiplier = model_rates.geo_multipliers.get(effective_geo)
        if multiplier is not None:
            input_cost *= multiplier
            output_cost *= multiplier
            cache_write_cost *= multiplier
            cache_read_cost *= multiplier

    # A flat per-request server-tool fee (documented separately from the
    # per-token rates on the pricing page), so it doesn't stack with the
    # fast/long-context/geo multipliers above.
    server_tool_cost = turn.web_search_requests / 1_000 * web_search_per_1000

    total = input_cost + output_cost + cache_write_cost + cache_read_cost + server_tool_cost
    return CostBreakdown(
        input_cost=input_cost,
        output_cost=output_cost,
        cache_write_cost=cache_write_cost,
        cache_read_cost=cache_read_cost,
        server_tool_cost=server_tool_cost,
        total=total,
        long_context_applied=long_context_applied,
        model_known=True,
        fast_applied=fast_applied,
    )


@dataclass(frozen=True, slots=True)
class EffectiveRates:
    """The per-million-token USD rates one turn was charged at: its
    model's rates with fast mode, the long-context rule and the
    data-residency uplift applied (see :func:`effective_rates`)."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float


def effective_rates(
    turn: Turn,
    rates: ModelRates | ResolvedRates | None,
    geo: str | None = _GEO_FROM_TURN,  # type: ignore[assignment]
) -> EffectiveRates | None:
    """The rates :func:`price_turn` charges ``turn`` at, for pricing
    tokens it doesn't itself count, such as a tag Claude wrote inside the
    turn's output or a note carried in its context. The multipliers apply
    in :func:`price_turn`'s order: fast mode, then the long-context rule,
    then the geo uplift. ``None`` for an unresolved model."""
    model_rates = rates.rates if isinstance(rates, ResolvedRates) else rates
    if model_rates is None:
        return None
    values = [
        model_rates.input,
        model_rates.output,
        model_rates.cache_write_5m,
        model_rates.cache_write_1h,
        model_rates.cache_read,
    ]
    if turn.speed == "fast" and model_rates.fast is not None:
        values = [v * model_rates.fast.multiplier for v in values]
    rule = model_rates.long_context
    if rule is not None and turn.ctx >= rule.threshold_tokens:
        if rule.overrides:
            keys = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
            values = [rule.overrides.get(key, v) for key, v in zip(keys, values)]
        elif rule.multiplier is not None:
            values = [v * rule.multiplier for v in values]
    effective_geo = turn.inference_geo if geo is _GEO_FROM_TURN else geo
    if effective_geo not in _NO_GEO_VALUES and model_rates.geo_multipliers:
        multiplier = model_rates.geo_multipliers.get(effective_geo)
        if multiplier is not None:
            values = [v * multiplier for v in values]
    return EffectiveRates(*values)


def cache_read_savings_usd(rows: list[dict], pricing: Pricing) -> float:
    """What cache reads saved against sending the same tokens fresh as
    input: for each row (one per model -- e.g. ``Store.
    cache_read_tokens_by_model``'s own ``{"model", "cache_read_tokens"}``
    shape), ``cache_read_tokens * (input - cache_read)`` per token,
    summed, in USD at list price -- ``/api/summary``'s additive
    ``cache_saved`` figure. A model this rate card doesn't resolve is
    left out entirely, the same "priced models only" rule every other
    per-model pricing loop in this project follows (see
    :func:`price_turn`'s own unresolved-model handling and
    ``explain.cost_split``)."""
    total = 0.0
    for row in rows:
        resolved = pricing.resolve_model(row.get("model"))
        if resolved is None:
            continue
        rates = resolved.rates
        cache_read_tokens = row.get("cache_read_tokens") or 0
        total += cache_read_tokens * (rates.input - rates.cache_read) / 1_000_000
    return total


@dataclass(slots=True)
class PricingCoverage:
    """Accumulates how much of a corpus was actually priced, for the
    report's "unknown model" table and the ``pricing-coverage``
    recommendation. One instance is shared across every turn priced in a
    run.
    """

    total_turns: int = 0
    priced_turns: int = 0
    total_tokens: int = 0
    priced_tokens: int = 0
    #: unknown model id -> {"turns": int, "tokens": int}
    unknown: dict[str, dict[str, int]] = field(default_factory=dict)
    #: observed model id -> {"priced_as": canonical id, "turns": int,
    #: "tokens": int}, for turns priced by closest (prefix) match rather
    #: than their own pricing.toml row (``ResolvedRates.approximate``).
    closest_matches: dict[str, dict] = field(default_factory=dict)
    #: observed model id -> {"turns": int, "tokens": int}, for turns
    #: flagged ``usage.speed == "fast"`` that were priced at standard
    #: rates because their model carries no ``[.fast]`` table.
    fast_priced_as_standard: dict[str, dict[str, int]] = field(default_factory=dict)
    #: PROF-08: observed model id -> {"turns": int, "tokens": int, "cost":
    #: float, "standard_cost": float}, for turns actually priced at a
    #: model's fast-mode rate (``breakdown.fast_applied``) -- the mirror
    #: image of ``fast_priced_as_standard`` above. ``cost`` is what was
    #: actually charged; ``standard_cost`` is what the same turn would
    #: have cost at that model's standard rate instead (the server-tool
    #: fee, which the fast multiplier never touches -- see
    #: ``price_turn``'s docstring -- is added back unscaled). Feeds
    #: ``whatif._fast_mode``'s "turn fastMode off" estimate, the one
    #: table it reads rather than re-deriving.
    fast_applied: dict[str, dict] = field(default_factory=dict)

    def add(
        self,
        turn: Turn,
        breakdown: CostBreakdown,
        resolved: ResolvedRates | None = None,
    ) -> None:
        """Record one priced (or unpriced) turn against the accumulator.

        ``resolved`` is optional (existing callers that only need the
        unknown-model/coverage_pct accounting may omit it) and, when
        given, also feeds the closest-match and fast-priced-as-standard
        breakdowns below.
        """
        tokens = _turn_token_total(turn)
        model_id = turn.model or "<unknown>"
        self.total_turns += 1
        self.total_tokens += tokens
        if breakdown.model_known:
            self.priced_turns += 1
            self.priced_tokens += tokens
        else:
            entry = self.unknown.setdefault(model_id, {"turns": 0, "tokens": 0})
            entry["turns"] += 1
            entry["tokens"] += tokens

        if resolved is not None and resolved.approximate:
            match_entry = self.closest_matches.setdefault(
                model_id, {"priced_as": resolved.canonical_id, "turns": 0, "tokens": 0}
            )
            match_entry["turns"] += 1
            match_entry["tokens"] += tokens

        if turn.speed == "fast" and breakdown.model_known and not breakdown.fast_applied:
            fast_entry = self.fast_priced_as_standard.setdefault(
                model_id, {"turns": 0, "tokens": 0}
            )
            fast_entry["turns"] += 1
            fast_entry["tokens"] += tokens

        if breakdown.fast_applied and resolved is not None and resolved.rates.fast is not None:
            multiplier = resolved.rates.fast.multiplier
            standard_cost = (
                (breakdown.total - breakdown.server_tool_cost) / multiplier + breakdown.server_tool_cost
                if multiplier
                else breakdown.total
            )
            applied_entry = self.fast_applied.setdefault(
                model_id, {"turns": 0, "tokens": 0, "cost": 0.0, "standard_cost": 0.0}
            )
            applied_entry["turns"] += 1
            applied_entry["tokens"] += tokens
            applied_entry["cost"] += breakdown.total
            applied_entry["standard_cost"] += standard_cost

    @property
    def coverage_pct(self) -> float:
        """Priced tokens as a percentage of all tokens seen. 100.0 when
        no turns have been recorded yet, so an empty corpus never reads
        as "0% covered"."""
        if self.total_tokens == 0:
            return 100.0
        return 100.0 * self.priced_tokens / self.total_tokens

    @property
    def closest_match_turns(self) -> int:
        """Total turns priced by closest (prefix) match rather than their
        own model's pricing.toml row."""
        return sum(entry["turns"] for entry in self.closest_matches.values())

    @property
    def fast_priced_as_standard_turns(self) -> int:
        """Total turns flagged ``usage.speed == "fast"`` that were priced
        at standard rates for lack of a ``[.fast]`` table."""
        return sum(entry["turns"] for entry in self.fast_priced_as_standard.values())

    @property
    def fast_applied_turns(self) -> int:
        """PROF-08: total turns actually priced at a fast-mode rate."""
        return sum(entry["turns"] for entry in self.fast_applied.values())

    def as_table(self) -> Table:
        """The unknown-model table: one row per unresolved model id."""
        columns = [
            Column(key="model_id", label="Model", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="tokens", label="Tokens", kind="tokens"),
        ]
        rows = [
            [model_id, counts["turns"], counts["tokens"]]
            for model_id, counts in sorted(self.unknown.items())
        ]
        notes = []
        if self.unknown:
            notes.append(
                "These model ids have no entry in the pricing file; their cost is"
                " reported as zero. Add them to pricing.toml to price them."
            )
        return Table(
            name="pricing_unknown_models",
            title="Unpriced models",
            columns=columns,
            rows=rows,
            notes=notes,
        )

    def as_closest_match_table(self) -> Table:
        """One row per model id priced by closest (prefix) match: what it
        was actually priced as, so the approximation is visible instead
        of reading as a full 100%-priced model. An id that looks like a
        newer release of what it was priced as (:func:`newer_version_of`)
        also gets a note of its own; the columns stay the same."""
        columns = [
            Column(key="model_id", label="Model", kind="str"),
            Column(key="priced_as", label="Priced as", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="tokens", label="Tokens", kind="tokens"),
        ]
        rows = [
            [model_id, entry["priced_as"], entry["turns"], entry["tokens"]]
            for model_id, entry in sorted(self.closest_matches.items())
        ]
        notes = []
        if self.closest_matches:
            notes.append(
                "These model ids have no pricing.toml row of their own. Their cost"
                " is estimated from the closest registered model's rate instead, so"
                " it may be off. Add a models.\"<id>\" row for this model's own"
                " rates to price it exactly."
            )
        # A newer release the rate card doesn't know yet gets its own
        # line, so it doesn't read as a mismatched model.
        for model_id, entry in sorted(self.closest_matches.items()):
            if newer_version_of(model_id, entry["priced_as"]):
                notes.append(
                    f"{model_id} looks like a newer version of {entry['priced_as']}. It is priced at"
                    f" {entry['priced_as']}'s rate until pricing.toml has a row of its own for it."
                )
        return Table(
            name="pricing_closest_match",
            title="Priced by closest match",
            columns=columns,
            rows=rows,
            notes=notes,
        )

    def as_fast_priced_as_standard_table(self) -> Table:
        """One row per model id seen with ``usage.speed == "fast"`` that
        was priced at its standard rate because it has no ``[.fast]``
        table on file."""
        columns = [
            Column(key="model_id", label="Model", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="tokens", label="Tokens", kind="tokens"),
        ]
        rows = [
            [model_id, entry["turns"], entry["tokens"]]
            for model_id, entry in sorted(self.fast_priced_as_standard.items())
        ]
        notes = []
        if self.fast_priced_as_standard:
            notes.append(
                "These turns were flagged fast mode, but their model has no fast"
                " rate on file, so they were priced at the model's standard rate."
                " Add a models.\"<id>\".fast table to price fast mode exactly."
            )
        return Table(
            name="pricing_fast_priced_as_standard",
            title="Fast turns priced at standard rate",
            columns=columns,
            rows=rows,
            notes=notes,
        )

    def as_fast_applied_table(self) -> Table:
        """PROF-08: one row per model id seen with ``usage.speed ==
        "fast"`` actually priced at its fast-mode rate -- what it cost,
        and what the same turns would have cost at that model's standard
        rate instead. The mirror image of
        :meth:`as_fast_priced_as_standard_table`: that one is turns
        priced fast but billed standard for lack of a rate; this one is
        turns billed fast, and what standard would have cost. Read by
        ``whatif._fast_mode`` for a "turn fastMode off" estimate."""
        columns = [
            Column(key="model_id", label="Model", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="tokens", label="Tokens", kind="tokens"),
            Column(key="cost", label="Cost at fast rate", kind="money"),
            Column(key="standard_cost", label="Cost at standard rate", kind="money"),
        ]
        rows = [
            [model_id, entry["turns"], entry["tokens"], entry["cost"], entry["standard_cost"]]
            for model_id, entry in sorted(self.fast_applied.items())
        ]
        notes = []
        if self.fast_applied:
            notes.append(
                "These replies were actually priced at their model's fast-mode rate (a documented multiplier "
                "over its standard rate). \"Cost at standard rate\" is what the same replies would have cost "
                "with fast mode off."
            )
        return Table(
            name="pricing_fast_applied",
            title="Fast-priced replies",
            columns=columns,
            rows=rows,
            notes=notes,
        )


#: A raw Claude model id inside prose, e.g. ``claude-haiku-4-5-20251001``
#: or ``claude-3-5-haiku-20241022``, with an optional ``[1m]`` suffix.
_MODEL_ID_RE = re.compile(r"\bclaude-(?:\d+-)*(?:opus|sonnet|haiku|fable)(?:-\d+)*(?:\[[0-9a-z]+\])?")


def model_name(model_id: str) -> str:
    """A model id as people say it: ``claude-opus-5-5`` -> ``Opus 5.5``,
    ``claude-haiku-4-5-20251001`` -> ``Haiku 4.5``, ``claude-3-5-haiku-20241022``
    -> ``Haiku 3.5``. A ``[1m]`` suffix is kept (``Opus 5 [1m]``); an id of
    another shape comes back as it is. The Python twin of the dashboard's
    ``format.js`` ``modelName``, for prose only: table cells and JSON keep
    the raw id."""
    text = str(model_id or "")
    suffix = ""
    bracket = re.search(r"\[[^\]]*\]$", text)
    if bracket:
        suffix = " " + bracket.group(0)
        text = text[: bracket.start()]
    core = re.sub(r"-\d{8}$", "", re.sub(r"^claude-", "", text))
    family = re.search(r"[a-z]+", core)
    if not family or not text.startswith("claude-"):
        return str(model_id or "")
    numbers = re.findall(r"\d+", core)
    name = family.group(0).capitalize() + (" " + ".".join(numbers) if numbers else "")
    return name + suffix


def model_names_in(text: str) -> str:
    """``text`` with every raw Claude model id in it read as a model name
    (:func:`model_name`), for a sentence built from a table cell such as
    ``claude-sonnet-5 (+2 more)``."""
    return _MODEL_ID_RE.sub(lambda m: model_name(m.group(0)), str(text))


__all__ = [
    "PricingError",
    "LongContextRule",
    "FastRule",
    "ModelRates",
    "ResolvedRates",
    "EffectiveRates",
    "Pricing",
    "PricingCoverage",
    "load_pricing",
    "newer_version_id",
    "newer_version_of",
    "price_turn",
    "effective_rates",
    "model_name",
    "model_names_in",
    "CLAUDEGLASS_DIRNAME",
]
