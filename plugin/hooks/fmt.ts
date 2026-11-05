import type { GlassSettings, GlassSnap } from '../types'
import { COPY } from './generated/copy.js'
import { formatClock } from './lib/cache.js'
import { fill } from './lib/text.js'
import { formatAmount, formatTokens } from './lib/units.js'
import { limitShare } from './limit'

// Amounts and times in the words the user's billing mode calls for. API
// billing reads in dollars ("$0.38"), a subscription as a share of its limit,
// and an unknown mode as list-price dollars, never a bare "$".

export { formatTokens }

export function billingOf(settings: GlassSettings) {
  const { mode, share_per_usd, period_label, currency } = settings.billing
  return { mode, share_per_usd, period_label, currency }
}

/** Appends "(est.)" when the figure rests on the fallback rates. */
export function withEst(text: string, settings: GlassSettings): string {
  return settings.isFallback ? `${text} ${COPY['amount.est']}` : text
}

/** An amount in the billing mode's own words: "$0.38", "about 0.4% of your weekly usage limit", "0.38 USD list-price equivalent". */
export function amount(usd: number, settings: GlassSettings): string {
  const billing = billingOf(settings)
  if (billing.mode === 'api') return formatAmount(usd, billing, { symbol: true })
  if (billing.mode === 'subscription') return formatAmount(usd, billing)
  const dollars = formatAmount(usd, { ...billing, mode: 'api' })
  return fill(COPY['amount.list'], { dollars })
}

/** A share of the 5-hour limit in percent: two decimals below 1%, so a small amount doesn't read "0.0%"; one above. */
function shareText(share: number): string {
  return share < 1 ? `${share.toFixed(2)}%` : `${share.toFixed(1)}%`
}

/**
 * An amount for the session in hand. On a subscription it is a share of the 5-hour
 * limit when that is known (limit.ts), else what `amount` says: the weekly share, else
 * the list price. Elsewhere it is `amount`.
 */
export function amountOf(usd: number, snap: GlassSnap, settings: GlassSettings): string {
  if (settings.billing.mode === 'subscription') {
    const share = limitShare(snap, usd)
    if (share !== null) return fill(COPY['amount.share.limit'], { share: shareText(share) })
  }
  return amount(usd, settings)
}

/** True when `amountOf` can phrase an amount without falling back to list-price dollars (always off a subscription). */
export function amountHasShare(usd: number, snap: GlassSnap, settings: GlassSettings): boolean {
  if (settings.billing.mode !== 'subscription') return true
  const perUsd = settings.billing.share_per_usd
  return limitShare(snap, usd) !== null || (typeof perUsd === 'number' && perUsd > 0)
}

/** The same, without its leading "about ", for a sentence that writes "about" itself. */
export function amountOfAfterAbout(usd: number, snap: GlassSnap, settings: GlassSettings): string {
  return amountOf(usd, snap, settings).replace(/^about /, '')
}

/** A short change for a tight spot: "$0.04" on the API, "0.04 USD" elsewhere. */
export function shortAmount(usd: number, settings: GlassSettings): string {
  const billing = billingOf(settings)
  return formatAmount(usd, { ...billing, mode: 'api' }, billing.mode === 'api' ? { symbol: true } : undefined)
}

/** True when a change is big enough to write down. */
export function isVisibleChange(usd: number | null): usd is number {
  return usd !== null && usd >= 0.005
}

/** A countdown as "m:ss" ("0:45", "4:12"). */
export function clock(ms: number): string {
  return formatClock(ms).replace(/^0(?=\d:)/, '')
}

export function minutes(ms: number): number {
  return Math.max(1, Math.round(ms / 60000))
}

/** A wall-clock time such as "3:40 pm" in the engine's local time zone; null when it can't be worked out. */
export function localTime(ms: number): string | null {
  try {
    const date = new Date(ms)
    const hours = date.getHours()
    const mins = date.getMinutes()
    if (!Number.isFinite(hours) || !Number.isFinite(mins)) return null
    const hour = hours % 12 === 0 ? 12 : hours % 12
    return `${hour}:${mins < 10 ? '0' : ''}${mins} ${hours < 12 ? 'am' : 'pm'}`
  } catch {
    return null
  }
}

export function percent(value: number): string {
  return String(Math.round(value))
}
