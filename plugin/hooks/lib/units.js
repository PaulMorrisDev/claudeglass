/* claudeglass mod: lib/units.js
 *
 * Amounts in the units that matter for the billing mode. A pay-per-token
 * (API) user sees dollars; a Pro or Max user sees a share of the weekly
 * usage limit, or the list-price dollars when that share isn't known yet.
 * Mirrors units.Units.money / money_text / money_cell (src/claudeglass/
 * units.py) and render/tables.py format_cell ("money" and "pct"), the same
 * rules the dashboard's format.js follows; tests/test_mod_vectors.py runs
 * both over the same amounts.
 *
 * Money reads the CLI's way by default ("12.34 USD", never a bare "$"),
 * which is what the Python renders. Pass `{ symbol: true }` for the
 * dashboard's way ("$12.34": two decimals under 10, one under 100, none
 * above, "<$0.01" for a positive amount that rounds to nothing).
 */

/**
 * @typedef {Object} Billing
 * @property {string} mode              "api" or "subscription".
 * @property {number|null} [share_per_usd]  Percent of the weekly limit per list-price dollar; null when unknown.
 * @property {string} [currency]        Currency code; "USD" when absent.
 * @property {string} [period_label]    Name of the limit; "weekly usage limit" when absent.
 */

/**
 * @typedef {Object} AmountOptions
 * @property {string} [period]   Appended after the amount, e.g. "over 30 days".
 * @property {boolean} [symbol]  Write "$12.34" (the dashboard's way) instead of "12.34 USD".
 */

/**
 * @typedef {Object} Amount
 * @property {string} primary    The amount, phrased for the billing mode.
 * @property {string} secondary  The same amount in list-price dollars on a plan with a known share; else "".
 */

// A share of the weekly limit at or above this many percent reads as
// weeks of it (units.WEEKS_FROM_SHARE).
export var WEEKS_FROM_SHARE = 200;

var MINUS = "−";

/**
 * `x` to `digits` decimals the way Python's format() does: an exact tie
 * (0.125 to two places) rounds to the even digit, where toFixed rounds it
 * up. Non-negative `x` only.
 * @param {number} x
 * @param {number} digits  0, 1 or 2.
 * @returns {string}
 */
function fixed(x, digits) {
  var text = x.toFixed(digits);
  var halves = Math.pow(2, digits + 1) * x;
  if (digits > 0 && Number.isInteger(halves) && halves % 2 !== 0) {
    var scaled = Math.floor(x * Math.pow(10, digits));
    if (scaled % 2 !== 0) scaled += 1;
    var digitsText = String(scaled);
    while (digitsText.length <= digits) digitsText = "0" + digitsText;
    text = digitsText.slice(0, digitsText.length - digits) + "." + digitsText.slice(digitsText.length - digits);
  }
  return text;
}

/**
 * `x` to `digits` decimals the way the dashboard's toLocaleString writes
 * it: from the shortest decimal that reads back as `x` ("1.005"), a tie
 * rounding up, so 1.005 is "1.01" where toFixed, working from the binary
 * value, says "1.00". Non-negative, finite `x` only.
 * @param {number} x
 * @param {number} digits
 * @returns {string}
 */
function localeFixed(x, digits) {
  var text = String(x);
  if (text.indexOf("e") !== -1) return x.toFixed(digits);
  var dot = text.indexOf(".");
  var whole = dot === -1 ? text : text.slice(0, dot);
  var fraction = dot === -1 ? "" : text.slice(dot + 1);
  while (fraction.length <= digits) fraction += "0";
  var kept = (whole + fraction.slice(0, digits)).split("");
  if (fraction.charAt(digits) >= "5") {
    var i = kept.length - 1;
    while (i >= 0 && kept[i] === "9") kept[i--] = "0";
    if (i < 0) kept.unshift("1");
    else kept[i] = String(Number(kept[i]) + 1);
  }
  var all = kept.join("");
  return digits === 0 ? all : all.slice(0, all.length - digits) + "." + all.slice(all.length - digits);
}

/**
 * Thousands separators on the whole part of a plain decimal string.
 * @param {string} text
 * @returns {string}
 */
function grouped(text) {
  var dot = text.indexOf(".");
  var whole = dot === -1 ? text : text.slice(0, dot);
  var rest = dot === -1 ? "" : text.slice(dot);
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + rest;
}

/**
 * format_cell(value, "money", currency): 2 decimals, thousands separators,
 * the currency code after ("1,234.50 USD").
 * @param {number} value
 * @param {string} [currency]
 * @returns {string}
 */
function moneyCode(value, currency) {
  // Python keeps the sign of a negative that rounds to zero: "-0.00 USD".
  var sign = value < 0 || Object.is(value, -0) ? "-" : "";
  return sign + grouped(fixed(Math.abs(value), 2)) + " " + (currency || "USD");
}

/**
 * The dashboard's money, character for character (format.js
 * currencyAmount over moneyNumber): "$0.38", "$45.7", "$1,235", "<$0.01",
 * and for a negative "−$3.20" or "<$−0.01" (the dashboard puts the sign
 * after the "<").
 * @param {number} value
 * @param {string} [currency]
 * @returns {string}
 */
export function moneySymbol(value, currency) {
  var abs = Math.abs(value);
  var symbol = !currency || currency === "USD" ? "$" : "";
  var suffix = symbol ? "" : " " + currency;
  if (abs > 0 && abs < 0.005) return "<" + symbol + (value > 0 ? "" : MINUS) + "0.01" + suffix;
  var digits = abs < 10 ? 2 : abs < 100 ? 1 : 0;
  var text = grouped(localeFixed(abs, digits));
  var negative = value < 0 && Number(abs.toFixed(digits)) !== 0;
  return (negative ? MINUS : "") + symbol + text + suffix;
}

/**
 * @param {number} value
 * @param {Billing} billing
 * @param {AmountOptions|undefined} opts
 * @returns {string}
 */
function dollars(value, billing, opts) {
  var currency = billing && billing.currency ? billing.currency : "USD";
  return opts && opts.symbol ? moneySymbol(value, currency) : moneyCode(value, currency);
}

/**
 * A share of the weekly limit: two decimals below 1%, so a small amount
 * doesn't read "0.0%"; one decimal above (units._share_text, with
 * format_cell's "pct").
 * @param {number} share  Percent.
 * @returns {string}
 */
function shareText(share) {
  return share < 1 ? fixed(share, 2) + "%" : fixed(share, 1) + "%";
}

/**
 * @param {*} usd
 * @returns {boolean}
 */
function isAmount(usd) {
  return typeof usd === "number" && isFinite(usd) && usd > 0;
}

/**
 * @param {Billing|null|undefined} billing
 * @returns {number|null}
 */
function sharePerUsd(billing) {
  var share = billing && billing.mode === "subscription" ? billing.share_per_usd : null;
  return typeof share === "number" && isFinite(share) && share > 0 ? share : null;
}

/**
 * `usd` (a list-price amount) phrased for the billing mode, as
 * Units.money does. Null for a non-positive or non-finite amount.
 * @param {number} usd
 * @param {Billing} billing
 * @param {AmountOptions} [opts]
 * @returns {Amount|null}
 */
export function amountParts(usd, billing, opts) {
  if (!isAmount(usd)) return null;
  var suffix = opts && opts.period ? " " + opts.period : "";
  var money = dollars(usd, billing, opts);
  if (!billing || billing.mode !== "subscription") return { primary: money + suffix, secondary: "" };
  var perUsd = sharePerUsd(billing);
  if (perUsd === null) return { primary: money + " list-price equivalent" + suffix, secondary: "" };
  var share = usd * perUsd;
  var limit = billing.period_label || "weekly usage limit";
  var primary =
    share >= WEEKS_FROM_SHARE
      ? "about " + fixed(share / 100, 1) + " weeks' worth of your usage limit" + suffix
      : "about " + shareText(share) + " of your " + limit + suffix;
  return { primary: primary, secondary: money + " list-price equivalent" };
}

/**
 * An amount phrased for the billing mode, never empty (Units.money_text
 * without a prefix): dollars on the API; "about 0.40% of your weekly usage
 * limit" on a plan with a known share; "0.38 USD list-price equivalent"
 * on a plan without one. The list-price equivalent that follows a share is
 * left off here (see amountText). A zero, negative or non-finite amount is
 * the plain figure ("0.00 USD").
 * @param {number} usd
 * @param {Billing} billing
 * @param {AmountOptions} [opts]
 * @returns {string}
 */
export function formatAmount(usd, billing, opts) {
  var amount = amountParts(usd, billing, opts);
  if (amount !== null) return amount.primary;
  return dollars(typeof usd === "number" && isFinite(usd) ? usd : 0, billing, opts);
}

/**
 * Like formatAmount, with the list-price equivalent after a share:
 * "about 0.40% of your weekly usage limit (0.38 USD list-price
 * equivalent)" (Amount.text, through Units.money_text).
 * @param {number} usd
 * @param {Billing} billing
 * @param {AmountOptions} [opts]
 * @returns {string}
 */
export function amountText(usd, billing, opts) {
  var amount = amountParts(usd, billing, opts);
  if (amount === null) return formatAmount(usd, billing, opts);
  return amount.secondary ? amount.primary + " (" + amount.secondary + ")" : amount.primary;
}

/**
 * A short amount for a tight spot (Units.money_cell): "1.0% (2.00 USD)" on
 * a plan with a known share, else the plain figure; "none" for a zero,
 * negative or non-finite amount.
 * @param {number} usd
 * @param {Billing} billing
 * @param {AmountOptions} [opts]
 * @returns {string}
 */
export function formatAmountCell(usd, billing, opts) {
  if (!isAmount(usd)) return "none";
  var money = dollars(usd, billing, opts);
  var perUsd = sharePerUsd(billing);
  return perUsd === null ? money : shareText(usd * perUsd) + " (" + money + ")";
}

/**
 * A token count at three significant figures, compacted with a lower-case
 * k as the plan's copy writes it: 999 -> "999", 12400 -> "12.4k",
 * 178000 -> "178k", 1240000 -> "1.24M". Rules of format.js compactNumber,
 * which writes "K"; a minus is a true minus sign. A missing or non-finite
 * count is "-".
 * @param {number|null|undefined} n
 * @returns {string}
 */
export function formatTokens(n) {
  if (typeof n !== "number" || !isFinite(n)) return "-";
  var abs = Math.abs(n);
  var negative = n < 0;
  if (abs < 1000) {
    var whole = Math.round(abs);
    return (negative && whole !== 0 ? MINUS : "") + grouped(String(whole));
  }
  /** @type {[number, string][]} */
  var units = [
    [1e12, "T"],
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "k"],
  ];
  for (var i = 0; i < units.length; i++) {
    var unit = /** @type {[number, string]} */ (units[i]);
    if (abs >= unit[0] * 0.9995) {
      var scaled = abs / unit[0];
      var digits = scaled < 10 ? 2 : scaled < 100 ? 1 : 0;
      var text = Number(scaled.toFixed(digits)).toString();
      // 999.95k rounds to 1000k: step up to the next unit instead.
      if (Number(text) >= 1000 && i > 0) {
        var larger = /** @type {[number, string]} */ (units[i - 1]);
        return (negative ? MINUS : "") + Number((abs / larger[0]).toFixed(2)).toString() + larger[1];
      }
      return (negative ? MINUS : "") + text + unit[1];
    }
  }
  return String(n);
}
