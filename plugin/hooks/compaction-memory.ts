import type { GlassKeepItem, GlassMark, GlassPin, GlassReview, GlassReviewed, GlassReviewRow, GlassSnap } from '../types'
import { COPY } from './generated/copy.js'
import { checkCoverage } from './lib/coverage.js'
import type { HoldState } from './lib/coverage.js'
import { buildKeepList, exactTokens, itemId, pinText, splitSuggested, squash, trimText } from './lib/keeplist.js'
import { fill } from './lib/text.js'
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
/** The summary Claude Code puts first after a compaction starts with this. */
export const SUMMARY_LEAD = 'This session is being continued'

export type Counts = { kept: number; missing: number; added: number }

/** What a render hook or another file leaves for the event hooks (a render may not write $.state). */
export const memory: {
  /** The sensor counts of summaries, waiting for the next reply (or the summary's own listener) to record them. */
  outbox: { ts: number; kind: 'auto' | 'manual'; counts: Counts }[]
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
    after: null,
    history: [],
    ui: { view: 'before', toggled: [], summary: false, list: false, earlier: false, busy: false, note: null },
  }
}

/** A review state from the host, whatever it holds (an older shape, or nothing). */
export function sound(review: GlassReview | null | undefined): GlassReview {
  return review === null || review === undefined || !Array.isArray(review.list) ? emptyReview() : review
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

/** The keep list for the conversation so far. */
export function keepList(messages: readonly MessageLike[], pins: readonly GlassPin[], claudeMd: string | null): GlassKeepItem[] {
  return buildKeepList(messages as MessageLike[], { pins: pins as GlassPin[], claudeMd }) as GlassKeepItem[]
}

/** How many items the before view suggests. */
export function suggestCount(items: readonly GlassKeepItem[]): number {
  return splitSuggested(items as GlassKeepItem[]).suggested.length
}

/** What to say when the summary has just been written, or null when there was nothing to check. */
export function reviewCompaction(args: {
  messages: readonly MessageLike[]
  summary: string
  pins: readonly GlassPin[]
  claudeMd: string | null
  trigger: 'auto' | 'manual'
  now: number
  before: number | null
  after: number | null
}): { reviewed: GlassReviewed; row: GlassReviewRow; counts: Counts } {
  const items = keepList(args.messages, args.pins, args.claudeMd)
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
  if (history.length > MAX_HISTORY) history.splice(0, history.length - MAX_HISTORY)
  return {
    ...review,
    ts: reviewed.ts,
    missing: row.missing,
    seen: row.missing === 0,
    held: false,
    list: reviewed.items,
    after: reviewed,
    history,
    ui: { ...review.ui, view: 'after', toggled: [], summary: false, list: false, earlier: false, busy: false, note: null },
  }
}

/** The boundary line in the transcript, or null when nothing the user said was checked (`total` counts the things they said). */
export function boundaryLine(counts: Counts, total: number): string | null {
  if (total === 0) return null
  const missing = counts.missing
  const text =
    missing === 0
      ? COPY['log.compacted.ok.said']
      : `${missing === 1 ? COPY['log.compacted.missing.one'] : fill(COPY['log.compacted.missing'], { count: missing })} ${COPY['log.compacted.review']}`
  return `● ${COPY['note.lead']} · ${fill(COPY['log.compacted'], { missing: text })}`
}

/** The pins as keep-list items. */
export function pinItems(pins: readonly GlassPin[]): GlassKeepItem[] {
  return pins.map((pin, index) => ({ id: pin.id, kind: 'pin', text: pin.text, safe: false, why: '', exact: exactTokens(pin.text) as string[], at: Number.MAX_SAFE_INTEGER - pins.length + index }))
}

/** The keep list with the pins as they stand now (the list in state is as of the last reply). */
export function listWithPins(list: readonly GlassKeepItem[], pins: readonly GlassPin[]): GlassKeepItem[] {
  return [...pinItems(pins), ...list.filter(item => item.kind !== 'pin')]
}

/** A pin for some text, or null when there is nothing to pin. */
export function newPin(text: string): GlassPin | null {
  const one = pinText(text)
  return one === '' ? null : { id: itemId('pin', one), text: one }
}
