import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { GlassReview } from '../types'
import { emptyReview, sound } from './compaction-memory'
import { settleReceipts } from './lib/receipt.js'
import { onTurnEnd } from './track'

// The receipt of a re-add: whether the replies that followed referenced the items put back. At the end of
// each main reply, what it said and the tool inputs it used (track.ts holds them in memory) are checked
// against the items still waiting; only the count and the verdict reach $.state, never the texts.

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const reviewAtom = atom({ plugin: 'claudeglass', key: 'review' } as const, emptyReview())

/** The texts of the reply that has just ended, waiting for the turn.complete hook below (it holds `$`, which a listener does not). */
let ended: string[] | null = null

export const register: Register = on => {
  // A listener is given no `$`, so it only passes the texts on; the hook settles the receipts.
  onTurnEnd(async ({ texts, answer }) => {
    // The final answer is the text most likely to name an item, so it is kept even when a long reply filled the limits.
    ended = [...(texts ?? []), answer ?? '']
  })

  on('turn.complete', { answer: /[\s\S]*/ }, async ($, e, next) => {
    const result = await next(e)
    // A subagent's turn can end while the main reply's bookkeeping is still running: its hook must not take the texts.
    if (e.agentId !== undefined) return result
    const texts = ended
    ended = null
    if (texts === null) return result
    try {
      const review = sound(await read($, reviewAtom))
      if (settleReceipts(review, texts) !== review) await update($, reviewAtom, (was): GlassReview => settleReceipts(sound(was), texts))
    } catch (error) {
      $.ui.log(`claudeglass: receipt ${String(error)}`, { to: 'debug' })
    }
    return result
  })
}
