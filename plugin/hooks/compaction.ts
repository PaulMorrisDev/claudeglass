import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { GlassKeepItem, GlassPin, GlassReview } from '../types'
import { drainSensor, emptyReview, forgetMemory, keepList, memory, reviewCompaction, boundaryLine, sound, suggestCount, withSummary } from './compaction-memory'
import type { MessageLike } from './compaction-memory'
import { COPY } from './generated/copy.js'
import { holdAfterReply, holdStep, saidTotal } from './lib/coverage.js'
import { buildKeepList, squash } from './lib/keeplist.js'
import { fill } from './lib/text.js'
import { emptySnap } from './live'
import { onTurnEnd } from './track'

// Compaction review, the event side. Before a summary: the keep list follows the
// conversation (in $.state, for the page and the band). At a summary of the main
// session: the list as it stood is checked against the summary Claude Code wrote,
// the result goes to $.state, one line to the transcript, and counts to the sensor.
// Two opt-ins, both off until the user turns them on in the page: hold an automatic
// summary once for review, and tell Claude the pins again after a summary.
// Nothing the user or Claude wrote is written to disk: the keep list and the
// summary live in $.state and module memory only.

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const pinsAtom = atom({ plugin: 'claudeglass', key: 'pins' } as const, [] as GlassPin[])
const reviewAtom = atom({ plugin: 'claudeglass', key: 'review' } as const, emptyReview())

/** The two opt-ins live in $.store under these keys; the page turns them on and off. */
export const HOLD_KEY = 'hold'
export const READD_KEY = 'readd'
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
  const items = keepList(messages, pins, await claudeMdOf($))
  memory.pinnable = pinnableOf(messages)
  const review = sound(await read($, reviewAtom))
  const suggest = suggestCount(items)
  if (same(review.list, items) && review.suggest === suggest) return
  await update($, reviewAtom, (was): GlassReview => ({ ...sound(was), list: items, suggest }))
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
  on('session.compact', { trigger: /^(manual|auto)$/ }, async ($, e, next) => {
    if (e.agentId !== undefined || (e.trigger !== 'manual' && e.trigger !== 'auto')) return next(e)
    try {
      if (e.trigger === 'auto') {
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
      } else {
        memory.hold = 'none'
      }
    } catch (error) {
      $.ui.log(`claudeglass: hold ${String(error)}`, { to: 'debug' })
    }

    const result = await next(e)
    if (result.skip !== undefined) return result
    try {
      const summary = result.messages.find(message => message.role === 'user' && message.text.trimStart().startsWith('This session is being continued'))?.text ?? result.messages[0]?.text ?? ''
      const now = await $.clock.now()
      const done = reviewCompaction({
        messages: e.messages,
        summary,
        pins: await read($, pinsAtom),
        claudeMd: await claudeMdOf($),
        trigger: e.trigger,
        now,
        before: result.tokensBefore ?? null,
        after: result.tokensAfter ?? null,
      })
      await update($, reviewAtom, (was): GlassReview => withSummary(sound(was), done.reviewed, done.row))
      const line = boundaryLine(done.counts, saidTotal(done.reviewed.items))
      if (line !== null) $.ui.log(line)
      memory.outbox.push({ ts: now, kind: e.trigger, counts: done.counts })
      memory.pinnable = null
    } catch (error) {
      $.ui.log(`claudeglass: compaction review ${String(error)}`, { to: 'debug' })
    }
    return result
  }).catch(($, e, next) => next(e))

  // After a summary, tell Claude the pins again (an opt-in).
  on('classic.SessionStart', { source: 'compact' }, async ($, e, next) => {
    const result = await next(e)
    // A subagent's own summary is not the session's: its context is not given the pins.
    if (e.agent_id !== undefined) return result
    try {
      if ((await $.store.get(READD_KEY)) !== true) return result
      const pins = await read($, pinsAtom)
      if (pins.length === 0) return result
      const text = fill(COPY['compaction.readd'], { items: pins.map(pin => pin.text).join('; ') })
      return { ...result, additionalContext: [...(result.additionalContext ?? []), text] }
    } catch (error) {
      $.ui.log(`claudeglass: re-add pins ${String(error)}`, { to: 'debug' })
      return result
    }
  }).catch(($, e, next) => next(e))

  // A /clear starts the keep list over; pins do not survive it.
  on('session.end', { reason: 'clear' }, async ($, e, next) => {
    try {
      const { cwd, outbox } = memory
      forgetMemory()
      memory.cwd = cwd
      memory.outbox = outbox
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
