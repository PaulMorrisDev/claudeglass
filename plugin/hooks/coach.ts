import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import { workflowLaunched } from './agents'
import type { Io } from './io'
import {
  clearPlanStatus,
  editBox,
  editsFile,
  goAheadPlan,
  leftPlanMode,
  markEdited,
  modeSent,
  noteBackground,
  planApproved,
  planRefused,
  planSettled,
  planStatusSet,
  refusedPlan,
  startedInBackground,
  submitted,
  turnEnded,
} from './coach-mem'
import type { PlanTip } from './coach-mem'
import { emptySnap, live, touch } from './live'
import { getSettings, setProject } from './settings'
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
//   session.start  the project the session runs in (coaching covers the projects the user's config leaves in)
//   prompt.edit    the draft's shape (length, and the words while it is short)
//   prompt.submit  a message sent: the run of small changes, the first message's size;
//                  a go-ahead typed after a plan was refused approves that plan, and so does a message sent
//                  after plan mode was left (classic.UserPromptSubmit carries the mode)
//   tool.call      a file changed; work sent to the background; a plan approved or refused;
//                  a workflow started in the background
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

/**
 * Learns the folder the session runs in and the project name Claude Code gave it, for the project filter.
 * Asked once when not known (a reload mid-session has no session.start) and again at each message, as a
 * /cd can move it. A session that can't say gets no filter.
 */
let looked = false
async function rememberProject($: EngineInterface, again = false): Promise<void> {
  if (looked && !again) return
  looked = true
  try {
    const root = await $.session.root()
    let name: string | null = null
    try {
      name = (await $.env.get('CLAUDE_CODE_PROJECT_DIR_NAME')) ?? null
    } catch {
      // No name: the folder gives it.
    }
    setProject(typeof root === 'string' && root !== '' ? root : null, name)
  } catch (error) {
    debug($, 'session.root', error)
  }
}

/** Puts the fresh-start tip for `plan` on the status line, if it applies. */
async function planTip($: EngineInterface, io: Io, plan: PlanTip): Promise<void> {
  const snap = await live(io)
  const settings = await getSettings(io)
  const tip = planApproved(snap, settings, plan, await $.clock.now())
  if (tip === null) return
  $.ui.status(tip)
  planStatusSet(() => {
    $.ui.status(undefined)
  })
  await touch(io)
}

type PlanOutput = { plan?: string | null; filePath?: string; isAgent?: boolean; awaitingLeaderApproval?: boolean }

export const register: Register = on => {
  // The project, for the filter coaching goes through. (A matcher, apart from track.ts's hook on the event.)
  on('session.start', { cwd: /./ }, async ($, e, next) => {
    await rememberProject($, true)
    return next(e)
  }).catch(($, e, next) => next(e))

  // Nothing to do per key but to look at the box; a redraw is asked for only when a hint could change.
  on('prompt.edit', async ($, e, next) => {
    const after = await next(e)
    try {
      await rememberProject($)
      if (editBox(e, after)) void $.ui.invalidate('ui.render')
    } catch (error) {
      debug($, 'prompt.edit', error)
    }
    return after
  }).catch(($, e, next) => next(e))

  on('prompt.submit', async ($, e, next) => {
    let refused: PlanTip | null = null
    let approved: PlanTip | null = null
    try {
      const io = kit($)
      await rememberProject($, true)
      // Which kinds of origin arrive is worth knowing (what counts as typed is coach-mem.ts's isTypedOrigin). Not a fault, so not worded as one.
      $.ui.log(`claudeglass prompt origin ${e.origin.kind}`, { to: 'debug' })
      // A go-ahead typed after a plan was refused approves it: the tip is asked before the message is counted.
      modeSent(null)
      refused = refusedPlan()
      approved = goAheadPlan(e.text, e.origin.kind)
      const snap = await live(io)
      const settings = await getSettings(io)
      submitted(snap, settings, { text: e.text, origin: e.origin.kind, queued: e.turnId !== undefined }, await $.clock.now())
      await touch(io)
      if (approved !== null) await planTip($, io, approved)
    } catch (error) {
      debug($, 'prompt.submit', error)
    }
    const result = await next(e)
    // The settings hooks ran beneath (classic.UserPromptSubmit below says the mode): a plan sent back and then a message
    // sent outside plan mode (plan mode left) is approved too, whatever the message says.
    try {
      if (approved === null && refused !== null && result.drop === undefined && leftPlanMode()) {
        planSettled()
        await planTip($, kit($), refused)
      }
    } catch (error) {
      debug($, 'plan mode', error)
    }
    return result
  }).catch(($, e, next) => next(e))

  // The permission mode a message is sent under. (The only hook of the event.)
  on('classic.UserPromptSubmit', async ($, e, next) => {
    modeSent(typeof e.permission_mode === 'string' ? e.permission_mode : null)
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
      await rememberProject($)
      const output: PlanOutput = result.result !== null && typeof result.result === 'object' ? (result.result as PlanOutput) : {}
      if (!failed && name !== 'ExitPlanMode') noteBackground(result.text, e.tool_use_id, await $.clock.now())
      if (!failed && name === 'Workflow' && startedInBackground(args, result.result, result.text)) {
        // Its reports come back as messages of their own, and each is answered by a reply that reads the whole session.
        const snap = await live(io)
        const settings = await getSettings(io)
        if (await workflowLaunched(io, snap, settings, true, await $.clock.now())) await touch(io)
      }
      if (name === 'ExitPlanMode' && output.isAgent !== true && output.awaitingLeaderApproval !== true) {
        const file = typeof output.filePath === 'string' && output.filePath !== '' ? output.filePath : null
        const plan = { chars: typeof output.plan === 'string' ? output.plan.length : 0, file }
        if (failed) {
          // The plan was sent back: a go-ahead typed next approves it.
          planRefused(plan)
        } else {
          planSettled()
          await planTip($, io, plan)
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
