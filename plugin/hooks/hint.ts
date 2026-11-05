import { atom, read } from 'claude-code'
import type { Register } from 'claude-code'

import { amountOfAfterAbout, minutes, withEst } from './fmt'
import { COPY } from './generated/copy.js'
import { emptySnap, flags } from './live'
import { chooseHint, hintText } from './coach-mem'
import { draftLine } from './draft'
import { drawView, sendCostNow } from './view'
import type { View } from './view'
import { sendCost } from './lib/pricing.js'
import { fill, joinParts } from './lib/text.js'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)

// The line under the prompt, at the one moment a send can still be changed.
// One item at most: the first of drip_feed, big_paste, status_poll and
// cold_return that applies (coach-mem.ts, under the capture hook's rest, mute
// and once rules), the cold-cache warning being cold_return with what sending
// will cost and what starting fresh would. In the detailed view a warm send
// says what it costs too, when nothing else needs saying.

/** What sending will cost over a cold cache, and what a fresh start would. */
function coldLine(view: View): string {
  const { settings, snap } = view
  flags.coldWarned = true
  const cold = fill(COPY['hint.cold'], { minutes: minutes(view.now - (snap.lastTs ?? view.now)), amount: amountOfAfterAbout(sendCostNow(view, false), snap, settings) })
  const startup = view.parts?.startup ?? snap.startupFirst
  const fresh =
    startup !== null && startup > 0
      ? fill(COPY['hint.cold.clear'], { amount: amountOfAfterAbout(sendCost(view.rates, startup, false, view.ttl), snap, settings) })
      : ''
  return joinParts([cold, fresh])
}

/** The hint's one item for this moment, or null when there is nothing worth saying. */
export function hintLine(view: View, surface = 'terminal'): string | null {
  const { settings, snap, cache, tokens } = view
  const found = chooseHint(view)
  if (found !== null) return found.kind === 'cold_return' ? coldLine(view) : hintText(found, surface)
  // The message in the box, when nothing above has anything to say (draft.ts).
  const drafted = draftLine(view)
  if (drafted !== null) return drafted
  if (cache === null || snap.lastTs === null || tokens <= 0 || !cache.isWarm || !view.detailed) return null
  return withEst(fill(COPY['hint.send'], { amount: amountOfAfterAbout(sendCostNow(view, true), snap, settings) }), settings)
}

export const register: Register = on => {
  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    if (!e.props.isDraft || e.props.isWorking) return next(e)
    const line = hintLine(drawView(await read($, snapAtom), await read($, settingsAtom), await read($, detailedAtom), await $.clock.now(), false), e.surface)
    if (line === null) return next(e)
    if (e.surface === 'terminal') return next({ ...e, props: { ...e.props, tail: line } })
    return next({ ...e, props: { ...e.props, hint: e.props.hint ? `${line} · ${e.props.hint}` : line } })
  })
}
