import type { Register, RenderElement } from 'claude-code'

import { barAlt, limitBarSvg, MARK_ROW, partsBarSvg, replyChartSvg, SVG_LIMIT, sparkline, textBar } from './charts'
import { amount, localTime, percent, sessionAmount } from './fmt'
import { COPY } from './generated/copy.js'
import { formatTokens } from './lib/units.js'
import { wholePercents } from './lib/shares.js'
import { fill, joinParts } from './lib/text.js'
import { addPage } from './pane'
import type { PageContext } from './pane'
import { figureRow, heading } from './rows'

// The Now page: how full the context is, what is filling it, and what the
// session has cost. The first screen holds the headline, the advice with its one
// action, the bar, three text rows and a line; Detailed adds the charts and the
// figures behind them. Each part of the page is a section Box and the page's column
// leaves a blank line between sections; the rows inside a section stay tight.

const COMPACT_COMMAND = '/compact'
const BAR_WIDTH = 360
const BAR_HEIGHT = 14
const TERMINAL_BAR_CELLS = 44
const FIVE_HOURS_MS = 5 * 60 * 60 * 1000
const CHART_REPLIES = 40

/**
 * What keys a row to its segment of the bar. On the desktop the bar is coloured from the dashboard's palette, which a
 * Text can't name in both themes, so the mark takes the nearest theme key (it follows the person's theme): red for the
 * startup context, Claude's orange for the conversation, amber for tool output. On the terminal the bar is shades, so the
 * mark is the shade the bar draws the part with. The words stay: colour is never the only signal.
 */
const SWATCH = {
  startup: { colour: 'error', shade: '█' },
  conversation: { colour: 'claude', shade: '▓' },
  output: { colour: 'warning', shade: '▒' },
} as const

type Row = { key: keyof typeof SWATCH; name: string; tokens: number }

function rowsOf(context: PageContext): Row[] {
  const parts = context.view.parts
  if (parts === null) return []
  return [
    { key: 'startup', name: COPY['now.part.startup'], tokens: parts.startup },
    { key: 'conversation', name: COPY['now.part.conversation'], tokens: parts.conversation },
    { key: 'output', name: COPY['now.part.output'], tokens: parts.output },
  ]
}

function headline(context: PageContext): string {
  const { view } = context
  const reading = view.snap.reading
  if (reading === null) return COPY['now.waiting']
  const pct = percent(reading.pct)
  if (view.replies !== null && view.replies >= 1) return fill(COPY['now.headline'], { pct, replies: view.replies })
  return fill(COPY['now.headline.point'], { pct, tokens: formatTokens(reading.point) })
}

function advice(context: PageContext): string | null {
  const reading = context.view.snap.reading
  if (reading === null) return null
  return COPY[`now.advice.${reading.state}`]
}

function sessionLine(context: PageContext): string {
  const { snap, settings } = context.view
  // On a subscription the 5-hour share when the whole session sits inside this window, else the weekly share, else the list price.
  const spent = sessionAmount(snap.cost ?? 0, snap, settings)
  return snap.replies === 1 ? fill(COPY['now.session.one'], { amount: spent }) : fill(COPY['now.session'], { amount: spent, replies: snap.replies })
}

function limitCells(pct: number, elapsed: number, cells: number): string {
  const used = Math.round((Math.min(100, Math.max(0, pct)) / 100) * cells)
  const cursor = Math.min(cells - 1, Math.max(0, Math.round(elapsed * cells)))
  let out = ''
  for (let i = 0; i < cells; i += 1) out += i === cursor ? '│' : i < used ? '█' : '░'
  return `▕${out}▏`
}

/** A tool's name as the page lists it: an MCP tool's own name says nothing, so it is "MCP tool". */
function toolName(name: string): string {
  return name.startsWith('mcp__') ? COPY['tool.mcp'] : name
}

async function render(context: PageContext): Promise<RenderElement> {
  const { table, view, state } = context
  // The terminal has no Svg; its table answers any name, so ask the surface.
  const Svg = context.e.surface !== 'terminal' && 'Svg' in table ? table.Svg : null
  const { Box, Text, Button } = table
  const { snap, settings } = view
  const reading = snap.reading
  const parts = view.parts

  if (reading === null || parts === null) {
    return (
      <Box flexDirection="column" gap={1}>
        <Text>{COPY['now.waiting']}</Text>
        {snap.cost !== null ? <Text>{sessionLine(context)}</Text> : null}
      </Box>
    )
  }

  const tip = advice(context)
  const compactNow = reading.state === 'soon' || reading.state === 'next'
  const rows = rowsOf(context)
  const alt = barAlt(parts, percent(reading.pct))
  const svgBar = Svg !== null ? partsBarSvg(parts, BAR_WIDTH, BAR_HEIGHT, alt, COPY['now.mark']) : ''
  const hasSvgBar = Svg !== null && svgBar.length <= SVG_LIMIT

  const children: RenderElement[] = []

  // The answer first: the headline, what to do about it, and the one action.
  children.push(
    <Box key="answer" flexDirection="column">
      <Text bold>{headline(context)}</Text>
      {tip !== null ? <Text>{tip}</Text> : null}
      {compactNow ? (
        <Box key="action" flexDirection="row">
          <Button key="copy" variant="primary" hotkey="c" autoFocus label={state.copied === 'compact' ? COPY['pane.copied'] : `${COPY['pane.copy']} ${COMPACT_COMMAND}`} onPress={press => context.copy(COMPACT_COMMAND, press.surface, 'compact')} />
        </Box>
      ) : null}
    </Box>,
  )

  if (hasSvgBar && Svg !== null) {
    children.push(
      <Box key="bar" flexDirection="column">
        {/* The label sits under the mark inside the drawing: a line of text can't be placed under it. */}
        <Svg source={svgBar} alt={alt} width={BAR_WIDTH} height={BAR_HEIGHT + MARK_ROW} />
      </Box>,
    )
  } else {
    children.push(
      <Box key="bar" flexDirection="column">
        <Text>{textBar(parts, TERMINAL_BAR_CELLS)}</Text>
        <Text dimColor>{`${' '.repeat(TERMINAL_BAR_CELLS)}▲ ${COPY['now.mark']}`}</Text>
      </Box>,
    )
  }

  // What is filling it: each row is keyed to its segment of the bar by a mark before its name.
  children.push(
    <Box key="filling" flexDirection="column">
      {heading(table, 'filling', COPY['now.filling'])}
      {rows.map(row => figureRow(table, `row:${row.key}`, row.name, formatTokens(row.tokens), { mark: hasSvgBar ? { glyph: '■', color: SWATCH[row.key].colour } : { glyph: SWATCH[row.key].shade } }))}
    </Box>,
  )

  // The session: what it has cost and, in Detailed, how that compares with the person's usual.
  if (snap.cost !== null) {
    const usual = settings.usual.sessionCostUsd
    children.push(
      <Box key="session" flexDirection="column">
        <Text>{sessionLine(context)}</Text>
        {view.detailed && usual !== null ? <Text>{fill(COPY[snap.cost <= usual ? 'band.usual.under' : 'band.usual'], { amount: amount(usual, settings) })}</Text> : null}
      </Box>,
    )
  }

  if (view.detailed) {
    // Cost and context per reply.
    const replies = snap.replyCosts.slice(-CHART_REPLIES)
    if (replies.length >= 2) {
      const chartAlt = fill(COPY['now.chart.alt'], { count: replies.length })
      const chart = Svg !== null ? replyChartSvg(replies, BAR_WIDTH, 48, chartAlt, (_, usd, ctx) => `${amount(usd, settings)} · ${formatTokens(ctx)}`) : ''
      children.push(
        <Box key="chart" flexDirection="column">
          {heading(table, 'chart-title', COPY['now.chart'])}
          {Svg !== null && chart.length <= SVG_LIMIT ? (
            <Svg source={chart} alt={chartAlt} width={BAR_WIDTH} height={48} />
          ) : (
            <Box flexDirection="column">
              <Text>{sparkline(replies.map(one => one.ctx))}</Text>
              <Text>{sparkline(replies.map(one => one.usd))}</Text>
            </Box>
          )}
          <Text dimColor>{COPY['now.chart.caption']}</Text>
        </Box>,
      )
    }

    // Where the spend goes: each part's share of the whole, largest first, adding up to 100. The session line above gives the total.
    const mix = snap.mix
    const spent = mix.read + mix.write + mix.input + mix.output
    if (snap.cost !== null && snap.cost > 0 && spent > 0) {
      const lines: [string, number][] = [
        [COPY['now.spend.read'], mix.read],
        [COPY['now.spend.write'], mix.write],
        [COPY['now.spend.input'], mix.input],
        [COPY['now.spend.output'], mix.output],
      ]
      lines.sort((a, b) => b[1] - a[1])
      // Whole percents that add up to 100: each rounded alone could read 102.
      const shares = wholePercents(lines.map(([, usd]) => usd))
      children.push(
        <Box key="spend" flexDirection="column">
          {heading(table, 'spend-title', COPY['now.spend'])}
          {lines.map(([name], at) => figureRow(table, name, name, `${shares[at] ?? 0}%`))}
        </Box>,
      )
    }

    // The heaviest single items, each with a few words on what the call did when they are known.
    if (snap.heaviest.length > 0) {
      children.push(
        <Box key="heaviest" flexDirection="column">
          {heading(table, 'heaviest-title', COPY['now.heaviest'])}
          {snap.heaviest.slice(0, 3).map(item => figureRow(table, `${item.name}:${item.tokens}`, joinParts([toolName(item.name), item.what ?? '']), formatTokens(item.tokens)))}
        </Box>,
      )
    }

    // The 5-hour limit, with a cursor for how much of the window has gone.
    const limit = snap.fiveHour
    const resetsAt = limit !== null && limit.resetsAt !== null ? Date.parse(limit.resetsAt) : Number.NaN
    if (limit !== null && Number.isFinite(resetsAt)) {
      const elapsed = Math.min(1, Math.max(0, 1 - (resetsAt - view.now) / FIVE_HOURS_MS))
      const time = localTime(resetsAt)
      const vars = { pct: percent(limit.pct), elapsed: percent(elapsed * 100), time: time ?? '' }
      const label = fill(COPY[time === null ? 'now.limit.line.plain' : 'now.limit.line'], vars)
      const limitAlt = fill(COPY['band.limit'], { pct: percent(limit.pct) })
      const bar = Svg !== null ? limitBarSvg(limit.pct, elapsed, BAR_WIDTH, 10, limitAlt) : ''
      children.push(
        <Box key="limit" flexDirection="column">
          {heading(table, 'limit-title', COPY['now.limit'])}
          {Svg !== null && bar.length <= SVG_LIMIT ? <Svg source={bar} alt={limitAlt} width={BAR_WIDTH} height={10} /> : <Text>{limitCells(limit.pct, elapsed, TERMINAL_BAR_CELLS)}</Text>}
          <Text dimColor>{label}</Text>
        </Box>,
      )
    }

    // The summaries so far, as one line: the Compaction page lists each.
    const summaries = snap.compactions.length
    children.push(
      <Box key="compactions" flexDirection="column">
        {heading(table, 'compactions-title', COPY['now.compactions'])}
        <Text>{summaries === 0 ? COPY['now.compactions.none'] : summaries === 1 ? COPY['now.compactions.one'] : fill(COPY['now.compactions.some'], { count: summaries })}</Text>
      </Box>,
    )
  }

  return (
    <Box flexDirection="column" gap={1}>
      {children}
    </Box>
  )
}

export const register: Register = () => {
  addPage({ id: 'now', hotkey: 'n', labelKey: 'pane.now', render })
}
