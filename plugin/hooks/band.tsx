import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import { runningCount } from './agent-view'
import { barAlt, partsBarSvg, SVG_LIMIT } from './charts'
import { emptyReview, sound } from './compaction-memory'
import { amount, amountHasShare, amountOfAfterAbout, clock, isVisibleChange, localTime, percent, shortAmount } from './fmt'
import { COPY } from './generated/copy.js'
import { narrow } from './lib/band.js'
import { formatTokens } from './lib/units.js'
import { fill, joinParts } from './lib/text.js'
import { emptySnap } from './live'
import { OPEN_ARGS } from './pane'
import { coldMin, drawView, sendCostNow } from './view'
import type { View } from './view'
import type { GlassReview } from '../types'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)
const reviewAtom = atom({ plugin: 'claudeglass', key: 'review' } as const, emptyReview())
const pageAtom = atom({ plugin: 'claudeglass', key: 'page' } as const, 'now')

// The band above the prompt: the glance. Two fixed segments (how full the
// context is and what the session has cost) and at most one that appears only
// when it matters (a summary that may have dropped something, the cache about
// to expire or expired, the 5-hour limit off pace, agents at work). On the
// desktop a small bar draws the context by part. As the band narrows each
// segment takes a short form, and then the least urgent ones go (lib/band.js).

export type Segment = {
  key: string
  text: string
  /** The words when the band is narrow; none when the segment has no shorter form. */
  short?: string
  /** Something to act on: the spend gives way before it does. */
  urgent?: boolean
  color?: string
  bold?: boolean
}

const ICONS = { plenty: '○', filling: '◐', soon: '◕', next: '●' } as const
const COLORS = { plenty: 'success', filling: 'suggestion', soon: 'warning', next: 'error' } as const

/** Cells the open button takes, and the desktop bar's width in cells. */
const BUTTON_CELLS = 14
const BAR_PIXELS = 120
const BAR_CELLS = 16
/** The cache segment appears in the last fifth of its lifetime. */
const ENDING_FRACTION = 0.2
/** A 5-hour limit that will run out within this long counts as more pressing than a cache about to expire. */
const PACE_SOON_MS = 60 * 60 * 1000

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
  const full = fill(COPY['band.full'], { pct: percent(reading.pct) })
  const runwayShort =
    left === null || left < 1
      ? fill(COPY['band.point.short'], { tokens: formatTokens(reading.point) })
      : left === 1
        ? COPY['band.runway.one.short']
        : fill(COPY['band.runway.short'], { replies: left })
  return {
    key: 'context',
    text: `${ICONS[state]} ${joinParts([word, full, runwayText, tokens])}`,
    short: `${ICONS[state]} ${joinParts([word, full, runwayShort])}`,
    color: COLORS[state],
    bold: word !== '',
  }
}

function spend(view: View, working: boolean): Segment | null {
  const { snap, settings } = view
  if (snap.cost === null) return null
  const subscription = settings.billing.mode === 'subscription'
  const limit = subscription && snap.fiveHour !== null ? percent(snap.fiveHour.pct) : null
  // On a subscription the dollars mean little, and the share of the weekly limit they are worth is
  // often not known: the 5-hour figure is what Claude Code reports, so the simple view shows that.
  if (limit !== null && !view.detailed) {
    return { key: 'spend', text: fill(COPY['band.limit'], { pct: limit }), short: fill(COPY['band.limit.short'], { pct: limit }) }
  }

  const change = working ? snap.turnUsd : snap.lastReplyUsd
  const changeText = !subscription && isVisibleChange(change) ? ` ${fill(COPY['band.change'], { amount: shortAmount(change, settings) })}` : ''
  const text = `${fill(COPY['band.cost'], { amount: amount(snap.cost, settings) })}${changeText}`

  const extra: string[] = []
  if (view.detailed) {
    if (limit !== null) extra.push(fill(COPY['band.limit'], { pct: limit }))
    const usual = settings.usual.sessionCostUsd
    if (usual !== null) {
      const key = snap.cost <= usual ? 'band.usual.under' : 'band.usual'
      extra.push(fill(COPY[key], { amount: amount(usual, settings) }))
    }
  }
  const short = limit !== null ? fill(COPY['band.limit.short'], { pct: limit }) : `${subscription ? amount(snap.cost, settings) : shortAmount(snap.cost, settings)}${changeText}`
  return { key: 'spend', text: joinParts([text, ...extra]), short }
}

/**
 * The one segment that appears only when it matters, the most pressing first: a summary
 * that may have dropped something, a cold cache over a big context, a 5-hour limit that
 * runs out within the hour, a cache about to expire, a limit that runs out before it
 * resets, then agents at work. The ones before the agents are things to act on (urgent).
 */
function conditional(view: View, working: boolean, review: GlassReview): Segment | null {
  const { cache, settings, snap, tokens } = view
  // A summary that may have dropped something the user said comes first, until they have looked.
  if (hasBadge(review)) {
    const one = review.missing === 1
    return {
      key: 'review',
      text: one ? COPY['band.missing.one'] : fill(COPY['band.missing'], { count: review.missing }),
      short: one ? COPY['band.missing.one.short'] : fill(COPY['band.missing.short'], { count: review.missing }),
      urgent: true,
      color: 'warning',
    }
  }
  if (!working && cache !== null && !cache.isWarm && tokens >= coldMin(settings)) {
    const usd = sendCostNow(view, false)
    // On a subscription with no share known, list-price dollars would mean little: say what happens instead.
    if (!amountHasShare(usd, snap, settings)) {
      return { key: 'cache', text: COPY['band.cache.cold.plain'], short: COPY['band.cache.cold.plain.short'], urgent: true, color: 'warning' }
    }
    const cost = amountOfAfterAbout(usd, snap, settings)
    return {
      key: 'cache',
      text: fill(COPY['band.cache.cold'], { amount: cost }),
      short: fill(COPY['band.cache.cold.short'], { amount: cost }),
      urgent: true,
      color: 'warning',
    }
  }
  const pace = view.pace
  const paceTime = pace === null ? null : localTime(pace.runsOutAt)
  const paceSegment = (): Segment | null =>
    pace === null || paceTime === null
      ? null
      : {
          key: 'pace',
          text: fill(COPY['band.pace'], { pct: percent(pace.pct), time: paceTime }),
          short: fill(COPY['band.pace.short'], { time: paceTime }),
          urgent: true,
          color: 'warning',
        }
  if (pace !== null && pace.runsOutAt - view.now <= PACE_SOON_MS) {
    const found = paceSegment()
    if (found !== null) return found
  }
  if (!working && cache !== null && cache.isWarm && cache.fractionLeft <= ENDING_FRACTION) {
    const time = clock(cache.remainingMs)
    return { key: 'cache', text: fill(COPY['band.cache.ending'], { time }), short: fill(COPY['band.cache.ending.short'], { time }), urgent: true, color: 'warning' }
  }
  const later = paceSegment()
  if (later !== null) return later
  // Agents at work: the lowest of the segments that appear only when they matter.
  const agents = runningCount(snap)
  if (agents > 0) {
    return {
      key: 'agents',
      text: agents === 1 ? COPY['band.agents.one'] : fill(COPY['band.agents'], { count: agents }),
      short: agents === 1 ? COPY['band.agents.one.short'] : fill(COPY['band.agents.short'], { count: agents }),
    }
  }
  if (view.detailed && !working && cache !== null && cache.isWarm) {
    const time = clock(cache.remainingMs)
    return { key: 'cache', text: fill(COPY['band.cache.warm'], { time }), short: fill(COPY['band.cache.warm.short'], { time }) }
  }
  return null
}

/** The segments to draw, at most three, in the order they are drawn. */
export function segmentsOf(view: View, working: boolean, review: GlassReview = emptyReview()): Segment[] {
  return [context(view), spend(view, working), conditional(view, working, review)].filter((one): one is Segment => one !== null)
}

/** The summary check found something and the user has not looked yet. */
export function hasBadge(review: GlassReview): boolean {
  return review.after !== null && review.missing > 0 && !review.seen
}

/** The offer to review before a summary: the context is near the point (or a summary was held) and there is something to pick. */
export function hasOffer(view: View, review: GlassReview): boolean {
  const state = view.snap.reading?.state
  return review.suggest > 0 && !hasBadge(review) && (state === 'soon' || state === 'next' || review.held)
}

/**
 * The segments, in the form that fits `columns` cells, and whether the bar fits. The order
 * things narrow in is lib/band.js's: short forms first, then what is not urgent goes. The
 * bar shows only when every segment fits at full length.
 */
export function fit(segments: Segment[], columns: number, hasBar: boolean): { segments: Segment[]; bar: boolean } {
  const pieces = segments.map(one => ({
    role: one.key === 'context' ? ('context' as const) : one.key === 'spend' ? ('spend' as const) : ('extra' as const),
    key: one.key,
    full: one.text.length,
    short: one.short === undefined ? null : one.short.length,
    urgent: one.urgent === true,
  }))
  const shape = narrow(pieces, columns - BUTTON_CELLS, hasBar ? BAR_CELLS : 0)
  const kept = segments
    .filter(one => shape.forms[one.key] !== 'gone')
    .map(one => (shape.forms[one.key] === 'short' && one.short !== undefined ? { ...one, text: one.short } : one))
  return { segments: kept, bar: shape.bar }
}

export const register: Register = on => {
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const base = await next(e)
    if (e.props.hasSurvey) return base
    const view = drawView(await read($, snapAtom), await read($, settingsAtom), await read($, detailedAtom), await $.clock.now(), e.props.isWorking)
    const review = sound(await read($, reviewAtom))
    const segments = segmentsOf(view, e.props.isWorking, review)
    if (view.snap.reading === null || segments.length === 0) return base

    const table = $.ui.resolve(e)
    // The terminal has no Svg; its table answers any name, so ask the surface.
    const Svg = e.surface !== 'terminal' && 'Svg' in table ? table.Svg : null
    const { Box, Text, Button } = table
    const parts = view.parts
    // The review buttons take room beside the Glass button.
    const extra = (hasBadge(review) ? COPY['band.review'].length + 4 : 0) + (hasOffer(view, review) ? COPY['band.review.before'].length + 4 : 0)
    const shown = fit(segments, e.props.bodyColumns - extra, Svg !== null && parts !== null)

    let bar = null
    if (shown.bar && Svg !== null && parts !== null && view.snap.reading !== null) {
      const alt = barAlt(parts, percent(view.snap.reading.pct))
      const source = partsBarSvg(parts, BAR_PIXELS, 10, alt)
      if (source.length <= SVG_LIMIT) bar = <Svg key="bar" source={source} alt={alt} width={BAR_PIXELS} height={10} isInteractive />
    }

    const showBadge = shown.segments.some(one => one.key === 'review')
    const showOffer = hasOffer(view, review)
    const openReview = async (which: 'before' | 'after') => {
      await update($, reviewAtom, was => ({ ...sound(was), seen: which === 'after' ? true : sound(was).seen, ui: { ...sound(was).ui, view: which, note: null } }))
      await update($, pageAtom, () => 'compaction')
      await $.store.set('page', 'compaction')
      void $.ui.open(OPEN_ARGS)
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
          {showBadge ? <Button key="review" label={COPY['band.review']} hotkey="r" onPress={() => void openReview('after')} /> : null}
          {showOffer ? <Button key="review-before" label={COPY['band.review.before']} hotkey="r" onPress={() => void openReview('before')} /> : null}
          <Button key="open" label={COPY['band.open']} hotkey="g" onPress={() => void $.ui.open(OPEN_ARGS)} />
        </Box>
      </Box>
    )
  })
}
