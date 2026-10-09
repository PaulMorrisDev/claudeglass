import type { Register } from 'claude-code'

import { ratingCandidate } from './coach-mem'
import { amountOfAfterAbout, formatTokens, withEst } from './fmt'
import { COPY } from './generated/copy.js'
import { THRESHOLDS } from './generated/coaching.js'
import { mayShow, noteAllowed, recordShown } from './lib/rest.js'
import type { RestThresholds } from './lib/rest.js'
import { sendCost } from './lib/pricing.js'
import { fill } from './lib/text.js'
import { noteShown, seconds } from './sensor'
import { onTurnEnd } from './track'
import type { TurnEnd } from './track'
import { buildView } from './view'

// Turn-end notes: at most one every five replies, one sentence with one action,
// under the rest rules the capture hook uses. They go to the transcript with
// $.ui.log, because turn.complete's text is the model's answer, not a place for
// the mod's own words. The kinds are the ones only the mod writes
// (MOD_NOTE_KINDS); "effort" is left out because usage carries no thinking tokens.

/** The most replies after which a "replies left" note is worth writing. */
export const RUNWAY_NOTE_REPLIES = 3
/** The context a model switch has to be carrying to be worth a note. */
export const SWITCH_NOTE_TOKENS = 50000
/** At most one note per this many replies. */
export const NOTE_EVERY = 5

type Candidate = { kind: string; stake: number; text: string; shown?: () => Promise<void> }

function candidates(end: TurnEnd): Candidate[] {
  const { snap, settings, now } = end
  const view = buildView(snap, settings, false, now, false)
  const found: Candidate[] = []

  const cold = snap.turn.compactedCold
  if (cold !== null) {
    const tokens = cold.tokensAfter ?? view.tokens
    const usd = sendCost(view.rates, tokens, false, view.ttl)
    found.push({ kind: 'cold_compaction', stake: usd, text: fill(COPY['note.cold.compaction'], { amount: withEst(amountOfAfterAbout(usd, snap, settings), settings) }) })
  }

  const left = view.replies
  if (left !== null && left <= RUNWAY_NOTE_REPLIES) {
    found.push({ kind: 'runway', stake: view.tokens, text: left < 1 ? COPY['note.runway.none'] : left === 1 ? COPY['note.runway.one'] : fill(COPY['note.runway'], { replies: left }) })
  }

  if (snap.turn.switched && snap.switches >= 2 && view.tokens > SWITCH_NOTE_TOKENS) {
    const usd = snap.turn.switchUsd
    found.push({ kind: 'switch', stake: usd, text: fill(COPY['note.switch'], { amount: withEst(amountOfAfterAbout(usd, snap, settings), settings) }) })
  }

  const big = snap.turn.big
  if (big !== null) {
    const tool = big.name.startsWith('mcp__') ? COPY['tool.mcp'] : big.name
    found.push({ kind: 'result', stake: big.tokens, text: fill(COPY['note.result'], { tool, tokens: formatTokens(big.tokens) }) })
  }

  if (snap.turn.compacted && snap.compactions.length >= 2) {
    found.push({ kind: 'compactions', stake: snap.compactions.length, text: fill(COPY['note.compactions'], { count: snap.compactions.length }) })
  }
  return found
}

async function deliver(end: TurnEnd): Promise<void> {
  const { io, snap, settings, now } = end
  if (!noteAllowed(snap.lastNoteReply, snap.replies, NOTE_EVERY)) return
  const th = { ...THRESHOLDS, ...settings.thresholds } as unknown as RestThresholds
  const nowS = seconds(now)
  const found = candidates(end)
  // The rating reminder (coach-mem.ts) comes last: it is not about this reply.
  const rating = await ratingCandidate(io, snap, settings, now)
  if (rating !== null) found.push(rating)
  for (const one of found) {
    if (settings.muted.includes(one.kind)) continue
    if (!mayShow(snap.rest, one.kind, one.stake, nowS, th)) continue
    snap.rest = recordShown(snap.rest, one.kind, one.stake, nowS, th, settings.once.includes(one.kind))
    snap.lastNoteReply = snap.replies
    io.note(`● ${COPY['note.lead']} · ${one.text}`)
    noteShown(snap, one.kind, 'log', now, snap.reading?.tokens ?? 0)
    await one.shown?.()
    return
  }
}

export const register: Register = () => {
  onTurnEnd(deliver)
}
