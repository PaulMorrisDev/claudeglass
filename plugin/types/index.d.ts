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
  /** Characters per token of text, by model family (the calibration), and the family a session on an unknown model counts as. */
  charsPerToken: { text: Record<string, number>; family: string }
  /**
   * Whether coaching is on for this user, and what the capture hook uses to decide it (the file's `coaching` block).
   * `enabled` is false unless `coaching_notes` is in `[capture] coaching`, and in the fallback.
   */
  coaching: {
    enabled: boolean
    /** The slug patterns of `[capture] projects` (a leading `!` leaves a project out) and `exclude_projects`, as the user wrote them. */
    projects: string[]
    excludeProjects: string[]
    /** False when `coaching.json` has `plan_fresh: false`. */
    planFresh: boolean
  }
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
}

/** One agent run of this session. The task prompt is never kept here (agents.ts holds it in memory). */
export type GlassAgent = {
  /** The id its loop's events carry (`agentId`). */
  id: string
  /** The Agent tool call that started it; empty for a workflow's agent that has none. */
  toolUseId: string
  /** The agent type (`Explore`, `general-purpose`); empty until known. */
  type: string
  /** The task's short description. */
  desc: string
  model: string
  background: boolean
  fork: boolean
  status: 'running' | 'done' | 'failed'
  startedAt: number
  endedAt: number | null
  requests: number
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
  /** Estimated from the rates in the settings; 0 for a model with no price. */
  usd: number
  /** Tool calls, and how many of each tool. */
  calls: number
  tools: Record<string, number>
  /** What its latest tool call was doing: a COPY key's last word (`agent.<activity>`). */
  activity: 'searching' | 'reading' | 'editing' | 'running' | 'fetching' | 'working'
  /** Characters of the task prompt, and times it summarised its own context. */
  briefChars: number
  compactions: number
  /** The agent that started it, when another agent did (its row has no Agent call of the main session's under it). */
  parentAgentId?: string
  /**
   * Set when a workflow script's `agent()` started it: the run's id and the Workflow call that began the run. The engine's
   * agent list leaves a workflow's agents out, so a row like this is not finished for being missing from it.
   */
  workflow?: { runId: string; call: string }
  /** When an event of its own last arrived (ms); a workflow's row with none for a long while is finished (agents.ts). */
  seenAt?: number
}

export type GlassSensorEvent =
  | { ts: number; event: 'present'; surface: string }
  | { ts: number; event: 'note'; kind: string; surface: string; outcome: string }
  | { ts: number; event: 'rebuild'; kind: string; outcome: 'expected' | 'unexpected' }
  | { ts: number; event: 'cold_send'; outcome: 'warned' | 'unwarned' }
  | {
      ts: number
      event: 'compaction'
      kind: GlassReviewTrigger
      kept: number
      missing: number
      added: number
      /** 1 when the hook gave the summariser a keep instruction, 0 when it did not. */
      instructed?: 0 | 1
      /** Put-back items the replies since have referenced, as of the summary. */
      referenced?: number
    }
  | { ts: number; event: 'tweak'; kind: GlassTweakKind; before: number; after?: number }

/** The buttons the mod offers whose press the sensor counts: summarise with the keep list, re-add, check with a model, hold. */
export type GlassTweakKind = 'summarise' | 'readd' | 'check' | 'hold'

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
  /** Where Claude Code summarises, in tokens; null when auto-compact is off (no automatic summary will happen). */
  point: number | null
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
  /**
   * The count of replies when the surface was last asked for with none known (ensure() in track.ts asks again at most once
   * per reply: a Desktop session starts with no surface and the client attaches later); null before it was asked that way.
   */
  surfaceAskedAt: number | null
  reading: GlassContextReading | null
  cost: number | null
  turnStartCost: number | null
  fiveHour: { pct: number; resetsAt: string | null } | null
  /** True once the 5-hour limit has been reached. */
  limitHit: boolean
  /** Session dollars and percent when this 5-hour window was first seen. */
  calBase: { usd: number; pct: number; resetsAt: string | null } | null
  /**
   * True while another session's heartbeat is fresh: the 5-hour percent moves with every session's spend, so this
   * session's share of it can't be told (limit.ts gives none). Checked at most once a minute (heartbeat.ts).
   */
  parallel: boolean
  reqs: GlassRequest[]
  /** The names of the tools the main session has called, once each. */
  toolsUsed: string[]
  firstCr: number | null
  startupFirst: number | null
  /**
   * What loads before the first message, as Claude Code counted it (a `full` breakdown, asked for once a session, after its
   * first reply): the name and tokens of each used category but Messages. It stands in for the estimates the per-reply
   * `summary` breakdown gives (lib/context.js `withCounted`). Null until counted; empty when Claude Code could not count.
   */
  startupCounted: { name: string; tokens: number }[] | null
  model: string | null
  /** Context after each reply, oldest first. */
  history: number[]
  replies: number
  lastTs: number | null
  compactions: GlassCompaction[]
  pendingCompaction: boolean
  toolKept: number
  rest: GlassRestState
  lastNoteReply: number | null
  /** Requests in the reply being worked on. */
  turnReqs: number
  /** When the main reply being worked on began (ms); null between replies. */
  turnAt: number | null
  /** The id of the main reply last begun (the `turnId` its first request carried): a request with another id begins the next reply, a subagent's never does. */
  turnId: string | null
  /**
   * What the reply being worked on has cost so far, in USD: each request priced from its usage, replaced by Claude Code's own
   * figure at each measurement. Between replies it is what the reply that ended cost (until the next one begins).
   */
  turnUsd: number
  /** False while `turnUsd` rests on the prices alone, with no measurement from Claude Code since the last request. */
  turnExact: boolean
  events: GlassSensorEvent[]
  pending: GlassPendingNote[]
  /** True when events changed since the sensor file was last written. */
  eventsDirty: boolean
  /** True once this process has read the session's sensor file left by an earlier process and added its events to `events` (sensor.ts writeSensor). */
  sensorMerged: boolean
  /** When the heartbeat was last written, in ms. */
  lastBeat: number
  /** The agents that ran in this session, oldest first. */
  agents: GlassAgent[]
}

/** A thing the user pinned to the keep list (compaction review). The text is theirs and lives in $.state only. */
export type GlassPin = { id: string; text: string }

/** One thing a summary must not lose (compaction review). Built from the session's messages, in memory and in $.state only. */
export type GlassKeepItem = {
  id: string
  kind: 'pin' | 'instruction' | 'decision' | 'error' | 'task' | 'file' | 'value'
  text: string
  /** Claude Code keeps it whatever the summary says. */
  safe: boolean
  why: '' | 'recent' | 'plan' | 'claude-md'
  /** Strings the summary must hold word for word (paths, ports, ids). */
  exact: string[]
  at: number
}

export type GlassMark = 'found' | 'partly' | 'missing' | 'safe'

/** What made a reviewed summary: the user's /compact, Claude Code at its threshold, or a plugin (a summary made ahead, `precompute`, is never reviewed). */
export type GlassReviewTrigger = 'auto' | 'manual' | 'plugin'

/**
 * What followed a put-back item (compaction review): when it was put back (ms), the main replies seen since, and
 * whether one of them referenced it (null until a reply does, or enough replies have passed without).
 */
export type GlassReceipt = { at: number; replies: number; referenced: boolean | null }

/** The check of the last summary against the keep list. */
export type GlassReviewed = {
  ts: number
  trigger: GlassReviewTrigger
  /** The summary Claude Code wrote (held in memory for the page; never written to disk). */
  summary: string
  items: GlassKeepItem[]
  marks: Record<string, GlassMark>
  /** True once Haiku's check replaced the quick check's marks. */
  judged: boolean
  /** The tokens that check used (input and output); absent before one. */
  judgedTokens?: number
  /** Prompts from before the summary, newest first. */
  earlier: { id: string; text: string }[]
}

/** One summary of this session, as the history lists it. */
export type GlassReviewRow = {
  ts: number
  trigger: GlassReviewTrigger
  before: number | null
  after: number | null
  kept: number
  missing: number
}

/** What the page remembers between presses. */
export type GlassReviewUi = {
  /** Which view the page opens on: the one before a summary or the one after. */
  view: 'before' | 'after'
  /** Ids of the items whose tick differs from the default (suggested items start ticked, the rest not). */
  toggled: string[]
  /** The summary text is shown. */
  summary: boolean
  /** The full list (after) or the more items (before) are shown. */
  list: boolean
  /** The earlier messages are shown. */
  earlier: boolean
  /** The Haiku check is running. */
  busy: boolean
  /** The press being carried out, while its Button shows it is busy; null when none. */
  action?: 'summarise' | 'readd' | 'check' | null
  /** One line under the section whose press set it (`noteFor`): what that press did. */
  note: string | null
  /** The section the note is drawn under; null when there is no note. */
  noteFor?: 'summarise' | 'readd' | 'check' | 'pin' | null
}

/** The compaction review: counts the band needs, and what the page draws from. */
export type GlassReview = {
  /** When the last reviewed summary was made, in ms; null before one. */
  ts: number | null
  /** Items the user said that the summary dropped or kept only in part (the ones the page offers to put back). */
  missing: number
  /** The count of main replies when the last summary was made; the band shows Kept while it is still the count. Null before one. */
  repliesAt?: number | null
  /** True once the user has opened the review for that summary. */
  seen: boolean
  /** True while an automatic summary is being held for review. */
  held: boolean
  /** Items worth reviewing before a summary (pins, instructions and decisions). */
  suggest: number
  /** The keep list as of the last reply. */
  list: GlassKeepItem[]
  /** Items the user put back after a summary lost them: kept in the list and sent to the summariser until a summary holds them. */
  readded: GlassKeepItem[]
  /** The receipt of each put-back item, by its id: what the replies since said of it. */
  receipts: Record<string, GlassReceipt>
  after: GlassReviewed | null
  history: GlassReviewRow[]
  ui: GlassReviewUi
}

declare module 'claude-code' {
  interface PluginState {
    claudeglass: {
      snap: GlassSnap
      settings: GlassSettings | null
      detailed: boolean
      /** The glossary entry the pane is explaining, if any. */
      explain: string | null
      /** The keep list's pins (compaction review). */
      pins: GlassPin[]
      review: GlassReview
    }
  }
}
