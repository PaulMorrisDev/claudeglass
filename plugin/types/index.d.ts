// The ClaudeGlass mod's $.state contract. Everything the mod draws from lives
// here, so a hot reload keeps it; the hooks write it, the render hooks read it.

export type GlassModelRates = {
  aliases?: string[]
  input: number
  output: number
  cache_write_5m: number
  cache_write_1h: number
  cache_read: number
  context_window_tokens?: number
}

/** The settings file as the mod reads it, or the fallback when it can't. */
export type GlassSettings = {
  /** True when the file was missing, unreadable or of an unknown schema. */
  isFallback: boolean
  loadedAt: number
  billing: {
    mode: 'api' | 'subscription' | 'unknown'
    share_per_usd: number | null
    period_label: string
    currency: string
  }
  models: Record<string, GlassModelRates>
  /** Cache lifetime in seconds. */
  ttl: number
  /** Where the lifetime came from: the user's config, their history, or the default. */
  ttlSource: 'config' | 'history' | 'default'
  thresholds: Record<string, number>
  rebuild: Record<string, number>
  muted: string[]
  once: string[]
  usual: { sessionCostUsd: number | null; endContextTokens: number | null }
  dashboardUrl: string | null
}

/** One main-session model request, as recorded from turn.step. */
export type GlassRequest = {
  ts: number
  ctx: number
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
  model: string
  tools: string[]
  afterCompaction: boolean
  afterLimitWait: boolean
  first: boolean
  /** Tokens the previous request's tool results added (0 when none). */
  growth: number
}

export type GlassCompaction = {
  ts: number
  trigger: string
  tokensBefore: number | null
  tokensAfter: number | null
  wasCold: boolean
}

export type GlassItem = { name: string; tokens: number }

export type GlassRebuild = {
  cause: 'limit' | 'compaction' | 'expiry' | 'prefix' | 'unknown'
  tokens: number
  usd: number
  expected: boolean
}

export type GlassSensorEvent =
  | { ts: number; event: 'present'; surface: string }
  | { ts: number; event: 'note'; kind: string; surface: string; outcome: string }
  | { ts: number; event: 'rebuild'; kind: string; outcome: 'expected' | 'unexpected' }
  | { ts: number; event: 'cold_send'; outcome: 'warned' | 'unwarned' }

export type GlassPendingNote = {
  ts: number
  kind: string
  surface: string
  replyIndex: number
  compactions: number
  /** Tokens in the context when the note was shown. */
  ctx: number
}

export type GlassContextReading = {
  pct: number
  tokens: number
  point: number
  window: number
  state: 'plenty' | 'filling' | 'soon' | 'next'
  parts: { startup: number | null; conversation: number | null; free: number | null }
}

export type GlassRestState = {
  hints: Record<string, { ts: number; stake: number; n: number; rest: number; once?: boolean }>
}

export type GlassSnap = {
  sessionId: string | null
  /** The surface the session runs on (terminal, desktop, ...); null until known. */
  surface: string | null
  reading: GlassContextReading | null
  cost: number | null
  turnStartCost: number | null
  lastReplyUsd: number | null
  fiveHour: { pct: number; resetsAt: string | null } | null
  /** True once the 5-hour limit has been reached. */
  limitHit: boolean
  /** Session dollars and percent when this 5-hour window was first seen. */
  calBase: { usd: number; pct: number; resetsAt: string | null } | null
  reqs: GlassRequest[]
  firstCr: number | null
  startupFirst: number | null
  model: string | null
  /** Context after each reply, oldest first. */
  history: number[]
  replies: number
  replyCosts: { usd: number; ctx: number }[]
  lastTs: number | null
  compactions: GlassCompaction[]
  pendingCompaction: boolean
  toolKept: number
  heaviest: GlassItem[]
  switches: number
  mix: { read: number; write: number; input: number; output: number }
  rest: GlassRestState
  lastNoteReply: number | null
  /** Requests in the reply being worked on. */
  turnReqs: number
  turn: {
    rebuilds: GlassRebuild[]
    big: GlassItem | null
    switched: boolean
    compactedCold: GlassCompaction | null
    compacted: boolean
    switchUsd: number
  }
  events: GlassSensorEvent[]
  pending: GlassPendingNote[]
  /** True when events changed since the sensor file was last written. */
  eventsDirty: boolean
  /** When the heartbeat was last written, in ms. */
  lastBeat: number
}

declare module 'claude-code' {
  interface PluginState {
    claudeglass: {
      snap: GlassSnap
      settings: GlassSettings | null
      detailed: boolean
      page: string
      /** The glossary entry the pane is explaining, if any. */
      explain: string | null
      help: boolean
      copied: boolean
    }
  }
}
