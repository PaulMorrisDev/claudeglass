import { expect, test } from 'claude-code/testing'

import { GLOSSARY } from '../hooks/generated/copy.js'
import { elements, faults, prepare, reply, settingsFile, SITE, START, textOf } from './support'
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

test('the Now page first screen: headline, bar, three text rows and a session line; hotkeys only on the page bar and the main action', async ($, on) => {
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
      // The mark's label is drawn under the mark inside the drawing, not as a line of text under the bar's left end.
      expect(String(svg?.props.source)).toMatch(/<text [^>]*text-anchor="end"[^>]*>summarises here<\/text>/)
      expect(shown.some(text => /summarises here/.test(text))).toBe(false)
    } else {
      expect(shown.some(text => /▲ summarises here$/.test(text))).toBe(true)
      expect(svg).toBeUndefined()
      expect(shown.some(text => /[█▓▒░]/.test(text))).toBe(true)
    }

    // The rows are text with the figure at the right edge, never Buttons; the estimate is said once.
    expect(elements(tree).filter(one => one.type === 'Button' && String(one.props?.key ?? '').startsWith('explain:'))).toHaveLength(0)
    expect(shown).toContain('Startup context')
    expect(shown).toContain('Conversation so far')
    expect(shown).toContain('Tool output kept in context')
    expect(shown.filter(text => text === 'Tool output is an estimate.')).toHaveLength(1)
    expect(shown.some(text => /\(est\.\)/.test(text))).toBe(false)
    expect(shown.some(text => /^This session \$0\.50 · 1 reply$/.test(text))).toBe(true)

    // Hotkeys: the page bar's keys only (the footer has none), and at most one primary action.
    const buttons = elements(tree).filter(one => one.type === 'Button')
    expect(buttons.length).toBeGreaterThanOrEqual(5)
    const keyed = buttons.filter(button => button.props?.hotkey !== undefined)
    expect(keyed.map(button => String(button.props?.key))).toEqual(expect.arrayContaining(['page:agents']))
    for (const button of keyed) expect(String(button.props?.key)).toMatch(/^(page:|copy$)/)
    for (const button of keyed) expect(['n', 'a', 'k', 's', 'c']).toContain(button.props?.hotkey)
    expect(buttons.filter(button => button.props?.variant === 'primary').map(button => String(button.props?.key))).toEqual(['copy'])

    // The first screen is five lines of content at a docked width of 110 columns.
    for (const text of shown) expect(text.length).toBeLessThanOrEqual(110)
    await mounted.unmount()
  }
  expect(faults(world)).toEqual([])
})

test('on a subscription the session line gives the 5-hour share once the limit has moved, else the weekly share, else the list price', async ($, on) => {
  const billing = (share: number | null) => settingsFile({ billing: { mode: 'subscription', share_per_usd: share, period_label: 'weekly', basis: 'list' } })

  // No share known and no limit reading: the list price.
  const world = prepare(on, { settings: billing(null) })
  await started($, world)
  const mounted = await pane($, 'terminal')
  expect(lines(await mounted.drawn())).toContain('This session 0.50 USD list-price equivalent · 1 reply')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('on a subscription with a weekly share known, the session line gives it', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ billing: { mode: 'subscription', share_per_usd: 0.02, period_label: 'weekly', basis: 'list' } }) })
  await started($, world)
  const mounted = await pane($, 'terminal')
  expect(lines(await mounted.drawn())).toContain('This session about 0.01% of your weekly · 1 reply')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('on a subscription the session line gives the 5-hour share when the limit has moved', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ billing: { mode: 'subscription', share_per_usd: 0.02, period_label: 'weekly', basis: 'list' } }) })
  const resetsAt = new Date(world.clock.now() + 4 * 60 * 60 * 1000).toISOString()
  world.limits = [{ kind: 'five_hour', percentUsed: 10, resetsAt }]
  await started($, world)
  world.limits = [{ kind: 'five_hour', percentUsed: 15, resetsAt }]
  await reply($, world, { ...SECOND, usd: 0.5 })
  const mounted = await pane($, 'terminal')
  const line = lines(await mounted.drawn()).find(text => /^This session /.test(text))
  // $1.00 spent, 5 points of the limit for the last $0.50: 10 points per dollar.
  expect(line).toBe('This session about 10.0% of your 5-hour limit · 2 replies')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Detailed explains each row in words under it, and a row is not pressable', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  const mounted = await pane($, 'terminal')
  expect(lines(await mounted.drawn()).join('\n')).not.toContain(GLOSSARY.conversation.body)
  expect(await mounted.find({ type: 'Button', key: 'explain:conversation' })).toBeUndefined()

  await $.command.run({ command: 'glass', args: 'detail' })
  const shown = lines(await mounted.drawn()).join('\n')
  expect(shown).toContain(GLOSSARY.startup.body)
  expect(shown).toContain(GLOSSARY.conversation.body)
  expect(shown).toContain(GLOSSARY.output.body)
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

test('the page bar marks Now and keys the others, and the footer has help and the dashboard link', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  const mounted = await pane($, 'desktop')

  // The page shown is marked as text; the others are the bar's keys.
  expect(await mounted.find({ type: 'Button', key: 'page:now' })).toBeUndefined()
  expect((await mounted.find({ type: 'Button', key: 'page:agents' }))?.props.hotkey).toBe('a')
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
  expect(copy?.props.variant).toBe('primary')
  expect(copy?.props.autoFocus).toBe(true)
  expect(String(copy?.props.label)).toBe('Copy /compact')
  expect(elements(await mounted.drawn()).filter(one => one.type === 'Button' && one.props?.variant === 'primary')).toHaveLength(1)
  await mounted.press({ key: 'copy' })
  expect(world.copied).toEqual(['/compact'])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a press of one copy button relabels only that button "Copied.", whichever it is', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 58000, output: 0 }], usd: 0.2 })
  // 150k of a 160k point: the soon state, which offers Copy /compact beside the footer's Copy link.
  await reply($, world, { steps: [{ cacheRead: 100000, cacheWrite: 50000, output: 200 }], usd: 0.2 })
  const mounted = await pane($, 'terminal')
  const labels = async () => [String((await mounted.find({ type: 'Button', key: 'copy' }))?.props.label), String((await mounted.find({ type: 'Button', key: 'link' }))?.props.label)]
  expect(await labels()).toEqual(['Copy /compact', 'Copy link'])

  // The footer's Copy link: it says Copied.; Copy /compact is as it was.
  await mounted.press({ key: 'link' })
  expect(await labels()).toEqual(['Copy /compact', 'Copied.'])
  expect(world.copied).toHaveLength(1)

  // The other way round, on a fresh mark (it goes after a few seconds): Copy /compact says Copied.; Copy link is as it was.
  await world.clock.advance(3500)
  expect(await labels()).toEqual(['Copy /compact', 'Copy link'])
  await mounted.press({ key: 'copy' })
  expect(await labels()).toEqual(['Copied.', 'Copy link'])
  expect(world.copied.at(-1)).toBe('/compact')

  // A press of the other while the first still shows moves the mark: only one button says Copied. at a time.
  await mounted.press({ key: 'link' })
  expect(await labels()).toEqual(['Copy /compact', 'Copied.'])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})
