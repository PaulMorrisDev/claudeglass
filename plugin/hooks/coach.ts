import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Io } from './io'
import { clearPlanStatus, editBox, editsFile, markEdited, noteBackground, planApproved, planStatusSet, submitted, turnEnded } from './coach-mem'
import { emptySnap, live, touch } from './live'
import { getSettings } from './settings'
import { onCompaction, onTurnEnd } from './track'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)

// The coaching that used to come after a message is sent, given before it. This
// file feeds coach-mem.ts from the events; hint.ts shows what it chooses for the
// draft, notes.ts the rating reminder, and the status line the plan tip.
//
//   prompt.edit    the draft's shape (length, and the words while it is short)
//   prompt.submit  a message sent: the run of small changes, the first message's size
//   tool.call      a file changed; work sent to the background; a plan approved
//   turn.complete  (through track.ts's onTurnEnd) what the reply said and used
//
// Nothing of a message or a plan is kept: lengths, counts and yes-or-no only.

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

function debug($: EngineInterface, where: string, error: unknown): void {
  $.ui.log(`claudeglass: coach ${where} ${String(error)}`, { to: 'debug' })
}

type PlanOutput = { plan?: string | null; filePath?: string; isAgent?: boolean; awaitingLeaderApproval?: boolean }

export const register: Register = on => {
  // Nothing to do per key but to look at the box; a redraw is asked for only when a hint could change.
  on('prompt.edit', async ($, e, next) => {
    const after = await next(e)
    try {
      if (editBox(e, after)) void $.ui.invalidate('ui.render')
    } catch (error) {
      debug($, 'prompt.edit', error)
    }
    return after
  }).catch(($, e, next) => next(e))

  on('prompt.submit', async ($, e, next) => {
    try {
      const io = kit($)
      const snap = await live(io)
      const settings = await getSettings(io)
      submitted(snap, settings, { text: e.text, origin: e.origin.kind, queued: e.turnId !== undefined }, await $.clock.now())
      await touch(io)
    } catch (error) {
      debug($, 'prompt.submit', error)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // The tools watched: edits, shell commands that may go to the background, a plan leaving plan mode.
  // (A matcher, apart from the other files' hooks on tool.call.)
  on('tool.call', { tool: /^(Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|Workflow|ExitPlanMode)$/ }, async ($, e, next) => {
    const result = await next(e)
    if (result.deny !== undefined) return result
    try {
      const name = String(e.tool)
      const args = e as unknown as Record<string, unknown>
      const failed = result.isError === true
      if (!failed && editsFile(name, args)) markEdited()
      if (e.agentId !== undefined) return result
      const io = kit($)
      if (!failed && name !== 'ExitPlanMode') noteBackground(result.text, e.tool_use_id, await $.clock.now())
      if (!failed && name === 'ExitPlanMode') {
        const output: PlanOutput = result.result !== null && typeof result.result === 'object' ? (result.result as PlanOutput) : {}
        if (output.isAgent !== true && output.awaitingLeaderApproval !== true) {
          const snap = await live(io)
          const settings = await getSettings(io)
          const file = typeof output.filePath === 'string' && output.filePath !== '' ? output.filePath : null
          const tip = planApproved(snap, settings, { chars: typeof output.plan === 'string' ? output.plan.length : 0, file }, await $.clock.now())
          if (tip !== null) {
            $.ui.status(tip)
            planStatusSet(() => {
              $.ui.status(undefined)
            })
            await touch(io)
          }
        }
      }
    } catch (error) {
      debug($, 'tool.call', error)
    }
    return result
  }).catch(($, e, next) => next(e))

  onTurnEnd(async end => {
    turnEnded(end.snap, end.settings, { answer: end.answer, reason: end.reason })
  })
  // A summary shrinks the context the plan tip was about.
  onCompaction(async () => {
    clearPlanStatus()
  })
}
