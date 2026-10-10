import type { GlassKeepItem, GlassMark, GlassPin, GlassReceipt, GlassReview, GlassReviewed, GlassReviewRow, GlassReviewTrigger, GlassSnap } from '../types'
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
/** Earlier prompts the page lists, and how many characters of each it keeps. */
export const MAX_EARLIER = 12
const EARLIER_CHARS = 300
const MAX_HISTORY = 20
/** Items the keep list suggests (ticked from the start) before the rest sit behind "more". */
export const SUGGESTED_SHOWN = 5
/** The summary Claude Code puts first after a compaction starts with this. */
export const SUMMARY_LEAD = 'This session is being continued'

export type Counts = { kept: number; missing: number; added: number }

/** What a render hook or another file leaves for the event hooks (a render may not write $.state). */
export const memory: {
  /** The sensor counts of summaries, waiting for the next reply (or the summary's own listener) to record them. */
  outbox: { ts: number; kind: GlassReviewTrigger; counts: Counts }[]
  /** Where the hold of automatic summaries stands (lib/coverage.js holdStep). */
  hold: HoldState
  /** CLAUDE.md as the mod can read it (project and user level), or null when not read yet. */
  claudeMd: string | null
  cwd: string | null
  /** Items the user drafted back in since the last summary was recorded. */
  added: number
  /** The one typed prompt whose row offers [p] pin (a render may not write $.state, so the rows read it from here). */
  pinnable: string | null
} = { outbox: [], hold: 'none', claudeMd: null, cwd: null, added: 0, pinnable: null }

export function forgetMemory(): void {
  memory.outbox = []
  memory.hold = 'none'
  memory.claudeMd = null
  memory.cwd = null
  memory.added = 0
  memory.pinnable = null
}

/** Records the waiting `compaction` events: counts only, never any text. Returns whether any were recorded. */
export function drainSensor(snap: GlassSnap): boolean {
  if (memory.outbox.length === 0) return false
  for (const one of memory.outbox) {
    // "Added back" is what the user drafted back in before this reply (counted in memory.added).
    const added = one === memory.outbox[memory.outbox.length - 1] ? one.counts.added + memory.added : one.counts.added
    addEvent(snap, { ts: seconds(one.ts), event: 'compaction', kind: one.kind, kept: one.counts.kept, missing: one.counts.missing, added })
  }
  memory.outbox = []
  memory.added = 0
  return true
}

export function emptyReview(): GlassReview {
  return {
    ts: null,
    missing: 0,
    seen: true,
    held: false,
    suggest: 0,
    list: [],
    readded: [],
    receipts: {},
    after: null,
    history: [],
    ui: { view: 'before', toggled: [], summary: false, list: false, earlier: false, busy: false, note: null },
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
  const row: GlassReviewRow = { ts: args.now, trigger: args.trigger, before: args.before, after: args.after, kept: coverage.kept, missing: coverage.missing }
  return { reviewed, row, counts: { kept: coverage.kept, missing: coverage.missing, added: 0 } }
}

/** The review after a summary: badge fields, the check, and a row in the history. */
export function withSummary(review: GlassReview, reviewed: GlassReviewed, row: GlassReviewRow): GlassReview {
  const history = [...review.history, row]
  // An item the summary now holds is no longer sent to the summariser as put back, and its receipt goes with it.
  const readded = review.readded.filter(item => !['found', 'safe'].includes(reviewed.marks[item.id] ?? ''))
  if (history.length > MAX_HISTORY) history.splice(0, history.length - MAX_HISTORY)
  return {
    ...review,
    ts: reviewed.ts,
    missing: row.missing,
    seen: row.missing === 0,
    held: false,
    list: reviewed.items,
    readded,
    receipts: Object.fromEntries(readded.map(item => [item.id, review.receipts[item.id]]).filter((one): one is [string, GlassReceipt] => one[1] !== undefined)),
    after: reviewed,
    history,
    ui: { ...review.ui, view: 'after', toggled: [], summary: false, list: false, earlier: false, busy: false, note: null },
  }
}

/**
 * The review with the things the user said that the last check marked missing or partly put back: added to `readded`
 * once each, with a fresh receipt. An item already put back keeps the receipt it has; everything else is as it was.
 */
export function readdMissing(review: GlassReview, now: number): GlassReview {
  const after = review.after
  if (after === null) return review
  const held = new Set(review.readded.map(item => item.id))
  const added = after.items.filter(item => isSaid(item) && !item.safe && ['missing', 'partly'].includes(after.marks[item.id] ?? '') && !held.has(item.id))
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
