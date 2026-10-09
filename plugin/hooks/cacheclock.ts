import type { GlassSnap } from '../types'
import type { Io } from './io'

// The band's "cheap for 0:45 more" and the hint's cold warning change with the
// clock, not with an event. This wakes the drawing every few seconds, and only
// from the last fifth of the cache's lifetime until it has run out.

export const WINDOW = 0.2
export const TICK_MS = 5000

type Handle = { cancel: () => void }

let waiting: Handle | null = null
let ticking: Handle | null = null

export function stopCacheClock(): void {
  waiting?.cancel()
  ticking?.cancel()
  waiting = null
  ticking = null
}

/** Arms the wake-ups for the cache a reply just wrote, counted from `lastTs` for `ttlSeconds`. */
export async function armCacheClock(io: Io, snap: GlassSnap, ttlSeconds: number): Promise<void> {
  stopCacheClock()
  if (snap.lastTs === null || !(ttlSeconds > 0)) return
  const ttlMs = ttlSeconds * 1000
  const endsAt = snap.lastTs + ttlMs
  const opensAt = endsAt - ttlMs * WINDOW
  const now = await io.now()
  if (now >= endsAt + TICK_MS) return
  waiting = io.after(Math.max(0, opensAt - now), () => {
    io.redraw()
    ticking = io.every(TICK_MS, () => {
      io.redraw()
      void io.now().then(late => {
        if (late >= endsAt + TICK_MS) stopCacheClock()
      })
    })
  })
}
