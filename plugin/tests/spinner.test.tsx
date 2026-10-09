import { expect, test } from 'claude-code/testing'

import { faults, measured, prepare, reply, settingsFile, START } from './support'

const SPINNER = { word: 'Thinking', message: null, suffix: '(3s)', mode: 'responding' } as const
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }

async function working($: Parameters<typeof reply>[0], world: Parameters<typeof reply>[1]): Promise<void> {
  await $.session.start(START)
  await reply($, world, FIRST)
  // The next turn has started and has cost $0.12 so far.
  await $.turn.start({ text: 'more', turnId: 'turn-next' })
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
