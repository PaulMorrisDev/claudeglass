/* claudeglass mod: lib/pricing.js
 *
 * What a request costs, from the rate card the settings file carries.
 * Pure: no clock, no I/O. Mirrors src/claudeglass/pricing.py
 * (Pricing.resolve_model and price_turn); tests/test_mod_vectors.py runs
 * both over the same cases.
 *
 * Rates are USD per million tokens. The settings file keys them by
 * canonical model id: models["claude-sonnet-5-5"] = { aliases: [...],
 * input, output, cache_write_5m, cache_write_1h, cache_read,
 * context_window_tokens }.
 */

/**
 * @typedef {Object} LongContextRule
 * @property {number} threshold_tokens  A request whose context is at or above this is priced by the rule.
 * @property {number} [multiplier]      Multiplies all five rates.
 * @property {Object<string, number>} [overrides]  Explicit replacement rates; win over `multiplier`.
 */

/**
 * @typedef {Object} ModelRates
 * @property {string[]} [aliases]
 * @property {number} input
 * @property {number} output
 * @property {number} cache_write_5m
 * @property {number} cache_write_1h
 * @property {number} cache_read
 * @property {number} [context_window_tokens]
 * @property {LongContextRule} [long_context]  Optional; the packaged rate card has none today.
 */

/**
 * @typedef {Object} Usage
 * @property {number} [input_tokens]
 * @property {number} [output_tokens]
 * @property {number} [cache_read_input_tokens]
 * @property {number} [cache_creation_input_tokens]
 * @property {string} [model]
 */

// Bedrock and Vertex wrapping around an Anthropic model id (pricing.py
// _strip_cloud_provider).
var CLOUD_PREFIXES = ["us.anthropic.", "eu.anthropic.", "anthropic."];
var BEDROCK_SUFFIX = "-v1:0";
var VERTEX_DATE_SUFFIX = /@\d{8}$/;
var CONTEXT_WINDOW_SUFFIX = "[1m]";

// A cache write lasts an hour from this TTL up; below it, five minutes.
var ONE_HOUR_S = 3600;

/**
 * @param {Object} object
 * @param {string} key
 * @returns {boolean}
 */
function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

/**
 * @param {string} text
 * @param {string} suffix
 * @returns {boolean}
 */
function endsWith(text, suffix) {
  return text.length >= suffix.length && text.slice(text.length - suffix.length) === suffix;
}

/**
 * @param {*} value
 * @returns {number}
 */
function count(value) {
  return typeof value === "number" && isFinite(value) ? value : 0;
}

/**
 * Strip Bedrock/Vertex wrapping: a `us.anthropic.`, `eu.anthropic.` or
 * `anthropic.` prefix, a trailing `-v1:0` and a trailing `@YYYYMMDD`.
 * @param {string} modelId
 * @returns {string}
 */
function stripCloudProvider(modelId) {
  var stripped = modelId;
  for (var i = 0; i < CLOUD_PREFIXES.length; i++) {
    if (stripped.indexOf(/** @type {string} */ (CLOUD_PREFIXES[i])) === 0) {
      stripped = stripped.slice(/** @type {string} */ (CLOUD_PREFIXES[i]).length);
      break;
    }
  }
  if (endsWith(stripped, BEDROCK_SUFFIX)) stripped = stripped.slice(0, stripped.length - BEDROCK_SUFFIX.length);
  return stripped.replace(VERTEX_DATE_SUFFIX, "");
}

/**
 * Exact id, then alias. The last model to list an alias wins, as the
 * Python alias table is built in order.
 * @param {Object<string, ModelRates>} models
 * @param {string} modelId
 * @returns {string|null}
 */
function lookupExactOrAlias(models, modelId) {
  if (has(models, modelId)) return modelId;
  /** @type {string|null} */
  var found = null;
  var ids = Object.keys(models);
  for (var i = 0; i < ids.length; i++) {
    var id = /** @type {string} */ (ids[i]);
    var aliases = models[id] && /** @type {ModelRates} */ (models[id]).aliases;
    if (Array.isArray(aliases) && aliases.indexOf(modelId) !== -1) found = id;
  }
  return found;
}

/**
 * Does `cleaned` start with `canonicalId` at a token boundary? The next
 * character must be `-`, `@` or nothing, so `claude-opus-4-10` is not a
 * variant of `claude-opus-4-1`.
 * @param {string} cleaned
 * @param {string} canonicalId
 * @returns {boolean}
 */
function prefixBoundaryMatch(cleaned, canonicalId) {
  if (cleaned.indexOf(canonicalId) !== 0) return false;
  var rest = cleaned.slice(canonicalId.length);
  return rest === "" || rest.charAt(0) === "-" || rest.charAt(0) === "@";
}

/**
 * The rate card's id for an observed model string, or null when it has
 * none. Order: exact id, alias, a trailing `[1m]` taken off, Bedrock or
 * Vertex wrapping taken off, then the longest registered id the string
 * starts with at a token boundary. `<synthetic>` and empty give null.
 * Same order as Pricing.resolve_model.
 * @param {Object<string, ModelRates>} models  The settings file's `pricing.models`.
 * @param {string|null|undefined} modelId
 * @returns {string|null}
 */
export function resolveModel(models, modelId) {
  if (!models || typeof modelId !== "string" || !modelId || modelId === "<synthetic>") return null;

  var direct = lookupExactOrAlias(models, modelId);
  if (direct !== null) return direct;

  var candidate = modelId;
  if (endsWith(candidate, CONTEXT_WINDOW_SUFFIX)) {
    candidate = candidate.slice(0, candidate.length - CONTEXT_WINDOW_SUFFIX.length);
    var withoutWindow = lookupExactOrAlias(models, candidate);
    if (withoutWindow !== null) return withoutWindow;
  }

  var cleaned = stripCloudProvider(candidate);
  if (cleaned !== candidate) {
    var unwrapped = lookupExactOrAlias(models, cleaned);
    if (unwrapped !== null) return unwrapped;
  }

  /** @type {string|null} */
  var best = null;
  var ids = Object.keys(models);
  for (var i = 0; i < ids.length; i++) {
    var id = /** @type {string} */ (ids[i]);
    if (prefixBoundaryMatch(cleaned, id) && (best === null || id.length > best.length)) best = id;
  }
  return best;
}

/**
 * The rates for an observed model string, or null.
 * @param {Object<string, ModelRates>} models
 * @param {string|null|undefined} modelId
 * @returns {ModelRates|null}
 */
export function ratesFor(models, modelId) {
  var id = resolveModel(models, modelId);
  return id === null ? null : /** @type {ModelRates} */ (models[id]);
}

/**
 * USD for one request. Each token kind is priced at its per-million rate.
 * The usage has no 5-minute/1-hour split of cache writes, so every write
 * is priced at the hour rate when `ttlSeconds` is 3600 or more and at the
 * five-minute rate below it. Unlike price_turn, this does not know about
 * fast mode, geo multipliers or web-search fees. It does apply a model's
 * `long_context` rule, on the request's context (input + cache read +
 * cache write), when the settings carry one. Null rates cost nothing, as
 * an unpriced model does in price_turn.
 * @param {ModelRates|null} rates
 * @param {Usage} usage
 * @param {number} ttlSeconds  The cache lifetime writes are made at.
 * @returns {number}
 */
export function priceUsage(rates, usage, ttlSeconds) {
  if (!rates) return 0;
  var u = usage || {};
  var input = count(u.input_tokens);
  var output = count(u.output_tokens);
  var read = count(u.cache_read_input_tokens);
  var write = count(u.cache_creation_input_tokens);

  var long = ttlSeconds >= ONE_HOUR_S;
  var inputRate = rates.input;
  var outputRate = rates.output;
  var writeRate = long ? rates.cache_write_1h : rates.cache_write_5m;
  var readRate = rates.cache_read;

  var rule = rates.long_context;
  if (rule && input + write + read >= rule.threshold_tokens) {
    var overrides = rule.overrides;
    if (overrides && Object.keys(overrides).length > 0) {
      var writeKey = long ? "cache_write_1h" : "cache_write_5m";
      if (has(overrides, "input")) inputRate = /** @type {number} */ (overrides.input);
      if (has(overrides, "output")) outputRate = /** @type {number} */ (overrides.output);
      if (has(overrides, writeKey)) writeRate = /** @type {number} */ (overrides[writeKey]);
      if (has(overrides, "cache_read")) readRate = /** @type {number} */ (overrides.cache_read);
    } else if (typeof rule.multiplier === "number") {
      inputRate *= rule.multiplier;
      outputRate *= rule.multiplier;
      writeRate *= rule.multiplier;
      readRate *= rule.multiplier;
    }
  }

  var inputCost = (input / 1000000) * inputRate;
  var outputCost = (output / 1000000) * outputRate;
  var writeCost = (write / 1000000) * writeRate;
  var readCost = (read / 1000000) * readRate;
  return inputCost + outputCost + writeCost + readCost;
}

/**
 * USD to send the next message over `contextTokens` of context. Warm: the
 * whole context is read from the cache. Cold: it is written again, at the
 * hour rate when `ttlSeconds` is 3600 or more, else the five-minute rate.
 * The message's own input and output are not counted. A model's
 * `long_context` rule applies as it does in priceUsage.
 * @param {ModelRates|null} rates
 * @param {number} contextTokens
 * @param {boolean} isWarm
 * @param {number} ttlSeconds
 * @returns {number}
 */
export function sendCost(rates, contextTokens, isWarm, ttlSeconds) {
  var tokens = count(contextTokens);
  return priceUsage(rates, isWarm ? { cache_read_input_tokens: tokens } : { cache_creation_input_tokens: tokens }, ttlSeconds);
}
