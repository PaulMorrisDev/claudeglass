import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { emptyReview } from '../hooks/compaction-memory'
import type { GlassReview } from '../types'
import { elements, faults, prepare, reply, SITE, START, textOf } from './support'
import type { World } from './support'

const PROPS = { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 160, ...SITE }

// One reply that leaves the context at 40k of a 160k summarising point: plenty of room.
const ROOMY = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 38000, output: 0 }], usd: 0.1 }
// One that leaves it at 120k: close to the point.
const TIGHT = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
const LATER = { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 }

/** A conversation with two constraints. */
const MESSAGES = [
  { role: 'user', text: "Never use port 8765 for the dev server, it is reserved. Don't read any code yet; wait for the other session to finish.", toolUses: [] },
  { role: 'assistant', text: 'Working on it.', toolUses: [] },
]
// A summary that drops both, and one that keeps both.
const LOST = ['This session is being continued from a previous conversation.', 'The user asked for rate limiting on the API.'].join('\n')
const KEPT = [
  'This session is being continued from a previous conversation.',
  "The user said never use port 8765 for the dev server, it is reserved. Don't read any code yet; wait for the other session to finish.",
].join('\n')

type Mounted = Awaited<ReturnType<Engine['ui']['mount']>>

async function band($: Engine, surface: 'terminal' | 'desktop' = 'terminal', props: Record<string, unknown> = {}) {
  return $.ui.mount({ plugin: 'claudeglass', surface, component: 'AbovePrompt', props: { ...PROPS, ...props } as never })
}

/** The band's texts: the dot, the state word (padded), and the line when there is room. */
async function texts(mounted: Mounted): Promise<string[]> {
  return elements(await mounted.drawn())
    .filter(one => one.type === 'Text')
    .map(textOf)
    .filter(text => text !== '')
}

/** The band's Buttons as they are drawn. */
async function buttons(mounted: Mounted) {
  return elements(await mounted.drawn())
    .filter(one => one.type === 'Button')
    .map(one => ({ key: String(one.props?.key), label: String(one.props?.label), hotkey: one.props?.hotkey as string | undefined, variant: one.props?.variant as string | undefined, dimColor: one.props?.dimColor as boolean | undefined }))
}

/** What the test sees of the review state: the last value written. */
function watchReview(on: On): { readonly latest: GlassReview } {
  let latest = emptyReview()
  on('state.set', async (_$, e, next) => {
    if (e.plugin === 'claudeglass' && e.key === 'review') latest = e.value as GlassReview
    return next(e)
  })
  return {
    get latest() {
      return latest
    },
  }
}

/** The toasts the mod raised, and the compactions that reached the engine. */
function bottoms(on: On, world: World, summary: string = LOST) {
  const seen: { toasts: string[]; compacts: { trigger: string; instructions?: string }[]; mode: 'write' | 'throw' | 'skip' } = { toasts: [], compacts: [], mode: 'write' }
  world.transcript = [...MESSAGES]
  on('session.compact', (_$, e) => {
    seen.compacts.push(e)
    // The engine refuses a compaction while a reply runs; a hook may veto one.
    if (seen.mode === 'throw') throw new Error('a turn is running')
    if (seen.mode === 'skip') return { skip: 'held for review' } as never
    return { messages: [{ role: 'user', text: summary, toolUses: [] }], tokensBefore: 150000, tokensAfter: 30000 } as never
  })
  on('ui.toast', (_$, e) => {
    seen.toasts.push(e.text)
    return { value: undefined } as never
  })
  return seen
}

/** A session with one reply, the band drawn once before any summary (it forgets the toast of an earlier summary). */
async function started($: Engine, world: World, one: typeof ROOMY): Promise<void> {
  await $.session.start(START)
  await reply($, world, one)
  const mounted = await band($)
  await mounted.unmount()
}

test('before there are figures the band adds nothing; with a survey up it adds nothing', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)

  let mounted = await band($)
  expect(await mounted.find({ type: 'Button' })).toBeUndefined()
  expect(await texts(mounted)).toEqual([])
  expect(await mounted.drawn()).toMatchObject({ type: 'Box' })
  await mounted.unmount()

  await reply($, world, ROOMY)
  mounted = await band($, 'terminal', { hasSurvey: true })
  expect(await mounted.find({ type: 'Button' })).toBeUndefined()
  await mounted.unmount()

  // The band draws below what the host drew.
  mounted = await band($)
  const tree = (await mounted.drawn()) as { type: string; children?: { type: string }[] }
  expect(tree.type).toBe('Box')
  expect(tree.children?.[0]?.type).toBe('Box')
  expect(tree.children).toHaveLength(2)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Fine: plenty of room says how full the context is and how many replies are left; the one Button opens Glass', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world, ROOMY)
  await reply($, world, { steps: [{ cacheRead: 40000, cacheWrite: 10000, output: 200 }], usd: 0.05 })
  await reply($, world, { steps: [{ cacheRead: 50000, cacheWrite: 10000, output: 200 }], usd: 0.05 })

  for (const surface of ['terminal', 'desktop'] as const) {
    const mounted = await band($, surface)
    const shown = await texts(mounted)
    expect(shown[0]).toBe('●')
    expect(shown[1]).toBe('Fine   ')
    expect(shown[2]).toMatch(/^3\d% full · about \d+ replies before a summary$/)
    expect(await buttons(mounted)).toEqual([{ key: 'open', label: 'Glass', hotkey: 'g', variant: undefined, dimColor: undefined }])
    // The old segments and the bar are gone.
    expect(shown.join(' ')).not.toMatch(/\$|cache|limit|agent/)
    expect(await mounted.find({ type: 'Svg' })).toBeUndefined()

    await mounted.press({ key: 'open' } as never)
    expect(world.opened.at(-1)).toMatchObject({ id: 'glass' })
    await mounted.unmount()
  }
  expect(faults(world)).toEqual([])
})

test('Tight: close to the summarising point, the Button summarises, and says how many items it keeps', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world, TIGHT)

  const mounted = await band($)
  const shown = await texts(mounted)
  expect(shown.slice(0, 2)).toEqual(['●', 'Tight  '])
  expect(shown[2]).toMatch(/^60% full · /)
  const found = await buttons(mounted)
  expect(found).toHaveLength(1)
  expect(found[0]).toMatchObject({ key: 'summarise', hotkey: 's', variant: 'primary' })
  expect(found[0]?.label).toMatch(/^Summarise, keeping \d+ items$|^Summarise, keeping 1 item$/)
  expect(found[0]?.dimColor).toBeUndefined()
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Tight with nothing to keep says so in the Button', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  world.transcript = []
  await started($, world, TIGHT)
  const mounted = await band($)
  expect((await buttons(mounted))[0]?.label).toBe('Summarise now')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Tight with the hold on offers a review first; the press opens the pane and summarises nothing', async ($, on) => {
  // The hold is on, as the pane's switch leaves it in the store.
  const world = prepare(on, { store: { hold: true } })
  const seen = bottoms(on, world)
  await started($, world, TIGHT)

  const mounted = await band($)
  expect(await buttons(mounted)).toEqual([{ key: 'review', label: 'Review before summarising', hotkey: 's', variant: 'primary', dimColor: undefined }])
  await mounted.press({ key: 'review' } as never)
  expect(world.opened.at(-1)).toMatchObject({ id: 'glass' })
  expect(seen.compacts).toEqual([])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the Summarise press starts a summary from the plugin and clears its busy mark', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  const seen = bottoms(on, world)
  await started($, world, TIGHT)

  let mounted = await band($)
  await mounted.press({ key: 'summarise' } as never)
  expect(seen.compacts).toHaveLength(1)
  expect(watch.latest.ui.action ?? null).toBeNull()
  expect(seen.toasts.filter(text => /reply is running/.test(text))).toEqual([])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a summary refused while a reply runs says so, and a summary a hook holds says why', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  const seen = bottoms(on, world)
  seen.mode = 'throw'
  await started($, world, TIGHT)

  const mounted = await band($)
  await mounted.press({ key: 'summarise' } as never)
  expect(seen.toasts).toEqual(['A reply is running. Summarise when it ends.'])
  expect(watch.latest.ui.action ?? null).toBeNull()

  seen.mode = 'skip'
  await mounted.press({ key: 'summarise' } as never)
  expect(seen.toasts.at(-1)).toBe('The summary was held: held for review')
  expect(watch.latest.ui.action ?? null).toBeNull()
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('while a reply runs the Summarise Button is dim and busy, and the press does nothing', async ($, on) => {
  const world = prepare(on)
  const seen = bottoms(on, world)
  await started($, world, TIGHT)

  const mounted = await band($, 'terminal', { isWorking: true })
  // Dim while the reply runs, but the label is Summarise's own: nothing is being summarised.
  expect(await buttons(mounted)).toEqual([{ key: 'summarise', label: 'Summarise, keeping 2 items', hotkey: 's', variant: 'primary', dimColor: true }])
  await mounted.press({ key: 'summarise' } as never)
  expect(seen.compacts).toEqual([])
  expect(seen.toasts).toEqual([])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Review: a summary that lost items says how many, in red, and the one Button puts them back', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  const seen = bottoms(on, world, LOST)
  await started($, world, TIGHT)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  expect(watch.latest.missing).toBe(2)

  let mounted = await band($)
  const shown = await texts(mounted)
  expect(shown.slice(0, 2)).toEqual(['●', 'Review '])
  expect(shown[2]).toBe('Summary lost 2 of 2 items')
  expect(await buttons(mounted)).toEqual([{ key: 'readd', label: 'Re-add 2 items', hotkey: 'r', variant: 'primary', dimColor: undefined }])

  // The colours are the theme's: error for Review.
  const colors = elements(await mounted.drawn())
    .filter(one => one.type === 'Text')
    .map(one => one.props?.color)
  expect(colors.slice(0, 2)).toEqual(['error', 'error'])

  // One toast for the summary, however often the band is drawn.
  await mounted.unmount()
  mounted = await band($)
  await mounted.unmount()
  expect(seen.toasts).toEqual(['The summary lost 2 of 2 items. Press Re-add to put them back.'])

  // The press does what the pane's Re-add does: the items are put back, once, and counted for the sensor.
  mounted = await band($)
  await mounted.press({ key: 'readd' } as never)
  expect(watch.latest.readded.map(one => one.text)).toEqual([expect.stringMatching(/8765/), expect.stringMatching(/read any code/)])
  expect(Object.keys(watch.latest.receipts)).toHaveLength(2)
  expect(watch.latest.seen).toBe(true)
  // The press says what it did, as the pane's Re-add does.
  expect(seen.toasts.at(-1)).toBe('Claude is told these again with its next reply.')
  await mounted.unmount()

  // Looked at: the band is no longer in Review.
  mounted = await band($)
  expect((await texts(mounted))[1]).toBe('Tight  ')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a summary that lost nothing raises no toast', async ($, on) => {
  const world = prepare(on)
  const seen = bottoms(on, world, KEPT)
  await started($, world, TIGHT)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)

  const mounted = await band($)
  await mounted.unmount()
  expect(seen.toasts).toEqual([])
  expect(faults(world)).toEqual([])
})

test('Kept: a summary that lost nothing says so until the next reply ends, then it is Fine again', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  bottoms(on, world, KEPT)
  await started($, world, ROOMY)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  expect(watch.latest.missing).toBe(0)

  let mounted = await band($)
  const shown = await texts(mounted)
  expect(shown.slice(0, 2)).toEqual(['●', 'Kept   '])
  expect(shown[2]).toMatch(/^Summary kept all \d+ items$|^Summary kept the 1 item$/)
  expect(await buttons(mounted)).toEqual([{ key: 'open', label: 'Glass', hotkey: 'g', variant: undefined, dimColor: undefined }])
  const colors = elements(await mounted.drawn())
    .filter(one => one.type === 'Text')
    .map(one => one.props?.color)
  expect(colors.slice(0, 2)).toEqual(['success', 'success'])
  await mounted.unmount()

  await reply($, world, { gap: 1000, steps: [{ cacheRead: 40000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
  mounted = await band($)
  expect((await texts(mounted))[1]).toBe('Fine   ')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the colours follow the theme: success for Fine, warning for Tight', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world, ROOMY)
  let mounted = await band($)
  let colors = elements(await mounted.drawn())
    .filter(one => one.type === 'Text')
    .map(one => one.props?.color)
  expect(colors.slice(0, 2)).toEqual(['success', 'success'])
  await mounted.unmount()

  await reply($, world, LATER)
  world.figures(120000, 0.7)
  mounted = await band($)
  colors = elements(await mounted.drawn())
    .filter(one => one.type === 'Text')
    .map(one => one.props?.color)
  expect(colors.slice(0, 2)).toEqual(['warning', 'warning'])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the line gives way to the state word and the Button as the band narrows', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world, TIGHT)

  const at = async (columns: number) => {
    const mounted = await band($, 'terminal', { bodyColumns: columns })
    const shown = await texts(mounted)
    const found = await buttons(mounted)
    await mounted.unmount()
    return { shown, found }
  }
  const wide = await at(160)
  expect(wide.shown).toHaveLength(3)
  expect(wide.shown[2]).toMatch(/ · /)

  // The runway goes first, then the line; the state word and the one Button stay.
  const middle = await at(60)
  expect(middle.shown[2]).toBe('60% full')
  const narrow = await at(40)
  expect(narrow.shown).toEqual(['●', 'Tight  '])
  expect(narrow.found).toHaveLength(1)
  expect(faults(world)).toEqual([])
})

test('a snapshot an older version left in state (the mod reloaded mid-session) still draws', async ($, on) => {
  const world = prepare(on)
  // $.state outlives a reload: what an older mod kept has no agents.
  let aged = false
  on('state.get', async (_$, e, next) => {
    const got = await next(e)
    const held = got.value as { value?: Record<string, unknown>; version: number } | undefined
    if (!aged || e.plugin !== 'claudeglass' || e.key !== 'snap' || held?.value === undefined) return got
    const old = { ...held.value }
    delete old.agents
    return { value: { ...held, value: old } } as never
  })
  await started($, world, TIGHT)
  aged = true

  for (const surface of ['terminal', 'desktop'] as const) {
    const mounted = await band($, surface)
    expect((await texts(mounted))[2]).toMatch(/^60% full/)
    expect(await mounted.find({ type: 'Button', key: 'summarise' })).toBeDefined()
    await mounted.unmount()
  }
  expect(faults(world)).toEqual([])
})

test('with no summarising point in the figures, the point is the window less the autocompact buffer', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  // As on a machine whose Claude Code names only the window and the buffer: 300k, 33k of it the buffer.
  world.shape = { window: 300000, buffer: 33000 }
  await started($, world, TIGHT)

  const mounted = await band($)
  // /context's percent is of the whole window (120k of 300k), and Claude summarises at 267k.
  expect((await texts(mounted))[2]).toBe('40% full · Claude summarises at 267k')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the busy label shows only while a summary runs; a second press before it ends starts none', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  let open: () => void = () => {}
  const gate = new Promise<void>(resolve => (open = resolve))
  let compactions = 0
  world.transcript = [...MESSAGES]
  on('session.compact', async () => {
    compactions += 1
    await gate
    return { messages: [{ role: 'user', text: LOST, toolUses: [] }], tokensBefore: 150000, tokensAfter: 30000 } as never
  })
  await started($, world, TIGHT)

  // A reply running: Summarise keeps its own label, dim.
  let mounted = await band($, 'terminal', { isWorking: true })
  expect((await buttons(mounted))[0]).toMatchObject({ label: 'Summarise, keeping 2 items', dimColor: true })
  await mounted.unmount()

  // A summary running: the label says so, with an ellipsis, and a second press starts nothing.
  mounted = await band($)
  const first = mounted.press({ key: 'summarise' } as never)
  const second = mounted.press({ key: 'summarise' } as never)
  await world.clock.advance(10)
  expect(watch.latest.ui.action).toBe('summarise')
  const busy = await band($)
  expect((await buttons(busy))[0]).toMatchObject({ label: 'Summarising…', dimColor: true })
  await busy.unmount()
  open()
  await Promise.all([first, second])
  expect(compactions).toBe(1)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('opening Glass from the command marks the summary as looked at, so the band leaves Review', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  bottoms(on, world)
  await started($, world, TIGHT)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  expect(watch.latest.seen).toBe(false)
  let mounted = await band($)
  expect((await texts(mounted))[1]).toBe('Review ')
  await mounted.unmount()

  await $.command.run({ command: 'glass', args: '' } as never)
  expect(watch.latest.seen).toBe(true)
  mounted = await band($)
  expect((await texts(mounted))[1]).toBe('Tight  ')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})
