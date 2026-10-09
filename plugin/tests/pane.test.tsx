import { expect, test } from 'claude-code/testing'

import { GLOSSARY } from '../hooks/generated/copy.js'
import { elements, faults, prepare, reply, SITE, START, textOf } from './support'
import type { World } from './support'

const PROPS = { title: 'Glass', isFocused: true, bodyColumns: 110, placement: 'dock', ...SITE } as const

// One reply that leaves the context at 120k of a 160k summarising point, $0.50 spent.
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
const SECOND = { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 }

type Dollar = Parameters<typeof reply>[0]

async function pane($: Dollar, surface: 'terminal' | 'desktop') {
  return $.ui.mount({ plugin: 'claudeglass', surface, component: 'Pane', requestId: 'glass', props: PROPS as never })
}

async function started($: Dollar, world: World): Promise<void> {
  await $.session.start(START)
  await reply($, world, FIRST)
}

/** The lines the pane's text shows, one per drawn Text or Button. */
function lines(tree: unknown): string[] {
  return elements(tree)
    .filter(one => one.type === 'Text' || one.type === 'Button')
    .map(textOf)
    .filter(text => text !== '')
}

test('/glass opens the pane and says so', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  const answer = await $.command.run({ command: 'glass', args: '' })
  expect(answer.text).toBe('Glass opened.')
  expect(world.opened).toHaveLength(1)
  expect(world.opened[0]).toMatchObject({ id: 'glass', focus: true, closeOnEscape: true })
  expect(faults(world)).toEqual([])
})

test('before the first figures the Now page says it is waiting', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  const mounted = await pane($, 'terminal')
  const text = lines(await mounted.drawn()).join('\n')
  expect(text).toMatch(/Figures show after Claude's first reply/)
  expect(await mounted.find({ type: 'Button', key: 'explain:startup' })).toBeUndefined()
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the Now page first screen: headline, bar, three rows and a session line; every Button has a hotkey', async ($, on) => {
  const world = prepare(on)
  await started($, world)

  for (const surface of ['terminal', 'desktop'] as const) {
    const mounted = await pane($, surface)
    const tree = await mounted.drawn()
    const shown = lines(tree)

    expect(shown.some(text => /^Context is 60% full/.test(text))).toBe(true)
    const svg = await mounted.find({ type: 'Svg' })
    if (surface === 'desktop') {
      expect(svg).toBeDefined()
      expect(String(svg?.props.alt)).toMatch(/60% full/)
      expect(String(svg?.props.source).length).toBeLessThanOrEqual(40000)
    } else {
      expect(svg).toBeUndefined()
      expect(shown.some(text => /[█▓▒░]/.test(text))).toBe(true)
    }

    const rows = elements(tree).filter(one => one.type === 'Button' && String(one.props?.key ?? '').startsWith('explain:'))
    expect(rows).toHaveLength(3)
    expect(rows.map(row => row.props?.hotkey)).toEqual(['1', '2', '3'])
    expect(shown.some(text => /^This session \$0\.50 · 1 reply$/.test(text))).toBe(true)

    // Every Button, in the page bar, the rows and the footer, can be pressed from the keyboard.
    const buttons = elements(tree).filter(one => one.type === 'Button')
    expect(buttons.length).toBeGreaterThanOrEqual(6)
    for (const button of buttons) expect(String(button.props?.hotkey ?? '')).toMatch(/^[a-z0-9]$/)

    // The first screen is five lines of content at a docked width of 110 columns.
    for (const text of shown) expect(text.length).toBeLessThanOrEqual(110)
    await mounted.unmount()
  }
  expect(faults(world)).toEqual([])
})

test('pressing a row explains it in words, and again closes it', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  const mounted = await pane($, 'terminal')

  await mounted.press({ key: 'explain:conversation' })
  expect(lines(await mounted.drawn()).join('\n')).toContain(GLOSSARY.conversation.body)
  await mounted.press({ key: 'explain:conversation' })
  expect(lines(await mounted.drawn()).join('\n')).not.toContain(GLOSSARY.conversation.body)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('/glass detail adds the Detailed sections, and More detail does the same', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  await reply($, world, SECOND)
  const mounted = await pane($, 'terminal')
  expect(lines(await mounted.drawn()).join('\n')).not.toMatch(/Conversation summaries/)

  const toggled = await $.command.run({ command: 'glass', args: 'detail' })
  expect(toggled.text).toMatch(/detail/i)
  const detailed = lines(await mounted.drawn()).join('\n')
  expect(detailed).toMatch(/Conversation summaries/)

  await mounted.press({ key: 'more' })
  expect(lines(await mounted.drawn()).join('\n')).not.toMatch(/Conversation summaries/)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the page bar names Now with its key, and the footer has help and the dashboard link', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  const mounted = await pane($, 'desktop')

  const now = await mounted.find({ type: 'Button', key: 'page:now' })
  expect(now?.props.hotkey).toBe('n')
  expect(String((await mounted.find({ type: 'Link' }))?.props.href)).toMatch(/^http:\/\/localhost:8792\//)

  await mounted.press({ key: 'link' })
  expect(world.copied).toHaveLength(1)
  expect(world.copied[0]).toMatch(/^http:\/\/127\.0\.0\.1:8792\/#\/spend\/sessions\?session=session-one$/)

  await mounted.press({ key: 'help' })
  expect(lines(await mounted.drawn()).join('\n')).toContain(GLOSSARY.conversation.title)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the compact command is offered as a copy button only when summarising is near', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 58000, output: 0 }], usd: 0.2 })
  let mounted = await pane($, 'terminal')
  expect(await mounted.find({ type: 'Button', key: 'copy' })).toBeUndefined()
  await mounted.unmount()

  // 150k of a 160k point.
  await reply($, world, { steps: [{ cacheRead: 100000, cacheWrite: 50000, output: 200 }], usd: 0.2 })
  mounted = await pane($, 'terminal')
  const copy = await mounted.find({ type: 'Button', key: 'copy' })
  expect(copy?.props.hotkey).toBe('c')
  await mounted.press({ key: 'copy' })
  expect(world.copied).toEqual(['/compact'])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})
