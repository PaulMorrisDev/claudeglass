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

/** Turns an opt-in on the way a person does: the Detailed view of the Compaction page. */
async function turnOn($: Dollar, key: 'hold' | 'readd'): Promise<void> {
  await $.command.run({ command: 'glass', args: 'detail' } as never)
  const mounted = await pane($)
  await mounted.press({ key: 'page:compaction' } as never)
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

test('the review page opens on what may be missing, with a draft for each; nothing is sent', async ($, on) => {
  const world = prepare(on)
  const seen = bottoms(on, world)
  await started($, world)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES } as never)

  const mounted = await reviewPane($, world)
  const shown = lines(await mounted.drawn())
  expect(shown.join('\n')).toMatch(/2 things you said may not be in the summary\./)
  const adds = shown.filter(text => text.startsWith('Add back'))
  expect(adds).toHaveLength(2)
  expect(adds.join('\n')).toMatch(/Never use port 8765/)
  expect(adds.join('\n')).toMatch(/Don't read any code yet/)
  // Only what may be missing: the kept items are not listed on the first screen.
  expect(shown.join('\n')).not.toMatch(/pnpm/)
  expect(shown.join('\n')).not.toMatch(/option B/)

  // Every Button has a hotkey, and none is used twice; the desktop draws the same lines.
  const keys = elements(await mounted.drawn()).filter(one => one.type === 'Button').map(one => String(one.props?.hotkey ?? ''))
  expect(keys.every(key => key !== '')).toBe(true)
  expect(new Set(keys).size).toBe(keys.length)
  const desktop = await pane($, 'desktop')
  expect(lines(await desktop.drawn())).toEqual(shown)
  await desktop.unmount()

  const first = elements(await mounted.drawn()).find(one => one.type === 'Button' && String(one.props?.label).startsWith('Add back'))
  await mounted.press({ key: String(first?.props?.key) } as never)
  expect(seen.filled).toHaveLength(1)
  expect(seen.filled[0]).toMatch(/^Before compaction we agreed: (Never use port 8765 for the dev server, it is reserved|Don't read any code yet; wait for the other session to finish)\.$/)
  expect(lines(await mounted.drawn()).join('\n')).toMatch(/A draft is in the prompt box/)

  await mounted.press({ key: 'add-all' } as never)
  expect(seen.filled[1]).toMatch(/^Before compaction we agreed: .*8765.*; .*read any code.*\.$/)
  expect(seen.submitted).toEqual([])
  await mounted.unmount()

  // The drafts count as added back when the next reply's counts reach the sensor.
  await reply($, world, SECOND)
  expect(sensor(world).filter(one => one.event === 'compaction')[0]).toMatchObject({ kind: 'manual', missing: 2, added: 3 })
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
  expect(text).toMatch(/✗ Never use port 8765/)
  expect(text).toMatch(/not in the summary/)
  expect(text).toMatch(/✓ Use pnpm, not npm/)

  // Pressing an item pins it; pressing it again unpins.
  const pinned = elements(await mounted.drawn()).find(one => one.type === 'Button' && String(one.props?.key).startsWith('pin:') && String(one.props?.label).includes('Never use port 8765'))
  await mounted.press({ key: String(pinned?.props?.key) } as never)
  expect(lines(await mounted.drawn()).join('\n')).toMatch(/Pinned · not in the summary/)
  await mounted.press({ key: String(pinned?.props?.key) } as never)
  expect(lines(await mounted.drawn()).join('\n')).not.toMatch(/Pinned · /)

  await mounted.press({ key: 'earlier' } as never)
  const earlier = elements(await mounted.drawn()).filter(one => one.type === 'Button' && String(one.props?.key).startsWith('earlier:'))
  expect(earlier.length).toBe(2)
  expect(String(earlier[0]?.props?.label)).toMatch(/^Let's go with option B/)
  await mounted.press({ key: String(earlier[0]?.props?.key) } as never)
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
  const link = await detailed.find({ type: 'Link' })
  expect(String(link?.props.href)).toBe('http://localhost:8792/#/spend/usage?t=compactions.compactions_summary')
  await detailed.press({ key: 'dashboard-copy' } as never)
  expect(world.copied.at(-1)).toBe('http://127.0.0.1:8792/#/spend/usage?t=compactions.compactions_summary')
  await detailed.unmount()
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
  expect(text).toMatch(/Before Claude summarises, pick what the summary must keep\./)
  expect(text).toMatch(/\[x\] Never use port 8765/)
  expect(text).toMatch(/\[x\] Let's go with option B/)
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
  expect(lines(await mounted.drawn()).join('\n')).toMatch(/3 things you said may not be in the summary\./)
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
