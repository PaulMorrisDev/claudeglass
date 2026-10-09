import type { GlassSnap } from '../types'
import type { Io } from './io'
import { live, touch } from './live'
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
/** Another session counts as running when its file was seen this recently (the capture hook's ten minutes). */
export const RUNNING_WITHIN_S = 600
/** The most files read in one look; older files are deleted after a day, so a folder rarely holds more. */
const MAX_LOOK = 40
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

/** When, and for which session, the folder was last looked at. */
let looked: { id: string; at: number } | null = null

/**
 * Whether another session's file was seen within ten minutes, put on the snapshot as `parallel` (limit.ts
 * gives no 5-hour share then). The folder is looked at once a minute at most; true when the snapshot changed.
 */
export async function checkParallel(io: Io, snap: GlassSnap): Promise<boolean> {
  const id = snap.sessionId
  if (id === null || io.listNames === undefined) return false
  const now = await io.now()
  if (looked !== null && looked.id === id && now >= looked.at && now - looked.at < BEAT_EVERY_MS) return false
  looked = { id, at: now }
  let others = false
  try {
    const dir = `${await configDir(io)}/mod/present`
    const names = (await io.listNames(dir)).filter(name => name.endsWith('.json') && PLAIN_ID.test(name.slice(0, -5)) && name.slice(0, -5) !== id)
    for (const name of names.slice(0, MAX_LOOK)) {
      const text = await io.readFile(`${dir}/${name}`)
      const raw: unknown = typeof text === 'string' && text !== '' ? JSON.parse(text) : null
      const seen = raw !== null && typeof raw === 'object' ? (raw as { seen?: unknown }).seen : null
      if (typeof seen === 'number' && seen > 0 && seconds(now) - seen <= RUNNING_WITHIN_S) {
        others = true
        break
      }
    }
  } catch {
    // Not looked at this time (no folder yet, or a file that isn't ours): the answer stands as it was.
    return false
  }
  const changed = snap.parallel !== others
  snap.parallel = others
  return changed
}

/** Marks the session as seen, at most once a minute unless `force`. */
export async function beat(io: Io, snap: GlassSnap, force = false): Promise<void> {
  if (snap.sessionId === null) return
  const now = await io.now()
  await checkParallel(io, snap)
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
        const snap = await live(io)
        await beat(io, snap, true)
        await touch(io)
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
  looked = null
  await write(io, id, 0)
}
