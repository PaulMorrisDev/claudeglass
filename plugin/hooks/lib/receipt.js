/* claudeglass mod: lib/receipt.js
 *
 * The receipt of a re-add: after the user puts lost items back, each one waits
 * for a later reply to show Claude still has it. Every main reply that ends
 * counts one against the item; a reply whose text (or whose tool inputs) holds
 * the item settles it as referenced; RECEIPT_REPLIES replies with no match
 * settle it as not referenced. A settled receipt is never touched again.
 *
 * The match is the same as the summary check's (coverage.js markItem): exact
 * tokens for files and values, the share of key words for text.
 *
 * Nothing here reads a clock, the disk or the engine, and the texts it is
 * given are looked at, never kept.
 */

import { markItem, prepareSummary } from "./coverage.js";

/** How many replies an item gets to be referenced before it counts as not referenced. */
export const RECEIPT_REPLIES = 10;

/**
 * @typedef {import("./coverage.js").KeepItem} KeepItem
 */

/**
 * @typedef {Object} Receipt
 * @property {number} at          When the item was put back (ms).
 * @property {number} replies     Main replies seen since.
 * @property {boolean|null} referenced  null until a reply references it or `limit` replies pass without.
 */

/**
 * @typedef {Object} ReceiptReview
 * @property {KeepItem[]} readded
 * @property {Record<string, Receipt>} receipts
 */

/**
 * Settles the waiting receipts against the texts of one reply that ended (its answers and tool inputs).
 * Returns a new review, or the same object when nothing changed. A receipt whose item is no longer in
 * `review.readded` is left as it is.
 * @template {ReceiptReview} R
 * @param {R} review
 * @param {readonly string[]} texts
 * @param {number} [limit]  Replies before an unmatched item counts as not referenced.
 * @returns {R}
 */
export function settleReceipts(review, texts, limit) {
  var cap = typeof limit === "number" && limit > 0 ? limit : RECEIPT_REPLIES;
  var receipts = review.receipts && typeof review.receipts === "object" ? review.receipts : {};
  var readded = Array.isArray(review.readded) ? review.readded : [];
  var prepared = (Array.isArray(texts) ? texts : []).filter(function (text) {
    return typeof text === "string" && text !== "";
  }).map(prepareSummary);
  /** @type {Record<string, Receipt>} */
  var next = {};
  var changed = false;
  Object.keys(receipts).forEach(function (id) {
    var receipt = receipts[id];
    next[id] = receipt;
    if (!receipt || receipt.referenced !== null) return;
    var item = readded.find(function (one) {
      return one.id === id;
    });
    if (!item) return;
    var seen = prepared.some(function (summary) {
      return markItem(item, summary) === "found";
    });
    var replies = receipt.replies + 1;
    next[id] = { at: receipt.at, replies: replies, referenced: seen ? true : replies >= cap ? false : null };
    changed = true;
  });
  return changed ? Object.assign({}, review, { receipts: next }) : review;
}
