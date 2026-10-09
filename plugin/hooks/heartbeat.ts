import type { GlassSnap } from '../types'
import type { Io } from './io'
import { live } from './live'
import { seconds } from './sensor'
import { configDir } from './settings'

// <config dir>/mod/present/<session id>.json:
//   {"schema": 1, "session_id": "<id>", "seen": <epoch seconds>}
// One file per session, written whole when the session starts and again every
// minute while it runs, so the capture hook can tell the mod is running for this
// session and leave the notes the mod delivers to it (it counts a file seen within
// ten minutes). The engine can't remove a file, so when the session ends the file
// is written once more with `seen` at 0, which reads as not running; ClaudeGlass
// deletes files a day old.

export const BEAT_EVERY_MS = 60 * 1000
/** The ids the capture hook looks for: letters, digits and hyphens. Anything else writes nothing. */
const PLAIN_ID = /^[A-Za-z0-9-]{1,64}$/

type Handle = { cancel: () => void }

let timer: Handle | null = null

/** The file's content for a session seen at `seen` (epoch seconds). */
export function presentFile(id: string, seen: number): string {
  return JSON.stringify({ schema: 1, session_id: id, seen })
}

async function write(io: Io, id: string, seen: number): Promise<void> {
  if (!PLAIN_ID.test(id)) return
  try {
    await io.writeFile(`${await configDir(io)}/mod/present/${id}.json`, presentFile(id, seen))
  } catch {
    // The heartbeat is a courtesy; a failed write is tried again in a minute.
  }
}

/** Marks the session as seen, at most once a minute unless `force`. */
export async function beat(io: Io, snap: GlassSnap, force = false): Promise<void> {
  if (snap.sessionId === null) return
  const now = await io.now()
  if (!force && now - snap.lastBeat < BEAT_EVERY_MS) return
  snap.lastBeat = now
  await write(io, snap.sessionId, seconds(now))
}

/** Writes the heartbeat every minute from now on, for whichever session is current, until `leave`. Starts over if already running. */
export function keepBeating(io: Io): void {
  stopBeating()
  timer = io.every(BEAT_EVERY_MS, () => {
    void (async () => {
      try {
        await beat(io, await live(io), true)
      } catch {
        // Tried again in a minute.
      }
    })()
  })
}

export function stopBeating(): void {
  timer?.cancel()
  timer = null
}

/** The session is over: the timer stops and the file says it is no longer seen. */
export async function leave(io: Io, id: string): Promise<void> {
  stopBeating()
  await write(io, id, 0)
}
