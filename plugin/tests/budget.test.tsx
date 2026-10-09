import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'

import { elements, faults, notes, prepare, reply, SESSION, settingsFile, SITE, START, textOf } from './support'
import type { World } from './support'

const PROPS = { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 200, ...SITE }
const SENSOR = `/cfg/claudeglass/mod/sensor/${SESSION}.json`

// One reply that leaves the context at 120k, $0.50 spent; and one that adds $0.25.
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
const AGAIN = { steps: [{ input: 500, cacheRead: 120000, cacheWrite: 500, output: 100 }], usd: 0.25 }
const SUBSCRIPTION = settingsFile({ billing: { mode: 'subscription', share_per_usd: null, period_label: 'weekly usage limit', basis: 'list' } })

async function started($: Engine, world: World): Promise<void> {
  await $.session.start(START)
  await reply($, world, FIRST)
}

/** The texts of the band's segments (its separators left out). */
async function segments($: Engine, surface: 'terminal' | 'desktop' = 'terminal', props: Record<string, unknown> = {}): Promise<string[]> {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface, component: 'AbovePrompt', props: { ...PROPS, ...props } as never })
  const texts = elements(await mounted.drawn())
    .filter(one => one.type === 'Text')
    .map(textOf)
    .filter(text => text !== '│' && text !== '')
  await mounted.unmount()
  return texts
}

async function budget($: Engine, args: string): Promise<string> {
  return String((await $.command.run({ command: 'glass', args: `budget ${args}`.trim() } as never)).text)
}

test('on API billing a budget shows as progress in the band, and /glass budget says where it stands', async ($, on) => {
  const world = prepare(on)
  await started($, world)

  expect(await budget($, '')).toMatch(/^No session budget is set\./)
  expect(await budget($, '$2')).toBe('Session budget set: $2.00.')
  expect((await segments($))[1]).toBe('$0.50 of $2.00 budget (+$0.50)')
  expect((await segments($, 'terminal', { bodyColumns: 90 }))[1]).toBe('$0.50 of $2.00 (+$0.50)')
  expect(await budget($, '')).toBe('Session budget: $2.00. This session has used $0.50.')

  // Off puts the ordinary spend back.
  expect(await budget($, 'off')).toBe('Session budget cleared.')
  expect((await segments($))[1]).toBe('$0.50 this session (+$0.50)')
  expect(faults(world)).toEqual([])
})

test('the budget in the Detailed band keeps the figures it had', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  await budget($, '2.5')
  await $.command.run({ command: 'glass', args: 'detail' } as never)
  const texts = await segments($)
  expect(texts[1]).toBe('$0.50 of $2.50 budget (+$0.50)')
})

test('an amount that does not fit the billing mode is turned back', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  for (const bad of ['5%', 'lots', '-3', '0', '1e3']) expect(await budget($, bad)).toBe("That isn't an amount. Try /glass budget 2 or /glass budget 2.50, or /glass budget off.")
  expect((await segments($))[1]).toBe('$0.50 this session (+$0.50)')
})

test('past the budget the band says so, and one note at the end of the reply says it once', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  await budget($, '0.6')
  expect(notes(world)).toEqual([])

  await reply($, world, { gap: 60000, ...AGAIN })
  expect((await segments($))[1]).toMatch(/^\$0\.75 of \$0\.60 budget, over/)
  expect(notes(world)).toEqual(['● Glass · This session is past its $0.60 budget and has used $0.75. Wrap up, or raise the budget with /glass budget.'])

  // The next reply doesn't repeat it.
  await reply($, world, { gap: 60000, ...AGAIN })
  expect(notes(world)).toHaveLength(1)

  // Raising the budget and passing it again earns a new one.
  await budget($, '1')
  await reply($, world, { gap: 60000, ...AGAIN })
  expect(notes(world)).toHaveLength(2)

  const file = JSON.parse(String(world.files.get(SENSOR))) as { events: { event: string; kind?: string; surface?: string }[] }
  expect(file.events.filter(one => one.event === 'note' && one.kind === 'budget').length).toBeGreaterThanOrEqual(1)
  expect(faults(world)).toEqual([])
})

test('a budget under the spend never writes a note while it is off', async ($, on) => {
  const world = prepare(on)
  await started($, world)
  await budget($, '0.1')
  await budget($, 'off')
  await reply($, world, { gap: 60000, ...AGAIN })
  expect(notes(world)).toEqual([])
})

test('on a subscription the budget is a share of the 5-hour limit', async ($, on) => {
  const world = prepare(on, { settings: SUBSCRIPTION })
  const resetsAt = new Date(world.clock.now() + 4 * 60 * 60 * 1000).toISOString()
  world.limits = [{ kind: 'five_hour', percentUsed: 10, resetsAt }]
  await started($, world)

  // Dollars are not what a subscription budget is written in.
  expect(await budget($, '$2')).toBe("That isn't an amount. Use a share of the 5-hour limit, such as /glass budget 5%, or /glass budget off.")
  expect(await budget($, '150%')).toMatch(/^That isn't an amount\. Use a share/)
  expect(await budget($, '20%')).toBe('Session budget set: 20%.')

  // Until the limit has moved there is no share to measure against: the band stays as it was.
  expect((await segments($))[1]).toBe('5-hour limit 10% used')

  world.limits = [{ kind: 'five_hour', percentUsed: 15, resetsAt }]
  await reply($, world, { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.5 })
  const texts = await segments($)
  expect(texts[1]).toMatch(/^10\.0% of 20% budget$/)
  expect(await budget($, '')).toBe('Session budget: 20%. This session has used 10.0%.')

  expect(await budget($, '5')).toBe('Session budget set: 5%.')
  expect((await segments($))[1]).toMatch(/^10\.0% of 5% budget, over$/)
  expect(faults(world)).toEqual([])
})
