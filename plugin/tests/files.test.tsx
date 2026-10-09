import { expect, test } from 'claude-code/testing'

import { SENSOR_VOCAB } from '../hooks/generated/coaching.js'
import { VERSION } from '../hooks/generated/version.js'
import { faults, prepare, reply, SESSION, START, T0 } from './support'
import type { Step, World } from './support'

const MINUTE = 60 * 1000
const PRESENT = '/cfg/claudeglass/mod/present.json'
const SENSOR = `/cfg/claudeglass/mod/sensor/${SESSION}.json`
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
const COLD = { steps: [{ input: 1000, cacheRead: 0, cacheWrite: 120000, output: 200 }], usd: 0.45 }

type Dollar = Parameters<typeof reply>[0]
type Event = { ts: number; event: string; kind?: string; surface?: string; outcome?: string }

function json(world: World, path: string): { [key: string]: unknown } {
  const text = world.files.get(path)
  expect(text).toBeDefined()
  return JSON.parse(String(text)) as { [key: string]: unknown }
}

function events(world: World): Event[] {
  return json(world, SENSOR).events as Event[]
}

/** One reply of two requests: Claude asks for a Bash call at `ctx`, and the result grows the context by `grew`. */
function bashReply(ctx: number, grew: number, first: boolean): Step[] {
  const ask: Step = first ? { input: 500, cacheRead: 0, cacheWrite: ctx - 500, output: 300, tools: ['Bash'] } : { input: 500, cacheRead: ctx - 500, cacheWrite: 0, output: 300, tools: ['Bash'] }
  return [ask, { input: 500, cacheRead: ctx - 500, cacheWrite: grew + 500, output: 300 }]
}

async function hint($: Dollar): Promise<void> {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'PromptHint', props: { isDraft: true, isWorking: false, hint: 'Enter to send' } as never })
  await mounted.unmount()
}

test('present.json: written when a session starts, then at most once a minute, with old entries dropped', async ($, on) => {
  const world = prepare(on)
  const now = Math.floor(T0 / 1000)
  world.files.set(PRESENT, JSON.stringify({ schema: 1, sessions: { 'old-one': now - 25 * 3600, 'recent-one': now - 3600 } }))
  await $.session.start(START)

  expect(json(world, PRESENT)).toEqual({ schema: 1, sessions: { 'recent-one': now - 3600, [SESSION]: now } })

  // Within a minute of the last write, a measurement leaves the file alone.
  world.files.set(PRESENT, 'kept')
  await reply($, world, FIRST)
  expect(world.files.get(PRESENT)).toBe('kept')

  // After it, the next measurement writes it again, from whatever was there.
  await world.clock.advance(61 * 1000)
  await reply($, world, { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
  const beat = json(world, PRESENT)
  expect(beat.schema).toBe(1)
  expect(Object.keys(beat.sessions as object)).toEqual([SESSION])
  expect((beat.sessions as { [id: string]: number })[SESSION]).toBeGreaterThan(now)
  expect(faults(world)).toEqual([])
})

test('the sensor file holds closed words and times only, in the shape the Python side reads', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)

  // Cache expired and the hint said so: the next send is a warned cold send, and it rebuilds the cache.
  await world.clock.advance(400 * 1000)
  await hint($)
  await reply($, world, COLD)
  // Expired again with no hint drawn: an unwarned one.
  await world.clock.advance(400 * 1000)
  await reply($, world, COLD)

  const file = json(world, SENSOR)
  expect(Object.keys(file).sort()).toEqual(['events', 'schema', 'session_id', 'version'])
  expect(file.schema).toBe(1)
  expect(file.session_id).toBe(SESSION)
  expect(file.version).toBe(VERSION)

  const seen = events(world)
  expect(seen[0]).toEqual({ ts: Math.floor(T0 / 1000), event: 'present', surface: 'terminal' })
  expect(seen.filter(one => one.event === 'cold_send').map(one => one.outcome)).toEqual(['warned', 'unwarned'])
  const rebuilds = seen.filter(one => one.event === 'rebuild')
  expect(rebuilds.length).toBeGreaterThanOrEqual(1)

  // Nothing outside the vocabulary: event names, and each word from its list.
  const vocab = SENSOR_VOCAB as unknown as { [event: string]: { [field: string]: string[] } }
  for (const one of seen) {
    expect(Object.keys(vocab)).toContain(one.event)
    expect(Number.isInteger(one.ts)).toBe(true)
    for (const [field, value] of Object.entries(one)) {
      if (field === 'ts' || field === 'event') continue
      expect(vocab[one.event]?.[field]).toContain(value)
    }
  }
  expect(JSON.stringify(file)).not.toMatch(/\/work|\$|answer|done/)
  expect(faults(world)).toEqual([])
})

test('a note the mod showed is settled by what followed it', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  for (let i = 0; i < 4; i += 1) {
    await reply($, world, { gap: i === 0 ? 0 : 40 * MINUTE, steps: bashReply(20000 + i * 40000, 40000, i === 0), usd: 0.2 })
  }
  const settled = events(world).filter(one => one.event === 'note')
  expect(settled.length).toBeGreaterThanOrEqual(1)
  const vocab = SENSOR_VOCAB.note
  for (const one of settled) {
    expect(vocab.kind).toContain(one.kind)
    expect(vocab.surface).toContain(one.surface)
    expect(vocab.outcome).toContain(one.outcome)
  }
  expect(faults(world)).toEqual([])
})

test('a session id that is not a plain word writes no sensor file', async ($, on) => {
  const world = prepare(on, { session: '../escape' })
  await $.session.start(START)
  await reply($, world, COLD)
  expect([...world.files.keys()].filter(path => path.includes('/sensor/'))).toEqual([])
})
