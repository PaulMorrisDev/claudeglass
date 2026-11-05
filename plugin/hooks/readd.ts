import { atom, read } from 'claude-code'
import type { Register } from 'claude-code'

import { emptyReview, sound } from './compaction-memory'
import { COPY } from './generated/copy.js'
import { addBackNote } from './lib/coverage.js'

// Re-add: after a summary lost items, one press (the page's kit.readd) puts them in the review's `readded`. From then
// on every request of the main session carries one extra system prompt section that tells Claude them again, until a
// summary holds them and compaction.ts drops them from `readded`. Nothing is sent for the user and no prompt box is
// touched. The section's text is built from the items alone, never from times, counts or status: any change in it
// rewrites the prompt cache for the rest of the conversation.

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const reviewAtom = atom({ plugin: 'claudeglass', key: 'review' } as const, emptyReview())

/** The section's id: the same on every request, so a hook above can find it. */
export const READD_SECTION = 'claudeglass:readd'
const LABELS = { files: COPY['compaction.label.files'], values: COPY['compaction.label.values'] }

export const register: Register = on => {
  // The matcher (every model) is what lets this and instructions.ts both hook prompt.compose: two without one are refused.
  on('prompt.compose', { model: /./ }, async ($, e, next) => {
    const result = await next(e)
    try {
      // A teammate has its own conversation: what the main session lost is not its to be told.
      if (e.traits.includes('teammate')) return result
      const text = addBackNote(sound(await read($, reviewAtom)).readded, COPY['compaction.addback'], LABELS)
      if (text === '') return result
      return { ...result, sections: [...result.sections.filter(one => one.id !== READD_SECTION), { id: READD_SECTION, text, scope: 'session' }] }
    } catch (error) {
      $.ui.log(`claudeglass: re-add ${String(error)}`, { to: 'debug' })
    }
    return result
  }).catch(($, e, next) => next(e))
}
