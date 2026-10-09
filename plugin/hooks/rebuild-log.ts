import type { Register } from 'claude-code'

import { amountOfAfterAbout, formatTokens, withEst } from './fmt'
import { COPY } from './generated/copy.js'
import { fill } from './lib/text.js'
import { limitShare } from './limit'
import { onTurnEnd } from './track'
import type { TurnEnd } from './track'

// The rebuild line: one line in the transcript when a reply paid to write the
// context again and it was not expected. An expected one (after a summary, or a
// usage-limit wait) is written only in the detailed view.

/** A rebuild below this many dollars is not worth a line. */
export const REBUILD_LINE_USD = 0.05
/** On a subscription, nor one below this share of the 5-hour limit. */
export const REBUILD_LINE_LIMIT_PCT = 1

async function write(end: TurnEnd): Promise<void> {
  const { io, snap, settings } = end
  const rebuilds = snap.turn.rebuilds
  if (rebuilds.length === 0) return
  const detailed = await io.getDetailed()
  const subscription = settings.billing.mode === 'subscription'

  let pick = null
  for (const one of rebuilds) {
    if (one.expected) {
      if (!detailed) continue
    } else {
      const share = subscription ? limitShare(snap, one.usd) : null
      const big = share !== null ? share >= REBUILD_LINE_LIMIT_PCT : one.usd >= REBUILD_LINE_USD
      if (!big) continue
    }
    if (pick === null || one.usd > pick.usd) pick = one
  }
  if (pick === null) return

  const cause = COPY[`cause.${pick.cause}` as keyof typeof COPY]
  const amount = withEst(amountOfAfterAbout(pick.usd, snap, settings), settings)
  const line = pick.expected
    ? fill(COPY['log.rebuild.expected'], { cause, amount })
    : fill(COPY['log.rebuild'], { tokens: formatTokens(pick.tokens), amount, cause })
  io.note(line)
}

export const register: Register = () => {
  onTurnEnd(write)
}
