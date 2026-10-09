import { expect, test } from 'claude-code/testing'

import { faults, notes, prepare, reply, settingsFile, START } from './support'
import type { Step, World } from './support'

const MINUTE = 60 * 1000

/**
 * One reply of two requests: Claude asks for a Bash call at `ctx`, and comes
 * back with the result, which added `grew` tokens to the context.
 */
function bashReply(ctx: number, grew: number, first: boolean): Step[] {
  const ask: Step = first ? { input: 500, cacheRead: 0, cacheWrite: ctx - 500, output: 300, tools: ['Bash'] } : { input: 500, cacheRead: ctx - 500, cacheWrite: 0, output: 300, tools: ['Bash'] }
  const back: Step = { input: 500, cacheRead: ctx - 500, cacheWrite: grew + 500, output: 300 }
  return [ask, back]
}

/** The (1-based) replies after which a note was written. */
async function run($: Parameters<typeof reply>[0], world: World, count: number, gap: number, grew: number, from: number): Promise<number[]> {
  await $.session.start(START)
  const at: number[] = []
  let seen = 0
  for (let i = 0; i < count; i += 1) {
    const ctx = from + i * grew
    await reply($, world, { gap: i === 0 ? 0 : gap, steps: bashReply(ctx, grew, i === 0), usd: 0.2 })
    if (notes(world).length > seen) {
      seen = notes(world).length
      at.push(i + 1)
    }
  }
  return at
}

test('a big tool result earns a note, then none for the next five replies, whatever happens', async ($, on) => {
  const world = prepare(on)
  // Forty minutes between replies, so the rest rule never holds a note back: only the cap does.
  const at = await run($, world, 12, 40 * MINUTE, 9000, 20000)
  expect(at.length).toBeGreaterThanOrEqual(2)
  expect(at.length).toBeLessThanOrEqual(Math.ceil(12 / 5))
  for (let i = 1; i < at.length; i += 1) expect((at[i] ?? 0) - (at[i - 1] ?? 0)).toBeGreaterThanOrEqual(5)
  expect(notes(world)[0]).toMatch(/^● Glass · That Bash output was about \d+(\.\d)?k, and every later reply re-reads it\./)
  expect(faults(world)).toEqual([])
})

test('the rest rule holds the same note back for half an hour', async ($, on) => {
  const world = prepare(on)
  const at = await run($, world, 8, MINUTE, 9000, 20000)
  expect(at).toEqual([1])
  expect(faults(world)).toEqual([])
})

test('a context close to the summary point earns the replies-left note', async ($, on) => {
  // Eight replies at this size also add up to a piece of work big enough to ask for a rating: muted, to see the other note.
  const world = prepare(on, { settings: settingsFile({ coaching: { muted: ['rating_reminder'] } }) })
  const at = await run($, world, 8, MINUTE, 6000, 100000)
  expect(at.length).toBeGreaterThanOrEqual(1)
  expect(notes(world)[0]).toMatch(/^● Glass · About \d+ repl(?:y|ies) before Claude summarises\. If this task is done, \/compact or \/clear now\.$/)
  expect(faults(world)).toEqual([])
})

test('with no room for another reply the note says Claude summarises after the next one, not "about 1 reply"', async ($, on) => {
  // The big-result note would come first, at the first reply.
  const world = prepare(on, { settings: settingsFile({ coaching: { muted: ['rating_reminder', 'result'] } }) })
  const at = await run($, world, 3, MINUTE, 9000, 140000)
  expect(at).toEqual([3])
  expect(notes(world)[0]).toBe('● Glass · Claude summarises after the next reply. If this task is done, /compact or /clear now.')
  expect(faults(world)).toEqual([])
})
