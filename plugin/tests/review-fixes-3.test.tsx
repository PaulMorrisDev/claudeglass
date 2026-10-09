import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { elements, faults, prepare, reply, SESSION, settingsFile, SITE, START, T0, textOf } from './support'
import type { Step, World } from './support'

// The third round of review fixes: the 5-hour share while another session runs, a long reply that a subagent's
// turn must not restart, a late measurement, the tokens after /compact, the first message after /clear, the urgent
// status line, the Haiku check's key, the Detailed boundary line, and a workflow's agents.

const MINUTE = 60 * 1000
const NOW_S = Math.floor(T0 / 1000)
const PANE = { title: 'Glass', isFocused: true, bodyColumns: 110, placement: 'dock', ...SITE } as const
const PROPS = { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 200, ...SITE }
const SUBSCRIPTION = settingsFile({ billing: { mode: 'subscription', share_per_usd: null, period_label: 'weekly usage limit', basis: 'list' } })

// One reply that leaves the context at 120k of a 160k summarising point, $0.50 spent.
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
// A reply priced exactly: 120k read at $0.30 per million and 20k written out at $15 per million is $0.336.
const PRICED: Step = { cacheRead: 120000, cacheWrite: 0, output: 20000 }
const PRICED_USD = 0.336
const SHARE = /% of your 5-hour limit/

type Mounted = Awaited<ReturnType<Engine['ui']['mount']>>

async function band($: Engine): Promise<string[]> {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'AbovePrompt', props: PROPS as never })
  const texts = await segments(mounted)
  await mounted.unmount()
  return texts
}

async function segments(mounted: Mounted): Promise<string[]> {
  return elements(await mounted.drawn())
    .filter(one => one.type === 'Text')
    .map(textOf)
    .filter(text => text !== '│' && text !== '')
}

/** The 5-hour limit at `pct` percent used, with the window ending at `resetsAt`. */
function limit(world: World, pct: number, resetsAt: string): void {
  world.limits = [{ kind: 'five_hour', percentUsed: pct, resetsAt }]
}

function present(world: World, id: string, seen: number): void {
  world.files.set(`/cfg/claudeglass/mod/present/${id}.json`, JSON.stringify({ schema: 1, session_id: id, seen }))
}

function presentOf(world: World, id: string): { session_id: string; seen: number } | null {
  const text = world.files.get(`/cfg/claudeglass/mod/present/${id}.json`)
  return text === undefined ? null : (JSON.parse(text) as { session_id: string; seen: number })
}

/** A main request of reply `turnId`, as the engine runs one (the `reply` helper does the same for whole replies). */
async function mainStep($: Engine, world: World, turnId: string, index: number, step: Step, agentId?: string): Promise<void> {
  world.next = { usage: step, tools: step.tools ?? [] }
  const stream = $.turn.step({ turnId, index, model: step.model ?? 'claude-sonnet-4-6', messageCount: 2 + index, ...(agentId === undefined ? {} : { agentId }) } as never)
  for await (const chunk of stream) void chunk
  await stream.result
}

const CONTEXT = PRICED.cacheRead + PRICED.cacheWrite + PRICED.output

async function measure($: Engine, world: World, usd: number): Promise<void> {
  world.usd = usd
  world.figures(CONTEXT, usd)
  await $.session.measure({ context: { tokens: CONTEXT, window: 200000, percent: 70 }, rateLimits: world.limits, cost: { usd }, changed: ['context', 'cost'] } as never)
}

async function complete($: Engine, world: World, turnId: string): Promise<void> {
  await $.turn.complete({ turnId, reason: 'answer', answer: 'done', isAborted: false, durationMs: 1000 } as never)
  // The snapshot reaches $.state at most four times a second.
  await world.clock.advance(300)
}

// -- A1 ------------------------------------------------------------------------------------------------------------

/** Two replies in one window, the limit moving 10% to 15% for a dollar and a half, then a cold cache: the band's third segment. */
async function coldSegment($: Engine, world: World, resetsAt: string): Promise<string | undefined> {
  limit(world, 10, resetsAt)
  await $.session.start(START)
  await reply($, world, FIRST)
  limit(world, 15, resetsAt)
  await reply($, world, { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.5 })
  await world.clock.advance(400 * 1000)
  return (await band($))[2]
}

test('A1: with another session\'s heartbeat fresh, the 5-hour limit is not split into this session\'s share', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  const resetsAt = new Date(T0 + 4 * 60 * MINUTE).toISOString()
  present(world, 'session-other', NOW_S - 30)
  const cold = await coldSegment($, world, resetsAt)
  expect(cold).toMatch(/^cache expired \(est\.\)/)
  expect(cold).not.toMatch(SHARE)
  expect(faults(world)).toEqual([])
})

test('A1: a heartbeat seen more than ten minutes ago, or the session\'s own, leaves the share as it was', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  const resetsAt = new Date(T0 + 4 * 60 * MINUTE).toISOString()
  present(world, 'session-other', NOW_S - 700)
  present(world, 'session-ended', 0)
  const cold = await coldSegment($, world, resetsAt)
  expect(cold).toMatch(/^cache expired \(est\.\): the next send costs about \d+\.\d+% of your 5-hour limit$/)
  expect(faults(world)).toEqual([])
})

test('A1: a fall in the 5-hour percent starts the share over, though the reset time is the same', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  const resetsAt = new Date(T0 + 4 * 60 * MINUTE).toISOString()
  limit(world, 50, resetsAt)
  await $.session.start(START)
  await reply($, world, FIRST)
  // The limit falls to 3% (a new window, with the same time named); the next dollar and a half moves it to 8%.
  limit(world, 3, resetsAt)
  await reply($, world, { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
  limit(world, 8, resetsAt)
  await reply($, world, { steps: [{ cacheRead: 121000, cacheWrite: 1000, output: 200 }], usd: 0.5 })
  await world.clock.advance(400 * 1000)
  // 5 points for $0.50 is 10% a dollar, so a cold send of about $0.45 is about 4.5%. Kept from the 50% it would have no share.
  expect((await band($))[2]).toMatch(/^cache expired \(est\.\): the next send costs about 4\.\d% of your 5-hour limit$/)
  expect(faults(world)).toEqual([])
})

test('A1: the heartbeat folder is looked at once a minute, not at every measurement', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  const resetsAt = new Date(T0 + 4 * 60 * MINUTE).toISOString()
  limit(world, 10, resetsAt)
  await $.session.start(START)
  for (let i = 0; i < 6; i += 1) await reply($, world, FIRST)
  expect(world.listed).toBe(1)
  await world.clock.advance(61 * 1000)
  await reply($, world, FIRST)
  expect(world.listed).toBe(2)
  expect(faults(world)).toEqual([])
})

// -- A2 ------------------------------------------------------------------------------------------------------------

test('A2: a subagent\'s turn twenty minutes into a long reply doesn\'t begin a new reply: no cold send, and the reply\'s cost stays whole', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)

  await $.turn.start({ text: 'go', turnId: 'long' })
  await mainStep($, world, 'long', 0, { ...PRICED, tools: ['Task'] })
  await world.clock.advance(20 * MINUTE)
  // An agent's turn begins and takes a request; then the main reply goes on.
  await $.turn.start({ text: 'agent task', turnId: 'sub' })
  await mainStep($, world, 'sub', 0, { ...PRICED, model: 'claude-haiku-4-5' }, 'agent-9')
  await mainStep($, world, 'long', 1, PRICED)
  await measure($, world, 0.5 + 2 * PRICED_USD)
  await complete($, world, 'long')

  expect((await band($))[1]).toBe('$1.17 this session (+$0.67)')
  const sensor = JSON.parse(String(world.files.get(`/cfg/claudeglass/mod/sensor/${SESSION}.json`))) as { events: { event: string }[] }
  expect(sensor.events.filter(one => one.event === 'cold_send')).toEqual([])
  expect(faults(world)).toEqual([])
})

// -- A3 ------------------------------------------------------------------------------------------------------------

test('A3: a measurement of the last reply that arrives after the next reply began is not counted in the next', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)

  // Reply 2 ends with no measurement yet. Reply 3 begins; then reply 2's figure arrives, then reply 3's own.
  await $.turn.start({ text: 'two', turnId: 'two' })
  await mainStep($, world, 'two', 0, PRICED)
  await complete($, world, 'two')
  await world.clock.advance(300)
  await $.turn.start({ text: 'three', turnId: 'three' })
  await mainStep($, world, 'three', 0, PRICED)
  await measure($, world, 0.5 + PRICED_USD)
  await measure($, world, 0.5 + 2 * PRICED_USD)
  await complete($, world, 'three')

  expect((await band($))[1]).toBe('$1.17 this session (+$0.34)')
  expect(faults(world)).toEqual([])
})

test('A3: a measurement that arrives before the next reply begins settles the reply it belongs to', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await reply($, world, { steps: [PRICED], usd: PRICED_USD })
  expect((await band($))[1]).toBe('$0.84 this session (+$0.34)')
  await reply($, world, { steps: [PRICED], usd: PRICED_USD })
  expect((await band($))[1]).toBe('$1.17 this session (+$0.34)')
  expect(faults(world)).toEqual([])
})

test('A3: the first reply of a session, and the first after a /clear, are settled from nothing', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  expect((await band($))[1]).toBe('$0.50 this session (+$0.50)')

  await $.session.end({ sessionId: SESSION, reason: 'clear' } as never)
  world.session = 'session-two'
  world.usd = 0
  await $.session.start(START)
  await $.turn.start({ text: 'again', turnId: 'again' })
  await mainStep($, world, 'again', 0, PRICED)
  await measure($, world, 0.4)
  await complete($, world, 'again')
  expect((await band($))[1]).toBe('$0.40 this session (+$0.40)')
  expect(faults(world)).toEqual([])
})

// -- A4 ------------------------------------------------------------------------------------------------------------

function summarising(on: On): void {
  on('session.compact', () => ({ messages: [{ role: 'user', text: 'Summary of the conversation.', toolUses: [] }], tokensBefore: 150000, tokensAfter: 30000 }) as never)
}

test('A4: right after /compact the band takes the context from the new breakdown, not from the last request\'s size', async ($, on) => {
  const world = prepare(on)
  summarising(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  expect((await band($))[0]).toMatch(/60% full/)

  // Claude Code's breakdown is the new context; its `context.tokens` is still the last request's.
  world.figures(30000, 0.5)
  world.stale = 120000
  await $.session.compact({ trigger: 'manual', messages: SAID } as never)
  await world.clock.advance(300)
  const texts = await band($)
  // The state word follows the tokens, the percent follows the breakdown: they agree.
  expect(texts[0]).toBe('○ 15% full · Claude summarises at 160k')
  expect(faults(world)).toEqual([])
})

test('A4: with no breakdown yet, the summary\'s own tokensAfter is the context', async ($, on) => {
  const world = prepare(on)
  summarising(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  world.figures(30000, 0.5)
  world.stale = 120000
  world.breakdown = false
  await $.session.compact({ trigger: 'manual', messages: SAID } as never)
  await world.clock.advance(300)
  const texts = await band($)
  expect(texts[0]).toBe('○ 15% full · Claude summarises at 200k')
  expect(faults(world)).toEqual([])
})

// -- A5 ------------------------------------------------------------------------------------------------------------

test('A5: the first message after a /clear leaves the new session\'s heartbeat, before its first turn begins', async ($, on) => {
  const world = prepare(on)
  on('prompt.submit', (_$, e) => e as never)
  await $.session.start(START)
  await reply($, world, FIRST)
  expect(presentOf(world, SESSION)?.seen).toBeGreaterThan(0)

  await $.session.end({ sessionId: SESSION, reason: 'clear' } as never)
  expect(presentOf(world, SESSION)?.seen).toBe(0)
  world.session = 'session-two'
  expect(presentOf(world, 'session-two')).toBeNull()

  await $.prompt.submit({ text: 'first after clear', wait: false, origin: { kind: 'composer' } } as never)
  expect(presentOf(world, 'session-two')).toEqual({ schema: 1, session_id: 'session-two', seen: expect.any(Number) })
  expect(presentOf(world, 'session-two')?.seen).toBeGreaterThanOrEqual(NOW_S)
  expect(faults(world)).toEqual([])
})

// -- A7 ------------------------------------------------------------------------------------------------------------

function statuses(on: On): (string | undefined)[] {
  const seen: (string | undefined)[] = []
  on('ui.status', (_$, e) => {
    seen.push(e.text)
    return { value: undefined } as never
  })
  return seen
}

test('A7: a 5-hour limit forecast to run out within fifteen minutes is on the status line, and goes when it no longer is', async ($, on) => {
  const world = prepare(on)
  const seen = statuses(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  // 95% gone in 4.5 of the 5 hours: about 14 minutes left at this pace, in a window that ends in 30.
  await world.clock.advance(270 * MINUTE)
  limit(world, 95, new Date(world.clock.now() + 30 * MINUTE).toISOString())
  await reply($, world, { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.01 })
  const line = seen.filter(one => one !== undefined)
  expect(line).toHaveLength(1)
  expect(line[0]).toMatch(/^5-hour limit runs out in about \d+ min at this pace$/)

  // A new window clears the line.
  limit(world, 4, new Date(world.clock.now() + 290 * MINUTE).toISOString())
  await reply($, world, { gap: MINUTE, steps: [{ cacheRead: 121000, cacheWrite: 1000, output: 200 }], usd: 0.01 })
  expect(seen[seen.length - 1]).toBeUndefined()
  expect(faults(world)).toEqual([])
})

test('A7: one reply or none left before Claude summarises is on the status line; room again clears it', async ($, on) => {
  const world = prepare(on)
  const seen = statuses(on)
  summarising(on)
  await $.session.start(START)
  // 10k more in every reply, from 100k: with 60k of room the line stays off, and with 10k it comes on.
  for (const ctx of [100000, 110000, 120000, 130000]) await reply($, world, { gap: MINUTE, steps: [{ cacheRead: ctx - 1000, cacheWrite: 800, output: 200 }], usd: 0.1 })
  expect(seen.filter(one => one !== undefined)).toEqual([])
  for (const ctx of [140000, 150000]) await reply($, world, { gap: MINUTE, steps: [{ cacheRead: ctx - 1000, cacheWrite: 800, output: 200 }], usd: 0.1 })
  expect(seen[seen.length - 1]).toBe('About 1 reply before Claude summarises')
  // One more reply and there is no room for another.
  await reply($, world, { gap: MINUTE, steps: [{ cacheRead: 158000, cacheWrite: 800, output: 200 }], usd: 0.1 })
  expect(seen[seen.length - 1]).toBe('Claude summarises on the next reply')

  // A summary brings the room back.
  world.figures(30000, 0.7)
  await $.session.compact({ trigger: 'manual', messages: SAID } as never)
  expect(seen[seen.length - 1]).toBeUndefined()
  expect(faults(world)).toEqual([])
})

// -- A8 ------------------------------------------------------------------------------------------------------------

test('A8: no two hotkeys on the Compaction page are the same, and the Haiku check has its own', async ($, on) => {
  const world = prepare(on)
  summarising(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await $.command.run({ command: 'glass', args: 'detail' } as never)
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'Pane', requestId: 'glass', props: PANE as never })
  await mounted.press({ key: 'page:compaction' } as never)
  const keys = elements(await mounted.drawn())
    .filter(one => one.type === 'Button' && typeof one.props?.hotkey === 'string')
    .map(one => String(one.props?.hotkey))
  // The pane's own page tabs and the band's keys share the screen; the page's buttons add theirs.
  expect(new Set(keys).size).toBe(keys.length)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

// -- A9 ------------------------------------------------------------------------------------------------------------

const SAID = [
  { role: 'user', text: "Never use port 8765 for the dev server, it is reserved. Don't read any code yet; wait for the other session to finish.", toolUses: [] },
  { role: 'assistant', text: 'Working on it.', toolUses: [] },
]

function boundary(world: World): string[] {
  return world.logs.filter(line => line.startsWith('● Glass · Claude summarised'))
}

async function summarised($: Engine, world: World, usage?: Record<string, number>): Promise<void> {
  world.transcript = [...SAID]
  await $.session.start(START)
  await reply($, world, FIRST)
  await $.session.compact({ trigger: 'manual', messages: SAID } as never)
  void usage
}

test('A9: the Simple boundary line is as it was, and the Detailed view adds the tokens before and after', async ($, on) => {
  const world = prepare(on)
  on('session.compact', () => ({ messages: [{ role: 'user', text: 'Summary.', toolUses: [] }], tokensBefore: 150000, tokensAfter: 30000 }) as never)
  on('classic.SessionStart', () => ({}) as never)
  await summarised($, world)
  expect(boundary(world)).toHaveLength(1)
  expect(boundary(world)[0]).not.toMatch(/became/)

  world.logs.length = 0
  await $.command.run({ command: 'glass', args: 'detail' } as never)
  await $.session.compact({ trigger: 'manual', messages: SAID } as never)
  expect(boundary(world)).toHaveLength(1)
  expect(boundary(world)[0]).toMatch(/ 150k tokens became 30k tokens\.$/)
  expect(faults(world)).toEqual([])
})

test('A9: when Claude Code reports what the summary cost, the Detailed line says so', async ($, on) => {
  const world = prepare(on)
  on('session.compact', () => ({ messages: [{ role: 'user', text: 'Summary.', toolUses: [] }], tokensBefore: 150000, tokensAfter: 30000, usage: { input_tokens: 150000, output_tokens: 3000, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } }) as never)
  on('classic.SessionStart', () => ({}) as never)
  await $.command.run({ command: 'glass', args: 'detail' } as never)
  await summarised($, world)
  expect(boundary(world)).toHaveLength(1)
  expect(boundary(world)[0]).toMatch(/ 150k tokens became 30k tokens, about \$0\.\d\d\.$/)
  expect(faults(world)).toEqual([])
})

// -- C1 ------------------------------------------------------------------------------------------------------------

type Listing = { id: string; status: string }[]

function agentRig(on: On): { listing: Listing } {
  const state = { listing: [] as Listing, count: 0 }
  on('agent.spawn', (_$, e) => {
    state.count += 1
    return { model: e.model ?? 'claude-haiku-4-5', agentId: `agent-${state.count}` } as never
  })
  on('tool.call', () => ({ result: {} }) as never)
  on('agent.list', () => ({ value: state.listing }) as never)
  return state
}

async function workflowAgent($: Engine, world: World): Promise<string> {
  const answer = await $.agent.spawn({
    tool_use_id: 'wf-call',
    prompt: 'Study the module and report.',
    description: 'Study',
    subagentType: 'general-purpose',
    model: 'claude-haiku-4-5',
    parentModel: 'claude-opus-4-6',
    background: false,
    fork: false,
    workflow: { runId: 'wf_1', agentIndex: 1 },
  } as never)
  await world.clock.advance(300)
  return String((answer as { agentId?: string }).agentId)
}

async function headline($: Engine): Promise<string> {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'Pane', requestId: 'glass', props: PANE as never })
  await mounted.press({ key: 'page:agents' })
  const text = elements(await mounted.drawn()).filter(one => one.type === 'Text').map(textOf).join('\n')
  await mounted.unmount()
  return text
}

test('C1: a workflow\'s agent the engine\'s list never shows is not finished at every poll; its own end finishes it', async ($, on) => {
  const world = prepare(on)
  agentRig(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await workflowAgent($, world)
  await mainStep($, world, `turn-${id}`, 0, { ...PRICED, model: 'claude-haiku-4-5' }, id)

  await world.clock.advance(45_000)
  expect(await headline($)).toMatch(/1 agent working/)

  await $.turn.complete({ turnId: `turn-${id}`, reason: 'answer', answer: 'done', isAborted: false, durationMs: 1000, agentId: id } as never)
  expect(await headline($)).toMatch(/No agents working/)
  expect(faults(world)).toEqual([])
})

test('C1: the Workflow call returning in the foreground finishes the run\'s agents', async ($, on) => {
  const world = prepare(on)
  agentRig(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await workflowAgent($, world)
  await mainStep($, world, `turn-${id}`, 0, { ...PRICED, model: 'claude-haiku-4-5' }, id)
  await world.clock.advance(45_000)
  expect(await headline($)).toMatch(/1 agent working/)

  await $.tool.call({ tool: 'Workflow', tool_use_id: 'wf-call' } as never)
  expect(await headline($)).toMatch(/No agents working/)
  expect(faults(world)).toEqual([])
})

test('C1: a workflow\'s agent with no event for half an hour is finished, so the band stops counting it', async ($, on) => {
  const world = prepare(on)
  agentRig(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await workflowAgent($, world)
  await mainStep($, world, `turn-${id}`, 0, { ...PRICED, model: 'claude-haiku-4-5' }, id)
  await world.clock.advance(20 * MINUTE)
  expect(await headline($)).toMatch(/1 agent working/)
  await world.clock.advance(15 * MINUTE)
  expect(await headline($)).toMatch(/No agents working/)
  expect(faults(world)).toEqual([])
})
