import { expect, test } from 'claude-code/testing'

import { elements, faults, prepare, reply, settingsFile, SITE, START, textOf } from './support'
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
    expect(texts[1]).toBe('$0.50 this session (+$0.50)')

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

const SUBSCRIPTION = settingsFile({ billing: { mode: 'subscription', share_per_usd: null, period_label: 'weekly usage limit', basis: 'list' } })

/** The 5-hour limit at `pct` percent used, `hours` of the 5 already gone. */
function fiveHour(world: World, pct: number, hours: number): void {
  const resetsAt = new Date(world.clock.now() + (5 - hours) * 60 * 60 * 1000).toISOString()
  world.limits = [{ kind: 'five_hour', percentUsed: pct, resetsAt }]
}

test('the band narrows by short forms first, then drops what is not urgent; the bar needs every segment at full length', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  // Past the cache's lifetime with a big context: the cold segment is on.
  await world.clock.advance(400 * 1000)

  const at = async (columns: number) => {
    const mounted = await band($, 'desktop', { bodyColumns: columns })
    const texts = await segments(mounted)
    const bar = (await mounted.find({ type: 'Svg' })) !== undefined
    await mounted.unmount()
    return { texts, bar }
  }

  const wide = await at(200)
  expect(wide.texts).toHaveLength(3)
  expect(wide.texts[1]).toBe('$0.50 this session (+$0.50)')
  expect(wide.bar).toBe(true)

  // Everything at full length fits (153 cells with the button), the bar does not (it needs 16 more).
  const full = await at(160)
  expect(full.texts).toHaveLength(3)
  expect(full.texts[0]).toMatch(/summarises at 160k$/)
  expect(full.bar).toBe(false)

  // The spend goes short first.
  const spendShort = await at(145)
  expect(spendShort.texts[0]).toMatch(/Claude summarises at 160k$/)
  expect(spendShort.texts[1]).toBe('$0.50 (+$0.50)')
  expect(spendShort.texts[2]).toMatch(/^cache expired \(est\.\): the next send costs about \$\d/)

  // Then the context.
  const bothShort = await at(135)
  expect(bothShort.texts[0]).toBe('◕ Compact soon · 60% full · summarises at 160k')
  expect(bothShort.texts[1]).toBe('$0.50 (+$0.50)')
  expect(bothShort.texts).toHaveLength(3)

  // Then the spend goes, because the cold segment is urgent.
  const noSpend = await at(120)
  expect(noSpend.texts).toHaveLength(2)
  expect(noSpend.texts[1]).toMatch(/^cache expired \(est\.\): the next send costs about \$\d/)

  // The cold segment goes short.
  const coldShort = await at(100)
  expect(coldShort.texts).toHaveLength(2)
  expect(coldShort.texts[1]).toMatch(/^cache expired \(est\.\): about \$\d/)

  // Then it goes, and the spend, which went before it, stays gone: an urgent segment outlasts the spend.
  const tight = await at(80)
  expect(tight.texts).toHaveLength(1)
  expect(tight.texts[0]).toMatch(/60% full/)
  const tiny = await at(30)
  expect(tiny.texts).toHaveLength(1)
  expect(tiny.texts[0]).toMatch(/60% full/)
  expect(tiny.bar).toBe(false)
  expect(faults(world)).toEqual([])
})

/** The band's segments at 110 columns once the cache has run out over a big context. */
async function coldAt110($: Parameters<typeof reply>[0], world: World): Promise<string[]> {
  await started($, world)
  await world.clock.advance(400 * 1000)
  const mounted = await band($, 'terminal', { bodyColumns: 110 })
  const texts = await segments(mounted)
  await mounted.unmount()
  return texts
}

test('at 110 columns on the API the cold-cache segment survives and the spend gives way', async ($, on) => {
  const world = prepare(on)
  const texts = await coldAt110($, world)
  expect(texts).toHaveLength(2)
  expect(texts[0]).toMatch(/60% full/)
  expect(texts[1]).toMatch(/^cache expired \(est\.\): about \$\d/)
  expect(faults(world)).toEqual([])
})

test('at 110 columns on a subscription the cold-cache segment survives, and the band shows no dollars', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  fiveHour(world, 12, 1)
  const texts = await coldAt110($, world)
  expect(texts).toHaveLength(2)
  expect(texts[0]).toMatch(/60% full/)
  expect(texts[1]).toMatch(/^cache expired \(est\.\)/)
  expect(texts.join(' ')).not.toMatch(/\$|USD|list-price/)
  expect(faults(world)).toEqual([])
})

test('on a subscription the simple band gives the 5-hour figure for the spend, and the detailed band adds the list price', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  fiveHour(world, 12, 1)
  await started($, world)

  let mounted = await band($, 'terminal')
  let texts = await segments(mounted)
  await mounted.unmount()
  expect(texts[1]).toBe('5-hour limit 12% used')
  expect(texts.join(' ')).not.toMatch(/\$|USD|this session/)

  expect((await $.command.run({ command: 'glass', args: 'detail' } as never)).text).toBe('Detailed view is on.')
  mounted = await band($, 'terminal', { bodyColumns: 300 })
  texts = await segments(mounted)
  await mounted.unmount()
  expect(texts[1]).toMatch(/list-price equivalent this session · 5-hour limit 12% used/)
  expect(faults(world)).toEqual([])
})

test('on a subscription the 5-hour share is what amounts are written in once the limit has moved', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  // One window throughout: another $0.50 moves the limit from 10% to 15%, which is 10% of the limit per dollar.
  const resetsAt = new Date(world.clock.now() + 4 * 60 * 60 * 1000).toISOString()
  world.limits = [{ kind: 'five_hour', percentUsed: 10, resetsAt }]
  await started($, world)
  world.limits = [{ kind: 'five_hour', percentUsed: 15, resetsAt }]
  await reply($, world, { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.5 })
  await world.clock.advance(400 * 1000)

  const mounted = await band($, 'terminal', { bodyColumns: 200 })
  const texts = await segments(mounted)
  await mounted.unmount()
  expect(texts[2]).toMatch(/^cache expired \(est\.\): the next send costs about \d+\.\d+% of your 5-hour limit$/)
  expect(faults(world)).toEqual([])
})

test('the urgent segments come in order: a limit that runs out within the hour, the cache ending, a limit that runs out later', async ($, on) => {
  const world = prepare(on)
  fiveHour(world, 80, 3)
  await started($, world)
  // 80% gone in 3 hours: 45 minutes left at this pace. That wins over the cache about to end.
  await world.clock.advance(270 * 1000)
  let mounted = await band($, 'terminal', { bodyColumns: 200 })
  let texts = await segments(mounted)
  await mounted.unmount()
  expect(texts[2]).toMatch(/^5-hour limit 80% used, runs out about/)

  // 60% gone in 2 hours: it runs out in 80 minutes, before it resets. The cache ending comes first.
  fiveHour(world, 60, 2)
  await reply($, world, { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
  await world.clock.advance(270 * 1000)
  mounted = await band($, 'terminal', { bodyColumns: 200 })
  texts = await segments(mounted)
  await mounted.unmount()
  expect(texts[2]).toMatch(/^resend soon/)

  // With the cache fresh again, the later limit shows.
  await reply($, world, { steps: [{ cacheRead: 121000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
  mounted = await band($, 'terminal', { bodyColumns: 200 })
  texts = await segments(mounted)
  await mounted.unmount()
  expect(texts[2]).toMatch(/^5-hour limit 60% used, runs out about/)
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

test('with no summarising point in the figures, the point is the window less the autocompact buffer', async ($, on) => {
  const world = prepare(on)
  // As on a machine whose Claude Code names only the window and the buffer: 300k, 33k of it the buffer.
  world.shape = { window: 300000, buffer: 33000 }
  await started($, world)

  const mounted = await band($, 'terminal')
  const texts = await segments(mounted)
  await mounted.unmount()
  // /context's percent is of the whole window (120k of 300k), and Claude summarises at 267k.
  expect(texts[0]).toBe('○ 40% full · Claude summarises at 267k')
  expect(faults(world)).toEqual([])
})

test('a snapshot an older version left in state (the mod reloaded mid-session) still draws', async ($, on) => {
  const world = prepare(on)
  // $.state outlives a reload: what an older mod kept has no agents or piece, and a shorter turn.
  let aged = false
  on('state.get', async (_$, e, next) => {
    const got = await next(e)
    const held = got.value as { value?: Record<string, unknown>; version: number } | undefined
    if (!aged || e.plugin !== 'claudeglass' || e.key !== 'snap' || held?.value === undefined) return got
    const old = { ...held.value, turn: { rebuilds: [], big: null } }
    delete old.agents
    delete old.piece
    return { value: { ...held, value: old } } as never
  })
  await started($, world)
  aged = true

  for (const surface of ['terminal', 'desktop'] as const) {
    const mounted = await band($, surface)
    const texts = await segments(mounted)
    expect(texts[0]).toMatch(/60% full/)
    expect(await mounted.find({ type: 'Button', key: 'open' })).toBeDefined()
    await mounted.unmount()
  }
  expect(faults(world)).toEqual([])
})
