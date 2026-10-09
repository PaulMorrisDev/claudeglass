import type { Register, RenderElement } from 'claude-code'

import { barAlt, limitBarSvg, partsBarSvg, replyChartSvg, SVG_LIMIT, sparkline, textBar } from './charts'
import { amount, amountOf, localTime, percent } from './fmt'
import { COPY, GLOSSARY } from './generated/copy.js'
import { formatTokens } from './lib/units.js'
import { fill } from './lib/text.js'
import { addPage } from './pane'
import type { PageContext } from './pane'

// The Now page: how full the context is, what is filling it, and what the
// session has cost. The first screen holds the headline, the advice, the bar,
// three rows and one line; Detailed adds the charts and the figures behind them.

const COMPACT_COMMAND = '/compact'
const BAR_WIDTH = 360
const BAR_HEIGHT = 14
const TERMINAL_BAR_CELLS = 44
const FIVE_HOURS_MS = 5 * 60 * 60 * 1000
const CHART_REPLIES = 40

type Row = { key: 'startup' | 'conversation' | 'output'; hotkey: string; name: string; tokens: number; estimated: boolean }

function rowsOf(context: PageContext): Row[] {
  const parts = context.view.parts
  if (parts === null) return []
  const startup = `${COPY['now.part.startup']} (${COPY['now.part.startup.detail']})`
  return [
    { key: 'startup', hotkey: '1', name: startup, tokens: parts.startup, estimated: false },
    { key: 'conversation', hotkey: '2', name: COPY['now.part.conversation'], tokens: parts.conversation, estimated: false },
    { key: 'output', hotkey: '3', name: COPY['now.part.output'], tokens: parts.output, estimated: true },
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
  // The same rule as the band: on a subscription the 5-hour share when known, else the weekly share, else the list price.
  const spent = amountOf(snap.cost ?? 0, snap, settings)
  return snap.replies === 1 ? fill(COPY['now.session.one'], { amount: spent }) : fill(COPY['now.session'], { amount: spent, replies: snap.replies })
}

function limitCells(pct: number, elapsed: number, cells: number): string {
  const used = Math.round((Math.min(100, Math.max(0, pct)) / 100) * cells)
  const cursor = Math.min(cells - 1, Math.max(0, Math.round(elapsed * cells)))
  let out = ''
  for (let i = 0; i < cells; i += 1) out += i === cursor ? '│' : i < used ? '█' : '░'
  return `▕${out}▏`
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
      <Box flexDirection="column">
        <Text>{COPY['now.waiting']}</Text>
        {snap.cost !== null ? <Text>{sessionLine(context)}</Text> : null}
      </Box>
    )
  }

  const tip = advice(context)
  const compactNow = reading.state === 'soon' || reading.state === 'next'
  const rows = rowsOf(context)
  const alt = barAlt(parts, percent(reading.pct))
  const svgBar = Svg !== null ? partsBarSvg(parts, BAR_WIDTH, BAR_HEIGHT, alt) : ''
  const hasSvgBar = Svg !== null && svgBar.length <= SVG_LIMIT

  const children: RenderElement[] = []

  children.push(
    <Text key="headline" bold>
      {headline(context)}
    </Text>,
  )

  if (tip !== null) {
    children.push(
      <Box key="advice" flexDirection="row" gap={1}>
        <Text>{tip}</Text>
        {compactNow ? (
          <Button key="copy" label={state.copied ? COPY['pane.copied'] : `${COPY['pane.copy']} ${COMPACT_COMMAND}`} hotkey="c" onPress={press => context.copy(COMPACT_COMMAND, press.surface)} />
        ) : null}
      </Box>,
    )
  }

  if (hasSvgBar && Svg !== null) {
    children.push(
      <Box key="bar" flexDirection="column">
        <Svg source={svgBar} alt={alt} width={BAR_WIDTH} height={BAR_HEIGHT} />
        <Text dimColor>{`▲ ${COPY['now.mark']}`}</Text>
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

  children.push(
    <Text key="filling" bold>
      {COPY['now.filling']}
    </Text>,
  )
  for (const row of rows) {
    const tokens = row.estimated ? `${formatTokens(row.tokens)} ${COPY['amount.est']}` : formatTokens(row.tokens)
    const open = state.explain === row.key
    children.push(
      <Box key={`row:${row.key}`} flexDirection="column">
        <Button key={`explain:${row.key}`} label={`${row.name} · ${tokens}`} hotkey={row.hotkey} onPress={() => context.explain(row.key)} />
        {open ? (
          <Box paddingLeft={4}>
            <Text dimColor>{GLOSSARY[row.key].body}</Text>
          </Box>
        ) : null}
      </Box>,
    )
  }

  if (snap.cost !== null) {
    children.push(<Text key="session">{sessionLine(context)}</Text>)
  }

  if (view.detailed) {
    // Cost and context per reply.
    const replies = snap.replyCosts.slice(-CHART_REPLIES)
    if (replies.length >= 2) {
      const chartAlt = fill(COPY['now.chart.alt'], { count: replies.length })
      const chart = Svg !== null ? replyChartSvg(replies, BAR_WIDTH, 48, chartAlt, (_, usd, ctx) => `${amount(usd, settings)} · ${formatTokens(ctx)}`) : ''
      children.push(
        <Box key="chart" flexDirection="column">
          <Text bold>{COPY['now.chart']}</Text>
          {Svg !== null && chart.length <= SVG_LIMIT ? (
            <Svg source={chart} alt={chartAlt} width={BAR_WIDTH} height={48} />
          ) : (
            <Box flexDirection="column">
              <Text>{sparkline(replies.map(one => one.ctx))}</Text>
              <Text>{sparkline(replies.map(one => one.usd))}</Text>
            </Box>
          )}
        </Box>,
      )
    }

    // Where the spend goes.
    if (snap.cost !== null && snap.cost > 0) {
      const mix = snap.mix
      const lines: [string, number][] = [
        [COPY['now.spend.read'], mix.read],
        [COPY['now.spend.write'], mix.write],
        [COPY['now.spend.input'], mix.input],
        [COPY['now.spend.output'], mix.output],
      ]
      children.push(
        <Box key="spend" flexDirection="column">
          <Text bold>{COPY['now.spend']}</Text>
          {lines.map(([name, usd]) => (
            <Text key={name}>{`${name} · ${amount(usd, settings)}`}</Text>
          ))}
        </Box>,
      )
    }

    // The heaviest single items.
    if (snap.heaviest.length > 0) {
      children.push(
        <Box key="heaviest" flexDirection="column">
          <Text bold>{COPY['now.heaviest']}</Text>
          {snap.heaviest.slice(0, 3).map(item => (
            <Text key={`${item.name}:${item.tokens}`}>{fill(COPY['now.largest'], { name: item.name.startsWith('mcp__') ? COPY['tool.mcp'] : item.name, tokens: formatTokens(item.tokens) })}</Text>
          ))}
        </Box>,
      )
    }

    // The 5-hour limit, with a cursor for how much of the window has gone.
    const limit = snap.fiveHour
    if (limit !== null && limit.resetsAt !== null) {
      const resetsAt = Date.parse(limit.resetsAt)
      const elapsed = Number.isFinite(resetsAt) ? 1 - (resetsAt - view.now) / FIVE_HOURS_MS : 0
      const label = `${fill(COPY['band.limit'], { pct: percent(limit.pct) })} · ${COPY['now.cursor']} ${localTime(view.now) ?? ''}`.trim()
      const limitAlt = fill(COPY['band.limit'], { pct: percent(limit.pct) })
      const bar = Svg !== null ? limitBarSvg(limit.pct, elapsed, BAR_WIDTH, 10, limitAlt) : ''
      children.push(
        <Box key="limit" flexDirection="column">
          <Text bold>{COPY['now.limit']}</Text>
          {Svg !== null && bar.length <= SVG_LIMIT ? <Svg source={bar} alt={limitAlt} width={BAR_WIDTH} height={10} /> : <Text>{limitCells(limit.pct, elapsed, TERMINAL_BAR_CELLS)}</Text>}
          <Text dimColor>{label}</Text>
        </Box>,
      )
    }

    // What the user usually spends.
    const usual = settings.usual.sessionCostUsd
    if (usual !== null && snap.cost !== null) {
      const key = snap.cost <= usual ? 'band.usual.under' : 'band.usual'
      children.push(<Text key="usual">{fill(COPY[key], { amount: amount(usual, settings) })}</Text>)
    }

    // The opt-in that asks before a costly send (draft.ts).
    children.push(
      <Box key="guard" flexDirection="column">
        <Button
          key="guard-toggle"
          label={`${COPY['guard.opt']}: ${context.options.guard ? COPY['guard.opt.on'] : COPY['guard.opt.off']}`}
          hotkey="s"
          onPress={() => void context.kit.setOption('guard', !context.options.guard)}
        />
        <Box paddingLeft={4}>
          <Text dimColor>{COPY['guard.opt.detail']}</Text>
        </Box>
      </Box>,
    )

    // The summaries so far.
    const known = snap.compactions.filter(one => one.tokensBefore !== null && one.tokensAfter !== null)
    children.push(
      <Box key="compactions" flexDirection="column">
        <Text bold>{COPY['now.compactions']}</Text>
        {known.length === 0 ? (
          <Text>{COPY['now.compactions.none']}</Text>
        ) : (
          known.map(one => (
            <Text key={String(one.ts)}>
              {fill(COPY[one.trigger === 'manual' ? 'now.compaction.manual' : 'now.compaction.auto'], {
                before: formatTokens(one.tokensBefore ?? 0),
                after: formatTokens(one.tokensAfter ?? 0),
              })}
            </Text>
          ))
        )}
      </Box>,
    )
  }

  return <Box flexDirection="column">{children}</Box>
}

export const register: Register = () => {
  addPage({ id: 'now', hotkey: 'n', labelKey: 'pane.now', render })
}
