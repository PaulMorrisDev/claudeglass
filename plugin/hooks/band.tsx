import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import { emptyReview, endSummarise, lostCount, memory, noteReadd, noteSummarise, readdMissing, sound, tickedItems } from './compaction-memory'
import { percent } from './fmt'
import { COPY } from './generated/copy.js'
import { saidTotal } from './lib/coverage.js'
import { buttonCells, healthLine, healthOf, LEAD_CELLS, WORD_GAP } from './lib/band.js'
import type { Health } from './lib/band.js'
import { fill } from './lib/text.js'
import { formatTokens } from './lib/units.js'
import { emptySnap } from './live'
import { OPEN_ARGS } from './pane'
import { drawView } from './view'
import type { GlassPin, GlassReview } from '../types'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)
const reviewAtom = atom({ plugin: 'claudeglass', key: 'review' } as const, emptyReview())
const pinsAtom = atom({ plugin: 'claudeglass', key: 'pins' } as const, [] as GlassPin[])

// The band above the prompt: one health line and one Button, right-aligned. The line
// says how the session stands (Fine, Tight, Review or Kept) and the Button is the one
// thing to do about it: open Glass, summarise now, or put back what a summary lost.
// The words are lib/band.js's; the cost, the 5-hour limit, agents and the cache are
// the dashboard's.

const COLORS: Record<Health, string> = { fine: 'success', kept: 'success', tight: 'warning', review: 'error' }
const NAMES: Record<Health, string> = { fine: COPY['health.fine'], kept: COPY['health.kept'], tight: COPY['health.tight'], review: COPY['health.review'] }
const DOT = '●'
/** Cells the state word is padded to, so the line starts in the same column in every state. */
const WORD_CELLS = 7
/** The least gap between the line and the Button. */
const BUTTON_GAP = 2
/** The key of the Hold option in $.store (compaction.ts's HOLD_KEY). */
const HOLD_KEY = 'hold'

/** The last summary whose loss was announced (a render may not write $.state, so the toast remembers it here). */
let toasted: number | null = null
/** A Summarise press is on its way (the press that began it has not finished): a second press before the band redraws does nothing. */
let summarising = false

/** Items the last summary was checked against: the things the user said. */
function totalOf(review: GlassReview): number {
  return review.after === null ? review.missing : saidTotal(review.after.items)
}

function summariseLabel(count: number): string {
  return count < 1 ? COPY['health.summarise.none'] : count === 1 ? COPY['health.summarise.one'] : fill(COPY['health.summarise'], { count })
}

function readdLabel(count: number): string {
  return count === 1 ? COPY['health.readd.one'] : fill(COPY['health.readd'], { count })
}

export const register: Register = on => {
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const base = await next(e)
    if (e.props.hasSurvey) return base
    const view = drawView(await read($, snapAtom), await read($, settingsAtom), await read($, detailedAtom), await $.clock.now(), e.props.isWorking)
    const review = sound(await read($, reviewAtom))
    const pins = await read($, pinsAtom)

    // One toast for each summary that lost something, and none when the user has already looked.
    if (review.ts === null) toasted = null
    else if (review.after !== null && review.missing > 0 && lostCount(review) > 0 && toasted !== review.ts) {
      toasted = review.ts
      if (!review.seen) $.ui.toast(fill(COPY['health.lost.toast'], { missing: review.missing, total: totalOf(review) }))
    }

    const reading = view.snap.reading
    const total = totalOf(review)
    const state = healthOf({
      reading: reading === null ? null : reading.state,
      summarised: review.after !== null,
      missing: review.missing,
      pending: lostCount(review),
      seen: review.seen,
      total,
      replies: view.snap.replies,
      repliesAt: review.repliesAt ?? null,
    })
    if (reading === null || state === null) return base

    const hold = (await $.store.get(HOLD_KEY)) === true
    const running = review.ui.action ?? null
    const busy = e.props.isWorking || running !== null

    // Opening Glass is looking at the last summary, so the band leaves Review.
    const open = () => {
      void update($, reviewAtom, was => ({ ...sound(was), seen: true }))
      void $.ui.open(OPEN_ARGS)
    }

    // The items the next summary is told to keep: what the Summarise Button says it keeps.
    const count = tickedItems(review, pins).length

    // Summarise: a compaction from this press. It throws while a reply runs, so say so then.
    const summarise = async () => {
      if (busy || summarising) return
      summarising = true
      try {
        await update($, reviewAtom, was => ({ ...sound(was), ui: { ...sound(was).ui, action: 'summarise' as const, note: null, noteFor: null } }))
        // The sensor counts the press; the summary it starts fills in what was kept (compaction.ts).
        const mine = noteSummarise(count, await $.clock.now())
        try {
          const done = await $.session.compact()
          if ('skip' in done && done.skip) $.ui.toast(fill(COPY['checkpoint.summarise.skipped'], { reason: done.skip }))
        } catch {
          $.ui.toast(COPY['health.busy.reply'])
        } finally {
          // A summary that went through filled in the press's `after` during compact() (compaction.ts); none is coming now.
          endSummarise(mine)
          await update($, reviewAtom, was => ({ ...sound(was), ui: { ...sound(was).ui, action: null } }))
        }
      } finally {
        summarising = false
      }
    }

    // Re-add: what the Checkpoint pane's Re-add does (Claude is told the lost items from its next request on, readd.ts).
    const readd = async () => {
      const now = await $.clock.now()
      let added = 0
      let ids: string[] = []
      await update($, reviewAtom, was => {
        const current = sound(was)
        const put = readdMissing(current, now)
        added = put.readded.length - current.readded.length
        ids = put.readded.slice(current.readded.length).map(item => item.id)
        return { ...put, seen: true, ui: { ...put.ui, note: null, noteFor: null } }
      })
      // The sensor counts these as added back when the next reply ends (drainSensor).
      memory.added += added
      noteReadd(ids, now)
      if (added > 0) $.ui.toast(COPY['checkpoint.readd.done'])
    }

    const table = $.ui.resolve(e)
    const { Box, Text, Button } = table

    // The one Button: what to do about the state.
    const action =
      state === 'review'
        ? { key: 'readd', label: running === 'readd' ? COPY['checkpoint.readd.busy'] : readdLabel(lostCount(review)), hotkey: 'r', primary: true, dim: running === 'readd', press: () => void readd() }
        : state === 'tight' && hold
          ? { key: 'review', label: COPY['health.review.hold'], hotkey: 's', primary: true, dim: false, press: open }
          : state === 'tight'
            ? { key: 'summarise', label: running === 'summarise' ? COPY['health.busy'] : summariseLabel(count), hotkey: 's', primary: true, dim: busy, press: () => void summarise() }
            : { key: 'open', label: COPY['band.open'], hotkey: 'g', primary: false, dim: false, press: open }

    const room = e.props.bodyColumns - buttonCells(action.label) - BUTTON_GAP - LEAD_CELLS - WORD_CELLS - 2 * WORD_GAP
    const line = healthLine(
      state,
      { pct: percent(reading.pct), replies: view.replies, point: reading.point === null ? null : formatTokens(reading.point), missing: review.missing, total },
      {
        full: COPY['band.full'],
        replies: COPY['health.replies'],
        repliesOne: COPY['health.replies.one'],
        point: COPY['band.point'],
        autoOff: COPY['band.auto.off'],
        lost: COPY['health.lost'],
        kept: COPY['health.kept.all'],
        keptOne: COPY['health.kept.one'],
      },
      room,
    )
    const color = COLORS[state]

    return (
      <Box flexDirection="column">
        {base}
        <Box flexDirection="row" justifyContent="space-between" gap={BUTTON_GAP}>
          <Box key="health" flexDirection="row" gap={WORD_GAP}>
            <Text key="dot" color={color}>
              {DOT}
            </Text>
            <Text key="state" color={color} bold>
              {NAMES[state].padEnd(WORD_CELLS)}
            </Text>
            {line === '' ? null : (
              <Text key="line" wrap="truncate">
                {line}
              </Text>
            )}
          </Box>
          <Button
            key={action.key}
            label={action.label}
            hotkey={action.hotkey}
            {...(action.primary ? { variant: 'primary' as const } : {})}
            {...(action.dim ? { dimColor: true } : {})}
            onPress={action.press}
          />
        </Box>
      </Box>
    )
  })
}
