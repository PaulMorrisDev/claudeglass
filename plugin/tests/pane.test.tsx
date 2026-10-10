import { expect, test } from 'claude-code/testing'

import { GLOSSARY } from '../hooks/generated/copy.js'
import { elements, faults, prepare, reply, settingsFile, SITE, START, textOf } from './support'
import type { Step, World } from './support'

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

    // The rows are text with the figure at the right edge, never Buttons; the one "(est.)" is in the tool output row's name.
    expect(elements(tree).filter(one => one.type === 'Button' && String(one.props?.key ?? '').startsWith('explain:'))).toHaveLength(0)
    expect(shown.some(text => /^[■█] ?Startup context$/.test(text))).toBe(true)
    expect(shown.some(text => /^[■▓] ?Conversation so far$/.test(text))).toBe(true)
    expect(shown.some(text => /^[■▒] ?Tool output kept in context \(est\.\)$/.test(text))).toBe(true)
    expect(shown.some(text => /estimate/i.test(text))).toBe(false)
    expect(shown.filter(text => /\(est\.\)/.test(text))).toHaveLength(1)
    // Each row's mark keys it to the bar: a coloured square on the desktop, the bar's own shade on the terminal.
    const marks = elements(tree).filter(one => one.type === 'Text' && /^[■█▓▒] $/.test(textOf(one)))
    if (surface === 'desktop') expect(marks.map(one => one.props?.color)).toEqual(['error', 'claude', 'warning'])
    else expect(shown.filter(text => /^[█▓▒] /.test(text))).toHaveLength(3)
    expect(shown.some(text => /^This session \$0\.50 · 1 reply$/.test(text))).toBe(true)

    // Hotkeys: the page bar's keys only (the footer has none), and at most one primary action.
    const buttons = elements(tree).filter(one => one.type === 'Button')
    expect(buttons.length).toBeGreaterThanOrEqual(5)
    const keyed = buttons.filter(button => button.props?.hotkey !== undefined)
    expect(keyed.map(button => String(button.props?.key))).toEqual(expect.arrayContaining(['page:agents']))
    for (const button of keyed) expect(String(button.props?.key)).toMatch(/^(page:|copy$)/)
    for (const button of keyed) expect(['n', 'a', 'k', 's', 'c']).toContain(button.props?.hotkey)
    expect(buttons.filter(button => button.props?.variant === 'primary').map(button => String(button.props?.key))).toEqual(['copy'])

    // The page's column leaves a blank line between its sections.
    const column = elements(tree).find(one => one.type === 'Box' && one.props?.gap === 1)
    expect(column).toBeDefined()
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

const SUBSCRIPTION = settingsFile({ billing: { mode: 'subscription', share_per_usd: 0.02, period_label: 'weekly', basis: 'list' } })
const HOUR = 60 * 60 * 1000

test('a session that began in an earlier 5-hour window is not given as a share of this one (it read 112% beside a 64% bar)', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  await $.session.start(START)
  // The session begins now, with no reading of the limit, and spends $10.
  await reply($, world, { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 10 })
  // Six hours later the limit is in a new window and moves 5 points for the next $0.50: 10 points per dollar.
  await world.clock.advance(6 * HOUR)
  const resetsAt = new Date(world.clock.now() + 4 * HOUR).toISOString()
  world.limits = [{ kind: 'five_hour', percentUsed: 10, resetsAt }]
  await reply($, world, { ...SECOND, usd: 0.5 })
  world.limits = [{ kind: 'five_hour', percentUsed: 15, resetsAt }]
  await reply($, world, { ...SECOND, usd: 0.5 })

  const mounted = await pane($, 'terminal')
  const line = lines(await mounted.drawn()).find(text => /^This session /.test(text))
  // $11 at 10 points a dollar would read 110% of the 5-hour limit; the session began before this window, so it is the weekly share.
  expect(line).toBe('This session about 0.22% of your weekly · 3 replies')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a session that began before this window is priced by the weekly share even when its 5-hour share would stay under 100%', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  await $.session.start(START)
  await reply($, world, { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 4.5 })
  // Six hours later the limit is in a new window and moves 5 points for the next $0.50: 10 points per dollar, so $5.50 would read 55%.
  await world.clock.advance(6 * HOUR)
  const resetsAt = new Date(world.clock.now() + 4 * HOUR).toISOString()
  world.limits = [{ kind: 'five_hour', percentUsed: 10, resetsAt }]
  await reply($, world, { ...SECOND, usd: 0.5 })
  world.limits = [{ kind: 'five_hour', percentUsed: 15, resetsAt }]
  await reply($, world, { ...SECOND, usd: 0.5 })

  const mounted = await pane($, 'terminal')
  const line = lines(await mounted.drawn()).find(text => /^This session /.test(text))
  expect(line).toBe('This session about 0.11% of your weekly · 3 replies')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a session inside this window whose share would pass 100% is not given as a share either', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  const resetsAt = new Date(world.clock.now() + 4 * HOUR).toISOString()
  world.limits = [{ kind: 'five_hour', percentUsed: 10, resetsAt }]
  await started($, world)
  // The limit jumps 55 points for the next $0.50: the $1 session would read 110%.
  world.limits = [{ kind: 'five_hour', percentUsed: 65, resetsAt }]
  await reply($, world, { ...SECOND, usd: 0.5 })
  const mounted = await pane($, 'terminal')
  const line = lines(await mounted.drawn()).find(text => /^This session /.test(text))
  expect(line).toBe('This session about 0.02% of your weekly · 2 replies')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Detailed adds no paragraph under each row; What these mean holds the words', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  const mounted = await pane($, 'terminal')
  expect(lines(await mounted.drawn()).join('\n')).not.toContain(GLOSSARY.conversation.body)
  expect(await mounted.find({ type: 'Button', key: 'explain:conversation' })).toBeUndefined()

  await $.command.run({ command: 'glass', args: 'detail' })
  const shown = lines(await mounted.drawn()).join('\n')
  for (const entry of [GLOSSARY.startup, GLOSSARY.conversation, GLOSSARY.output]) expect(shown).not.toContain(entry.body)
  await mounted.press({ key: 'help' })
  const help = lines(await mounted.drawn()).join('\n')
  for (const entry of [GLOSSARY.startup, GLOSSARY.conversation, GLOSSARY.output]) expect(help).toContain(entry.body)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

/** A reply whose first request calls Bash and whose second shows the result's size in the context. */
const MESSAGE = { role: 'user', text: 'Keep going.', toolUses: [] }

const BASH_THEN_RESULT: { steps: Step[]; usd: number } = {
  steps: [
    { input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0, tools: ['Bash'], inputs: { Bash: { description: 'Run the plugin tests', command: 'T=/c/x npm test' } } },
    { cacheRead: 120000, cacheWrite: 9000, output: 200 },
  ],
  usd: 0.6,
}

test('Detailed: sections with a blank line between, spend as shares that add up to 100, heaviest items with what they did, the limit line, one summaries line and the chart caption', async ($, on) => {
  const world = prepare(on)
  const resetsAt = new Date(world.clock.now() + 1 * HOUR).toISOString()
  world.limits = [{ kind: 'five_hour', percentUsed: 64, resetsAt }]
  on('session.compact', () => ({ messages: [MESSAGE], tokensBefore: 120000, tokensAfter: 30000 }) as never)
  await $.session.start(START)
  await reply($, world, FIRST)
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] } as never)
  await $.session.compact({ trigger: 'auto', messages: [MESSAGE] } as never)
  await world.clock.advance(1000)
  await reply($, world, BASH_THEN_RESULT)
  await reply($, world, SECOND)
  await $.command.run({ command: 'glass', args: 'detail' })
  const mounted = await pane($, 'terminal')
  const tree = await mounted.drawn()
  const shown = lines(tree)

  // Sections are Boxes under a column that gaps them.
  const column = elements(tree).find(one => one.type === 'Box' && one.props?.gap === 1 && (one.children ?? []).some(child => (child as { props?: { key?: unknown } }).props?.key === 'answer'))
  expect(column).toBeDefined()
  const sections = (column?.children ?? []).filter(one => (one as { type?: string }).type === 'Box').map(one => String((one as { props?: { key?: unknown } }).props?.key))
  expect(sections).toEqual(expect.arrayContaining(['answer', 'bar', 'filling', 'session', 'spend', 'heaviest', 'limit', 'compactions']))

  // Spend: whole percents, largest first, adding up to 100.
  const from = shown.indexOf("Where this session's spend goes")
  expect(from).toBeGreaterThanOrEqual(0)
  const pcts = [2, 4, 6, 8].map(offset => Number((shown[from + offset] ?? '').replace('%', '')))
  expect(shown.slice(from + 1, from + 9).filter((_, index) => index % 2 === 0).sort()).toEqual(["Claude's output", 'New input', 'Re-reading the context', 'Writing the context to the cache'])
  expect(pcts.every(Number.isFinite)).toBe(true)
  expect(pcts).toEqual([...pcts].sort((a, b) => b - a))
  // The shares add up to 100, and no Total line prices the session in another unit than the session line does.
  expect(pcts.reduce((sum, one) => sum + one, 0)).toBe(100)
  expect(shown.filter(text => /^Total/.test(text))).toEqual([])
  expect(shown.some(text => /list-price/.test(text))).toBe(false)

  // The heaviest item says what the call did.
  expect(shown).toContain('Bash · Run the plugin tests')

  // The limit line explains the bar's mark: used, how much of the window has gone, and when it resets.
  expect(shown.some(text => /^64% used, 80% of the 5 hours gone · resets at \d{1,2}:\d\d (am|pm)$/.test(text))).toBe(true)

  // Two summaries are one line, not a line each.
  expect(shown).toContain('Claude summarised 2 times this session. The Compaction page lists each one.')
  expect(shown.filter(text => /^Claude summarised the conversation|became/.test(text))).toEqual([])

  // The reply chart has one dim caption.
  const caption = elements(tree).find(one => one.type === 'Text' && textOf(one) === 'Bars: cost of each reply. Line: context after it.')
  expect(caption?.props.dimColor).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the spend shares add up to 100 when each would round the wrong way (three equal parts read 33 + 33 + 33)', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  // Re-reading, writing and new input cost $0.30 each; Claude's output costs nothing.
  await reply($, world, { steps: [{ input: 100000, cacheRead: 1000000, cacheWrite: 80000, output: 0 }], usd: 0.9 })
  await $.command.run({ command: 'glass', args: 'detail' })
  const mounted = await pane($, 'terminal')
  const shown = lines(await mounted.drawn())
  const from = shown.indexOf("Where this session's spend goes")
  expect(from).toBeGreaterThanOrEqual(0)
  const pcts = [2, 4, 6, 8].map(offset => Number((shown[from + offset] ?? '').replace('%', '')))
  expect([...pcts].sort()).toEqual([0, 33, 33, 34])
  expect(pcts.reduce((sum, one) => sum + one, 0)).toBe(100)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('one summary is said in the singular, and none says none', async ($, on) => {
  const world = prepare(on)
  on('session.compact', () => ({ messages: [MESSAGE], tokensBefore: 120000, tokensAfter: 30000 }) as never)
  await started($, world)
  await $.command.run({ command: 'glass', args: 'detail' })
  const mounted = await pane($, 'terminal')
  expect(lines(await mounted.drawn())).toContain('None yet in this session.')
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] } as never)
  await world.clock.advance(1000)
  expect(lines(await mounted.drawn())).toContain('Claude summarised once this session. The Compaction page lists it.')
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

test('Simple has no Settings button; Detailed has one quiet button that opens all three options under the page, each with its dim detail at the same left edge', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  const mounted = await pane($, 'terminal')
  expect(await mounted.find({ type: 'Button', key: 'settings' })).toBeUndefined()

  await $.command.run({ command: 'glass', args: 'detail' })
  const settings = await mounted.find({ type: 'Button', key: 'settings' })
  expect(settings?.props.label).toBe('Settings')
  expect(settings?.props.dimColor).toBe(true)
  expect(settings?.props.hotkey).toBeUndefined()
  expect(lines(await mounted.drawn()).join('\n')).not.toMatch(/Ask before a costly send|Hold auto-compact/)

  await mounted.press({ key: 'settings' })
  const tree = await mounted.drawn()
  const options = elements(tree).filter(one => one.type === 'Button' && String(one.props?.key).startsWith('opt:'))
  expect(options.map(one => String(one.props?.label))).toEqual(['Ask before a costly send: Off', 'Hold auto-compact for review: Off'])
  expect(options.every(one => one.props?.dimColor === true && one.props?.hotkey === undefined)).toBe(true)
  // Each option's detail is a dim Text in the same Box, directly under it, with no padding.
  for (const option of options) {
    const box = elements(tree).find(one => one.type === 'Box' && (one.children ?? []).includes(option))
    expect(box?.props?.paddingLeft).toBeUndefined()
    const detail = (box?.children ?? [])[1] as { type: string; props?: Record<string, unknown> } | undefined
    expect(detail?.type).toBe('Text')
    expect(detail?.props?.dimColor).toBe(true)
    expect(textOf(detail)).not.toBe('')
  }
  expect((await mounted.find({ type: 'Button', key: 'settings' }))?.props.label).toBe('Hide settings')

  // The view stays when another page is shown, and an option turns on from any page.
  await mounted.press({ key: 'page:agents' })
  expect(lines(await mounted.drawn()).join('\n')).toMatch(/Hold auto-compact for review: Off/)
  await mounted.press({ key: 'opt:hold' })
  expect(lines(await mounted.drawn()).join('\n')).toMatch(/Hold auto-compact for review: On/)
  await mounted.press({ key: 'settings' })
  expect(lines(await mounted.drawn()).join('\n')).not.toMatch(/Hold auto-compact/)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the Settings view does not come back unasked: leaving Detailed and opening What these mean both put it away', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  await $.command.run({ command: 'glass', args: 'detail' })
  const mounted = await pane($, 'terminal')
  const open = async () => lines(await mounted.drawn()).some(text => /Ask before a costly send/.test(text))

  // Detailed to Simple (More/Less detail) and back.
  await mounted.press({ key: 'settings' })
  expect(await open()).toBe(true)
  await mounted.press({ key: 'more' })
  expect(await open()).toBe(false)
  await mounted.press({ key: 'more' })
  expect(await open()).toBe(false)

  // The same by /glass detail.
  await mounted.press({ key: 'settings' })
  expect(await open()).toBe(true)
  await $.command.run({ command: 'glass', args: 'detail' })
  await $.command.run({ command: 'glass', args: 'detail' })
  expect(await open()).toBe(false)

  // What these mean opens over it; closing the meanings does not bring it back.
  await mounted.press({ key: 'settings' })
  expect(await open()).toBe(true)
  await mounted.press({ key: 'help' })
  expect(await open()).toBe(false)
  await mounted.press({ key: 'help' })
  expect(await open()).toBe(false)
  expect((await mounted.find({ type: 'Button', key: 'settings' }))?.props.label).toBe('Settings')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the footer link follows the page: the session, the subagent runs, the compactions and the CLAUDE.md files', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  const mounted = await pane($, 'desktop')
  const hrefs: Record<string, string> = {}
  for (const id of ['now', 'agents', 'compaction', 'instructions']) {
    if (id !== 'now') await mounted.press({ key: `page:${id}` })
    else if (await mounted.find({ type: 'Button', key: 'page:now' })) await mounted.press({ key: 'page:now' })
    hrefs[id] = String((await mounted.find({ type: 'Link' }))?.props.href)
  }
  expect(hrefs.now).toBe('http://localhost:8792/#/spend/sessions?session=session-one')
  expect(hrefs.agents).toBe('http://localhost:8792/#/agents/subagents?t=habits.habits_agent_runs')
  expect(hrefs.compaction).toBe('http://localhost:8792/#/spend/usage?t=compactions.compactions_summary')
  expect(hrefs.instructions).toBe('http://localhost:8792/#/agents/context')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})
