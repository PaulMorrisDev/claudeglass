import type { GlassSnap } from '../types'

// What a list-price amount is worth in the 5-hour limit. Claude Code reports the
// limit as a percent used, not a rate, so the rate comes from this session's own
// figures: the dollars spent and the percent the limit moved since the window was
// first seen (calBase, set in track.ts at the first measurement of each window).

/** The least spend, in USD, and the least movement of the limit, in percent, before the rate is trusted. */
export const MIN_RATE_USD = 0.1
export const MIN_RATE_PCT = 1

/**
 * Percent of the 5-hour limit per list-price dollar, or null when it isn't known:
 * the limit hasn't been reported, no window has been seen yet (a new session or a
 * new window starts over), too little has been spent or moved to tell, or another session
 * is running: the percent is the account's, so it moves with that session's spend too and
 * would read as a higher rate for this one (heartbeat.ts keeps `parallel`). It is an
 * estimate: the percent is rounded by Claude Code.
 */
export function pctPerUsd(snap: GlassSnap): number | null {
  const base = snap.calBase
  if (snap.parallel === true) return null
  if (base === null || snap.fiveHour === null || snap.cost === null) return null
  const usd = snap.cost - base.usd
  const pct = snap.fiveHour.pct - base.pct
  return usd >= MIN_RATE_USD && pct >= MIN_RATE_PCT ? pct / usd : null
}

/** Percent of the 5-hour limit that `usd` of this session's spend has been worth in this window, or null when not known yet. */
export function limitShare(snap: GlassSnap, usd: number): number | null {
  const rate = pctPerUsd(snap)
  return rate === null || !Number.isFinite(usd) || usd < 0 ? null : usd * rate
}

const FIVE_HOURS_MS = 5 * 60 * 60 * 1000

/**
 * True when the session's first reply came inside the 5-hour window now running (the window ends at the limit's
 * reset and began five hours before). False when that can't be told: the reset time is unknown, or the session
 * was not seen from its start (a resumed one), so some of its spend may belong to an earlier window.
 */
export function startedInWindow(snap: GlassSnap): boolean {
  const limit = snap.fiveHour
  if (limit === null || limit.resetsAt === null || snap.startedAt === null) return false
  const resetsAt = Date.parse(limit.resetsAt)
  return Number.isFinite(resetsAt) && snap.startedAt >= resetsAt - FIVE_HOURS_MS
}

/**
 * Percent of the 5-hour limit the whole session's spend `usd` is worth, or null when that would mislead: the
 * session began before this window (its spend is spread over more than one, so one window's rate does not
 * price it), or the share comes out over 100.
 */
export function sessionShare(snap: GlassSnap, usd: number): number | null {
  const share = limitShare(snap, usd)
  return share !== null && share <= 100 && startedInWindow(snap) ? share : null
}
