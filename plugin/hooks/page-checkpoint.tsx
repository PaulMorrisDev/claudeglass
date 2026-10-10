import type { RenderElement } from 'claude-code'

import type { GlassKeepItem, GlassMark, GlassReviewed, GlassReviewUi } from '../types'
import { isTicked, keepCandidates, lostItems, lostSaid, MAX_PINS, memory, newPin, splitKeep, SUGGESTED_SHOWN, tickedItems } from './compaction-memory'
import { formatTokens, localTime, percent } from './fmt'
import { COPY } from './generated/copy.js'
import { judgePrompt, MARK_SYMBOL, parseJudge, saidTotal, tally } from './lib/coverage.js'
import { buildKeepList } from './lib/keeplist.js'
import { fill, joinParts } from './lib/text.js'
import type { PageContext } from './pane'
import { figureRow, heading, ROW_CELLS } from './rows'

// The Checkpoint pane's page: three sections in the order of the summary moment. The header says how full the
// context is and offers the one way to summarise now. The keep list is what the next summary is told to carry,
// as ticks. After a summary, Last summary says what it lost, puts it back in one press, and checks the result with
// a model on request. Every Button has its states: busy while its action runs (the review's `ui.action`),
// dim when there is nothing to do. The hold switch, Startup, the dashboard link and the meanings are the footer's
// (pane.tsx).

/**
 * The model the optional check asks, by its alias: Claude Code resolves it to the current Haiku and checks it against
 * the user's allowlist, where a full id may be one they cannot use.
 */
const HAIKU = 'haiku'
/** The check's answer is one short line per item. */
const JUDGE_ANSWER_TOKENS = 600
/** What a Button's frame takes beyond its label (`[ ` and ` ]`, lib/band.js BUTTON_PADDING); the ticks are plain, the Unpin Button is not. */
const FRAME = 4
/** The tick (`[x] `) at the start of a keep row, and the gap before what sits at its right edge. */
const TICK_CELLS = 4
const ROW_GAP = 2
const KIND_CELLS = Math.max(...(['pin', 'instruction', 'decision', 'error', 'task', 'file', 'value'] as const).map(kind => COPY[`checkpoint.kind.${kind}`].length))
/**
 * What is left of a keep row for the item's text: the row less the tick (with the Button frame counted, so a native
 * Button's padding fits too), the gap, and the kind label; a pin's row also holds the gap and the Unpin Button.
 */
function textChars(kind: GlassKeepItem['kind']): number {
  const right = kind === 'pin' ? COPY['checkpoint.kind.pin'].length + ROW_GAP + FRAME + COPY['checkpoint.unpin'].length : KIND_CELLS
  return ROW_CELLS - FRAME - TICK_CELLS - ROW_GAP - right
}
/** What a press clears: the note under the section whose press set it. */
const NO_NOTE = { note: null, noteFor: null } as const
/** The most lost items listed one by one; Re-add puts back all of them, and the rest are counted. */
const LOST_SHOWN = 5
const EARLIER_SHOWN = 8

type Copy = keyof typeof COPY

function trim(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`
}

/** The instructions and decisions in a prompt, as one line to pin, or null when it holds none. */
export function pinnable(text: string): string | null {
  const found = buildKeepList([{ role: 'user', text }], {})
    .filter(item => item.kind === 'instruction' || item.kind === 'decision')
    .map(item => item.text.replace(/[.!]+$/, ''))
  return found.length === 0 ? null : found.join('; ')
}

/** Where the dashboard lists the summaries, or null when its address isn't known. */
export function dashboardAddress(base: string | null): string | null {
  return base === null ? null : `${base}/#/spend/usage?t=compactions.compactions_summary`
}

function triggerWord(trigger: GlassReviewed['trigger']): string {
  return COPY[`checkpoint.trigger.${trigger}` as Copy]
}

/** The words for a put-back item's receipt: not yet seen, referenced, or not referenced. */
function receiptWord(referenced: boolean | null | undefined): string {
  if (referenced === true) return COPY['checkpoint.receipt.referenced']
  if (referenced === false) return COPY['checkpoint.receipt.unreferenced']
  return COPY['checkpoint.receipt.waiting']
}

export function render(context: PageContext): RenderElement {
  const { table, review, pins, kit, view } = context
  const { Box, Text, Button } = table
  const ui: GlassReviewUi = review.ui
  const after = review.after
  const action = ui.action ?? null
  const working = view.snap.turnAt !== null

  /** Changes what the page remembers; reads the state as it is when pressed. A press that is not the one setting a note passes NO_NOTE. */
  const patch = (change: Partial<GlassReviewUi>) => kit.updateReview(was => ({ ...was, seen: true, ui: { ...was.ui, ...change } }))
  const toggle = (id: string) => kit.updateReview(was => ({ ...was, ui: { ...was.ui, ...NO_NOTE, toggled: was.ui.toggled.includes(id) ? was.ui.toggled.filter(one => one !== id) : [...was.ui.toggled, id] } }))

  // The keep list: what the next summary is told, the same choice the hook makes (compaction-memory.ts tickedItems).
  const items = keepCandidates(review, pins)
  const { suggested, more } = splitKeep(items, review.readded, SUGGESTED_SHOWN)
  const chosen = tickedItems(review, pins)
  const nothing = suggested.length === 0 && more.length === 0

  // What the summary lost (the things the user said that it dropped or kept in part), and of those what is still to put back.
  const lost = after === null ? [] : lostSaid(after)
  const back = new Set(review.readded.map(item => item.id))
  const left = lostItems(review)
  /** The section a note is drawn under: the one whose press set it. */
  const noteAt = ui.note === null ? null : (ui.noteFor ?? 'summarise')

  // Header: how full the context is, and the one way to summarise now.
  const reading = view.snap.reading
  // With no count of replies yet the band's own words say where Claude summarises.
  const replies =
    reading === null
      ? ''
      : view.replies === null || view.replies < 1
        ? fill(COPY['band.point'], { tokens: formatTokens(reading.point) })
        : view.replies === 1
          ? COPY['health.replies.one']
          : fill(COPY['health.replies'], { replies: view.replies })
  const where = reading === null ? '' : joinParts([fill(COPY['checkpoint.context'], { pct: percent(reading.pct) }), replies])
  const summarising = action === 'summarise'
  const summariseLabel = summarising ? COPY['health.busy'] : chosen.length === 0 ? COPY['checkpoint.summarise.none'] : chosen.length === 1 ? COPY['checkpoint.summarise.one'] : fill(COPY['checkpoint.summarise'], { count: chosen.length })
  const readdable = left.length > 0 && action === null
  // Two primary Buttons would not say which to press: while Re-add is offered, Summarise steps back.
  const offered = left.length > 0
  const inFlight = summarising || working

  const header = (
    <Box key="header" flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={2}>
        <Box flexShrink={1} minWidth={0}>
          <Text wrap="truncate">{where}</Text>
        </Box>
        <Button
          key="summarise"
          variant={offered ? 'secondary' : 'primary'}
          hotkey="s"
          {...(readdable ? {} : { autoFocus: true as const })}
          dimColor={inFlight}
          label={summariseLabel}
          onPress={() => void (inFlight ? Promise.resolve() : kit.summarise())}
        />
      </Box>
      {summarising ? <Text dimColor>{COPY['checkpoint.summarise.running']}</Text> : null}
      {!summarising && working ? <Text dimColor>{COPY['health.busy.reply']}</Text> : null}
      {noteAt === 'summarise' ? <Text dimColor>{ui.note}</Text> : null}
    </Box>
  )

  // Keep list: a tick Button per item with its kind, five rows and the rest behind "Show n more".
  const tick = (item: GlassKeepItem, isSuggested: boolean): RenderElement => {
    const on = isTicked(ui.toggled, item.id, isSuggested)
    const kind = COPY[`checkpoint.kind.${item.kind}` as Copy]
    return (
      <Box key={`row:${item.id}`} flexDirection="row" justifyContent="space-between" gap={2} width={ROW_CELLS}>
        <Button key={`tick:${item.id}`} plain label={`${on ? '[x]' : '[ ]'} ${trim(item.text, textChars(item.kind))}`} dimColor={!on} onPress={() => void toggle(item.id)} />
        <Box flexShrink={0} flexDirection="row" gap={2}>
          <Text color="subtle">{kind}</Text>
          {item.kind === 'pin' ? <Button key={`unpin:${item.id}`} label={COPY['checkpoint.unpin']} dimColor onPress={() => void Promise.all([kit.updatePins(was => was.filter(one => one.id !== item.id)), patch(NO_NOTE)])} /> : null}
        </Box>
      </Box>
    )
  }
  const ticks = [...suggested.map(item => tick(item, true)), ...(ui.list ? more.map(item => tick(item, false)) : [])]
  const count = chosen.length === 1 ? COPY['checkpoint.keep.count.one'] : fill(COPY['checkpoint.keep.count'], { count: chosen.length })

  const line = memory.pinnable === null ? null : pinnable(memory.pinnable)
  const pin = line === null || pins.length >= MAX_PINS || pins.some(one => one.text === line) ? null : newPin(line)
  const keepButtons: RenderElement[] = []
  if (pin !== null) {
    keepButtons.push(
      <Button
        key="pin-last"
        label={COPY['checkpoint.pin.last']}
        dimColor
        onPress={() => void kit.updatePins(was => (was.some(one => one.id === pin.id) || was.length >= MAX_PINS ? was : [...was, pin])).then(() => patch({ note: COPY['compaction.pin.done'], noteFor: 'pin' }))}
      />,
    )
  }
  if (more.length > 0) {
    keepButtons.push(<Button key="more" label={ui.list ? COPY['checkpoint.show.fewer'] : fill(COPY['checkpoint.show.more'], { count: more.length })} dimColor onPress={() => void patch({ list: !ui.list, ...NO_NOTE })} />)
  }
  const keep = (
    <Box key="keep" flexDirection="column">
      {heading(table, 'keep-title', COPY['checkpoint.keep.title'], nothing ? '' : count)}
      {nothing ? <Text dimColor>{COPY['checkpoint.keep.empty']}</Text> : ticks}
      {keepButtons.length > 0 ? (
        <Box key="keep-actions" flexDirection="row" gap={2}>
          {keepButtons as never}
        </Box>
      ) : null}
      {noteAt === 'pin' ? <Text dimColor>{ui.note}</Text> : null}
    </Box>
  )

  const sections: RenderElement[] = [header, keep]
  if (after !== null) sections.push(last(context, lost, left, back, readdable, patch, noteAt))
  return (
    <Box flexDirection="column" gap={1}>
      {sections as never}
    </Box>
  )
}

/** Last summary: what it lost, each item's receipt once put back, and the buttons that act on it. */
function last(
  context: PageContext,
  lost: GlassKeepItem[],
  left: GlassKeepItem[],
  back: Set<string>,
  readdable: boolean,
  patch: (change: Partial<GlassReviewUi>) => Promise<void>,
  noteAt: NonNullable<GlassReviewUi['noteFor']> | null,
): RenderElement {
  const { table, review, kit } = context
  const { Box, Text, Button } = table
  const after = review.after as GlassReviewed
  const ui = review.ui
  const action = ui.action ?? null
  // What the summary kept and lost of the things the user said; the two add up to the things checked.
  const lostNow = lost.length
  const keptNow = saidTotal(after.items) - lostNow

  const rows: RenderElement[] =
    lost.length === 0
      ? [<Text key="last-ok">{COPY['checkpoint.last.ok']}</Text>]
      : lost.slice(0, LOST_SHOWN).map(item => {
          const mark = (after.marks[item.id] ?? 'missing') as GlassMark
          const figure = back.has(item.id) ? receiptWord(review.receipts[item.id]?.referenced) : ''
          return figureRow(table, `lost:${item.id}`, trim(item.text, 100), figure, { mark: { glyph: MARK_SYMBOL[mark], color: 'error' } })
        })

  if (lost.length > LOST_SHOWN) rows.push(<Text key="lost-more" dimColor>{fill(COPY['compaction.more'], { count: lost.length - LOST_SHOWN })}</Text>)

  // Re-add: primary while there is something to put back; busy while it runs; once everything lost is back it is dim text with the time.
  const readding = action === 'readd'
  const readdLabel = readding ? COPY['checkpoint.readd.busy'] : fill(COPY[left.length === 1 ? 'health.readd.one' : 'health.readd'], { count: left.length })
  const readdAt = review.readded.reduce((newest, item) => Math.max(newest, review.receipts[item.id]?.at ?? 0), 0)
  const readdTime = readdAt > 0 ? localTime(readdAt) : null
  const readd = async () => {
    await patch({ action: 'readd', ...NO_NOTE })
    try {
      await kit.readd()
      await patch({ note: COPY['checkpoint.readd.done'], noteFor: 'readd' })
    } finally {
      await patch({ action: null })
    }
  }

  // Check with a model: today's judge, one press; the marks update and the note says what it used.
  const judgeable = after.items.filter(item => !item.safe)
  const checking = action === 'check' || ui.busy
  const check = async () => {
    const prompt = judgePrompt(judgeable, after.summary)
    await patch({ action: 'check', busy: true, ...NO_NOTE })
    try {
      const answer = await kit.ask(HAIKU, prompt, JUDGE_ANSWER_TOKENS)
      if ('reason' in answer) {
        await patch({ note: COPY['checkpoint.check.failed'], noteFor: 'check' })
        return
      }
      const marks = { ...after.marks, ...parseJudge(answer.text, judgeable) }
      const done = tally(judgeable, marks).counts
      const note = fill(COPY['checkpoint.check.done'], { count: judgeable.length, found: done.found, partly: done.partly, missing: done.missing })
      await kit.updateReview(was => {
        const judged = was.after === null ? null : { ...was.after, marks, judged: true, judgedTokens: answer.input + answer.output }
        // The model's marks move the count the band, the toast and the sensor share: what was said and is now missing or partly.
        return { ...was, missing: judged === null ? was.missing : lostSaid(judged).length, after: judged, ui: { ...was.ui, note, noteFor: 'check' as const } }
      })
    } finally {
      await patch({ action: null, busy: false })
    }
  }

  // The earlier summaries: every row but the newest, which the section above is about.
  const earlier = [...review.history].reverse().slice(1)
  const earlierLabel = ui.earlier ? COPY['checkpoint.earlier.hide'] : earlier.length === 1 ? COPY['checkpoint.earlier.one'] : fill(COPY['checkpoint.earlier'], { count: earlier.length })
  const buttons: RenderElement[] = []
  if (lost.length > 0) {
    buttons.push(
      left.length === 0 && !readding ? (
        <Text key="readd-at" dimColor>
          {fill(COPY['checkpoint.readd.at'], { time: readdTime ?? '' }).trim()}
        </Text>
      ) : (
        <Button key="readd" variant="primary" hotkey="r" {...(readdable ? { autoFocus: true as const } : {})} dimColor={readding} label={readdLabel} onPress={() => void (readdable ? readd() : Promise.resolve())} />
      ),
    )
  }
  // Check with a model: once it has run the Button gives way to what it used.
  if (after.judged) {
    buttons.push(
      <Text key="check-at" dimColor>
        {fill(COPY['checkpoint.check.at'], { tokens: formatTokens(after.judgedTokens ?? 0) })}
      </Text>,
    )
  } else {
    buttons.push(
      <Button
        key="check"
        variant="secondary"
        dimColor={checking || judgeable.length === 0}
        label={checking ? COPY['checkpoint.check.busy'] : COPY['checkpoint.check']}
        onPress={() => void (checking || judgeable.length === 0 ? Promise.resolve() : check())}
      />,
    )
  }
  if (earlier.length > 0) buttons.push(<Button key="earlier" label={earlierLabel} dimColor onPress={() => void patch({ earlier: !ui.earlier, ...NO_NOTE })} />)

  const time = localTime(after.ts)
  const history = ui.earlier
    ? earlier.slice(0, EARLIER_SHOWN).map(row => (
        <Text key={`history:${row.ts}`} dimColor>
          {fill(COPY['checkpoint.earlier.row'], { time: localTime(row.ts) ?? '', trigger: triggerWord(row.trigger), kept: row.kept, missing: row.missing })}
        </Text>
      ))
    : []

  const counts = fill(COPY[review.readded.length === 0 ? 'checkpoint.last.counts' : 'checkpoint.last.counts.readded'], { kept: keptNow, missing: lostNow, readded: review.readded.length })

  return (
    <Box key="last" flexDirection="column">
      {heading(table, 'last-title', joinParts([COPY['checkpoint.last.title'], time, triggerWord(after.trigger)]), counts)}
      {rows as never}
      <Box key="last-actions" flexDirection="row" gap={2}>
        {buttons as never}
      </Box>
      {noteAt === 'readd' || noteAt === 'check' ? <Text dimColor>{review.ui.note}</Text> : null}
      {history.length > 0 ? <Box key="history" flexDirection="column">{history as never}</Box> : null}
    </Box>
  )
}
