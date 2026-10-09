import type { Register, RenderElement } from 'claude-code'

import { activityWords, agentsLink, agentsUsd, cacheShare, costText, ICONS, modelColor, modelName, nameOf, ordered, runningCount, sharePercent, taskName, toolsLine } from './agent-view'
import { shortAmount } from './fmt'
import { COPY } from './generated/copy.js'
import { fill } from './lib/text.js'
import { formatTokens } from './lib/units.js'
import { addPage } from './pane'
import type { PageContext } from './pane'

// The Agents page: who is working, on what, and what it costs. The first
// screen is one sentence and a text row per agent, running first, each named by
// the task it was given; Detailed adds, per row, a line of its type, tokens, calls
// and cache and a dim line of the tools it called, and the main session's row.
// No buttons and no hotkeys: the page bar has the keys.

const FIRST_SCREEN_ROWS = 6
const NAME_CELLS = 36
const MODEL_CELLS = 8
const ACTIVITY_CELLS = 22
const TOOLS_SHOWN = 5

function headline(context: PageContext): string {
  const { snap } = context.view
  const running = runningCount(snap)
  const pct = sharePercent(snap)
  if (running === 0) return fill(COPY['agents.headline.idle'], { pct })
  return running === 1 ? fill(COPY['agents.headline.one'], { pct }) : fill(COPY['agents.headline'], { count: running, pct })
}

function figures(agent: ReturnType<typeof ordered>[number]): string {
  const input = agent.input + agent.cacheRead + agent.cacheWrite
  const cache = cacheShare(agent)
  // The type leads the line, unless the row is already named by it.
  const type = nameOf(agent)
  const parts = [
    ...(agent.type.trim() !== '' && taskName(agent) !== type ? [type] : []),
    fill(COPY['agents.tokens'], { input: formatTokens(input), output: formatTokens(agent.output) }),
    agent.calls === 1 ? COPY['agents.calls.one'] : fill(COPY['agents.calls'], { count: agent.calls }),
  ]
  if (cache !== null) parts.push(fill(COPY['agents.cache'], { pct: cache }))
  return parts.join(' · ')
}

async function render(context: PageContext): Promise<RenderElement> {
  const { table, view } = context
  const { Box, Text } = table
  const { snap, settings } = view
  const list = ordered(snap)

  if (list.length === 0) return <Text>{COPY['agents.none']}</Text>

  const shown = view.detailed ? list : list.slice(0, FIRST_SCREEN_ROWS)
  // The rows, the "more" line and the main session's row are one section under the headline.
  const children: RenderElement[] = []

  for (const agent of shown) {
    const tools = view.detailed ? toolsLine(agent, TOOLS_SHOWN) : ''
    children.push(
      <Box key={`row:${agent.id}`} flexDirection="column">
        <Box flexDirection="row" gap={1}>
          <Box width={NAME_CELLS}>
            <Text wrap="truncate">{`${ICONS[agent.status]} ${taskName(agent)}`}</Text>
          </Box>
          <Box width={MODEL_CELLS}>
            <Text color={modelColor(agent)}>{modelName(agent)}</Text>
          </Box>
          <Box width={ACTIVITY_CELLS}>
            <Text wrap="truncate">{activityWords(agent)}</Text>
          </Box>
          {/* The last column is not boxed: an amount on a subscription ("12.34 USD") is wider than any width worth fixing, and a box would wrap it. */}
          <Text>{costText(agent, settings)}</Text>
        </Box>
        {view.detailed ? (
          <Box paddingLeft={2} flexDirection="column">
            <Text dimColor>{figures(agent)}</Text>
            {tools !== '' ? <Text dimColor>{tools}</Text> : null}
          </Box>
        ) : null}
      </Box>,
    )
  }

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

  return (
    <Box flexDirection="column" gap={1}>
      <Text bold>{headline(context)}</Text>
      <Box key="rows" flexDirection="column">
        {children as never}
      </Box>
    </Box>
  )
}

export const register: Register = () => {
  addPage({ id: 'agents', hotkey: 'a', labelKey: 'pane.agents', render, dashboard: view => agentsLink(view.settings) })
}
