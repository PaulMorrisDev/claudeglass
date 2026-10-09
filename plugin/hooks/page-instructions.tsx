import type { Register, RenderElement } from 'claude-code'

import type { GlassSettings } from '../types'
import { linkTarget } from './agent-view'
import { COPY } from './generated/copy.js'
import { ledger, register as watch } from './instructions'
import { charsPerToken } from './lib/coach.js'
import { adviceFor, agentTypesOf, entriesOf, groupsOf, humanize, largest, shortPath, startupChars, tokensOf } from './lib/instructions.js'
import type { Entry } from './lib/instructions.js'
import { fill } from './lib/text.js'
import { formatTokens } from './lib/units.js'
import { addPage } from './pane'
import type { PageContext } from './pane'

// The Instructions page: what this session loaded into Claude's context, in the
// order it loaded, with an estimated size for each. The first screen is the
// startup total, the five largest items and, when one is big enough to fix, one
// line of advice; Detailed lists everything grouped by kind, marks the files that
// loaded later because Claude reached matching paths, and says which agent types
// received CLAUDE.md. Pressing an item shows what it is, where it came from and
// its size. The sizes are estimates: characters of text over the calibrated
// characters per token (the ledger is in instructions.ts).

/** One key per row, as the Agents page has: the digits, then letters the pane's own buttons don't use. */
const ROW_KEYS = '123456789bdefjoqrstuvwxz'
const COPY_KEY = 'y'
const TOP = 5

type Kinds = 'system' | 'instructions' | 'context' | 'skills' | 'tools' | 'deferred' | 'mcp' | 'attachment'

function est(tokens: number): string {
  return `${formatTokens(tokens)} ${COPY['amount.est']}`
}

function kindLabel(kind: string): string {
  return COPY[`instructions.kind.${kind as Kinds}`]
}

function fromLabel(entry: Entry): string {
  if (entry.kind === 'mcp') return fill(COPY['instructions.from.mcp'], { server: entry.server ?? '' })
  return COPY[`instructions.from.${entry.from as 'engine'}`] ?? COPY['instructions.from.engine']
}

/** What a row is called: a rolled-up group by its kind and count, an item by its own name. */
function nameOf(entry: Entry): string {
  if (entry.kind === 'tools' || entry.kind === 'deferred') return fill(COPY[`instructions.name.${entry.kind}`], { count: entry.count })
  if (entry.kind === 'mcp') return fill(COPY[entry.count === 1 ? 'instructions.name.mcp.one' : 'instructions.name.mcp'], { server: entry.server ?? '', count: entry.count })
  if (entry.kind === 'instructions') return entry.path !== undefined ? shortPath(entry.path) : entry.name
  const name = entry.kind === 'system' || entry.kind === 'attachment' ? humanize(entry.name) : entry.name
  return entry.count > 1 ? fill(COPY['instructions.times'], { name, count: entry.count }) : name
}

/** The size a row is counted at in the startup context (what is in every request), or all of it for a row that is not part of it. */
function sizeOf(entry: Entry, cpt: number): number {
  return tokensOf(entry.startupChars > 0 ? entry.startupChars : entry.chars, cpt)
}

function contextLink(settings: GlassSettings): string | null {
  const base = settings.dashboardUrl
  return base === null ? null : `${base}/#/agents/context`
}

function headline(count: number, tokens: number): string {
  return fill(COPY[count === 1 ? 'instructions.headline.one' : 'instructions.headline'], { tokens: est(tokens), count })
}

function adviceLine(entries: Entry[], cpt: number): string | null {
  const found = adviceFor(entries, cpt)
  if (found === null) return null
  return fill(COPY[found.kind === 'mcp' ? 'instructions.advice.mcp' : 'instructions.advice.file'], { server: found.name, name: found.name, tokens: est(found.tokens) })
}

function details(context: PageContext, entry: Entry, cpt: number): RenderElement {
  const { Box, Text, Button, Link } = context.table
  const link = entry.kind === 'instructions' ? contextLink(context.view.settings) : null
  const lines: string[] = [
    fill(COPY['instructions.detail.kind'], { kind: kindLabel(entry.kind) }),
    fill(COPY['instructions.detail.from'], { from: fromLabel(entry) }),
    fill(COPY['instructions.detail.size'], { tokens: est(tokensOf(entry.chars, cpt)), chars: entry.chars.toLocaleString('en-US') }),
  ]
  if (entry.startupChars > 0 && entry.startupChars !== entry.chars) {
    lines.push(fill(COPY['instructions.detail.live'], { tokens: est(tokensOf(entry.startupChars, cpt)) }))
  }
  if (entry.deferred) lines.push(COPY['instructions.detail.deferred'])
  if (entry.scoped) {
    const globs = entry.globs ?? []
    lines.push(globs.length > 0 ? fill(COPY['instructions.detail.scoped.globs'], { reply: entry.reply, globs: globs.join(', ') }) : fill(COPY['instructions.detail.scoped'], { reply: entry.reply }))
  }
  if (entry.parent !== undefined) lines.push(fill(COPY['instructions.detail.parent'], { path: entry.parent }))
  if (entry.biggest.length > 0) {
    lines.push(fill(COPY['instructions.detail.biggest'], { names: entry.biggest.map(one => `${one.name} ${formatTokens(tokensOf(one.chars, cpt))}`).join(', ') }))
  }
  return (
    <Box key={`details:${entry.key}`} flexDirection="column" paddingLeft={4}>
      <Text bold>{nameOf(entry)}</Text>
      {entry.path !== undefined ? <Text dimColor>{entry.path}</Text> : null}
      {lines.map(line => (
        <Text key={line}>{line}</Text>
      ))}
      {link !== null ? (
        <Box key="dashboard" flexDirection="row" gap={1}>
          <Link key="dashboard-link" href={linkTarget(link)} label={COPY['instructions.dashboard']} />
          <Button key="dashboard-copy" label={context.state.copied ? COPY['pane.copied'] : COPY['pane.link']} hotkey={COPY_KEY} onPress={press => context.copy(link, press.surface)} />
        </Box>
      ) : null}
    </Box>
  )
}

/** A row's words after its name: its size, and in the detailed list what makes it special. */
function figures(entry: Entry, cpt: number, detailed: boolean): string {
  const parts = [est(sizeOf(entry, cpt))]
  if (detailed && entry.scoped) parts.push(fill(COPY['instructions.scoped.mark'], { reply: entry.reply }))
  if (detailed && entry.deferred) parts.push(COPY['instructions.deferred.mark'])
  return parts.join(' · ')
}

async function render(context: PageContext): Promise<RenderElement> {
  const { table, view, state } = context
  const { Box, Text, Button } = table
  const entries = entriesOf(ledger.items)

  if (entries.length === 0) return <Text>{COPY['instructions.none']}</Text>

  const cpt = charsPerToken(view.settings.charsPerToken, view.snap.model)
  const total = tokensOf(startupChars(entries), cpt)
  const startupCount = entries.filter(one => one.startupChars > 0).length
  const advice = adviceLine(entries, cpt)
  let used = 0

  const row = (entry: Entry): RenderElement => {
    const key = `item:${entry.key}`
    const hotkey = ROW_KEYS.charAt(used)
    used += 1
    const label = `${nameOf(entry)} · ${figures(entry, cpt, view.detailed)}`
    return (
      <Box key={`row:${entry.key}`} flexDirection="column">
        {hotkey === '' ? <Text>{label}</Text> : <Button key={key} label={label} hotkey={hotkey} onPress={() => context.explain(key)} />}
        {state.explain === key ? details(context, entry, cpt) : null}
      </Box>
    )
  }

  const children: RenderElement[] = [
    <Text key="headline" bold>
      {headline(startupCount, total)}
    </Text>,
  ]
  if (advice !== null) children.push(<Text key="advice">{advice}</Text>)

  if (!view.detailed) {
    children.push(
      <Text key="largest" bold>
        {COPY['instructions.largest']}
      </Text>,
      ...largest(entries, TOP).map(row),
    )
  }
  children.push(<Text key="total">{fill(COPY['instructions.total'], { tokens: est(total) })}</Text>)

  if (view.detailed) {
    const theirs = view.parts?.startup ?? null
    if (theirs !== null) {
      children.push(
        <Box key="compare" flexDirection="column">
          <Text>{fill(COPY['instructions.compare'], { theirs: formatTokens(theirs), mine: est(total) })}</Text>
          <Text dimColor>{COPY['instructions.compare.why']}</Text>
        </Box>,
      )
    }
    for (const group of groupsOf(entries)) {
      children.push(
        <Box key={`group:${group.kind}`} flexDirection="column">
          <Text bold>{kindLabel(group.kind)}</Text>
          {group.entries.map(row) as never}
        </Box>,
      )
    }
    const agents = agentTypesOf(view.snap.agents, ledger.agents)
    if (agents.got.length > 0 || agents.skipped.length > 0) {
      children.push(
        <Box key="agents" flexDirection="column">
          <Text bold>{COPY['instructions.agents']}</Text>
          {agents.got.length > 0 ? <Text>{fill(COPY['instructions.agents.got'], { types: agents.got.join(', ') })}</Text> : null}
          {agents.skipped.length > 0 ? <Text>{fill(COPY['instructions.agents.skipped'], { types: agents.skipped.join(', ') })}</Text> : null}
        </Box>,
      )
    }
  }

  return <Box flexDirection="column">{children as never}</Box>
}

export const register: Register = (on, options) => {
  watch(on, options)
  addPage({ id: 'instructions', hotkey: 'i', labelKey: 'pane.instructions', render })
}
