import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { elements, faults, notes, prepare, reply, SESSION, settingsFile, SITE, START, textOf } from './support'
import type { World } from './support'

const MINUTE = 60 * 1000
const SENSOR = `/cfg/claudeglass/mod/sensor/${SESSION}.json`

// One reply that leaves the main context at 120k, and one that only reads it again.
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
// A short first reply: the session carries next to nothing yet (the startup is all of the context).
const SMALL = { steps: [{ input: 500, cacheRead: 0, cacheWrite: 7500, output: 0 }], usd: 0.05 }
const AGAIN = { steps: [{ input: 500, cacheRead: 120000, cacheWrite: 500, output: 100 }], usd: 0.1 }

// A message of the session, long enough that the session has a vocabulary to compare a new topic with.
const OPENING =
  'refactor the invoice rendering pipeline so the totals use decimal arithmetic and the rounding follows the accounting policy, then update the ledger export, the tax summary, the currency conversion and the customer statements to match'
const NEW_TOPIC = 'translate the onboarding wizard copy into german and french including tooltip strings'
const NO_PATH = 'add retry logic with exponential backoff to the invoice rendering pipeline when the ledger export fails'
const READ_ALL = 'read the whole codebase and explain how the invoice rendering and the ledger export fit together'
const FOLLOW_UP = 'now run the tests and fix the failing one'

type Rig = {
  /** What the prompt box holds. */
  box: string
  /** What reached the bottom of prompt.submit. */
  submitted: string[]
  /** What prompt.fill was asked to put in the box. */
  filled: string[]
  /** The questions asked, and the answer the next one gets (null: the dialog is dismissed). */
  asked: string[]
  answer: string | null
  /** What model.complete is asked, and what it answers. */
  completions: { system: string; prompt: string }[]
  rewrite: string | null
  /** The output tokens model.complete reports (the call's cap is the draft's: a count at the cap is a cut reply). */
  outputTokens: number
  closed: string[]
  /** How long a dismissed dialog takes to say so, in ms (the mocked clock). */
  dismissAfter: number
}

/** What sits under the mod's events: a prompt box that applies each edit and fill, the dialog, the model and the pane. */
function rig(on: On, world: World): Rig {
  const state: Rig = { box: '', submitted: [], filled: [], asked: [], answer: 'Send', completions: [], rewrite: null, outputTokens: 60, closed: [], dismissAfter: 0 }
  on('prompt.edit', (_$, e) => {
    const text = e.text.slice(0, e.start) + e.inputText + e.text.slice(e.end)
    return { text, cursor: e.start + e.inputText.length } as never
  })
  on('prompt.submit', (_$, e) => {
    state.submitted.push(e.text)
    return e as never
  })
  on('prompt.fill', (_$, e) => {
    state.filled.push(e.text)
    state.box = e.text
    return { isFilled: true, text: e.text } as never
  })
  on('prompt.read', () => ({ value: { text: state.box, cursor: state.box.length } }) as never)
  on('tool.call', async (_$, e) => {
    if (e.tool !== 'AskUserQuestion') return { result: {} } as never
    const questions = (e as unknown as { questions: { question: string }[] }).questions
    const question = questions[0]?.question ?? ''
    state.asked.push(question)
    if (state.dismissAfter > 0) await world.clock.advance(state.dismissAfter)
    if (state.answer === null) throw new Error('dismissed')
    return { result: { questions, answers: { [question]: state.answer } }, text: state.answer } as never
  })
  on('model.complete', (_$, e) => {
    state.completions.push({ system: String(e.system), prompt: String(e.prompt) })
    const usage = { input_tokens: 400, output_tokens: state.outputTokens, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }
    return { value: state.rewrite === null ? { isAnswered: false, reason: 'api-error', usage } : { isAnswered: true, text: state.rewrite, usage } } as never
  })
  on('ui.close', (_$, e) => {
    state.closed.push(e.id)
    return { value: undefined } as never
  })
  return state
}

type Edit = { origin: { kind: 'composer' }; text: string; cursor: number; start: number; end: number; inputText: string }
type Prompt = { edit: (e: Edit) => Promise<{ text: string }> }

/** Types or pastes `text` at the end of the box. */
async function put($: Engine, state: Rig, text: string): Promise<void> {
  const answer = await ($.prompt as unknown as Prompt).edit({ origin: { kind: 'composer' }, text: state.box, cursor: state.box.length, start: state.box.length, end: state.box.length, inputText: text })
  state.box = answer.text
}

/** Empties the box, as a select-all and delete does. */
async function wipe($: Engine, state: Rig): Promise<void> {
  const answer = await ($.prompt as unknown as Prompt).edit({ origin: { kind: 'composer' }, text: state.box, cursor: state.box.length, start: 0, end: state.box.length, inputText: '' })
  state.box = answer.text
}

/** Replaces the whole box in one edit, as typing over a selection does. */
async function replaceBox($: Engine, state: Rig, text: string): Promise<void> {
  const answer = await ($.prompt as unknown as Prompt).edit({ origin: { kind: 'composer' }, text: state.box, cursor: state.box.length, start: 0, end: state.box.length, inputText: text })
  state.box = answer.text
}

type Sent = { text: string; drop?: string }

/** Sends the message the way the composer does. */
async function send($: Engine, state: Rig, text: string): Promise<Sent> {
  const out = (await $.prompt.submit({ text, wait: false, origin: { kind: 'composer' } } as never)) as Sent
  if (out.drop === undefined) state.box = ''
  return out
}

async function hint($: Engine, world: World, surface: 'terminal' | 'desktop' = 'terminal'): Promise<Record<string, unknown>> {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface, component: 'PromptHint', props: { isDraft: true, isWorking: false, hint: 'Enter to send' } as never })
  await mounted.unmount()
  return world.seen.PromptHint ?? {}
}

/** A session with one message sent and one reply, the cache warm. */
async function started($: Engine, world: World, state: Rig, first = OPENING, firstReply: typeof FIRST = FIRST): Promise<void> {
  await $.session.start(START)
  await send($, state, first)
  await reply($, world, firstReply)
}

async function tail($: Engine, world: World): Promise<string | undefined> {
  const seen = await hint($, world)
  return seen.tail === undefined ? undefined : String(seen.tail)
}

// -- the hint line -------------------------------------------------------------------

test('a new topic over a big context says what a fresh start would save on every reply', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)

  await put($, state, NEW_TOPIC)
  expect(await tail($, world)).toMatch(/^New topic\? \/clear first makes every reply about \$0\.0\d cheaper$/)
  expect(faults(world)).toEqual([])
})

test('the desktop gets the same line in front of the engine hint', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)

  await put($, state, NEW_TOPIC)
  const seen = await hint($, world, 'desktop')
  expect(String(seen.hint)).toMatch(/^New topic\? \/clear first makes every reply about \$0\.0\d cheaper · Enter to send$/)
})

test('a follow-up, or a draft that shares words with the session, is not a new topic', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)

  await put($, state, FOLLOW_UP)
  expect(await tail($, world)).toBeUndefined()
  await wipe($, state)
  await put($, state, 'make the invoice totals use decimal arithmetic in the rendering pipeline for credit notes too')
  expect(await tail($, world)).toBeUndefined()
})

test('a new topic over a small context says nothing', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await $.session.start(START)
  await send($, state, OPENING)
  await reply($, world, { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 8000, output: 0 }], usd: 0.05 })
  await put($, state, NEW_TOPIC)
  expect(await tail($, world)).toBeUndefined()
})

test('the cold-cache warning keeps the line: the draft check waits until nothing else has anything to say', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  await world.clock.advance(400 * 1000)

  await put($, state, NEW_TOPIC)
  expect(await tail($, world)).toMatch(/^Idle \d+ min, so the cache expired/)
})

test('a change asked for with no file named, in a session that has touched none, asks for the file', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state, OPENING, SMALL)

  await put($, state, NO_PATH)
  expect(await tail($, world)).toBe('No file named yet. Naming the file or function saves Claude a search.')

  // A draft that names the file has no use for the line.
  await wipe($, state)
  await put($, state, 'add retry logic with exponential backoff to src/billing/ledger.ts when the ledger export fails')
  expect(await tail($, world)).toBeUndefined()
})

test('a session that carries earlier work (a resumed one) has touched no file this run, and the missing file is no hint', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)

  await put($, state, NO_PATH)
  expect(await tail($, world)).toBeUndefined()
})

test('once a file has been read the missing file is no longer news', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  await $.tool.call({ tool: 'Read', tool_use_id: 'call-read', file_path: 'src/billing/ledger.ts' } as never)

  await put($, state, NO_PATH)
  expect(await tail($, world)).toBeUndefined()
})

test('a request to read the whole codebase says so', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  await $.tool.call({ tool: 'Read', tool_use_id: 'call-read', file_path: 'src/billing/ledger.ts' } as never)

  await put($, state, READ_ALL)
  expect(await tail($, world)).toBe('Reading everything fills the context fast. Name the folders or files that matter instead.')
})

test('a check shows once per draft, then rests', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state, OPENING, SMALL)

  await put($, state, NO_PATH)
  expect(await tail($, world)).toBeDefined()
  expect(await tail($, world)).toBeDefined()

  // The same kind of draft again, a moment later: it is resting.
  await wipe($, state)
  await put($, state, NO_PATH)
  expect(await tail($, world)).toBeUndefined()
})

test('the draft checks are left out when coaching is off', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { enabled: false } }) })
  const state = rig(on, world)
  await started($, world, state)

  await put($, state, NO_PATH)
  expect(await tail($, world)).toBeUndefined()
})

test('a draft check the person muted is left out', async ($, on) => {
  const world = prepare(on, { settings: settingsFile({ coaching: { muted: ['draft_no_paths'] } }) })
  const state = rig(on, world)
  await started($, world, state)

  await put($, state, NO_PATH)
  expect(await tail($, world)).toBeUndefined()
})

test('a draft check that showed is a note shown, settled by what followed', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state, OPENING, SMALL)

  await put($, state, NO_PATH)
  expect(await tail($, world)).toBeDefined()
  await send($, state, NO_PATH)
  await reply($, world, { gap: MINUTE, ...AGAIN })
  await reply($, world, { gap: MINUTE, ...AGAIN })

  const file = JSON.parse(String(world.files.get(SENSOR))) as { events: { event: string; kind?: string; surface?: string; outcome?: string }[] }
  const shown = file.events.filter(one => one.event === 'note' && one.kind === 'draft_no_paths')
  expect(shown).toHaveLength(1)
  expect(shown[0]).toMatchObject({ surface: 'hint', outcome: 'sent' })
  // Words are never written down.
  expect(JSON.stringify(file)).not.toMatch(/backoff|retry|ledger/)
  expect(faults(world)).toEqual([])
})

// -- /glass tighten ---------------------------------------------------------------------

const WORDY = 'Could you please take a look at the upload service and add some kind of retry logic with exponential backoff for when the network connection drops, and make sure that the retries are capped.'
const TIGHT = 'Add capped retries with exponential backoff to the upload service for dropped connections.'

const TIGHTEN_PANE = { title: 'Tighten', isFocused: true, bodyColumns: 110, placement: 'dock', ...SITE } as const

async function tightenPane($: Engine, surface: 'terminal' | 'desktop' = 'terminal') {
  return $.ui.mount({ plugin: 'claudeglass', surface, component: 'Pane', requestId: 'glass-tighten', props: TIGHTEN_PANE as never })
}

function lines(tree: unknown): string[] {
  return elements(tree)
    .filter(one => one.type === 'Text' || one.type === 'Button')
    .map(textOf)
    .filter(text => text !== '')
}

test('/glass tighten asks Haiku for a shorter draft and shows the two as a diff, with the call\'s cost', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  state.rewrite = TIGHT
  await started($, world, state)
  state.box = WORDY

  const answer = await $.command.run({ command: 'glass', args: 'tighten' } as never)
  expect(answer.text).toBe('A shorter version of your draft is open in the Glass pane. Nothing is sent.')
  expect(world.opened).toContainEqual(expect.objectContaining({ id: 'glass-tighten', closeOnEscape: true }))
  expect(state.completions).toHaveLength(1)
  expect(state.completions[0]?.prompt).toContain(WORDY)
  expect(state.completions[0]?.system).toMatch(/Keep every requirement/)

  for (const surface of ['terminal', 'desktop'] as const) {
    const mounted = await tightenPane($, surface)
    const tree = await mounted.drawn()
    const code = elements(tree).find(one => one.type === 'Code')
    expect(code?.props?.format).toBe('diff')
    expect(String(code?.props?.source)).toMatch(/^@@ -1,\d+ \+1,\d+ @@\n/)
    expect(String(code?.props?.source)).toContain(`+${TIGHT}`)
    const shown = lines(tree).join('\n')
    expect(shown).toMatch(/Your draft is 34 words\. The shorter version is 13\./)
    // Tokens only: the answer names no model, so no amount could be sure of the right price.
    expect(shown).toMatch(/Haiku used 460 tokens\./)
    expect(shown).not.toMatch(/about/)
    const buttons = elements(tree).filter(one => one.type === 'Button')
    expect(buttons.map(one => [one.props?.label, one.props?.hotkey])).toEqual([
      ['Use this', 'u'],
      ['Keep mine', 'e'],
    ])
    await mounted.unmount()
  }
  expect(state.submitted).toEqual([OPENING])
  expect(faults(world)).toEqual([])
})

test('Use this puts the shorter version in the box and closes the pane; nothing is sent', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  state.rewrite = TIGHT
  await started($, world, state)
  state.box = WORDY
  await $.command.run({ command: 'glass', args: 'tighten' } as never)

  const mounted = await tightenPane($)
  await mounted.press({ key: 'use' } as never)
  expect(state.filled).toEqual([TIGHT])
  expect(state.box).toBe(TIGHT)
  expect(state.closed).toEqual(['glass-tighten'])
  expect(state.submitted).toEqual([OPENING])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Keep mine closes the pane and leaves the draft as it was', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  state.rewrite = TIGHT
  await started($, world, state)
  state.box = WORDY
  await $.command.run({ command: 'glass', args: 'tighten' } as never)

  const mounted = await tightenPane($)
  await mounted.press({ key: 'keep' } as never)
  expect(state.closed).toEqual(['glass-tighten'])
  expect(state.box).toBe(WORDY)
  expect(state.filled).toEqual([])
  await mounted.unmount()
})

test('the command typed over the draft still tightens the draft that was there', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  state.rewrite = TIGHT
  await started($, world, state)
  await put($, state, WORDY)
  // Select all and type the command: the box now holds the command.
  await replaceBox($, state, '/glass tighten')

  const answer = await $.command.run({ command: 'glass', args: 'tighten' } as never)
  expect(answer.text).toMatch(/^A shorter version of your draft is open/)
  expect(state.completions[0]?.prompt).toContain(WORDY)

  // Keep mine puts the draft back in the emptied box.
  state.box = ''
  const mounted = await tightenPane($)
  await mounted.press({ key: 'keep' } as never)
  expect(state.box).toBe(WORDY)
  await mounted.unmount()
})

test('a rewrite that used the whole allowance was cut off: it is a failure and never shown', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  state.rewrite = TIGHT
  state.outputTokens = 9999
  await started($, world, state)
  state.box = WORDY

  const answer = await $.command.run({ command: 'glass', args: 'tighten' } as never)
  expect(answer.text).toBe("Haiku's rewrite was cut short, so it was not used. Try a shorter draft.")
  expect(world.opened.filter(one => (one as { id: string }).id === 'glass-tighten')).toHaveLength(0)
  expect(faults(world)).toEqual(['claudeglass: tighten cut'])
})

test('/glass tighten <text> tightens that text, whatever the box holds', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  state.rewrite = TIGHT
  await started($, world, state)
  state.box = '/glass tighten'

  const answer = await $.command.run({ command: 'glass', args: `tighten ${WORDY}` } as never)
  expect(answer.text).toMatch(/^A shorter version of your draft is open/)
  expect(state.completions).toHaveLength(1)
  expect(state.completions[0]?.prompt).toContain(WORDY)
  expect(state.completions[0]?.prompt).not.toContain('/glass')
  expect(faults(world)).toEqual([])
})

test('the last draft is kept when the box is emptied, until a message is sent', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  state.rewrite = TIGHT
  await started($, world, state)
  await put($, state, WORDY)
  await wipe($, state)
  expect(state.box).toBe('')

  const answer = await $.command.run({ command: 'glass', args: 'tighten' } as never)
  expect(answer.text).toMatch(/^A shorter version of your draft is open/)
  expect(state.completions[0]?.prompt).toContain(WORDY)

  // A message sent ends the draft: nothing is left to tighten.
  await send($, state, FOLLOW_UP)
  expect((await $.command.run({ command: 'glass', args: 'tighten' } as never)).text).toBe('Your prompt box is empty, so there is nothing to tighten.')
  expect(faults(world)).toEqual([])
})

test('/glass tighten with nothing to tighten, an error, or no gain says so and opens nothing', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)

  expect((await $.command.run({ command: 'glass', args: 'tighten' } as never)).text).toBe('Your prompt box is empty, so there is nothing to tighten.')

  state.box = WORDY
  state.rewrite = null
  expect((await $.command.run({ command: 'glass', args: 'tighten' } as never)).text).toBe('Haiku could not rewrite the draft, so it is unchanged.')

  state.rewrite = WORDY
  expect((await $.command.run({ command: 'glass', args: 'tighten' } as never)).text).toBe('Your draft is already tight. Haiku found nothing to cut.')

  expect(world.opened.filter(one => (one as { id: string }).id === 'glass-tighten')).toHaveLength(0)
  // The model's refusal is for the debug log, worded as what happened.
  expect(faults(world)).toEqual(['claudeglass: tighten api-error'])
})

// -- the guard ------------------------------------------------------------------------------

/** The cache expired over a big context: a send now is a costly one. */
async function gone($: Engine, world: World): Promise<void> {
  await world.clock.advance(400 * 1000)
}

test('the guard is off until it is turned on: a costly send goes without a question', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  await gone($, world)

  const out = await send($, state, NEW_TOPIC)
  expect(out.drop).toBeUndefined()
  expect(state.asked).toEqual([])
  expect(state.submitted).toEqual([OPENING, NEW_TOPIC])
})

test('/glass guard turns it on and off and says which', async ($, on) => {
  const world = prepare(on)
  rig(on, world)
  await $.session.start(START)
  expect((await $.command.run({ command: 'glass', args: 'guard' } as never)).text).toBe('Guard is on. Glass asks before a costly send.')
  expect((await $.command.run({ command: 'glass', args: 'guard' } as never)).text).toBe('Guard is off.')
  expect((await $.command.run({ command: 'glass', args: 'guard on' } as never)).text).toBe('Guard is on. Glass asks before a costly send.')
})

test('with the guard on, a send over an expired cache asks with the figures, and Send sends', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  await $.command.run({ command: 'glass', args: 'guard' } as never)
  await gone($, world)

  const out = await send($, state, NO_PATH)
  expect(out.drop).toBeUndefined()
  expect(state.asked).toHaveLength(1)
  expect(state.asked[0]).toMatch(/^The cache expired after \d+ min idle, so sending costs about \$\d.* Clearing first costs about \$\d.* What now\?$/)
  expect(state.submitted).toEqual([OPENING, NO_PATH])
  expect(faults(world)).toEqual([])
})

test('Cancel stops the send and keeps the draft in the box', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  await $.command.run({ command: 'glass', args: 'guard' } as never)
  await gone($, world)
  state.answer = 'Cancel'

  const out = await send($, state, NO_PATH)
  expect(out.drop).toBe('Not sent. Your message is still in the box.')
  expect(state.submitted).toEqual([OPENING])
  expect(state.box).toBe(NO_PATH)
  expect(faults(world)).toEqual([])
})

test('a dialog the person dismisses counts as Cancel; one that fails at once sends as normal', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  await $.command.run({ command: 'glass', args: 'guard' } as never)
  await gone($, world)
  state.answer = null

  state.dismissAfter = 3000
  const dismissed = await send($, state, NO_PATH)
  expect(dismissed.drop).toBe('Not sent. Your message is still in the box.')
  expect(state.submitted).toEqual([OPENING])

  state.dismissAfter = 0
  const refused = await send($, state, NO_PATH)
  expect(refused.drop).toBeUndefined()
  expect(state.submitted).toEqual([OPENING, NO_PATH])
})

test('Clear first sets /clear in the box and gives the draft back after the clear', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  await $.command.run({ command: 'glass', args: 'guard' } as never)
  await gone($, world)
  state.answer = 'Clear first'

  const out = await send($, state, NO_PATH)
  expect(out.drop).toBe('Not sent. Press Enter to clear; your message comes back after.')
  expect(state.submitted).toEqual([OPENING])
  expect(state.box).toBe('/clear')

  // Enter on /clear is a slash command the guard leaves alone; then the conversation is cleared.
  state.answer = 'Cancel'
  expect((await send($, state, '/clear')).drop).toBeUndefined()
  await $.session.end({ sessionId: SESSION, reason: 'clear' } as never)
  await world.clock.advance(1000)
  expect(state.box).toBe(NO_PATH)
  expect(state.asked).toHaveLength(1)
  expect(faults(world)).toEqual([])
})

test('the guard asks about a new topic over a big context, even with the cache warm, and leaves ordinary sends alone', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  await $.command.run({ command: 'glass', args: 'guard' } as never)

  await send($, state, FOLLOW_UP)
  expect(state.asked).toEqual([])

  state.answer = 'Cancel'
  const out = await send($, state, NEW_TOPIC)
  expect(out.drop).toBeDefined()
  expect(state.asked[0]).toMatch(/^This looks like a new topic, and the session carries about 1\d\dk tokens\. A fresh start makes every reply about \$0\.0\d cheaper\. What now\?$/)
  expect(notes(world)).toEqual([])
})

test('the Now page turns the guard on in Detailed; Simple has no toggle', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'Pane', requestId: 'glass', props: { title: 'Glass', isFocused: true, bodyColumns: 110, placement: 'dock', ...SITE } as never })
  expect(await mounted.find({ type: 'Button', key: 'guard-toggle' })).toBeUndefined()

  await $.command.run({ command: 'glass', args: 'detail' } as never)
  const toggle = await mounted.find({ type: 'Button', key: 'guard-toggle' })
  expect(toggle?.props.hotkey).toBeUndefined()
  expect(toggle?.props.dimColor).toBe(true)
  expect(String(toggle?.props.label)).toBe('Ask before a costly send: Off')

  await mounted.press({ key: 'guard-toggle' })
  expect(String((await mounted.find({ type: 'Button', key: 'guard-toggle' }))?.props.label)).toBe('Ask before a costly send: On')
  await mounted.unmount()

  await gone($, world)
  state.answer = 'Cancel'
  expect((await send($, state, NO_PATH)).drop).toBeDefined()
  expect(state.asked).toHaveLength(1)
  expect(faults(world)).toEqual([])
})

// -- review fixes 3: C2 (the work carried), C5 (attachments) ------------------------------------

const ANOTHER_TOPIC = 'plan a holiday itinerary for lisbon including restaurants and museums and the evening trains'
// 30k in the context less an 18k startup is 12k carried: not yet worth a fresh start.
const LIGHT = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 28000, output: 0 }], usd: 0.2 }

test('a new topic is judged by the work carried, not the whole context: startup is not carried work', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state, OPENING, LIGHT)

  await put($, state, NEW_TOPIC)
  expect(await tail($, world)).toBeUndefined()

  // The guard reads the same carried figure: nothing to ask about.
  await $.command.run({ command: 'glass', args: 'guard' } as never)
  state.answer = 'Cancel'
  const out = await send($, state, NEW_TOPIC)
  expect(out.drop).toBeUndefined()
  expect(state.asked).toEqual([])
  expect(faults(world)).toEqual([])
})

for (const coaching of [{ muted: ['draft_topic'] }, { enabled: false }]) {
  test(`the guard asks about a new topic under the hint rules: not with ${JSON.stringify(coaching)}`, async ($, on) => {
    const world = prepare(on, { settings: settingsFile({ coaching }) })
    const state = rig(on, world)
    await started($, world, state)
    await $.command.run({ command: 'glass', args: 'guard' } as never)
    state.answer = 'Cancel'
    const out = await send($, state, NEW_TOPIC)
    expect(out.drop).toBeUndefined()
    expect(state.asked).toEqual([])
    expect(faults(world)).toEqual([])
  })
}

test('the guard asks about a new topic once, then the topic question rests like the hint does', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  await $.command.run({ command: 'glass', args: 'guard' } as never)

  state.answer = 'Send'
  expect((await send($, state, NEW_TOPIC)).drop).toBeUndefined()
  expect(state.asked).toHaveLength(1)

  // Another new topic a moment later is resting: no question.
  expect((await send($, state, ANOTHER_TOPIC)).drop).toBeUndefined()
  expect(state.asked).toHaveLength(1)
  expect(state.submitted).toEqual([OPENING, NEW_TOPIC, ANOTHER_TOPIC])
  expect(faults(world)).toEqual([])
})

test('a send with pasted images is never held by the guard: Cancel and Clear first could not give the images back', async ($, on) => {
  const world = prepare(on)
  const state = rig(on, world)
  await started($, world, state)
  await $.command.run({ command: 'glass', args: 'guard' } as never)
  await gone($, world)
  state.answer = 'Cancel'

  const out = await $.prompt.submit({ text: NO_PATH, wait: false, origin: { kind: 'composer' }, attachments: [{ type: 'image', mediaType: 'image/png' }] } as never)
  expect((out as { drop?: string }).drop).toBeUndefined()
  expect(state.asked).toEqual([])
  expect(state.submitted).toEqual([OPENING, NO_PATH])

  // The same message without the image is still asked about.
  expect((await send($, state, NO_PATH)).drop).toBeDefined()
  expect(state.asked).toHaveLength(1)
  expect(faults(world)).toEqual([])
})
