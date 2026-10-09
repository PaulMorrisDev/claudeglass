import { expect, test } from 'claude-code/testing'

import { elements, faults, prepare, reply, SITE, START, textOf } from './support'
import type { World } from './support'

const PROPS = { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 160, ...SITE }

// One reply that leaves the context at 120k of a 160k summarising point, $0.50 spent.
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }

async function band($: Parameters<typeof reply>[0], surface: 'terminal' | 'desktop', props: Record<string, unknown> = {}) {
  return $.ui.mount({ plugin: 'claudeglass', surface, component: 'AbovePrompt', props: { ...PROPS, ...props } as never })
}

/** The texts of the band's segments (its separators left out). */
async function segments(mounted: Awaited<ReturnType<typeof band>>): Promise<string[]> {
  return elements(await mounted.drawn())
    .filter(one => one.type === 'Text')
    .map(textOf)
    .filter(text => text !== '│' && text !== '')
}

async function started($: Parameters<typeof reply>[0], world: World): Promise<void> {
  await $.session.start(START)
  await reply($, world, FIRST)
}

test('the band has two fixed segments and a Glass button; the bar is desktop only', async ($, on) => {
  const world = prepare(on)
  await started($, world)

  for (const surface of ['terminal', 'desktop'] as const) {
    const mounted = await band($, surface)
    const texts = await segments(mounted)
    expect(texts).toHaveLength(2)
    expect(texts[0]).toMatch(/60% full/)
    expect(texts[0]).toMatch(/summarises at 160k/)
    expect(texts[1]).toBe('$0.50 this session')

    const open = await mounted.find({ type: 'Button', key: 'open' })
    expect(open?.props.hotkey).toBe('g')
    expect(open?.props.label).toBe('Glass')

    const svg = await mounted.find({ type: 'Svg' })
    if (surface === 'desktop') {
      expect(svg).toBeDefined()
      expect(String(svg?.props.alt)).toMatch(/60% full/)
      expect(String(svg?.props.alt)).toMatch(/summarises at 160k/)
      expect(String(svg?.props.source).length).toBeLessThanOrEqual(40000)
    } else {
      expect(svg).toBeUndefined()
    }
    await mounted.unmount()
  }
  expect(faults(world)).toEqual([])
})

test('a third segment appears only for the cache running out, the cache gone, or the 5-hour pace', async ($, on) => {
  const world = prepare(on)
  await started($, world)

  // Inside the last fifth of the 5-minute lifetime.
  await world.clock.advance(270 * 1000)
  let mounted = await band($, 'terminal')
  let texts = await segments(mounted)
  expect(texts).toHaveLength(3)
  expect(texts[2]).toMatch(/^resend soon: cheap for 0:\d\d more$/)
  await mounted.unmount()

  // Past it, with a big context.
  await world.clock.advance(60 * 1000)
  mounted = await band($, 'desktop')
  texts = await segments(mounted)
  expect(texts).toHaveLength(3)
  expect(texts[2]).toMatch(/^cache expired \(est\.\): the next send costs about \$\d/)
  await mounted.unmount()

  // The 5-hour limit going faster than the window: the cache has already expired, which comes first.
  const resetsAt = new Date(world.clock.now() + 2 * 60 * 60 * 1000).toISOString()
  world.limits = [{ kind: 'five_hour', percentUsed: 80, resetsAt }]
  await reply($, world, { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
  mounted = await band($, 'terminal')
  texts = await segments(mounted)
  expect(texts).toHaveLength(3)
  expect(texts[2]).toMatch(/^5-hour limit 80% used, runs out about \d{1,2}:\d\d (am|pm) at this pace$/)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('segments drop from the right as the band narrows, and the bar goes first', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  await world.clock.advance(270 * 1000)

  const wanted: [number, number, boolean][] = [
    [140, 3, true],
    [110, 2, true],
    [70, 1, false],
    [30, 1, false],
  ]
  for (const [columns, count, hasBar] of wanted) {
    const mounted = await band($, 'desktop', { bodyColumns: columns })
    expect((await segments(mounted)).length).toBeLessThanOrEqual(3)
    expect((await segments(mounted)).length).toBeGreaterThanOrEqual(1)
    expect((await segments(mounted)).length).toBe(count)
    expect((await mounted.find({ type: 'Svg' })) !== undefined).toBe(hasBar)
    await mounted.unmount()
  }
  expect(faults(world)).toEqual([])
})

test('the band keeps what is beneath it, and adds nothing while a survey is up or before there are figures', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)

  // No figures yet.
  let mounted = await band($, 'terminal')
  expect(await mounted.find({ key: 'open' })).toBeUndefined()
  expect(await mounted.drawn()).toMatchObject({ type: 'Box' })
  await mounted.unmount()

  await reply($, world, FIRST)

  // A survey is up.
  mounted = await band($, 'terminal', { hasSurvey: true })
  expect(await mounted.find({ key: 'open' })).toBeUndefined()
  expect(await segments(mounted)).toEqual([])
  await mounted.unmount()

  // The band draws below what the host drew.
  mounted = await band($, 'terminal')
  const tree = (await mounted.drawn()) as { type: string; children?: { type: string }[] }
  expect(tree.type).toBe('Box')
  expect(tree.children?.[0]?.type).toBe('Box')
  expect(tree.children).toHaveLength(2)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})
