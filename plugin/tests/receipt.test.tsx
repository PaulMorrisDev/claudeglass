import { expect, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { emptyReview } from '../hooks/compaction-memory'
import { itemId } from '../hooks/lib/keeplist.js'
import type { GlassKeepItem, GlassReceipt, GlassReview } from '../types'
import { faults, prepare, reply, SESSION, START } from './support'

const REPLY = { steps: [{ cacheRead: 0, cacheWrite: 20000, output: 100 }], usd: 0.1 }

function item(kind: GlassKeepItem['kind'], text: string, exact: string[] = []): GlassKeepItem {
  return { id: itemId(kind, text), kind, text, safe: false, why: '', exact, at: 0 }
}

const PORT = item('instruction', 'Never use port 8765 for the dev server, it is reserved', ['8765'])
const FILE = item('file', 'src/api/limiter.ts', ['src/api/limiter.ts', 'limiter.ts'])

/** The review state as the host keeps it, seeded with items put back and their receipts; reads back what the mod wrote. */
function keepReview(on: On, items: GlassKeepItem[]): { readonly latest: GlassReview } {
  let held: GlassReview = { ...emptyReview(), readded: items, receipts: Object.fromEntries(items.map(one => [one.id, { at: 1000, replies: 0, referenced: null } satisfies GlassReceipt])) }
  on('state.set', async (_$, e, next) => {
    if (e.plugin === 'claudeglass' && e.key === 'review') {
      held = e.value as GlassReview
    }
    return next(e)
  })
  on('state.get', async (_$, e, next) => {
    const got = await next(e)
    if (e.plugin !== 'claudeglass' || e.key !== 'review') return got
    const stored = got.value as { version: number } | undefined
    return { value: { version: stored?.version ?? 0, value: held } } as never
  })
  return {
    get latest() {
      return held
    },
  }
}

test('a reply whose answer says a put-back item marks its receipt referenced', async ($, on) => {
  const world = prepare(on)
  const watch = keepReview(on, [PORT, FILE])
  await $.session.start(START)
  await reply($, world, { ...REPLY, steps: [{ ...REPLY.steps[0]!, answer: 'Starting the server on port 9000: port 8765 is reserved and must never be used.' }] })
  expect(watch.latest.receipts[PORT.id]).toMatchObject({ replies: 1, referenced: true })
  // The file was not named: it waits.
  expect(watch.latest.receipts[FILE.id]).toMatchObject({ replies: 1, referenced: null })
  expect(faults(world)).toEqual([])
})

test('a tool input that names the file counts, whichever step of the reply made it', async ($, on) => {
  const world = prepare(on)
  const watch = keepReview(on, [PORT, FILE])
  await $.session.start(START)
  const step = { cacheRead: 0, cacheWrite: 20000, output: 100 }
  await reply($, world, { usd: 0.1, steps: [{ ...step, answer: 'Reading the code.' }, { ...step, tools: ['Edit'], inputs: { Edit: { file_path: 'src/api/limiter.ts' } } }] })
  expect(watch.latest.receipts[FILE.id]).toMatchObject({ replies: 1, referenced: true })
  expect(watch.latest.receipts[PORT.id]).toMatchObject({ replies: 1, referenced: null })
  expect(faults(world)).toEqual([])
})

test('a tool input is read as the strings it holds, so a Windows path or a command with line breaks still counts', async ($, on) => {
  const world = prepare(on)
  const watch = keepReview(on, [PORT, FILE])
  await $.session.start(START)
  const step = { cacheRead: 0, cacheWrite: 20000, output: 100 }
  await reply($, world, {
    usd: 0.1,
    steps: [{ ...step, tools: ['Bash'], inputs: { Bash: { command: 'cd C:\\Dev\\app\nnever use port 8765 here\nnpm start', description: 'Start the app' } } }, { ...step, tools: ['Read'], inputs: { Read: { file_path: 'C:\\Dev\\app\\src\\api\\limiter.ts' } } }],
  })
  expect(watch.latest.receipts[PORT.id]).toMatchObject({ replies: 1, referenced: true })
  expect(watch.latest.receipts[FILE.id]).toMatchObject({ replies: 1, referenced: true })
  expect(faults(world)).toEqual([])
})

test('every reply counts, what one said is not carried into the next, and the tenth without a match settles it', async ($, on) => {
  const world = prepare(on)
  const watch = keepReview(on, [PORT])
  await $.session.start(START)
  const step = { cacheRead: 0, cacheWrite: 20000, output: 100 }
  // Said in the first reply only: the second must not see it.
  await reply($, world, { usd: 0.1, steps: [{ ...step, answer: 'Renamed the helper.' }] })
  expect(watch.latest.receipts[PORT.id]).toMatchObject({ replies: 1, referenced: null })
  for (let count = 2; count < 10; count++) await reply($, world, { usd: 0.1, steps: [{ ...step, answer: 'Formatted the file.' }] })
  expect(watch.latest.receipts[PORT.id]).toMatchObject({ replies: 9, referenced: null })
  await reply($, world, REPLY)
  expect(watch.latest.receipts[PORT.id]).toEqual({ at: 1000, replies: 10, referenced: false })
  // Settled: a later reply that does say it changes nothing.
  await reply($, world, { usd: 0.1, steps: [{ ...step, answer: 'Never use port 8765, it is reserved.' }] })
  expect(watch.latest.receipts[PORT.id]).toEqual({ at: 1000, replies: 10, referenced: false })
  expect(faults(world)).toEqual([])
})

test('a subagent\'s steps are not what the reply said, and the texts never reach the state or the sensor', async ($, on) => {
  const world = prepare(on)
  const watch = keepReview(on, [PORT])
  await $.session.start(START)
  // A subagent's request, in the middle of the main reply, that says the item.
  world.turns += 1
  const turnId = `turn-${world.turns}`
  await $.turn.start({ text: 'go', turnId })
  world.next = { usage: { cacheRead: 0, cacheWrite: 20000, output: 100, answer: 'Never use port 8765, it is reserved.' }, tools: [] }
  const stream = $.turn.step({ turnId, index: 0, model: 'claude-sonnet-4-6', messageCount: 1, agentId: 'agent-1' } as never)
  for await (const chunk of stream) void chunk
  await stream.result
  world.next = { usage: { cacheRead: 0, cacheWrite: 20000, output: 100, answer: 'Done.' }, tools: [] }
  const main = $.turn.step({ turnId, index: 1, model: 'claude-sonnet-4-6', messageCount: 2 })
  for await (const chunk of main) void chunk
  await main.result
  await $.turn.complete({ turnId, reason: 'answer', answer: 'Done.', isAborted: false, durationMs: 1000 })
  expect(watch.latest.receipts[PORT.id]).toMatchObject({ replies: 1, referenced: null })
  expect(JSON.stringify(watch.latest)).not.toMatch(/reserved\./)
  expect([...world.files].filter(([path]) => path.includes(SESSION)).map(([, text]) => text).join('')).not.toMatch(/must never|reserved\./)
  expect(faults(world)).toEqual([])
})
