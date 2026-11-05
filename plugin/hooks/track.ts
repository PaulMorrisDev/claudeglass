import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, TurnUsage } from 'claude-code'

import type { GlassCompaction, GlassRequest, GlassSettings, GlassSnap } from '../types'
import { armCacheClock, stopCacheClock } from './cacheclock'
import { COPY } from './generated/copy.js'
import { drainSensor } from './compaction-memory'
import { drainShown, endCoach, resetCoach } from './coach-mem'
import { beat, keepBeating, leave } from './heartbeat'
import type { Io } from './io'
import { emptySnap, flags, flushNow, forget, live, restart, touch } from './live'
import { cacheState } from './lib/cache.js'
import { countedRows, readContext, toolGrowth, withCounted } from './lib/context.js'
import { priceUsage, ratesFor } from './lib/pricing.js'
import { detectRebuild } from './lib/rebuild.js'
import { addEvent, announcePresent, isSurface, seconds, settleAfterReply, settleAtEnd, writeSensor } from './sensor'
import { getSettings, reloadSettings } from './settings'
import { coldMin } from './view'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)

// Follows the main session: Claude Code's own figures at each measurement, one
// record per model request, one per reply, and each conversation summary. All
// of it lands in the snapshot (live.ts); nothing here draws.

const MAX_REQUESTS = 200
const MAX_REPLIES = 100
const MAX_COMPACTIONS = 20
/** The most tool names kept as called this session (a session calls a few dozen at most). */
const MAX_TOOLS_USED = 200
/** A wait this long after a request that hit the usage limit counts as the limit's pause. */
const LIMIT_PAUSE_MS = 60 * 1000

export type TurnEnd = {
  io: Io
  snap: GlassSnap
  settings: GlassSettings
  now: number
  /** The reply's text and why it ended; for a turn end only. Looked at, never kept. */
  answer?: string
  reason?: string
  /** What the reply said at each step and the inputs of the tools it called; for a turn end only. Looked at, never kept. */
  texts?: string[]
}

type Listener = (end: TurnEnd) => Promise<void>

/** The most texts, and the most characters in all, held for one reply. */
const MAX_TEXTS = 64
const MAX_TEXT_CHARS = 200000

/** What the main reply now running has said and called, in memory only, until its turn ends (the reply a later one replaces is dropped). */
let said: { turnId: string; texts: string[]; chars: number } = { turnId: '', texts: [], chars: 0 }

/** Holds one text of the reply `turnId`, as much of it as the limits leave room for. */
function hold(turnId: string, text: string): void {
  if (said.turnId !== turnId) said = { turnId, texts: [], chars: 0 }
  const room = MAX_TEXT_CHARS - said.chars
  if (text === '' || said.texts.length >= MAX_TEXTS || room <= 0) return
  const kept = text.length > room ? text.slice(0, room) : text
  said.texts.push(kept)
  said.chars += kept.length
}

/** The strings in a tool's input, however deep, one per line: what the call named, without JSON's escapes in the way. */
function leaves(value: unknown, out: string[] = [], depth = 0): string[] {
  if (typeof value === 'string') out.push(value)
  else if (Array.isArray(value)) {
    if (depth < 8) for (const one of value) leaves(one, out, depth + 1)
  } else if (value !== null && typeof value === 'object' && depth < 8) for (const one of Object.values(value)) leaves(one, out, depth + 1)
  return out
}

/** The texts held for the reply `turnId`; they are emptied from memory. */
function release(turnId: string): string[] {
  const texts = said.turnId === turnId ? said.texts : []
  said = { turnId: '', texts: [], chars: 0 }
  return texts
}

const listeners: Listener[] = []

/** Runs `listener` at the end of each main-session reply, after the bookkeeping, in the order added. */
export function onTurnEnd(listener: Listener): void {
  listeners.push(listener)
}

const compactListeners: Listener[] = []

/** Runs `listener` after each main-session summary has been recorded (a manual /compact or an automatic one), in the order added. */
export function onCompaction(listener: Listener): void {
  compactListeners.push(listener)
}

const measureListeners: Listener[] = []

/** Runs `listener` after each measurement of the main session has been taken in, in the order added. */
export function onMeasure(listener: Listener): void {
  measureListeners.push(listener)
}

const clearListeners: (() => void)[] = []

/** Runs `listener` when the conversation is cleared (a /clear): what a file keeps in memory about the session goes. */
export function onClear(listener: () => void): void {
  clearListeners.push(listener)
}

/**
 * The engine as the other files see it. Claude Code follows `$` only within the
 * file that holds it, so every call on `$` the mod's work needs is spelled out here.
 */
function kit($: EngineInterface): Io {
  return {
    now: () => $.clock.now(),
    after: (ms, run) => $.clock.after(ms, run),
    every: (ms, run) => $.clock.every(ms, run),
    redraw: () => {
      void $.ui.invalidate('ui.render')
    },
    debug: text => {
      $.ui.log(text, { to: 'debug' })
    },
    note: text => {
      $.ui.log(text)
    },
    places: async () => ({
      configDir: await $.env.get('CLAUDE_CONFIG_DIR'),
      userProfile: await $.env.get('USERPROFILE'),
      home: await $.env.get('HOME'),
    }),
    readFile: async path => {
      const text = await $.fs.read(path)
      return typeof text === 'string' ? text : null
    },
    writeFile: async (path, text) => {
      await $.fs.write(path, text)
    },
    getSnap: () => read($, snapAtom),
    putSnap: async snap => {
      await update($, snapAtom, () => snap)
    },
    getSettings: () => read($, settingsAtom),
    putSettings: async settings => {
      await update($, settingsAtom, () => settings)
    },
    getDetailed: () => read($, detailedAtom),
    listNames: async path => {
      const entries = await $.fs.list(path)
      return entries.filter(one => one.kind === 'file').map(one => one.name)
    },
    status: text => {
      $.ui.status(text)
    },
  }
}

async function firstSurface($: EngineInterface): Promise<string | null> {
  try {
    const surfaces = await $.session.surfaces()
    return surfaces[0] ?? null
  } catch {
    return null
  }
}

/**
 * A session that was claimed with no surface known is asked again, at most once per reply. Claude Code starts the
 * sessions the Desktop app, VS Code and the mobile app drive with no surface, and the client attaches a moment later,
 * so the one question at the claim often finds none. Once a surface is named, `present` is announced as it would have
 * been at the start. A headless session, whose surfaces stay empty, is asked once a reply and never announces it.
 */
async function lateSurface($: EngineInterface, snap: GlassSnap): Promise<void> {
  if (snap.surface !== null || snap.surfaceAskedAt === snap.replies) return
  snap.surfaceAskedAt = snap.replies
  const surface = await firstSurface($)
  if (surface === null) return
  snap.surface = surface
  announcePresent(snap, await $.clock.now())
}

/**
 * The snapshot of the session now running. A new session id (a /clear raises
 * no session.start) starts it over and says the mod is present again.
 */
export async function ensure($: EngineInterface): Promise<GlassSnap> {
  const io = kit($)
  const snap = await live(io)
  const id = await $.session.id()
  if (snap.sessionId === id) {
    await lateSurface($, snap)
    return snap
  }
  const surface = snap.surface ?? (await firstSurface($))
  // A snapshot no session has claimed yet (the first event of a session, or after a /clear) is claimed where it stands,
  // so what another hook has put on it since (a message counted) isn't lost to a second start.
  const fresh = snap.sessionId === null ? Object.assign(snap, { sessionId: id, surface }) : await restart(io, { sessionId: id, surface })
  announcePresent(fresh, await $.clock.now())
  await beat(io, fresh, true)
  keepBeating(io)
  return fresh
}

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

/**
 * The context as Claude Code reports it now (what /context shows). Right after a summary `context.tokens` is still the
 * last request's size while the breakdown is the new context, so then the tokens come from the breakdown's used total
 * (what its percentage is of), else from the summary's own `tokensAfter`: the state, the runway and the figures agree.
 * Each reading asks for the summary breakdown (local and free), which estimates the built-in tools well over their count;
 * the startup rows counted once a session (`countStartup`) are put in place of those estimates, so the startup and the
 * conversation parts match what /context counts.
 */
async function refreshReading($: EngineInterface, snap: GlassSnap, summary?: { tokensAfter: number | null }): Promise<void> {
  const usage = await $.session.usage({ breakdown: 'summary' })
  const context = withCounted(usage.context, snap.startupCounted)
  if (summary === undefined) {
    if (typeof context.tokens === 'number') snap.reading = readContext(context)
    return
  }
  const fresh = readContext({ ...context, tokens: undefined })
  if (fresh.tokens > 0) snap.reading = fresh
  else if (summary.tokensAfter !== null && summary.tokensAfter > 0) snap.reading = readContext({ ...context, tokens: summary.tokensAfter })
  else if (typeof context.tokens === 'number') snap.reading = readContext(context)
}

/** The session whose startup is being counted, while the count is on its way (it is not asked for twice). */
let counting: string | null = null

/**
 * Claude Code's own count of what loads before the first message, asked for once a session. A summary breakdown (every
 * reading's) estimates some categories from characters, the built-in tools at about 1.7 times their count: the startup
 * part reads about 12k high and the conversation as much low. A full one counts each tool and file with the token-count
 * request, one for each, as /context does, which takes a moment, so it is asked for once, off to the side of the hook, and
 * after the session's first reply, when its MCP servers have connected. Its startup rows are kept on the snapshot
 * (`startupCounted`) and put in place of the estimates in every summary reading after it (`refreshReading`) and in the
 * Startup pane (pane.tsx). A count that fails leaves an empty list, so it isn't tried again.
 */
function countStartup($: EngineInterface, io: Io, snap: GlassSnap): void {
  const id = snap.sessionId
  if (id === null || snap.startupCounted !== null || counting === id) return
  counting = id
  io.after(0, () => {
    void (async () => {
      let rows: { name: string; tokens: number }[] = []
      try {
        rows = countedRows((await $.session.usage({ breakdown: 'full' })).context)
      } catch (error) {
        $.ui.log(`claudeglass: startup count ${String(error)}`, { to: 'debug' })
      }
      try {
        const now = await live(io)
        // A /clear or a new session in the meantime is another conversation: its first reply asks for its own.
        if (now.sessionId === id) {
          now.startupCounted = rows
          if (rows.length > 0) await refreshReading($, now)
          await touch(io)
        }
      } catch (error) {
        $.ui.log(`claudeglass: startup count ${String(error)}`, { to: 'debug' })
      } finally {
        counting = null
      }
    })()
  })
}

/**
 * Claude Code's cost figure, when it moves, is the reply's cost: during a reply it replaces
 * the estimate from the requests priced so far (later requests add to it); after a reply that
 * ended before any figure arrived it settles that reply's cost.
 */
function settleCost(snap: GlassSnap): void {
  if (snap.cost === null) return
  if (snap.turnStartCost === null) {
    // The first figure since a reply began without one: what the session cost before that reply is not known (a resumed
    // session may start from Claude Code's whole ledger), so the base is what leaves this reply at its estimate. The
    // estimate stands for the reply; the figures that follow settle the replies after it.
    if (snap.turnId !== null) snap.turnStartCost = Math.max(0, snap.cost - snap.turnUsd)
    return
  }
  const spent = snap.cost - snap.turnStartCost
  if (!(spent > 0)) return
  if (snap.turnAt !== null) {
    snap.turnUsd = spent
    snap.turnExact = true
  } else if (!snap.turnExact) {
    // The reply has ended: this is what it cost, and `turnUsd` keeps it until the next reply begins.
    snap.turnExact = true
    snap.turnUsd = spent
  }
}

async function measure($: EngineInterface, snap: GlassSnap, cost: number | undefined, limits: readonly { kind: string; percentUsed: number; resetsAt?: string }[]): Promise<void> {
  await refreshReading($, snap)
  if (typeof cost === 'number') {
    snap.cost = cost
    settleCost(snap)
  }
  const five = limits.find(one => one.kind === 'five_hour')
  if (five !== undefined) {
    const resetsAt = five.resetsAt ?? null
    // A fall in the percent is a new window, whether or not Claude Code names when it resets.
    const dropped = snap.fiveHour !== null && five.percentUsed < snap.fiveHour.pct
    snap.fiveHour = { pct: five.percentUsed, resetsAt }
    if (snap.calBase === null || snap.calBase.resetsAt !== resetsAt || dropped) {
      snap.calBase = { usd: snap.cost ?? 0, pct: five.percentUsed, resetsAt }
    }
  }
  snap.limitHit = limits.some(one => one.percentUsed >= 100)
}

/**
 * A new main reply begins: the cold-send check, then the reply's own figures start over. Its cost is measured from
 * what the session had cost, or from where the last reply's estimate ends when that is more and Claude Code hasn't
 * settled that reply yet (its measurement may arrive after this reply has begun, and must not be counted in this one;
 * a measure made part way through it may hold only part of it). With no figure yet there is no base: the first
 * figure sets it (settleCost), since a session may have been resumed with a cost already behind it; only the request of
 * a conversation's very first message (`fresh`) is known to have nothing before it, so a figure that follows settles it.
 */
function beginReply(snap: GlassSnap, settings: GlassSettings, now: number, turnId: string, fresh: boolean): void {
  stopCacheClock()
  const ctx = snap.reading?.tokens ?? snap.reqs[snap.reqs.length - 1]?.ctx ?? 0
  if (snap.lastTs !== null && ctx >= coldMin(settings) && !cacheState(snap.lastTs, now, settings.ttl).isWarm) {
    addEvent(snap, { ts: seconds(now), event: 'cold_send', outcome: flags.coldWarned ? 'warned' : 'unwarned' })
  }
  flags.coldWarned = false
  drainShown(snap)
  const unsettled = snap.turnId !== null && !snap.turnExact
  const lastEnd = unsettled && snap.turnStartCost !== null ? snap.turnStartCost + snap.turnUsd : null
  snap.turnAt = now
  snap.turnId = turnId
  snap.turnStartCost = snap.cost === null ? (fresh && snap.replies === 0 ? 0 : null) : lastEnd === null ? snap.cost : Math.max(snap.cost, lastEnd)
  snap.turnUsd = 0
  snap.turnExact = false
  snap.turnReqs = 0
}

/** Records one main-session model request. */
async function recordRequest(
  snap: GlassSnap,
  settings: GlassSettings,
  step: { index: number; messageCount: number; model: string },
  usage: TurnUsage,
  uses: readonly { name: string; input: unknown }[],
  now: number,
): Promise<void> {
  const input = count(usage.input_tokens)
  const output = count(usage.output_tokens)
  const cacheRead = count(usage.cache_read_input_tokens)
  const cacheWrite = count(usage.cache_creation_input_tokens)
  const ctx = input + cacheRead + cacheWrite
  const tools = uses.map(use => use.name)
  const prev = snap.reqs[snap.reqs.length - 1] ?? null
  const model = usage.model || step.model
  const first = step.index === 0 && step.messageCount <= 1 && snap.reqs.length === 0 && snap.replies === 0
  const afterCompaction = snap.pendingCompaction
  const afterLimitWait = snap.limitHit && prev !== null && now - prev.ts >= LIMIT_PAUSE_MS
  const growth = prev !== null && step.index > 0 && !afterCompaction ? toolGrowth(prev.ctx, prev.output, ctx) : 0

  const request: GlassRequest = { ts: now, ctx, input, output, cacheRead, cacheWrite, model, tools, afterCompaction, afterLimitWait, first, growth }
  if (first) {
    snap.firstCr = cacheRead
    snap.startupFirst = ctx
  }
  for (const name of tools) {
    if (!snap.toolsUsed.includes(name) && snap.toolsUsed.length < MAX_TOOLS_USED) snap.toolsUsed.push(name)
  }

  const rates = ratesFor(settings.models, model)
  const ttl = settings.ttl
  const rebuild = detectRebuild(prev, request, settings.rebuild, ttl, snap.firstCr)
  if (rebuild !== null) {
    addEvent(snap, { ts: seconds(now), event: 'rebuild', kind: rebuild.cause, outcome: rebuild.expected ? 'expected' : 'unexpected' })
  }

  if (growth > 0 && prev !== null) snap.toolKept += growth

  snap.turnUsd += priceUsage(rates, usage, ttl)
  snap.turnExact = false

  snap.reqs.push(request)
  if (snap.reqs.length > MAX_REQUESTS) snap.reqs.splice(0, snap.reqs.length - MAX_REQUESTS)
  snap.model = model
  snap.lastTs = now
  snap.turnReqs += 1
  snap.pendingCompaction = false
}

/** Records a conversation summary of the main session. */
function recordCompaction(snap: GlassSnap, trigger: string, before: number | null, after: number | null, now: number): void {
  const compaction: GlassCompaction = { ts: now, trigger, tokensBefore: before, tokensAfter: after }
  snap.compactions.push(compaction)
  if (snap.compactions.length > MAX_COMPACTIONS) snap.compactions.splice(0, snap.compactions.length - MAX_COMPACTIONS)
  snap.pendingCompaction = true
  snap.toolKept = 0
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const io = kit($)
    try {
      forget()
      counting = null
      resetCoach()
      await reloadSettings(io)
      const detailed = await $.store.get('detailed')
      await update($, detailedAtom, () => detailed === true)
      const snap = await ensure($)
      if (snap.surface === null && isSurface(e.surface)) {
        snap.surface = e.surface
        announcePresent(snap, await $.clock.now())
      }
      await beat(io, snap, true)
      keepBeating(io)
      await touch(io)
      await $.command.register({ name: 'glass', description: COPY['checkpoint.command'], argumentHint: '[startup | detail | pin <text> | unpin <words> | tighten | guard]', immediate: true })
    } catch (error) {
      $.ui.log(`claudeglass: session.start ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  })

  // A message sent makes sure of the session, so its heartbeat is there for the first message after a /clear:
  // the capture hook looks for it when that message is sent. (A matcher, apart from the other files' hooks on the event.)
  on('prompt.submit', { text: /[\s\S]*/ }, async ($, e, next) => {
    try {
      await ensure($)
    } catch (error) {
      $.ui.log(`claudeglass: prompt.submit ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('session.end', async ($, e, next) => {
    const io = kit($)
    try {
      const snap = await live(io)
      if (snap.sessionId === e.sessionId) {
        endCoach(snap)
        settleAtEnd(snap, e.reason)
        drainSensor(snap)
        await writeSensor(io, snap)
        snap.turnAt = null
      }
      await leave(io, e.sessionId)
      stopCacheClock()
      if (e.reason === 'clear') {
        release('')
        // The coaching's memory and the agents' prompts are the cleared conversation's.
        resetCoach()
        for (const forget of clearListeners) forget()
        await restart(io, { surface: snap.surface })
      } else await flushNow(io)
    } catch (error) {
      $.ui.log(`claudeglass: session.end ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const io = kit($)
    try {
      const snap = await ensure($)
      await measure($, snap, e.cost?.usd, e.rateLimits)
      const settings = await getSettings(io)
      await beat(io, snap)
      const now = await $.clock.now()
      for (const listener of measureListeners) {
        try {
          await listener({ io, snap, settings, now })
        } catch (error) {
          $.ui.log(`claudeglass: measure ${String(error)}`, { to: 'debug' })
        }
      }
      await touch(io)
    } catch (error) {
      $.ui.log(`claudeglass: session.measure ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  })

  // turn.start carries no agent id, so it can't say whose turn begins: the reply begins at its first request (turn.step),
  // which does. Here the session is only made sure of (a new one starts its snapshot, and its heartbeat, at once).
  on('turn.start', async ($, e, next) => {
    try {
      await ensure($)
    } catch (error) {
      $.ui.log(`claudeglass: turn.start ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    // A request of the main session whose turn is not the reply being worked on begins the next reply. A subagent's
    // requests carry its agent id, and a subagent's turn never begins one, however long the reply has run.
    if (e.agentId === undefined) {
      try {
        const io = kit($)
        const snap = await ensure($)
        if (snap.turnId !== e.turnId) {
          beginReply(snap, await getSettings(io), await $.clock.now(), e.turnId, e.index === 0 && e.messageCount <= 1)
          await touch(io)
        }
      } catch (error) {
        $.ui.log(`claudeglass: turn begin ${String(error)}`, { to: 'debug' })
      }
    }
    const result = yield* next(e)
    // What the step said and the tools it called, for the end of the reply (a subagent's steps are never held).
    if (e.agentId === undefined) {
      try {
        if (typeof result.answer === 'string') hold(e.turnId, result.answer)
        for (const use of result.toolUses) hold(e.turnId, leaves(use.input).join('\n'))
      } catch (error) {
        $.ui.log(`claudeglass: turn.step texts ${String(error)}`, { to: 'debug' })
      }
    }
    if (e.agentId === undefined && result.usage !== null) {
      const io = kit($)
      try {
        const snap = await ensure($)
        const settings = await getSettings(io)
        const now = await $.clock.now()
        await recordRequest(snap, settings, e, result.usage, result.toolUses, now)
        await touch(io)
      } catch (error) {
        $.ui.log(`claudeglass: turn.step ${String(error)}`, { to: 'debug' })
      }
    }
    return result
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
    const io = kit($)
    const texts = release(e.turnId)
    try {
      const snap = await ensure($)
      const settings = await getSettings(io)
      const now = await $.clock.now()
      const last = snap.reqs[snap.reqs.length - 1]
      // Only a turn that began a reply (its first request did, in turn.step) ends one: Esc before the first request, or a hook
      // that answers without going on, completes a turn that made none, and the last reply's requests are not another reply.
      const began = snap.turnAt !== null && snap.turnId === e.turnId
      snap.turnAt = null
      if (!began) snap.turnReqs = 0
      if (began && snap.turnReqs > 0 && last !== undefined) {
        snap.replies += 1
        snap.history.push(last.ctx)
        if (snap.history.length > MAX_REPLIES) snap.history.splice(0, snap.history.length - MAX_REPLIES)
        // A reply the rates could not price (a model with no rate) is what Claude Code's cost moved by.
        if (!(snap.turnUsd > 0) && snap.cost !== null && snap.turnStartCost !== null) snap.turnUsd = Math.max(0, snap.cost - snap.turnStartCost)
        settleAfterReply(snap, last.ctx)
        for (const listener of listeners) {
          try {
            await listener({ io, snap, settings, now, answer: e.answer, reason: e.reason, texts })
          } catch (error) {
            $.ui.log(`claudeglass: turn end ${String(error)}`, { to: 'debug' })
          }
        }
        await writeSensor(io, snap)
        await armCacheClock(io, snap, settings.ttl)
        countStartup($, io, snap)
      }
      await touch(io)
    } catch (error) {
      $.ui.log(`claudeglass: turn.complete ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  })

  on('session.compact', async ($, e, next) => {
    const io = kit($)
    const result = await next(e)
    // A summary made ahead (precompute) installs nothing, so it is not recorded.
    if (e.agentId === undefined && result.skip === undefined && e.trigger !== 'precompute') {
      try {
        const snap = await ensure($)
        const settings = await getSettings(io)
        const now = await $.clock.now()
        recordCompaction(snap, e.trigger, result.tokensBefore ?? null, result.tokensAfter ?? null, now)
        // The band and the pane match /context straight away, not at the next measurement.
        try {
          await refreshReading($, snap, { tokensAfter: result.tokensAfter ?? null })
        } catch (error) {
          $.ui.log(`claudeglass: session.compact reading ${String(error)}`, { to: 'debug' })
        }
        for (const listener of compactListeners) {
          try {
            await listener({ io, snap, settings, now })
          } catch (error) {
            $.ui.log(`claudeglass: compaction ${String(error)}`, { to: 'debug' })
          }
        }
        await touch(io)
      } catch (error) {
        $.ui.log(`claudeglass: session.compact ${String(error)}`, { to: 'debug' })
      }
    }
    return result
  }).catch(($, e, next) => next(e))
}
