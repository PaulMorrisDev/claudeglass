import type { Register, RenderElement } from 'claude-code'

import { activityWords, agentsLink, agentsUsd, cacheShare, costText, ICONS, linkTarget, modelColor, modelName, nameOf, ordered, runningCount, sharePercent } from './agent-view'
import { promptOf } from './agents'
import { shortAmount } from './fmt'
import { COPY } from './generated/copy.js'
import { fill } from './lib/text.js'
import { formatTokens } from './lib/units.js'
import { addPage } from './pane'
import type { PageContext } from './pane'

// The Agents page: who is working, on what, and what it costs. The first
// screen is one sentence and a row per agent, running first; Detailed adds the
// tokens, calls and cache per row and the main session's row; pressing a row
// shows its task prompt, the tools it used and its biggest results.

/**
 * One key per row: the digits, then letters the pane's own buttons don't use
 * (a, c, i and n are page keys, g opens the pane, h, l and m are the footer's, k is held back
 * for a page, and y copies the dashboard link).
 */
const ROW_KEYS = '123456789bdefjoqrstuvwxz'
const COPY_KEY = 'y'
const NAME_CELLS = 24
const MODEL_CELLS = 8
const ACTIVITY_CELLS = 24

function headline(context: PageContext): string {
  const { snap } = context.view
  const running = runningCount(snap)
  const pct = sharePercent(snap)
  if (running === 0) return fill(COPY['agents.headline.idle'], { pct })
  return running === 1 ? fill(COPY['agents.headline.one'], { pct }) : fill(COPY['agents.headline'], { count: running, pct })
}

function figures(context: PageContext, agent: ReturnType<typeof ordered>[number]): string {
  const input = agent.input + agent.cacheRead + agent.cacheWrite
  const cache = cacheShare(agent)
  const parts = [
    fill(COPY['agents.tokens'], { input: formatTokens(input), output: formatTokens(agent.output) }),
    agent.calls === 1 ? COPY['agents.calls.one'] : fill(COPY['agents.calls'], { count: agent.calls }),
  ]
  if (cache !== null) parts.push(fill(COPY['agents.cache'], { pct: cache }))
  return parts.join(' · ')
}

function details(context: PageContext, agent: ReturnType<typeof ordered>[number]): RenderElement {
  const { Box, Text, Button, Link } = context.table
  const settings = context.view.settings
  const prompt = promptOf(agent.id)
  const tools = Object.entries(agent.tools).sort((a, b) => b[1] - a[1])
  const link = agentsLink(settings)
  const where = [nameOf(agent), modelName(agent), agent.background ? COPY['agents.background'] : ''].filter(one => one !== '').join(' · ')

  const children: RenderElement[] = [
    <Text key="where" bold>
      {where}
    </Text>,
    <Text key="task-title" bold>
      {COPY['agents.task']}
    </Text>,
  ]
  if (prompt === null) children.push(<Text key="task-gone">{COPY['agents.prompt.gone']}</Text>)
  else prompt.split('\n').forEach((line, index) => children.push(<Text key={`task:${index}`} dimColor>{line === '' ? ' ' : line}</Text>))

  if (tools.length > 0) {
    children.push(
      <Text key="tools-title" bold>
        {COPY['agents.tools']}
      </Text>,
      <Text key="tools">{tools.map(([name, count]) => fill(COPY['agents.tool'], { name: name.startsWith('mcp__') ? COPY['tool.mcp'] : name, count })).join(' · ')}</Text>,
    )
  }
  if (agent.heaviest.length > 0) {
    children.push(
      <Text key="biggest-title" bold>
        {COPY['now.heaviest']}
      </Text>,
      ...agent.heaviest.slice(0, 3).map(item => (
        <Text key={`biggest:${item.name}:${item.tokens}`}>{fill(COPY['now.largest'], { name: item.name.startsWith('mcp__') ? COPY['tool.mcp'] : item.name, tokens: formatTokens(item.tokens) })}</Text>
      )),
    )
  }
  if (link !== null) {
    children.push(
      <Box key="dashboard" flexDirection="row" gap={1}>
        <Link key="dashboard-link" href={linkTarget(link)} label={COPY['agents.dashboard']} />
        <Button key="dashboard-copy" label={context.state.copied ? COPY['pane.copied'] : COPY['pane.link']} hotkey={COPY_KEY} onPress={press => context.copy(link, press.surface)} />
      </Box>,
    )
  }
  return (
    <Box key={`details:${agent.id}`} flexDirection="column" paddingLeft={4}>
      <Text bold>{COPY['agents.details']}</Text>
      {children as never}
    </Box>
  )
}

async function render(context: PageContext): Promise<RenderElement> {
  const { table, view, state } = context
  const { Box, Text, Button } = table
  const { snap, settings } = view
  const list = ordered(snap)

  if (list.length === 0) return <Text>{COPY['agents.none']}</Text>

  const shown = list.slice(0, ROW_KEYS.length)
  const children: RenderElement[] = [
    <Text key="headline" bold>
      {headline(context)}
    </Text>,
  ]

  shown.forEach((agent, index) => {
    const key = `agent:${agent.id}`
    const open = state.explain === key
    children.push(
      <Box key={`row:${agent.id}`} flexDirection="column">
        <Box flexDirection="row" gap={1}>
          <Box width={NAME_CELLS}>
            <Button key={key} label={nameOf(agent)} hotkey={ROW_KEYS.charAt(index)} onPress={() => context.explain(key)} />
          </Box>
          <Box width={MODEL_CELLS}>
            <Text color={modelColor(agent)}>{modelName(agent)}</Text>
          </Box>
          <Box width={ACTIVITY_CELLS}>
            <Text>{`${ICONS[agent.status]} ${activityWords(agent)}`}</Text>
          </Box>
          <Text>{costText(agent, settings)}</Text>
        </Box>
        {view.detailed ? (
          <Box paddingLeft={4}>
            <Text dimColor>{figures(context, agent)}</Text>
          </Box>
        ) : null}
        {open ? details(context, agent) : null}
      </Box>,
    )
  })

  if (list.length > shown.length) {
    children.push(<Text key="more-rows" dimColor>{fill(COPY['agents.more.rows'], { count: list.length - shown.length })}</Text>)
  }

  if (view.detailed) {
    const main = Math.max(0, (snap.cost ?? 0) - agentsUsd(snap))
    children.push(
      <Box key="main" flexDirection="row" gap={1}>
        <Box width={NAME_CELLS}>
          <Text>{COPY['agents.main']}</Text>
        </Box>
        <Box width={MODEL_CELLS}>
          <Text color={snap.model === null ? undefined : modelColor({ model: snap.model } as never)}>{snap.model === null ? '' : modelName({ model: snap.model } as never)}</Text>
        </Box>
        <Box width={ACTIVITY_CELLS}>
          <Text dimColor>{snap.replies === 1 ? COPY['agents.main.replies.one'] : fill(COPY['agents.main.replies'], { count: snap.replies })}</Text>
        </Box>
        <Text>{shortAmount(main, settings)}</Text>
      </Box>,
    )
  }

  return <Box flexDirection="column">{children as never}</Box>
}

export const register: Register = () => {
  addPage({ id: 'agents', hotkey: 'a', labelKey: 'pane.agents', render })
}
