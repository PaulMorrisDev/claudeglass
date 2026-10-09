/* claudeglass mod: lib/band.js
 *
 * How the band above the prompt gets narrower. It has up to three pieces: the
 * context, the spend, and one that appears only when it matters (the "extra"). Each
 * has a full form and a short one. When they don't fit, the forms change in a fixed
 * order, so what the user must act on is the last thing to go:
 *
 *   1. the spend goes short
 *   2. the context goes short
 *   3. the spend goes, while the extra is urgent (the cache is cold or ending, the
 *      5-hour limit will run out before it resets, or a summary may have dropped
 *      something)
 *   4. the extra goes short
 *   5. the extra goes: when it is not urgent the spend comes back, short, if it fits
 *   6. the spend goes (the only step left after 5 when the extra is urgent: an urgent
 *      extra never goes before the spend does, so the spend stays gone)
 *
 * The context is always kept. The desktop's bar shows only when every piece fits at
 * full length. Nothing here reads a clock, the disk or the engine; the widths are in
 * cells and the caller measures them.
 */

/**
 * @typedef {"context"|"spend"|"extra"} Role
 */

/**
 * @typedef {Object} Piece
 * @property {Role} role
 * @property {string} key
 * @property {number} full         Cells at full length.
 * @property {number|null} short   Cells of the short form; null when it has none.
 * @property {boolean} [urgent]    For the extra: it is something to act on, so the spend gives way first.
 */

/**
 * @typedef {"full"|"short"|"gone"} Form
 */

/**
 * @typedef {Object} Shape
 * @property {Object<string, Form>} forms  By piece key.
 * @property {boolean} bar                 The bar fits too.
 */

// Cells a separator takes between two pieces (a gap, the rule, a gap).
export var SEPARATOR_CELLS = 3;

/**
 * The forms each piece takes in the order of steps above, one plan per step.
 * @param {boolean} urgent
 * @returns {{ context: Form, spend: Form, extra: Form }[]}
 */
function plans(urgent) {
  /** @type {{ context: Form, spend: Form, extra: Form }[]} */
  var list = [
    { context: "full", spend: "full", extra: "full" },
    { context: "full", spend: "short", extra: "full" },
    { context: "short", spend: "short", extra: "full" },
  ];
  if (urgent) list.push({ context: "short", spend: "gone", extra: "full" });
  list.push({ context: "short", spend: urgent ? "gone" : "short", extra: "short" });
  // An urgent extra is what the spend gave way to: the spend does not come back when it goes.
  if (!urgent) list.push({ context: "short", spend: "short", extra: "gone" });
  list.push({ context: "short", spend: "gone", extra: "gone" });
  return list;
}

/**
 * The forms the pieces take to fit `room` cells, and whether the bar fits.
 * When nothing fits the last plan stands: the context alone, short.
 * @param {Piece[]} pieces    In the order they are drawn; at most one of each role.
 * @param {number} room       Cells for the pieces and their separators.
 * @param {number} [barCells] Cells the bar takes; 0 or left out when there is no bar.
 * @returns {Shape}
 */
export function narrow(pieces, room, barCells) {
  /** @type {Piece|undefined} */
  var extra;
  for (var i = 0; i < pieces.length; i++) {
    var one = /** @type {Piece} */ (pieces[i]);
    if (one.role === "extra") extra = one;
  }
  var list = plans(extra !== undefined && extra.urgent === true);

  /**
   * @param {{ context: Form, spend: Form, extra: Form }} plan
   * @returns {{ forms: Object<string, Form>, width: number }}
   */
  function measure(plan) {
    /** @type {Object<string, Form>} */
    var forms = {};
    var width = 0;
    var shown = 0;
    for (var j = 0; j < pieces.length; j++) {
      var piece = /** @type {Piece} */ (pieces[j]);
      var form = plan[piece.role];
      forms[piece.key] = form;
      if (form === "gone") continue;
      width += form === "short" && piece.short !== null ? piece.short : piece.full;
      shown += 1;
    }
    return { forms: forms, width: width + SEPARATOR_CELLS * Math.max(0, shown - 1) };
  }

  var bars = typeof barCells === "number" && barCells > 0 ? barCells : 0;
  for (var k = 0; k < list.length; k++) {
    var found = measure(/** @type {{ context: Form, spend: Form, extra: Form }} */ (list[k]));
    if (found.width <= room || k === list.length - 1) {
      return { forms: found.forms, bar: bars > 0 && k === 0 && found.width + bars <= room };
    }
  }
  return { forms: {}, bar: false };
}
