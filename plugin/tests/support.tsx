import { mock } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { RATES } from '../hooks/generated/rates.js'

// Shared set-up for the ClaudeGlass tests: a mocked clock, store and
// environment, a file system in memory, and Claude Code's own figures
// (session.usage) that a test moves as it goes.

export const SITE = { scroll: undefined, view: undefined }
export const CONFIG = '/cfg'
export const T0 = Date.UTC(2026, 9, 9, 12, 0, 0)
export const SESSION = 'session-one'
export const WINDOW = 200000
export const POINT = 160000

export type Step = {
  input?: number
  cacheRead: number
  cacheWrite: number
  output: number
  model?: string
  tools?: string[]
}

export type World = {
  clock: ReturnType<typeof mock.clock>
  files: Map<string, string>
  logs: string[]
  /** Sets what session.usage reports: context tokens and session cost. */
  figures: (tokens: number, usd: number) => void
  opened: unknown[]
  copied: string[]
  /** What the next model request reports (turn.step's bottom answers with it). */
  next: { usage: Step; tools: string[] }
  /** The session's cost so far. */
  usd: number
  turns: number
  /** The props each component last reached the host with, after the mod's hooks. */
  seen: Record<string, Record<string, unknown>>
  /** The rate-limit windows session.measure reports. */
  limits: Limit[]
  /** How many times each component reached the bottom of the render chain: once for every draw of it. */
  draws: Record<string, number>
  /** What session.messages reports: the transcript so far (a test fills it). */
  transcript: unknown[]
  /** The window and summarising point session.usage reports (a 200k window and a 160k point when left out). */
  shape: { window?: number; point?: number; buffer?: number; startupRows?: { name: string; tokens: number }[] }
  /** The project root session.root reports. */
  root: string
}

/** The coaching block of a settings file written for a user who turned coaching and the rating reminder on. */
const COACHING_ON = { enabled: true, feedback_reminder: true }

export function settingsFile(over: Record<string, unknown> = {}): string {
  const coaching = typeof over.coaching === 'object' && over.coaching !== null ? (over.coaching as Record<string, unknown>) : {}
  return JSON.stringify({
    schema: 1,
    billing: { mode: 'api', share_per_usd: null, period_label: 'week', basis: 'list' },
    currency: 'USD',
    pricing: { version: RATES.version, models: RATES.models },
    cache: { ttl_seconds: 300, source: 'default' },
    dashboard_url: 'http://127.0.0.1:8792',
    ...over,
    coaching: { ...COACHING_ON, ...coaching },
  })
}

/**
 * Claude Code's context figures for `tokens` in use. The breakdown names the summarising point
 * (`autoCompactThreshold`) unless `over.buffer` is given: then it names an "Autocompact buffer" category
 * instead and the point is the window less that, as on a machine whose Claude Code leaves the threshold out.
 */
export function usageFor(tokens: number, usd: number, over: { point?: number; window?: number; startup?: number; buffer?: number; startupRows?: { name: string; tokens: number }[] } = {}) {
  const window = over.window ?? WINDOW
  const point = over.point ?? POINT
  // The startup part is one "System prompt" row, or the rows a test names (Memory files, System tools, ...) when it gives them.
  const rows = over.startupRows ?? [{ name: 'System prompt', tokens: Math.min(over.startup ?? 18000, tokens) }]
  const startup = rows.reduce((sum, row) => sum + row.tokens, 0)
  return {
    startedAt: 0,
    context: {
      tokens,
      window,
      percent: (tokens / window) * 100,
      breakdown: {
        maxTokens: window,
        percentage: (tokens / window) * 100,
        ...(over.buffer === undefined ? { autoCompactThreshold: point } : {}),
        categories: [
          ...rows.map(row => ({ kind: 'used', name: row.name, tokens: row.tokens })),
          { kind: 'used', name: 'Messages', tokens: tokens - startup },
          { kind: 'free', name: 'Free space', tokens: Math.max(0, window - tokens - (over.buffer ?? 0)) },
          ...(over.buffer === undefined ? [] : [{ kind: 'buffer', name: 'Autocompact buffer', tokens: over.buffer }]),
        ],
      },
    },
    rateLimits: [],
    cost: { usd },
  }
}

export function prepare(on: On, options: { settings?: string | null; now?: number; session?: string } = {}): World {
  const clock = mock.clock(on, { now: options.now ?? T0 })
  mock.store(on)
  mock.env(on, { CLAUDE_CONFIG_DIR: '/cfg' })
  const files = new Map<string, string>()
  if (options.settings !== null) files.set(`${CONFIG}/claudeglass/mod/settings.json`, options.settings ?? settingsFile())
  const world: World = { clock, files, logs: [], figures: () => {}, opened: [], copied: [], next: { usage: { cacheRead: 0, cacheWrite: 0, output: 0 }, tools: [] }, usd: 0, turns: 0, seen: {}, limits: [], draws: {}, transcript: [], shape: {}, root: '/work/project' }
  let current = usageFor(0, 0)
  world.figures = (tokens, usd) => {
    current = usageFor(tokens, usd, world.shape)
  }
  on('session.id', () => ({ value: options.session ?? SESSION }) as never)
  on('session.surfaces', () => ({ value: ['terminal'] }) as never)
  on('session.messages', () => ({ value: world.transcript }) as never)
  on('session.usage', () => ({ value: current }) as never)
  on('session.root', () => ({ value: world.root }) as never)
  // The engine makes a path absolute (C:\cfg\... on Windows); the files are kept by the path as the mod wrote it.
  const key = (path: string) => path.replace(/[\\]/g, '/').replace(/^[A-Za-z]:/, '')
  on('fs.read', (_$, e) => {
    // A file that isn't there reads as empty: a throw here fails the test, where the engine would reject.
    return { value: files.get(key(e.path)) ?? '' } as never
  })
  on('fs.write', (_$, e) => {
    files.set(key(e.path), e.text)
    return { value: undefined } as never
  })
  on('ui.log', (_$, e) => {
    world.logs.push(e.text)
    return { value: undefined }
  })
  on('ui.invalidate', () => ({ value: undefined }) as never)
  on('ui.open', (_$, e) => {
    world.opened.push(e)
    return { value: { isPlaced: true } } as never
  })
  on('ui.copy', (_$, e) => {
    world.copied.push(e.text)
    return { value: { isCopied: true } } as never
  })
  on('command.register', (_$, e) => ({ value: { command: e.name } }) as never)
  // What sits under the mod's hooks: an empty Box, as the host's own drawing stands.
  on('ui.render', ($, e) => {
    world.seen[e.component] = e.props as Record<string, unknown>
    world.draws[e.component] = (world.draws[e.component] ?? 0) + 1
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.measure', (_$, e) => ({ changed: e.changed }))
  on('session.end', (_$, e) => ({ sessionId: e.sessionId, reason: e.reason }) as never)
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', () => ({ text: 'done' }))
  on('turn.step', async function* (_$, e) {
    const { usage, tools } = world.next
    return {
      turnId: e.turnId,
      index: e.index,
      answer: '',
      toolUses: tools.map(name => ({ id: name, name, input: {} })),
      stopReason: 'end_turn',
      usage: { model: usage.model ?? 'claude-sonnet-4-6', input_tokens: usage.input ?? 0, output_tokens: usage.output, cache_read_input_tokens: usage.cacheRead, cache_creation_input_tokens: usage.cacheWrite },
    } as never
  })
  return world
}

export const START = { cwd: '/work', surface: 'terminal', isInteractive: true } as const

export type Limit = { kind: string; percentUsed: number; resetsAt?: string }

export function measured(tokens: number, usd: number, limits: Limit[] = []) {
  return { context: { tokens, window: WINDOW, percent: (tokens / WINDOW) * 100 }, rateLimits: limits, cost: { usd }, changed: ['context', 'cost'] } as never
}

export type Reply = {
  /** Idle time before the message is sent, in ms. */
  gap?: number
  /** The model requests of the reply, in order; each is the context it read and wrote. */
  steps: Step[]
  /** What the reply cost, in USD. */
  usd: number
}

/** Runs one reply the way the engine does: turn.start, a measurement, each request, turn.complete. */
export async function reply($: Engine, world: World, one: Reply): Promise<void> {
  if (one.gap) await world.clock.advance(one.gap)
  world.turns += 1
  const turnId = `turn-${world.turns}`
  await $.turn.start({ text: 'go', turnId })
  let index = 0
  for (const step of one.steps) {
    world.next = { usage: step, tools: step.tools ?? [] }
    const stream = $.turn.step({ turnId, index, model: step.model ?? 'claude-sonnet-4-6', messageCount: index === 0 && world.turns === 1 ? 1 : 2 + index })
    for await (const chunk of stream) void chunk
    await stream.result
    index += 1
  }
  const last = one.steps[one.steps.length - 1]
  const context = last === undefined ? 0 : (last.input ?? 0) + last.cacheRead + last.cacheWrite + last.output
  world.usd += one.usd
  world.figures(context, world.usd)
  await $.session.measure(measured(context, world.usd, world.limits))
  await $.turn.complete({ turnId, reason: 'answer', answer: 'done', isAborted: false, durationMs: 1000 })
  await world.clock.advance(300)
}

/** The mod's own faults, which it writes to the debug log. */
export function faults(world: World): string[] {
  return world.logs.filter(line => line.startsWith('claudeglass:'))
}

/** The notes the mod wrote to the transcript. */
export function notes(world: World): string[] {
  return world.logs.filter(line => line.startsWith('● Glass'))
}

type Node = string | { type: string; props?: Record<string, unknown>; children?: unknown[] }

/** The text a drawn element shows. */
export function textOf(node: unknown): string {
  if (typeof node === 'string') return node
  if (node === null || typeof node !== 'object') return ''
  const one = node as Exclude<Node, string>
  if (one.type === 'Button' || one.type === 'Link') return String(one.props?.label ?? '')
  if (one.type === 'Svg') return ''
  return (one.children ?? []).map(textOf).join('')
}

/** Every element of a drawn tree, outermost first. */
export function elements(node: unknown): Exclude<Node, string>[] {
  if (node === null || typeof node !== 'object') return []
  const one = node as Exclude<Node, string>
  return [one, ...(one.children ?? []).flatMap(elements)]
}
