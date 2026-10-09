import type { Register } from 'claude-code'

import { planStatusShowing } from './coach-mem'
import { COPY } from './generated/copy.js'
import { minutes } from './fmt'
import { fill } from './lib/text.js'
import { onCompaction, onMeasure, onTurnEnd } from './track'
import type { TurnEnd } from './track'
import { buildView } from './view'
import type { View } from './view'

// The status line, for what can't wait: the 5-hour limit forecast to run out within
// 15 minutes, or one reply or none left before Claude summarises. It is set when
// either holds and cleared when neither does. The plan tip (coach.ts) has the line
// while it shows, and is never overwritten; this looks again at the next measurement,
// reply end or summary.

/** A 5-hour limit that runs out within this long is worth the status line. */
export const LIMIT_MS = 15 * 60 * 1000
/** This many replies or fewer before Claude summarises is too. */
export const REPLIES_LEFT = 1

/** What is on the status line because of this file; null when nothing is. */
let showing: string | null = null

/** The words for the urgent line, or null when nothing is urgent. The limit comes first. */
export function urgentLine(view: View): string | null {
  const { pace, replies, now } = view
  if (pace !== null && pace.runsOutAt - now <= LIMIT_MS) return fill(COPY['status.limit'], { minutes: minutes(pace.runsOutAt - now) })
  if (replies !== null && replies <= REPLIES_LEFT) return replies === 1 ? COPY['status.compact'] : COPY['status.compact.now']
  return null
}

async function refresh(end: TurnEnd): Promise<void> {
  const { io, snap, settings, now } = end
  if (io.status === undefined) return
  // The plan tip has the line: it owns it until it is cleared, and the next look sets this one afresh.
  if (planStatusShowing()) {
    showing = null
    return
  }
  const text = urgentLine(buildView(snap, settings, false, now, false))
  if (text === showing) return
  showing = text
  io.status(text ?? undefined)
}

/** Forgets what was shown (a test). */
export function resetUrgent(): void {
  showing = null
}

export const register: Register = () => {
  onMeasure(refresh)
  onTurnEnd(refresh)
  onCompaction(refresh)
}
