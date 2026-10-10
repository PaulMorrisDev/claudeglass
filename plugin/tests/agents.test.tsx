import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import type { GlassAgent, GlassSnap } from '../types'
import { faults, notes, prepare, reply, settingsFile, SITE, START } from './support'
import type { Step, World } from './support'

const BAND = { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 160, ...SITE }

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
  latest = null
  on('state.set', async (_$, e, next) => {
    if (e.plugin === 'claudeglass' && e.key === 'snap') latest = e.value as GlassSnap
    return next(e)
  })
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
  await $.tool.call({ tool: 'Agent', prompt: one.prompt ?? PROMPT, description: one.desc ?? 'Find callers', subagent_type: one.type ?? 'Explore' } as never)
  await world.clock.advance(300)
  return id
}

/** The snapshot as last written (bottoms() watches it). */
let latest: GlassSnap | null = null

/** The agent runs the snapshot holds: what agents.ts followed. The pane no longer lists them; the dashboard does. */
function runs(): GlassAgent[] {
  return latest?.agents ?? []
}

/** The coaching notes kept on run `id`, as the words they were given in. */
function noted(id: string): string[] {
  return (runs().find(one => one.id === id)?.notes ?? []).map(note => note.text)
}

test('a run is followed from its spawn to its end: requests, tools, calls, cost and the Agent call', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await foreground($, world)

  const [run, ...rest] = runs()
  expect(rest).toEqual([])
  expect(run).toMatchObject({ id, type: 'Explore', desc: 'Find callers', model: 'claude-haiku-4-5', status: 'done', background: false, requests: 2, calls: 3, tools: { Grep: 1, Read: 1 } })
  expect(run?.usd).toBeGreaterThan(0)
  expect(run?.endedAt).not.toBeNull()
  // The task prompt is never kept, in the snapshot or on disk.
  expect(JSON.stringify(run)).not.toMatch(/parseConfig/)
  expect([...world.files.values()].join('\n')).not.toMatch(/parseConfig/)
  expect(faults(world)).toEqual([])
})

test('a running run is followed with what it is doing, and ends when its turn does', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const first = await foreground($, world)
  const second = await spawn($, world, { type: 'general-purpose', desc: 'Check docs' })
  await work($, world, second, { ...HAIKU, tools: ['WebSearch'] }, 0)

  const running = runs().find(one => one.id === second)
  expect(running).toMatchObject({ status: 'running', desc: 'Check docs', activity: 'fetching' })
  expect(runs().find(one => one.id === first)?.status).toBe('done')
  await end($, world, second)
  expect(runs().find(one => one.id === second)?.status).toBe('done')
  expect(faults(world)).toEqual([])
})

test('a run with no description is kept with its type alone', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await foreground($, world, { desc: '' })

  expect(runs()[0]).toMatchObject({ type: 'Explore', desc: '' })
  expect(faults(world)).toEqual([])
})

test('every tool a run called is counted, MCP tools by their own names', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await spawn($, world)
  const used = ['Bash', 'Bash', 'Bash', 'Read', 'Read', 'Edit', 'Grep', 'Glob', 'mcp__a__one', 'mcp__b__two', 'Write']
  for (const [index, name] of used.entries()) await work($, world, id, { ...HAIKU, tools: [name] }, index)

  const [run] = runs()
  expect(run?.tools).toEqual({ Bash: 3, Read: 2, Edit: 1, Grep: 1, Glob: 1, mcp__a__one: 1, mcp__b__two: 1, Write: 1 })
  expect(run?.calls).toBe(used.length)
  expect(faults(world)).toEqual([])
})

test('report_reread: a background agent started over a big context is told, once', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, BIG)
  const first = await spawn($, world, { background: true })
  const shown = noted(first)
  expect(shown).toHaveLength(1)
  expect(shown[0]).toMatch(/^This session holds about 160k tokens, and the reply to each report/)

  // The rest rule holds the same note back for half an hour.
  const second = await spawn($, world, { background: true, type: 'Plan' })
  expect(noted(second)).toEqual([])
  expect(faults(world)).toEqual([])
})

test('report_reread is not told when the agent is muted in the settings', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { muted: ['report_reread'] } }) })
  bottoms(on)
  await $.session.start(START)
  await reply($, world, BIG)
  const id = await spawn($, world, { background: true })
  expect(noted(id)).toEqual([])
  expect(faults(world)).toEqual([])
})

test('report_reread is not told for a foreground agent', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, BIG)
  const id = await spawn($, world, { background: false })
  expect(noted(id)).toEqual([])
  expect(faults(world)).toEqual([])
})

test('report_reread stays quiet while the context is below the threshold', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const id = await spawn($, world, { background: true })
  expect(noted(id)).toEqual([])
  expect(faults(world)).toEqual([])
})

test('split_run: a foreground run that began from a long brief is told', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const long = `${'Study this in depth. '.repeat(400)}\nEnd.`
  const id = await foreground($, world, { prompt: long })

  const shown = noted(id)
  expect(shown).toHaveLength(1)
  expect(shown[0]).toMatch(/^In this session .*6,000 characters/)
  expect(faults(world)).toEqual([])
})

test('split_run: a short brief earns nothing, and a skipped agent type earns nothing', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const short = await foreground($, world)
  expect(noted(short)).toEqual([])
  const skipped = await foreground($, world, { type: 'statusline-setup', prompt: 'x'.repeat(9000) })
  expect(noted(skipped)).toEqual([])
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

test('an agent that ends in an error is kept as failed, and the engine list ends a run whose end was not seen', async ($, on) => {
  const world = prepare(on)
  const state = bottoms(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const failed = await spawn($, world)
  await work($, world, failed, HAIKU, 0)
  await end($, world, failed, 'error')
  const lost = await spawn($, world, { type: 'Plan' })
  await work($, world, lost, HAIKU, 0)

  let all = runs()
  expect(all.find(one => one.id === failed)?.status).toBe('failed')
  expect(all.find(one => one.id === lost)?.status).toBe('running')

  state.listing = [{ id: lost, status: 'completed' }]
  await world.clock.advance(11000)
  all = runs()
  expect(all.find(one => one.id === lost)?.status).toBe('done')
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
