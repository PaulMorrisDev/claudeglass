import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { GlassRestState, GlassSettings } from '../types'
import { isTypedOrigin, thresholdsOf } from './coach-mem'
import { amountHasShare, amountOfAfterAbout, formatTokens, minutes, withEst } from './fmt'
import { COPY } from './generated/copy.js'
import type { Io } from './io'
import { carriedTokens, draftFacts, emptyBag, factsKey, isSlash, learnFile, learnPrompt, pickDraft, STALE_TOKENS } from './lib/draft.js'
import type { Bag, Facts } from './lib/draft.js'
import { mergeRest } from './lib/coach.js'
import { sendCost } from './lib/pricing.js'
import { mayShow, recordShown } from './lib/rest.js'
import type { RestThresholds } from './lib/rest.js'
import { fill } from './lib/text.js'
import { emptySnap, flags, live, touch } from './live'
import { noteShown, seconds } from './sensor'
import { coachingApplies, getSettings } from './settings'
import { onClear, onTurnEnd } from './track'
import { buildView, coldMin, sendCostNow } from './view'
import type { View } from './view'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)

// Help with the message in the box, before it is sent.
//
//   The hint line (hint.ts asks draftLine when coach-mem.ts has nothing to say):
//     draft_topic     a new topic in a context worth clearing (the words share nothing with the session's)
//     draft_no_paths  a change asked for with no file named, in a session that has touched none
//     draft_read_all  a request to read the whole codebase
//   The guard (opt-in, off until `guard` is switched on in $.store): a send that would be costly asks first.
//
// The words of the session (a bag of word stems with counts, no order, no sentences) and the draft in the
// box are kept in module memory only: never in $.state, never on disk. The decisions are lib/draft.js.

/** A dialog dismissed sooner than this was not dismissed by a person: nobody could be asked, so the message goes. */
const ASK_MIN_MS = 800
/** A draft kept for after a /clear is dropped after this long. */
const RESTORE_MS = 10 * 60 * 1000
/** How often, and how many times, the box is looked at after it was filled (the engine may empty it as a send ends). */
const FILL_CHECK_MS = 200
const FILL_CHECKS = 4

type Shown = { kind: string; ts: number; ctx: number }

type Mem = {
  /** The draft in the box now. */
  text: string
  /** The last draft that wasn't a slash command, kept until a message is sent: what /glass tighten works on when the command has taken the box's place. */
  last: string
  /** What the draft was checked for, and a key for it that changes when a hint could. */
  facts: Facts
  key: string
  /** Which check leads (the one pickDraft would choose before the context is known); a new one starts a new episode. */
  lead: string
  /** Word stems of the prompts sent, the replies and the files' names. */
  bag: Bag
  /** Files read or changed so far this session. */
  files: number
  /** The kind of hint now on screen; it stays while it applies, without asking the rest rule again. */
  episode: string | null
  /** The rest rows this file stamped, laid over the snapshot's. */
  overlay: GlassRestState
  shown: Shown[]
  /** A draft set aside while the person clears the session (the guard's Clear first), and when. */
  restore: { text: string; at: number } | null
}

function leadOf(facts: Facts): string {
  return facts.topic ? 'topic' : facts.noPaths ? 'paths' : facts.readAll ? 'all' : ''
}

function fresh(): Mem {
  const bag = emptyBag()
  const facts = draftFacts('', bag, 0)
  return { text: '', last: '', facts, key: factsKey(facts), lead: leadOf(facts), bag, files: 0, episode: null, overlay: { hints: {} }, shown: [], restore: null }
}

let mem = fresh()

/** Forgets the session's words and the draft (a /clear, a new session, or a test). A draft set aside for after the clear stays. */
export function resetDraft(): void {
  const restore = mem.restore
  mem = fresh()
  mem.restore = restore
}

/** Forgets everything, the set-aside draft too (a test). */
export function forgetDraft(): void {
  mem = fresh()
}

/** Whether the draft checks may show: coaching is on for this user and project. */
export function draftChecksAllowed(settings: GlassSettings): boolean {
  return coachingApplies(settings)
}

/** The engine, as the file's hooks need it: every call on `$` spelled out here. */
function kit($: EngineInterface): Io {
  return {
    now: () => $.clock.now(),
    after: (ms, run) => $.clock.after(ms, run),
    every: (ms, run) => $.clock.every(ms, run),
    redraw: () => {
      void $.ui.invalidate('ui.render')
    },
    debug: text => {
      $.ui.log(text, { to: 'debug' })
    },
    note: text => {
      $.ui.log(text)
    },
    places: async () => ({
      configDir: await $.env.get('CLAUDE_CONFIG_DIR'),
      userProfile: await $.env.get('USERPROFILE'),
      home: await $.env.get('HOME'),
    }),
    readFile: async path => {
      const text = await $.fs.read(path)
      return typeof text === 'string' ? text : null
    },
    writeFile: async (path, text) => {
      await $.fs.write(path, text)
    },
    getSnap: () => read($, snapAtom),
    putSnap: async snap => {
      await update($, snapAtom, () => snap)
    },
    getSettings: () => read($, settingsAtom),
    putSettings: async settings => {
      await update($, settingsAtom, () => settings)
    },
    getDetailed: () => read($, detailedAtom),
  }
}

function debug($: EngineInterface, where: string, error: unknown): void {
  $.ui.log(`claudeglass: draft ${where} ${String(error)}`, { to: 'debug' })
}

// -- the draft ---------------------------------------------------------------

/** Checks the draft; true when what decides a hint changed, so it should be drawn again. */
function checkText(text: string): boolean {
  mem.text = text
  // A box that begins with a slash is a command being typed (or a path): the draft before it stands. So does an
  // emptied box: the last draft is kept until a message is sent, so /glass tighten can still find it.
  if (text.trim() !== '' && !text.trimStart().startsWith('/')) mem.last = text
  mem.facts = draftFacts(text, mem.bag, mem.files)
  const key = factsKey(mem.facts)
  // A check that leads again after another (or none) was the lead has its rest asked anew.
  const lead = leadOf(mem.facts)
  if (lead !== mem.lead) {
    mem.lead = lead
    mem.episode = null
  }
  if (key === mem.key) return false
  mem.key = key
  return true
}

/** A message went in: its words join the session's, and the draft starts over. */
function sentText(text: string, typed: boolean): void {
  if (typed && text.trim() !== '' && !isSlash(text)) {
    mem.bag = learnPrompt(mem.bag, text)
    mem.restore = null
  }
  mem.text = ''
  // A slash command sent (/glass tighten itself) leaves the draft before it: that is what it works on.
  if (!isSlash(text)) mem.last = ''
  mem.facts = draftFacts('', mem.bag, mem.files)
  mem.key = factsKey(mem.facts)
  mem.lead = leadOf(mem.facts)
  mem.episode = null
}

/** The last draft typed that wasn't a slash command; kept when the box is emptied, and empty once a message is sent. */
export function rememberedDraft(): string {
  return mem.last
}

/** Text was put in the box by a press (a rewrite, a draft handed back): it is the draft now. */
export function filledDraft(text: string): void {
  mem.last = text
}

/** A file was read or changed. */
function touchedFile(path: unknown): boolean {
  mem.files += 1
  if (typeof path === 'string' && path !== '') mem.bag = learnFile(mem.bag, path)
  return checkText(mem.text)
}

/** Puts what the screen showed into the snapshot: the sensor list and the rest rows. */
async function drain(io: Io): Promise<void> {
  if (mem.shown.length === 0) return
  const snap = await live(io)
  for (const one of mem.shown) noteShown(snap, one.kind, 'hint', one.ts, one.ctx)
  mem.shown = []
  snap.rest = mergeRest(snap.rest, mem.overlay)
  await touch(io)
}

// -- the hint line -----------------------------------------------------------

function startupOf(view: View): number {
  return view.parts?.startup ?? view.snap.startupFirst ?? 0
}

/** What each later reply would cost less after a fresh start, in USD; null when it can't be priced. */
function savedPerReply(view: View): number | null {
  const startup = startupOf(view)
  if (view.rates === null || startup <= 0 || view.tokens <= startup) return null
  return sendCost(view.rates, view.tokens, true, view.ttl) - sendCost(view.rates, startup, true, view.ttl)
}

function topicLine(view: View): string {
  const saved = savedPerReply(view)
  if (saved === null || saved < 0.005 || !amountHasShare(saved, view.snap, view.settings)) return COPY['hint.topic']
  return withEst(fill(COPY['hint.topic.save'], { amount: amountOfAfterAbout(saved, view.snap, view.settings) }), view.settings)
}

/**
 * The hint for the draft in the box now, or null. hint.ts asks only when coach-mem.ts has nothing to
 * say. One check at most, the first that applies; once shown it stays while it applies, then rests
 * under the capture hook's rules (so it comes back later than it first did, unless more is at stake).
 */
export function draftLine(view: View): string | null {
  const { snap, settings, now } = view
  if (!draftChecksAllowed(settings)) {
    mem.episode = null
    return null
  }
  const kind = pickDraft(mem.facts, carriedTokens(view.tokens, startupOf(view)))
  if (kind === null || settings.muted.includes(kind)) {
    mem.episode = null
    return null
  }
  if (kind !== mem.episode) {
    const th = thresholdsOf(settings) as unknown as RestThresholds
    const nowS = seconds(now)
    const stake = kind === 'draft_topic' ? carriedTokens(view.tokens, startupOf(view)) : view.tokens
    const rest = mergeRest(snap.rest, mem.overlay)
    if (!mayShow(rest, kind, stake, nowS, th)) {
      mem.episode = null
      return null
    }
    mem.overlay = recordShown(rest, kind, stake, nowS, th, settings.once.includes(kind))
    mem.episode = kind
    mem.shown.push({ kind, ts: now, ctx: view.tokens })
  }
  return kind === 'draft_topic' ? topicLine(view) : kind === 'draft_no_paths' ? COPY['hint.nopaths'] : COPY['hint.readall']
}

// -- the guard ---------------------------------------------------------------

type Costly = { question: string; cold: boolean }

/**
 * Whether the guard may ask the topic question: under the hint's own rules (coaching on, not muted, and the rest
 * rule, unless the hint is on screen already). Asking counts as showing it, so it rests like the hint does.
 */
function topicMayAsk(view: View, carried: number): boolean {
  const { snap, settings, now } = view
  if (!draftChecksAllowed(settings) || settings.muted.includes('draft_topic')) return false
  if (mem.episode === 'draft_topic') return true
  const th = thresholdsOf(settings) as unknown as RestThresholds
  const nowS = seconds(now)
  const rest = mergeRest(snap.rest, mem.overlay)
  if (!mayShow(rest, 'draft_topic', carried, nowS, th)) return false
  mem.overlay = recordShown(rest, 'draft_topic', carried, nowS, th, settings.once.includes('draft_topic'))
  mem.episode = 'draft_topic'
  mem.shown.push({ kind: 'draft_topic', ts: now, ctx: view.tokens })
  return true
}

/** The question a costly send is asked with, or null when the send is an ordinary one. */
function costlySend(view: View, text: string): Costly | null {
  const { snap, settings } = view
  const idle = snap.lastTs === null ? 0 : view.now - snap.lastTs
  const cold = view.cache !== null && !view.cache.isWarm && view.tokens >= coldMin(settings)
  const startup = startupOf(view)
  if (cold) {
    const priced = view.rates !== null && startup > 0
    const send = sendCostNow(view, false)
    const clear = view.rates === null ? 0 : sendCost(view.rates, startup, false, view.ttl)
    if (priced && amountHasShare(send, snap, settings)) {
      const question = fill(COPY['guard.cold'], { minutes: minutes(idle), amount: amountOfAfterAbout(send, snap, settings), fresh: amountOfAfterAbout(clear, snap, settings) })
      return { question: withEst(question, settings), cold: true }
    }
    return { question: fill(COPY['guard.cold.plain'], { minutes: minutes(idle) }), cold: true }
  }
  const facts = draftFacts(text, mem.bag, mem.files)
  const carried = carriedTokens(view.tokens, startup)
  if (facts.topic && carried >= STALE_TOKENS && topicMayAsk(view, carried)) {
    const tokens = formatTokens(carried)
    const saved = savedPerReply(view)
    if (saved !== null && saved >= 0.005 && amountHasShare(saved, snap, settings)) {
      return { question: withEst(fill(COPY['guard.topic'], { tokens, amount: amountOfAfterAbout(saved, snap, settings) }), settings), cold: false }
    }
    return { question: fill(COPY['guard.topic.plain'], { tokens }), cold: false }
  }
  return null
}

/**
 * Puts `text` in the box, and looks again a few times: the engine may empty the box as the submission
 * that was stopped finishes. The box is filled again only while it is empty, never over a person's typing.
 */
async function place($: EngineInterface, text: string, checks = FILL_CHECKS): Promise<void> {
  try {
    await $.prompt.fill({ text, mode: 'replace' })
  } catch (error) {
    debug($, 'fill', error)
    return
  }
  if (checks <= 0) return
  $.clock.after(FILL_CHECK_MS, () => {
    void (async () => {
      try {
        const box = await $.prompt.read()
        if (box.text === '') await place($, text, checks - 1)
      } catch (error) {
        debug($, 'box', error)
      }
    })()
  })
}

/**
 * Asks about a costly send when the guard is on. Null lets the message go; anything else stops it:
 * Cancel keeps the draft in the box, Clear first sets /clear in the box and keeps the draft for after.
 */
async function guard($: EngineInterface, io: Io, e: { text: string; turnId?: string; attachments?: readonly unknown[]; origin: { kind: string } }): Promise<{ drop: string } | null> {
  if ((await $.store.get('guard')) !== true) return null
  // A queued message, a notification and the engine's own are not asked about; a slash command is the person's own cure.
  if (e.turnId !== undefined || (e.origin.kind !== 'composer' && e.origin.kind !== 'sdk')) return null
  if (e.text.trim() === '' || isSlash(e.text)) return null
  // Cancel and Clear first put the text back in the box and nothing else: a send with pasted images goes as normal.
  if (e.attachments !== undefined && e.attachments.length > 0) return null
  const snap = await live(io)
  const settings = await getSettings(io)
  const now = await $.clock.now()
  const costly = costlySend(buildView(snap, settings, false, now, false), e.text)
  await drain(io)
  if (costly === null) return null
  if (costly.cold) flags.coldWarned = true
  let answer: string
  try {
    answer = await $.ui.ask(costly.question, { options: [COPY['guard.send'], COPY['guard.clear'], COPY['guard.cancel']], header: COPY['guard.header'] })
  } catch (error) {
    // Dismissed by a person is Cancel; refused at once (nobody to ask, a headless run) sends as normal.
    if ((await $.clock.now()) - now < ASK_MIN_MS) {
      debug($, 'ask', error)
      return null
    }
    answer = COPY['guard.cancel']
  }
  if (answer === COPY['guard.send']) return null
  if (answer === COPY['guard.clear']) {
    mem.restore = { text: e.text, at: now }
    await place($, '/clear')
    return { drop: COPY['guard.dropped.clear'] }
  }
  await place($, e.text)
  return { drop: COPY['guard.dropped'] }
}

export const register: Register = on => {
  // The draft in the box. (Matchers, apart from the other files' hooks on these events.)
  on('prompt.edit', { text: /[\s\S]*/ }, async ($, e, next) => {
    const after = await next(e)
    try {
      if (checkText(after.text)) void $.ui.invalidate('ui.render')
      await drain(kit($))
    } catch (error) {
      debug($, 'prompt.edit', error)
    }
    return after
  }).catch(($, e, next) => next(e))

  on('prompt.submit', { text: /[\s\S]*/ }, async ($, e, next) => {
    try {
      const stopped = await guard($, kit($), e)
      if (stopped !== null) return stopped
    } catch (error) {
      debug($, 'guard', error)
    }
    const result = await next(e)
    try {
      if (result.drop === undefined) {
        sentText(e.text, isTypedOrigin(e.origin.kind))
        await drain(kit($))
      }
    } catch (error) {
      debug($, 'prompt.submit', error)
    }
    return result
  }).catch(($, e, next) => next(e))

  // Files read or changed: a session that has touched none doesn't know where to look. (A matcher, apart from the other files' hooks.)
  on('tool.call', { tool: /^(Read|Edit|Write|MultiEdit|NotebookEdit)$/ }, async ($, e, next) => {
    const result = await next(e)
    if (result.deny !== undefined || result.isError === true) return result
    try {
      const args = e as unknown as Record<string, unknown>
      if (touchedFile(args.file_path ?? args.notebook_path)) void $.ui.invalidate('ui.render')
    } catch (error) {
      debug($, 'tool.call', error)
    }
    return result
  }).catch(($, e, next) => next(e))

  // A new session starts the words over. (A matcher, apart from the other files' hooks on the event.)
  on('session.start', { cwd: /./ }, async ($, e, next) => {
    resetDraft()
    return next(e)
  }).catch(($, e, next) => next(e))

  // After a /clear the draft set aside by Clear first comes back.
  on('session.end', { reason: 'clear' }, async ($, e, next) => {
    try {
      const kept = mem.restore
      mem.restore = null
      if (kept !== null && (await $.clock.now()) - kept.at < RESTORE_MS) $.clock.after(FILL_CHECK_MS * 2, () => void place($, kept.text))
    } catch (error) {
      debug($, 'session.end', error)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // The reply's words are the session's too: a follow-up that uses them is not a new topic.
  onTurnEnd(async end => {
    if (end.answer !== undefined && end.answer !== '') mem.bag = learnPrompt(mem.bag, end.answer.slice(0, 4000))
  })
  onClear(() => {
    resetDraft()
  })
}
