import { expect, test } from 'claude-code/testing'

import { GLOSSARY } from '../hooks/generated/copy.js'
import { elements, faults, prepare, reply, SITE, START, textOf } from './support'
import type { World } from './support'

const PROPS = { title: 'Glass', isFocused: true, bodyColumns: 110, placement: 'dock', ...SITE } as const

// One reply that leaves the context at 120k of a 160k summarising point, $0.50 spent.
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }

const MESSAGES = [
  { role: 'user', text: "Never use port 8765 for the dev server, it is reserved. Don't read any code yet; wait for the other session to finish. Please add rate limiting to the API.", toolUses: [] },
  { role: 'user', text: "Let's go with option B, the queue-based design. Use pnpm, not npm.", toolUses: [] },
]

type Dollar = Parameters<typeof reply>[0]

async function pane($: Dollar, requestId: 'glass' | 'glass-startup' = 'glass', surface: 'terminal' | 'desktop' = 'terminal') {
  return $.ui.mount({ plugin: 'claudeglass', surface, component: 'Pane', requestId, props: PROPS as never })
}

async function started($: Dollar, world: World): Promise<void> {
  world.transcript = [...MESSAGES]
  await $.session.start(START)
  await reply($, world, FIRST)
}

/** The lines the pane's text shows, one per drawn Text, Button or Link. */
function lines(tree: unknown): string[] {
  return elements(tree)
    .filter(one => one.type === 'Text' || one.type === 'Button' || one.type === 'Link')
    .map(textOf)
    .filter(text => text !== '')
}

function buttons(tree: unknown) {
  return elements(tree)
    .filter(one => one.type === 'Button')
    .map(one => ({ key: String(one.props?.key), label: String(one.props?.label), hotkey: one.props?.hotkey as string | undefined, variant: one.props?.variant as string | undefined, dim: one.props?.dimColor === true }))
}

test('/glass opens Checkpoint and /glass startup opens Startup, each its own pane', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  const answer = await $.command.run({ command: 'glass', args: '' })
  expect(answer.text).toBe('Glass opened.')
  expect(world.opened).toHaveLength(1)
  expect(world.opened[0]).toMatchObject({ id: 'glass', title: 'Checkpoint', focus: true, closeOnEscape: true })

  const startup = await $.command.run({ command: 'glass', args: 'startup' })
  expect(startup.text).toBe('Glass opened.')
  expect(world.opened[1]).toMatchObject({ id: 'glass-startup', title: 'Startup', focus: true, closeOnEscape: true })
  expect(faults(world)).toEqual([])
})

test('before the first reply Checkpoint has no figures, an empty keep list that says what fills it, and a Summarise button', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  const mounted = await pane($)
  const tree = await mounted.drawn()
  const shown = lines(tree)
  expect(shown).toContain('Keep through the next summary')
  expect(shown).toContain('Nothing to keep yet. Pins and instructions appear here as you work.')
  expect(shown.some(text => /^Context /.test(text))).toBe(false)
  // No summary yet: no Last summary section, and nothing to pin.
  expect(shown).not.toContain('Last summary')
  expect(buttons(tree).some(one => one.key === 'pin-last')).toBe(false)
  const summarise = buttons(tree).find(one => one.key === 'summarise')
  expect(summarise).toMatchObject({ label: 'Summarise now', hotkey: 's', variant: 'primary', dim: false })
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Checkpoint is one view: the header, the keep list, then the footer; no page bar, no Settings, More or Copy link', async ($, on) => {
  const world = prepare(on)
  await started($, world)

  for (const surface of ['terminal', 'desktop'] as const) {
    const mounted = await pane($, 'glass', surface)
    const tree = await mounted.drawn()
    const shown = lines(tree)

    expect(shown[0]).toMatch(/^Context 60% full/)
    expect(shown.indexOf('Keep through the next summary')).toBeGreaterThan(0)
    expect(shown).toContain('4 items')
    expect(shown.some(text => /^\[x\] Never use port 8765/.test(text))).toBe(true)
    // Each row has a quiet kind label.
    expect(shown).toContain('instruction')
    expect(shown).toContain('decision')

    // The footer: the hold switch, Open Startup, the dashboard link and the meanings, in that order and after the keep list.
    const footer = shown.slice(shown.indexOf('Hold auto-summaries for review: off'))
    expect(footer).toEqual(['Hold auto-summaries for review: off', 'Open Startup', 'Open in dashboard', 'What these mean'])

    // Nothing of the old pane is left.
    const keys = buttons(tree).map(one => one.key)
    for (const gone of ['more', 'link', 'settings', 'page:now', 'page:agents', 'page:compaction', 'page:instructions']) expect(keys).not.toContain(gone)
    expect(shown.some(text => /More detail|Less detail|Settings|Copy link/.test(text))).toBe(false)
    expect(shown.some(text => /^(Now|Agents|Compaction|Startup)$/.test(text))).toBe(false)

    // One hotkey, on the one main action, which is the one primary Button; the footer's controls are quiet.
    const keyed = buttons(tree).filter(one => one.hotkey !== undefined)
    expect(keyed.map(one => one.key)).toEqual(['summarise'])
    expect(buttons(tree).filter(one => one.variant === 'primary').map(one => one.key)).toEqual(['summarise'])
    for (const key of ['hold', 'startup', 'help']) expect(buttons(tree).find(one => one.key === key)?.dim).toBe(true)
    for (const text of shown) expect(text.length).toBeLessThanOrEqual(110)
    await mounted.unmount()
  }
  expect(faults(world)).toEqual([])
})

test('the hold switch is a quiet Button: off says nothing more, on says what it does under it, and the choice stays', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  const mounted = await pane($)
  const detail = 'When Claude is about to summarise on its own with room to spare, wait once so you can review.'
  expect(lines(await mounted.drawn())).not.toContain(detail)

  await mounted.press({ key: 'hold' })
  let shown = lines(await mounted.drawn())
  expect(shown).toContain('Hold auto-summaries for review: on')
  // The detail is the line directly under the switch.
  expect(shown[shown.indexOf('Hold auto-summaries for review: on') + 1]).toBe(detail)

  // A new draw of the pane keeps it.
  await mounted.unmount()
  const again = await pane($)
  expect(lines(await again.drawn())).toContain('Hold auto-summaries for review: on')
  await again.press({ key: 'hold' })
  shown = lines(await again.drawn())
  expect(shown).toContain('Hold auto-summaries for review: off')
  expect(shown).not.toContain(detail)
  await again.unmount()
  expect(faults(world)).toEqual([])
})

test('Open Startup opens the second pane; Startup has its own page with the dashboard link and the meanings only', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  const mounted = await pane($)
  await mounted.press({ key: 'startup' })
  expect(world.opened.at(-1)).toMatchObject({ id: 'glass-startup', title: 'Startup', focus: true })
  await mounted.unmount()

  const startup = await pane($, 'glass-startup')
  const tree = await startup.drawn()
  // Nothing has loaded: the page says so, and the footer is the link and the meanings.
  expect(lines(tree)).toEqual(['Nothing has loaded yet. What Claude reads for this session shows here once it starts.', 'Open in dashboard', 'What these mean'])
  expect(buttons(tree).map(one => one.key)).toEqual(['help'])
  expect(buttons(tree).some(one => one.hotkey !== undefined)).toBe(false)
  await startup.unmount()
  expect(faults(world)).toEqual([])
})

test('What these mean shows the glossary in place of the page, in the pane it was pressed in only', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  const mounted = await pane($)
  const other = await pane($, 'glass-startup')
  expect(lines(await mounted.drawn()).join('\n')).not.toContain(GLOSSARY.conversation.body)

  await mounted.press({ key: 'help' })
  const help = lines(await mounted.drawn())
  for (const entry of [GLOSSARY.startup, GLOSSARY.conversation, GLOSSARY.output]) {
    expect(help).toContain(entry.title)
    expect(help).toContain(entry.body)
  }
  // The page is put away while the meanings show; the footer stays, and its button says how to leave.
  expect(help).not.toContain('Keep through the next summary')
  expect(help).toContain('Hide the meanings')
  // The other pane is as it was.
  expect(lines(await other.drawn()).join('\n')).not.toContain(GLOSSARY.conversation.body)

  await mounted.press({ key: 'help' })
  expect(lines(await mounted.drawn())).toContain('Keep through the next summary')
  await mounted.unmount()
  await other.unmount()
  expect(faults(world)).toEqual([])
})

test('the footer link follows the pane: Checkpoint goes to the summaries, Startup to the CLAUDE.md files', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  const checkpoint = await pane($, 'glass', 'desktop')
  const startup = await pane($, 'glass-startup', 'desktop')
  expect(String((await checkpoint.find({ type: 'Link' }))?.props.href)).toBe('http://localhost:8792/#/spend/usage?t=compactions.compactions_summary')
  expect(String((await startup.find({ type: 'Link' }))?.props.href)).toBe('http://localhost:8792/#/agents/context')
  await checkpoint.unmount()
  await startup.unmount()
  expect(faults(world)).toEqual([])
})

test('/glass detail still turns the Detailed view on and off, and the choice is kept', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  expect((await $.command.run({ command: 'glass', args: 'detail' })).text).toBe('Detailed view is on.')
  expect((await $.command.run({ command: 'glass', args: 'detail' })).text).toBe('Detailed view is off.')
  expect(faults(world)).toEqual([])
})
