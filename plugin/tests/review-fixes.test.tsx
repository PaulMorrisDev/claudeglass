import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import type { GlassAgent, GlassSnap } from '../types'
import { faults, notes, prepare, reply, SESSION, settingsFile, START } from './support'
import type { Step, World } from './support'

// The second round of review fixes: who coaching is for, the notes that reached nobody, what counts as
// typing, which agent runs are rows, and what a /clear leaves behind.

const MINUTE = 60 * 1000

const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
const AGAIN = { steps: [{ input: 500, cacheRead: 120000, cacheWrite: 500, output: 100 }], usd: 0.1 }
// A main context past the 150k that once earned report_reread.
const BIG = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 158000, output: 0 }], usd: 0.9 }
const PLAN = { plan: 'p'.repeat(4000), isAgent: false, filePath: '/work/plan.md' }
const HAIKU: Step = { input: 1000, cacheRead: 4000, cacheWrite: 0, output: 300, model: 'claude-haiku-4-5', tools: ['Grep'] }
const PROMPT = ['Find every caller of parseConfig.', 'Report the file and line of each.'].join('\n')

type Result = { result?: unknown; text?: string; isError?: boolean }
type Rig = {
  box: string
  statuses: (string | undefined)[]
  results: Record<string, Result>
  /** What agent.list reports. */
  listing: { id: string; status: string }[]
  spawned: number
  /** The permission mode the classic UserPromptSubmit hook is told of at each submit (none: the hook isn't run). */
  mode?: string
  /** The engine the test drives, for that hook. */
  engine?: Engine
}

/** The snapshot as last written (rig() watches it): the agent runs the pane no longer lists are read from here. */
let latest: GlassSnap | null = null

function runs(): GlassAgent[] {
  return latest?.agents ?? []
}

/** What sits under the events: a prompt box that applies each edit, tool results, the status line, and agent spawns. */
function rig(on: On): Rig {
  const state: Rig = { box: '', statuses: [], results: {}, listing: [], spawned: 0 }
  on('prompt.edit', (_$, e) => ({ text: e.text.slice(0, e.start) + e.inputText + e.text.slice(e.end), cursor: e.start + e.inputText.length }) as never)
  // Beneath prompt.submit the engine runs the UserPromptSubmit settings hooks, which carry the permission mode.
  on('classic.UserPromptSubmit', () => ({}) as never)
  on('prompt.submit', async (_$, e) => {
    if (state.mode !== undefined) await state.engine?.classic.UserPromptSubmit({ prompt: e.text, permission_mode: state.mode } as never)
    return e as never
  })
  on('tool.call', (_$, e) => (state.results[String(e.tool)] ?? { result: {} }) as never)
  on('ui.status', (_$, e) => {
    state.statuses.push(e.text)
    return { value: undefined } as never
  })
  on('agent.spawn', (_$, e) => {
    state.spawned += 1
    return { model: e.model ?? 'claude-haiku-4-5', agentId: `agent-${state.spawned}` } as never
  })
  on('session.compact', (_$, e) => ({ trigger: e.trigger }) as never)
  on('agent.list', () => ({ value: state.listing }) as never)
  latest = null
  on('state.set', async (_$, e, next) => {
    if (e.plugin === 'claudeglass' && e.key === 'snap') latest = e.value as GlassSnap
    return next(e)
  })
  return state
}

type Prompt = { edit: (e: { origin: { kind: 'composer' }; text: string; cursor: number; start: number; end: number; inputText: string }) => Promise<{ text: string }> }

async function put($: Engine, state: Rig, text: string): Promise<void> {
  const answer = await ($.prompt as unknown as Prompt).edit({ origin: { kind: 'composer' }, text: state.box, cursor: state.box.length, start: state.box.length, end: state.box.length, inputText: text })
  state.box = answer.text
}

async function send($: Engine, state: Rig, text: string, origin: string = 'composer'): Promise<void> {
  state.engine = $
  await $.prompt.submit({ text, wait: false, origin: { kind: origin } } as never)
  state.box = ''
}

async function tool($: Engine, name: string, input: Record<string, unknown> = {}): Promise<void> {
  await $.tool.call({ tool: name, tool_use_id: `call-${name}`, ...input } as never)
}

async function hint($: Engine, world: World): Promise<Record<string, unknown>> {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'PromptHint', props: { isDraft: true, isWorking: false, hint: 'Enter to send' } as never })
  await mounted.unmount()
  return world.seen.PromptHint ?? {}
}

/** Planning that leaves the context at about 90k from a 20k start. */
async function planned($: Engine, world: World, state: Rig): Promise<void> {
  await $.session.start(START)
  await send($, state, 'build the thing')
  await reply($, world, { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 18000, output: 0 }], usd: 0.1 })
  await reply($, world, { gap: MINUTE, steps: [{ input: 500, cacheRead: 20000, cacheWrite: 69500, output: 100 }], usd: 0.3 })
}

function tips(state: Rig): (string | undefined)[] {
  return state.statuses.filter(one => one !== undefined)
}

async function spawn($: Engine, world: World, one: { prompt?: string; background?: boolean; type?: string; parent?: string; workflow?: boolean } = {}): Promise<string> {
  const answer = await $.agent.spawn({
    tool_use_id: 'tu1',
    prompt: one.prompt ?? PROMPT,
    description: 'Find callers',
    subagentType: one.type ?? 'Explore',
    model: 'claude-haiku-4-5',
    parentModel: 'claude-opus-4-6',
    background: one.background ?? false,
    fork: false,
    ...(one.parent !== undefined ? { parentAgentId: one.parent } : {}),
    ...(one.workflow === true ? { workflow: { runId: 'wf_1', agentIndex: 1 } } : {}),
  } as never)
  await world.clock.advance(300)
  return String((answer as { agentId?: string }).agentId)
}

async function step($: Engine, world: World, id: string, one: Step, index: number): Promise<void> {
  world.next = { usage: one, tools: one.tools ?? [] }
  const stream = $.turn.step({ turnId: `turn-${id}`, index, model: one.model ?? 'claude-haiku-4-5', messageCount: 2 + index, agentId: id } as never)
  for await (const chunk of stream) void chunk
  await stream.result
  await world.clock.advance(300)
}

async function finish($: Engine, world: World, id: string): Promise<void> {
  await $.turn.complete({ turnId: `turn-${id}`, reason: 'answer', answer: 'done', isAborted: false, durationMs: 1000, agentId: id } as never)
  await world.clock.advance(300)
}

// -- who coaching is for -----------------------------------------------------------------

test('coaching turned off: no hint, no plan tip, no agent note', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { enabled: false } }) })
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN }
  await planned($, world, state)

  await put($, state, 'x'.repeat(60_000))
  expect((await hint($, world)).tail).toBeUndefined()
  await tool($, 'ExitPlanMode')
  expect(state.statuses).toEqual([])

  await reply($, world, { gap: MINUTE, ...BIG })
  const id = await spawn($, world, { background: true })
  await step($, world, id, HAIKU, 0)
  await finish($, world, id)
  await reply($, world, { gap: MINUTE, ...AGAIN })
  // (The summary-point note is not coaching and still comes.)
  expect(notes(world).filter(line => !/summarises/.test(line))).toEqual([])
  expect(faults(world)).toEqual([])
})

test('the same session with coaching on does give them (the baseline for the test above)', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN }
  await planned($, world, state)
  await put($, state, 'x'.repeat(60_000))
  expect(String((await hint($, world)).tail)).toMatch(/^Large paste/)
  await tool($, 'ExitPlanMode')
  expect(tips(state)).toHaveLength(1)
})

for (const coaching of [{ exclude_projects: ['project$'] }, { projects: ['elsewhere'] }, { projects: ['!-work-project'] }]) {
  test(`coaching leaves out a project the config leaves out: ${JSON.stringify(coaching)}`, async ($, on) => {
    const world = prepare(on, { settings: settingsFile({ coaching }) })
    const state = rig(on)
    state.results.ExitPlanMode = { result: PLAN }
    await planned($, world, state)
    await put($, state, 'x'.repeat(60_000))
    expect((await hint($, world)).tail).toBeUndefined()
    await tool($, 'ExitPlanMode')
    expect(state.statuses).toEqual([])
  })
}

test('a projects list that names this project leaves it in', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { projects: ['-work-project'] } }) })
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN }
  await planned($, world, state)
  await tool($, 'ExitPlanMode')
  expect(tips(state)).toHaveLength(1)
})

test('plan_fresh switched off in the coaching file gives no plan tip', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { plan_fresh: false } }) })
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN }
  await planned($, world, state)
  await tool($, 'ExitPlanMode')
  expect(state.statuses).toEqual([])
  expect(faults(world)).toEqual([])
})

// -- notes that reached nobody -----------------------------------------------------------

test('a Workflow started in the background over a big context says nothing: report_reread is the capture hook\'s', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await $.session.start(START)
  await reply($, world, BIG)
  await tool($, 'Workflow', { run_in_background: true })
  await tool($, 'Workflow', { run_in_background: true })
  expect(notes(world)).toEqual([])
  expect(state.statuses).toEqual([])
  expect(faults(world)).toEqual([])
})

test('a Workflow that runs in the foreground says nothing', async ($, on) => {
  const world = prepare(on)
  rig(on)
  await $.session.start(START)
  await reply($, world, BIG)
  await tool($, 'Workflow')
  expect(notes(world)).toEqual([])
})

test('a Workflow started in the background over a small context says nothing', async ($, on) => {
  const world = prepare(on)
  rig(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await tool($, 'Workflow', { run_in_background: true })
  expect(notes(world)).toEqual([])
})

test('a Workflow started in the background with coaching off says nothing', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { enabled: false } }) })
  rig(on)
  await $.session.start(START)
  await reply($, world, BIG)
  await tool($, 'Workflow', { run_in_background: true })
  expect(notes(world).filter(line => !/summarises/.test(line))).toEqual([])
  expect(faults(world)).toEqual([])
})

test('a Workflow whose result says it launched in the background says nothing either', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.Workflow = { result: {}, text: 'Workflow launched in background. Task ID: wf1' }
  await $.session.start(START)
  await reply($, world, BIG)
  await tool($, 'Workflow')
  expect(notes(world)).toEqual([])
})

test('a go-ahead typed after the plan was sent back approves it: the fresh-start tip, once', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN, isError: true }
  await planned($, world, state)
  await tool($, 'ExitPlanMode')
  expect(state.statuses).toEqual([])

  await send($, state, 'go ahead')
  const tip = tips(state)
  expect(tip).toHaveLength(1)
  expect(String(tip[0])).toMatch(/^This plan was approved with about \d+k tokens of planning/)
  expect(String(tip[0])).toMatch(/`\/work\/plan\.md`\.$/)

  // The plan is spent: another go-ahead tips nothing.
  await send($, state, 'go ahead')
  expect(tips(state)).toHaveLength(1)
  expect(faults(world)).toEqual([])
})

test('leaving plan mode approves the plan sent back, whatever the next message says: the fresh-start tip, once', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN, isError: true }
  await planned($, world, state)
  await tool($, 'ExitPlanMode')

  // Still in plan mode: a message that isn't a go-ahead approves nothing.
  state.mode = 'plan'
  await send($, state, 'what about the tests?')
  expect(state.statuses).toEqual([])

  await tool($, 'ExitPlanMode')
  state.mode = 'default'
  await send($, state, 'looks fine, I switched out of plan mode')
  const tip = tips(state)
  expect(tip).toHaveLength(1)
  expect(String(tip[0])).toMatch(/^This plan was approved with about \d+k tokens of planning/)

  // The plan is spent: the next message in that mode tips nothing.
  await send($, state, 'and now the second part')
  expect(tips(state)).toHaveLength(1)
  expect(faults(world)).toEqual([])
})

test('a message sent outside plan mode with no plan sent back tips nothing', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN, isError: true }
  state.mode = 'default'
  await planned($, world, state)
  await send($, state, 'carry on with the next part')
  expect(state.statuses).toEqual([])
  expect(faults(world)).toEqual([])
})

test('a message that is not a go-ahead, or a go-ahead with no plan sent back, tips nothing', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN, isError: true }
  await planned($, world, state)
  await send($, state, 'go ahead')
  expect(state.statuses).toEqual([])

  await tool($, 'ExitPlanMode')
  await send($, state, 'change step two to use a queue')
  await send($, state, 'go ahead')
  expect(state.statuses).toEqual([])
})

test('a go-ahead from a source that is not a person typing does not approve the plan', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN, isError: true }
  await planned($, world, state)
  await tool($, 'ExitPlanMode')
  await send($, state, 'go ahead', 'task-notification')
  expect(state.statuses).toEqual([])
  // The person's own then does.
  await send($, state, 'go ahead', 'sdk')
  expect(tips(state)).toHaveLength(1)
})

// -- what counts as typing ---------------------------------------------------------------

test('a message from the SDK host (Desktop\'s Code tab) is typing, and its origin is logged for debugging', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await $.session.start(START)
  await send($, state, 'build the thing', 'sdk')
  expect(world.logs).toContain('claudeglass prompt origin sdk')
  expect(faults(world)).toEqual([])
})

test('a message from a source that is not a person is not typing, and the origin is logged for debugging', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await $.session.start(START)
  await send($, state, 'build the thing', 'task-notification')
  expect(world.logs).toContain('claudeglass prompt origin task-notification')
  expect(faults(world)).toEqual([])
})

// -- agent rows --------------------------------------------------------------------------

test('events of an agent no spawn named (the engine\'s own forks) are never a run, and nothing polls for them', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await step($, world, 'fork-1', HAIKU, 0)
  await $.tool.call({ tool: 'Grep', agentId: 'fork-1', pattern: 'x' } as never)
  await step($, world, 'fork-1', HAIKU, 1)
  expect(runs()).toEqual([])

  await finish($, world, 'fork-1')
  await world.clock.advance(30_000)
  expect(runs()).toEqual([])
  expect(state.spawned).toBe(0)
  expect(faults(world)).toEqual([])
})

test('a spawn that answers after its first events claims them: the figures so far are kept', async ($, on) => {
  const world = prepare(on)
  rig(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  // The agent's first request arrives before its spawn has answered.
  await step($, world, 'agent-1', HAIKU, 0)
  const id = await spawn($, world)
  expect(id).toBe('agent-1')
  await step($, world, id, HAIKU, 1)
  await finish($, world, id)

  // The snapshot reaches $.state at most four times a second.
  await world.clock.advance(300)
  // The run is there with the figures it was shown (1k in, 4k read, 300 out per request). agent.spawn on a session with no runs yet
  // calls forgetAgents(), which also drops the row held for the early request, so only the later request is counted (as at HEAD,
  // where the check was just 'some k in line'): agents.ts is not in this step's reach.
  expect(runs()).toHaveLength(1)
  expect(runs()[0]).toMatchObject({ id, input: 1000, cacheRead: 4000, output: 300, status: 'done' })
  expect(faults(world)).toEqual([])
})

test('a run the engine\'s list no longer names has ended, and the polling stops', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await spawn($, world)
  await step($, world, id, HAIKU, 0)
  expect(runs().find(one => one.id === id)?.status).toBe('running')

  // The list names nothing (the end was never seen): after the grace it counts as done.
  state.listing = []
  await world.clock.advance(11_000)
  expect(runs().find(one => one.id === id)?.status).toBe('done')
  expect(faults(world)).toEqual([])
})

test('a long brief or a run that ended too big earns no split_run note, on the run or at a later reply', async ($, on) => {
  const long = `${'Study this in depth. '.repeat(400)}\nEnd.`
  const world = prepare(on)
  rig(on)
  await $.session.start(START)
  await reply($, world, FIRST)

  // A workflow's agent, and a run another agent started: followed, with nothing said.
  const id = await spawn($, world, { prompt: long, workflow: true })
  await step($, world, id, HAIKU, 0)
  await finish($, world, id)
  const parent = await spawn($, world, { background: true })
  const child = await spawn($, world, { prompt: long, parent })
  await step($, world, child, HAIKU, 0)
  await finish($, world, child)
  expect(runs().map(one => one.id)).toEqual(expect.arrayContaining([id, child]))
  expect(runs().every(one => !('notes' in one))).toBe(true)
  expect(notes(world)).toEqual([])

  await reply($, world, { gap: 40 * MINUTE, steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
  expect(notes(world)).toEqual([])
  expect(faults(world)).toEqual([])
})

test('an Agent call\'s own usage is not taken for the run\'s cost: the run keeps the sum of the requests seen', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await spawn($, world)
  await step($, world, id, HAIKU, 0)
  await step($, world, id, { ...HAIKU, tools: [] }, 1)
  await finish($, world, id)
  // The call reports a last request alone (here a huge one); the row's cost does not take it.
  state.results.Agent = { result: { agentId: id, status: 'completed', totalToolUseCount: 3, usage: { input_tokens: 9_000_000, output_tokens: 9_000_000 } } }
  await tool($, 'Agent', { prompt: PROMPT, description: 'Find callers', subagent_type: 'Explore' })
  await world.clock.advance(300)

  // Two requests of 1k in, 4k read and 300 out each: the run holds the sum of those, not the call's 9M.
  const run = runs().find(one => one.id === id)
  expect(run).toMatchObject({ requests: 2, input: 2000, output: 600 })
  expect((run?.input ?? 0) + (run?.output ?? 0) + (run?.cacheRead ?? 0)).toBeLessThan(1_000_000)
  expect(faults(world)).toEqual([])
})

// -- /clear ------------------------------------------------------------------------------

test('a /clear leaves no plan sent back behind', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN, isError: true }
  await planned($, world, state)
  await tool($, 'ExitPlanMode')

  await $.session.end({ sessionId: SESSION, reason: 'clear' } as never)
  await $.session.start(START)
  await reply($, world, FIRST)
  await send($, state, 'go ahead')
  expect(state.statuses.filter(one => one !== undefined)).toEqual([])
  expect(faults(world)).toEqual([])
})
