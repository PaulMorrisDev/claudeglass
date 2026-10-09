import type { GlassSensorEvent, GlassSnap } from '../types'
import { VERSION } from './generated/version.js'
import type { Io } from './io'
import { configDir } from './settings'

// The sensor file: what the mod showed and what followed, as closed words and
// times only (no text, paths or amounts). One file per session, rewritten whole
// at each turn's end when it changed. The Python side reads it
// (src/claudeglass/mod_sensor.py) and drops anything outside its vocabulary.

export const MAX_EVENTS = 500
const SESSION_ID = /^[A-Za-z0-9_-]{1,64}$/
/** A reply counts as a fresh start when its context is back near what a session starts with. */
const CLEAR_FACTOR = 1.3
/** How many replies after a note decide what the user did about it. */
const OUTCOME_REPLIES = 2

const SURFACES = ['desktop', 'terminal', 'vscode', 'mobile']

export function seconds(ms: number): number {
  return Math.floor(ms / 1000)
}

export function isSurface(word: string | null): word is string {
  return word !== null && SURFACES.includes(word)
}

/**
 * The events in time order, the newest MAX_EVENTS of them. A settled note carries the time it was
 * shown, so it can arrive after events that are newer; the Python side replaces the rows from the
 * file's first time on, so the file must start at the oldest row it keeps, and a row is dropped for
 * being old, not for having been added early.
 */
export function newestEvents(events: readonly GlassSensorEvent[]): GlassSensorEvent[] {
  const ordered = events.map((event, index) => ({ event, index })).sort((a, b) => a.event.ts - b.event.ts || a.index - b.index)
  return ordered.slice(-MAX_EVENTS).map(one => one.event)
}

export function addEvent(snap: GlassSnap, event: GlassSensorEvent): void {
  snap.events.push(event)
  if (snap.events.length > MAX_EVENTS) snap.events = newestEvents(snap.events)
  snap.eventsDirty = true
}

/** `present`, once per session, when the surface is one the vocabulary names. */
export function announcePresent(snap: GlassSnap, now: number): void {
  if (isSurface(snap.surface)) addEvent(snap, { ts: seconds(now), event: 'present', surface: snap.surface })
}

/** A note was shown; what followed is decided over the next replies. */
export function noteShown(snap: GlassSnap, kind: string, surface: string, now: number, ctx: number): void {
  snap.pending.push({ ts: seconds(now), kind, surface, replyIndex: snap.replies, compactions: snap.compactions.length, ctx })
}

/**
 * Decides the notes still waiting, after a reply: a summary since the note is
 * "compact", a context back near its start "clear", and two replies with
 * neither "sent".
 */
export function settleAfterReply(snap: GlassSnap, ctx: number): void {
  const startup = snap.startupFirst ?? snap.reading?.parts.startup ?? 0
  const waiting = []
  for (const note of snap.pending) {
    let outcome: string | null = null
    if (snap.compactions.length > note.compactions) outcome = 'compact'
    else if (startup > 0 && ctx <= startup * CLEAR_FACTOR && ctx < note.ctx * 0.8) outcome = 'clear'
    else if (snap.replies - note.replyIndex >= OUTCOME_REPLIES) outcome = 'sent'
    if (outcome === null) waiting.push(note)
    else addEvent(snap, { ts: note.ts, event: 'note', kind: note.kind, surface: note.surface, outcome })
  }
  snap.pending = waiting
}

/** The session is ending: a /clear settles every waiting note as "clear", anything else as "none". */
export function settleAtEnd(snap: GlassSnap, reason: string): void {
  const outcome = reason === 'clear' ? 'clear' : 'none'
  for (const note of snap.pending) {
    addEvent(snap, { ts: note.ts, event: 'note', kind: note.kind, surface: note.surface, outcome })
  }
  snap.pending = []
}

export function sensorFile(snap: GlassSnap): string | null {
  if (snap.sessionId === null || !SESSION_ID.test(snap.sessionId)) return null
  return JSON.stringify({ schema: 1, session_id: snap.sessionId, version: VERSION, events: newestEvents(snap.events) })
}

/** Rewrites the session's sensor file when its events changed. A failed write is tried again at the next turn. */
export async function writeSensor(io: Io, snap: GlassSnap): Promise<void> {
  if (!snap.eventsDirty) return
  const text = sensorFile(snap)
  if (text === null) return
  try {
    const dir = await configDir(io)
    await io.writeFile(`${dir}/mod/sensor/${snap.sessionId}.json`, text)
    snap.eventsDirty = false
  } catch {
    // Left dirty: the next turn's end writes it again.
  }
}
