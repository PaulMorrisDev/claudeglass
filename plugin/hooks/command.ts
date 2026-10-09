import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { GlassBudget, GlassPin } from '../types'
import { runBudget } from './budget'
import { MAX_PINS, newPin } from './compaction-memory'
import { rememberedDraft } from './draft'
import { COPY } from './generated/copy.js'
import { emptySnap } from './live'
import { fallbackSettings } from './settings'
import { fill } from './lib/text.js'
import { OPEN_ARGS } from './pane'
import { runTighten, TIGHTEN_OPEN } from './tighten'
import type { TightenKit } from './tighten'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)
const pinsAtom = atom({ plugin: 'claudeglass', key: 'pins' } as const, [] as GlassPin[])
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const budgetAtom = atom({ plugin: 'claudeglass', key: 'budget' } as const, null as GlassBudget | null)

// /glass opens the pane; /glass detail turns the Detailed view on or off;
// /glass pin <text> adds a pin to the compaction review's keep list. The
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
      if (args === 'tighten' || args.startsWith('tighten ')) return { text: await runTighten(tightenKit($), args.slice(8)) }
      if (args === 'guard' || args === 'guard on' || args === 'guard off') {
        const enabled = args === 'guard' ? (await $.store.get('guard')) !== true : args === 'guard on'
        await $.store.set('guard', enabled)
        return { text: enabled ? COPY['guard.on'] : COPY['guard.off'] }
      }
      if (args === 'budget' || args.startsWith('budget ')) {
        const text = await runBudget(args.slice(6), {
          get: () => read($, budgetAtom),
          set: async budget => {
            await update($, budgetAtom, () => budget)
          },
          snap: () => read($, snapAtom),
          settings: async () => (await read($, settingsAtom)) ?? fallbackSettings(),
        })
        return { text }
      }
      const opened = await $.ui.open(OPEN_ARGS)
      return { text: opened.isPlaced ? COPY['pane.opened'] : COPY['pane.failed'] }
    } catch (error) {
      $.ui.log(`claudeglass: command ${String(error)}`, { to: 'debug' })
      return { text: COPY['pane.failed'] }
    }
  })
}
