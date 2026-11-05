import type { GlassSettings, GlassSnap } from '../types'
import { cacheState } from './lib/cache.js'
import type { CacheState } from './lib/cache.js'
import { runway } from './lib/context.js'
import { ratesFor, sendCost } from './lib/pricing.js'
import type { ModelRates } from './lib/pricing.js'
import { soundSettings } from './settings'
import { soundSnap } from './live'

// What the surfaces draw from: the snapshot and the settings, with the few
// figures worked out from them (the cache countdown, the runway, the parts of
// the context, the 5-hour pace). Reading it from a render hook subscribes the
// drawing, so a change to the snapshot draws it again.

export type Parts = {
  startup: number
  conversation: number
  output: number
  free: number
  /** Where Claude Code summarises; null when auto-compact is off. */
  point: number | null
  tokens: number
}

export type Pace = { pct: number; runsOutAt: number; resetsAt: number }

export type View = {
  snap: GlassSnap
  settings: GlassSettings
  detailed: boolean
  now: number
  working: boolean
  ttl: number
  rates: ModelRates | null
  /** Null until a reply has finished. */
  cache: CacheState | null
  /** Tokens in the context now. */
  tokens: number
  /** Replies before Claude summarises; null when the pace isn't known yet. */
  replies: number | null
  parts: Parts | null
  pace: Pace | null
}

const FIVE_HOURS_MS = 5 * 60 * 60 * 1000

/** The tokens at which an idle session counts as having a big context to rewrite. */
export function coldMin(settings: GlassSettings): number {
  return settings.thresholds.cold_min_tokens ?? 100000
}

/** Startup context, conversation, tool output kept and room left, in tokens; null before there are figures. */
export function partsOf(snap: GlassSnap): Parts | null {
  const reading = snap.reading
  if (reading === null) return null
  const tokens = reading.tokens
  let startup = reading.parts.startup
  let conversation = reading.parts.conversation
  if (startup === null || conversation === null) {
    const first = snap.startupFirst
    startup = first === null ? 0 : Math.min(first, tokens)
    conversation = Math.max(0, tokens - startup)
  }
  const output = Math.min(snap.toolKept, conversation)
  return {
    startup,
    conversation: conversation - output,
    output,
    // With auto-compact off the room left is what the window holds.
    free: Math.max(0, (reading.point ?? reading.window) - tokens),
    point: reading.point,
    tokens,
  }
}

/** The 5-hour limit's pace: set when, at this rate, it runs out before it resets. */
export function paceOf(snap: GlassSnap, now: number): Pace | null {
  const limit = snap.fiveHour
  if (limit === null || limit.resetsAt === null) return null
  const resetsAt = Date.parse(limit.resetsAt)
  if (!Number.isFinite(resetsAt) || resetsAt <= now) return null
  const elapsed = FIVE_HOURS_MS - (resetsAt - now)
  if (elapsed < 15 * 60 * 1000 || limit.pct < 5 || limit.pct >= 100) return null
  const runsOutAt = now + ((100 - limit.pct) / limit.pct) * elapsed
  return runsOutAt < resetsAt ? { pct: limit.pct, runsOutAt, resetsAt } : null
}

/** USD to send the next message over the context now, warm or cold. */
export function sendCostNow(view: View, isWarm: boolean): number {
  return sendCost(view.rates, view.tokens, isWarm, view.ttl)
}

export function buildView(snap: GlassSnap, settings: GlassSettings, detailed: boolean, now: number, working: boolean): View {
  const ttl = settings.ttl
  const last = snap.reqs[snap.reqs.length - 1]
  const tokens = snap.reading?.tokens ?? last?.ctx ?? 0
  const point = snap.reading?.point ?? 0
  return {
    snap,
    settings,
    detailed,
    now,
    working,
    ttl,
    rates: ratesFor(settings.models, snap.model),
    cache: snap.lastTs === null ? null : cacheState(snap.lastTs, now, ttl),
    tokens,
    // Right after a summary the history still ends at the old, large context: no count until the next reply.
    replies: point > 0 && !snap.pendingCompaction ? runway(snap.history, point) : null,
    parts: partsOf(snap),
    pace: paceOf(snap, now),
  }
}

/**
 * What a render hook draws from. The hook reads the four values itself, so the
 * drawing subscribes to them (read(\$, snapAtom), read(\$, settingsAtom),
 * read(\$, detailedAtom), \$.clock.now()); settings not read yet draw as the fallback.
 */
export function drawView(snap: GlassSnap, settings: GlassSettings | null, detailed: boolean, now: number, working: boolean): View {
  return buildView(soundSnap(snap), soundSettings(settings), detailed, now, working)
}
