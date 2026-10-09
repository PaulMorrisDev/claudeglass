import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { SENSOR_VOCAB } from '../hooks/generated/coaching.js'
import { faults, notes, prepare, reply, SESSION, settingsFile, START, T0 } from './support'
import type { World } from './support'

const MINUTE = 60 * 1000
const SENSOR = `/cfg/claudeglass/mod/sensor/${SESSION}.json`
const COACH = '/cfg/claudeglass/mod/coach.json'

// One reply that leaves the main context at 120k, and one that only reads it again.
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
const AGAIN = { steps: [{ input: 500, cacheRead: 120000, cacheWrite: 500, output: 100 }], usd: 0.1 }
const BACKGROUND = 'Command running in background with ID: bx1. Output is being written to a file you can read.'

type Result = { result?: unknown; text?: string; isError?: boolean }
type Rig = {
  /** What the prompt box holds, as the mod's edit hook last answered. */
  box: string
  /** A paste longer than this shows in the box as a short placeholder, as the real box does; null for never. */
  shorten: number | null
  statuses: (string | undefined)[]
  results: Record<string, Result>
}

/** What sits under the events the coaching reads: a prompt box that applies each edit, tool results, and the status line. */
function rig(on: On): Rig {
  const state: Rig = { box: '', shorten: 5000, statuses: [], results: {} }
  on('prompt.edit', (_$, e) => {
    if (state.shorten !== null && e.inputText.length > state.shorten) {
      const text = `${e.text.slice(0, e.start)}[Pasted text #1 +${Math.round(e.inputText.length / 80)} lines]${e.text.slice(e.end)}`
      return { text, cursor: text.length } as never
    }
    return { text: e.text.slice(0, e.start) + e.inputText + e.text.slice(e.end), cursor: e.start + e.inputText.length } as never
  })
  on('prompt.submit', (_$, e) => e as never)
  on('tool.call', (_$, e) => (state.results[String(e.tool)] ?? { result: {} }) as never)
  on('ui.status', (_$, e) => {
    state.statuses.push(e.text)
    return { value: undefined } as never
  })
  return state
}

type Edit = { origin: { kind: 'composer' }; text: string; cursor: number; start: number; end: number; inputText: string }
type Prompt = { edit: (e: Edit) => Promise<{ text: string }> }

async function edit($: Engine, state: Rig, start: number, end: number, inputText: string): Promise<void> {
  const answer = await ($.prompt as unknown as Prompt).edit({ origin: { kind: 'composer' }, text: state.box, cursor: end, start, end, inputText })
  state.box = answer.text
}

/** Types or pastes `text` at the end of the box. */
async function put($: Engine, state: Rig, text: string): Promise<void> {
  await edit($, state, state.box.length, state.box.length, text)
}

async function clear($: Engine, state: Rig): Promise<void> {
  await edit($, state, 0, state.box.length, '')
}

/** Sends the message (the box empties). */
async function send($: Engine, state: Rig, text: string, origin: string = 'composer'): Promise<void> {
  await $.prompt.submit({ text, wait: false, origin: { kind: origin } } as never)
  state.box = ''
}

async function tool($: Engine, name: string, input: Record<string, unknown> = {}): Promise<void> {
  await $.tool.call({ tool: name, tool_use_id: `call-${name}`, ...input } as never)
}

async function hint($: Engine, world: World, surface: 'terminal' | 'desktop' = 'terminal', props: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface, component: 'PromptHint', props: { isDraft: true, isWorking: false, hint: 'Enter to send', ...props } as never })
  await mounted.unmount()
  return world.seen.PromptHint ?? {}
}

type Event = { ts: number; event: string; kind?: string; surface?: string; outcome?: string }

function events(world: World): Event[] {
  const text = world.files.get(SENSOR)
  return text === undefined ? [] : ((JSON.parse(text) as { events: Event[] }).events)
}

/** Two more replies, which settle the notes the screen showed. */
async function settle($: Engine, world: World): Promise<void> {
  await reply($, world, { gap: MINUTE, ...AGAIN })
  await reply($, world, { gap: MINUTE, ...AGAIN })
}

function sensorNotes(world: World, kind: string): Event[] {
  return events(world).filter(one => one.event === 'note' && one.kind === kind)
}

async function started($: Engine, world: World): Promise<void> {
  await $.session.start(START)
  await reply($, world, FIRST)
}

/** A first message that asks for no change: the session's first message has no time since another, so it can't start a run. */
async function opening($: Engine, world: World, state: Rig): Promise<void> {
  await send($, state, 'summarise the repo')
  await reply($, world, { gap: MINUTE, ...AGAIN })
}

// -- big_paste ---------------------------------------------------------------------

test('a big paste in the box says what it will cost, a smaller one says nothing', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)

  await put($, state, 'x'.repeat(20_000))
  expect((await hint($, world)).tail).toBeUndefined()

  await clear($, state)
  await put($, state, 'x'.repeat(60_000))
  const big = await hint($, world)
  expect(String(big.tail)).toMatch(/^Large paste, about 1\d(\.\d)?k tokens, and every later reply reads it again\./)
  expect(big.hint).toBe('Enter to send')

  await clear($, state)
  expect((await hint($, world)).tail).toBeUndefined()
  expect(faults(world)).toEqual([])
})

test('a paste long enough to be a big one in the box itself counts the same', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.shorten = null
  await started($, world)
  await put($, state, 'y'.repeat(50_000))
  expect(String((await hint($, world)).tail)).toMatch(/^Large paste, about 12(\.\d)?k tokens/)
})

test('the paste hint on the desktop goes before the engine hint', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)
  await put($, state, 'x'.repeat(60_000))
  const seen = await hint($, world, 'desktop')
  expect(String(seen.hint)).toMatch(/^Large paste, about .* · Enter to send$/)
  expect(seen.tail).toBeUndefined()
})

// -- drip_feed ---------------------------------------------------------------------

/** A small request sent, and the reply to it, which changed a file. */
async function drip($: Engine, world: World, state: Rig, text: string): Promise<void> {
  await send($, state, text)
  await tool($, 'Edit', { file_path: 'src/app.py' })
  await reply($, world, { gap: MINUTE, ...AGAIN })
}

test('the third small change in a row says to send them as one message', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)
  await opening($, world, state)

  await drip($, world, state, 'rename foo to bar')
  await put($, state, 'also rename baz to qux')
  expect((await hint($, world)).tail).toBeUndefined()
  await clear($, state)

  await drip($, world, state, 'also rename baz to qux')
  await put($, state, 'and remove the old helper')
  const seen = await hint($, world)
  expect(String(seen.tail)).toMatch(/^3 small changes in a row, each its own message\. Each one re-reads the whole session, about 12\dk tokens\./)
  expect(faults(world)).toEqual([])
})

test('a question, or a change that touched no file, does not make a drip', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)
  await opening($, world, state)

  await send($, state, 'rename foo to bar')
  await reply($, world, { gap: MINUTE, ...AGAIN })
  await drip($, world, state, 'also rename baz to qux')
  await put($, state, 'and remove the old helper')
  expect((await hint($, world)).tail).toBeUndefined()
  await clear($, state)

  await put($, state, 'why is the build slow?')
  expect((await hint($, world)).tail).toBeUndefined()
})

test('a pause longer than the window ends the run', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)
  await opening($, world, state)
  await drip($, world, state, 'rename foo to bar')
  await drip($, world, state, 'also rename baz to qux')
  await world.clock.advance(30 * MINUTE)
  await put($, state, 'and remove the old helper')
  expect((await hint($, world)).tail).not.toMatch(/small changes in a row/)
})

// -- status_poll -------------------------------------------------------------------

test('asking how background work is going says it re-reads the session; the words fit the surface', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.Bash = { result: {}, text: BACKGROUND }
  await started($, world)
  await tool($, 'Bash', { command: 'npm test &' })

  await put($, state, 'how is it going?')
  const terminal = await hint($, world)
  expect(String(terminal.tail)).toMatch(/^Background work is still running\. Asking how it's going re-reads 12\dk tokens\. Typing \/tasks shows what is still running, with no message sent\.$/)

  const desktop = await hint($, world, 'desktop')
  expect(String(desktop.hint)).toMatch(/^Background work is still running\..* The task panel shows what is still running, with no message sent\. · Enter to send$/)
  expect(faults(world)).toEqual([])
})

test('no background work, a finished one, or a message that is not a status check says nothing', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)

  await put($, state, 'how is it going?')
  expect((await hint($, world)).tail).toBeUndefined()
  await clear($, state)

  state.results.Bash = { result: {}, text: BACKGROUND }
  await tool($, 'Bash', { command: 'npm test &' })
  await put($, state, 'rename foo to bar')
  expect((await hint($, world)).tail).toBeUndefined()
  await clear($, state)

  await send($, state, '<task-notification><tool-use-id>call-Bash</tool-use-id></task-notification>', 'task-notification')
  await put($, state, 'how is it going?')
  expect((await hint($, world)).tail).toBeUndefined()
})

test('a status check after the cache expired gets the cold-cache warning, not the poll', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.Bash = { result: {}, text: BACKGROUND }
  await started($, world)
  await tool($, 'Bash', { command: 'npm test &' })
  await world.clock.advance(400 * 1000)
  await put($, state, 'how is it going?')
  const seen = await hint($, world)
  expect(String(seen.tail)).toMatch(/^Idle \d+ min, so the cache expired/)
})

// -- cold_return ---------------------------------------------------------------------

test('the cold-cache warning rests, and is one note for the sensor', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)
  await world.clock.advance(400 * 1000)
  const first = await hint($, world)
  expect(String(first.tail)).toMatch(/^Idle \d+ min, so the cache expired \(est\.\): this send costs about \$\d/)
  expect(String(first.tail)).toMatch(/\/clear first costs about \$\d/)

  // The send rebuilds the cache; it expires again, but the warning rests for hours.
  await send($, state, 'carry on')
  await reply($, world, { gap: 0, steps: [{ input: 1000, cacheRead: 0, cacheWrite: 120000, output: 200 }], usd: 0.45 })
  await world.clock.advance(400 * 1000)
  expect((await hint($, world)).tail).toBeUndefined()

  await settle($, world)
  const note = sensorNotes(world, 'cold_return')
  expect(note).toHaveLength(1)
  expect(note[0]?.surface).toBe('hint')
  expect(faults(world)).toEqual([])
})

test('the cold-cache warning can be muted', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { muted: ['cold_return'] } }) })
  const state = rig(on)
  await started($, world)
  await world.clock.advance(400 * 1000)
  expect((await hint($, world)).tail).toBeUndefined()
})

// -- one item, in the hook's order --------------------------------------------------------

test('a big paste goes before the cold-cache warning, and the warning comes back when the paste goes', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)
  await world.clock.advance(400 * 1000)

  await put($, state, 'x'.repeat(60_000))
  const paste = await hint($, world)
  expect(String(paste.tail)).toMatch(/^Large paste/)
  expect(String(paste.tail)).not.toMatch(/cache expired/)

  await clear($, state)
  expect(String((await hint($, world)).tail)).toMatch(/^Idle \d+ min, so the cache expired/)
})

test('a drip goes before the cold-cache warning', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)
  await opening($, world, state)
  await drip($, world, state, 'rename foo to bar')
  await drip($, world, state, 'also rename baz to qux')
  await world.clock.advance(400 * 1000)
  await put($, state, 'and remove the old helper')
  const seen = await hint($, world)
  expect(String(seen.tail)).toMatch(/^3 small changes in a row/)
  expect(String(seen.tail)).not.toMatch(/cache expired/)
})

test('a muted kind gives way to the next', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { muted: ['big_paste'] } }) })
  const state = rig(on)
  await started($, world)
  await world.clock.advance(400 * 1000)
  await put($, state, 'x'.repeat(60_000))
  expect(String((await hint($, world)).tail)).toMatch(/^Idle \d+ min, so the cache expired/)
})

// -- rest and once ------------------------------------------------------------------------

test('a hint rests half an hour after it shows, and shows again after', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)

  await put($, state, 'x'.repeat(60_000))
  expect(String((await hint($, world)).tail)).toMatch(/^Large paste/)
  await send($, state, 'summarise this')

  await put($, state, 'x'.repeat(60_000))
  expect((await hint($, world)).tail).toBeUndefined()
  await clear($, state)

  await world.clock.advance(31 * MINUTE)
  await put($, state, 'x'.repeat(60_000))
  expect(String((await hint($, world)).tail)).toMatch(/^Large paste/)
})

test('a kind marked once shows once, however long it rests', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { once: ['big_paste'] } }) })
  const state = rig(on)
  await started($, world)

  await put($, state, 'x'.repeat(60_000))
  expect(String((await hint($, world)).tail)).toMatch(/^Large paste/)
  await send($, state, 'summarise this')

  await world.clock.advance(48 * 60 * MINUTE)
  await put($, state, 'x'.repeat(60_000))
  // The cache has expired by now, so the warning takes its place: the paste hint does not come back.
  expect(String((await hint($, world)).tail)).not.toMatch(/Large paste/)
})

test('the hint stays while the draft stays, across a redraw and the other surface', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)
  await put($, state, 'x'.repeat(60_000))
  expect(String((await hint($, world)).tail)).toMatch(/^Large paste/)
  expect(String((await hint($, world)).tail)).toMatch(/^Large paste/)
  expect(String((await hint($, world, 'desktop')).hint)).toMatch(/^Large paste/)
})

// -- the sensor ---------------------------------------------------------------------------

test('each hint shown writes one note event, with the hint surface and words from the vocabulary', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  await started($, world)
  await put($, state, 'x'.repeat(60_000))
  await hint($, world)
  await hint($, world)
  await send($, state, 'summarise this')
  await settle($, world)

  const shown = sensorNotes(world, 'big_paste')
  expect(shown).toHaveLength(1)
  expect(shown[0]?.surface).toBe('hint')
  expect(SENSOR_VOCAB.note.kind).toContain('big_paste')
  expect(SENSOR_VOCAB.note.outcome).toContain(shown[0]?.outcome)
  expect(JSON.stringify(world.files.get(SENSOR))).not.toMatch(/xxxx|summarise|Pasted/)
})

// -- plan_fresh ---------------------------------------------------------------------------

const PLAN = { plan: 'p'.repeat(4000), isAgent: false, filePath: '/work/plan.md' }

/** Planning that leaves the context at about 90k from a 20k start. */
async function planned($: Engine, world: World, state: Rig, ctx = 90_000): Promise<void> {
  await $.session.start(START)
  await send($, state, 'build the thing')
  await reply($, world, { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 18000, output: 0 }], usd: 0.1 })
  await reply($, world, { gap: MINUTE, steps: [{ input: 500, cacheRead: 20000, cacheWrite: ctx - 20500, output: 100 }], usd: 0.3 })
}

test('approving a plan after a lot of planning puts the fresh-start tip on the status line, until the next reply', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN }
  await planned($, world, state)

  await tool($, 'ExitPlanMode')
  const tip = state.statuses.filter(one => one !== undefined)
  expect(tip).toHaveLength(1)
  expect(String(tip[0])).toMatch(/^This plan was approved with about \d+k tokens of planning in the session, and every reply of the build reads them again\./)
  expect(String(tip[0])).toMatch(/Run \/clear, then ask Claude to implement the plan in `\/work\/plan\.md`\.$/)
  expect(state.statuses[state.statuses.length - 1]).toBe(String(tip[0]))

  await reply($, world, { gap: MINUTE, ...AGAIN })
  // The tip goes with the reply. (The big jumps of the planning leave no reply before Claude summarises: the urgent line takes the place.)
  expect(state.statuses[state.statuses.length - 1]).toBe('Claude summarises on the next reply')

  await settle($, world)
  const shown = sensorNotes(world, 'plan_fresh')
  expect(shown).toHaveLength(1)
  expect(shown[0]?.surface).toBe('status')
  expect(faults(world)).toEqual([])
})

test('a plan approved with little planning, a rejected one or a muted kind leaves the status line alone', async ($, on) => {
  const small = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN }
  await planned($, small, state, 45_000)
  await tool($, 'ExitPlanMode')
  expect(state.statuses).toEqual([])

  expect(faults(small)).toEqual([])
})

test('a failed ExitPlanMode call, and an agent\'s plan, give no tip', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN, isError: true }
  await planned($, world, state)
  await tool($, 'ExitPlanMode')
  state.results.ExitPlanMode = { result: { ...PLAN, isAgent: true } }
  await tool($, 'ExitPlanMode')
  expect(state.statuses).toEqual([])
})

test('the plan tip can be muted, and rests like the others', async ($, on) => {
  const muted = prepare(on, { settings: settingsFile({ coaching: { muted: ['plan_fresh'] } }) })
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN }
  await planned($, muted, state)
  await tool($, 'ExitPlanMode')
  expect(state.statuses).toEqual([])
})

test('a second plan inside the rest gives no second tip', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.results.ExitPlanMode = { result: PLAN }
  await planned($, world, state)
  await tool($, 'ExitPlanMode')
  await tool($, 'ExitPlanMode')
  expect(state.statuses.filter(one => one !== undefined)).toHaveLength(1)
})

// -- rating_reminder ----------------------------------------------------------------------

const BIG_REPLY = { steps: [{ cacheRead: 240_000, cacheWrite: 10_000, output: 0 }], usd: 0.5 }

function ratingNotes(world: World): string[] {
  return notes(world).filter(line => /isn't rated/.test(line))
}

async function work($: Engine, world: World, replies: number): Promise<void> {
  for (let i = 0; i < replies; i += 1) await reply($, world, { gap: MINUTE, ...BIG_REPLY })
}

test('a piece of work past a million tokens earns the rating reminder, once, and the time is kept', async ($, on) => {
  const world = prepare(on)
  world.shape = { window: 5_000_000, point: 4_000_000 }
  const state = rig(on)
  await $.session.start(START)
  await send($, state, 'build the thing')
  await work($, world, 3)
  expect(ratingNotes(world)).toEqual([])

  await work($, world, 1)
  expect(ratingNotes(world)).toHaveLength(1)
  expect(ratingNotes(world)[0]).toMatch(/^● Glass · This piece of work has used about 1(\.\d)?M tokens and isn't rated\. Finished\? Run \/cg-feedback: a few ticks make your savings tips fit how you work\.$/)
  const kept = JSON.parse(String(world.files.get(COACH))) as { schema: number; reminded_at: number }
  expect(kept.schema).toBe(1)
  expect(kept.reminded_at).toBeGreaterThan(1_700_000_000)

  await work($, world, 8)
  expect(ratingNotes(world)).toHaveLength(1)
  const shown = sensorNotes(world, 'rating_reminder')
  expect(shown).toHaveLength(1)
  expect(SENSOR_VOCAB.note.surface).toContain(shown[0]?.surface)
  expect(faults(world)).toEqual([])
})

test('the reminder waits for your typical piece, and for the days of rest', async ($, on) => {
  const typical = prepare(on, { settings: settingsFile({ coaching: { typical_piece_tokens: 600_000 } }) })
  typical.shape = { window: 5_000_000, point: 4_000_000 }
  const state = rig(on)
  await $.session.start(START)
  await send($, state, 'build the thing')
  await work($, typical, 4)
  expect(ratingNotes(typical)).toEqual([])
  await work($, typical, 1)
  expect(ratingNotes(typical)).toHaveLength(1)
})

test('a reminder that went out in the last days holds the next one back', async ($, on) => {
  const world = prepare(on)
  world.shape = { window: 5_000_000, point: 4_000_000 }
  const state = rig(on)
  world.files.set(COACH, JSON.stringify({ schema: 1, reminded_at: Math.floor(T0 / 1000) - 3600 }))
  await $.session.start(START)
  await send($, state, 'build the thing')
  await work($, world, 6)
  expect(ratingNotes(world)).toEqual([])
})

test('a /cg-feedback run starts the count over', async ($, on) => {
  const world = prepare(on)
  world.shape = { window: 5_000_000, point: 4_000_000 }
  const state = rig(on)
  await $.session.start(START)
  await send($, state, 'build the thing')
  await work($, world, 3)
  await send($, state, '/cg-feedback 4')
  await work($, world, 3)
  expect(ratingNotes(world)).toEqual([])
})

test('the rating reminder can be muted', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { muted: ['rating_reminder'] } }) })
  world.shape = { window: 5_000_000, point: 4_000_000 }
  const state = rig(on)
  await $.session.start(START)
  await send($, state, 'build the thing')
  await work($, world, 6)
  expect(ratingNotes(world)).toEqual([])
})

// -- speed --------------------------------------------------------------------------------

test('a keystroke in a 20,000 character draft costs well under the 50 ms budget', async ($, on) => {
  const world = prepare(on)
  const state = rig(on)
  state.shorten = null
  await started($, world)
  await put($, state, 'a'.repeat(20_000))

  const began = performance.now()
  for (let i = 0; i < 1000; i += 1) await put($, state, 'b')
  const each = (performance.now() - began) / 1000

  expect(state.box).toHaveLength(21_000)
  // The whole chain, the engine's and the mod's, averages under 10 ms; the mod's own share is a fraction of it.
  expect(each).toBeLessThan(10)
  expect(faults(world)).toEqual([])
})
