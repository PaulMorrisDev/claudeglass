import type { GlassSettings } from '../types'
import { THRESHOLDS } from './generated/coaching.js'
import { RATES } from './generated/rates.js'
import { parseTtl } from './lib/cache.js'
import { REBUILD_DEFAULTS } from './lib/rebuild.js'
import type { Io } from './io'

// The settings file ClaudeGlass writes (src/claudeglass/mod_settings.py) and
// the fallback the mod uses when it can't read it. The file is read with
// the file at session.start and again at most every ten minutes.

export const SETTINGS_REFRESH_MS = 10 * 60 * 1000
const SCHEMA = 1

type Json = Record<string, unknown>

function record(value: unknown): Json | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Json) : null
}

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function words(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((one): one is string => typeof one === 'string') : []
}

function numbers(value: unknown): Record<string, number> {
  const out: Record<string, number> = {}
  const source = record(value)
  if (source === null) return out
  for (const key of Object.keys(source)) {
    const number = finite(source[key])
    if (number !== null) out[key] = number
  }
  return out
}

/** What the mod uses when the settings file is missing, unreadable or of another schema. */
export function fallbackSettings(): GlassSettings {
  return {
    isFallback: true,
    loadedAt: 0,
    billing: { mode: 'unknown', share_per_usd: null, period_label: 'weekly usage limit', currency: 'USD' },
    models: RATES.models as GlassSettings['models'],
    ttl: 300,
    ttlSource: 'default',
    thresholds: { ...THRESHOLDS },
    rebuild: { ...REBUILD_DEFAULTS },
    muted: [],
    once: [],
    usual: { sessionCostUsd: null, endContextTokens: null },
    charsPerToken: { text: {}, family: '' },
    typicalPiece: 0,
    dashboardUrl: null,
  }
}

/** The settings file's text as the mod reads it, or null for anything it can't use. */
export function parseSettings(text: string, now: number): GlassSettings | null {
  let raw: unknown
  try {
    raw = JSON.parse(text)
  } catch {
    return null
  }
  const file = record(raw)
  if (file === null || file.schema !== SCHEMA) return null

  const base = fallbackSettings()
  const billing = record(file.billing)
  const mode = billing?.mode
  const pricing = record(file.pricing)
  const models = record(pricing?.models)
  const cache = record(file.cache)
  const coaching = record(file.coaching)
  const usual = record(file.usual)
  const calibration = record(file.calibration)
  const ttl = parseTtl(cache?.ttl_seconds)
  const source = cache?.source
  const url = typeof file.dashboard_url === 'string' && file.dashboard_url !== '' ? file.dashboard_url.replace(/\/+$/, '') : null

  return {
    isFallback: false,
    loadedAt: now,
    billing: {
      mode: mode === 'api' || mode === 'subscription' ? mode : 'unknown',
      share_per_usd: finite(billing?.share_per_usd),
      period_label: typeof billing?.period_label === 'string' && billing.period_label !== '' ? billing.period_label : base.billing.period_label,
      currency: typeof file.currency === 'string' && file.currency !== '' ? file.currency : 'USD',
    },
    models: models !== null && Object.keys(models).length > 0 ? (models as GlassSettings['models']) : base.models,
    ttl: ttl !== null && ttl > 0 ? ttl : base.ttl,
    ttlSource: source === 'config' || source === 'history' ? source : 'default',
    thresholds: { ...THRESHOLDS, ...numbers(coaching?.thresholds) },
    rebuild: { ...REBUILD_DEFAULTS, ...numbers(file.rebuild) },
    muted: words(coaching?.muted),
    once: words(coaching?.once),
    usual: { sessionCostUsd: finite(usual?.session_cost_usd), endContextTokens: finite(usual?.end_context_tokens) },
    charsPerToken: { text: numbers(calibration?.text), family: typeof calibration?.default_family === 'string' ? calibration.default_family : '' },
    typicalPiece: finite(coaching?.typical_piece_tokens) ?? 0,
    dashboardUrl: url,
  }
}

function trimSlash(path: string): string {
  return path.replace(/[\\/]+$/, '')
}

/** `<config_dir>`: `$CLAUDE_CONFIG_DIR/claudeglass`, else `~/.claude/claudeglass` (config._default_config_dir). */
export async function configDir(io: Io): Promise<string> {
  const places = await io.places()
  if (places.configDir) return `${trimSlash(places.configDir)}/claudeglass`
  const home = places.userProfile ?? places.home ?? ''
  return `${trimSlash(home)}/.claude/claudeglass`
}

let cached: GlassSettings | null = null
let loading: Promise<GlassSettings> | null = null

async function load(io: Io): Promise<GlassSettings> {
  const now = await io.now()
  // A later read that fails keeps the last good settings rather than falling back.
  let settings = cached !== null && !cached.isFallback ? cached : fallbackSettings()
  try {
    const dir = await configDir(io)
    const text = await io.readFile(`${dir}/mod/settings.json`)
    const parsed = typeof text === 'string' ? parseSettings(text, now) : null
    if (parsed !== null) settings = parsed
  } catch {
    // Missing or unreadable: the fallback stands.
  }
  settings = { ...settings, loadedAt: now }
  cached = settings
  await io.putSettings(settings)
  return settings
}

/** Reads the file again now. */
export function reloadSettings(io: Io): Promise<GlassSettings> {
  loading ??= load(io).finally(() => {
    loading = null
  })
  return loading
}

/**
 * The settings, from memory or the state they were mirrored to; read from
 * the file the first time and again once they are older than ten minutes.
 */
export async function getSettings(io: Io): Promise<GlassSettings> {
  if (cached === null) cached = await io.getSettings()
  if (cached === null) return reloadSettings(io)
  const now = await io.now()
  if (now - cached.loadedAt > SETTINGS_REFRESH_MS) return reloadSettings(io)
  return cached
}
