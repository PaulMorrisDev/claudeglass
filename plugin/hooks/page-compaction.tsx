import type { Register, RenderElement } from 'claude-code'

import type { GlassKeepItem, GlassMark, GlassReviewed, GlassReviewUi, GlassSettings } from '../types'
import { isTicked, keepCandidates, memory, splitKeep, SUGGESTED_SHOWN, tickedItems } from './compaction-memory'
import { amount, formatTokens, localTime } from './fmt'
import { COPY } from './generated/copy.js'
import { compactKeep, judgePrompt, KEEP_MAX, MARK_SYMBOL, missingSaid, parseJudge, saidTotal, tally } from './lib/coverage.js'
import { priceUsage } from './lib/pricing.js'
import { fill, joinParts } from './lib/text.js'
import { addPage } from './pane'
import type { PageContext } from './pane'
import { figureRow, heading, ROW_CELLS } from './rows'

// The Compaction page. After a summary it says what the summary may have dropped and offers one note that puts it
// back; the summary, the full list and the earlier messages sit behind quiet buttons (one open at a time), and the
// history behind "More detail". The options are on the pane's Settings view and the dashboard link is the footer's. Before a summary it offers what the summary should keep as a handful of ticks and fills
// the prompt box with a /compact command. Nothing is ever sent.

/**
 * The model the optional check asks, by its alias: Claude Code resolves it to the current Haiku and checks it against
 * the user's allowlist, where a full id may be one they cannot use. The answer does not name the model it came from,
 * so the cost below is priced at the newest Haiku in the rates (see haikuRates).
 */
const HAIKU = 'haiku'
/** Characters per token the estimate before the check assumes. */
const CHARS_PER_TOKEN = 4
/** The check's answer is one short line per item. */
const JUDGE_ANSWER_TOKENS = 600
const LABELS = { files: COPY['compaction.label.files'], values: COPY['compaction.label.values'] }
/** What the first screen lists: the missing items after a summary, the suggested ticks before one. */
const MISSING_SHOWN = 4
const EARLIER_SHOWN = 8
const LABEL_CHARS = 100

type Copy = keyof typeof COPY

/**
 * The rates of the newest Haiku among the models priced, or null when there is none. The rates carry no alias,
 * so the 'haiku' alias is matched by name; the highest id (ids sort by their version numbers) is the newest.
 */
function haikuRates(models: GlassSettings['models']): GlassSettings['models'][string] | null {
  const ids = Object.keys(models).filter(id => id.toLowerCase().includes('haiku'))
  ids.sort((a, b) => b.localeCompare(a, 'en', { numeric: true }))
  const best = ids[0]
  return best === undefined ? null : (models[best] ?? null)
}

function trim(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`
}

/** The words for a checked item's mark; an item that is safe anyway is never listed, so it reads as found. */
function markWord(mark: GlassMark): string {
  return COPY[`compaction.mark.${mark === 'safe' ? 'found' : mark}` as Copy]
}

function isOn(ui: GlassReviewUi, item: GlassKeepItem, suggested: boolean): boolean {
  return isTicked(ui.toggled, item.id, suggested)
}

function trigger(reviewed: { trigger: GlassReviewed['trigger'] }): string {
  return COPY[`compaction.trigger.${reviewed.trigger}` as Copy]
}

export function dashboardAddress(base: string | null): string | null {
  return base === null ? null : `${base}/#/spend/usage?t=compactions.compactions_summary`
}

function render(context: PageContext): RenderElement {
  const { table, review, pins, kit, view } = context
  const { Box, Text, Button } = table
  const ui = review.ui
  const after = review.after
  const showAfter = after !== null && ui.view === 'after'
  const children: RenderElement[] = []
  /** The sections after the first, each its own Box; the page's column leaves a blank line between them. */
  const sections: RenderElement[] = []

  /** Changes what the page remembers; reads the state as it is when pressed. */
  const patch = (change: Partial<GlassReviewUi>) => kit.updateReview(was => ({ ...was, seen: true, ui: { ...was.ui, ...change } }))
  const say = (text: string | null) => patch({ note: text })
  const toggle = (id: string) => kit.updateReview(was => ({ ...was, ui: { ...was.ui, note: null, toggled: was.ui.toggled.includes(id) ? was.ui.toggled.filter(one => one !== id) : [...was.ui.toggled, id] } }))
  const draft = async (text: string, done: Copy, added: number) => {
    if (text === '') return
    const filled = await kit.fill(text)
    if (filled) memory.added += added
    await say(COPY[filled ? done : 'compaction.draft.failed'])
  }

  /** A quiet control: no hotkey, drawn dim. */
  const quiet = (id: string, label: string, onPress: () => Promise<void>): RenderElement => <Button key={id} label={label} dimColor onPress={() => void onPress()} />

  if (showAfter && after !== null) {
    // Only what the user said counts here; files, values, tasks and errors are checked too, and the full list shows them.
    const missing = missingSaid(after.items, after.marks)
    const total = saidTotal(after.items)
    const time = localTime(review.history[review.history.length - 1]?.ts ?? after.ts)
    const status = total === 0 ? COPY['compaction.none'] : missing.length === 0 ? COPY['compaction.ok'] : missing.length === 1 ? COPY['compaction.missing.one'] : fill(COPY['compaction.missing'], { count: missing.length })
    children.push(
      <Text key="headline" bold>
        {time === null ? COPY['compaction.lead.plain'] : fill(COPY['compaction.lead'], { time })}
      </Text>,
      <Text key="status">{status}</Text>,
    )

    // First screen: only what may be missing, and one note that puts it all back.
    for (const item of missing.slice(0, MISSING_SHOWN)) children.push(<Text key={`missing:${item.id}`}>{`• ${trim(item.text, LABEL_CHARS)}`}</Text>)
    if (missing.length > MISSING_SHOWN) children.push(<Text key="missing-more" dimColor>{fill(COPY['compaction.more'], { count: missing.length - MISSING_SHOWN })}</Text>)
    if (missing.length > 0) {
      children.push(
        <Box key="action" flexDirection="row">
          <Button
            key="add-back"
            variant="primary"
            hotkey="b"
            autoFocus
            label={missing.length === 1 ? COPY['compaction.add.one'] : COPY['compaction.add.many']}
            onPress={() => void kit.readd().then(() => say(COPY['compaction.readded.note']))}
          />
        </Box>,
      )
    }
    if (ui.note !== null) children.push(<Text key="note">{ui.note}</Text>)

    // The three views open one at a time: opening one closes the others, so the page stays short.
    const views: RenderElement[] = []
    views.push(
      <Box key="actions" flexDirection="row" gap={2}>
        {quiet('summary', ui.summary ? COPY['compaction.summary.hide'] : COPY['compaction.summary'], () => patch({ summary: !ui.summary, list: false, earlier: false }))}
        {quiet('list', ui.list ? COPY['compaction.list.hide'] : COPY['compaction.list'], () => patch({ list: !ui.list, summary: false, earlier: false }))}
        {quiet('earlier', ui.earlier ? COPY['compaction.earlier.hide'] : COPY['compaction.earlier'], () => patch({ earlier: !ui.earlier, summary: false, list: false }))}
      </Box>,
    )

    if (ui.summary) {
      const { Markdown } = table
      views.push(after.summary === '' ? <Text key="summary-none" dimColor>{COPY['compaction.summary.none']}</Text> : <Markdown key="summary" text={after.summary} />)
    }

    if (ui.list) {
      views.push(heading(table, 'list-title', COPY['compaction.list']))
      if (!after.judged) {
        views.push(
          <Text key="quick" dimColor>
            {COPY['compaction.quick']}
          </Text>,
        )
      }
      if (after.items.length === 0) views.push(<Text key="list-none">{COPY['compaction.none']}</Text>)
      // The mark's word comes first, so a long item can never push it into a column that wraps.
      for (const item of after.items.filter(one => !one.safe)) {
        const mark = after.marks[item.id] ?? 'found'
        views.push(
          <Text key={`item:${item.id}`} dimColor={mark === 'found'}>
            {`${MARK_SYMBOL[mark]} ${markWord(mark)} · ${trim(item.text, LABEL_CHARS)}`}
          </Text>,
        )
      }
      const safe = after.items.filter(one => one.safe).length
      if (safe > 0) views.push(<Text key="list-safe" dimColor>{fill(COPY['compaction.safe.anyway'], { count: safe })}</Text>)
    }

    if (ui.earlier) {
      views.push(
        heading(table, 'earlier-title', COPY['compaction.earlier']),
        <Text key="earlier-headline" dimColor>
          {after.earlier.length === 0 ? COPY['compaction.earlier.none'] : COPY['compaction.earlier.headline']}
        </Text>,
      )
      for (const one of after.earlier.slice(0, EARLIER_SHOWN)) {
        views.push(
          <Box key={`earlier-row:${one.id}`} flexDirection="row" justifyContent="space-between" gap={2} width={ROW_CELLS}>
            <Box flexShrink={1} minWidth={0}>
              <Text dimColor wrap="truncate">
                {trim(one.text, LABEL_CHARS)}
              </Text>
            </Box>
            <Button key={`earlier:${one.id}`} label={COPY['compaction.earlier.use']} dimColor onPress={() => void draft(one.text, 'compaction.drafted', 0)} />
          </Box>,
        )
      }
    }

    sections.push(<Box key="views" flexDirection="column">{views as never}</Box>)

    if (view.detailed) {
      const judgeable = after.items.filter(item => !item.safe)
      if (!after.judged && judgeable.length > 0 && !ui.busy) {
        const prompt = judgePrompt(judgeable, after.summary)
        const settings = view.settings
        const ask = async () => {
          await patch({ busy: true, note: COPY['compaction.haiku.busy'] })
          const answer = await kit.ask(HAIKU, prompt, JUDGE_ANSWER_TOKENS)
          if ('reason' in answer) {
            await patch({ busy: false, note: COPY[answer.reason === 'unavailable' ? 'compaction.haiku.skip' : 'compaction.haiku.failed'] })
            return
          }
          const marks = { ...after.marks, ...parseJudge(answer.text, judgeable) }
          const counts = tally(judgeable, marks).counts
          const tokens = answer.input + answer.output
          const rates = haikuRates(settings.models)
          const cost = rates === null ? fill(COPY['compaction.haiku.cost'], { tokens: formatTokens(tokens) }) : fill(COPY['compaction.haiku.cost.amount'], { tokens: formatTokens(tokens), amount: amount(priceUsage(rates, { input_tokens: answer.input, output_tokens: answer.output }, settings.ttl), settings) })
          const note = fill(COPY['compaction.haiku.done'], { count: judgeable.length, found: counts.found, partly: counts.partly, missing: counts.missing, cost })
          await kit.updateReview(was => ({
            ...was,
            missing: tally(judgeable, marks).missing,
            after: was.after === null ? null : { ...was.after, marks, judged: true },
            ui: { ...was.ui, busy: false, note },
          }))
        }
        sections.push(
          <Box key="haiku" flexDirection="column">
            {quiet('haiku-check', COPY['compaction.haiku'], ask)}
            <Text dimColor>{fill(COPY['compaction.haiku.detail'], { tokens: formatTokens(Math.ceil(prompt.length / CHARS_PER_TOKEN)) })}</Text>
          </Box>,
        )
      }

      const history: RenderElement[] = [heading(table, 'history-title', COPY['compaction.history'])]
      for (const row of [...review.history].reverse()) {
        const checked = row.kept + row.missing > 0 ? fill(COPY['compaction.history.checked'], { kept: row.kept, missing: row.missing }) : ''
        const size = (tokens: number | null) => (tokens === null ? COPY['compaction.history.unknown'] : formatTokens(tokens))
        const figure = fill(COPY['compaction.history.figure'], { before: size(row.before), after: size(row.after) })
        history.push(figureRow(table, `history:${row.ts}`, joinParts([localTime(row.ts), trigger(row), checked]), figure))
      }
      sections.push(<Box key="history" flexDirection="column">{history as never}</Box>, <Box key="to-before">{quiet('to-before', COPY['compaction.before'], () => patch({ view: 'before', note: null }))}</Box>)
    }
    return (
      <Box flexDirection="column" gap={1}>
        <Box key="answer" flexDirection="column">
          {children as never}
        </Box>
        {sections as never}
      </Box>
    )
  }

  // Before a summary: what it should keep, as ticks.
  const items = keepCandidates(review, pins)
  const { suggested, more } = splitKeep(items, review.readded, SUGGESTED_SHOWN)
  // The same choice the summariser is told at the next summary (compaction-memory.ts), so the page and the hook agree.
  const chosen = tickedItems(review, pins)
  const nothing = suggested.length === 0 && more.length === 0

  children.push(
    <Text key="headline" bold>
      {nothing ? COPY['compaction.before.none'] : COPY['compaction.before.headline']}
    </Text>,
  )
  if (review.held) children.push(<Text key="held" dimColor>{COPY['compaction.held.skip']}</Text>)

  // The ticks are real controls, so they are Buttons; they carry no hotkey. A pinned one also has a quiet Unpin at its row's end.
  const tick = (item: GlassKeepItem, isSuggested: boolean): RenderElement => {
    const on = isOn(ui, item, isSuggested)
    const button = <Button key={`tick:${item.id}`} label={`${on ? '[x]' : '[ ]'} ${trim(item.text, LABEL_CHARS)}`} dimColor={!on} onPress={() => void toggle(item.id)} />
    if (item.kind !== 'pin') return button
    return (
      <Box key={`row:${item.id}`} flexDirection="row" gap={2}>
        {button}
        {quiet(`unpin:${item.id}`, COPY['compaction.unpin'], () => kit.updatePins(was => was.filter(one => one.id !== item.id)))}
      </Box>
    )
  }
  const ticks: RenderElement[] = [...suggested.map(item => tick(item, true)), ...(ui.list ? more.map(item => tick(item, false)) : [])]
  if (ticks.length > 0) sections.push(<Box key="ticks" flexDirection="column">{ticks as never}</Box>)

  if (!nothing) {
    const command = compactKeep(chosen, COPY['compaction.keep'], KEEP_MAX, LABELS)
    sections.push(
      <Box key="action" flexDirection="row">
        <Button
          key="use"
          variant="primary"
          hotkey="c"
          autoFocus
          label={COPY['compaction.before.use']}
          onPress={() => void (command === '' ? say(COPY['compaction.before.none.checked']) : draft(command, 'compaction.before.drafted', 0))}
        />
      </Box>,
    )
  }
  if (ui.note !== null) sections.push(<Text key="note">{ui.note}</Text>)

  const row: RenderElement[] = []
  if (more.length > 0) row.push(quiet('more', ui.list ? COPY['compaction.fewer'] : fill(COPY['compaction.list.more'], { count: more.length }), () => patch({ list: !ui.list })))
  if (after !== null) row.push(quiet('to-after', COPY['compaction.back'], () => patch({ view: 'after', note: null })))
  if (row.length > 0) {
    sections.push(
      <Box key="actions" flexDirection="row" gap={2}>
        {row as never}
      </Box>,
    )
  }
  return (
    <Box flexDirection="column" gap={1}>
      <Box key="answer" flexDirection="column">
        {children as never}
      </Box>
      {sections as never}
    </Box>
  )
}

export const register: Register = () => {
  addPage({ id: 'compaction', hotkey: 'k', labelKey: 'pane.compaction', render: context => render(context), dashboard: view => dashboardAddress(view.settings.dashboardUrl) })
}
