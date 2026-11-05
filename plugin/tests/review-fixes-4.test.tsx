import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { coldText, spendText, watchState } from './figures'
import type { Watched } from './figures'
import { faults, prepare, reply, SESSION, settingsFile, START, T0 } from './support'
import type { Step, World } from './support'

// The fourth round of review fixes: the 5-hour share while another session is idle or has gone quiet, the cost of the
// first reply after a resume, a turn that makes no request, a measure that arrives part way through a reply, the urgent
// status line after the plan tip, and the urgent line going stale.

const MINUTE = 60 * 1000
const NOW_S = Math.floor(T0 / 1000)
const SUBSCRIPTION = settingsFile({ billing: { mode: 'subscription', share_per_usd: null, period_label: 'weekly usage limit', basis: 'list' } })

// One reply that leaves the context at 120k of a 160k summarising point, $0.50 spent.
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
// A reply priced exactly: 120k read at $0.30 per million and 20k written out at $15 per million is $0.336.
const PRICED: Step = { cacheRead: 120000, cacheWrite: 0, output: 20000 }
const PRICED_USD = 0.336
const CONTEXT = PRICED.cacheRead + PRICED.cacheWrite + PRICED.output
const SHARE = /\d+\.\d% of your 5-hour limit/

/** How many replies the snapshot counts for the main session. */
async function repliesCounted(seen: Watched, world: World): Promise<number | undefined> {
  // The snapshot reaches $.state at most four times a second.
  await world.clock.advance(300)
  return seen.snap?.replies
}

function limit(world: World, pct: number, resetsAt: string): void {
  world.limits = [{ kind: 'five_hour', percentUsed: pct, resetsAt }]
}

function present(world: World, id: string, seen: number, active?: number): void {
  world.files.set(`/cfg/claudeglass/mod/present/${id}.json`, JSON.stringify({ schema: 1, session_id: id, seen, ...(active === undefined ? {} : { active }) }))
}

/** A main request of reply `turnId`, as the engine runs one; `messageCount` 1 is a conversation's first message. */
async function mainStep($: Engine, world: World, turnId: string, index: number, step: Step, messageCount = 2 + index): Promise<void> {
  world.next = { usage: step, tools: step.tools ?? [] }
  const stream = $.turn.step({ turnId, index, model: step.model ?? 'claude-sonnet-4-6', messageCount } as never)
  for await (const chunk of stream) void chunk
  await stream.result
}

async function measure($: Engine, world: World, usd: number): Promise<void> {
  world.usd = usd
  world.figures(CONTEXT, usd)
  await $.session.measure({ context: { tokens: CONTEXT, window: 200000, percent: 70 }, rateLimits: world.limits, cost: { usd }, changed: ['context', 'cost'] } as never)
}

async function complete($: Engine, world: World, turnId: string, reason = 'answer'): Promise<void> {
  await $.turn.complete({ turnId, reason, answer: 'done', isAborted: reason === 'aborted', durationMs: 1000 } as never)
  await world.clock.advance(300)
}

// -- D1 ------------------------------------------------------------------------------------------------------------

/** Two replies in one window, the limit moving 10% to 15% for a dollar and a half, then a cold cache: the cache segment the band no longer draws. */
async function coldSegment($: Engine, world: World, seen: Watched, resetsAt: string): Promise<string | undefined> {
  limit(world, 10, resetsAt)
  await $.session.start(START)
  await reply($, world, FIRST)
  limit(world, 15, resetsAt)
  await reply($, world, { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.5 })
  await world.clock.advance(400 * 1000)
  return coldText(seen, world)
}

test('D1: another session that is open but idle (its last request long ago) leaves the 5-hour share as it was', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  const seen = watchState(on)
  const resetsAt = new Date(T0 + 4 * 60 * MINUTE).toISOString()
  // Its heartbeat is fresh (the process is open), but it last sent a request an hour ago.
  present(world, 'session-other', NOW_S - 30, NOW_S - 3600)
  const cold = await coldSegment($, world, seen, resetsAt)
  expect(cold).toMatch(/^cache expired \(est\.\): the next send costs about \d+\.\d+% of your 5-hour limit$/)
  expect(faults(world)).toEqual([])
})

test('D1: another session whose last request was within ten minutes splits the limit: no share', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  const seen = watchState(on)
  const resetsAt = new Date(T0 + 4 * 60 * MINUTE).toISOString()
  present(world, 'session-other', NOW_S - 30, NOW_S - 120)
  const cold = await coldSegment($, world, seen, resetsAt)
  expect(cold).toMatch(/^cache expired \(est\.\)/)
  expect(cold).not.toMatch(SHARE)
  expect(faults(world)).toEqual([])
})

test('D1: when the other session goes quiet the share starts over, so its points do not count as this session\'s', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  const seen = watchState(on)
  const resetsAt = new Date(T0 + 4 * 60 * MINUTE).toISOString()
  present(world, 'session-other', NOW_S, NOW_S)
  limit(world, 10, resetsAt)
  await $.session.start(START)
  await reply($, world, FIRST)
  // The other session works: the limit climbs 30 points in a reply that cost this one $0.50.
  limit(world, 40, resetsAt)
  await reply($, world, { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.5 })

  // Its heartbeat goes on, but its last request is now long ago; a minute later the look finds it quiet.
  await world.clock.advance(11 * MINUTE)
  present(world, 'session-other', NOW_S + 11 * 60, NOW_S)
  await world.clock.advance(2 * MINUTE)
  limit(world, 42, resetsAt)
  await reply($, world, { steps: [{ cacheRead: 121000, cacheWrite: 1000, output: 200 }], usd: 0.5 })
  await world.clock.advance(400 * 1000)
  // 2 points for $0.50 is about 4% a dollar. Kept from the first base it would be 32 points for $1.00, eight times that.
  const share = /about (\d+\.\d)% of your 5-hour limit/.exec((coldText(seen, world)) ?? '')
  expect(share).not.toBeNull()
  expect(Number(share?.[1])).toBeLessThan(4)
  expect(faults(world)).toEqual([])
})

// -- D2 ------------------------------------------------------------------------------------------------------------

test('D2: the first reply after a resume is its own estimate, not the whole session\'s cost; the next is exact', async ($, on) => {
  const world = prepare(on)
  const seen = watchState(on)
  await $.session.start(START)
  // A resumed conversation: the first request already holds earlier messages, and Claude Code's ledger holds the earlier cost.
  await $.turn.start({ text: 'go on', turnId: 'resumed' })
  await mainStep($, world, 'resumed', 0, PRICED, 9)
  await measure($, world, 42)
  await complete($, world, 'resumed')
  expect(spendText(seen, world)).toBe('$42.0 this session (+$0.34)')

  await $.turn.start({ text: 'again', turnId: 'again' })
  await mainStep($, world, 'again', 0, PRICED)
  await measure($, world, 42.5)
  await complete($, world, 'again')
  expect(spendText(seen, world)).toBe('$42.5 this session (+$0.50)')
  expect(faults(world)).toEqual([])
})

// -- D3 ------------------------------------------------------------------------------------------------------------

test('D3: a turn that made no request (Esc first, or a hook that answered) is not counted as another reply', async ($, on) => {
  const world = prepare(on)
  const seen = watchState(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  expect(await repliesCounted(seen, world)).toBe(1)

  // Esc before the first request.
  await $.turn.start({ text: 'oops', turnId: 'esc' })
  await complete($, world, 'esc', 'aborted')
  // A hook that answers the turn without going on to the model: no step at all.
  await $.turn.start({ text: 'hooked', turnId: 'hooked' })
  await complete($, world, 'hooked')

  expect(await repliesCounted(seen, world)).toBe(1)
  expect(spendText(seen, world)).toBe('$0.50 this session (+$0.50)')

  // The next real reply is the second.
  await reply($, world, { steps: [PRICED], usd: PRICED_USD })
  expect(await repliesCounted(seen, world)).toBe(2)
  expect(faults(world)).toEqual([])
})

// -- D4 ------------------------------------------------------------------------------------------------------------

test('D4: a measure part way through a reply does not leave the next reply short when the last figure comes late', async ($, on) => {
  const world = prepare(on)
  const seen = watchState(on)
  await $.session.start(START)
  await reply($, world, FIRST)

  // Reply 2: Claude Code's figure arrives after its first request ($0.30 of it), the second request follows, no more figures.
  await $.turn.start({ text: 'two', turnId: 'two' })
  await mainStep($, world, 'two', 0, PRICED)
  await measure($, world, 0.8)
  await mainStep($, world, 'two', 1, PRICED)
  await complete($, world, 'two')

  // Reply 3 begins; its figure then covers both replies: $0.50, reply 2 ($0.30 and $0.336), and this one.
  await $.turn.start({ text: 'three', turnId: 'three' })
  await mainStep($, world, 'three', 0, PRICED)
  await measure($, world, 0.5 + 0.3 + 2 * PRICED_USD)
  await complete($, world, 'three')
  expect(spendText(seen, world)).toBe('$1.47 this session (+$0.34)')
  expect(faults(world)).toEqual([])
})

// -- D5 and D6 -----------------------------------------------------------------------------------------------------

function statusRig(on: On): { statuses: (string | undefined)[] } {
  const rig = { statuses: [] as (string | undefined)[] }
  on('ui.status', (_$, e) => {
    rig.statuses.push(e.text)
    return { value: undefined } as never
  })
  return rig
}

const PLAN = { plan: 'p'.repeat(4000), isAgent: false, filePath: '/work/plan.md' }
const LIMIT_LINE = /^5-hour limit runs out in about \d+ min at this pace$/

test('D5: the urgent line comes back after the plan tip is cleared at a reply end with no measure of its own', async ($, on) => {
  const world = prepare(on)
  const rig = statusRig(on)
  on('prompt.submit', (_$, e) => e as never)
  on('tool.call', () => ({ result: PLAN }) as never)
  const resetsAt = new Date(T0 + 60 * MINUTE).toISOString()
  limit(world, 95, resetsAt)
  await $.session.start(START)
  await $.prompt.submit({ text: 'build the thing', wait: false, origin: { kind: 'composer' } } as never)
  await reply($, world, { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 18000, output: 0 }], usd: 0.1 })
  await reply($, world, { gap: MINUTE, steps: [{ input: 500, cacheRead: 20000, cacheWrite: 69500, output: 100 }], usd: 0.3 })
  expect(rig.statuses[rig.statuses.length - 1]).toMatch(LIMIT_LINE)
  const urgent = rig.statuses[rig.statuses.length - 1]

  await $.tool.call({ tool: 'ExitPlanMode', tool_use_id: 'plan' } as never)
  expect(String(rig.statuses[rig.statuses.length - 1])).toMatch(/^This plan was approved/)

  // The next reply ends with no figure from Claude Code in between: the tip is cleared and the urgent line is on again.
  await $.turn.start({ text: 'build it', turnId: 'build' })
  await mainStep($, world, 'build', 0, PRICED)
  await complete($, world, 'build')
  expect(rig.statuses[rig.statuses.length - 1]).toMatch(LIMIT_LINE)
  expect(rig.statuses[rig.statuses.length - 1]).toBe(urgent)
  expect(faults(world)).toEqual([])
})

test('D6: while idle the limit line is looked at again each minute, and goes at the limit\'s reset', async ($, on) => {
  const world = prepare(on)
  const rig = statusRig(on)
  const resetsAt = new Date(T0 + 60 * MINUTE).toISOString()
  limit(world, 95, resetsAt)
  await $.session.start(START)
  await reply($, world, FIRST)
  const first = rig.statuses[rig.statuses.length - 1]
  expect(first).toMatch(LIMIT_LINE)
  const minutesOf = (line: string | undefined): number => Number(/about (\d+) min/.exec(line ?? '')?.[1])

  // Nothing happens in the session, but the pace goes on: the minutes named follow it.
  await world.clock.advance(40 * MINUTE)
  const later = rig.statuses[rig.statuses.length - 1]
  expect(later).toMatch(LIMIT_LINE)
  expect(minutesOf(later)).toBeGreaterThan(minutesOf(first))

  // The window resets at the hour: the line goes with it, and nothing is looked at after that.
  await world.clock.advance(21 * MINUTE)
  expect(rig.statuses[rig.statuses.length - 1]).toBeUndefined()
  const count = rig.statuses.length
  await world.clock.advance(30 * MINUTE)
  expect(rig.statuses).toHaveLength(count)
  expect(faults(world)).toEqual([])
})

test('D6: the urgent line is cleared when the session ends or is cleared, and is not looked at again', async ($, on) => {
  const world = prepare(on)
  const rig = statusRig(on)
  const resetsAt = new Date(T0 + 60 * MINUTE).toISOString()
  limit(world, 95, resetsAt)
  await $.session.start(START)
  await reply($, world, FIRST)
  expect(rig.statuses[rig.statuses.length - 1]).toMatch(LIMIT_LINE)

  await $.session.end({ sessionId: SESSION, reason: 'clear' } as never)
  expect(rig.statuses[rig.statuses.length - 1]).toBeUndefined()
  const count = rig.statuses.length
  await world.clock.advance(10 * MINUTE)
  expect(rig.statuses).toHaveLength(count)
  expect(faults(world)).toEqual([])
})
