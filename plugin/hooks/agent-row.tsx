import { atom, read } from 'claude-code'
import type { Register } from 'claude-code'

import { ICONS, modelColor, rowLine } from './agent-view'
import { COPY } from './generated/copy.js'
import { emptySnap, soundSnap } from './live'
import { drawView } from './view'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)

// The agent's row under its Agent (or Task) call in the transcript: its state,
// name, model and cost, with the tokens in and out when detailed. A coaching
// note about the run (report_reread, split_run) sits under it.

const STATUS_COLORS = { running: 'subtle', done: 'success', failed: 'error' } as const

function isAgentTool(name: string): boolean {
  return name === 'Agent' || name === 'Task'
}

/** The agentId the call's result carries, if it has one. */
function agentIdOf(output: unknown): string | null {
  if (output === null || typeof output !== 'object') return null
  const id = (output as { agentId?: unknown }).agentId
  return typeof id === 'string' && id !== '' ? id : null
}

export const register: Register = on => {
  on('ui.render', { component: 'ToolResult' }, async ($, e, next) => {
    const base = await next(e)
    if (!isAgentTool(e.props.tool)) return base
    const snap = soundSnap(await read($, snapAtom))
    const id = agentIdOf(e.props.output)
    const agent = snap.agents.find(one => (id !== null ? one.id === id : one.toolUseId === e.props.tool_use_id))
    if (agent === undefined) return base

    const view = drawView(snap, await read($, settingsAtom), await read($, detailedAtom), await $.clock.now(), false)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="column">
        {base}
        <Box flexDirection="row" gap={1}>
          <Text color={STATUS_COLORS[agent.status]}>{ICONS[agent.status]}</Text>
          <Text color={modelColor(agent)} wrap="truncate">
            {rowLine(agent, view.settings, view.detailed)}
          </Text>
        </Box>
        {agent.notes.map(note => (
          <Text key={`note:${note.kind}`} dimColor>
            {`${COPY['note.lead']} · ${note.text}`}
          </Text>
        ))}
      </Box>
    )
  })
}
