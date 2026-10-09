import { expect, test } from 'claude-code/testing'

import { SENSOR_VOCAB } from '../hooks/generated/coaching.js'
import { VERSION } from '../hooks/generated/version.js'
import { emptySnap } from '../hooks/live'
import { addEvent, MAX_EVENTS, sensorFile } from '../hooks/sensor'
import { faults, prepare, reply, SESSION, START, T0 } from './support'
import type { Step, World } from './support'

const MINUTE = 60 * 1000
const PRESENT = `/cfg/claudeglass/mod/present/${SESSION}.json`
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

test('the heartbeat is one file per session, written at the start, every minute, and zeroed at the end', async ($, on) => {
  const world = prepare(on)
  const now = Math.floor(T0 / 1000)
  await $.session.start(START)

  // The shape the capture hook reads: schema, the session's own id, the seconds it was seen, and when its last request was (none yet).
  expect(json(world, PRESENT)).toEqual({ schema: 1, session_id: SESSION, seen: now, active: 0 })
  expect([...world.files.keys()].filter(path => path.includes('/present'))).toEqual([PRESENT])

  // A measurement within a minute of the last write leaves the file alone.
  world.files.set(PRESENT, 'kept')
  await reply($, world, FIRST)
  expect(world.files.get(PRESENT)).toBe('kept')

  // After a minute the timer writes it again, with nothing else going on.
  await world.clock.advance(61 * 1000)
  const first = json(world, PRESENT)
  expect(first.schema).toBe(1)
  expect(first.session_id).toBe(SESSION)
  expect(Object.keys(first).sort()).toEqual(['active', 'schema', 'seen', 'session_id'])
  // The first reply's request was at the start, and the file says so.
  expect(first.active).toBe(now)
  expect(first.seen).toBeGreaterThan(now)

  // And again a minute on, for as long as the session is open.
  await world.clock.advance(60 * 1000)
  expect(json(world, PRESENT).seen).toBeGreaterThan(first.seen as number)

  // The engine can't remove a file: at the end it says the session is no longer seen, and the timer stops.
  await $.session.end({ sessionId: SESSION, reason: 'exit' } as never)
  expect(json(world, PRESENT)).toEqual({ schema: 1, session_id: SESSION, seen: 0, active: 0 })
  world.files.set(PRESENT, 'after the end')
  await world.clock.advance(5 * 60 * 1000)
  expect(world.files.get(PRESENT)).toBe('after the end')
  expect(faults(world)).toEqual([])
})

test('a session id the capture hook would not look for (an underscore) gets a sensor file but no heartbeat', async ($, on) => {
  const world = prepare(on, { session: 'with_underscore' })
  await $.session.start(START)
  await world.clock.advance(61 * 1000)
  await reply($, world, COLD)
  await $.session.end({ sessionId: 'with_underscore', reason: 'exit' } as never)
  expect([...world.files.keys()].filter(path => path.includes('/present'))).toEqual([])
  expect([...world.files.keys()].filter(path => path.includes('/sensor/'))).toEqual(['/cfg/claudeglass/mod/sensor/with_underscore.json'])
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
  await $.session.end({ sessionId: '../escape', reason: 'exit' } as never)
  expect([...world.files.keys()].filter(path => path.includes('/sensor/'))).toEqual([])
  expect([...world.files.keys()].filter(path => path.includes('/present'))).toEqual([])
})

test('the sensor file keeps the newest events by time, oldest first, whatever order they were added in', async () => {
  const snap = emptySnap()
  snap.sessionId = SESSION
  // A run of events, then a settled note that was shown long before them: it is the oldest row.
  for (let i = 0; i < MAX_EVENTS; i += 1) addEvent(snap, { ts: 1000 + i, event: 'cold_send', outcome: 'warned' })
  addEvent(snap, { ts: 10, event: 'note', kind: 'runway', surface: 'log', outcome: 'sent' })
  addEvent(snap, { ts: 5000, event: 'cold_send', outcome: 'unwarned' })

  // The old note is the row dropped, not the oldest of the run; the file starts at the oldest row kept.
  expect(snap.events).toHaveLength(MAX_EVENTS)
  expect(snap.events[0]?.ts).toBe(1001)
  expect(snap.events.some(one => one.ts === 10)).toBe(false)
  const file = JSON.parse(String(sensorFile(snap))) as { events: Event[] }
  expect(file.events).toHaveLength(MAX_EVENTS)
  expect(file.events.map(one => one.ts)).toEqual([...file.events.map(one => one.ts)].sort((a, b) => a - b))
  expect(file.events[file.events.length - 1]?.ts).toBe(5000)

  // Under the limit nothing is dropped, and a late note is written in its place in time.
  const few = emptySnap()
  few.sessionId = SESSION
  addEvent(few, { ts: 300, event: 'cold_send', outcome: 'warned' })
  addEvent(few, { ts: 100, event: 'note', kind: 'runway', surface: 'log', outcome: 'sent' })
  const written = JSON.parse(String(sensorFile(few))) as { events: Event[] }
  expect(written.events.map(one => one.ts)).toEqual([100, 300])
})
