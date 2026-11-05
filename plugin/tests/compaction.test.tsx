import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { emptyReview, lostCount, lostItems, readdMissing, sound, withSummary } from '../hooks/compaction-memory'
import type { GlassKeepItem, GlassMark, GlassReview } from '../types'
import { elements, faults, prepare, reply, SESSION, SITE, START, textOf, usageFor } from './support'
import type { World } from './support'

const PANE = { title: 'Glass', isFocused: true, bodyColumns: 110, placement: 'dock', ...SITE } as const
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

type Bottoms = { filled: string[]; submitted: string[]; compacts: { trigger: string; agentId?: string; instructions?: string; messages: readonly unknown[] }[] }

/** What sits under the review: the transcript, the summary a compaction writes, and a prompt box that records its drafts. */
function bottoms(on: On, world: World): Bottoms {
  const seen: Bottoms = { filled: [], submitted: [], compacts: [] }
  world.transcript = [...MESSAGES]
  on('session.compact', (_$, e) => {
    seen.compacts.push(e)
    return { messages: [SUMMARY_MESSAGE], tokensBefore: 150000, tokensAfter: 30000 } as never
  })
  on('prompt.fill', (_$, e) => {
    seen.filled.push(e.text)
    return { isFilled: true }
  })
  on('prompt.submit', (_$, e) => {
    seen.submitted.push(String(e.text))
    return e as never
  })
  return seen
}

async function started($: Dollar, world: World): Promise<void> {
  await $.session.start(START)
  await reply($, world, FIRST)
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

/** The Buttons of a drawn page, as the hotkeys, variants and looks they carry. */
function buttons(tree: unknown) {
  return elements(tree)
    .filter(one => one.type === 'Button')
    .map(one => ({ key: String(one.props?.key), label: String(one.props?.label), hotkey: one.props?.hotkey as string | undefined, variant: one.props?.variant as string | undefined, dim: one.props?.dimColor === true }))
}

/** Presses the footer's hold switch the way a person does, so the next automatic summary is held for review. */
async function turnOn($: Dollar, key: 'hold'): Promise<void> {
  const mounted = await pane($)
  await mounted.press({ key } as never)
  expect(lines(await mounted.drawn())).toContain('Hold auto-summaries for review: on')
  await mounted.unmount()
}

function boundary(world: World): string[] {
  return world.logs.filter(line => line.startsWith('● Glass · Claude summarised'))
}

type Event = { ts?: number; event: string; kind?: string; kept?: number; missing?: number; added?: number; instructed?: number; referenced?: number; before?: number; after?: number }

function sensor(world: World): Event[] {
  return (JSON.parse(String(world.files.get(SENSOR))) as { events: Event[] }).events
}

/** The Checkpoint pane after a summary: the same pane, mounted fresh. */
async function reviewPane($: Dollar, _world: World, surface: 'terminal' | 'desktop' = 'terminal') {
  return pane($, surface)
}

test('a summary of the main session: a boundary line, the counts in state, and a sensor event with counts only', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  bottoms(on, world)
  await started($, world)

  // Before a summary there is nothing to review.
  expect(watch.latest.after).toBeNull()
  expect(watch.latest.history).toEqual([])

  await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)
  expect(boundary(world)).toHaveLength(1)
  expect(boundary(world)[0]).toMatch(/^● Glass · Claude summarised the conversation\. 2 things you said may be missing\./)
  expect(watch.latest).toMatchObject({ missing: 2, seen: false })
  expect(watch.latest.history).toHaveLength(1)

  // The counts reach the sensor with the next reply: kept, missing, added; no words.
  await reply($, world, SECOND)
  const found = sensor(world).filter(one => one.event === 'compaction')
  expect(found).toHaveLength(1)
  expect(found[0]).toMatchObject({ kind: 'auto', missing: 2, added: 0 })
  expect(Object.keys(found[0] as object).sort()).toEqual(['added', 'event', 'instructed', 'kept', 'kind', 'missing', 'referenced', 'ts'])
  // The keep list was told to the summariser, and no put-back item has been referenced yet.
  expect(found[0]).toMatchObject({ instructed: 1, referenced: 0 })
  expect(faults(world)).toEqual([])
})

test('a subagent summary is left alone', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'auto', agentId: 'agent-7', messages: MESSAGES } as never)
  expect(boundary(world)).toEqual([])
  expect(watch.latest.after).toBeNull()
  expect(watch.latest.history).toEqual([])
  const mounted = await pane($)
  expect(lines(await mounted.drawn())).not.toContain('Last summary')
  await mounted.unmount()
  await reply($, world, SECOND)
  expect(sensor(world).filter(one => one.event === 'compaction')).toEqual([])
  expect(faults(world)).toEqual([])
})

test('Last summary says what the summary kept and lost, one row per lost item, and one press puts them back; nothing is sent', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  const seen = bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)

  const mounted = await reviewPane($, world)
  const tree = await mounted.drawn()
  const shown = lines(tree)
  expect(shown).toContainEqual(expect.stringMatching(/^Last summary · \d{1,2}:\d\d [ap]m · you asked$/))
  // The counts sit at the heading's right edge, and say nothing of re-adding until something is re-added.
  expect(shown).toContain('kept 2 · lost 2')
  // One row per lost item, with the lost mark; the kept ones are not listed here.
  expect(shown).toContainEqual(expect.stringMatching(/^✗ Never use port 8765/))
  expect(shown).toContainEqual(expect.stringMatching(/^✗ Don't read any code yet/))
  expect(shown.filter(text => /^✗ [^ ]/.test(text))).toHaveLength(2)
  expect(shown.join('\n')).not.toMatch(/✗ .*(pnpm|option B)/)
  expect(shown.some(text => /re-added/.test(text))).toBe(false)

  // The Buttons: Re-add is primary with its own key, Check with a model is secondary, and there is no earlier summary to show.
  const all = buttons(tree)
  expect(all.find(one => one.key === 'readd')).toEqual({ key: 'readd', label: 'Re-add 2 items', hotkey: 'r', variant: 'primary', dim: false })
  expect(all.find(one => one.key === 'check')).toEqual({ key: 'check', label: 'Check with a model', hotkey: undefined, variant: 'secondary', dim: false })
  expect(all.some(one => one.key === 'earlier')).toBe(false)
  expect(all.filter(one => one.hotkey !== undefined).map(one => one.key).sort()).toEqual(['readd', 'summarise'])
  const desktop = await pane($, 'desktop')
  expect(lines(await desktop.drawn())).toEqual(shown)
  await desktop.unmount()

  // One press puts everything lost back through the system prompt: nothing goes to the prompt box, nothing is sent.
  await mounted.press({ key: 'readd' } as never)
  expect(watch.latest.readded.map(one => one.text)).toEqual([expect.stringMatching(/8765/), expect.stringMatching(/read any code/)])
  const done = lines(await mounted.drawn())
  expect(done).toContain('Claude is told these again with its next reply.')
  expect(done).toContain('kept 2 · lost 2 · re-added 2')
  // Each re-added row says where its receipt stands, in words.
  expect(done.filter(text => text === 're-added, not yet seen')).toHaveLength(2)
  // Done: the Button gives way to when it was done, so there is nothing left to press twice.
  expect(buttons(await mounted.drawn()).some(one => one.key === 'readd')).toBe(false)
  expect(done.filter(text => /^Re-added \d{1,2}:\d\d [ap]m$/.test(text))).toHaveLength(1)
  const receipts = watch.latest.receipts
  await expect(mounted.press({ key: 'readd' } as never)).rejects.toThrow()
  expect(watch.latest.receipts).toEqual(receipts)
  expect(seen.filled).toEqual([])
  expect(seen.submitted).toEqual([])
  await mounted.unmount()

  // The items put back count as added back when the next reply's counts reach the sensor.
  await reply($, world, SECOND)
  expect(sensor(world).filter(one => one.event === 'compaction')[0]).toMatchObject({ kind: 'manual', missing: 2, added: 2 })
  expect(faults(world)).toEqual([])
})

test('a re-added row names its receipt: referenced once a reply says it, not referenced after ten replies that do not', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'readd' } as never)
  const words = async () => lines(await mounted.drawn()).filter(text => text.startsWith('re-added, '))
  expect(await words()).toEqual(['re-added, not yet seen', 're-added, not yet seen'])

  // The next reply names the port; the other item waits.
  const step = { cacheRead: 120000, cacheWrite: 1000, output: 200 }
  await reply($, world, { usd: 0.05, steps: [{ ...step, answer: 'I will not use port 8765 for the dev server.' }] })
  expect(await words()).toEqual(['re-added, referenced ✓', 're-added, not yet seen'])

  // Nine more replies that do not say the other: the tenth reply settles it as not referenced.
  for (let count = 0; count < 9; count++) await reply($, world, { usd: 0.05, steps: [{ ...step, answer: 'Formatted the file.' }] })
  expect(await words()).toEqual(['re-added, referenced ✓', 're-added, not referenced'])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the Last summary says everything is kept, or that nothing needed keeping; one lost item reads in the singular; more than five are counted', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  const written = { summary: '' }
  world.transcript = [...MESSAGES]
  // The summary each compaction writes is set before it.
  on('session.compact', () => ({ messages: [{ role: 'user', text: written.summary, toolUses: [] }], tokensBefore: 150000, tokensAfter: 30000 }) as never)
  await started($, world)

  // The summary carries both constraints, so nothing is lost: no Re-add, and the line says so.
  written.summary = `This session is being continued. The team uses pnpm, not npm, and chose option B, the queue-based design. Never use port 8765. Do not read any code yet, wait for the other session to finish.`
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  let mounted = await pane($)
  let shown = lines(await mounted.drawn())
  expect(shown).toContain('The summary kept everything you asked for.')
  expect(shown).toContain('kept 4 · lost 0')
  expect(shown.some(text => /^✗ [^ ]/.test(text))).toBe(false)
  expect(buttons(await mounted.drawn()).some(one => one.key === 'readd')).toBe(false)
  await mounted.unmount()

  // Nothing the user said is worth keeping.
  written.summary = 'This session is being continued. Work went on.'
  await $.session.compact({ trigger: 'manual', messages: [{ role: 'user', text: 'Thanks, that looks right.', toolUses: [] }] } as never)
  mounted = await pane($)
  shown = lines(await mounted.drawn())
  expect(shown).toContain('The summary kept everything you asked for.')
  expect(buttons(await mounted.drawn()).some(one => one.key === 'readd')).toBe(false)
  await mounted.unmount()

  // One lost item: "Re-add 1 item".
  await $.session.compact({ trigger: 'manual', messages: [{ role: 'user', text: 'Never use port 8765 for the dev server.', toolUses: [] }] } as never)
  mounted = await pane($)
  expect(buttons(await mounted.drawn()).find(one => one.key === 'readd')?.label).toBe('Re-add 1 item')
  await mounted.unmount()

  // Seven lost: five rows, then a count, and Re-add puts back all seven.
  const seven = Array.from({ length: 7 }, (_, n) => ({ role: 'user', text: `Never touch module${n}x.`, toolUses: [] }))
  await $.session.compact({ trigger: 'manual', messages: seven } as never)
  mounted = await pane($)
  shown = lines(await mounted.drawn())
  expect(shown.filter(text => /^✗ [^ ]/.test(text))).toHaveLength(5)
  expect(shown).toContain('and 2 more')
  expect(buttons(await mounted.drawn()).find(one => one.key === 'readd')?.label).toBe('Re-add 7 items')
  await mounted.press({ key: 'readd' } as never)
  expect(watch.latest.readded.map(one => one.text).sort()).toEqual(seven.map((_, n) => `Never touch module${n}x.`))
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

/** The tokens a fake model reports. */
const USAGE = { input_tokens: 800, output_tokens: 200, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }

/** What sits under $.model.complete: it answers every item of the check with `word`, or fails, after the gate opens. */
function judging(on: On, how: { word: 'yes' | 'partly' | 'no'; fail: boolean; gate: Promise<void> | null }): { prompts: string[] } {
  const seen: { prompts: string[] } = { prompts: [] }
  on('model.complete', async (_$, e) => {
    seen.prompts.push(String(e.prompt))
    if (how.gate !== null) await how.gate
    if (how.fail) return { value: { isAnswered: false, reason: 'api-error', usage: USAGE } } as never
    const items = String(e.prompt).split('ITEMS\n')[1]?.split('\n') ?? []
    return { value: { isAnswered: true, text: items.map((_, n) => `${n + 1}: ${how.word}`).join('\n'), usage: USAGE } } as never
  })
  return seen
}

test('Check with a model asks Haiku once, updates the marks and says what it used; busy while it runs, done afterwards', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  let open: () => void = () => {}
  const how = { word: 'yes' as const, fail: false, gate: new Promise<void>(resolve => (open = resolve)) }
  const asked = judging(on, how)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  expect(buttons(await mounted.drawn()).find(one => one.key === 'check')).toMatchObject({ label: 'Check with a model', dim: false })

  // While the model answers the button is dim and says so, and a second press does nothing.
  const pressed = mounted.press({ key: 'check' } as never)
  await world.clock.advance(10)
  expect(buttons(await mounted.drawn()).find(one => one.key === 'check')).toMatchObject({ label: 'Checking…', dim: true })
  await mounted.press({ key: 'check' } as never).catch(() => undefined)
  open()
  await pressed
  expect(asked.prompts).toHaveLength(1)
  expect(asked.prompts[0]).toMatch(/Never use port 8765/)

  // Every item now counts as found: nothing is lost, the note says what the model used, the button is spent.
  const shown = lines(await mounted.drawn())
  expect(shown).toContain('The model checked 5 items: 5 found, 0 partly, 0 missing.')
  expect(shown).toContain('The summary kept everything you asked for.')
  expect(shown.some(text => /^✗ [^ ]/.test(text))).toBe(false)
  expect(buttons(await mounted.drawn()).some(one => one.key === 'readd')).toBe(false)
  // The Button gives way to what the check used.
  expect(buttons(await mounted.drawn()).some(one => one.key === 'check')).toBe(false)
  expect(shown).toContain('Checked · about 1k tokens')
  expect(watch.latest).toMatchObject({ missing: 0 })
  expect(watch.latest.after?.judged).toBe(true)
  expect(watch.latest.ui).toMatchObject({ action: null, busy: false })
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Check with a model that cannot answer leaves the quick check standing and the button ready again', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  judging(on, { word: 'no', fail: true, gate: null })
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'check' } as never)
  const shown = lines(await mounted.drawn())
  expect(shown).toContain('The model could not check, so the quick check stands.')
  expect(shown).toContainEqual(expect.stringMatching(/^✗ Never use port 8765/))
  expect(buttons(await mounted.drawn()).find(one => one.key === 'check')).toMatchObject({ label: 'Check with a model', dim: false })
  expect(watch.latest.after?.judged).toBe(false)
  expect(watch.latest.ui).toMatchObject({ action: null, busy: false })
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('earlier summaries sit behind a dim Button that shows one row each and hides again', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)
  await world.clock.advance(60000)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  await $.session.compact({ trigger: 'plugin', messages: MESSAGES } as never)
  expect(watch.latest.history).toHaveLength(3)

  const mounted = await reviewPane($, world)
  // The newest summary is the section above; the two before it are the earlier ones.
  expect(buttons(await mounted.drawn()).find(one => one.key === 'earlier')).toMatchObject({ label: 'Show 2 earlier summaries', dim: true, hotkey: undefined })
  expect(lines(await mounted.drawn()).some(text => /kept \d+ · lost \d+$/.test(text) && text.includes('automatic'))).toBe(false)

  await mounted.press({ key: 'earlier' } as never)
  const shown = lines(await mounted.drawn())
  expect(shown).toContainEqual(expect.stringMatching(/^\d{1,2}:\d\d [ap]m · you asked · kept 2 · lost 2$/))
  expect(shown).toContainEqual(expect.stringMatching(/^\d{1,2}:\d\d [ap]m · automatic · kept 2 · lost 2$/))
  expect(buttons(await mounted.drawn()).find(one => one.key === 'earlier')?.label).toBe('Hide earlier summaries')

  await mounted.press({ key: 'earlier' } as never)
  expect(lines(await mounted.drawn()).some(text => /automatic · kept/.test(text))).toBe(false)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the heading names the trigger: pressed here for a summary the pane started', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'plugin', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  expect(lines(await mounted.drawn())).toContainEqual(expect.stringMatching(/^Last summary · \d{1,2}:\d\d [ap]m · pressed here$/))
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the keep list: ticks with a kind label each, five rows and the rest behind Show n more, the count follows the ticks', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  world.transcript = [
    ...MESSAGES,
    { role: 'user', text: 'Always run the tests before you commit.', toolUses: [] },
    { role: 'user', text: 'Never edit the generated files by hand.', toolUses: [] },
    { role: 'user', text: 'Prefer small commits over one big one.', toolUses: [] },
  ]
  await $.session.start(START)
  await reply($, world, FIRST)
  const mounted = await pane($)
  const ticks = async () => buttons(await mounted.drawn()).filter(one => one.key.startsWith('tick:'))
  const first = await ticks()
  expect(first).toHaveLength(5)
  expect(first.every(one => one.label.startsWith('[x] ') && one.hotkey === undefined && one.variant === undefined)).toBe(true)
  const more = buttons(await mounted.drawn()).find(one => one.key === 'more')
  expect(more).toMatchObject({ dim: true, hotkey: undefined })
  expect(more?.label).toMatch(/^Show \d+ more$/)
  expect(lines(await mounted.drawn())).toContain('5 items')

  // Past the first five the items start unticked.
  await mounted.press({ key: 'more' } as never)
  const all = await ticks()
  expect(all.length).toBeGreaterThan(5)
  expect(all.slice(5).every(one => one.label.startsWith('[ ] ') && one.dim)).toBe(true)
  expect(buttons(await mounted.drawn()).find(one => one.key === 'more')?.label).toBe('Show fewer')
  await mounted.press({ key: 'more' } as never)
  expect(await ticks()).toHaveLength(5)

  // Unticking changes the count the heading and the Summarise Button give.
  await mounted.press({ key: String(first[0]?.key) } as never)
  expect(lines(await mounted.drawn())).toContain('4 items')
  expect(buttons(await mounted.drawn()).find(one => one.key === 'summarise')?.label).toBe('Summarise now, keeping 4 items')
  expect((await ticks())[0]?.label.startsWith('[ ] ')).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Summarise now starts a summary from the pane, and a summary is told the ticked items only', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  const seen = bottoms(on, world)
  await started($, world)
  const mounted = await pane($)
  expect(lines(await mounted.drawn())).not.toContain('Last summary')

  // The press reaches the engine once, and leaves the Button as it was. (The test engine fills in no trigger or messages for
  // a call with none, as Claude Code does; the summaries below are called whole.)
  await mounted.press({ key: 'summarise' } as never)
  expect(seen.compacts).toHaveLength(1)
  expect(watch.latest.ui).toMatchObject({ action: null })
  expect(buttons(await mounted.drawn()).find(one => one.key === 'summarise')).toMatchObject({ label: 'Summarise now, keeping 4 items', dim: false })

  // The ticked items are what the summary is told; unticking one takes it out.
  await $.session.compact({ trigger: 'plugin', messages: MESSAGES } as never)
  expect(seen.compacts.at(-1)?.instructions).toMatch(/^Keep: .*8765/)
  const tick = elements(await mounted.drawn()).find(one => one.type === 'Button' && String(one.props?.label).includes('Never use port 8765'))
  await mounted.press({ key: String(tick?.props?.key) } as never)
  await $.session.compact({ trigger: 'plugin', messages: MESSAGES } as never)
  expect(seen.compacts.at(-1)?.instructions).toMatch(/^Keep: /)
  expect(seen.compacts.at(-1)?.instructions).not.toMatch(/8765/)
  expect(seen.compacts.at(-1)?.instructions).toMatch(/pnpm/)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Summarise now is dim and says Summarising while the summary runs', async ($, on) => {
  const world = prepare(on)
  let open: () => void = () => {}
  const gate = new Promise<void>(resolve => (open = resolve))
  world.transcript = [...MESSAGES]
  on('session.compact', async () => {
    await gate
    return { messages: [SUMMARY_MESSAGE], tokensBefore: 150000, tokensAfter: 30000 } as never
  })
  await started($, world)
  const mounted = await pane($)
  const pressed = mounted.press({ key: 'summarise' } as never)
  await world.clock.advance(10)
  const busy = buttons(await mounted.drawn()).find(one => one.key === 'summarise')
  expect(busy).toMatchObject({ label: 'Summarising…', dim: true, variant: 'primary' })
  expect(lines(await mounted.drawn())).toContain('A summary is running.')
  open()
  await pressed
  expect(buttons(await mounted.drawn()).find(one => one.key === 'summarise')).toMatchObject({ label: 'Summarise now, keeping 4 items', dim: false })
  expect(lines(await mounted.drawn())).not.toContain('A summary is running.')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Summarise now while a reply runs is dim and says to wait; a press that Claude Code refuses says so and clears the busy mark', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  world.transcript = [...MESSAGES]
  const mode = { throws: false, skip: null as string | null }
  let compactCalls = 0
  on('session.compact', () => {
    compactCalls += 1
    if (mode.throws) throw new Error('a turn is running')
    if (mode.skip !== null) return { skip: mode.skip } as never
    return { messages: [SUMMARY_MESSAGE], tokensBefore: 150000, tokensAfter: 30000 } as never
  })
  await started($, world)
  const mounted = await pane($)

  // A press that Claude Code refuses says why, under the Button, once.
  mode.throws = true
  await mounted.press({ key: 'summarise' } as never)
  expect(watch.latest.ui).toMatchObject({ action: null, note: 'A reply is running. Summarise when it ends.', noteFor: 'summarise' })
  expect(lines(await mounted.drawn()).filter(text => text === 'A reply is running. Summarise when it ends.')).toHaveLength(1)

  // A summary a hook holds says why, with the words the hook gave; the next press clears the earlier note.
  mode.throws = false
  mode.skip = 'held for review'
  await mounted.press({ key: 'summarise' } as never)
  expect(watch.latest.ui).toMatchObject({ action: null, note: 'The summary was held: held for review', noteFor: 'summarise' })
  expect(lines(await mounted.drawn())).toContain('The summary was held: held for review')
  expect(lines(await mounted.drawn())).not.toContain('A reply is running. Summarise when it ends.')
  expect(watch.latest.after).toBeNull()
  mode.skip = null

  // A reply running (its first request has begun, and it has not ended): the Button is dim with the reason, and a press does nothing.
  const compacts = compactCalls
  await $.turn.start({ text: 'go', turnId: 'turn-open' })
  world.next = { usage: { cacheRead: 120000, cacheWrite: 1000, output: 200 }, tools: [] }
  const stream = $.turn.step({ turnId: 'turn-open', index: 0, model: 'claude-sonnet-4-6', messageCount: 3 })
  for await (const chunk of stream) void chunk
  await stream.result
  await world.clock.advance(300)
  expect(buttons(await mounted.drawn()).find(one => one.key === 'summarise')).toMatchObject({ dim: true, label: 'Summarise now, keeping 4 items' })
  // The reason shows once: the Button's note line, not a second line from a press.
  expect(lines(await mounted.drawn()).filter(text => text === 'A reply is running. Summarise when it ends.')).toHaveLength(1)
  await mounted.press({ key: 'summarise' } as never)
  expect(compactCalls).toBe(compacts)
  // The press did not touch the note (it still says the earlier hold), and the reply line is not doubled.
  expect(watch.latest.ui.note).toBe('The summary was held: held for review')
  expect(lines(await mounted.drawn()).filter(text => text === 'A reply is running. Summarise when it ends.')).toHaveLength(1)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Pin the last thing I said pins the newest instruction or decision, once; a pinned row has a quiet Unpin', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  bottoms(on, world)
  await started($, world)
  const mounted = await pane($)
  const pin = buttons(await mounted.drawn()).find(one => one.key === 'pin-last')
  expect(pin).toMatchObject({ label: 'Pin the last thing I said', dim: true, hotkey: undefined })

  await mounted.press({ key: 'pin-last' } as never)
  const shown = lines(await mounted.drawn())
  expect(shown).toContain('Pinned. It stays on your keep list until /clear.')
  // Pins come first in the list, with their label, and the Button is gone: it is pinned already.
  const rows = buttons(await mounted.drawn()).filter(one => one.key.startsWith('tick:'))
  expect(rows[0]?.label).toMatch(/^\[x\] (Let's go with option B|Use pnpm)/)
  expect(shown).toContain('pinned')
  expect(buttons(await mounted.drawn()).some(one => one.key === 'pin-last')).toBe(false)
  expect(watch.latest.list.length).toBeGreaterThan(0)

  const unpin = buttons(await mounted.drawn()).filter(one => one.label === 'Unpin')
  expect(unpin).toHaveLength(1)
  expect(unpin[0]).toMatchObject({ dim: true, hotkey: undefined })
  await mounted.press({ key: String(unpin[0]?.key) } as never)
  expect(buttons(await mounted.drawn()).some(one => one.label === 'Unpin')).toBe(false)
  expect(lines(await mounted.drawn())).not.toContain('pinned')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('files in a scratch place are not kept; a project inside one is, because the session folder is known', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  bottoms(on, world)
  const edit = (path: string) => ({ role: 'assistant', text: '', toolUses: [{ tool_use_id: path, tool: 'Edit', input: { file_path: path }, text: 'ok' }] })
  await $.session.start({ ...START, cwd: '/tmp/proj' })
  await reply($, world, FIRST)
  const messages = [...MESSAGES, edit('/tmp/proj/src/a.ts'), edit('/tmp/other/b.ts'), edit('/home/me/.claude/plans/plan.md')]
  await $.session.compact({ trigger: 'manual', messages } as never)
  // Of the three new edits and the one in MESSAGES, the project's file and src/api/limiter.ts are recent and kept anyway.
  const safe = (watch.latest.after?.items ?? []).filter(one => one.safe).map(one => one.text)
  expect(safe).toHaveLength(2)
  expect(safe.join('\n')).toMatch(/src\/a\.ts/)
  expect(safe.join('\n')).toMatch(/limiter\.ts/)
  expect(safe.join('\n')).not.toMatch(/other\/b\.ts|plan\.md/)
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
  const before = lines(await mounted.drawn())
  expect(before.findIndex(text => text.includes('rotate the staging key'))).toBeGreaterThan(-1)
  expect(before.findIndex(text => text.includes('rotate the staging key'))).toBeLessThan(before.findIndex(text => text.includes('Never use port 8765')))
  await mounted.unmount()

  // The summary drops it; the page says so.
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  mounted = await pane($)
  const lost = lines(await mounted.drawn())
  expect(lost).toContain('kept 2 · lost 3')
  expect(lost.filter(text => /^✗ [^ ]/.test(text))).toHaveLength(3)
  expect(lost.join('\n')).toMatch(/✗ rotate the staging key/)
  await mounted.unmount()

  await $.session.end({ sessionId: SESSION, reason: 'clear' } as never)
  mounted = await pane($)
  expect(lines(await mounted.drawn()).join('\n')).not.toMatch(/staging key/)
  await mounted.unmount()
  expect(seen.submitted).toEqual([])
  expect(faults(world)).toEqual([])
})

test('/glass unpin removes the pins that hold those words, or all of them; a pinned row has a quiet Unpin', async ($, on) => {
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
  const mounted = await pane($)
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

test('straight after a summary the pane reads the context as Claude Code reports it then, not as it was', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)

  let mounted = await pane($)
  expect(lines(await mounted.drawn())).toContainEqual(expect.stringMatching(/^Context 60% full/))
  await mounted.unmount()

  // The summary leaves 30k in the context; no measurement has come yet.
  world.figures(30000, 0.5)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  await world.clock.advance(300)
  mounted = await pane($)
  const text = lines(await mounted.drawn()).join(' ')
  await mounted.unmount()
  expect(text).toMatch(/Context 15% full/)
  expect(text).not.toMatch(/60% full/)
  expect(faults(world)).toEqual([])
})

test("with auto-compact off the pane's header says how full the context is and that Claude will not summarise", async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  // A million-token window, no threshold, and the few thousand tokens of buffer Claude Code still names.
  world.shape = { window: 1000000, buffer: 3000, autoCompact: false }
  await started($, world)

  const mounted = await pane($)
  const header = lines(await mounted.drawn())[0]
  await mounted.unmount()
  expect(header).toMatch(/^Context \d+% full · Claude won't summarise by itself/)
  expect(header).not.toMatch(/summarises at|replies/)
  expect(faults(world)).toEqual([])
})

/** What the test sees of the review state: the last value written. */
type Watch = { readonly latest: GlassReview }

function watchReview(on: On): Watch {
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

test('every summary is told the keep list as instructions, after whatever the user typed, on every trigger', async ($, on) => {
  const world = prepare(on)
  const seen = bottoms(on, world)
  await started($, world)

  await $.session.compact({ trigger: 'manual', instructions: 'Focus on the API.', messages: MESSAGES } as never)
  const manual = seen.compacts.at(-1)
  expect(manual?.trigger).toBe('manual')
  expect(manual?.messages).toEqual(MESSAGES)
  expect(manual?.instructions?.startsWith('Focus on the API.\n\nKeep: ')).toBe(true)
  expect(manual?.instructions).toMatch(/Never use port 8765/)
  expect(manual?.instructions).toMatch(/pnpm, not npm/)
  expect(manual?.instructions).not.toMatch(/\/compact/)

  // Nothing typed after /compact: the instructions are the keep list alone.
  await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)
  expect(seen.compacts.at(-1)?.instructions?.startsWith('Keep: ')).toBe(true)

  // A subagent's summary is left as it came.
  await $.session.compact({ trigger: 'auto', agentId: 'agent-7', messages: MESSAGES } as never)
  expect(seen.compacts.at(-1)?.instructions).toBeUndefined()
  expect(faults(world)).toEqual([])
})

test('before any reply the keep list is rebuilt from the messages the summary runs over; with nothing to keep the instructions are left alone', async ($, on) => {
  const world = prepare(on)
  const seen = bottoms(on, world)
  await $.session.start(START)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  expect(seen.compacts.at(-1)?.instructions).toMatch(/^Keep: .*Never use port 8765/)

  // A conversation with nothing worth keeping, in a session that has not had a reply yet.
  await $.session.end({ sessionId: SESSION, reason: 'clear' } as never)
  await $.session.compact({ trigger: 'manual', instructions: 'Be brief.', messages: [{ role: 'user', text: 'Thanks, that looks right.', toolUses: [] }] } as never)
  expect(seen.compacts.at(-1)?.instructions).toBe('Be brief.')
  expect(faults(world)).toEqual([])
})

test('a summary a plugin starts is reviewed like a manual one', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'plugin', messages: MESSAGES } as never)
  expect(boundary(world)).toHaveLength(1)
  await reply($, world, SECOND)
  const found = sensor(world).filter(one => one.event === 'compaction')
  expect(found).toHaveLength(1)
  expect(found[0]).toMatchObject({ kind: 'plugin', missing: 2 })
  expect(watch.latest.history).toHaveLength(1)
  expect(watch.latest.history[0]).toMatchObject({ trigger: 'plugin', before: 150000, after: 30000 })
  expect(faults(world)).toEqual([])
})

test('a summary made ahead is told the keep list and nothing else happens: no history, no line, no sensor event', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  const seen = bottoms(on, world)
  await started($, world)
  const before = world.logs.length
  await $.session.compact({ trigger: 'precompute', messages: MESSAGES } as never)
  expect(seen.compacts.at(-1)).toMatchObject({ trigger: 'precompute' })
  expect(seen.compacts.at(-1)?.instructions).toMatch(/^Keep: .*Never use port 8765/)
  expect(world.logs.slice(before)).toEqual([])
  expect(watch.latest.history).toEqual([])
  expect(watch.latest.after).toBeNull()
  const mounted = await pane($)
  expect(lines(await mounted.drawn())).not.toContain('Last summary')
  await mounted.unmount()
  await reply($, world, SECOND)
  expect(sensor(world).filter(one => one.event === 'compaction')).toEqual([])
  expect(faults(world)).toEqual([])
})

test('items put back are kept in the list and told to the summariser until a summary holds them', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  const written = { summary: '' }
  const told: (string | undefined)[] = []
  world.transcript = [...MESSAGES]
  on('session.compact', (_$, e) => {
    told.push(e.instructions)
    return { messages: [{ role: 'user', text: written.summary, toolUses: [] }], tokensBefore: 150000, tokensAfter: 30000 } as never
  })
  await started($, world)

  // The first summary drops two things the user said; the press on the page puts them back.
  written.summary = 'This session is being continued. The team uses pnpm, not npm, and chose option B, the queue-based design.'
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const lost = (watch.latest.after?.items ?? []).filter(one => /8765|read any code/.test(one.text))
  expect(lost).toHaveLength(2)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'readd' } as never)
  await mounted.unmount()
  expect(watch.latest.readded.map(one => one.id)).toEqual(lost.map(one => one.id))

  // The messages no longer hold them, but the next reply's list still does, once each.
  world.transcript = [{ role: 'user', text: 'Thanks, that looks right.', toolUses: [] }]
  await reply($, world, SECOND)
  const listed = watch.latest.list.map(one => one.id)
  expect(listed).toEqual(expect.arrayContaining(lost.map(one => one.id)))
  expect(new Set(listed).size).toBe(listed.length)
  expect(watch.latest.readded).toHaveLength(2)

  // The next summary is told them, and holds one: that one is dropped from the put-back items, the other stays.
  written.summary = 'This session is being continued. Never use port 8765 for the dev server.'
  await $.session.compact({ trigger: 'manual', messages: world.transcript } as never)
  expect(told.at(-1)).toMatch(/^Keep: .*8765.*read any code|^Keep: .*read any code.*8765/)
  expect(watch.latest.readded.map(one => one.text)).toEqual(lost.filter(one => /read any code/.test(one.text)).map(one => one.text))
  expect(watch.latest.missing).toBe(1)
  expect(faults(world)).toEqual([])
})

test('a review state from before items could be put back still draws', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  let aged = false
  on('state.get', async (_$, e, next) => {
    const got = await next(e)
    const held = got.value as { value?: Record<string, unknown>; version: number } | undefined
    if (!aged || e.plugin !== 'claudeglass' || e.key !== 'review' || held?.value === undefined) return got
    const old = { ...held.value }
    delete old.readded
    delete old.receipts
    return { value: { ...held, value: old } } as never
  })
  await started($, world)
  aged = true
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  await reply($, world, SECOND)
  const mounted = await pane($)
  expect(lines(await mounted.drawn()).length).toBeGreaterThan(0)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a /compact that already carries the keep list is not told it twice', async ($, on) => {
  const world = prepare(on)
  const seen = bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', instructions: 'Keep: Never use port 8765.', messages: MESSAGES } as never)
  expect(seen.compacts.at(-1)?.instructions).toBe('Keep: Never use port 8765.')
  expect(faults(world)).toEqual([])
})

// --- Re-add: a press fills `readded`, prompt.compose tells Claude them ---

const COMPOSE = { model: 'claude-sonnet-4-6', promptModel: 'claude-sonnet-4-6', surfaces: ['terminal'], tools: [], outputStyle: null, traits: [] }
const READD = 'claudeglass:readd'

/** What sits under prompt.compose: the engine's own sections. */
function composing(on: On): void {
  on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'You are Claude.', scope: 'shared' }] }) as never)
}

type Composed = { id: string; text: string; scope: string }[]

async function compose($: Dollar, traits: string[] = []): Promise<Composed> {
  return (await $.prompt.compose({ ...COMPOSE, traits } as never)).sections as unknown as Composed
}

/** A summary that drops both constraints, then the page's press that puts them back. */
async function lossyThenReadd($: Dollar, world: World): Promise<void> {
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'readd' } as never)
  await mounted.unmount()
}

function item(id: string, kind: GlassKeepItem['kind'], text: string, safe = false): GlassKeepItem {
  return { id, kind, text, safe, why: '', exact: [], at: 0 }
}

/** A review whose last check marked these items. */
function checked(marks: Record<string, GlassMark>, items: GlassKeepItem[]): GlassReview {
  const review = emptyReview()
  return { ...review, after: { ts: 1, trigger: 'manual', summary: '', items, marks, judged: false, earlier: [] } }
}

test('after a press the main session is told the items again in one session section; before it, and for a teammate, there is none', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  composing(on)
  await $.session.start(START)

  // Nothing put back: the engine's sections come back as they were.
  expect((await compose($)).map(one => one.id)).toEqual(['intro'])

  await lossyThenReadd($, world)
  const main = await compose($)
  expect(main.map(one => one.id)).toEqual(['intro', READD])
  const added = main[1] as Composed[number]
  expect(added.scope).toBe('session')
  expect(added.text).toMatch(/^Before compaction we agreed: .*8765.*; .*read any code.*\.$/)
  expect(added.text).not.toMatch(/pnpm|option B/)

  // A teammate has its own conversation.
  expect((await compose($, ['teammate'])).map(one => one.id)).toEqual(['intro'])
  // Other traits still get it.
  expect((await compose($, ['lean'])).map(one => one.id)).toEqual(['intro', READD])
  expect(faults(world)).toEqual([])
})

test('the re-add text is byte-identical across requests, whatever the time or the replies since', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  composing(on)
  await lossyThenReadd($, world)
  const first = (await compose($)).find(one => one.id === READD)?.text
  expect(first).toBeDefined()
  await world.clock.advance(120000)
  await reply($, world, SECOND)
  const second = (await compose($)).find(one => one.id === READD)?.text
  expect(second).toBe(first)
  expect(new TextEncoder().encode(String(second))).toEqual(new TextEncoder().encode(String(first)))
  expect(faults(world)).toEqual([])
})

test('a summary that holds the items drops the section again, and the receipts with it', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  const written = { summary: 'This session is being continued. The team uses pnpm, not npm, and chose option B, the queue-based design.' }
  world.transcript = [...MESSAGES]
  on('session.compact', () => ({ messages: [{ role: 'user', text: written.summary, toolUses: [] }], tokensBefore: 150000, tokensAfter: 30000 }) as never)
  composing(on)
  await lossyThenReadd($, world)
  expect(Object.keys(watch.latest.receipts)).toHaveLength(2)

  written.summary = 'This session is being continued. Never use port 8765 for the dev server. Do not read any code yet; wait for the other session to finish.'
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  expect(watch.latest.readded).toEqual([])
  expect(watch.latest.receipts).toEqual({})
  expect((await compose($)).map(one => one.id)).toEqual(['intro'])
  expect(faults(world)).toEqual([])
})

test('the page press fills readded with what the check marked missing, bumps the count the sensor records, and a second press adds nothing', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'readd' } as never)
  expect(watch.latest.readded).toHaveLength(2)
  expect(Object.values(watch.latest.receipts)).toEqual([
    { at: expect.any(Number), replies: 0, referenced: null },
    { at: expect.any(Number), replies: 0, referenced: null },
  ])
  const receipts = watch.latest.receipts
  // The Button is gone once everything is back; a press that still arrives adds nothing.
  await mounted.press({ key: 'readd' } as never).catch(() => undefined)
  expect(watch.latest.readded).toHaveLength(2)
  expect(watch.latest.receipts).toEqual(receipts)
  await mounted.unmount()

  await reply($, world, SECOND)
  expect(sensor(world).filter(one => one.event === 'compaction')[0]).toMatchObject({ kind: 'manual', missing: 2, added: 2 })
  expect(faults(world)).toEqual([])
})

test('readdMissing puts back what was missing or partly there, once each, and leaves the rest as it was', () => {
  const lost = item('a', 'instruction', 'Never use port 8765')
  const half = item('b', 'decision', 'Use the queue design')
  const kept = item('c', 'instruction', 'Use pnpm')
  const file = item('d', 'file', 'src/api/limiter.ts')
  const safe = item('e', 'instruction', 'Always safe', true)
  const marks: Record<string, GlassMark> = { a: 'missing', b: 'partly', c: 'found', d: 'missing', e: 'safe' }
  const review = checked(marks, [lost, half, kept, file, safe])

  const once = readdMissing(review, 1000)
  expect(once.readded.map(one => one.id)).toEqual(['a', 'b'])
  expect(once.receipts).toEqual({ a: { at: 1000, replies: 0, referenced: null }, b: { at: 1000, replies: 0, referenced: null } })
  expect({ ...once, readded: review.readded, receipts: review.receipts }).toEqual(review)

  // A second call, later, skips what is already there and keeps its receipt.
  const twice = readdMissing(once, 2000)
  expect(twice).toBe(once)
  const more = readdMissing({ ...checked({ ...marks, c: 'missing' }, [lost, half, kept, file, safe]), readded: once.readded, receipts: once.receipts }, 3000)
  expect(more.readded.map(one => one.id)).toEqual(['a', 'b', 'c'])
  expect(more.receipts.a).toEqual({ at: 1000, replies: 0, referenced: null })
  expect(more.receipts.c).toEqual({ at: 3000, replies: 0, referenced: null })

  // Nothing checked yet: nothing to add.
  expect(readdMissing(emptyReview(), 1)).toEqual(emptyReview())
})

test('a summary that holds a put-back item drops its receipt and keeps the others', () => {
  const a = item('a', 'instruction', 'Never use port 8765')
  const b = item('b', 'instruction', 'Do not read any code')
  const review = readdMissing(checked({ a: 'missing', b: 'missing' }, [a, b]), 5)
  const reviewed = { ts: 9, trigger: 'manual' as const, summary: '', items: [a, b], marks: { a: 'found', b: 'missing' } as Record<string, GlassMark>, judged: false, earlier: [] }
  const next = withSummary(sound(review), reviewed, { ts: 9, trigger: 'manual', before: null, after: null, kept: 1, missing: 1 })
  expect(next.readded.map(one => one.id)).toEqual(['b'])
  expect(Object.keys(next.receipts)).toEqual(['b'])
})

test('a summary counts as lost what the user said that it dropped or kept in part; what is already put back is not offered again', () => {
  const a = item('a', 'instruction', 'Never use port 8765')
  const b = item('b', 'decision', 'Use the queue design')
  const c = item('c', 'instruction', 'Use pnpm')
  const file = item('d', 'file', 'src/api/limiter.ts')
  const marks: Record<string, GlassMark> = { a: 'missing', b: 'partly', c: 'found', d: 'missing' }
  const reviewed = { ts: 9, trigger: 'manual' as const, summary: '', items: [a, b, c, file], marks, judged: false, earlier: [] }
  const next = withSummary(emptyReview(), reviewed, { ts: 9, trigger: 'manual', before: null, after: null, kept: 2, missing: 2 }, 7)
  // The band, the toast and the sensor read this one number: partly counts, a file does not.
  expect(next.missing).toBe(2)
  expect(next.seen).toBe(false)
  // The band keeps Kept while the count of replies is still this one.
  expect(next.repliesAt).toBe(7)
  expect(lostItems(next).map(one => one.id)).toEqual(['a', 'b'])
  expect(lostCount(next)).toBe(2)

  // Put back, nothing is left to offer, though the summary still lost them.
  const back = readdMissing(next, 1)
  expect(lostCount(back)).toBe(0)
  expect(back.missing).toBe(2)
  expect(lostCount(emptyReview())).toBe(0)
})

test('while Re-add is offered Summarise steps back to secondary; a note sits under the section whose press set it; the ticks are plain and fit their row', async ($, on) => {
  const world = prepare(on)
  const watch = watchReview(on)
  bottoms(on, world)
  await started($, world)
  await $.command.run({ command: 'glass', args: 'pin rotate the staging key before friday and the other long thing nobody should have to read in full' } as never)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)

  const tree = await mounted.drawn()
  expect(buttons(tree).find(one => one.key === 'summarise')).toMatchObject({ variant: 'secondary' })
  expect(buttons(tree).find(one => one.key === 'readd')).toMatchObject({ variant: 'primary' })
  // Plain ticks: no `[ ... ]` frame around the `[x]`, and every row fits the row width, the pin's with its Unpin.
  const ticks = elements(tree).filter(one => one.type === 'Button' && String(one.props?.key).startsWith('tick:'))
  expect(ticks.length).toBeGreaterThan(0)
  for (const one of ticks) expect(one.props?.plain).toBe(true)
  const pinned = ticks.find(one => /staging key/.test(String(one.props?.label)))
  expect(pinned).toBeDefined()
  expect(String(pinned?.props?.label).length + 2 + 'pinned'.length + 2 + 'Unpin'.length + 4).toBeLessThanOrEqual(56)
  for (const one of ticks) expect(String(one.props?.label).length + 2 + 'instruction'.length).toBeLessThanOrEqual(56)

  // The pin's note sits under the Keep list, before the Last summary; Re-add's sits under the Last summary.
  await mounted.press({ key: 'readd' } as never)
  expect(watch.latest.ui).toMatchObject({ note: 'Claude is told these again with its next reply.', noteFor: 'readd' })
  const shown = lines(await mounted.drawn())
  expect(shown.indexOf('Claude is told these again with its next reply.')).toBeGreaterThan(shown.findIndex(text => text.startsWith('Last summary')))
  // The next press clears it.

  await mounted.press({ key: String(ticks[0]?.props?.key) } as never)
  expect(watch.latest.ui).toMatchObject({ note: null, noteFor: null })
  expect(lines(await mounted.drawn())).not.toContain('Claude is told these again with its next reply.')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

// -- the sensor: what the summaries were told, and what each press did -------------------------

/** The `tweak` events the sensor file holds, in the order they were written. */
function tweaks(world: World): Event[] {
  return sensor(world).filter(one => one.event === 'tweak')
}

test('a summary the hook gave the keep list writes instructed 1, and one with nothing to keep writes 0', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  await reply($, world, SECOND)
  expect(sensor(world).filter(one => one.event === 'compaction')[0]).toMatchObject({ kind: 'manual', instructed: 1 })
  expect(faults(world)).toEqual([])
})

test('a summary with nothing to keep writes instructed 0, not a missing field', async ($, on) => {
  const world = prepare(on)
  const quiet = [{ role: 'user', text: 'Thanks, that looks right.', toolUses: [] }]
  bottoms(on, world)
  world.transcript = quiet
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: quiet } as never)
  await reply($, world, SECOND)
  const found = sensor(world).filter(one => one.event === 'compaction')
  expect(found).toHaveLength(1)
  expect(found[0]?.instructed).toBe(0)
  expect(faults(world)).toEqual([])
})

test('a summary counts the put-back items the replies have referenced by then', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'readd' } as never)
  await mounted.unmount()

  // The next reply names the port: one of the two put-back items is referenced.
  const step = { cacheRead: 120000, cacheWrite: 1000, output: 200 }
  await reply($, world, { usd: 0.05, steps: [{ ...step, answer: 'I will not use port 8765 for the dev server.' }] })
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  await reply($, world, SECOND)
  const found = sensor(world).filter(one => one.event === 'compaction')
  expect(found).toHaveLength(2)
  expect(found[0]).toMatchObject({ referenced: 0 })
  expect(found[1]).toMatchObject({ referenced: 1 })
  expect(faults(world)).toEqual([])
})

test('Summarise now writes a tweak with the ticked count, and the summary it starts fills in the kept count', async ($, on) => {
  const world = prepare(on)
  let open: () => void = () => {}
  const gate = new Promise<void>(resolve => (open = resolve))
  world.transcript = [...MESSAGES]
  // The test engine runs the press's own call with no trigger, so the hook sees the summary that is made while the press waits.
  on('session.compact', async (_$, e) => {
    if (e.trigger === undefined) await gate
    return { messages: [SUMMARY_MESSAGE], tokensBefore: 150000, tokensAfter: 30000 } as never
  })
  await started($, world)
  const mounted = await pane($)
  expect(buttons(await mounted.drawn()).find(one => one.key === 'summarise')?.label).toBe('Summarise now, keeping 4 items')
  const pressed = mounted.press({ key: 'summarise' } as never)
  await world.clock.advance(10)
  await $.session.compact({ trigger: 'plugin', messages: MESSAGES } as never)
  open()
  await pressed
  await mounted.unmount()
  await reply($, world, SECOND)

  expect(tweaks(world)).toHaveLength(1)
  expect(tweaks(world)[0]).toMatchObject({ kind: 'summarise', before: 4, after: 2 })
  expect(Object.keys(tweaks(world)[0] as object).sort()).toEqual(['after', 'before', 'event', 'kind', 'ts'])
  expect(faults(world)).toEqual([])
})

test('a Summarise press that gets no summary writes its tweak with no after, and a later summary does not fill it in', async ($, on) => {
  const world = prepare(on)
  world.transcript = [...MESSAGES]
  let skip = true
  on('session.compact', () => (skip ? { skip: 'Held for review' } : { messages: [SUMMARY_MESSAGE], tokensBefore: 150000, tokensAfter: 30000 }) as never)
  await started($, world)
  const mounted = await pane($)
  await mounted.press({ key: 'summarise' } as never)
  await mounted.unmount()
  skip = false
  await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)
  await reply($, world, SECOND)

  expect(tweaks(world)).toHaveLength(1)
  expect(tweaks(world)[0]).toMatchObject({ kind: 'summarise', before: 4 })
  expect('after' in (tweaks(world)[0] as object)).toBe(false)
  expect(faults(world)).toEqual([])
})

test('a Summarise press that is over does not wait for a summary: a later one does not fill in its after', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  const mounted = await pane($)
  // The press's own call goes through but the hook never reviews it (no trigger), so no summary is coming for the press.
  await mounted.press({ key: 'summarise' } as never)
  await mounted.unmount()
  await $.session.compact({ trigger: 'auto', messages: MESSAGES } as never)
  await reply($, world, SECOND)

  expect(tweaks(world)).toHaveLength(1)
  expect('after' in (tweaks(world)[0] as object)).toBe(false)
  expect(faults(world)).toEqual([])
})

test('a Summarise press while another press is working does nothing: no summary, no tweak', async ($, on) => {
  const world = prepare(on)
  let open: () => void = () => {}
  const gate = new Promise<void>(resolve => (open = resolve))
  judging(on, { word: 'yes', fail: false, gate })
  let compactCalls = 0
  world.transcript = [...MESSAGES]
  on('session.compact', () => {
    compactCalls += 1
    return { messages: [SUMMARY_MESSAGE], tokensBefore: 150000, tokensAfter: 30000 } as never
  })
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const calls = compactCalls
  const mounted = await reviewPane($, world)
  const checking = mounted.press({ key: 'check' } as never)
  await world.clock.advance(10)
  await mounted.press({ key: 'summarise' } as never)
  expect(compactCalls).toBe(calls)
  open()
  await checking
  await mounted.unmount()
  await reply($, world, SECOND)
  expect(tweaks(world).map(one => one.kind)).toEqual(['check'])
  expect(faults(world)).toEqual([])
})

test('Re-add writes a tweak with the items put back, and its referenced count once every receipt is settled', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'readd' } as never)
  await mounted.unmount()

  // The first reply records the press; one item waits on the replies, so there is no count yet.
  const step = { cacheRead: 120000, cacheWrite: 1000, output: 200 }
  await reply($, world, { usd: 0.05, steps: [{ ...step, answer: 'I will not use port 8765 for the dev server.' }] })
  expect(tweaks(world)).toHaveLength(1)
  expect(tweaks(world)[0]).toMatchObject({ kind: 'readd', before: 2 })
  expect('after' in (tweaks(world)[0] as object)).toBe(false)

  // The tenth reply settles the other as not referenced; the count reaches the event with the next reply's.
  for (let count = 0; count < 10; count++) await reply($, world, { usd: 0.05, steps: [{ ...step, answer: 'Formatted the file.' }] })
  expect(tweaks(world)).toHaveLength(1)
  expect(tweaks(world)[0]).toMatchObject({ kind: 'readd', before: 2, after: 1 })
  expect(faults(world)).toEqual([])
})

test('a /clear before the next reply keeps the referenced count of a settled re-add for its tweak', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'readd' } as never)
  await mounted.unmount()

  const step = { cacheRead: 120000, cacheWrite: 1000, output: 200 }
  await reply($, world, { usd: 0.05, steps: [{ ...step, answer: 'I will not use port 8765 for the dev server.' }] })
  // The tenth reply settles the other item, and the /clear comes before the reply that would record the count.
  for (let count = 0; count < 9; count++) await reply($, world, { usd: 0.05, steps: [{ ...step, answer: 'Formatted the file.' }] })
  expect(tweaks(world)[0]).toMatchObject({ kind: 'readd', before: 2 })
  expect('after' in (tweaks(world)[0] as object)).toBe(false)
  await $.session.end({ sessionId: SESSION, reason: 'clear' } as never)
  expect(tweaks(world)).toHaveLength(1)
  expect(tweaks(world)[0]).toMatchObject({ kind: 'readd', before: 2, after: 1 })
  expect(faults(world)).toEqual([])
})

test('Check with a model writes a tweak with the missing count before and after', async ($, on) => {
  const world = prepare(on)
  judging(on, { word: 'yes', fail: false, gate: null })
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'check' } as never)
  await mounted.unmount()
  await reply($, world, SECOND)
  expect(tweaks(world)).toHaveLength(1)
  expect(tweaks(world)[0]).toMatchObject({ kind: 'check', before: 2, after: 0 })
  expect(faults(world)).toEqual([])
})

test('a Check press the model could not answer writes its tweak with no after', async ($, on) => {
  const world = prepare(on)
  judging(on, { word: 'no', fail: true, gate: null })
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'check' } as never)
  await mounted.unmount()
  await reply($, world, SECOND)
  expect(tweaks(world)).toHaveLength(1)
  expect(tweaks(world)[0]).toMatchObject({ kind: 'check', before: 2 })
  expect('after' in (tweaks(world)[0] as object)).toBe(false)
  expect(faults(world)).toEqual([])
})

test('the Hold switch writes a tweak: 1 when it goes on, 0 when it goes off, never an after', async ($, on) => {
  const world = prepare(on)
  bottoms(on, world)
  await started($, world)
  const mounted = await pane($)
  await mounted.press({ key: 'hold' } as never)
  await mounted.press({ key: 'hold' } as never)
  await mounted.unmount()
  await reply($, world, SECOND)

  expect(tweaks(world).map(one => [one.kind, one.before])).toEqual([
    ['hold', 1],
    ['hold', 0],
  ])
  for (const one of tweaks(world)) expect(Object.keys(one).sort()).toEqual(['before', 'event', 'kind', 'ts'])
  expect(faults(world)).toEqual([])
})

test('the sensor file holds counts and words from the vocabulary only: no text from a press either', async ($, on) => {
  const world = prepare(on)
  judging(on, { word: 'yes', fail: false, gate: null })
  bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)
  const mounted = await reviewPane($, world)
  await mounted.press({ key: 'readd' } as never)
  await mounted.press({ key: 'check' } as never)
  await mounted.press({ key: 'hold' } as never)
  await mounted.press({ key: 'summarise' } as never)
  await mounted.unmount()
  await reply($, world, SECOND)

  const text = String(world.files.get(SENSOR))
  expect(text).not.toMatch(/8765|pnpm|option B|queue-based|limiter|continued from a previous|rate limiting|Held for review/)
  expect(tweaks(world).map(one => one.kind).sort()).toEqual(['check', 'hold', 'readd', 'summarise'])
  for (const one of tweaks(world)) {
    for (const [key, value] of Object.entries(one)) {
      if (key === 'event' || key === 'kind') expect(typeof value).toBe('string')
      else expect(Number.isInteger(value)).toBe(true)
    }
  }
  expect(faults(world)).toEqual([])
})
