import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { GlassAgent, GlassSettings, GlassSnap } from '../types'
import { COPY } from './generated/copy.js'
import { HOW, SKIP_AGENT_TYPES, SPLIT_WHY, THRESHOLDS, TIPS } from './generated/coaching.js'
import type { Io } from './io'
import { activityOf, addSplit, newSplit, reportReread, splitCounts, splitRun } from './lib/agents.js'
import type { Decision, Split } from './lib/agents.js'
import { priceUsage, ratesFor } from './lib/pricing.js'
import { mayShow, noteAllowed, recordShown } from './lib/rest.js'
import type { RestThresholds } from './lib/rest.js'
import { fill } from './lib/text.js'
import { emptySnap, live, touch } from './live'
import { startedInBackground } from './coach-mem'
import { NOTE_EVERY, noteShown, seconds } from './sensor'
import { coachingApplies, getSettings } from './settings'
import { onClear, onTurnEnd } from './track'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)

// Follows the agents of the session. Each run is a row in the snapshot
// (snap.agents), kept up to date from the events it raises:
//
//   agent.spawn      what started it (type, description, model, background, fork);
//                    its answer carries the agentId every later event of that
//                    run carries, which is how a spawn is linked to its events
//   tool.call        the tools it uses and what it is doing now (an event with an
//                    agentId is the agent's; without one, the main session's)
//   turn.step        its requests: tokens, cost, and what each tool result added
//   turn.complete    its end: the run's own answer carries the agentId. The
//                    reason says whether it finished or stopped on an error
//   tool.call (main) the Agent call resolving: a foreground run's totals, and
//                    the agentId a background launch is linked to its row by
//   agent.list       the engine's own status, asked every few seconds while some
//                    agent is running, for a run that ended unseen
//
// Only a run that has a spawn (or an Agent call naming it) is a row. The engine's
// own forks (a summary being written, memory work) and a skill's fork carry an
// agentId too, with no spawn: their events are held aside for a few seconds in case a
// spawn claims them, and dropped when none does, so they are never counted as agents.
//
// The hooks that watch an agent's events are matched on agentId (`/./` only
// matches an event that has one), apart from track.ts's hooks on the same
// events, which take the main session's: validate refuses two unmatched hooks
// on one event.
//
// The writes go through live.ts's touch(), which puts the snapshot in $.state
// at most four times a second whatever the number of agents.

const MAX_ROWS = 40

/** How often the engine's list of agents is asked while one is running. */
const POLL_MS = 10000

/**
 * A workflow's agent the engine's list never shows is finished at its own turn.complete or when its Workflow call
 * returns; one that has raised no event for this long is finished too, so that a run whose end was never seen doesn't
 * keep the band and the polling going.
 */
const WORKFLOW_IDLE_MS = 30 * 60 * 1000

/** How long the events of an agentId no spawn has named are held before they are dropped. */
const UNCLAIMED_MS = 5000

/** Rows of agentIds that have raised events but no spawn (yet); not in the snapshot. */
const unclaimed = new Map<string, GlassAgent>()
let pendingSplit: Split = newSplit()
let polling: { cancel: () => void } | null = null

function stopPolling(): void {
  polling?.cancel()
  polling = null
}

/** Forgets the runs waiting to be told (a new session, or a test). */
export function forgetAgents(): void {
  stopPolling()
  unclaimed.clear()
  pendingSplit = newSplit()
}

/** The engine, as the file's hooks need it: every call on `$` spelled out here. */
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

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function blank(id: string, now: number): GlassAgent {
  return {
    id,
    toolUseId: '',
    type: '',
    desc: '',
    model: '',
    background: false,
    fork: false,
    status: 'running',
    startedAt: now,
    endedAt: null,
    requests: 0,
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    usd: 0,
    calls: 0,
    tools: {},
    activity: 'working',
    briefChars: 0,
    compactions: 0,
    notes: [],
  }
}

/** Drops the held rows of agentIds that no spawn claimed in time. */
function sweep(now: number): void {
  for (const [id, row] of unclaimed) if (now - row.startedAt > UNCLAIMED_MS) unclaimed.delete(id)
}

/**
 * The row of agent `id` for one of its events: the snapshot's if a spawn made it, else one held aside
 * (see the header); a spawn that answers late claims it with the figures so far.
 */
function rowOf(snap: GlassSnap, id: string, now: number): GlassAgent {
  const known = snap.agents.find(one => one.id === id)
  if (known !== undefined) return known
  sweep(now)
  let row = unclaimed.get(id)
  if (row === undefined) {
    row = blank(id, now)
    unclaimed.set(id, row)
  }
  return row
}

/** Whether an agent's row is one of the snapshot's: it has a spawn, or an Agent call, behind it. */
function isRow(snap: GlassSnap, row: GlassAgent): boolean {
  return snap.agents.includes(row)
}

/** The row of agent `id` in the snapshot, made now (from what was held aside, if anything) for a spawn or an Agent call that names it. */
function claim(snap: GlassSnap, id: string, now: number): GlassAgent {
  let row = snap.agents.find(one => one.id === id)
  if (row === undefined) {
    row = unclaimed.get(id) ?? blank(id, now)
    unclaimed.delete(id)
    snap.agents.push(row)
    if (snap.agents.length > MAX_ROWS) {
      const old = snap.agents.findIndex(one => one.status !== 'running')
      if (old >= 0) {
        snap.agents.splice(old, 1)
      }
    }
  }
  return row
}

/** The thresholds the rest rules and the two agent notes read. */
type Thresholds = RestThresholds & Record<string, number> & { report_reread_tokens: number; split_brief_chars: number }

function thresholds(settings: GlassSettings): Thresholds {
  return { ...THRESHOLDS, ...settings.thresholds } as unknown as Thresholds
}

/** The tip's own words, with the how-to words in their Desktop variant. */
function tipText(decision: Decision): string {
  const how: Record<string, string> = {}
  for (const [key, variants] of Object.entries(HOW)) how[key] = variants.desktop
  const template = TIPS[decision.kind as keyof typeof TIPS] ?? ''
  return fill(template, { ...how, ...decision.fields })
}

/**
 * Tells `decision` if the rest rules allow it: records that it showed (and the
 * sensor's note event) and returns the words, or returns null.
 */
function tell(snap: GlassSnap, settings: GlassSettings, decision: Decision | null, surface: string, now: number): string | null {
  if (decision === null) return null
  // Coaching is for those who turned it on, in the projects it covers.
  if (!coachingApplies(settings)) return null
  if (settings.muted.includes(decision.kind)) return null
  const th = thresholds(settings)
  const nowS = seconds(now)
  if (!mayShow(snap.rest, decision.kind, decision.stake, nowS, th)) return null
  snap.rest = recordShown(snap.rest, decision.kind, decision.stake, nowS, th, settings.once.includes(decision.kind))
  noteShown(snap, decision.kind, surface, now, snap.reading?.tokens ?? snap.reqs[snap.reqs.length - 1]?.ctx ?? 0)
  return tipText(decision)
}

/** The context the main session holds now. */
function mainContext(snap: GlassSnap): number | null {
  const last = snap.reqs[snap.reqs.length - 1]
  return last?.ctx ?? snap.reading?.tokens ?? null
}

/** An agent's run ended: its status, its time, and what it adds to the runs waiting to be told. */
function finish(snap: GlassSnap, settings: GlassSettings, row: GlassAgent, status: 'done' | 'failed', now: number): void {
  if (row.status !== 'running') return
  row.status = status
  row.endedAt = now
  if (settings.muted.includes('split_run')) return
  const one = splitCounts({ type: row.type, compacted: row.compactions > 0, briefChars: row.briefChars, th: thresholds(settings), skip: SKIP_AGENT_TYPES })
  pendingSplit = addSplit(pendingSplit, one)
  // A run the main session waited for has its row under its Agent call: tell it there. A run with no call
  // of the main session's (a workflow's, or one an agent started) has no row on screen: its turn-end note.
  if (!row.background && row.toolUseId !== '' && row.parentAgentId === undefined) {
    const found = splitRun({ compaction: pendingSplit.compaction, brief: pendingSplit.brief, th: thresholds(settings), why: SPLIT_WHY })
    const text = tell(snap, settings, found, 'agent', now)
    if (text !== null) {
      row.notes.push({ kind: 'split_run', text })
      pendingSplit = newSplit()
    }
  }
}

function debug($: EngineInterface, where: string, error: unknown): void {
  $.ui.log(`claudeglass: agents ${where} ${String(error)}`, { to: 'debug' })
}

type CallRecord = {
  agentId?: string
  status?: string
  resolvedModel?: string
  description?: string
  prompt?: string
  totalToolUseCount?: number
}

function isAgentTool(name: string): boolean {
  return name === 'Agent' || name === 'Task'
}

/**
 * A Workflow the main session started in the background while it holds a lot: the report_reread note, as a note at
 * the next free place (a workflow has no Agent call row to carry it). Returns true when it was told. Under the same
 * rules as the other notes: coaching on, not muted, at most one per NOTE_EVERY replies, the rest rule.
 */
export async function workflowLaunched(io: Io, snap: GlassSnap, settings: GlassSettings, background: boolean, now: number): Promise<boolean> {
  if (!background || !noteAllowed(snap.lastNoteReply, snap.replies, NOTE_EVERY)) return false
  const text = tell(snap, settings, reportReread({ background, ctx: mainContext(snap), th: thresholds(settings) }), 'log', now)
  if (text === null) return false
  snap.lastNoteReply = snap.replies
  io.note(`● ${COPY['note.lead']} · ${text}`)
  return true
}

/** Marks the runs the engine's own list says have ended, though their end wasn't seen; stops asking once none is running. */
async function reconcile($: EngineInterface): Promise<void> {
  const io = kit($)
  const snap = await live(io)
  if (!snap.agents.some(one => one.status === 'running')) {
    stopPolling()
    return
  }
  const list = await $.agent.list()
  const settings = await getSettings(io)
  const now = await $.clock.now()
  sweep(now)
  for (const row of snap.agents) {
    if (row.status !== 'running') continue
    const info = list.find(one => one.id === row.id)
    // The list leaves a workflow's agents out: not being in it says nothing of those. Their own turn.complete or
    // their Workflow call returning ends them, and so does a long silence.
    if (row.workflow !== undefined) {
      if (info === undefined && now - (row.seenAt ?? row.startedAt) >= WORKFLOW_IDLE_MS) finish(snap, settings, row, 'done', now)
      else if (info !== undefined && (info.status === 'completed' || info.status === 'idle')) finish(snap, settings, row, 'done', now)
      continue
    }
    // A run the engine no longer lists has ended (the grace is for the list to catch up with a run just started).
    if (info === undefined) {
      if (now - row.startedAt >= POLL_MS) finish(snap, settings, row, 'done', now)
      continue
    }
    if (info.status === 'completed' || info.status === 'idle') finish(snap, settings, row, 'done', now)
    else if (info.status === 'failed' || info.status === 'killed') finish(snap, settings, row, 'failed', now)
  }
  await touch(io)
  if (!snap.agents.some(one => one.status === 'running')) stopPolling()
}

/** Asks the engine's list every few seconds while an agent runs. */
function startPolling($: EngineInterface): void {
  if (polling !== null) return
  polling = $.clock.every(POLL_MS, () => {
    reconcile($).catch(() => {
      // The list is a second opinion; the events are the first.
    })
  })
}

export const register: Register = on => {
  // A /clear: the runs waiting to be told and the held events are the cleared conversation's.
  onClear(forgetAgents)

  // The spawn: what started the run, and the agentId its events will carry.
  on('agent.spawn', async ($, e, next) => {
    const result = await next(e)
    if (result.deny !== undefined || typeof result.agentId !== 'string') return result
    try {
      const io = kit($)
      const snap = await live(io)
      const settings = await getSettings(io)
      const now = await $.clock.now()
      // A session with no agents yet holds nothing from an earlier one.
      if (snap.agents.length === 0) forgetAgents()
      const row = claim(snap, result.agentId, now)
      row.parentAgentId = e.parentAgentId
      row.toolUseId = e.workflow === undefined ? e.tool_use_id : ''
      row.type = e.subagentType
      row.desc = e.description
      row.model = result.model || row.model
      row.background = e.background
      row.fork = e.fork
      row.briefChars = e.prompt.length
      if (e.workflow !== undefined) row.workflow = { runId: e.workflow.runId, call: e.tool_use_id }
      row.seenAt = now
      // Told on the row of the launch: an agent the main session started in the background, while it holds a lot.
      if (e.workflow === undefined && e.parentAgentId === undefined && e.isTeammate === undefined) {
        const text = tell(snap, settings, reportReread({ background: e.background, ctx: mainContext(snap), th: thresholds(settings) }), 'agent', now)
        if (text !== null) row.notes.push({ kind: 'report_reread', text })
      }
      await touch(io)
      startPolling($)
    } catch (error) {
      debug($, 'agent.spawn', error)
    }
    return result
  }).catch(($, e, next) => next(e))

  // A tool call inside an agent is the agent's. (Matched on agentId so this stays apart from the other files' hooks on the same events.)
  on('tool.call', { agentId: /./ }, async ($, e, next) => {
    if (e.agentId === undefined) return next(e)
    try {
      const io = kit($)
      const snap = await live(io)
      const now = await $.clock.now()
      const row = rowOf(snap, e.agentId, now)
      row.seenAt = now
      const name = String(e.tool)
      row.calls += 1
      row.tools[name] = (row.tools[name] ?? 0) + 1
      row.activity = activityOf(name)
      if (row.status !== 'running') {
        row.status = 'running'
        row.endedAt = null
      }
      await touch(io)
    } catch (error) {
      debug($, 'tool.call', error)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // The main session's Agent call resolving: a foreground run's totals, and the agentId a background launch is linked by.
  // A Workflow call returning in the foreground is its run's end: the agents of the run that are still going are finished.
  on('tool.call', { tool: /^(Agent|Task|Workflow)$/ }, async ($, e, next) => {
    const name = String(e.tool)
    if (e.agentId !== undefined || (!isAgentTool(name) && name !== 'Workflow')) return next(e)
    const result = await next(e)
    try {
      const io = kit($)
      const snap = await live(io)
      const settings = await getSettings(io)
      const now = await $.clock.now()
      if (name === 'Workflow') {
        if (result.deny === undefined && !startedInBackground(e as unknown as Record<string, unknown>, result.result, result.text)) {
          for (const row of snap.agents) if (row.workflow !== undefined && row.workflow.call === e.tool_use_id) finish(snap, settings, row, 'done', now)
          await touch(io)
        }
        return result
      }
      const record: CallRecord = result.deny === undefined && result.result !== null && typeof result.result === 'object' ? (result.result as CallRecord) : {}
      const id = record.agentId
      if (typeof id === 'string' && id !== '') {
        const row = claim(snap, id, now)
        if (row.toolUseId === '') row.toolUseId = e.tool_use_id
        const input = e as unknown as { subagent_type?: string; description?: string; prompt?: string }
        if (row.type === '') row.type = input.subagent_type ?? record.description ?? ''
        if (row.desc === '') row.desc = input.description ?? record.description ?? ''
        if (row.model === '' && typeof record.resolvedModel === 'string') row.model = record.resolvedModel
        if (typeof input.prompt === 'string') row.briefChars = Math.max(row.briefChars, input.prompt.length)
        if (record.status === 'async_launched') row.background = true
        if (record.status === 'completed') {
          // The call's `usage` is the run's last request alone, not its total: the row's cost stays the sum of the
          // requests seen (turn.step), and is left at what was seen for a run whose requests were missed.
          row.calls = Math.max(row.calls, count(record.totalToolUseCount))
          finish(snap, settings, row, 'done', now)
        }
        await touch(io)
      }
    } catch (error) {
      debug($, 'tool.call Agent', error)
    }
    return result
  }).catch(($, e, next) => next(e))

  // A request of an agent: tokens, cost, and what the tool results before it added to its context.
  on('turn.step', { agentId: /./ }, async function* ($, e, next) {
    const result = yield* next(e)
    if (e.agentId !== undefined && result.usage !== null) {
      try {
        const io = kit($)
        const snap = await live(io)
        const settings = await getSettings(io)
        const now = await $.clock.now()
        const row = rowOf(snap, e.agentId, now)
        row.seenAt = now
        const usage = result.usage
        const input = count(usage.input_tokens)
        const output = count(usage.output_tokens)
        const cacheRead = count(usage.cache_read_input_tokens)
        const cacheWrite = count(usage.cache_creation_input_tokens)
        const model = usage.model || e.model
        const tools = result.toolUses.map(use => use.name)
        row.requests += 1
        row.input += input
        row.output += output
        row.cacheRead += cacheRead
        row.cacheWrite += cacheWrite
        row.usd += priceUsage(ratesFor(settings.models, model), { input_tokens: input, output_tokens: output, cache_read_input_tokens: cacheRead, cache_creation_input_tokens: cacheWrite }, settings.ttl)
        if (model !== '') row.model = model
        if (tools.length > 0) row.activity = activityOf(tools[tools.length - 1] ?? null)
        if (row.status !== 'running') {
          row.status = 'running'
          row.endedAt = null
        }
        await touch(io)
      } catch (error) {
        debug($, 'turn.step', error)
      }
    }
    return result
  })

  // The run's end: its own turn.complete carries the agentId.
  on('turn.complete', { agentId: /./ }, async ($, e, next) => {
    if (e.agentId === undefined) return next(e)
    try {
      const io = kit($)
      const snap = await live(io)
      const settings = await getSettings(io)
      const now = await $.clock.now()
      const row = rowOf(snap, e.agentId, now)
      // A fork of the engine's that no spawn named ends here with nothing to show.
      if (isRow(snap, row)) finish(snap, settings, row, e.reason === 'answer' ? 'done' : 'failed', now)
      else unclaimed.delete(e.agentId)
      await touch(io)
    } catch (error) {
      debug($, 'turn.complete', error)
    }
    return next(e)
  })

  // A run that had to summarise its own context part-way through counts for split_run.
  on('session.compact', { agentId: /./ }, async ($, e, next) => {
    const result = await next(e)
    if (e.agentId !== undefined && result.skip === undefined) {
      try {
        const io = kit($)
        const snap = await live(io)
        rowOf(snap, e.agentId, await $.clock.now()).compactions += 1
        await touch(io)
      } catch (error) {
        debug($, 'session.compact', error)
      }
    }
    return result
  }).catch(($, e, next) => next(e))

  // Runs that ended too big in the background have no row to tell it on: it is told at the end of a reply, under the notes' own cap.
  onTurnEnd(async end => {
    const { io, snap, settings, now } = end
    if (pendingSplit.compaction + pendingSplit.brief <= 0) return
    if (!noteAllowed(snap.lastNoteReply, snap.replies, NOTE_EVERY)) return
    const found = splitRun({ compaction: pendingSplit.compaction, brief: pendingSplit.brief, th: thresholds(settings), why: SPLIT_WHY })
    const text = tell(snap, settings, found, 'log', now)
    if (text === null) return
    pendingSplit = newSplit()
    snap.lastNoteReply = snap.replies
    io.note(`● ${COPY['note.lead']} · ${text}`)
  })
}
