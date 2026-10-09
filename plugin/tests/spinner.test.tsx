import { expect, test } from 'claude-code/testing'

import { elements, faults, measured, prepare, reply, settingsFile, START, textOf } from './support'

const SPINNER = { word: 'Thinking', message: null, suffix: '(3s)', mode: 'responding' } as const
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }

async function working($: Parameters<typeof reply>[0], world: Parameters<typeof reply>[1]): Promise<void> {
  await $.session.start(START)
  await reply($, world, FIRST)
  // The next turn has started and has cost $0.12 so far.
  await $.turn.start({ text: 'more', turnId: 'turn-next' })
  // The reply begins at its first request.
  world.next = { usage: { cacheRead: 120000, cacheWrite: 400, output: 0 }, tools: [] }
  const stream = $.turn.step({ turnId: 'turn-next', index: 0, model: 'claude-sonnet-4-6', messageCount: 4 })
  for await (const chunk of stream) void chunk
  await stream.result
  await $.session.measure(measured(120400, 0.62))
  await world.clock.advance(300)
}

async function suffix($: Parameters<typeof reply>[0], world: Parameters<typeof reply>[1], surface: 'terminal' | 'desktop'): Promise<string> {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface, component: 'Spinner', props: SPINNER as never })
  await mounted.unmount()
  return String(world.seen.Spinner?.suffix)
}

test('the spinner suffix adds how full the context is and what this turn has cost, after what was there', async ($, on) => {
  const world = prepare(on)
  await working($, world)
  for (const surface of ['terminal', 'desktop'] as const) {
    expect(await suffix($, world, surface)).toBe('(3s) · 60% full · +$0.12')
  }
  expect(faults(world)).toEqual([])
})

test('on a subscription the spinner gives the fullness alone', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ billing: { mode: 'subscription', share_per_usd: 0.02, period_label: 'weekly', basis: 'list' } }) })
  await working($, world)
  expect(await suffix($, world, 'terminal')).toBe('(3s) · 60% full')
  expect(faults(world)).toEqual([])
})

test('the spinner leaves the suffix alone before there are figures', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  expect(await suffix($, world, 'terminal')).toBe('(3s)')
})

/** Runs one model request of the reply in progress, with no measurement from Claude Code after it. */
async function request($: Parameters<typeof reply>[0], world: Parameters<typeof reply>[1], index: number, step: { cacheRead: number; cacheWrite: number; output: number }): Promise<void> {
  world.next = { usage: step, tools: [] }
  const stream = $.turn.step({ turnId: 'turn-live', index, model: 'claude-sonnet-4-6', messageCount: 2 + index })
  for await (const chunk of stream) void chunk
  await stream.result
  await world.clock.advance(300)
}

test('the spinner shows the reply cost as each request is priced, before Claude Code reports any cost, and keeps its own figure once it does', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)

  await $.turn.start({ text: 'more', turnId: 'turn-live' })
  expect(await suffix($, world, 'terminal')).toBe('(3s) · 60% full')

  // 120k read from the cache, 1k written, 200 out: $0.036 + $0.0038 + $0.003 = $0.04.
  await request($, world, 0, { cacheRead: 120000, cacheWrite: 1000, output: 200 })
  expect(await suffix($, world, 'terminal')).toBe('(3s) · 60% full · +$0.04')

  // A subagent's turn starts in the middle of the reply and makes requests of its own: it does not begin the reply again.
  await $.turn.start({ text: 'agent', turnId: 'turn-sub' })
  world.next = { usage: { cacheRead: 5000, cacheWrite: 100, output: 50 }, tools: [] }
  const sub = $.turn.step({ turnId: 'turn-sub', index: 0, agentId: 'agent-1', model: 'claude-sonnet-4-6', messageCount: 2 } as never)
  for await (const chunk of sub) void chunk
  await sub.result
  await world.clock.advance(300)
  expect(await suffix($, world, 'terminal')).toBe('(3s) · 60% full · +$0.04')

  // Claude Code's number for the reply so far replaces the estimate, and the next request adds to it.
  await $.session.measure(measured(121200, 0.6))
  await world.clock.advance(300)
  expect(await suffix($, world, 'terminal')).toBe('(3s) · 60% full · +$0.10')
  await request($, world, 1, { cacheRead: 121000, cacheWrite: 1000, output: 200 })
  expect(await suffix($, world, 'terminal')).toBe('(3s) · 60% full · +$0.14')
  expect(faults(world)).toEqual([])
})

test('a reply that ends before Claude Code reports its cost is settled by the cost that follows', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)

  await $.turn.start({ text: 'more', turnId: 'turn-live' })
  await request($, world, 0, { cacheRead: 120000, cacheWrite: 1000, output: 200 })
  await $.turn.complete({ turnId: 'turn-live', reason: 'answer', answer: 'done', isAborted: false, durationMs: 1000 })
  await world.clock.advance(300)

  const change = async () => {
    const mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 200, scroll: undefined, view: undefined } as never })
    const spend = elements(await mounted.drawn()).filter(one => one.type === 'Text').map(textOf).find(text => text.includes('this session'))
    await mounted.unmount()
    return spend
  }
  expect(await change()).toBe('$0.50 this session (+$0.04)')

  world.figures(121200, 0.62)
  await $.session.measure(measured(121200, 0.62))
  await world.clock.advance(300)
  expect(await change()).toBe('$0.62 this session (+$0.12)')
  expect(faults(world)).toEqual([])
})
