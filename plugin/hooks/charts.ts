import { ENTITIES, PALETTE } from './generated/palette.js'
import { COPY } from './generated/copy.js'
import { fill } from './lib/text.js'
import { formatTokens } from './lib/units.js'
import type { Parts } from './view'

// The drawings. On the desktop they are SVG, coloured from the dashboard's own
// palette; the drawing carries a light and a dark set and picks by the viewer's
// scheme (a stylesheet inside the SVG), because the mod can't read the theme.
// On the terminal they are block characters. The shades differ along the bar
// (and the estimated part is hatched), so colour is never the only signal.

/** The most characters one SVG may have. */
export const SVG_LIMIT = 40000

type Pair = { light: string; dark: string }

function part(name: 'startup' | 'conversation' | 'output' | 'free'): Pair {
  return ENTITIES.part[name]
}

function ink(name: keyof typeof PALETTE.light): Pair {
  return { light: PALETTE.light[name], dark: PALETTE.dark[name] }
}

/** `.cls { fill: ... }` for the light scheme, and the dark values under a media query. */
function sheet(rules: Record<string, Pair>, property = 'fill'): string {
  const light = Object.entries(rules).map(([cls, pair]) => `.${cls}{${property}:${pair.light}}`)
  const dark = Object.entries(rules).map(([cls, pair]) => `.${cls}{${property}:${pair.dark}}`)
  return `<style>${light.join('')}@media (prefers-color-scheme: dark){${dark.join('')}}</style>`
}

function esc(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

function svg(width: number, height: number, title: string, inner: string): string {
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img">` +
    `<title>${esc(title)}</title>${inner}</svg>`
  )
}

function round(value: number): number {
  return Math.round(value * 10) / 10
}

/** The width the bar is drawn to: the point Claude summarises at, plus a little past it. */
function span(parts: Parts): number {
  return Math.max(parts.point * 1.15, parts.tokens * 1.02, 1)
}

/** The alt text of the bar, with its parts and where Claude summarises. */
export function barAlt(parts: Parts, pct: string): string {
  return fill(COPY['band.bar.alt'], {
    full: fill(COPY['band.full'], { pct }),
    startup: formatTokens(parts.startup),
    conversation: formatTokens(parts.conversation),
    output: formatTokens(parts.output),
    point: formatTokens(parts.point),
  })
}

/** How full the context is, by part, with a mark where Claude summarises. */
export function partsBarSvg(parts: Parts, width: number, height: number, title: string): string {
  const total = span(parts)
  const x = (tokens: number) => round((tokens / total) * width)
  const startupW = x(parts.startup)
  const convoW = x(parts.conversation)
  const outputW = x(parts.output)
  const mark = x(parts.point)
  const radius = Math.min(3, height / 2)
  const rules = sheet({
    st: part('startup'),
    cv: part('conversation'),
    ou: part('output'),
    fr: part('free'),
    mk: ink('ink-1'),
  })
  const inner =
    rules +
    `<defs><clipPath id="c"><rect width="${width}" height="${height}" rx="${radius}"/></clipPath>` +
    `<pattern id="h" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="4" height="4" class="ou"/><rect width="2" height="4" fill="#fff" fill-opacity="0.45"/></pattern></defs>` +
    `<g clip-path="url(#c)">` +
    `<rect width="${width}" height="${height}" class="fr" fill-opacity="0.28"/>` +
    `<rect x="${mark}" width="${Math.max(0, width - mark)}" height="${height}" class="fr" fill-opacity="0.2"/>` +
    `<rect width="${startupW}" height="${height}" class="st"/>` +
    `<rect x="${startupW}" width="${convoW}" height="${height}" class="cv"/>` +
    `<rect x="${round(startupW + convoW)}" width="${outputW}" height="${height}" fill="url(#h)"/>` +
    `</g>` +
    `<rect x="${Math.max(0, mark - 1)}" width="2" height="${height}" class="mk"/>`
  return svg(width, height, title, inner)
}

/** The cost of each reply as a bar, with the context after it as a line over them. */
export function replyChartSvg(replies: { usd: number; ctx: number }[], width: number, height: number, title: string, label: (index: number, usd: number, ctx: number) => string): string {
  const count = replies.length
  const maxUsd = Math.max(...replies.map(one => one.usd), 0.0001)
  const maxCtx = Math.max(...replies.map(one => one.ctx), 1)
  const slot = width / Math.max(count, 1)
  const barW = Math.max(1, Math.min(14, slot - 2))
  const rules = sheet({ bar: ink('accent'), axis: ink('axis-line') }) + sheet({ line: ink('ink-2') }, 'stroke')
  let bars = ''
  let points = ''
  replies.forEach((one, index) => {
    const h = round(Math.max(1, (one.usd / maxUsd) * (height - 4)))
    const left = round(index * slot + (slot - barW) / 2)
    bars += `<rect x="${left}" y="${round(height - h)}" width="${round(barW)}" height="${h}" class="bar"><title>${esc(label(index, one.usd, one.ctx))}</title></rect>`
    points += `${round(left + barW / 2)},${round(height - (one.ctx / maxCtx) * (height - 4) - 1)} `
  })
  const inner = rules + `<rect y="${height - 1}" width="${width}" height="1" class="axis"/>` + bars + `<polyline points="${points.trim()}" fill="none" stroke-width="1.5" class="line"/>`
  return svg(width, height, title, inner)
}

/** The 5-hour limit: how much is used, with a cursor for how much of the window has passed. */
export function limitBarSvg(pct: number, elapsed: number, width: number, height: number, title: string): string {
  const used = round((Math.min(100, Math.max(0, pct)) / 100) * width)
  const cursor = round(Math.min(1, Math.max(0, elapsed)) * width)
  const ahead = pct / 100 > elapsed
  const rules = sheet({ track: ink('border'), ok: ink('good'), over: ink('serious'), now: ink('ink-1') })
  const inner =
    rules +
    `<rect width="${width}" height="${height}" rx="3" class="track"/>` +
    `<rect width="${used}" height="${height}" rx="3" class="${ahead ? 'over' : 'ok'}"/>` +
    `<rect x="${Math.max(0, cursor - 1)}" width="2" height="${height}" class="now"/>`
  return svg(width, height, title, inner)
}

const BLOCKS = '▁▂▃▄▅▆▇█'

/** Values as a row of blocks, each as tall as it is against the largest. */
export function sparkline(values: number[]): string {
  const max = Math.max(...values, 0)
  if (max <= 0) return values.map(() => BLOCKS[0]).join('')
  return values.map(value => BLOCKS[Math.min(BLOCKS.length - 1, Math.floor((value / max) * (BLOCKS.length - 1) + 0.5))]).join('')
}

/**
 * The context as characters: startup █, conversation ▓, tool output kept ▒, room
 * left ░, with │ where Claude summarises. `width` counts the cells between the ends.
 */
export function textBar(parts: Parts, width: number): string {
  const total = span(parts)
  const cells = (tokens: number) => Math.round((tokens / total) * width)
  const mark = Math.min(width - 1, Math.max(0, cells(parts.point)))
  let startup = cells(parts.startup)
  let convo = cells(parts.conversation)
  let output = cells(parts.output)
  while (startup + convo + output > mark) {
    if (convo > 0) convo -= 1
    else if (startup > 0) startup -= 1
    else output -= 1
  }
  const used = startup + convo + output
  const left = '█'.repeat(startup) + '▓'.repeat(convo) + '▒'.repeat(output) + '░'.repeat(Math.max(0, mark - used))
  return `▕${left}│${'░'.repeat(Math.max(0, width - mark - 1))}▏`
}
