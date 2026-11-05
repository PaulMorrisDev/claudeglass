import type { GlassKeepItem, GlassMark, GlassPin, GlassReceipt, GlassReview, GlassReviewed, GlassReviewRow, GlassReviewTrigger, GlassSnap, GlassTweakKind } from '../types'
import { COPY } from './generated/copy.js'
import { checkCoverage, compactKeep, isSaid, KEEP_MAX } from './lib/coverage.js'
import type { HoldState } from './lib/coverage.js'
import { buildKeepList, exactTokens, itemId, pinText, splitSuggested, squash, trimText } from './lib/keeplist.js'
import { fill } from './lib/text.js'
import { formatTokens } from './lib/units.js'
import { addEvent, seconds } from './sensor'

// The compaction review's plain logic and the little it keeps in module memory.
// Nothing here touches `$` (Claude Code follows `$` only within the file that
// holds it); compaction.ts, pins.ts, band.tsx and the page hand it what it needs.

/** A message as `$.session.messages()` and session.compact give it (the parts the keep list reads). */
export type MessageLike = {
  role: string
  text?: string
  toolUses?: { tool?: string; input?: Record<string, unknown>; isError?: boolean; text?: string }[]
  toolResults?: { tool_use_id?: string; text?: string; isError?: boolean }[]
}

/** Pins the keep list may hold. */
export const MAX_PINS = 20
/** Re-adds followed at once for their receipts. */
const MAX_WATCHED = 10
/** Earlier prompts the page lists, and how many characters of each it keeps. */
export const MAX_EARLIER = 12
const EARLIER_CHARS = 300
const MAX_HISTORY = 20
/** Items the keep list suggests (ticked from the start) before the rest sit behind "more". */
export const SUGGESTED_SHOWN = 5
/** The summary Claude Code puts first after a compaction starts with this. */
export const SUMMARY_LEAD = 'This session is being continued'

export type Counts = { kept: number; missing: number; added: number }

/** A press the sensor counts, waiting for the next reply to record it: counts only, never any text. `after` is filled in when it is known. */
export type Tweak = { ts: number; kind: GlassTweakKind; before: number; after?: number }

/** What a render hook or another file leaves for the event hooks (a render may not write $.state). */
export const memory: {
  /**
   * The sensor counts of summaries, waiting for the next reply (or the summary's own listener) to record them: whether
   * the summariser was told what to keep (`instructed`) and how many put-back items still in the review the replies had
   * referenced when the summary was written.
   */
  outbox: { ts: number; kind: GlassReviewTrigger; counts: Counts; instructed: 0 | 1; referenced: number }[]
  /** The button presses waiting for the next reply to record them. */
  tweaks: Tweak[]
  /** The Summarise press whose summary has not been recorded yet: the summary's kept count becomes its `after`. */
  summarising: Tweak | null
  /** Re-adds whose receipts are not all settled yet: the press, and the items it put back. */
  readds: { tweak: Tweak; ids: string[] }[]
  /** Re-adds that settled after their press was recorded: the press's time (seconds), the items it put back and the referenced count to add to it. */
  settled: { ts: number; before: number; after: number }[]
  /** Where the hold of automatic summaries stands (lib/coverage.js holdStep). */
  hold: HoldState
  /** CLAUDE.md as the mod can read it (project and user level), or null when not read yet. */
  claudeMd: string | null
  cwd: string | null
  /** Items the user drafted back in since the last summary was recorded. */
  added: number
  /** The one typed prompt whose row offers [p] pin (a render may not write $.state, so the rows read it from here). */
  pinnable: string | null
} = { outbox: [], tweaks: [], summarising: null, readds: [], settled: [], hold: 'none', claudeMd: null, cwd: null, added: 0, pinnable: null }

export function forgetMemory(): void {
  memory.outbox = []
  memory.tweaks = []
  memory.summarising = null
  memory.readds = []
  memory.settled = []
  memory.hold = 'none'
  memory.claudeMd = null
  memory.cwd = null
  memory.added = 0
  memory.pinnable = null
}

/**
 * Notes a press for the sensor: the button, the count it acted on and, when known, the count that came out of it.
 * Counts only, never any text. Recorded when the next reply ends (drainSensor); the returned press can still be given
 * its `after` until then.
 */
export function noteTweak(kind: GlassTweakKind, before: number, after?: number, now: number = Date.now()): Tweak {
  const one: Tweak = after === undefined ? { ts: now, kind, before } : { ts: now, kind, before, after }
  memory.tweaks.push(one)
  return one
}

/** A Summarise press: the summary it starts fills in `after` (the count it kept) when the summary is recorded. */
export function noteSummarise(before: number, now: number = Date.now()): Tweak {
  const mine = noteTweak('summarise', before, undefined, now)
  memory.summarising = mine
  return mine
}

/** A Summarise press is done waiting for its summary (it was recorded, held, refused or skipped); a later press's slot is left alone. */
export function endSummarise(mine: Tweak): void {
  if (memory.summarising === mine) memory.summarising = null
}

/** A Re-add press: `after` is the count the replies referenced, filled in once every item's receipt is settled. */
export function noteReadd(ids: readonly string[], now: number = Date.now()): void {
  if (ids.length === 0) return
  memory.readds.push({ tweak: noteTweak('readd', ids.length, undefined, now), ids: [...ids] })
  // Only the newest few are followed: a re-add whose receipts never settle must not pile up.
  if (memory.readds.length > MAX_WATCHED) memory.readds.splice(0, memory.readds.length - MAX_WATCHED)
}

/** How many put-back items the replies have referenced, by their receipts. */
export function referencedCount(review: GlassReview): number {
  return Object.values(review.receipts).filter(receipt => receipt.referenced === true).length
}

/**
 * Called after the receipts are settled by a reply: a re-add whose receipts are all settled gets its referenced count;
 * one whose items left the review (a summary held them) can no longer say and is let go.
 */
export function settleReadds(review: GlassReview): void {
  memory.readds = memory.readds.filter(watch => {
    const receipts = watch.ids.map(id => review.receipts[id])
    if (receipts.some(receipt => receipt === undefined)) return false
    if (receipts.some(receipt => receipt?.referenced === null)) return true
    const after = receipts.filter(receipt => receipt?.referenced === true).length
    // Still waiting to be recorded, it takes the count itself; already recorded, the next drain adds it to the event.
    if (memory.tweaks.includes(watch.tweak)) watch.tweak.after = after
    else memory.settled.push({ ts: seconds(watch.tweak.ts), before: watch.ids.length, after })
    return false
  })
}

/** Records the waiting `compaction` and `tweak` events: counts only, never any text. Returns whether any were recorded. */
export function drainSensor(snap: GlassSnap): boolean {
  let recorded = false
  // A re-add that settled after its press was recorded: the event gets its `after` now.
  for (const one of memory.settled) {
    const event = snap.events.find(found => found.event === 'tweak' && found.kind === 'readd' && found.ts === one.ts && found.before === one.before && found.after === undefined)
    if (event?.event === 'tweak') {
      event.after = one.after
      snap.eventsDirty = true
      recorded = true
    }
  }
  memory.settled = []
  for (const one of memory.tweaks) {
    addEvent(snap, one.after === undefined ? { ts: seconds(one.ts), event: 'tweak', kind: one.kind, before: one.before } : { ts: seconds(one.ts), event: 'tweak', kind: one.kind, before: one.before, after: one.after })
    recorded = true
  }
  memory.tweaks = []
  for (const one of memory.outbox) {
    // "Added back" is what the user drafted back in before this reply (counted in memory.added).
    const added = one === memory.outbox[memory.outbox.length - 1] ? one.counts.added + memory.added : one.counts.added
    addEvent(snap, { ts: seconds(one.ts), event: 'compaction', kind: one.kind, kept: one.counts.kept, missing: one.counts.missing, added, instructed: one.instructed, referenced: one.referenced })
    recorded = true
  }
  if (memory.outbox.length > 0) memory.added = 0
  memory.outbox = []
  return recorded
}

export function emptyReview(): GlassReview {
  return {
    ts: null,
    missing: 0,
    repliesAt: null,
    seen: true,
    held: false,
    suggest: 0,
    list: [],
    readded: [],
    receipts: {},
    after: null,
    history: [],
    ui: { view: 'before', toggled: [], summary: false, list: false, earlier: false, busy: false, action: null, note: null, noteFor: null },
  }
}

/** A review state from the host, whatever it holds (an older shape, or nothing). */
export function sound(review: GlassReview | null | undefined): GlassReview {
  if (review === null || review === undefined || !Array.isArray(review.list)) return emptyReview()
  // A state kept from before items could be put back has no `readded` or `receipts`.
  const readded = Array.isArray(review.readded) ? review.readded : []
  const receipts = review.receipts !== null && typeof review.receipts === 'object' ? review.receipts : {}
  return readded === review.readded && receipts === review.receipts ? review : { ...review, readded, receipts }
}

/** Whether a message is the summary Claude Code puts first, not something the user typed. */
export function isSummaryText(text: string): boolean {
  return text.trimStart().startsWith(SUMMARY_LEAD)
}

function visible(message: MessageLike): string | null {
  if (message.role !== 'user') return null
  const text = squash(message.text ?? '')
  if (text === '' || text.startsWith('<') || isSummaryText(text)) return null
  return text
}

/** What the user typed before the summary, newest first. */
export function earlierPrompts(messages: readonly MessageLike[]): { id: string; text: string }[] {
  const out: { id: string; text: string }[] = []
  for (let i = messages.length - 1; i >= 0 && out.length < MAX_EARLIER; i--) {
    const text = visible(messages[i] as MessageLike)
    if (text === null) continue
    const kept = trimText(text, EARLIER_CHARS)
    out.push({ id: itemId('prompt', kept), text: kept })
  }
  return out
}

/** The keep list for the conversation so far; files under `cwd`, the session's folder, are listed relative to it. */
export function keepList(messages: readonly MessageLike[], pins: readonly GlassPin[], claudeMd: string | null, cwd: string | null = null): GlassKeepItem[] {
  return buildKeepList(messages as MessageLike[], { pins: pins as GlassPin[], claudeMd, cwd }) as GlassKeepItem[]
}

/** The rebuilt keep list with the items put back after a summary lost them: after the pins, and none twice. */
export function withReadded(items: readonly GlassKeepItem[], readded: readonly GlassKeepItem[]): GlassKeepItem[] {
  const held = new Set(items.map(item => item.id))
  const back = readded.filter(item => !held.has(item.id))
  const pins = items.filter(item => item.kind === 'pin')
  return [...pins, ...back, ...items.filter(item => item.kind !== 'pin')]
}

/** What the keep list offers now: the pins as they stand, then the list as of the last reply with the put-back items in it. */
export function keepCandidates(review: GlassReview, pins: readonly GlassPin[]): GlassKeepItem[] {
  return withReadded(listWithPins(review.list, pins), review.readded)
}

/** The items suggested (ticked from the start) and the rest; put-back items count as suggested, whatever their kind, and come right after the pins. */
export function splitKeep(items: readonly GlassKeepItem[], readded: readonly GlassKeepItem[], limit: number = SUGGESTED_SHOWN): { suggested: GlassKeepItem[]; more: GlassKeepItem[] } {
  const back = new Set(readded.map(item => item.id))
  const { suggested, more } = splitSuggested(items.filter(item => !back.has(item.id)) as GlassKeepItem[], limit)
  const pinned = suggested.filter(item => item.kind === 'pin')
  return { suggested: [...pinned, ...items.filter(item => back.has(item.id)), ...suggested.filter(item => item.kind !== 'pin')], more }
}

/** Whether an item's tick is on: suggested items start on, the rest off, and an id in `toggled` is the other way round. */
export function isTicked(toggled: readonly string[], id: string, suggested: boolean): boolean {
  return toggled.includes(id) ? !suggested : suggested
}

/** The items the keep list has ticked, in list order: what the page's command and the summariser's instructions carry. */
export function tickedItems(review: GlassReview, pins: readonly GlassPin[]): GlassKeepItem[] {
  const items = keepCandidates(review, pins)
  const { suggested, more } = splitKeep(items, review.readded)
  const order = new Map(items.map((item, index) => [item.id, index]))
  const chosen = [...suggested.filter(item => isTicked(review.ui.toggled, item.id, true)), ...more.filter(item => isTicked(review.ui.toggled, item.id, false))]
  return chosen.sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0))
}

/** The text that tells the summariser what to keep: the ticked items in the words of `compaction.instruct`, or "" when there are none. */
export function keepInstruction(chosen: readonly GlassKeepItem[]): string {
  return compactKeep(chosen as GlassKeepItem[], COPY['compaction.instruct'], KEEP_MAX, { files: COPY['compaction.label.files'], values: COPY['compaction.label.values'] })
}

/** How many items the before view suggests: the put-back items count, whatever their kind, as the ticks show them. */
export function suggestCount(items: readonly GlassKeepItem[], readded: readonly GlassKeepItem[]): number {
  return splitKeep(items, readded).suggested.length
}

/** The start of the summariser's keep instruction ("Keep: "), as `compaction.instruct` words it. */
export const INSTRUCT_LEAD = COPY['compaction.instruct'].split('{items}')[0] ?? ''

/** What to say when the summary has just been written, or null when there was nothing to check. */
export function reviewCompaction(args: {
  messages: readonly MessageLike[]
  summary: string
  pins: readonly GlassPin[]
  claudeMd: string | null
  cwd?: string | null
  /** Items put back after an earlier summary lost them; they are no longer in `messages`, so they are checked from here. */
  readded?: readonly GlassKeepItem[]
  trigger: GlassReviewTrigger
  now: number
  before: number | null
  after: number | null
}): { reviewed: GlassReviewed; row: GlassReviewRow; counts: Counts } {
  const items = withReadded(keepList(args.messages, args.pins, args.claudeMd, args.cwd ?? null), args.readded ?? [])
  const coverage = checkCoverage(items, args.summary)
  const reviewed: GlassReviewed = {
    ts: args.now,
    trigger: args.trigger,
    summary: args.summary,
    items,
    marks: coverage.marks as Record<string, GlassMark>,
    judged: false,
    earlier: earlierPrompts(args.messages),
  }
  // "Missing" is what the user would have to put back: the things they said that were dropped or kept only in part.
  const missing = lostSaid(reviewed).length
  const row: GlassReviewRow = { ts: args.now, trigger: args.trigger, before: args.before, after: args.after, kept: coverage.kept, missing }
  return { reviewed, row, counts: { kept: coverage.kept, missing, added: 0 } }
}

/** The review after a summary: badge fields, the check, and a row in the history. */
export function withSummary(review: GlassReview, reviewed: GlassReviewed, row: GlassReviewRow, replies: number | null = null): GlassReview {
  const history = [...review.history, row]
  // An item the summary now holds is no longer sent to the summariser as put back, and its receipt goes with it.
  const readded = review.readded.filter(item => !['found', 'safe'].includes(reviewed.marks[item.id] ?? ''))
  if (history.length > MAX_HISTORY) history.splice(0, history.length - MAX_HISTORY)
  return {
    ...review,
    ts: reviewed.ts,
    missing: lostSaid(reviewed).length,
    repliesAt: replies,
    seen: row.missing === 0,
    held: false,
    list: reviewed.items,
    readded,
    receipts: Object.fromEntries(readded.map(item => [item.id, review.receipts[item.id]]).filter((one): one is [string, GlassReceipt] => one[1] !== undefined)),
    after: reviewed,
    history,
    ui: { ...review.ui, view: 'after', toggled: [], summary: false, list: false, earlier: false, busy: false, action: null, note: null, noteFor: null },
  }
}

/**
 * The things the user said that a check marked missing or only partly kept: what a summary lost. This is the one
 * meaning of "lost" the band, the toast, the sensor and the pane share.
 */
export function lostSaid(reviewed: GlassReviewed): GlassKeepItem[] {
  return reviewed.items.filter(item => isSaid(item) && !item.safe && ['missing', 'partly'].includes(reviewed.marks[item.id] ?? ''))
}

/** The lost items not yet put back: what Re-add would add now. */
export function lostItems(review: GlassReview): GlassKeepItem[] {
  if (review.after === null) return []
  const back = new Set(review.readded.map(item => item.id))
  return lostSaid(review.after).filter(item => !back.has(item.id))
}

/** How many items Re-add would add now. */
export function lostCount(review: GlassReview): number {
  return lostItems(review).length
}

/**
 * The review with the things the user said that the last check marked missing or partly put back: added to `readded`
 * once each, with a fresh receipt. An item already put back keeps the receipt it has; everything else is as it was.
 */
export function readdMissing(review: GlassReview, now: number): GlassReview {
  const added = lostItems(review)
  if (added.length === 0) return review
  const receipts = { ...review.receipts }
  for (const item of added) receipts[item.id] = { at: now, replies: 0, referenced: null }
  return { ...review, readded: [...review.readded, ...added], receipts }
}

/** What the Detailed view adds to the boundary line: the context before and after, and what the summary cost when that is known (an amount in the user's billing words). */
export type BoundaryDetail = { before: number | null; after: number | null; amount: string | null }

/**
 * The boundary line in the transcript, or null when nothing the user said was checked (`total` counts the things
 * they said). With `detail` (the Detailed view) it also says how the context changed, and what the summary cost.
 */
export function boundaryLine(counts: Counts, total: number, detail: BoundaryDetail | null = null): string | null {
  if (total === 0) return null
  const missing = counts.missing
  const text =
    missing === 0
      ? COPY['log.compacted.ok.said']
      : `${missing === 1 ? COPY['log.compacted.missing.one'] : fill(COPY['log.compacted.missing'], { count: missing })} ${COPY['log.compacted.review']}`
  const line = `● ${COPY['note.lead']} · ${fill(COPY['log.compacted'], { missing: text })}`
  if (detail === null || detail.before === null || detail.after === null) return line
  const figures = { before: `${formatTokens(detail.before)} tokens`, after: `${formatTokens(detail.after)} tokens` }
  return `${line} ${detail.amount === null ? fill(COPY['log.compacted.tokens.plain'], figures) : fill(COPY['log.compacted.tokens'], { ...figures, amount: detail.amount })}`
}

/** The pins as keep-list items. */
export function pinItems(pins: readonly GlassPin[]): GlassKeepItem[] {
  return pins.map((pin, index) => ({ id: pin.id, kind: 'pin', text: pin.text, safe: false, why: '', exact: exactTokens(pin.text) as string[], at: Number.MAX_SAFE_INTEGER - pins.length + index }))
}

/** The keep list with the pins as they stand now (the list in state is as of the last reply). */
export function listWithPins(list: readonly GlassKeepItem[], pins: readonly GlassPin[]): GlassKeepItem[] {
  return [...pinItems(pins), ...list.filter(item => item.kind !== 'pin')]
}

/** The pins whose text holds all of `words` (case does not matter); every pin when `words` is "all". */
export function pinsMatching(pins: readonly GlassPin[], words: string): GlassPin[] {
  const wanted = squash(words).toLowerCase()
  if (wanted === '') return []
  return wanted === 'all' ? [...pins] : pins.filter(pin => squash(pin.text).toLowerCase().includes(wanted))
}

/** A pin for some text, or null when there is nothing to pin. */
export function newPin(text: string): GlassPin | null {
  const one = pinText(text)
  return one === '' ? null : { id: itemId('pin', one), text: one }
}
