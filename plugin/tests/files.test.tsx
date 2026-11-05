import { expect, test } from 'claude-code/testing'

import { SENSOR_VOCAB } from '../hooks/generated/coaching.js'
import { VERSION } from '../hooks/generated/version.js'
import { emptySnap } from '../hooks/live'
import { addEvent, MAX_EVENTS, sensorFile } from '../hooks/sensor'
import { faults, prepare, reply, SDK_START, SESSION, START, T0 } from './support'
import type { World } from './support'

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

// -- The surface arrives after the session starts ----------------------------------------------------------------------

const NOW = Math.floor(T0 / 1000)

test('a session that starts with no surface learns it when the client attaches, and says it is present once', async ($, on) => {
  const world = prepare(on)
  world.surfaces = []
  await $.session.start(SDK_START)
  // Nothing to say yet: no surface, and no file.
  expect(world.files.has(SENSOR)).toBe(false)

  // The Desktop client attaches after the session has started, before the first reply.
  world.surfaces = ['desktop']
  await reply($, world, FIRST)
  await reply($, world, { ...COLD, gap: 400 * 1000 })
  expect(events(world).filter(one => one.event === 'present')).toEqual([{ ts: NOW, event: 'present', surface: 'desktop' }])
  expect(faults(world)).toEqual([])
})

test('a headless session, whose surfaces stay empty, never says it is present, and is asked once a reply', async ($, on) => {
  const world = prepare(on)
  world.surfaces = []
  await $.session.start(SDK_START)
  const atStart = world.surfaceCalls

  await reply($, world, FIRST)
  await reply($, world, { ...COLD, gap: 400 * 1000 })
  await reply($, world, { ...COLD, gap: 400 * 1000 })
  expect(events(world).some(one => one.event === 'present')).toBe(false)
  // Once before each of the three replies, not at every hook of them.
  expect(world.surfaceCalls - atStart).toBe(3)
  expect(faults(world)).toEqual([])
})

test('a surface the session starts with is announced at the start and asked for no more', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  const atStart = world.surfaceCalls
  await reply($, world, FIRST)
  await reply($, world, { ...COLD, gap: 400 * 1000 })
  expect(events(world).filter(one => one.event === 'present')).toEqual([{ ts: NOW, event: 'present', surface: 'terminal' }])
  expect(world.surfaceCalls).toBe(atStart)
})

// -- A new process for the same session --------------------------------------------------------------------------------

/** The sensor file an earlier process of the session left. */
function seeded(world: World, events: object[], over: Record<string, unknown> = {}): void {
  world.files.set(SENSOR, JSON.stringify({ schema: 1, session_id: SESSION, version: '0.0.1', events, ...over }))
}

const EARLIER = [
  { ts: NOW - 7200, event: 'present', surface: 'desktop' },
  { ts: NOW - 3600, event: 'compaction', kind: 'auto', kept: 3, missing: 1, added: 0, instructed: 1 },
  { ts: NOW - 3000, event: 'tweak', kind: 'readd', before: 1, after: 1 },
]

test('a new process for the same session adds to its sensor file instead of replacing it', async ($, on) => {
  const world = prepare(on)
  seeded(world, EARLIER)
  await $.session.start(START)
  await reply($, world, FIRST)
  await reply($, world, { ...COLD, gap: 400 * 1000 })

  const seen = events(world)
  // What the earlier process wrote, then this one's: its own present and the cold send, all in time order.
  expect(seen.slice(0, 3)).toEqual(EARLIER)
  expect(seen[3]).toEqual({ ts: NOW, event: 'present', surface: 'terminal' })
  expect(seen.some(one => one.event === 'cold_send')).toBe(true)
  expect(seen.map(one => one.ts)).toEqual([...seen.map(one => one.ts)].sort((a, b) => a - b))
  // The file keeps the shape the Python side reads.
  expect(Object.keys(json(world, SENSOR)).sort()).toEqual(['events', 'schema', 'session_id', 'version'])
  expect(json(world, SENSOR).version).toBe(VERSION)
  expect(faults(world)).toEqual([])
})

test('a process that starts and ends with no reply keeps the earlier events and adds its present', async ($, on) => {
  const world = prepare(on)
  seeded(world, EARLIER)
  await $.session.start(START)
  await $.session.end({ sessionId: SESSION, reason: 'exit' } as never)
  expect(events(world)).toEqual([...EARLIER, { ts: NOW, event: 'present', surface: 'terminal' }])
  expect(faults(world)).toEqual([])
})

test('later writes of a process keep what it holds and do not read the file again', async ($, on) => {
  const world = prepare(on)
  seeded(world, EARLIER)
  await $.session.start(START)
  await reply($, world, FIRST)
  expect(events(world).slice(0, 3)).toEqual(EARLIER)

  // Something else rewrites the file. The process writes its own history whole at its next change.
  seeded(world, [{ ts: NOW - 10, event: 'present', surface: 'mobile' }])
  await reply($, world, { ...COLD, gap: 400 * 1000 })
  const seen = events(world)
  expect(seen.slice(0, 3)).toEqual(EARLIER)
  expect(seen.some(one => one.surface === 'mobile')).toBe(false)
})

test('events the file holds from this very process (a reload) are not added twice', async ($, on) => {
  const world = prepare(on)
  // The file already has the present this process is about to announce, at the same second.
  seeded(world, [{ ts: NOW - 60, event: 'cold_send', outcome: 'warned' }, { ts: NOW, event: 'present', surface: 'terminal' }])
  await $.session.start(START)
  await $.session.end({ sessionId: SESSION, reason: 'exit' } as never)
  expect(events(world)).toEqual([{ ts: NOW - 60, event: 'cold_send', outcome: 'warned' }, { ts: NOW, event: 'present', surface: 'terminal' }])
})

test('the file keeps the newest events when the earlier ones and this process hold more than the limit', async ($, on) => {
  const world = prepare(on)
  const old = Array.from({ length: MAX_EVENTS }, (_, i) => ({ ts: NOW - 5000 + i, event: 'cold_send', outcome: 'warned' }))
  seeded(world, old)
  await $.session.start(START)
  await $.session.end({ sessionId: SESSION, reason: 'exit' } as never)
  const seen = events(world)
  expect(seen).toHaveLength(MAX_EVENTS)
  // The oldest row is the one dropped, and the file starts at the oldest it keeps.
  expect(seen[0]).toEqual(old[1])
  expect(seen[seen.length - 1]).toEqual({ ts: NOW, event: 'present', surface: 'terminal' })
})

const NOT_USABLE: [string, string][] = [
  ['text that is not JSON', 'not json'],
  ['nothing at all', ''],
  ['another schema', JSON.stringify({ schema: 2, session_id: SESSION, events: EARLIER })],
  ['another session', JSON.stringify({ schema: 1, session_id: 'session-two', events: EARLIER })],
  ['no list of events', JSON.stringify({ schema: 1, session_id: SESSION, events: 'x' })],
  ['a JSON value that is not a file', '42'],
]

for (const [what, text] of NOT_USABLE) {
  test(`a sensor file that is ${what} is left out, and the new events are written`, async ($, on) => {
    const world = prepare(on)
    world.files.set(SENSOR, text)
    await $.session.start(START)
    await reply($, world, FIRST)
    expect(events(world)).toEqual([{ ts: NOW, event: 'present', surface: 'terminal' }])
    expect(faults(world)).toEqual([])
  })
}

test('events in the file that are not events are dropped, the rest are kept', async ($, on) => {
  const world = prepare(on)
  seeded(world, [null, 7, 'x', { ts: 'soon', event: 'present' }, { ts: NOW - 5.5, event: 'present' }, { event: 'present' }, EARLIER[1]])
  await $.session.start(START)
  await $.session.end({ sessionId: SESSION, reason: 'exit' } as never)
  expect(events(world)).toEqual([EARLIER[1], { ts: NOW, event: 'present', surface: 'terminal' }])
})
