import type { GlassAgent, GlassSettings, GlassSnap } from '../types'
import { shortAmount } from './fmt'
import { COPY } from './generated/copy.js'
import { agentLabel, agentShare, shortModel, sortAgents, tierOf } from './lib/agents.js'
import { fill } from './lib/text.js'
import { formatTokens } from './lib/units.js'

// What the Agents page, the agent rows in the transcript and the band's agents
// segment all work out from the snapshot's agent list: who is running, the
// words for a row, the agents' share of the session's cost.

/** The model's colour is a theme key, so it follows the person's theme (a raw colour can't tell light from dark). */
const TIER_COLORS = { opus: 'suggestion', sonnet: 'claude', haiku: 'success', other: 'subtle' } as const

export const ICONS = { running: '●', done: '✓', failed: '✗' } as const

/** The agents running now. */
export function runningCount(snap: GlassSnap): number {
  return snap.agents.filter(one => one.status === 'running').length
}

/** What every agent of the session has cost so far, estimated from the rates in the settings. */
export function agentsUsd(snap: GlassSnap): number {
  return snap.agents.reduce((sum, one) => sum + one.usd, 0)
}

/** The agents' share of the session's cost, a whole percent. */
export function sharePercent(snap: GlassSnap): number {
  return agentShare(agentsUsd(snap), snap.cost)
}

/** The rows in page order: running first. */
export function ordered(snap: GlassSnap): GlassAgent[] {
  return sortAgents(snap.agents)
}

export function nameOf(agent: GlassAgent): string {
  return agentLabel(agent.type, agent.desc)
}

export function modelName(agent: GlassAgent): string {
  return shortModel(agent.model)
}

export function modelColor(agent: GlassAgent): string {
  return TIER_COLORS[tierOf(agent.model)]
}

/** What the agent is doing, in plain words: its latest tool's, or "done" / "stopped with an error". */
export function activityWords(agent: GlassAgent): string {
  if (agent.status === 'done') return COPY['agent.done']
  if (agent.status === 'failed') return COPY['agent.failed']
  return COPY[`agent.${agent.activity}`]
}

export function costText(agent: GlassAgent, settings: GlassSettings): string {
  return shortAmount(agent.usd, settings)
}

/** The agent's row in the transcript: `Explore · haiku · $0.004`, with the tokens in and out when detailed. */
export function rowLine(agent: GlassAgent, settings: GlassSettings, detailed: boolean): string {
  const vars = { name: nameOf(agent), model: modelName(agent), amount: costText(agent, settings), input: formatTokens(agent.input + agent.cacheRead + agent.cacheWrite), output: formatTokens(agent.output) }
  return fill(COPY[detailed ? 'agent.line.detailed' : 'agent.line'], vars)
}

/** The share of an agent's input that came from the cache, a whole percent; null before it has made a request. */
export function cacheShare(agent: GlassAgent): number | null {
  const total = agent.input + agent.cacheRead + agent.cacheWrite
  return total > 0 ? Math.round((agent.cacheRead / total) * 100) : null
}

/** The desktop opens http links only for localhost, so the dashboard's loopback address is written that way for the Link. */
export function linkTarget(link: string): string {
  return link.replace(/^http:\/\/127\.0\.0\.1(?=[:/])/, 'http://localhost')
}

/** Where the dashboard lists subagent runs, or null when its address isn't known. */
export function agentsLink(settings: GlassSettings): string | null {
  const base = settings.dashboardUrl
  return base === null ? null : `${base}/#/agents/subagents?t=habits.habits_agent_runs`
}
