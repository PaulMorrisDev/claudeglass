import type { On } from 'claude-code'

import type { GlassSettings, GlassSnap } from '../types'
import { amount, amountHasShare, amountOfAfterAbout, isVisibleChange, shortAmount } from '../hooks/fmt'
import { fill } from '../hooks/lib/text.js'
import { coldMin, drawView, sendCostNow } from '../hooks/view'
import type { View } from '../hooks/view'
import type { World } from './support'

// The cost and the cold cache are no longer on the band (it is one health line), but the mod still keeps
// them in its snapshot for the dashboard and the hint. These read the snapshot the way the band once drew
// it, so the tests of what the snapshot holds (the cost of a reply, the share of the 5-hour limit) keep
// their figures and their words.

// The band's old words for these figures are no longer the mod's copy; the tests write them here.
const COST = '{amount} this session'
const CHANGE = '(+{amount})'
const COLD = 'cache expired (est.): the next send costs about {amount}'
const COLD_PLAIN = 'cache expired (est.): the next send rewrites the context'

/** The snapshot and settings the mod last wrote to its state. */
export type Watched = { readonly snap: GlassSnap | null; readonly settings: GlassSettings | null }

export function watchState(on: On): Watched {
  let snap: GlassSnap | null = null
  let settings: GlassSettings | null = null
  on('state.set', async (_$, e, next) => {
    if (e.plugin === 'claudeglass' && e.key === 'snap') snap = e.value as GlassSnap
    if (e.plugin === 'claudeglass' && e.key === 'settings') settings = e.value as GlassSettings
    return next(e)
  })
  return {
    get snap() {
      return snap
    },
    get settings() {
      return settings
    },
  }
}

function viewOf(seen: Watched, world: World): View {
  return drawView(seen.snap as GlassSnap, seen.settings, false, world.clock.now(), false)
}

/** What the session has cost and what the last reply added, as the band once wrote it: "$1.17 this session (+$0.67)". */
export function spendText(seen: Watched, world: World): string | undefined {
  const { snap, settings } = viewOf(seen, world)
  if (snap.cost === null) return undefined
  const change = snap.lastReplyUsd
  const added = isVisibleChange(change) ? ` ${fill(CHANGE, { amount: shortAmount(change, settings) })}` : ''
  return `${fill(COST, { amount: amount(snap.cost, settings) })}${added}`
}

/** What the next send costs once the cache has expired over a big context, as the band once wrote it; undefined while the cache is warm. */
export function coldText(seen: Watched, world: World): string | undefined {
  const view = viewOf(seen, world)
  const { cache, settings, snap, tokens } = view
  if (cache === null || cache.isWarm || tokens < coldMin(settings)) return undefined
  const usd = sendCostNow(view, false)
  if (!amountHasShare(usd, snap, settings)) return COLD_PLAIN
  return fill(COLD, { amount: amountOfAfterAbout(usd, snap, settings) })
}
