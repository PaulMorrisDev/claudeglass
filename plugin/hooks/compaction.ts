import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { GlassKeepItem, GlassPin, GlassReview, GlassSettings } from '../types'
import { drainSensor, emptyReview, forgetMemory, INSTRUCT_LEAD, keepInstruction, keepList, memory, referencedCount, reviewCompaction, boundaryLine, sound, suggestCount, tickedItems, withReadded, withSummary } from './compaction-memory'
import type { BoundaryDetail, MessageLike } from './compaction-memory'
import { amountOfAfterAbout } from './fmt'
import { COPY } from './generated/copy.js'
import { holdAfterReply, holdStep, saidTotal } from './lib/coverage.js'
import { buildKeepList, squash } from './lib/keeplist.js'
import { priceUsage, ratesFor } from './lib/pricing.js'
import { emptySnap, soundSnap } from './live'
import { soundSettings } from './settings'
import { onTurnEnd } from './track'

// Compaction review, the event side. Before a summary: the keep list follows the
// conversation (in $.state, for the page and the band). At a summary of the main
// session: the list as it stood is checked against the summary Claude Code wrote,
// the result goes to $.state, one line to the transcript, and counts to the sensor.
// Every summary (a /compact, an automatic one, a plugin's and one made ahead) is told
// the ticked keep list as instructions for the summariser. One opt-in, off until the
// user turns it on in the page: hold an automatic summary once for review.
// Nothing the user or Claude wrote is written to disk: the keep list and the
// summary live in $.state and module memory only.

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const pinsAtom = atom({ plugin: 'claudeglass', key: 'pins' } as const, [] as GlassPin[])
const reviewAtom = atom({ plugin: 'claudeglass', key: 'review' } as const, emptyReview())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null as GlassSettings | null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)

/** The opt-in lives in $.store under this key; the page turns it on and off. */
export const HOLD_KEY = 'hold'
const MAX_CLAUDE_MD = 200000

function joinPath(base: string, name: string): string {
  return `${base.replace(/[\\/]+$/, '')}/${name}`
}

/** What CLAUDE.md says in the project and at user level; imports, nested and path-scoped rules and auto memory are not read. */
async function claudeMdOf($: EngineInterface): Promise<string> {
  if (memory.claudeMd !== null) return memory.claudeMd
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME'))
  const configured = await $.env.get('CLAUDE_CONFIG_DIR')
  const paths: string[] = []
  if (memory.cwd !== null) paths.push(joinPath(memory.cwd, 'CLAUDE.md'))
  if (configured !== undefined && configured !== '') paths.push(joinPath(configured, 'CLAUDE.md'))
  else if (home !== undefined && home !== '') paths.push(joinPath(joinPath(home, '.claude'), 'CLAUDE.md'))
  const texts: string[] = []
  for (const path of paths) {
    try {
      const text = await $.fs.read(path)
      if (typeof text === 'string' && text !== '') texts.push(text)
    } catch {
      // No such file: nothing to add.
    }
  }
  memory.claudeMd = texts.join('\n').slice(0, MAX_CLAUDE_MD)
  return memory.claudeMd
}

/** True when two lists name the same items in the same state (a redraw is not worth it otherwise). */
function same(a: readonly GlassKeepItem[], b: readonly GlassKeepItem[]): boolean {
  return a.length === b.length && a.every((one, index) => one.id === b[index]?.id && one.safe === b[index]?.safe)
}

/** The newest typed prompt a pin could be taken from, for the one row that offers [p] pin. */
function pinnableOf(messages: readonly MessageLike[]): string | null {
  for (let i = messages.length - 1, seen = 0; i >= 0 && seen < 8; i--) {
    const message = messages[i] as MessageLike
    if (message.role !== 'user' || (message.text ?? '') === '') continue
    seen += 1
    const found = buildKeepList([message], {}).some(item => item.kind === 'instruction' || item.kind === 'decision')
    if (found) return squash(message.text ?? '')
  }
  return null
}

/** Brings the keep list up to date with the conversation so far. */
async function refresh($: EngineInterface): Promise<void> {
  const messages = (await $.session.messages()) as readonly MessageLike[]
  const pins = await read($, pinsAtom)
  const review = sound(await read($, reviewAtom))
  // Items put back after a summary lost them are no longer in the messages, so they are merged in.
  const items = withReadded(keepList(messages, pins, await claudeMdOf($), memory.cwd), review.readded)
  memory.pinnable = pinnableOf(messages)
  const suggest = suggestCount(items, review.readded)
  if (same(review.list, items) && review.suggest === suggest) return
  await update($, reviewAtom, (was): GlassReview => ({ ...sound(was), list: items, suggest }))
}

/** What the summariser is told to keep: the ticked items of the keep list, or of a rescan of the messages when the list is empty. */
async function keepFor($: EngineInterface, messages: readonly MessageLike[]): Promise<string> {
  const review = sound(await read($, reviewAtom))
  const pins = await read($, pinsAtom)
  const empty = review.list.length === 0 && review.readded.length === 0 && pins.length === 0
  // The list is as of the last reply; a rescan costs time the hook has little of, so it waits for an empty list.
  const now = empty ? { ...review, list: keepList(messages, pins, await claudeMdOf($), memory.cwd) } : review
  return keepInstruction(tickedItems(now, pins))
}

export const register: Register = on => {
  on('session.start', { cwd: /./ }, async ($, e, next) => {
    memory.cwd = e.cwd
    memory.claudeMd = null
    return next(e)
  })

  on('turn.complete', { reason: /./ }, async ($, e, next) => {
    if (e.agentId === undefined) {
      // The reply a summary was held in has ended: the next automatic summary goes through.
      memory.hold = holdAfterReply(memory.hold)
      try {
        await refresh($)
      } catch (error) {
        $.ui.log(`claudeglass: keep list ${String(error)}`, { to: 'debug' })
      }
    }
    return next(e)
  })

  // The summary of the main session. A subagent's own summary has an agentId and is left alone.
  on('session.compact', { trigger: /^(manual|auto|plugin|precompute)$/ }, async ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
    const trigger = e.trigger
    try {
      if (trigger === 'auto') {
        const reading = (await read($, snapAtom)).reading
        const step = holdStep({
          enabled: (await $.store.get(HOLD_KEY)) === true,
          trigger: 'auto',
          window: reading?.window ?? null,
          point: reading?.point ?? null,
          tokens: reading?.tokens ?? null,
          state: memory.hold,
        })
        memory.hold = step.next
        const hold = step.hold
        if (hold) {
          await update($, reviewAtom, (was): GlassReview => ({ ...sound(was), held: true, ui: { ...sound(was).ui, view: 'before' } }))
          if (step.first) void $.ui.toast(COPY['compaction.held.toast'])
          return { skip: COPY['compaction.held.skip'] }
        }
      } else if (trigger !== 'precompute') {
        memory.hold = 'none'
      }
    } catch (error) {
      $.ui.log(`claudeglass: hold ${String(error)}`, { to: 'debug' })
    }

    // The keep list goes to the summariser on every summary, after whatever the user typed after /compact.
    let given = e
    // Whether the summariser was told what to keep, by this hook or by a /compact that already carried the list.
    let instructed: 0 | 1 = 0
    try {
      // A /compact the page drafted already carries the list, so it is not sent twice.
      const drafted = INSTRUCT_LEAD !== '' && (e.instructions ?? '').trimStart().startsWith(INSTRUCT_LEAD)
      const keep = drafted ? '' : await keepFor($, e.messages)
      if (drafted || keep !== '') instructed = 1
      if (keep !== '') given = { ...e, instructions: [e.instructions, keep].filter(Boolean).join('\n\n') }
    } catch (error) {
      $.ui.log(`claudeglass: keep instructions ${String(error)}`, { to: 'debug' })
    }
    const result = await next(given)
    // A summary made ahead installs nothing yet: there is nothing to check, record or say.
    if (trigger === 'precompute' || result.skip !== undefined) return result
    try {
      const summary = result.messages.find(message => message.role === 'user' && message.text.trimStart().startsWith('This session is being continued'))?.text ?? result.messages[0]?.text ?? ''
      const now = await $.clock.now()
      const was = sound(await read($, reviewAtom))
      const done = reviewCompaction({
        messages: e.messages,
        summary,
        pins: await read($, pinsAtom),
        claudeMd: await claudeMdOf($),
        cwd: memory.cwd,
        readded: was.readded,
        trigger,
        now,
        before: result.tokensBefore ?? null,
        after: result.tokensAfter ?? null,
      })
      // The count of replies so far is kept with the summary: the band says Kept until the next reply ends.
      const replies = soundSnap(await read($, snapAtom)).replies
      await update($, reviewAtom, (was): GlassReview => withSummary(sound(was), done.reviewed, done.row, replies))
      // The Detailed view says how the context changed and what the summary cost, when Claude Code reported its usage.
      let detail: BoundaryDetail | null = null
      if ((await read($, detailedAtom)) === true) {
        const snap = soundSnap(await read($, snapAtom))
        const settings = soundSettings(await read($, settingsAtom))
        const rates = result.usage === undefined ? null : ratesFor(settings.models, snap.model)
        const usd = result.usage === undefined || rates === null ? null : priceUsage(rates, result.usage, settings.ttl)
        detail = { before: result.tokensBefore ?? null, after: result.tokensAfter ?? null, amount: usd === null ? null : amountOfAfterAbout(usd, snap, settings) }
      }
      const line = boundaryLine(done.counts, saidTotal(done.reviewed.items), detail)
      if (line !== null) $.ui.log(line)
      // `referenced` is the receipts still in the review that are settled as referenced, as of this summary.
      memory.outbox.push({ ts: now, kind: trigger, counts: done.counts, instructed, referenced: referencedCount(was) })
      // The Summarise press that asked for this summary learns what it kept.
      if (memory.summarising !== null) memory.summarising.after = done.counts.kept
      memory.summarising = null
      memory.pinnable = null
    } catch (error) {
      $.ui.log(`claudeglass: compaction review ${String(error)}`, { to: 'debug' })
    }
    return result
  }).catch(($, e, next) => next(e))

  // A /clear starts the keep list over; pins do not survive it.
  on('session.end', { reason: 'clear' }, async ($, e, next) => {
    try {
      const { cwd, outbox, tweaks, settled } = memory
      forgetMemory()
      memory.cwd = cwd
      memory.outbox = outbox
      memory.tweaks = tweaks
      // A re-add that settled on the last reply before the /clear still gives its press the `after` at the next drain.
      memory.settled = settled
      await update($, pinsAtom, () => [])
      await update($, reviewAtom, () => emptyReview())
    } catch (error) {
      $.ui.log(`claudeglass: clear ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  })

  // The counts of a summary go to the sensor when the next reply ends.
  onTurnEnd(async ({ snap }) => {
    drainSensor(snap)
  })
}
