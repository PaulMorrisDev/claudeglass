import type { Register, RenderElement } from 'claude-code'

import type { GlassSettings, GlassSnap } from '../types'
import { COPY } from './generated/copy.js'
import { BUILT_IN, BUILT_IN_PREFIX } from './generated/desktop.js'
import { ledger, register as watch } from './instructions'
import { charsPerToken } from './lib/coach.js'
import { adviceFor, agentTypesOf, bestAdvice, entriesOf, fitToCategories, serverLabel, startupChars, startupGroups, TOOL_ADVICE_REPLIES, tokensOf, unusedTool } from './lib/instructions.js'
import type { Entry, StartupGroups, StartupItem, StartupRow } from './lib/instructions.js'
import { fill } from './lib/text.js'
import { formatTokens } from './lib/units.js'
import { addPage } from './pane'
import type { PageContext } from './pane'
import { figureRow, heading } from './rows'

// The Startup page (the page id is still `instructions`, so a stored choice keeps
// working): what is in every request before the person types, and what they can do
// about it. It opens with that total in one sentence and, when one thing is big
// enough to fix, one line of advice. Then three groups: what the person controls
// (MCP servers, built-in tools, CLAUDE.md and memory, the skills list, plugin tools),
// Claude Code's own, which is one total because it can't be changed, and what was added as the
// work went on (files Claude reached, skills it ran, reminders). Claude Code's own
// count of the startup context is the total when it has one, and the rows are shares
// of it by characters of text; before it has counted, or for a row no row of /context
// covers, the size is characters over the calibrated characters per token (the ledger
// is in instructions.ts). Every size is an estimate, said once in a footnote.
// Detailed lists each server and file under its row, the ten largest built-in tools under
// theirs (a bare tool name in the deny rules drops it from the context) and each MCP server's
// largest tools under it, and says which agent types received CLAUDE.md. What waits behind
// tool search costs nothing, so it is one dim line, not a row each. On the desktop the
// servers the app brings are counted in Claude Code's own and listed under it. The advice is
// the one largest thing worth fixing: a server, a built-in tool Claude has not called, or a file.

/** The words that name a row: a server count is part of its name. */
function rowName(row: StartupRow): string {
  switch (row.id) {
    case 'mcp':
      return fill(COPY['startup.mcp'], { count: row.count })
    case 'tools':
      return fill(COPY['startup.tools'], { count: row.count })
    case 'files':
      return COPY['startup.files']
    case 'skills':
      return COPY['startup.skills']
    case 'plugin':
      return COPY['startup.plugin']
    case 'files-later':
      return COPY['startup.later.files']
    case 'skills-used':
      return COPY['startup.later.skills']
    case 'attached':
      return COPY['startup.later.attached']
    default:
      return COPY['startup.later.reminders']
  }
}

/** Tokens by row, where Claude Code's count has sized the row (lib/instructions.js fitToCategories). */
type Sizes = Record<string, number>

/** The size a row is counted at in the startup context (what is in every request), or all of it for a row that is not part of it. */
function sizeOf(entry: Entry, cpt: number, sizes: Sizes): number {
  return sizes[entry.key] ?? tokensOf(entry.startupChars > 0 ? entry.startupChars : entry.chars, cpt)
}

/** Where the dashboard lists the CLAUDE.md files and agent context, or null when its address isn't known. */
export function contextLink(settings: GlassSettings): string | null {
  const base = settings.dashboardUrl
  return base === null ? null : `${base}/#/agents/context`
}

/** Whether a server is one the desktop app brings (src/claudeglass/desktop_servers.py). */
function appServer(server: string): boolean {
  return BUILT_IN.includes(server) || server.startsWith(BUILT_IN_PREFIX)
}

/** The tool names Claude has called this session: the main session's, and any subagent's. */
function toolsCalled(snap: GlassSnap): string[] {
  return [...new Set([...snap.toolsUsed, ...snap.agents.flatMap(agent => Object.keys(agent.tools))])]
}

function adviceLine(entries: Entry[], cpt: number, sizes: Sizes, names: Record<string, string>, groups: StartupGroups, snap: GlassSnap, own: ((server: string) => boolean) | undefined): string | null {
  // A built-in tool is advised against only once Claude has had a few replies to use it: a fresh or resumed session has called nothing yet.
  const tool = snap.replies >= TOOL_ADVICE_REPLIES ? unusedTool(groups.control.rows, toolsCalled(snap)) : null
  const found = bestAdvice(adviceFor(entries, cpt, undefined, sizes, own), tool)
  if (found === null) return null
  const tokens = formatTokens(found.tokens)
  if (found.kind === 'file') return fill(COPY['startup.advice.file'], { name: found.name, tokens })
  if (found.kind === 'tool') return fill(COPY['startup.advice.tool'], { tool: found.name, tokens })
  const server = serverLabel(found.name, names, entries.find(one => one.key === `mcp:${found.name}`)?.tools)
  return fill(COPY['startup.advice.mcp'], { server, tokens })
}

/** The most tools listed under one MCP server before "and N more". */
const SERVER_TOOLS = 5
/** The most built-in tools listed before "N more". */
const BUILT_IN_TOOLS = 10

/** The waiting lines carry no figure, so they may run past the figure column (ROW_CELLS) to this width. */
const WAITING_CELLS = 84

/** The one dim line for what waits behind tool search: how many, and as many names as fit the row, then an ellipsis. */
function waitingLine(context: PageContext, key: string, items: StartupItem[], many: string, one: string): RenderElement | null {
  if (items.length === 0) return null
  const { Box, Text } = context.table
  // A name that is only an id ("Connector (...)") says least, so the others come first.
  const labels = [...items.filter(item => !item.label.startsWith('Connector')), ...items.filter(item => item.label.startsWith('Connector'))].map(item => item.label)
  const indent = 2
  const room = WAITING_CELLS - indent
  const say = (names: string) => fill(items.length === 1 ? one : many, { count: items.length, names })
  // Whole names only: as many as the row holds, then ", …" for the rest; never a cut inside a name.
  let fits = say(labels.join(', '))
  for (let kept = labels.length - 1; fits.length > room && kept >= 0; kept -= 1) fits = say(kept === 0 ? '…' : `${labels.slice(0, kept).join(', ')}, …`)
  return (
    <Box key={key} paddingLeft={indent}>
      <Text dimColor wrap="truncate">
        {fits}
      </Text>
    </Box>
  )
}

/** A row's heading line is its figure; in Detailed the servers, tools or files under it follow, indented. */
function rowLines(context: PageContext, row: StartupRow): RenderElement[] {
  const { table } = context
  const { Text } = table
  const lines = [figureRow(table, `row:${row.id}`, rowName(row), formatTokens(row.tokens))]
  if (!context.view.detailed || !['mcp', 'tools', 'files', 'files-later'].includes(row.id)) return lines
  // What waits behind tool search costs nothing at the start: it is counted in the row's name and said once, in a dim line.
  const waiting = row.items.filter(item => item.deferred)
  const listed = row.items.filter(item => !item.deferred)
  const shown = row.id === 'tools' ? listed.slice(0, BUILT_IN_TOOLS) : listed
  shown.forEach((item, at) => {
    const name = item.instructions === true ? COPY['startup.mcp.instructions'] : row.id === 'files-later' ? fill(COPY['startup.later.mark'], { name: item.label }) : item.label
    lines.push(figureRow(table, `item:${row.id}:${at}`, name, formatTokens(item.tokens), { indent: 2 }))
    // A server's largest tools under it, the rest as a count.
    const tools = item.tools ?? []
    tools.slice(0, SERVER_TOOLS).forEach((tool, nth) => lines.push(figureRow(table, `item:${row.id}:${at}:${nth}`, tool.label, formatTokens(tool.tokens), { indent: 4 })))
    if (tools.length > SERVER_TOOLS) lines.push(figureRow(table, `item:${row.id}:${at}:more`, fill(COPY['startup.more'], { count: tools.length - SERVER_TOOLS }), '', { indent: 4, dim: true }))
  })
  if (row.id === 'tools' && listed.length > shown.length) {
    const rest = listed.slice(shown.length)
    lines.push(figureRow(table, 'item:tools:more', fill(COPY['startup.tools.more'], { count: rest.length }), formatTokens(rest.reduce((sum, item) => sum + item.tokens, 0)), { indent: 2, dim: true }))
  }
  if (row.id === 'tools') {
    const line = waitingLine(context, 'item:tools:waiting', waiting, COPY['startup.deferred.tools'], COPY['startup.deferred.tools.one'])
    if (line !== null) lines.push(line)
  } else if (row.id === 'mcp') {
    const line = waitingLine(context, 'item:mcp:waiting', waiting, COPY['startup.deferred.servers'], COPY['startup.deferred.servers.one'])
    if (line !== null) lines.push(line)
  }
  if (row.id === 'tools' && row.items.length > 0) {
    lines.push(
      <Text key="tools-note" dimColor>
        {COPY['startup.tools.note']}
      </Text>,
    )
  }
  return lines
}

/**
 * What is listed under Claude Code's own, in Detailed: the servers the app brings, when there are any, led by Claude Code's
 * instructions with the rest of the group's tokens, so the rows add up to the group. Without such servers there are no rows.
 */
function ownRows(table: PageContext['table'], own: StartupGroups['builtin']): RenderElement[] {
  if (own.items.length === 0) return []
  const servers = own.items.reduce((sum, item) => sum + item.tokens, 0)
  return [
    figureRow(table, 'own:instructions', COPY['startup.own.instructions'], formatTokens(Math.max(0, own.tokens - servers)), { indent: 2 }),
    ...own.items.map((item, at) => figureRow(table, `own:${at}`, item.label, formatTokens(item.tokens), { indent: 2 })),
  ]
}

async function render(context: PageContext): Promise<RenderElement> {
  const { table, view } = context
  const { Box, Text } = table
  const entries = entriesOf(ledger.items)

  if (entries.length === 0) return <Text>{COPY['startup.none']}</Text>

  const cpt = charsPerToken(view.settings.charsPerToken, view.snap.model)
  // Claude Code's own count of the startup context, split among the rows; the estimate alone when it has none yet.
  const fit = fitToCategories(entries, await context.kit.categories())
  const sizes: Sizes = fit.matched ? fit.sizes : {}
  // A count that leaves out the built-in tools in the prompt can't hold the rows: the estimate stands, with no total to take them from.
  const total = fit.matched ? (fit.covered ? fit.total : undefined) : tokensOf(startupChars(entries), cpt)
  const names = await context.kit.mcpNames()
  // The servers the app brings count as Claude Code's own only when the session runs in the Desktop app (not the terminal, VS Code or mobile).
  // The session's own surface is the one recorded when it started; before it is known, the surface this pane is drawn on.
  const desktop = (view.snap.surface ?? context.e.surface) === 'desktop'
  const own = desktop ? appServer : undefined
  const groups = startupGroups(entries, one => sizeOf(one, cpt, sizes), names, total, await context.kit.mcpTools(), own)
  const advice = adviceLine(entries, cpt, sizes, names, groups, view.snap, own)
  // Claude Code's figure is not an estimate; the estimate says so in the lead. A count too small to hold the
  // rows the page lists (a breakdown that left a part out) gives way to the estimate, so the lead is the groups' sum.
  const counted = fit.matched && fit.covered && groups.fits
  const lead = fill(COPY[counted ? 'startup.lead' : 'startup.lead.est'], { tokens: formatTokens(groups.total) })

  const children: RenderElement[] = [
    <Box key="lead" flexDirection="column">
      <Text bold>{lead}</Text>
      {advice !== null ? <Text>{advice}</Text> : null}
    </Box>,
  ]

  if (groups.control.rows.length > 0) {
    children.push(
      <Box key="control" flexDirection="column">
        {heading(table, 'control', COPY['startup.control'], formatTokens(groups.control.tokens))}
        {groups.control.rows.flatMap(row => rowLines(context, row))}
      </Box>,
    )
  }
  children.push(
    <Box key="own" flexDirection="column">
      {heading(table, 'own', COPY['startup.own'], formatTokens(groups.builtin.tokens))}
      <Text dimColor>{COPY[desktop ? 'startup.own.note.desktop' : 'startup.own.note']}</Text>
      {view.detailed ? ownRows(table, groups.builtin) : null}
    </Box>,
  )
  if (groups.later.rows.length > 0) {
    children.push(
      <Box key="later" flexDirection="column">
        {heading(table, 'later', COPY['startup.later'], formatTokens(groups.later.tokens))}
        <Text dimColor>{COPY['startup.later.note']}</Text>
        {groups.later.rows.flatMap(row => rowLines(context, row))}
      </Box>,
    )
  }
  if (groups.control.rows.length > 0 || groups.later.rows.length > 0) {
    children.push(
      <Text key="estimates" dimColor>
        {COPY['startup.estimates']}
      </Text>,
    )
  }

  if (view.detailed) {
    const theirs = view.parts?.startup ?? null
    // Claude Code has a count but its rows don't fit what loaded, so the estimate stands beside it.
    if (!counted && theirs !== null && theirs > 0) {
      children.push(
        <Box key="compare" flexDirection="column">
          <Text>{fill(COPY['startup.compare'], { theirs: formatTokens(theirs), mine: formatTokens(groups.total) })}</Text>
          <Text dimColor>{COPY['startup.compare.why']}</Text>
        </Box>,
      )
    }
    const agents = agentTypesOf(view.snap.agents, ledger.agents)
    if (agents.got.length > 0 || agents.skipped.length > 0) {
      children.push(
        <Box key="agents" flexDirection="column">
          <Text bold>{COPY['startup.agents']}</Text>
          {agents.got.length > 0 ? <Text>{fill(COPY['startup.agents.got'], { types: agents.got.join(', ') })}</Text> : null}
          {agents.skipped.length > 0 ? <Text>{fill(COPY['startup.agents.skipped'], { types: agents.skipped.join(', ') })}</Text> : null}
        </Box>,
      )
    }
  }

  return (
    <Box flexDirection="column" gap={1}>
      {children}
    </Box>
  )
}

export const register: Register = (on, options) => {
  watch(on, options)
  addPage({ id: 'instructions', hotkey: 's', labelKey: 'pane.instructions', render, dashboard: view => contextLink(view.settings) })
}
