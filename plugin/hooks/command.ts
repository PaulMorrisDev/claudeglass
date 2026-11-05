import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { GlassPin, GlassReview } from '../types'
import { emptyReview, MAX_PINS, newPin, pinsMatching, sound } from './compaction-memory'
import { rememberedDraft } from './draft'
import { COPY } from './generated/copy.js'
import { fill } from './lib/text.js'
import { OPEN_ARGS, STARTUP_OPEN_ARGS } from './pane'
import { runTighten, TIGHTEN_OPEN } from './tighten'
import type { TightenKit } from './tighten'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)
const explainAtom = atom({ plugin: 'claudeglass', key: 'explain' } as const, null)
const pinsAtom = atom({ plugin: 'claudeglass', key: 'pins' } as const, [] as GlassPin[])
const reviewAtom = atom({ plugin: 'claudeglass', key: 'review' } as const, emptyReview())

// /glass opens the Checkpoint pane and /glass startup the Startup pane; /glass detail turns the Detailed view on or off;
// /glass pin <text> adds a pin to the compaction review's keep list and
// /glass unpin <words> (or "all") removes the pins that hold those words. The
// command is declared in track.ts at session.start. It is immediate, so it
// works while a reply is still being written. /glass tighten has Haiku rewrite
// the draft shorter (tighten.tsx; /glass tighten <text> tightens that text); /glass guard turns the question before a
// costly send on or off (draft.ts).

/** The model, the prompt box and the pane, as /glass tighten needs them. */
function tightenKit($: EngineInterface): TightenKit {
  return {
    box: async () => (await $.prompt.read()).text,
    remembered: rememberedDraft,
    complete: async (system, prompt, maxTokens) => {
      try {
        const answer = await $.model.complete({ model: 'haiku', system, prompt, maxTokens, effort: 'low', timeoutMs: 60000 })
        if (!answer.isAnswered) return { reason: answer.reason }
        return { text: answer.text, input: answer.usage.input_tokens, output: answer.usage.output_tokens }
      } catch {
        // No model call in this Claude Code, or it refused the request.
        return { reason: 'unavailable' }
      }
    },
    open: async () => (await $.ui.open(TIGHTEN_OPEN)).isPlaced,
    redraw: () => {
      void $.ui.invalidate('ui.render')
    },
    debug: text => {
      $.ui.log(text, { to: 'debug' })
    },
  }
}

export const register: Register = on => {
  on('command.run', { command: 'glass' }, async ($, e) => {
    try {
      if (e.args.trim() === 'detail') {
        const next = !(await read($, detailedAtom))
        await update($, detailedAtom, () => next)
        // The Settings view belongs to Detailed: leaving Detailed puts it away, so it doesn't come back unasked.
        if (!next) await update($, explainAtom, () => null)
        await $.store.set('detailed', next)
        return { text: next ? COPY['pane.detail.on'] : COPY['pane.detail.off'] }
      }
      const args = e.args.trim()
      if (args === 'pin' || args.startsWith('pin ')) {
        const one = newPin(args.slice(3))
        if (one === null) return { text: COPY['compaction.pin.empty'] }
        const pins = await read($, pinsAtom)
        if (pins.length >= MAX_PINS && !pins.some(have => have.id === one.id)) return { text: fill(COPY['compaction.pin.full'], { count: pins.length }) }
        await update($, pinsAtom, was => (was.some(have => have.id === one.id) ? was : [...was, one]))
        return { text: COPY['compaction.pin.done'] }
      }
      if (args === 'unpin' || args.startsWith('unpin ')) {
        const words = args.slice(5).trim()
        if (words === '') return { text: COPY['compaction.unpin.empty'] }
        const pins = await read($, pinsAtom)
        if (pins.length === 0) return { text: COPY['compaction.unpin.nopins'] }
        const gone = new Set(pinsMatching(pins, words).map(one => one.id))
        if (gone.size === 0) return { text: COPY['compaction.unpin.none'] }
        await update($, pinsAtom, was => was.filter(one => !gone.has(one.id)))
        return { text: gone.size === 1 ? COPY['compaction.unpin.one'] : fill(COPY['compaction.unpin.many'], { count: gone.size }) }
      }
      if (args === 'tighten' || args.startsWith('tighten ')) return { text: await runTighten(tightenKit($), args.slice(8)) }
      if (args === 'guard' || args === 'guard on' || args === 'guard off') {
        const enabled = args === 'guard' ? (await $.store.get('guard')) !== true : args === 'guard on'
        await $.store.set('guard', enabled)
        return { text: enabled ? COPY['guard.on'] : COPY['guard.off'] }
      }
      // Opening the Checkpoint pane is looking at the last summary, so the band leaves Review.
      if (args !== 'startup') await update($, reviewAtom, (was): GlassReview => ({ ...sound(was), seen: true }))
      const opened = await $.ui.open(args === 'startup' ? STARTUP_OPEN_ARGS : OPEN_ARGS)
      return { text: opened.isPlaced ? COPY['pane.opened'] : COPY['pane.failed'] }
    } catch (error) {
      $.ui.log(`claudeglass: command ${String(error)}`, { to: 'debug' })
      return { text: COPY['pane.failed'] }
    }
  })
}
