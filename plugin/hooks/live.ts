import type { GlassSnap } from '../types'
import type { Io } from './io'
import { fillMissing } from './lib/shape.js'

// What the mod has seen this session, kept in $.state so a hot reload keeps it
// and the render hooks (which may not write) can read it. The event hooks work
// on a copy in memory and write it back at most four times a second.

/** What a render hook leaves for the event hooks, which module memory carries (a render may not write $.state). */
export const flags = { coldWarned: false }

/** A write to $.state redraws what reads it; this is the least gap between writes. */
export const FLUSH_MS = 250

export function emptySnap(): GlassSnap {
  return {
    sessionId: null,
    surface: null,
    reading: null,
    cost: null,
    turnStartCost: null,
    lastReplyUsd: null,
    fiveHour: null,
    limitHit: false,
    calBase: null,
    parallel: false,
    reqs: [],
    startedAt: null,
    toolsUsed: [],
    firstCr: null,
    startupFirst: null,
    model: null,
    history: [],
    replies: 0,
    replyCosts: [],
    lastTs: null,
    compactions: [],
    pendingCompaction: false,
    toolKept: 0,
    heaviest: [],
    switches: 0,
    mix: { read: 0, write: 0, input: 0, output: 0 },
    rest: { hints: {} },
    lastNoteReply: null,
    turnReqs: 0,
    turnAt: null,
    turnId: null,
    turnUsd: 0,
    turnExact: false,
    turn: emptyTurn(),
    events: [],
    pending: [],
    eventsDirty: false,
    lastBeat: 0,
    agents: [],
    piece: { tokens: 0, noted: false, surveying: false },
  }
}

/**
 * The snapshot as this version reads it. $.state outlives a reload of the mod, so one an older
 * version wrote can lack fields added since: those come from an empty snapshot.
 */
export function soundSnap(snap: GlassSnap | null | undefined): GlassSnap {
  return fillMissing(snap, emptySnap())
}

export function emptyTurn(): GlassSnap['turn'] {
  return { rebuilds: [], big: null, switched: false, compactedCold: null, compacted: false, switchUsd: 0 }
}

let work: GlassSnap | null = null
let lastFlush = Number.NEGATIVE_INFINITY
let scheduled = false

/** The snapshot to work on: the copy in memory, or the one in $.state after a reload. */
export async function live(io: Io): Promise<GlassSnap> {
  // What $.state returns is read-only; the event hooks change theirs, so they work on a copy (the snapshot is plain JSON).
  if (work === null) work = JSON.parse(JSON.stringify(soundSnap(await io.getSnap()))) as GlassSnap
  return work
}

/** Starts the snapshot over (a new session). */
export async function restart(io: Io, keep: Partial<GlassSnap> = {}): Promise<GlassSnap> {
  work = { ...emptySnap(), ...keep }
  await flushNow(io)
  return work
}

export async function flushNow(io: Io): Promise<void> {
  if (work === null) return
  const snap = work
  lastFlush = await io.now()
  await io.putSnap(snap)
}

/** Writes the snapshot to $.state now when the last write was long enough ago, else a little later. */
export async function touch(io: Io): Promise<void> {
  if (scheduled) return
  scheduled = true
  try {
    const now = await io.now()
    const wait = lastFlush + FLUSH_MS - now
    if (wait <= 0) {
      await flushNow(io)
      scheduled = false
      return
    }
    io.after(wait, () => {
      scheduled = false
      void flushNow(io)
    })
  } catch (error) {
    scheduled = false
    throw error
  }
}

/** Forgets the copy in memory, so the next read comes from $.state (a test, or a reload). */
export function forget(): void {
  work = null
  lastFlush = Number.NEGATIVE_INFINITY
  scheduled = false
}
