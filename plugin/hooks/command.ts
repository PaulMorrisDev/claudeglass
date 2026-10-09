import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import { COPY } from './generated/copy.js'
import { OPEN_ARGS } from './pane'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)

// /glass opens the pane; /glass detail turns the Detailed view on or off. The
// command is declared in track.ts at session.start. It is immediate, so it
// works while a reply is still being written.

export const register: Register = on => {
  on('command.run', { command: 'glass' }, async ($, e) => {
    try {
      if (e.args.trim() === 'detail') {
        const next = !(await read($, detailedAtom))
        await update($, detailedAtom, () => next)
        await $.store.set('detailed', next)
        return { text: next ? COPY['pane.detail.on'] : COPY['pane.detail.off'] }
      }
      const opened = await $.ui.open(OPEN_ARGS)
      return { text: opened.isPlaced ? COPY['pane.opened'] : COPY['pane.failed'] }
    } catch (error) {
      $.ui.log(`claudeglass: command ${String(error)}`, { to: 'debug' })
      return { text: COPY['pane.failed'] }
    }
  })
}
