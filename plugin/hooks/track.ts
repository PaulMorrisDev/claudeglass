import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, TurnUsage } from 'claude-code'

import type { GlassCompaction, GlassRequest, GlassSettings, GlassSnap } from '../types'
import { armCacheClock, stopCacheClock } from './cacheclock'
import { COPY } from './generated/copy.js'
import { beat } from './heartbeat'
import type { Io } from './io'
import { emptySnap, emptyTurn, flags, flushNow, forget, live, restart, touch } from './live'
import { cacheState } from './lib/cache.js'
import { readContext, toolGrowth } from './lib/context.js'
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
const pageAtom = atom({ plugin: 'claudeglass', key: 'page' } as const, 'now')

// Follows the main session: Claude Code's own figures at each measurement, one
// record per model request, one per reply, and each conversation summary. All
// of it lands in the snapshot (live.ts); nothing here draws.

const MAX_REQUESTS = 200
const MAX_REPLIES = 100
const MAX_REPLY_COSTS = 40
const MAX_COMPACTIONS = 20
const MAX_HEAVIEST = 5
/** A wait this long after a request that hit the usage limit counts as the limit's pause. */
const LIMIT_PAUSE_MS = 60 * 1000
/** A tool result this big is worth a note, for the tools whose output Claude isn't already coached on. */
export const BIG_RESULT_TOKENS = 8000
const NOTE_TOOLS = ['Bash', 'Grep', 'Glob']

/** Which tool a note about a big result names, or null when the tool isn't one it covers. */
export function noteTool(name: string): boolean {
  return NOTE_TOOLS.includes(name) || name.startsWith('mcp__')
}

export type TurnEnd = {
  io: Io
  snap: GlassSnap
  settings: GlassSettings
  now: number
}

type Listener = (end: TurnEnd) => Promise<void>

const listeners: Listener[] = []

/** Runs `listener` at the end of each main-session reply, after the bookkeeping, in the order added. */
export function onTurnEnd(listener: Listener): void {
  listeners.push(listener)
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
 * The snapshot of the session now running. A new session id (a /clear raises
 * no session.start) starts it over and says the mod is present again.
 */
export async function ensure($: EngineInterface): Promise<GlassSnap> {
  const io = kit($)
  const snap = await live(io)
  const id = await $.session.id()
  if (snap.sessionId === id) return snap
  const surface = snap.surface ?? (await firstSurface($))
  const fresh = await restart(io, { sessionId: id, surface })
  announcePresent(fresh, await $.clock.now())
  await beat(io, fresh, true)
  return fresh
}

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function pctPerUsd(snap: GlassSnap): number | null {
  const base = snap.calBase
  if (base === null || snap.fiveHour === null || snap.cost === null) return null
  const usd = snap.cost - base.usd
  const pct = snap.fiveHour.pct - base.pct
  return usd >= 0.1 && pct >= 1 ? pct / usd : null
}

/** Percent of the 5-hour limit that `usd` of this session's spend has been worth in this window, or null when not known yet. */
export function limitShare(snap: GlassSnap, usd: number): number | null {
  const rate = pctPerUsd(snap)
  return rate === null ? null : usd * rate
}

async function measure($: EngineInterface, snap: GlassSnap, cost: number | undefined, limits: readonly { kind: string; percentUsed: number; resetsAt?: string }[]): Promise<void> {
  const usage = await $.session.usage({ breakdown: 'summary' })
  if (typeof usage.context.tokens === 'number') snap.reading = readContext(usage.context)
  if (typeof cost === 'number') snap.cost = cost
  const five = limits.find(one => one.kind === 'five_hour')
  if (five !== undefined) {
    const resetsAt = five.resetsAt ?? null
    snap.fiveHour = { pct: five.percentUsed, resetsAt }
    if (snap.calBase === null || snap.calBase.resetsAt !== resetsAt) {
      snap.calBase = { usd: snap.cost ?? 0, pct: five.percentUsed, resetsAt }
    }
  }
  snap.limitHit = limits.some(one => one.percentUsed >= 100)
}

function toolLabel(name: string): string {
  return name
}

/** Records one main-session model request. */
async function recordRequest(
  snap: GlassSnap,
  settings: GlassSettings,
  step: { index: number; messageCount: number; model: string },
  usage: TurnUsage,
  tools: string[],
  now: number,
): Promise<void> {
  const input = count(usage.input_tokens)
  const output = count(usage.output_tokens)
  const cacheRead = count(usage.cache_read_input_tokens)
  const cacheWrite = count(usage.cache_creation_input_tokens)
  const ctx = input + cacheRead + cacheWrite
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

  const rates = ratesFor(settings.models, model)
  const ttl = settings.ttl
  const rebuild = detectRebuild(prev, request, settings.rebuild, ttl, snap.firstCr)
  if (rebuild !== null) {
    const extra = priceUsage(rates, { cache_creation_input_tokens: rebuild.tokens }, ttl) - priceUsage(rates, { cache_read_input_tokens: rebuild.tokens }, ttl)
    snap.turn.rebuilds.push({ cause: rebuild.cause, tokens: rebuild.tokens, usd: Math.max(0, extra), expected: rebuild.expected })
    addEvent(snap, { ts: seconds(now), event: 'rebuild', kind: rebuild.cause, outcome: rebuild.expected ? 'expected' : 'unexpected' })
  }

  if (prev !== null && prev.model !== '' && model !== '' && prev.model !== model) {
    snap.switches += 1
    snap.turn.switched = true
    if (rates !== null) {
      const write = ttl >= 3600 ? rates.cache_write_1h : rates.cache_write_5m
      snap.turn.switchUsd = (ctx / 1000000) * Math.max(0, write - rates.cache_read)
    }
  }

  if (growth > 0 && prev !== null) {
    snap.toolKept += growth
    const noted = prev.tools.find(noteTool)
    const name = toolLabel(noted ?? prev.tools[0] ?? '')
    if (name !== '') {
      snap.heaviest.push({ name, tokens: growth })
      snap.heaviest.sort((a, b) => b.tokens - a.tokens)
      snap.heaviest.splice(MAX_HEAVIEST)
    }
    if (noted !== undefined && growth >= BIG_RESULT_TOKENS && growth > (snap.turn.big?.tokens ?? 0)) {
      snap.turn.big = { name: noted, tokens: growth }
    }
  }

  snap.mix.read += priceUsage(rates, { cache_read_input_tokens: cacheRead }, ttl)
  snap.mix.write += priceUsage(rates, { cache_creation_input_tokens: cacheWrite }, ttl)
  snap.mix.input += priceUsage(rates, { input_tokens: input }, ttl)
  snap.mix.output += priceUsage(rates, { output_tokens: output }, ttl)

  snap.reqs.push(request)
  if (snap.reqs.length > MAX_REQUESTS) snap.reqs.splice(0, snap.reqs.length - MAX_REQUESTS)
  snap.model = model
  snap.lastTs = now
  snap.turnReqs += 1
  snap.pendingCompaction = false
}

/** Records a conversation summary of the main session. */
function recordCompaction(snap: GlassSnap, settings: GlassSettings, trigger: string, before: number | null, after: number | null, now: number): void {
  const idle = snap.lastTs === null ? false : !cacheState(snap.lastTs, now, settings.ttl).isWarm
  const size = before ?? snap.reading?.tokens ?? 0
  const compaction: GlassCompaction = { ts: now, trigger, tokensBefore: before, tokensAfter: after, wasCold: idle && size >= coldMin(settings) }
  snap.compactions.push(compaction)
  if (snap.compactions.length > MAX_COMPACTIONS) snap.compactions.splice(0, snap.compactions.length - MAX_COMPACTIONS)
  snap.pendingCompaction = true
  snap.toolKept = 0
  snap.heaviest = []
  snap.turn.compacted = true
  if (compaction.wasCold) snap.turn.compactedCold = compaction
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const io = kit($)
    try {
      forget()
      await reloadSettings(io)
      const detailed = await $.store.get('detailed')
      await update($, detailedAtom, () => detailed === true)
      const page = await $.store.get('page')
      if (typeof page === 'string') await update($, pageAtom, () => page)
      const snap = await ensure($)
      if (snap.surface === null && isSurface(e.surface)) {
        snap.surface = e.surface
        announcePresent(snap, await $.clock.now())
      }
      await beat(io, snap, true)
      await touch(io)
      await $.command.register({ name: 'glass', description: COPY['pane.command'], argumentHint: '[detail]', immediate: true })
    } catch (error) {
      $.ui.log(`claudeglass: session.start ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    const io = kit($)
    try {
      const snap = await live(io)
      if (snap.sessionId === e.sessionId) {
        settleAtEnd(snap, e.reason)
        await writeSensor(io, snap)
      }
      stopCacheClock()
      if (e.reason === 'clear') await restart(io, { surface: snap.surface })
      else await flushNow(io)
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
      await getSettings(io)
      await beat(io, snap)
      await touch(io)
    } catch (error) {
      $.ui.log(`claudeglass: session.measure ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    const io = kit($)
    try {
      const snap = await ensure($)
      const settings = await getSettings(io)
      const now = await $.clock.now()
      stopCacheClock()
      const ctx = snap.reading?.tokens ?? snap.reqs[snap.reqs.length - 1]?.ctx ?? 0
      if (snap.lastTs !== null && ctx >= coldMin(settings) && !cacheState(snap.lastTs, now, settings.ttl).isWarm) {
        addEvent(snap, { ts: seconds(now), event: 'cold_send', outcome: flags.coldWarned ? 'warned' : 'unwarned' })
      }
      flags.coldWarned = false
      snap.turnStartCost = snap.cost
      // A summary made between replies (a /compact) is still waiting for its note.
      snap.turn = { ...emptyTurn(), compacted: snap.turn.compacted, compactedCold: snap.turn.compactedCold }
      snap.turnReqs = 0
      await touch(io)
    } catch (error) {
      $.ui.log(`claudeglass: turn.start ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    const result = yield* next(e)
    if (e.agentId === undefined && result.usage !== null) {
      const io = kit($)
      try {
        const snap = await ensure($)
        const settings = await getSettings(io)
        const now = await $.clock.now()
        await recordRequest(snap, settings, e, result.usage, result.toolUses.map(use => use.name), now)
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
    try {
      const snap = await ensure($)
      const settings = await getSettings(io)
      const now = await $.clock.now()
      const last = snap.reqs[snap.reqs.length - 1]
      if (snap.turnReqs > 0 && last !== undefined) {
        snap.replies += 1
        snap.history.push(last.ctx)
        if (snap.history.length > MAX_REPLIES) snap.history.splice(0, snap.history.length - MAX_REPLIES)
        const spent = snap.cost !== null && snap.turnStartCost !== null ? Math.max(0, snap.cost - snap.turnStartCost) : null
        snap.lastReplyUsd = spent
        snap.replyCosts.push({ usd: spent ?? 0, ctx: last.ctx })
        if (snap.replyCosts.length > MAX_REPLY_COSTS) snap.replyCosts.splice(0, snap.replyCosts.length - MAX_REPLY_COSTS)
        settleAfterReply(snap, last.ctx)
        for (const listener of listeners) {
          try {
            await listener({ io, snap, settings, now })
          } catch (error) {
            $.ui.log(`claudeglass: turn end ${String(error)}`, { to: 'debug' })
          }
        }
        snap.turn = emptyTurn()
        await writeSensor(io, snap)
        await armCacheClock(io, snap, settings.ttl)
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
    if (e.agentId === undefined && result.skip === undefined && (e.trigger === 'manual' || e.trigger === 'auto')) {
      try {
        const snap = await ensure($)
        const settings = await getSettings(io)
        recordCompaction(snap, settings, e.trigger, result.tokensBefore ?? null, result.tokensAfter ?? null, await $.clock.now())
        await touch(io)
      } catch (error) {
        $.ui.log(`claudeglass: session.compact ${String(error)}`, { to: 'debug' })
      }
    }
    return result
  }).catch(($, e, next) => next(e))
}
