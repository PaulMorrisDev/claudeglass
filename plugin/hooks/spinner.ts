import { atom, read } from 'claude-code'
import type { Register } from 'claude-code'

import { isVisibleChange, percent, shortAmount } from './fmt'
import { COPY } from './generated/copy.js'
import { fill } from './lib/text.js'
import { emptySnap } from './live'
import { drawView } from './view'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)

// While Claude works: how full the context is, and what this turn has cost so
// far. On a subscription only the first, since a dollar change means little there.

export const register: Register = on => {
  on('ui.render', { component: 'Spinner' }, async ($, e, next) => {
    const view = drawView(await read($, snapAtom), await read($, settingsAtom), await read($, detailedAtom), await $.clock.now(), true)
    const { snap, settings } = view
    if (snap.reading === null) return next(e)

    const pct = percent(snap.reading.pct)
    const spent = snap.cost !== null && snap.turnStartCost !== null ? snap.cost - snap.turnStartCost : null
    const text =
      settings.billing.mode === 'subscription' || !isVisibleChange(spent)
        ? fill(COPY['band.full'], { pct })
        : fill(COPY['spinner.suffix'], { pct, amount: shortAmount(spent, settings) })
    return next({ ...e, props: { ...e.props, suffix: `${e.props.suffix} · ${text}` } })
  })
}
