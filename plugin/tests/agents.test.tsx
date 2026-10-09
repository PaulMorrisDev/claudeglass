import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { elements, faults, notes, prepare, reply, settingsFile, showPage, SITE, START, textOf } from './support'
import type { Step, World } from './support'

const PANE = { title: 'Glass', isFocused: true, bodyColumns: 110, placement: 'dock', ...SITE } as const
const BAND = { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 160, ...SITE }
const RESULT = { tool_use_id: 'tu1', tool: 'Agent', isErrored: false, ...SITE }

// One reply that leaves the main context at 120k of a 160k summarising point, $0.50 spent.
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
// A main context past the 150k that earns report_reread.
const BIG = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 158000, output: 0 }], usd: 0.9 }

const PROMPT = ['Find every caller of parseConfig.', 'Report the file and line of each.', 'Do not change any file.'].join('\n')
const HAIKU: Step = { input: 1000, cacheRead: 4000, cacheWrite: 0, output: 300, model: 'claude-haiku-4-5', tools: ['Grep'] }

type Spawn = { id?: string; type?: string; desc?: string; model?: string; prompt?: string; background?: boolean; parent?: string }

type Listing = { id: string; status: string }[]

/** What sits under the agent events: an id for each spawn, an Agent call that completes, and the engine's own list. */
function bottoms(on: On): { listing: Listing } {
  const state = { listing: [] as Listing, count: 0 }
  on('agent.spawn', (_$, e) => {
    state.count += 1
    return { model: e.model ?? 'claude-haiku-4-5', agentId: `agent-${state.count}` } as never
  })
  on('tool.call', (_$, e) => {
    if (e.tool === 'Agent') return { result: { agentId: `agent-${state.count}`, status: 'completed', totalToolUseCount: 3, resolvedModel: 'claude-haiku-4-5' } } as never
    return { result: {} } as never
  })
  on('session.compact', (_$, e) => ({ trigger: e.trigger }) as never)
  on('agent.list', () => ({ value: state.listing }) as never)
  return state
}

async function spawn($: Engine, world: World, one: Spawn = {}): Promise<string> {
  const answer = await $.agent.spawn({
    tool_use_id: 'tu1',
    prompt: one.prompt ?? PROMPT,
    description: one.desc ?? 'Find callers',
    subagentType: one.type ?? 'Explore',
    model: one.model ?? 'claude-haiku-4-5',
    parentModel: 'claude-opus-4-6',
    background: one.background ?? false,
    fork: false,
  } as never)
  await world.clock.advance(300)
  return String((answer as { agentId?: string }).agentId)
}

/** One request of an agent, then one of its tool calls. */
async function work($: Engine, world: World, id: string, step: Step, index: number): Promise<void> {
  world.next = { usage: step, tools: step.tools ?? [] }
  const stream = $.turn.step({ turnId: `turn-${id}`, index, model: step.model ?? 'claude-haiku-4-5', messageCount: 2 + index, agentId: id } as never)
  for await (const chunk of stream) void chunk
  await stream.result
  for (const name of step.tools ?? []) await $.tool.call({ tool: name, agentId: id, pattern: 'x' } as never)
  await world.clock.advance(300)
}

async function end($: Engine, world: World, id: string, reason: 'answer' | 'error' = 'answer'): Promise<void> {
  await $.turn.complete({ turnId: `turn-${id}`, reason, answer: 'done', isAborted: false, durationMs: 1000, agentId: id } as never)
  await world.clock.advance(300)
}

/** A foreground run, start to end, with the main session's Agent call resolving last. */
async function foreground($: Engine, world: World, one: Spawn = {}): Promise<string> {
  const id = await spawn($, world, one)
  await work($, world, id, HAIKU, 0)
  await work($, world, id, { ...HAIKU, tools: ['Read'], cacheRead: 5000 }, 1)
  await end($, world, id)
  await $.tool.call({ tool: 'Agent', prompt: one.prompt ?? PROMPT, description: 'Find callers', subagent_type: one.type ?? 'Explore' } as never)
  await world.clock.advance(300)
  return id
}

async function pane($: Engine, surface: 'terminal' | 'desktop' = 'terminal') {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface, component: 'Pane', requestId: 'glass', props: PANE as never })
  await showPage(mounted, 'agents', 'Agents')
  return mounted
}

function lines(tree: unknown): string[] {
  return elements(tree)
    .filter(one => one.type === 'Text' || one.type === 'Button' || one.type === 'Link')
    .map(textOf)
    .filter(text => text !== '')
}
/** What the Agent call's result row draws for agent `id`: its line, then any note under it. */
async function rowLines($: Engine, id: string, status = 'completed'): Promise<string[]> {
  const props = { ...RESULT, output: { agentId: id, status } }
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'ToolResult', requestId: 'tu1', props: props as never })
  const out = lines(await mounted.drawn())
  await mounted.unmount()
  return out
}

function noted(shown: string[]): string[] {
  return shown.filter(text => /^Glass · /.test(text))
}

test('a run is followed from its spawn to its end: requests, tools, calls, cost and the Agent call', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await foreground($, world)

  const mounted = await pane($)
  await mounted.press({ key: 'more' })
  await mounted.press({ key: 'agent:agent-1' })
  const shown = lines(await mounted.drawn())
  expect(shown.some(text => /^Explore · haiku$/.test(text))).toBe(true)
  expect(shown.some(text => /^[\d.]+k in · [\d.]+k? out · 3 calls · \d+% re-read from the cache$/.test(text))).toBe(true)
  expect(shown).toContain('Grep x1 · Read x1')
  expect(shown.some(text => /done/.test(text))).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the Agents page first screen: a sentence and a row for the run', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await foreground($, world)

  for (const surface of ['terminal', 'desktop'] as const) {
    const mounted = await pane($, surface)
    const shown = lines(await mounted.drawn())
    expect(shown.some(text => /^No agents working · agents are \d+% of this session's cost$/.test(text))).toBe(true)
    expect(shown).toContain('✓ Explore')
    expect(shown).toContain('haiku')
    expect(shown.some(text => /done/.test(text))).toBe(true)
    expect(shown.some(text => /^\$0\.\d+$/.test(text))).toBe(true)
    // Simple shows no token figures and no main session row.
    expect(shown.some(text => /in · .* out/.test(text))).toBe(false)
    expect(shown).not.toContain('Main session')
    // The row is text; its Details button is quiet and has no hotkey.
    const details = await mounted.find({ type: 'Button', key: 'agent:agent-1' })
    expect(details?.props.label).toBe('Details')
    expect(details?.props.dimColor).toBe(true)
    expect(details?.props.hotkey).toBeUndefined()
    // The only hotkeys on the page are the page bar's.
    for (const button of elements(await mounted.drawn()).filter(one => one.type === 'Button' && one.props?.hotkey !== undefined)) expect(String(button.props?.key)).toMatch(/^page:/)
    await mounted.unmount()
  }
  expect(faults(world)).toEqual([])
})

const BILLINGS = [
  { mode: 'subscription', share_per_usd: null, period_label: 'weekly', basis: 'list' },
  { mode: 'unknown', share_per_usd: null, period_label: 'weekly', basis: 'list' },
]

for (const billing of BILLINGS) {
  test(`on ${billing.mode} billing an amount like "0.12 USD" is not boxed, so the row does not wrap`, async ($, on) => {
    const world = prepare(on, { settings: settingsFile({ billing }) })
    bottoms(on)
    await $.session.start(START)
    await reply($, world, FIRST)
    await foreground($, world)

    for (const surface of ['terminal', 'desktop'] as const) {
      const mounted = await pane($, surface)
      const tree = await mounted.drawn()
      const amount = lines(tree).find(text => / USD$/.test(text) || /^\$\d/.test(text))
      expect(amount).toBeDefined()
      // The fixed-width cells are the name, the model and the activity; the amount is a Text of its own after them, with no width.
      const boxed = elements(tree).filter(one => one.type === 'Box' && one.props?.width !== undefined)
      expect(boxed.length).toBeGreaterThanOrEqual(3)
      expect(boxed.some(one => textOf(one).includes(String(amount)))).toBe(false)
      expect(elements(tree).some(one => one.type === 'Text' && textOf(one) === amount)).toBe(true)
      await mounted.unmount()
    }
    expect(faults(world)).toEqual([])
  })
}

test('Detailed adds the tokens, calls and cache share of each run, and the main session', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await foreground($, world)

  const mounted = await pane($)
  await mounted.press({ key: 'more' })
  const shown = lines(await mounted.drawn())
  expect(shown.some(text => /^[\d.]+k in · [\d.]+k? out · 3 calls · \d+% re-read from the cache$/.test(text))).toBe(true)
  expect(shown).toContain('Main session')
  expect(shown).toContain('1 reply')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a running run is listed first and counted in the headline, with what it is doing', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const first = await foreground($, world)
  const second = await spawn($, world, { type: 'general-purpose', desc: 'Check docs' })
  await work($, world, second, { ...HAIKU, tools: ['WebSearch'] }, 0)

  const mounted = await pane($)
  const shown = lines(await mounted.drawn())
  expect(shown.some(text => /^1 agent working · agents are \d+% of this session's cost$/.test(text))).toBe(true)
  expect(shown.some(text => /reading the web/.test(text))).toBe(true)
  const names = elements(await mounted.drawn())
    .filter(one => one.type === 'Button' && String(one.props?.key).startsWith('agent:'))
    .map(one => one.props?.key)
  expect(names).toEqual([`agent:${second}`, `agent:${first}`])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('pressing Details shows the task prompt, the tools and the dashboard link; the prompt stays off disk', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await foreground($, world)

  const mounted = await pane($)
  await mounted.press({ key: 'agent:agent-1' })
  const shown = lines(await mounted.drawn())
  expect(shown).toContain('Find every caller of parseConfig.')
  expect(shown).toContain('Report the file and line of each.')
  expect(shown).toContain('Grep x1 · Read x1')
  const link = (elements(await mounted.drawn()).find(one => one.type === 'Link' && one.props?.label === 'Subagent runs in the dashboard'))
  expect(String(link?.props.href)).toBe('http://localhost:8792/#/agents/subagents?t=habits.habits_agent_runs')
  const copy = await mounted.find({ type: 'Button', key: 'dashboard-copy' })
  expect(copy?.props.hotkey).toBeUndefined()
  expect(copy?.props.dimColor).toBe(true)
  await mounted.press({ key: 'dashboard-copy' })
  expect(world.copied).toContain('http://127.0.0.1:8792/#/agents/subagents?t=habits.habits_agent_runs')
  expect([...world.files.values()].join('\n')).not.toMatch(/parseConfig/)

  expect((await mounted.find({ type: 'Button', key: 'agent:agent-1' }))?.props.label).toBe('Hide')
  await mounted.press({ key: 'agent:agent-1' })
  expect(lines(await mounted.drawn())).not.toContain('Find every caller of parseConfig.')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('with no agent the page says none has run', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const mounted = await pane($)
  expect(lines(await mounted.drawn()).some(text => /^No agents have run in this session yet\./.test(text))).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the band says how many agents work, only while one does and nothing more urgent is showing', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)

  const texts = async () => {
    const mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'AbovePrompt', props: BAND as never })
    const out = elements(await mounted.drawn())
      .filter(one => one.type === 'Text')
      .map(textOf)
      .filter(text => text !== '│' && text !== '')
    await mounted.unmount()
    return out
  }

  expect(await texts()).toHaveLength(2)
  const one = await spawn($, world, { background: true })
  await work($, world, one, HAIKU, 0)
  expect((await texts())[2]).toBe('1 agent working')
  const two = await spawn($, world, { background: true, type: 'Plan' })
  await work($, world, two, HAIKU, 0)
  expect((await texts())[2]).toBe('2 agents working')
  await end($, world, one)
  expect((await texts())[2]).toBe('1 agent working')
  await end($, world, two)
  expect(await texts()).toHaveLength(2)
  expect(faults(world)).toEqual([])
})

test('the Agent call has its row under it: name, model and cost; Detailed adds tokens', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await foreground($, world)

  expect((await rowLines($, id)).some(text => /^Explore · haiku · \$0\.\d+$/.test(text))).toBe(true)

  // A call that is not an Agent call has no row.
  const other = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'ToolResult', requestId: 'tu2', props: { ...RESULT, tool: 'Read', tool_use_id: 'tu2', output: { agentId: id } } as never })
  expect(lines(await other.drawn())).toEqual([])
  await other.unmount()

  const mounted = await pane($)
  await mounted.press({ key: 'more' })
  await mounted.unmount()
  expect((await rowLines($, id)).some(text => /^Explore · haiku · [\d.]+k in · [\d.]+k? out · \$0\.\d+$/.test(text))).toBe(true)
  expect(faults(world)).toEqual([])
})

test('report_reread: a background agent started over a big context is told on its row, once', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, BIG)
  const first = await spawn($, world, { background: true })
  const shown = await rowLines($, first, 'async_launched')
  expect(noted(shown)).toHaveLength(1)
  expect(noted(shown)[0]).toMatch(/^Glass · This session holds about 160k tokens, and the reply to each report/)

  // The rest rule holds the same note back for half an hour.
  const second = await spawn($, world, { background: true, type: 'Plan' })
  expect(noted(await rowLines($, second, 'async_launched'))).toEqual([])
  expect(faults(world)).toEqual([])
})

test('report_reread is not told when the agent is muted in the settings', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { muted: ['report_reread'] } }) })
  bottoms(on)
  await $.session.start(START)
  await reply($, world, BIG)
  const id = await spawn($, world, { background: true })
  expect(noted(await rowLines($, id, 'async_launched'))).toEqual([])
  expect(faults(world)).toEqual([])
})

test('report_reread is not told for a foreground agent', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, BIG)
  const id = await spawn($, world, { background: false })
  expect(noted(await rowLines($, id))).toEqual([])
  expect(faults(world)).toEqual([])
})

test('report_reread stays quiet while the context is below the threshold', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await spawn($, world, { background: true })
  expect(noted(await rowLines($, id, 'async_launched'))).toEqual([])
  expect(faults(world)).toEqual([])
})

test('split_run: a foreground run that began from a long brief is told on its row', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const long = `${'Study this in depth. '.repeat(400)}\nEnd.`
  const id = await foreground($, world, { prompt: long })

  const shown = noted(await rowLines($, id))
  expect(shown).toHaveLength(1)
  expect(shown[0]).toMatch(/^Glass · In this session .*6,000 characters/)
  expect(faults(world)).toEqual([])
})

test('split_run: a short brief earns nothing, and a skipped agent type earns nothing', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const short = await foreground($, world)
  expect(noted(await rowLines($, short))).toEqual([])
  const skipped = await foreground($, world, { type: 'statusline-setup', prompt: 'x'.repeat(9000) })
  expect(noted(await rowLines($, skipped))).toEqual([])
  expect(notes(world)).toEqual([])
  expect(faults(world)).toEqual([])
})

test('split_run: a background run that ended too big is told at the end of a later reply, as a note in the transcript', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await spawn($, world, { background: true, prompt: 'y'.repeat(7000) })
  await work($, world, id, HAIKU, 0)
  await end($, world, id)
  expect(notes(world)).toEqual([])

  await reply($, world, { gap: 40 * 60 * 1000, steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
  expect(notes(world)).toHaveLength(1)
  expect(notes(world)[0]).toMatch(/^● Glass · In this session .*6,000 characters/)
  expect(faults(world)).toEqual([])
})

test('an agent that ends in an error shows as stopped, and the engine list ends a run whose end was not seen', async ($, on) => {
  const world = prepare(on)
  const state = bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const failed = await spawn($, world)
  await work($, world, failed, HAIKU, 0)
  await end($, world, failed, 'error')
  const lost = await spawn($, world, { type: 'Plan' })
  await work($, world, lost, HAIKU, 0)

  const mounted = await pane($)
  let shown = lines(await mounted.drawn())
  expect(shown.some(text => /stopped with an error/.test(text))).toBe(true)
  expect(shown.some(text => /^1 agent working/.test(text))).toBe(true)

  state.listing = [{ id: lost, status: 'completed' }]
  await world.clock.advance(11000)
  shown = lines(await mounted.drawn())
  expect(shown.some(text => /^No agents working/.test(text))).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('many agents at once draw at most four times a second', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const ids: string[] = []
  for (let i = 0; i < 8; i += 1) ids.push(await spawn($, world, { background: true, desc: `Task ${i}` }))
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'AbovePrompt', props: BAND as never })
  await world.clock.advance(1000)

  // Forty-eight requests inside about a second: the snapshot is written, so the band drawn again, at most four times a second.
  const before = world.draws.AbovePrompt ?? 0
  const step = { ...HAIKU, tools: [] }
  for (let round = 0; round < 6; round += 1) {
    for (const id of ids) {
      world.next = { usage: step, tools: [] }
      const stream = $.turn.step({ turnId: `turn-${id}`, index: round, model: 'claude-haiku-4-5', messageCount: 2 + round, agentId: id } as never)
      for await (const chunk of stream) void chunk
      await stream.result
    }
    await world.clock.advance(100)
  }
  await world.clock.advance(1000)
  const drawn = (world.draws.AbovePrompt ?? 0) - before
  expect(drawn).toBeGreaterThanOrEqual(1)
  expect(drawn).toBeLessThanOrEqual(4 * 2)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the first screen shows six agents and says how many more there are; Detailed lists them all', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  for (let i = 0; i < 8; i += 1) await spawn($, world, { desc: `Task ${i}`, background: true })

  const mounted = await pane($)
  const rows = async () => elements(await mounted.drawn()).filter(one => one.type === 'Button' && String(one.props?.key).startsWith('agent:'))
  expect(await rows()).toHaveLength(6)
  expect(lines(await mounted.drawn())).toContain('and 2 more')
  await mounted.press({ key: 'more' })
  expect(await rows()).toHaveLength(8)
  expect(lines(await mounted.drawn()).some(text => /^and \d+ more$/.test(text))).toBe(false)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})
