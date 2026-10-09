import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { elements, faults, notes, prepare, reply, SESSION, settingsFile, showPage, SITE, START, T0, textOf } from './support'
import type { Step, World } from './support'

// The second round of review fixes: who coaching is for, the notes that reached nobody, what counts as
// typing, which agent runs are rows, and what a /clear leaves behind.

const MINUTE = 60 * 1000
const COACH = '/cfg/claudeglass/mod/coach.json'
const PANE = { title: 'Glass', isFocused: true, bodyColumns: 110, placement: 'dock', ...SITE } as const
const NOW_S = Math.floor(T0 / 1000)

const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
const AGAIN = { steps: [{ input: 500, cacheRead: 120000, cacheWrite: 500, output: 100 }], usd: 0.1 }
// A main context past the 150k that earns report_reread.
const BIG = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 158000, output: 0 }], usd: 0.9 }
const BIG_REPLY = { steps: [{ cacheRead: 240_000, cacheWrite: 10_000, output: 0 }], usd: 0.5 }
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

function ratingNotes(world: World): string[] {
  return notes(world).filter(line => /isn't rated/.test(line))
}

async function work($: Engine, world: World, replies: number): Promise<void> {
  for (let i = 0; i < replies; i += 1) await reply($, world, { gap: MINUTE, ...BIG_REPLY })
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

async function agentsPage($: Engine) {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'Pane', requestId: 'glass', props: PANE as never })
  await showPage(mounted, 'agents', 'Agents')
  return mounted
}

function lines(tree: unknown): string[] {
  return elements(tree)
    .filter(one => one.type === 'Text' || one.type === 'Button' || one.type === 'Link')
    .map(textOf)
    .filter(text => text !== '')
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

// -- the rating reminder -----------------------------------------------------------------

async function piece($: Engine, world: World, state: Rig): Promise<void> {
  world.shape = { window: 5_000_000, point: 4_000_000 }
  await $.session.start(START)
  await send($, state, 'build the thing')
  await work($, world, 6)
}

for (const coaching of [{ feedback_reminder: false }, { exclude_projects: ['work'] }]) {
  test(`no rating reminder with ${JSON.stringify(coaching)}`, async ($, on) => {
    const world = prepare(on, { settings: settingsFile({ coaching }) })
    await piece($, world, rig(on))
    expect(ratingNotes(world)).toEqual([])
  })
}

test('a project filter that leaves this project in keeps the rating reminder', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { exclude_projects: ['elsewhere'] } }) })
  await piece($, world, rig(on))
  expect(ratingNotes(world)).toHaveLength(1)
})

// The capture hook's reminder goes by the opt-in and the project filter alone; coaching notes being off doesn't hold it back.
test('the rating reminder still comes with coaching notes off, when the feedback reminder is on', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { enabled: false, feedback_reminder: true } }) })
  await piece($, world, rig(on))
  expect(ratingNotes(world)).toHaveLength(1)
  expect(faults(world)).toEqual([])
})

test('the capture hook last reminder holds the next one back', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { reminded_at: NOW_S - 3600 } }) })
  await piece($, world, rig(on))
  expect(ratingNotes(world)).toEqual([])
})

test('a rating on the dashboard since the piece began holds the reminder back', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { rated: { [SESSION]: NOW_S + 30 } } }) })
  await piece($, world, rig(on))
  expect(ratingNotes(world)).toEqual([])
})

test('a rating from before the piece began is not the rating this reminder asks for', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { rated: { [SESSION]: NOW_S - 86_400 }, reminded_at: NOW_S - 90 * 86_400 } }) })
  await piece($, world, rig(on))
  expect(ratingNotes(world)).toHaveLength(1)
  expect(JSON.parse(String(world.files.get(COACH))).reminded_at).toBeGreaterThan(NOW_S)
  expect(faults(world)).toEqual([])
})

// -- notes that reached nobody -----------------------------------------------------------

test('report_reread for a Workflow started in the background over a big context is a note, once', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await $.session.start(START)
  await reply($, world, BIG)
  await tool($, 'Workflow', { run_in_background: true })
  expect(notes(world)).toHaveLength(1)
  expect(notes(world)[0]).toMatch(/^● Glass · This session holds about 160k tokens, and the reply to each report/)
  // The same note rests.
  await tool($, 'Workflow', { run_in_background: true })
  expect(notes(world)).toHaveLength(1)
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

test('a Workflow whose result says it launched in the background counts as one', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.Workflow = { result: {}, text: 'Workflow launched in background. Task ID: wf1' }
  await $.session.start(START)
  await reply($, world, BIG)
  await tool($, 'Workflow')
  expect(notes(world)).toHaveLength(1)
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

test('a message from the SDK host (Desktop\'s Code tab) is typing: it starts the piece the rating reminder counts', async ($, on) => {
  const world = prepare(on)
  world.shape = { window: 5_000_000, point: 4_000_000 }
  const state = rig(on)
  await $.session.start(START)
  await send($, state, 'build the thing', 'sdk')
  await work($, world, 4)
  expect(ratingNotes(world)).toHaveLength(1)
  expect(world.logs).toContain('claudeglass prompt origin sdk')
  expect(faults(world)).toEqual([])
})

test('a message from a source that is not a person is not typing, and the origin is logged for debugging', async ($, on) => {
  const world = prepare(on)
  world.shape = { window: 5_000_000, point: 4_000_000 }
  const state = rig(on)
  await $.session.start(START)
  await send($, state, 'build the thing', 'task-notification')
  await work($, world, 6)
  // Nothing typed began a piece, so this reminder still counts the session from its start: it is given the same.
  expect(world.logs).toContain('claudeglass prompt origin task-notification')
  expect(faults(world)).toEqual([])
})

// -- agent rows --------------------------------------------------------------------------

test('events of an agent no spawn named (the engine\'s own forks) are never a row, and nothing polls for them', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await step($, world, 'fork-1', HAIKU, 0)
  await $.tool.call({ tool: 'Grep', agentId: 'fork-1', pattern: 'x' } as never)
  await step($, world, 'fork-1', HAIKU, 1)

  const mounted = await agentsPage($)
  let shown = lines(await mounted.drawn())
  expect(shown.some(text => /^No agents have run in this session yet\./.test(text))).toBe(true)

  await finish($, world, 'fork-1')
  await world.clock.advance(30_000)
  shown = lines(await mounted.drawn())
  expect(shown.some(text => /^No agents have run in this session yet\./.test(text))).toBe(true)
  await mounted.unmount()
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

  const mounted = await agentsPage($)
  await mounted.press({ key: 'more' })
  await mounted.press({ key: 'agent:agent-1' })
  const shown = lines(await mounted.drawn())
  // Two requests of 5k read and written each, 3k output... the row holds both.
  expect(shown.some(text => /^[\d.]+k in · /.test(text))).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a run the engine\'s list no longer names has ended, and the polling stops', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await spawn($, world)
  await step($, world, id, HAIKU, 0)
  const mounted = await agentsPage($)
  expect(lines(await mounted.drawn()).some(text => /^1 agent working/.test(text))).toBe(true)

  // The list names nothing (the end was never seen): after the grace it counts as done.
  state.listing = []
  await world.clock.advance(11_000)
  expect(lines(await mounted.drawn()).some(text => /^No agents working/.test(text))).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('split_run is told on the row only for a run that has an Agent call of the main session\'s; any other waits for the turn-end note', async ($, on) => {
  const long = `${'Study this in depth. '.repeat(400)}\nEnd.`
  const world = prepare(on)
  rig(on)
  await $.session.start(START)
  await reply($, world, FIRST)

  // A workflow's agent: foreground, but with no Agent call behind it, so no row on screen.
  const id = await spawn($, world, { prompt: long, workflow: true })
  await step($, world, id, HAIKU, 0)
  await finish($, world, id)
  const props = { tool_use_id: 'tu1', tool: 'Agent', isErrored: false, ...SITE, output: { agentId: id, status: 'completed' } }
  const row = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'ToolResult', requestId: 'tu1', props: props as never })
  expect(lines(await row.drawn()).some(text => /^Glass · /.test(text))).toBe(false)
  await row.unmount()
  expect(notes(world)).toEqual([])

  // It is told once at the end of a later reply.
  await reply($, world, { gap: 40 * MINUTE, steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
  expect(notes(world)).toHaveLength(1)
  expect(notes(world)[0]).toMatch(/^● Glass · In this session .*6,000 characters/)
  expect(faults(world)).toEqual([])
})

test('a run another agent started has no row on screen either: split_run waits for the turn-end note', async ($, on) => {
  const long = `${'Study this in depth. '.repeat(400)}\nEnd.`
  const world = prepare(on)
  rig(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const parent = await spawn($, world, { background: true })
  const child = await spawn($, world, { prompt: long, parent })
  await step($, world, child, HAIKU, 0)
  await finish($, world, child)
  expect(notes(world)).toEqual([])
  await reply($, world, { gap: 40 * MINUTE, steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
  expect(notes(world)).toHaveLength(1)
  expect(faults(world)).toEqual([])
})

test('an Agent call\'s own usage is not taken for the run\'s cost: the row keeps the sum of the requests seen', async ($, on) => {
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

  const mounted = await agentsPage($)
  await mounted.press({ key: 'more' })
  await mounted.press({ key: `agent:${id}` })
  const shown = lines(await mounted.drawn())
  expect(shown.some(text => /9[,.]?0*M|9000k/.test(text))).toBe(false)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

// -- /clear ------------------------------------------------------------------------------

test('a /clear leaves no plan sent back and no waiting split_run behind', async ($, on) => {
  const long = `${'Study this in depth. '.repeat(400)}\nEnd.`
  const world = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN, isError: true }
  await planned($, world, state)
  await tool($, 'ExitPlanMode')
  const id = await spawn($, world, { prompt: long, background: true })
  await step($, world, id, HAIKU, 0)
  await finish($, world, id)

  await $.session.end({ sessionId: SESSION, reason: 'clear' } as never)
  await $.session.start(START)
  await reply($, world, FIRST)
  await send($, state, 'go ahead')
  expect(state.statuses.filter(one => one !== undefined)).toEqual([])
  await reply($, world, { gap: 40 * MINUTE, steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
  expect(notes(world).filter(line => /6,000 characters/.test(line))).toEqual([])
  expect(faults(world)).toEqual([])
})
