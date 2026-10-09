import { atom, read } from 'claude-code'
import type { Register } from 'claude-code'

import { barAlt, partsBarSvg, SVG_LIMIT } from './charts'
import { amount, amountAfterAbout, clock, isVisibleChange, localTime, percent, shortAmount } from './fmt'
import { COPY } from './generated/copy.js'
import { formatTokens } from './lib/units.js'
import { fill, joinParts } from './lib/text.js'
import { emptySnap } from './live'
import { OPEN_ARGS } from './pane'
import { coldMin, drawView, sendCostNow } from './view'
import type { View } from './view'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)

// The band above the prompt: the glance. Two fixed segments (how full the
// context is and what the session has cost) and at most one that appears only
// when it matters (the cache about to expire or expired, the 5-hour limit off
// pace). On the desktop a small bar draws the context by part. Segments drop
// from the right as the band narrows.

export type Segment = { key: string; text: string; color?: string; bold?: boolean }

const ICONS = { plenty: '○', filling: '◐', soon: '◕', next: '●' } as const
const COLORS = { plenty: 'success', filling: 'suggestion', soon: 'warning', next: 'error' } as const

/** Cells the open button takes, and the desktop bar's width in cells. */
const BUTTON_CELLS = 14
const BAR_PIXELS = 120
const BAR_CELLS = 16
const SEPARATOR_CELLS = 3
/** The cache segment appears in the last fifth of its lifetime. */
const ENDING_FRACTION = 0.2

function context(view: View): Segment | null {
  const reading = view.snap.reading
  if (reading === null) return null
  const state = reading.state
  const word = state === 'soon' ? COPY['state.soon'] : state === 'next' ? COPY['state.next'] : ''
  const left = view.replies
  const runwayText =
    left === null || left < 1
      ? fill(COPY['band.point'], { tokens: formatTokens(reading.point) })
      : left === 1
        ? COPY['band.runway.one']
        : fill(COPY['band.runway'], { replies: left })
  const tokens = view.detailed ? fill(COPY['band.tokens'], { tokens: formatTokens(reading.tokens), point: formatTokens(reading.point) }) : ''
  const text = joinParts([word, fill(COPY['band.full'], { pct: percent(reading.pct) }), runwayText, tokens])
  return { key: 'context', text: `${ICONS[state]} ${text}`, color: COLORS[state], bold: word !== '' }
}

function spend(view: View, working: boolean): Segment | null {
  const { snap, settings } = view
  if (snap.cost === null) return null
  const subscription = settings.billing.mode === 'subscription'
  const change = working && snap.turnStartCost !== null ? snap.cost - snap.turnStartCost : snap.lastReplyUsd
  let text = fill(COPY['band.cost'], { amount: amount(snap.cost, settings) })
  if (!subscription && isVisibleChange(change)) text += ` ${fill(COPY['band.change'], { amount: shortAmount(change, settings) })}`

  const extra: string[] = []
  if (view.detailed) {
    if (subscription && snap.fiveHour !== null) extra.push(fill(COPY['band.limit'], { pct: percent(snap.fiveHour.pct) }))
    const usual = settings.usual.sessionCostUsd
    if (usual !== null) {
      const key = snap.cost <= usual ? 'band.usual.under' : 'band.usual'
      extra.push(fill(COPY[key], { amount: amount(usual, settings) }))
    }
  }
  return { key: 'spend', text: joinParts([text, ...extra]) }
}

/** The one segment that appears only when it matters, the most urgent first. */
function conditional(view: View, working: boolean): Segment | null {
  const { cache, settings, tokens } = view
  if (!working && cache !== null) {
    if (!cache.isWarm && tokens >= coldMin(settings)) {
      const cost = amountAfterAbout(sendCostNow(view, false), settings)
      return { key: 'cache', text: fill(COPY['band.cache.cold'], { amount: cost }), color: 'warning' }
    }
    if (cache.isWarm && cache.fractionLeft <= ENDING_FRACTION) {
      return { key: 'cache', text: fill(COPY['band.cache.ending'], { time: clock(cache.remainingMs) }), color: 'warning' }
    }
  }
  const pace = view.pace
  if (pace !== null) {
    const time = localTime(pace.runsOutAt)
    if (time !== null) return { key: 'pace', text: fill(COPY['band.pace'], { pct: percent(pace.pct), time }), color: 'warning' }
  }
  if (view.detailed && !working && cache !== null && cache.isWarm) {
    return { key: 'cache', text: fill(COPY['band.cache.warm'], { time: clock(cache.remainingMs) }) }
  }
  return null
}

/** The segments to draw, at most three, in the order they are drawn. */
export function segmentsOf(view: View, working: boolean): Segment[] {
  return [context(view), spend(view, working), conditional(view, working)].filter((one): one is Segment => one !== null)
}

/** Which segments, and whether the bar, fit in `columns` cells. Segments drop from the right; the bar only takes what is left over. */
export function fit(segments: Segment[], columns: number, hasBar: boolean): { segments: Segment[]; bar: boolean } {
  const room = columns - BUTTON_CELLS
  const width = (list: Segment[]) => list.reduce((sum, one) => sum + one.text.length, 0) + SEPARATOR_CELLS * Math.max(0, list.length - 1)
  let kept = segments
  while (kept.length > 1 && width(kept) > room) kept = kept.slice(0, -1)
  return { segments: kept, bar: hasBar && width(kept) + BAR_CELLS <= room }
}

export const register: Register = on => {
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const base = await next(e)
    if (e.props.hasSurvey) return base
    const view = drawView(await read($, snapAtom), await read($, settingsAtom), await read($, detailedAtom), await $.clock.now(), e.props.isWorking)
    const segments = segmentsOf(view, e.props.isWorking)
    if (view.snap.reading === null || segments.length === 0) return base

    const table = $.ui.resolve(e)
    // The terminal has no Svg; its table answers any name, so ask the surface.
    const Svg = e.surface !== 'terminal' && 'Svg' in table ? table.Svg : null
    const { Box, Text, Button } = table
    const parts = view.parts
    const shown = fit(segments, e.props.bodyColumns, Svg !== null && parts !== null)

    let bar = null
    if (shown.bar && Svg !== null && parts !== null && view.snap.reading !== null) {
      const alt = barAlt(parts, percent(view.snap.reading.pct))
      const source = partsBarSvg(parts, BAR_PIXELS, 10, alt)
      if (source.length <= SVG_LIMIT) bar = <Svg key="bar" source={source} alt={alt} width={BAR_PIXELS} height={10} isInteractive />
    }

    const row: unknown[] = []
    shown.segments.forEach((segment, index) => {
      if (index > 0) row.push(<Text key={`sep${index}`} dimColor>│</Text>)
      row.push(
        <Text key={segment.key} color={segment.color} bold={segment.bold} wrap="truncate">
          {segment.text}
        </Text>,
      )
      if (index === 0 && bar !== null) row.push(bar)
    })

    return (
      <Box flexDirection="column">
        {base}
        <Box flexDirection="row" gap={1}>
          {row as never}
          <Button key="open" label={COPY['band.open']} hotkey="g" onPress={() => void $.ui.open(OPEN_ARGS)} />
        </Box>
      </Box>
    )
  })
}
