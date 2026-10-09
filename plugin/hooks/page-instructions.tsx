import type { Register, RenderElement } from 'claude-code'

import type { GlassSettings } from '../types'
import { linkTarget } from './agent-view'
import { COPY } from './generated/copy.js'
import { ledger, register as watch } from './instructions'
import { charsPerToken } from './lib/coach.js'
import { adviceFor, agentTypesOf, entriesOf, fitToCategories, serverLabel, startupChars, startupGroups, tokensOf } from './lib/instructions.js'
import type { Entry, StartupRow } from './lib/instructions.js'
import { fill } from './lib/text.js'
import { formatTokens } from './lib/units.js'
import { addPage } from './pane'
import type { PageContext } from './pane'
import { figureRow, heading } from './rows'

// The Startup page (the page id is still `instructions`, so a stored choice keeps
// working): what is in every request before the person types, and what they can do
// about it. It opens with that total in one sentence and, when one thing is big
// enough to fix, one line of advice. Then three groups: what the person controls
// (MCP servers, CLAUDE.md and memory, the skills list, plugin tools), Claude Code's
// own, which is one total because it can't be changed, and what was added as the
// work went on (files Claude reached, skills it ran, reminders). Claude Code's own
// count of the startup context is the total when it has one, and the rows are shares
// of it by characters of text; before it has counted, or for a row no row of /context
// covers, the size is characters over the calibrated characters per token (the ledger
// is in instructions.ts). Every size is an estimate, said once in a footnote.
// Detailed lists each server and file under its row, says which agent types received
// CLAUDE.md, and links to the CLAUDE.md files in the dashboard.

/** The words that name a row: a server count is part of its name. */
function rowName(row: StartupRow): string {
  switch (row.id) {
    case 'mcp':
      return fill(COPY['startup.mcp'], { count: row.count })
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

function contextLink(settings: GlassSettings): string | null {
  const base = settings.dashboardUrl
  return base === null ? null : `${base}/#/agents/context`
}

function adviceLine(entries: Entry[], cpt: number, sizes: Sizes, names: Record<string, string>): string | null {
  const found = adviceFor(entries, cpt, undefined, sizes)
  if (found === null) return null
  if (found.kind === 'file') return fill(COPY['startup.advice.file'], { name: found.name, tokens: formatTokens(found.tokens) })
  const server = serverLabel(found.name, names, entries.find(one => one.key === `mcp:${found.name}`)?.tools)
  return fill(COPY['startup.advice.mcp'], { server, tokens: formatTokens(found.tokens) })
}

/** A row's heading line is its figure; in Detailed the servers or files under it follow, indented. */
function rowLines(context: PageContext, row: StartupRow): RenderElement[] {
  const { table } = context
  const lines = [figureRow(table, `row:${row.id}`, rowName(row), formatTokens(row.tokens))]
  if (!context.view.detailed || !['mcp', 'files', 'files-later'].includes(row.id)) return lines
  row.items.forEach((item, at) => {
    const name = item.instructions === true ? COPY['startup.mcp.instructions'] : row.id === 'files-later' ? fill(COPY['startup.later.mark'], { name: item.label }) : item.label
    const figure = item.deferred ? COPY['startup.deferred'] : formatTokens(item.tokens)
    lines.push(figureRow(table, `item:${row.id}:${at}`, name, figure, { indent: 2, dim: item.deferred }))
  })
  return lines
}

async function render(context: PageContext): Promise<RenderElement> {
  const { table, view } = context
  const { Box, Text, Button, Link } = table
  const entries = entriesOf(ledger.items)

  if (entries.length === 0) return <Text>{COPY['startup.none']}</Text>

  const cpt = charsPerToken(view.settings.charsPerToken, view.snap.model)
  // Claude Code's own count of the startup context, split among the rows; the estimate alone when it has none yet.
  const fit = fitToCategories(entries, await context.kit.categories())
  const sizes: Sizes = fit.matched ? fit.sizes : {}
  const total = fit.matched ? fit.total : tokensOf(startupChars(entries), cpt)
  const names = await context.kit.mcpNames()
  const groups = startupGroups(entries, one => sizeOf(one, cpt, sizes), names, total)
  const advice = adviceLine(entries, cpt, sizes, names)
  // Claude Code's figure is not an estimate; the estimate says so in the lead. A count too small to hold the
  // rows the page lists (a breakdown that left a part out) gives way to the estimate, so the lead is the groups' sum.
  const counted = fit.matched && groups.fits
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
      <Text dimColor>{COPY['startup.own.note']}</Text>
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
    const link = contextLink(view.settings)
    if (link !== null) {
      children.push(
        <Box key="dashboard" flexDirection="row" gap={2} alignItems="center">
          <Link key="dashboard-link" href={linkTarget(link)} label={COPY['startup.dashboard']} />
          <Button key="dashboard-copy" label={context.state.copied === 'context-link' ? COPY['pane.copied'] : COPY['startup.dashboard.link']} dimColor onPress={press => context.copy(link, press.surface, 'context-link')} />
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
  addPage({ id: 'instructions', hotkey: 's', labelKey: 'pane.instructions', render })
}
