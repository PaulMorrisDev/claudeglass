import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { GlassPin } from '../types'
import { emptyReview, isSummaryText, MAX_PINS, memory, newPin, sound } from './compaction-memory'
import { COPY } from './generated/copy.js'
import { squash } from './lib/keeplist.js'
import { pinnable } from './page-checkpoint'
import { OPEN_ARGS } from './pane'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const pinsAtom = atom({ plugin: 'claudeglass', key: 'pins' } as const, [] as GlassPin[])
const reviewAtom = atom({ plugin: 'claudeglass', key: 'review' } as const, emptyReview())

// Two buttons in the transcript's own rows, each on at most one row so a key is
// never used twice: [p] pin on the newest prompt that holds an instruction or a
// decision, and [v] review under the summary row while the summary may have
// dropped something. Pinning is also /glass pin <text> (command.ts) and a press
// on an item in the Compaction page.

export const register: Register = on => {
  on('ui.render', { component: 'UserMessage' }, async ($, e, next) => {
    const base = await next(e)
    const text = squash(e.props.text)
    const review = sound(await read($, reviewAtom))

    let button: 'pin' | 'review' | null = null
    if (isSummaryText(text)) {
      // The summary row: only the newest summary, and only while it may have dropped something.
      if (review.after !== null && review.missing > 0 && squash(review.after.summary) === text) button = 'review'
    } else if (memory.pinnable !== null && memory.pinnable === text) {
      const pins = await read($, pinsAtom)
      const line = pinnable(text)
      if (line !== null && pins.length < MAX_PINS && !pins.some(one => one.text === line)) button = 'pin'
    }
    if (button === null) return base

    const { Box, Button } = $.ui.resolve(e)
    const press =
      button === 'pin'
        ? async () => {
            const one = newPin(pinnable(text) ?? '')
            if (one === null) return
            await update($, pinsAtom, was => (was.some(have => have.id === one.id) || was.length >= MAX_PINS ? was : [...was, one]))
            $.ui.toast(COPY['compaction.pin.done'])
          }
        : async () => {
            await update($, reviewAtom, was => ({ ...sound(was), seen: true, ui: { ...sound(was).ui, view: 'after' as const, note: null } }))
            await $.ui.open(OPEN_ARGS)
          }
    return (
      <Box flexDirection="column">
        {base}
        <Box flexDirection="row" gap={1}>
          {button === 'pin' ? (
            <Button key="pin" label={COPY['compaction.pin']} hotkey="p" onPress={() => void press()} />
          ) : (
            <Button key="review" label={COPY['compaction.review']} hotkey="v" onPress={() => void press()} />
          )}
        </Box>
      </Box>
    )
  })
}
