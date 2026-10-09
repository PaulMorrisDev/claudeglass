import { atom, read } from 'claude-code'
import type { Register } from 'claude-code'

import { amountAfterAbout, minutes, withEst } from './fmt'
import { COPY } from './generated/copy.js'
import { emptySnap, flags } from './live'
import { coldMin, drawView, sendCostNow } from './view'
import type { View } from './view'
import { sendCost } from './lib/pricing.js'
import { fill, joinParts } from './lib/text.js'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)

// The line under the prompt, at the one moment a send can still be changed:
// once the cache has expired and the context is big, what sending will cost
// and what starting fresh would. In the detailed view a warm send says what it
// costs too.

/** The hint's one item for this moment, or null when there is nothing worth saying. */
export function hintLine(view: View): string | null {
  const { settings, snap, cache, tokens } = view
  if (cache === null || snap.lastTs === null || tokens <= 0) return null

  if (!cache.isWarm) {
    if (tokens < coldMin(settings)) return null
    flags.coldWarned = true
    const cold = fill(COPY['hint.cold'], { minutes: minutes(view.now - snap.lastTs), amount: amountAfterAbout(sendCostNow(view, false), settings) })
    const startup = view.parts?.startup ?? snap.startupFirst
    const fresh =
      startup !== null && startup > 0
        ? fill(COPY['hint.cold.clear'], { amount: amountAfterAbout(sendCost(view.rates, startup, false, view.ttl), settings) })
        : ''
    return joinParts([cold, fresh])
  }

  if (!view.detailed) return null
  return withEst(fill(COPY['hint.send'], { amount: amountAfterAbout(sendCostNow(view, true), settings) }), settings)
}

export const register: Register = on => {
  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    if (!e.props.isDraft || e.props.isWorking) return next(e)
    const line = hintLine(drawView(await read($, snapAtom), await read($, settingsAtom), await read($, detailedAtom), await $.clock.now(), false))
    if (line === null) return next(e)
    if (e.surface === 'terminal') return next({ ...e, props: { ...e.props, tail: line } })
    return next({ ...e, props: { ...e.props, hint: e.props.hint ? `${line} · ${e.props.hint}` : line } })
  })
}
