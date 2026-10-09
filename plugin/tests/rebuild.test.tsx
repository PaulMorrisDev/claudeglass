import { expect, test } from 'claude-code/testing'

import { faults, prepare, reply, START } from './support'

const MESSAGE = { role: 'user', text: 'The summary.', toolUses: [] } as const
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }

function rebuildLines(logs: string[]): string[] {
  return logs.filter(line => line.startsWith('Cache rebuild'))
}

test('an unexpected rebuild of a big context writes a line with its cost and likely cause', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  // The cache lives five minutes; this reply comes after ten and writes it all again.
  await reply($, world, { gap: 600 * 1000, steps: [{ input: 1000, cacheRead: 0, cacheWrite: 120000, output: 200 }], usd: 0.45 })
  const lines = rebuildLines(world.logs)
  expect(lines).toHaveLength(1)
  expect(lines[0]).toMatch(/^Cache rebuild: about 120k written again, about \$0\.\d\d\. Likely cause: .+\.$/)
  expect(faults(world)).toEqual([])
})

test('a reply that read the cache as it should writes no line', async ($, on) => {
  const world = prepare(on)
  await $.session.start(START)
  await reply($, world, FIRST)
  await reply($, world, { gap: 30 * 1000, steps: [{ input: 500, cacheRead: 118000, cacheWrite: 2000, output: 200 }], usd: 0.05 })
  expect(rebuildLines(world.logs)).toEqual([])
})

test('the rebuild after a summary is expected: a line only in the detailed view', async ($, on) => {
  const world = prepare(on)
  on('session.compact', () => ({ messages: [MESSAGE], tokensBefore: 120000, tokensAfter: 32000 }) as never)
  await $.session.start(START)
  await reply($, world, FIRST)

  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] })
  await reply($, world, { gap: 20 * 1000, steps: [{ input: 500, cacheRead: 0, cacheWrite: 31500, output: 200 }], usd: 0.12 })
  expect(rebuildLines(world.logs)).toEqual([])

  expect((await $.command.run({ command: 'glass', args: 'detail' } as never)).text).toBe('Detailed view is on.')
  await $.session.compact({ trigger: 'manual', messages: [MESSAGE] })
  await reply($, world, { gap: 20 * 1000, steps: [{ input: 500, cacheRead: 0, cacheWrite: 31500, output: 200 }], usd: 0.12 })
  const lines = rebuildLines(world.logs)
  expect(lines).toHaveLength(1)
  expect(lines[0]).toMatch(/^Cache rebuild after .+, as expected: about \$0\.\d\d\.$/)
  expect(faults(world)).toEqual([])
})
