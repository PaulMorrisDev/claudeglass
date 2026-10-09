import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { elements, faults, prepare, reply, SESSION, SITE, START, textOf, usageFor } from './support'
import type { World } from './support'

const PANE = { title: 'Glass', isFocused: true, bodyColumns: 110, placement: 'dock', ...SITE } as const
const BAND = { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 200, ...SITE }
const SENSOR = `/cfg/claudeglass/mod/sensor/${SESSION}.json`

// One reply that leaves the context at 120k of a 160k summarising point, $0.50 spent.
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
const SECOND = { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 }

/** A conversation with two constraints the summary below drops. */
const MESSAGES = [
  { role: 'user', text: "Never use port 8765 for the dev server, it is reserved. Don't read any code yet; wait for the other session to finish. Please add rate limiting to the API.", toolUses: [] },
  { role: 'assistant', text: 'Working on it.', toolUses: [{ tool_use_id: 't1', tool: 'TodoWrite', input: { todos: [{ content: 'Add the burst test', status: 'pending' }] }, text: 'ok' }] },
  { role: 'user', text: "Let's go with option B, the queue-based design. Use pnpm, not npm.", toolUses: [] },
  { role: 'assistant', text: 'Editing.', toolUses: [{ tool_use_id: 't2', tool: 'Edit', input: { file_path: 'src/api/limiter.ts' }, text: 'ok' }] },
]

const SUMMARY = [
  'This session is being continued from a previous conversation.',
  'The user asked for rate limiting on the API and chose option B, the queue-based design.',
  'The team uses pnpm, not npm. Files changed: src/api/limiter.ts. Open work: add the burst test.',
].join('\n')

const SUMMARY_MESSAGE = { role: 'user', text: SUMMARY, toolUses: [] }

type Dollar = Engine

type Bottoms = { filled: string[]; submitted: string[] }

/** What sits under the review: the transcript, the summary a compaction writes, and a prompt box that records its drafts. */
function bottoms(on: On, world: World): Bottoms {
  const seen: Bottoms = { filled: [], submitted: [] }
  world.transcript = [...MESSAGES]
  on('session.compact', () => ({ messages: [SUMMARY_MESSAGE], tokensBefore: 150000, tokensAfter: 30000 }) as never)
  on('prompt.fill', (_$, e) => {
    seen.filled.push(e.text)
    return { isFilled: true }
  })
  on('prompt.submit', (_$, e) => {
    seen.submitted.push(String(e.text))
    return e as never
  })
  on('classic.SessionStart', () => ({}) as never)
  return seen
}

async function started($: Dollar, world: World): Promise<void> {
  await $.session.start(START)
  await reply($, world, FIRST)
}

async function band($: Dollar, surface: 'terminal' | 'desktop' = 'terminal') {
  return $.ui.mount({ plugin: 'claudeglass', surface, component: 'AbovePrompt', props: BAND as never })
}

async function pane($: Dollar, surface: 'terminal' | 'desktop' = 'terminal') {
  return $.ui.mount({ plugin: 'claudeglass', surface, component: 'Pane', requestId: 'glass', props: PANE as never })
}

/** The lines the pane's text shows, one per drawn Text or Button. */
function lines(tree: unknown): string[] {
  return elements(tree)
    .filter(one => one.type === 'Text' || one.type === 'Button')
    .map(textOf)
    .filter(text => text !== '')
}

/** The lines of the page itself, from its lead sentence on (the page bar's words come before it). */
function fromLead(tree: unknown): string[] {
  const shown = lines(tree)
  const at = shown.findIndex(line => line.startsWith('Claude summarised'))
  return at < 0 ? shown : shown.slice(at)
}

/** Whether the page bar draws the page as the current one: bold Text with its name, not a Button. */
function showsPage(tree: unknown, name: string): boolean {
  return elements(tree).some(one => one.type === 'Text' && one.props?.bold === true && textOf(one) === name)
}

/** Turns an opt-in on the way a person does: the Detailed view of the Compaction page. */
async function turnOn($: Dollar, key: 'hold' | 'readd'): Promise<void> {
  await $.command.run({ command: 'glass', args: 'detail' } as never)
  const mounted = await pane($)
  // The page showing is drawn as bold text; any other page has its tab on the bar, and that is what gets pressed.
  if (!showsPage(await mounted.drawn(), 'Compaction')) {
    expect(await mounted.find({ type: 'Button', key: 'page:compaction' })).toBeDefined()
    await mounted.press({ key: 'page:compaction' } as never)
  }
  expect(showsPage(await mounted.drawn(), 'Compaction')).toBe(true)
  await mounted.press({ key: `opt:${key}` } as never)
  expect(lines(await mounted.drawn()).join('\n')).toMatch(key === 'hold' ? /Hold auto-compact for review: On/ : /Re-add pins after compaction: On/)
  await mounted.unmount()
}

function boundary(world: World): string[] {
  return world.logs.filter(line => line.startsWith('● Glass · Claude summarised'))
}

type Event = { event: string; kind?: string; kept?: number; missing?: number; added?: number }

function sensor(world: World): Event[] {
  return (JSON.parse(String(world.files.get(SENSOR))) as { events: Event[] }).events
}

/** Presses the band's review button, which opens the Compaction page, then draws the pane. */
async function reviewPane($: Dollar, world: World, surface: 'terminal' | 'desktop' = 'terminal') {
  const mounted = await band($, surface)
  const button = await mounted.find({ type: 'Button', key: 'review' })
  expect(button?.props.hotkey).toBe('r')
  await mounted.press({ key: 'review' } as never)
  await mounted.unmount()
  expect(world.opened.at(-1)).toMatchObject({ id: 'glass' })
  return pane($, surface)
}

test('a summary of the main session: a boundary line, a badge, the counts in state, and a sensor event with counts only', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)

  // Before a summary there is no badge (the offer to review beforehand is tested below).
  let mounted = await band($)
  expect(await mounted.find({ type: 'Button', key: 'review' })).toBeUndefined()
  await mounted.unmount()

  await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)
  const done = boundary(world)
  expect(done).toEqual(['● Glass · Claude summarised the conversation. 2 things you said may be missing. Press Review above the prompt to check.'])

  mounted = await band($)
  const texts = elements(await mounted.drawn()).filter(one => one.type === 'Text').map(textOf)
  expect(texts).toContain('2 things may be missing from the summary')
  const review = await mounted.find({ type: 'Button', key: 'review' })
  expect(review?.props.label).toBe('Review')
  await mounted.unmount()

  // The counts reach the sensor with the next reply: kept, missing, added; no words.
  await reply($, world, SECOND)
  const found = sensor(world).filter(one => one.event === 'compaction')
  expect(found).toHaveLength(1)
  expect(found[0]).toMatchObject({ kind: 'auto', missing: 2, added: 0 })
  expect(Object.keys(found[0] as object).sort()).toEqual(['added', 'event', 'kept', 'kind', 'missing', 'ts'])
  expect(faults(world)).toEqual([])
})

test('a subagent summary is left alone', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'auto', agentId: 'agent-7', messages: MESSAGES } as never)
  expect(boundary(world)).toEqual([])
  const mounted = await band($)
  expect(await mounted.find({ type: 'Button', key: 'review' })).toBeUndefined()
  await mounted.unmount()
  await reply($, world, SECOND)
  expect(sensor(world).filter(one => one.event === 'compaction')).toEqual([])
  expect(faults(world)).toEqual([])
})

/** The Buttons of a drawn page, as the hotkeys and variants they carry. */
function buttons(tree: unknown) {
  return elements(tree)
    .filter(one => one.type === 'Button')
    .map(one => ({ key: String(one.props?.key), label: String(one.props?.label), hotkey: one.props?.hotkey as string | undefined, variant: one.props?.variant as string | undefined }))
}

test('the review page opens on what may be missing and one note that puts it back; nothing is sent', async ($, on) => {
  const world = prepare(on)
  const seen = bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)

  const mounted = await reviewPane($, world)
  const shown = fromLead(await mounted.drawn())
  const text = shown.join('\n')
  // One bold sentence first, then what may be missing as plain rows with no kind label.
  expect(shown[0]).toMatch(/^Claude summarised the conversation( at \d{1,2}:\d\d [ap]m)?\.$/)
  expect(shown[1]).toBe('2 things you asked for may be missing:')
  expect(shown).toContainEqual(expect.stringMatching(/^• Never use port 8765/))
  expect(shown).toContainEqual(expect.stringMatching(/^• Don't read any code yet/))
  // Only what may be missing: the kept items are not listed on the first screen.
  expect(text).not.toMatch(/pnpm/)
  expect(text).not.toMatch(/option B/)
  expect(text).not.toMatch(/What you asked/)

  // Rows are text. The only hotkeys are the page bar's and the one main action, which is the one primary Button.
  const all = buttons(await mounted.drawn())
  const primary = all.filter(one => one.variant === 'primary')
  expect(primary).toEqual([{ key: 'add-back', label: 'Add these back', hotkey: 'b', variant: 'primary' }])
  expect(all.filter(one => one.hotkey !== undefined && one.key !== 'add-back').every(one => one.key.startsWith('page:'))).toBe(true)
  expect(all.filter(one => one.key.startsWith('add:') || one.key === 'add-all')).toEqual([])
  const desktop = await pane($, 'desktop')
  expect(fromLead(await desktop.drawn())).toEqual(shown)
  await desktop.unmount()

  // One note for everything missing, put in the prompt box, never sent.
  await mounted.press({ key: 'add-back' } as never)
  expect(seen.filled).toHaveLength(1)
  expect(seen.filled[0]).toMatch(/^Before compaction we agreed: .*8765.*; .*read any code.*\.$/)
  expect(lines(await mounted.drawn()).join('\n')).toMatch(/A draft is in the prompt box/)
  expect(seen.submitted).toEqual([])
  await mounted.unmount()

  // The draft counts as added back when the next reply's counts reach the sensor.
  await reply($, world, SECOND)
  expect(sensor(world).filter(one => one.event === 'compaction')[0]).toMatchObject({ kind: 'manual', missing: 2, added: 2 })
  expect(faults(world)).toEqual([])
})

test('the second line says everything is in the summary, or that nothing needed keeping; one missing item reads in the singular; more than four are counted', async ($, on) => {
  const world = prepare(on)
  const seen: Bottoms = { filled: [], submitted: [] }
  const written = { summary: '' }
  world.transcript = [...MESSAGES]
  // The summary each compaction writes is set before it.
  on('session.compact', () => ({ messages: [{ role: 'user', text: written.summary, toolUses: [] }], tokensBefore: 150000, tokensAfter: 30000 }) as never)
  on('prompt.fill', (_$, e) => {
    seen.filled.push(e.text)
    return { isFilled: true }
  })
  on('classic.SessionStart', () => ({}) as never)
  await started($, world)

  // The summary carries both constraints, so nothing is missing.
  written.summary = `This session is being continued. The team uses pnpm, not npm, and chose option B, the queue-based design. Never use port 8765. Do not read any code yet, wait for the other session to finish.`
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  let mounted = await pane($)
  await mounted.press({ key: 'page:compaction' } as never)
  let shown = fromLead(await mounted.drawn())
  expect(shown[1]).toBe('Everything you asked Claude to keep is in the summary.')
  expect(await mounted.find({ type: 'Button', key: 'add-back' })).toBeUndefined()
  await mounted.unmount()

  // Nothing the user said is worth keeping.
  written.summary = 'This session is being continued. Work went on.'
  await $.session.compact({ trigger: 'manual', messages: [{ role: 'user', text: 'Thanks, that looks right.', toolUses: [] }] } as never)
  mounted = await pane($)
  shown = fromLead(await mounted.drawn())
  expect(shown[1]).toBe('Nothing you said needed keeping.')
  expect(await mounted.find({ type: 'Button', key: 'add-back' })).toBeUndefined()
  await mounted.unmount()

  // One missing item: singular line, "Add it back".
  await $.session.compact({ trigger: 'manual', messages: [{ role: 'user', text: 'Never use port 8765 for the dev server.', toolUses: [] }] } as never)
  mounted = await pane($)
  shown = fromLead(await mounted.drawn())
  expect(shown[1]).toBe('1 thing you asked for may be missing:')
  expect(buttons(await mounted.drawn()).find(one => one.key === 'add-back')?.label).toBe('Add it back')
  await mounted.unmount()

  // Six missing: four rows, then a count.
  const six = Array.from({ length: 6 }, (_, n) => ({ role: 'user', text: `Never touch module${n}x.`, toolUses: [] }))
  await $.session.compact({ trigger: 'manual', messages: six } as never)
  mounted = await pane($)
  shown = fromLead(await mounted.drawn())
  expect(shown[1]).toBe('6 things you asked for may be missing:')
  expect(shown.filter(line => line.startsWith('• '))).toHaveLength(4)
  expect(shown).toContain('and 2 more')
  await mounted.press({ key: 'add-back' } as never)
  expect(seen.filled.at(-1)).toMatch(/module0x.*module5x|module5x.*module0x/)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the summary, the full list with marks, the earlier messages and the history sit behind a press', async ($, on) => {
  const world = prepare(on)
  const seen = bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)

  await mounted.press({ key: 'summary' } as never)
  const md = await mounted.find({ type: 'Markdown' })
  expect(String(md?.props.text)).toMatch(/option B/)

  await mounted.press({ key: 'list' } as never)
  const text = lines(await mounted.drawn()).join('\n')
  expect(text).toMatch(/Quick check \(est\.\)/)
  // The mark's word comes first on one line, so it can never wrap into a column of its own.
  expect(text).toMatch(/^✗ not in the summary · Never use port 8765/m)
  expect(text).toMatch(/^✓ in the summary · Use pnpm, not npm/m)
  // Files Claude edited are not listed one by one; they are one dim line.
  expect(text).not.toMatch(/limiter\.ts/)
  expect(text).toMatch(/Recent files, the plan and CLAUDE\.md are kept anyway \(1\)\./)
  // Rows are text: nothing to press here, no pin buttons, and the quiet toggle now offers to hide the list.
  const open = buttons(await mounted.drawn())
  expect(open.filter(one => one.key.startsWith('pin:') || one.key.startsWith('item:'))).toEqual([])
  expect(open.find(one => one.key === 'list')?.label).toBe('Hide the list')

  await mounted.press({ key: 'earlier' } as never)
  const earlier = buttons(await mounted.drawn()).filter(one => one.key.startsWith('earlier:'))
  expect(earlier.length).toBe(2)
  expect(earlier.every(one => one.label === 'Use this' && one.hotkey === undefined)).toBe(true)
  expect(lines(await mounted.drawn())).toContainEqual(expect.stringMatching(/^Let's go with option B/))
  await mounted.press({ key: String(earlier[0]?.key) } as never)
  expect(seen.filled.at(-1)).toMatch(/^Let's go with option B/)

  // Simple: no history, options or dashboard.
  expect(lines(await mounted.drawn()).join('\n')).not.toMatch(/Hold auto-compact/)
  await mounted.unmount()

  // Detailed adds the history, the dashboard link with a copy button, and the options.
  await $.command.run({ command: 'glass', args: 'detail' } as never)
  const detailed = await pane($)
  const more = lines(await detailed.drawn()).join('\n')
  expect(more).toMatch(/you ran \/compact|on its own/)
  expect(more).toMatch(/150k to 30k/)
  expect(more).toMatch(/Hold auto-compact for review: Off/)
  expect(more).toMatch(/Re-add pins after compaction: Off/)
  expect(more).toMatch(/Choose what to keep next time/)
  const link = await detailed.find({ type: 'Link' })
  expect(String(link?.props.href)).toBe('http://localhost:8792/#/spend/usage?t=compactions.compactions_summary')
  await detailed.press({ key: 'dashboard-copy' } as never)
  expect(world.copied.at(-1)).toBe('http://127.0.0.1:8792/#/spend/usage?t=compactions.compactions_summary')
  // Nothing on this page but the page bar and the one main action has a hotkey, and no two are alike.
  const drawn = buttons(await detailed.drawn())
  const keys = drawn.filter(one => one.hotkey !== undefined)
  expect(keys.filter(one => !one.key.startsWith('page:')).map(one => one.key)).toEqual(['add-back'])
  expect(new Set(keys.map(one => one.hotkey)).size).toBe(keys.length)
  expect(drawn.filter(one => one.variant === 'primary')).toHaveLength(1)
  await detailed.unmount()
  expect(faults(world)).toEqual([])
})

test('the Detailed view offers the Haiku check as a quiet button with its size line', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)
  await $.command.run({ command: 'glass', args: 'detail' } as never)
  const mounted = await reviewPane($, world)
  const check = buttons(await mounted.drawn()).find(one => one.key === 'haiku-check')
  expect(check?.label).toBe('Check with Haiku')
  expect(check?.hotkey).toBeUndefined()
  expect(check?.variant).toBeUndefined()
  expect(lines(await mounted.drawn()).join('\n')).toMatch(/Sends the summary and your list to Haiku, about \d+ tokens\./)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('files in a scratch place are not kept; a project inside one is, because the session folder is known', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  const edit = (path: string) => ({ role: 'assistant', text: '', toolUses: [{ tool_use_id: path, tool: 'Edit', input: { file_path: path }, text: 'ok' }] })
  await $.session.start({ ...START, cwd: '/tmp/proj' })
  await reply($, world, FIRST)
  const messages = [...MESSAGES, edit('/tmp/proj/src/a.ts'), edit('/tmp/other/b.ts'), edit('/home/me/.claude/plans/plan.md')]
  await $.session.compact({ trigger: 'manual', messages } as never)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'list' } as never)
  // Of the three new edits and the one in MESSAGES, the project's file and src/api/limiter.ts are recent and kept anyway.
  expect(lines(await mounted.drawn()).join('\n')).toMatch(/kept anyway \(2\)\./)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('before a summary: ticks, then a /compact Keep command in the prompt box, at most 1,500 characters', async ($, on) => {
  const world = prepare(on)
  const seen = bottoms(on, world)
  await started($, world)
  await reply($, world, { gap: 10000, steps: [{ cacheRead: 120000, cacheWrite: 31000, output: 200 }], usd: 0.2 })
  // 150k of a 160k point: soon. The band offers the review.
  let mounted = await band($)
  const offer = await mounted.find({ type: 'Button', key: 'review-before' })
  expect(offer?.props.label).toBe('Review before compacting')
  expect(offer?.props.hotkey).toBe('r')
  await mounted.press({ key: 'review-before' } as never)
  await mounted.unmount()

  mounted = await pane($)
  const text = lines(await mounted.drawn()).join('\n')
  expect(text).toMatch(/Claude will summarise this conversation soon\. Tick what it must keep\./)
  expect(text).toMatch(/\[x\] Never use port 8765/)
  expect(text).toMatch(/\[x\] Let's go with option B/)
  // The ticks are real controls but carry no hotkey and no kind label; the one main action has `c`.
  const drawn = buttons(await mounted.drawn())
  const ticks = drawn.filter(one => one.key.startsWith('tick:'))
  expect(ticks.length).toBeGreaterThanOrEqual(4)
  expect(ticks.length).toBeLessThanOrEqual(5)
  expect(ticks.every(one => one.hotkey === undefined)).toBe(true)
  expect(text).not.toMatch(/What you asked|Decisions/)
  expect(drawn.find(one => one.key === 'use')).toEqual({ key: 'use', label: 'Fill in /compact', hotkey: 'c', variant: 'primary' })
  expect(drawn.filter(one => one.variant === 'primary')).toHaveLength(1)
  await mounted.press({ key: 'use' } as never)
  const command = seen.filled.at(-1) ?? ''
  expect(command.startsWith('/compact Keep: ')).toBe(true)
  expect(command).toMatch(/Never use port 8765/)
  expect(command.length).toBeLessThanOrEqual(1500)

  // Unticking an item takes it out of the command.
  const tick = elements(await mounted.drawn()).find(one => one.type === 'Button' && String(one.props?.label).includes('Never use port 8765'))
  await mounted.press({ key: String(tick?.props?.key) } as never)
  await mounted.press({ key: 'use' } as never)
  expect(seen.filled.at(-1)).not.toMatch(/8765/)
  expect(seen.submitted).toEqual([])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('/glass pin adds a pin that comes first in the list and survives a summary; /clear drops it', async ($, on) => {
  const world = prepare(on)
  const seen = bottoms(on, world)
  await started($, world)
  expect((await $.command.run({ command: 'glass', args: 'pin' } as never)).text).toMatch(/^Say what to pin/)
  expect((await $.command.run({ command: 'glass', args: 'pin rotate the staging key before friday' } as never)).text).toMatch(/^Pinned\./)
  await reply($, world, SECOND)

  let mounted = await pane($)
  await mounted.press({ key: 'page:compaction' } as never)
  const before = lines(await mounted.drawn())
  expect(before.findIndex(text => text.includes('rotate the staging key'))).toBeGreaterThan(-1)
  expect(before.findIndex(text => text.includes('rotate the staging key'))).toBeLessThan(before.findIndex(text => text.includes('Never use port 8765')))
  await mounted.unmount()

  // The summary drops it; the page says so.
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  mounted = await pane($)
  expect(lines(await mounted.drawn()).join('\n')).toMatch(/3 things you asked for may be missing:/)
  await mounted.unmount()

  // Re-adding pins is an opt-in: off, the session start after a summary adds nothing; on, it adds the pin.
  expect(await $.classic.SessionStart({ source: 'compact' } as never)).not.toHaveProperty('additionalContext')
  await turnOn($, 'readd')
  const result = (await $.classic.SessionStart({ source: 'compact' } as never)) as { additionalContext?: string[] }
  expect(result.additionalContext).toEqual(['Before the summary, you pinned: rotate the staging key before friday.'])
  expect(await $.classic.SessionStart({ source: 'startup' } as never)).not.toHaveProperty('additionalContext')

  await $.session.end({ sessionId: SESSION, reason: 'clear' } as never)
  mounted = await pane($)
  expect(lines(await mounted.drawn()).join('\n')).not.toMatch(/staging key/)
  await mounted.unmount()
  expect(seen.submitted).toEqual([])
  expect(faults(world)).toEqual([])
})

test('/glass unpin removes the pins that hold those words, or all of them; the before view has a quiet Unpin on a pinned row', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  const run = async (args: string) => String((await $.command.run({ command: 'glass', args } as never)).text)
  await run('pin rotate the staging key before friday')
  await run('pin never use port 9999')

  expect(await run('unpin')).toMatch(/^Say what to unpin/)
  expect(await run('unpin no such words')).toBe('No pin has those words.')
  // Case does not matter, and a word is enough.
  expect(await run('unpin STAGING Key')).toBe('Unpinned 1 pin.')
  await reply($, world, SECOND)

  // The press on a pinned row's Unpin takes that pin away; it is a quiet Button without a hotkey.
  let mounted = await pane($)
  await mounted.press({ key: 'page:compaction' } as never)
  let page = await mounted.drawn()
  expect(lines(page).join('\n')).not.toMatch(/staging key/)
  const unpins = buttons(page).filter(one => one.label === 'Unpin')
  expect(unpins.length).toBe(1)
  expect(unpins[0]?.hotkey).toBeUndefined()
  expect(elements(page).find(one => one.type === 'Button' && one.props?.label === 'Unpin')?.props?.dimColor).toBe(true)
  expect(lines(page).join('\n')).toMatch(/never use port 9999/)
  await mounted.press({ key: String(unpins[0]?.key) } as never)
  page = await mounted.drawn()
  expect(lines(page).join('\n')).not.toMatch(/port 9999/)
  expect(buttons(page).some(one => one.label === 'Unpin')).toBe(false)
  await mounted.unmount()

  // "all" takes every pin; with none left it says so.
  await run('pin first thing to keep')
  await run('pin second thing to keep')
  expect(await run('unpin all')).toBe('Unpinned 2 pins.')
  expect(await run('unpin all')).toBe('You have no pins.')
  expect(await run('unpin anything')).toBe('You have no pins.')

  // A full list points at /glass unpin.
  for (let i = 0; i < 20; i++) await run(`pin keep item number ${i}`)
  expect(await run('pin one more')).toMatch(/20 pins already\. Remove one first with \/glass unpin/)
  expect(await run('unpin number 7')).toBe('Unpinned 1 pin.')
  expect(await run('pin one more')).toMatch(/^Pinned\./)
  expect(faults(world)).toEqual([])
})

test('the hold: an automatic summary waits for the rest of the reply, the next reply lets one through, never on a 200k window or near the window', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await $.session.start(START)
  await reply($, world, FIRST)

  // Off by default: nothing is held.
  expect((await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)).skip).toBeUndefined()

  // On, but a 200k window has no room to wait.
  await turnOn($, 'hold')
  expect((await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)).skip).toBeUndefined()

  // A 1M window with the point at 300k: the attempts of the reply running are held, the first one after it goes through.
  world.shape = { window: 1000000, point: 300000 }
  const measure = async (tokens: number) => {
    world.figures(tokens, 1)
    await $.session.measure({ context: { tokens, window: 1000000, percent: tokens / 10000 }, rateLimits: [], cost: { usd: 1 }, changed: ['context'] } as never)
    await world.clock.advance(300)
  }
  await measure(280000)
  const held = await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)
  expect(held.skip).toMatch(/held this summary/)
  expect(world.logs.filter(line => line.startsWith('● Glass · Claude summarised')).length).toBe(2)
  // Claude Code asks again at the next step of the same reply: held too, and no second toast.
  expect((await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)).skip).toMatch(/held this summary/)
  expect((await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)).skip).toMatch(/held this summary/)
  // Within 20k of the window there is no room for another request: it goes through, and the hold is over.
  await measure(985000)
  expect((await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)).skip).toBeUndefined()
  // A new hold, ended by the reply: the first summary after it goes through, and the one after that is held again.
  await measure(280000)
  expect((await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)).skip).toMatch(/held this summary/)
  await reply($, world, { steps: [{ cacheRead: 270000, cacheWrite: 5000, output: 5000 }], usd: 1 })
  expect((await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)).skip).toBeUndefined()
  expect((await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)).skip).toMatch(/held this summary/)
  expect((await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)).skip).toBeUndefined()
  expect(faults(world)).toEqual([])
})

test('only counts reach the disk: no prompt, summary or pin text is written', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  await $.command.run({ command: 'glass', args: 'pin rotate the staging key before friday' } as never)
  await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)
  await reply($, world, SECOND)
  await $.session.end({ sessionId: SESSION, reason: 'exit' } as never)

  const written = [...world.files.entries()].filter(([path]) => !path.endsWith('settings.json'))
  expect(written.length).toBeGreaterThan(0)
  for (const [path, text] of written) {
    expect(text, path).not.toMatch(/8765|pnpm|option B|staging key|continued from a previous|rate limiting/)
  }
  expect(sensor(world).some(one => one.event === 'compaction')).toBe(true)
  expect(faults(world)).toEqual([])
})

test('a prompt row can carry [p] pin, and the summary row [v] review', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  const text = "Let's go with option B, the queue-based design. Use pnpm, not npm."
  const props = (body: string) => ({ text: body, origin: { kind: 'composer' }, isExpanded: false, ...SITE }) as never

  // The reply that just ended looked at the prompts: the newest one with an instruction or a decision offers [p] pin.
  let mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'UserMessage', props: props(text) })
  const pin = await mounted.find({ type: 'Button', key: 'pin' })
  expect(pin?.props.hotkey).toBe('p')
  await mounted.press({ key: 'pin' } as never)
  await mounted.unmount()
  mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'UserMessage', props: props(text) })
  expect(await mounted.find({ type: 'Button', key: 'pin' })).toBeUndefined()
  await mounted.unmount()

  // Another prompt gets no button.
  mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'UserMessage', props: props('Thanks, that looks right.') })
  expect(await mounted.find({ type: 'Button', key: 'pin' })).toBeUndefined()
  await mounted.unmount()

  await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)
  mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'UserMessage', props: props(SUMMARY) })
  const review = await mounted.find({ type: 'Button', key: 'review' })
  expect(review?.props.hotkey).toBe('v')
  await mounted.press({ key: 'review' } as never)
  expect(world.opened.at(-1)).toMatchObject({ id: 'glass' })
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('straight after a summary the band reads the context as Claude Code reports it then, not as it was', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)

  let mounted = await band($)
  expect(elements(await mounted.drawn()).map(textOf).join(' ')).toMatch(/60% full/)
  await mounted.unmount()

  // The summary leaves 30k in the context; no measurement has come yet.
  world.figures(30000, 0.5)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  await world.clock.advance(300)
  mounted = await band($)
  const text = elements(await mounted.drawn()).map(textOf).join(' ')
  await mounted.unmount()
  expect(text).toMatch(/15% full/)
  expect(text).not.toMatch(/60% full/)
  expect(faults(world)).toEqual([])
})
