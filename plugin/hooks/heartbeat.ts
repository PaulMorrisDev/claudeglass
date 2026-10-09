import type { GlassSnap } from '../types'
import type { Io } from './io'
import { seconds } from './sensor'
import { configDir } from './settings'

// present.json: {"schema": 1, "sessions": {"<session id>": <epoch seconds>}},
// read and written whole when a session starts and at most once a minute
// after, so the capture hook can tell the mod is running for this session and
// leave the notes the mod delivers to it. Entries older than a day are dropped.

export const BEAT_EVERY_MS = 60 * 1000
export const KEEP_MS = 24 * 60 * 60 * 1000

/** present.json's content after `id` was seen at `now`, from the file's text as it was (or null). */
export function presentFile(text: string | null, id: string, now: number): string {
  const sessions: Record<string, number> = {}
  if (text !== null) {
    try {
      const raw: unknown = JSON.parse(text)
      const old = raw !== null && typeof raw === 'object' ? (raw as { sessions?: unknown }).sessions : null
      if (old !== null && typeof old === 'object') {
        for (const [key, value] of Object.entries(old as Record<string, unknown>)) {
          if (typeof value === 'number' && Number.isFinite(value) && now - value * 1000 <= KEEP_MS) sessions[key] = value
        }
      }
    } catch {
      // A file that doesn't parse is started over.
    }
  }
  sessions[id] = seconds(now)
  return JSON.stringify({ schema: 1, sessions })
}

/** Marks the session as seen, at most once a minute unless `force`. */
export async function beat(io: Io, snap: GlassSnap, force = false): Promise<void> {
  if (snap.sessionId === null) return
  const now = await io.now()
  if (!force && now - snap.lastBeat < BEAT_EVERY_MS) return
  snap.lastBeat = now
  try {
    const path = `${await configDir(io)}/mod/present.json`
    let text: string | null = null
    try {
      text = await io.readFile(path)
    } catch {
      text = null
    }
    await io.writeFile(path, presentFile(text, snap.sessionId, now))
  } catch {
    // The heartbeat is a courtesy; a failed write is tried again in a minute.
  }
}
