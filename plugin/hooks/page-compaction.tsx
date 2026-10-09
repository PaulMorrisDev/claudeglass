import type { Register, RenderElement } from 'claude-code'

import type { GlassKeepItem, GlassMark, GlassPin, GlassReviewUi, GlassSettings } from '../types'
import { linkTarget } from './agent-view'
import { listWithPins, MAX_PINS, memory, newPin } from './compaction-memory'
import { amount, formatTokens, localTime } from './fmt'
import { COPY } from './generated/copy.js'
import { addBackNote, compactKeep, judgePrompt, KEEP_MAX, MARK_SYMBOL, missingSaid, parseJudge, saidTotal, tally } from './lib/coverage.js'
import { splitSuggested } from './lib/keeplist.js'
import { priceUsage } from './lib/pricing.js'
import { fill } from './lib/text.js'
import { addPage } from './pane'
import type { PageContext } from './pane'

// The Compaction page. After a summary it opens on only what the summary may
// have dropped, each with a draft that puts it back; the summary, the full list,
// the earlier messages and (Detailed) the history and options sit behind a press.
// Before a summary it offers what the summary should keep as a handful of ticks
// and fills the prompt box with a /compact command. Nothing is ever sent.

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
/**
 * Keys for rows: the digits, then letters nothing else on the pane, the band or
 * the transcript uses (a, c, d, e, f, g, h, i, k, l, m, n, o, p, r, s, v, w, x, y are taken).
 * The last free letter, z, is the Haiku check's, so no row takes it (`i` is the Instructions tab's).
 */
const ROW_KEYS = '123456789bjqtu'
const HAIKU_KEY = 'z'
const LABELS = { files: COPY['compaction.label.files'], values: COPY['compaction.label.values'] }
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

function kindName(item: GlassKeepItem): string {
  return COPY[`compaction.kind.${item.kind}` as Copy]
}

function markWord(mark: GlassMark, item: GlassKeepItem): string {
  if (mark === 'safe') return COPY[`compaction.safe.${item.why === '' ? 'rules' : item.why === 'claude-md' ? 'rules' : item.why}` as Copy]
  return COPY[`compaction.mark.${mark}` as Copy]
}

/** Hands out the row keys, one to a Button, so no key is used twice on the screen. */
function keyGiver(): () => string | null {
  let next = 0
  return () => (next < ROW_KEYS.length ? ROW_KEYS.charAt(next++) : null)
}

function isOn(ui: GlassReviewUi, item: GlassKeepItem, suggested: boolean): boolean {
  return ui.toggled.includes(item.id) ? !suggested : suggested
}

function trigger(reviewed: { trigger: 'auto' | 'manual' }): string {
  return reviewed.trigger === 'auto' ? COPY['compaction.trigger.auto'] : COPY['compaction.trigger.manual']
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
  const nextKey = keyGiver()

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
  const pin = async (item: GlassKeepItem) => {
    if (pins.some(one => one.id === item.id || one.text === item.text)) {
      await kit.updatePins(was => was.filter(one => one.id !== item.id && one.text !== item.text))
      await say(null)
      return
    }
    const one = newPin(item.text)
    if (one === null) return
    if (pins.length >= MAX_PINS) {
      await say(fill(COPY['compaction.pin.full'], { count: pins.length }))
      return
    }
    await kit.updatePins(was => (was.some(have => have.id === one.id) ? was : [...was, one]))
    await say(COPY['compaction.pin.done'])
  }
  const isPinned = (item: GlassKeepItem, list: readonly GlassPin[]) => list.some(one => one.id === item.id || one.text === item.text)

  const button = (label: string, key: string | null, onPress: () => Promise<void>, id: string): RenderElement | null =>
    key === null ? null : <Button key={id} label={label} hotkey={key} onPress={() => void onPress()} />

  const options = (): RenderElement => (
    <Box key="options" flexDirection="column">
      <Text bold>{COPY['compaction.options']}</Text>
      {[
        { id: 'hold', key: 'x', label: COPY['compaction.opt.hold'], detail: COPY['compaction.opt.hold.detail'], on: context.options.hold },
        { id: 'readd', key: 'y', label: COPY['compaction.opt.readd'], detail: COPY['compaction.opt.readd.detail'], on: context.options.readd },
      ].map(one => (
        <Box key={one.id} flexDirection="column">
          <Button key={`opt:${one.id}`} label={`${one.label}: ${one.on ? COPY['compaction.on'] : COPY['compaction.off']}`} hotkey={one.key} onPress={() => void kit.setOption(one.id, !one.on)} />
          <Box paddingLeft={4}>
            <Text dimColor>{one.detail}</Text>
          </Box>
        </Box>
      )) as never}
    </Box>
  )

  if (showAfter && after !== null) {
    // Only what the user said counts here; files, values, tasks and errors are checked too, and the full list shows them.
    const missing = missingSaid(after.items, after.marks)
    const total = saidTotal(after.items)
    const headline = total === 0 ? COPY['compaction.list.none'] : missing.length === 0 ? COPY['compaction.headline.ok.said'] : missing.length === 1 ? COPY['compaction.headline.missing.one'] : fill(COPY['compaction.headline.missing'], { count: missing.length })
    children.push(
      <Text key="headline" bold>
        {headline}
      </Text>,
    )
    if (ui.note !== null) children.push(<Text key="note">{ui.note}</Text>)

    // First screen: only what may be missing, each with its draft.
    for (const item of missing) {
      const key = nextKey()
      if (key === null) break
      children.push(
        <Box key={`missing:${item.id}`} flexDirection="row" gap={1}>
          <Button key={`add:${item.id}`} label={`${COPY['compaction.add']} · ${trim(item.text, LABEL_CHARS)}`} hotkey={key} onPress={() => void draft(addBackNote([item], COPY['compaction.addback'], LABELS), 'compaction.drafted', 1)} />
          <Text dimColor>{kindName(item)}</Text>
        </Box>,
      )
    }

    const row: (RenderElement | null)[] = []
    if (missing.length > 1) row.push(button(COPY['compaction.add.all'], 'd', () => draft(addBackNote(missing, COPY['compaction.addback'], LABELS), 'compaction.drafted', missing.length), 'add-all'))
    row.push(button(ui.summary ? COPY['compaction.summary.hide'] : COPY['compaction.summary'], 's', () => patch({ summary: !ui.summary }), 'summary'))
    row.push(button(COPY['compaction.list'], 'f', () => patch({ list: !ui.list }), 'list'))
    row.push(button(COPY['compaction.earlier'], 'e', () => patch({ earlier: !ui.earlier }), 'earlier'))
    children.push(
      <Box key="actions" flexDirection="row" gap={1}>
        {row.filter(one => one !== null) as never}
      </Box>,
    )

    if (ui.summary) {
      const { Markdown } = table
      children.push(after.summary === '' ? <Text key="summary-none" dimColor>{COPY['compaction.summary.none']}</Text> : <Markdown key="summary" text={after.summary} />)
    }

    if (ui.list) {
      children.push(
        <Text key="list-title" bold>
          {COPY['compaction.list']}
        </Text>,
      )
      if (!after.judged) {
        children.push(
          <Text key="quick" dimColor>
            {COPY['compaction.quick']}
          </Text>,
        )
      }
      if (after.items.length === 0) children.push(<Text key="list-none">{COPY['compaction.list.none']}</Text>)
      for (const item of after.items) {
        const mark = after.marks[item.id] ?? 'found'
        const key = nextKey()
        const label = `${MARK_SYMBOL[mark]} ${trim(item.text, LABEL_CHARS)}`
        const word = `${isPinned(item, pins) ? `${COPY['compaction.pinned']} · ` : ''}${markWord(mark, item)}`
        children.push(
          <Box key={`item:${item.id}`} flexDirection="row" gap={1}>
            {key === null ? <Text>{label}</Text> : <Button key={`pin:${item.id}`} label={label} hotkey={key} onPress={() => void pin(item)} />}
            <Text dimColor>{word}</Text>
          </Box>,
        )
      }
    }

    if (ui.earlier) {
      children.push(
        <Text key="earlier-title" bold>
          {COPY['compaction.earlier']}
        </Text>,
        <Text key="earlier-headline" dimColor>
          {after.earlier.length === 0 ? COPY['compaction.earlier.none'] : COPY['compaction.earlier.headline']}
        </Text>,
      )
      for (const one of after.earlier.slice(0, EARLIER_SHOWN)) {
        const key = nextKey()
        if (key === null) break
        children.push(<Button key={`earlier:${one.id}`} label={trim(one.text, LABEL_CHARS)} hotkey={key} onPress={() => void draft(one.text, 'compaction.drafted', 0)} />)
      }
    }

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
        children.push(
          <Box key="haiku" flexDirection="column">
            <Button key="haiku-check" label={COPY['compaction.haiku']} hotkey={HAIKU_KEY} onPress={() => void ask()} />
            <Box paddingLeft={4}>
              <Text dimColor>{fill(COPY['compaction.haiku.detail'], { tokens: formatTokens(Math.ceil(prompt.length / CHARS_PER_TOKEN)) })}</Text>
            </Box>
          </Box>,
        )
      }

      children.push(
        <Text key="history-title" bold>
          {COPY['compaction.history']}
        </Text>,
      )
      for (const row of [...review.history].reverse()) {
        const time = localTime(row.ts) ?? ''
        const figures = { time, trigger: trigger(row), before: row.before === null ? '?' : formatTokens(row.before), after: row.after === null ? '?' : formatTokens(row.after), kept: row.kept, missing: row.missing }
        children.push(<Text key={`history:${row.ts}`}>{fill(COPY[row.kept + row.missing > 0 ? 'compaction.history.row' : 'compaction.history.row.plain'], figures)}</Text>)
      }
      const link = dashboardAddress(view.settings.dashboardUrl)
      if (link !== null) {
        const { Link } = table
        children.push(
          <Box key="dashboard" flexDirection="row" gap={1}>
            <Link key="dashboard-link" href={linkTarget(link)} label={COPY['compaction.dashboard']} />
            <Button key="dashboard-copy" label={context.state.copied ? COPY['pane.copied'] : COPY['compaction.dashboard.link']} hotkey="c" onPress={press => context.copy(link, press.surface)} />
          </Box>,
        )
      }
      children.push(options(), <Box key="to-before"><Button key="to-before-button" label={COPY['compaction.before']} hotkey="w" onPress={() => void patch({ view: 'before', note: null })} /></Box>)
    }
    return <Box flexDirection="column">{children as never}</Box>
  }

  // Before a summary: what it should keep, as ticks.
  const items = listWithPins(review.list, pins)
  const { suggested, more } = splitSuggested(items)
  const ticked = (list: readonly GlassKeepItem[], isSuggested: boolean) => list.filter(item => isOn(ui, item, isSuggested))
  const chosen = [...ticked(suggested, true), ...ticked(more, false)]
  const order = new Map(items.map((item, index) => [item.id, index]))
  chosen.sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0))

  children.push(
    <Text key="headline" bold>
      {suggested.length === 0 && more.length === 0 ? COPY['compaction.before.none'] : COPY['compaction.before.headline']}
    </Text>,
  )
  if (review.held) children.push(<Text key="held" dimColor>{COPY['compaction.held.skip']}</Text>)
  if (ui.note !== null) children.push(<Text key="note">{ui.note}</Text>)

  const tick = (item: GlassKeepItem, isSuggested: boolean): RenderElement | null => {
    const key = nextKey()
    const label = `${isOn(ui, item, isSuggested) ? '[x]' : '[ ]'} ${trim(item.text, LABEL_CHARS)}`
    return (
      <Box key={`pick:${item.id}`} flexDirection="row" gap={1}>
        {key === null ? <Text>{label}</Text> : <Button key={`tick:${item.id}`} label={label} hotkey={key} onPress={() => void toggle(item.id)} />}
        <Text dimColor>{item.kind === 'pin' ? COPY['compaction.pinned'] : kindName(item)}</Text>
      </Box>
    )
  }
  for (const item of suggested) children.push(tick(item, true) as RenderElement)
  if (ui.list) {
    children.push(
      <Text key="more-title" bold>
        {COPY['compaction.list.more']}
      </Text>,
    )
    if (more.length === 0) children.push(<Text key="more-none" dimColor>{COPY['compaction.list.none']}</Text>)
    for (const item of more) children.push(tick(item, false) as RenderElement)
  }

  const command = compactKeep(chosen, COPY['compaction.keep'], KEEP_MAX, LABELS)
  const row: (RenderElement | null)[] = [
    button(COPY['compaction.before.use'], 'c', async () => (command === '' ? say(COPY['compaction.before.none.checked']) : draft(command, 'compaction.before.drafted', 0)), 'use'),
  ]
  if (more.length > 0) row.push(button(COPY['compaction.list.more'], 'o', () => patch({ list: !ui.list }), 'more'))
  if (after !== null) row.push(button(COPY['compaction.review'], 'w', () => patch({ view: 'after', note: null }), 'to-after'))
  children.push(
    <Box key="actions" flexDirection="row" gap={1}>
      {row.filter(one => one !== null) as never}
    </Box>,
  )
  if (view.detailed) children.push(options())
  return <Box flexDirection="column">{children as never}</Box>
}

export const register: Register = () => {
  addPage({ id: 'compaction', hotkey: 'k', labelKey: 'pane.compaction', render: context => render(context) })
}
