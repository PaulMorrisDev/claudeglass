import type { Register } from 'claude-code'

import { planStatusShowing } from './coach-mem'
import { COPY } from './generated/copy.js'
import { minutes } from './fmt'
import type { Io } from './io'
import { live } from './live'
import { fill } from './lib/text.js'
import { getSettings } from './settings'
import { onCompaction, onMeasure, onTurnEnd } from './track'
import type { TurnEnd } from './track'
import { buildView } from './view'
import type { View } from './view'

// The status line, for what can't wait: the 5-hour limit forecast to run out within
// 15 minutes, or one reply or none left before Claude summarises. It is set when
// either holds and cleared when neither does. The plan tip (coach.ts) has the line
// while it shows, and is never overwritten; this looks again at the next measurement,
// reply end or summary. While the limit is on the line it also looks again every minute
// (and at the limit's reset), for the minutes it names go on while the session is idle;
// the line goes when the session ends or is cleared.

/** A 5-hour limit that runs out within this long is worth the status line. */
export const LIMIT_MS = 15 * 60 * 1000
/** This many replies or fewer before Claude summarises is too. */
export const REPLIES_LEFT = 1

/** How often the limit's minutes on the line are looked at again while nothing else is happening. */
export const LOOK_EVERY_MS = 60 * 1000

/** What is on the status line because of this file; null when nothing is. */
let showing: string | null = null

/** The look at the limit that is waiting its turn, if one is. */
let timer: { cancel: () => void } | null = null

function stopLooking(): void {
  timer?.cancel()
  timer = null
}

/** The words for the urgent line, or null when nothing is urgent. The limit comes first. */
export function urgentLine(view: View): string | null {
  const { pace, replies, now } = view
  if (pace !== null && pace.runsOutAt - now <= LIMIT_MS) return fill(COPY['status.limit'], { minutes: minutes(pace.runsOutAt - now) })
  if (replies !== null && replies <= REPLIES_LEFT) return replies === 1 ? COPY['status.compact'] : COPY['status.compact.now']
  return null
}

async function refresh(end: TurnEnd): Promise<void> {
  const { io, snap, settings, now } = end
  stopLooking()
  if (io.status === undefined) return
  // The plan tip has the line: it owns it until it is cleared, and the next look sets this one afresh.
  if (planStatusShowing()) {
    showing = null
    return
  }
  const view = buildView(snap, settings, false, now, false)
  const text = urgentLine(view)
  // The limit's minutes (and its reset) move on without any event: look again in a minute, or at the reset if that is sooner.
  if (view.pace !== null && view.pace.runsOutAt - now <= LIMIT_MS) {
    const wait = Math.max(1000, Math.min(LOOK_EVERY_MS, view.pace.resetsAt - now + 1000))
    timer = io.after(wait, () => {
      void lookAgain(io)
    })
  }
  if (text === showing) return
  showing = text
  io.status(text ?? undefined)
}

async function lookAgain(io: Io): Promise<void> {
  timer = null
  try {
    await refresh({ io, snap: await live(io), settings: await getSettings(io), now: await io.now() })
  } catch {
    // Looked at again at the next event.
  }
}

/** Forgets what was shown, and stops looking again (a test, or the plan tip taking the line). */
export function resetUrgent(): void {
  showing = null
  stopLooking()
}

export const register: Register = on => {
  onMeasure(refresh)
  onTurnEnd(refresh)
  onCompaction(refresh)
  // The session that ended (or was cleared) takes its line with it, and nobody looks at its limit again.
  // (A matcher, apart from track.ts's hook on the event.)
  on('session.end', { reason: /[\s\S]*/ }, async ($, e, next) => {
    stopLooking()
    if (showing !== null) {
      showing = null
      try {
        $.ui.status(undefined)
      } catch {
        // The line goes with the session.
      }
    }
    return next(e)
  })
}
