import type { GlassSettings, GlassSnap } from '../types'

// The engine, as the mod's event side sees it. Claude Code follows `$` only
// within the file that receives it, so track.ts builds this from `$` (every
// call spelled out there) and hands it to the files that do the work. Nothing
// here is imported at run time.

type Handle = { cancel: () => void }

export type Io = {
  now: () => Promise<number>
  after: (ms: number, run: () => void) => Handle
  every: (ms: number, run: () => void) => Handle
  /** Draws every ui.render hook again. */
  redraw: () => void
  /** A line for the debug log. */
  debug: (text: string) => void
  /** A line in the transcript. */
  note: (text: string) => void
  /** The variables that say where the config folder is. */
  places: () => Promise<{ configDir?: string; userProfile?: string; home?: string }>
  /** The file's text, or null when it can't be read. */
  readFile: (path: string) => Promise<string | null>
  writeFile: (path: string, text: string) => Promise<void>
  getSnap: () => Promise<GlassSnap>
  putSnap: (snap: GlassSnap) => Promise<void>
  getSettings: () => Promise<GlassSettings | null>
  putSettings: (settings: GlassSettings) => Promise<void>
  getDetailed: () => Promise<boolean>
  /** The names of the files in a folder (track.ts's engine only; absent where a file doesn't need it). */
  listNames?: (path: string) => Promise<string[]>
  /** Sets the status line, or clears it with undefined (track.ts's engine only). */
  status?: (text: string | undefined) => void
}
