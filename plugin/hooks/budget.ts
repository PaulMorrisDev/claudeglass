import type { Register } from 'claude-code'

import type { GlassBudget, GlassSettings, GlassSnap } from '../types'
import { shortAmount } from './fmt'
import { COPY } from './generated/copy.js'
import { fill } from './lib/text.js'
import { limitShare } from './limit'
import { noteShown } from './sensor'
import { onTurnEnd } from './track'

// /glass budget <amount>: a budget for this session. Money (list-price dollars) on API billing, a
// share of the 5-hour limit on a subscription. The band's spend segment shows how far along the
// session is ("$0.38 of $2.00 budget"), and one note at the end of the reply that passes it says
// so, once. The budget lives in $.state (types/index.d.ts); a clear starts the spend over, and the
// note is told again for the new conversation.

const MAX_USD = 100000
const MAX_PCT = 100

/** How far along the session is against its budget, in the words the band writes. */
export type Progress = { spent: number; over: boolean; text: string; short: string; budget: string; used: string }

type Parsed = { ok: true; budget: GlassBudget } | { ok: false }

/** What the person typed after `budget`, as a budget for their billing mode: "2", "$2.50", "5", "5%". */
export function parseBudget(args: string, settings: GlassSettings): Parsed {
  const found = /^(\$?)\s*(\d+(?:\.\d+)?)\s*(%?)$/.exec(args.trim())
  if (found === null) return { ok: false }
  const value = Number(found[2])
  if (!Number.isFinite(value) || value <= 0) return { ok: false }
  if (settings.billing.mode === 'subscription') {
    if (found[1] === '$' || value > MAX_PCT) return { ok: false }
    return { ok: true, budget: { kind: 'pct', value } }
  }
  if (found[3] === '%' || value > MAX_USD) return { ok: false }
  return { ok: true, budget: { kind: 'usd', value } }
}

function pctWord(value: number): string {
  return `${value < 1 ? value.toFixed(2) : value.toFixed(1)}%`
}

/** The budget as the person set it: "$2.00", "5%". */
export function budgetLabel(budget: GlassBudget, settings: GlassSettings): string {
  return budget.kind === 'usd' ? shortAmount(budget.value, settings) : `${budget.value}%`
}

/**
 * Where the session stands against the budget, or null when that can't be told yet: a share of the 5-hour
 * limit is known once the limit has moved enough to say what a dollar is worth (limit.ts).
 */
export function budgetProgress(budget: GlassBudget, snap: GlassSnap, settings: GlassSettings): Progress | null {
  if (snap.cost === null) return null
  const spent = budget.kind === 'usd' ? snap.cost : limitShare(snap, snap.cost)
  if (spent === null) return null
  const used = budget.kind === 'usd' ? shortAmount(spent, settings) : pctWord(spent)
  const label = budgetLabel(budget, settings)
  const over = spent > budget.value
  return {
    spent,
    over,
    used,
    budget: label,
    text: fill(COPY[over ? 'band.budget.over' : 'band.budget'], { spent: used, budget: label }),
    short: fill(COPY[over ? 'band.budget.over.short' : 'band.budget.short'], { spent: used, budget: label }),
  }
}

/** The budget as the turn-end note last saw it: the command and the band keep it current (a render writes only module memory). */
let mirror: GlassBudget | null = null
/** The session the note was given for. */
let notedFor: string | null = null

/** Keeps the budget in module memory for the turn-end note. */
export function mirrorBudget(budget: GlassBudget | null): void {
  if (mirror !== null && budget !== null && mirror.kind === budget.kind && mirror.value === budget.value) return
  mirror = budget
  notedFor = null
}

/** What /glass budget needs from the engine, spelled out where `$` is held. */
export type BudgetKit = {
  get: () => Promise<GlassBudget | null>
  set: (budget: GlassBudget | null) => Promise<void>
  snap: () => Promise<GlassSnap>
  settings: () => Promise<GlassSettings>
}

/** Runs /glass budget: the line the command answers with. `args` is what follows the word `budget`. */
export async function runBudget(args: string, kit: BudgetKit): Promise<string> {
  const word = args.trim()
  const settings = await kit.settings()
  if (word === '') {
    const budget = await kit.get()
    if (budget === null) return COPY['budget.none']
    const progress = budgetProgress(budget, await kit.snap(), settings)
    const label = budgetLabel(budget, settings)
    return progress === null ? fill(COPY['budget.current.plain'], { budget: label }) : fill(COPY['budget.current'], { budget: label, spent: progress.used })
  }
  if (word.toLowerCase() === 'off') {
    await kit.set(null)
    mirrorBudget(null)
    return COPY['budget.off']
  }
  const parsed = parseBudget(word, settings)
  if (!parsed.ok) return COPY['budget.bad']
  await kit.set(parsed.budget)
  mirrorBudget(parsed.budget)
  return fill(COPY['budget.set'], { budget: budgetLabel(parsed.budget, settings) })
}

export const register: Register = () => {
  // One note, at the end of the reply that passes the budget; the conversation after a /clear is told again.
  onTurnEnd(async end => {
    const { io, snap, settings, now } = end
    const budget = mirror
    if (budget === null || settings.muted.includes('budget')) return
    if (snap.sessionId !== null && notedFor !== null && notedFor !== snap.sessionId) notedFor = null
    const progress = budgetProgress(budget, snap, settings)
    if (progress === null || !progress.over || notedFor !== null) return
    notedFor = snap.sessionId ?? ''
    io.note(`● ${COPY['note.lead']} · ${fill(COPY['note.budget'], { budget: progress.budget, spent: progress.used })}`)
    noteShown(snap, 'budget', 'log', now, snap.reading?.tokens ?? 0)
  })
}
