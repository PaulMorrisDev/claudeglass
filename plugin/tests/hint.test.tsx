import { expect, test } from 'claude-code/testing'

import { faults, prepare, reply, START } from './support'

const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }

async function hint($: Parameters<typeof reply>[0], world: Parameters<typeof reply>[1], surface: 'terminal' | 'desktop', props: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface, component: 'PromptHint', props: { isDraft: true, isWorking: false, hint: 'Enter to send', ...props } as never })
  await mounted.unmount()
  return world.seen.PromptHint ?? {}
}

test('the hint says nothing while the cache is warm', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  const seen = await hint($, world, 'terminal')
  expect(seen.tail).toBeUndefined()
  expect(seen.hint).toBe('Enter to send')
  expect(faults(world)).toEqual([])
})

test('once the cache has expired the hint line says nothing about it: that hint is off', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await world.clock.advance(400 * 1000)

  const terminal = await hint($, world, 'terminal')
  expect(terminal.tail).toBeUndefined()
  expect(terminal.hint).toBe('Enter to send')

  const desktop = await hint($, world, 'desktop')
  expect(desktop.hint).toBe('Enter to send')
  expect(desktop.tail).toBeUndefined()
  expect(faults(world)).toEqual([])
})

test('the hint is left alone with no draft, or while Claude works', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await world.clock.advance(400 * 1000)
  expect((await hint($, world, 'terminal', { isDraft: false })).tail).toBeUndefined()
  expect((await hint($, world, 'terminal', { isWorking: true })).tail).toBeUndefined()
})

test('in the detailed view a warm send says what it costs', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  expect((await $.command.run({ command: 'glass', args: 'detail' } as never)).text).toBe('Detailed view is on.')
  const seen = await hint($, world, 'terminal')
  expect(String(seen.tail)).toMatch(/^Sending costs about \$\d/)
  expect(faults(world)).toEqual([])
})
