import { atom, read } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import { emptySnap, soundSnap } from './live'
import { addAttachment, compacted, follow, isAttached, mcpServer, newLedger, receivedClaudeMd, record } from './lib/instructions.js'
import type { Ledger } from './lib/instructions.js'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())

// What this session loaded into Claude's context, in the order it loaded. Every
// hook only looks: it asks `next(e)` and returns that answer unchanged, and it
// adds no section, tool or attachment of its own.
//
//   prompt.compose      the system prompt's sections (the first render of the session)
//   prompt.context      the first message's context blocks, and the files behind
//                       CLAUDE.md (the user's, the project's, rules, imports)
//   tool.describe       each tool's description: built in, deferred, or an MCP server's
//   skill.prompt        a skill's prompt when it ran
//   prompt.attachment   what Claude Code injects on its own: the skill list, reminders,
//                       a file that loads when Claude reaches matching paths; told
//                       apart by who wrote the text (the engine, or a settings hook
//                       or plugin) and by the type (a file added to a message)
//   session.compact     the main session's summary: what loaded after the start is
//                       dropped, as Claude Code counts it again from the summary
//   classic.InstructionsLoaded
//                       names a path-scoped file and why it loaded
//   turn.complete       (an agent's) whether its context opened with CLAUDE.md
//
// Names and sizes stay in this module's memory, for the Instructions page: never
// in $.state, in a file or in the sensor. A hot reload empties them, and the page
// says nothing has loaded until the next thing does. Sizes are characters of
// description or text (the schema of a tool is not in the event), so the page turns
// them into tokens at the calibrated rate and says they are estimates.

/** The ledger the Instructions page reads. */
export const ledger: Ledger = newLedger()

/** A redraw is asked for at most this often, however many tools describe themselves at once. */
const REDRAW_MS = 400

let redrawing: { cancel: () => void } | null = null

/** Forgets everything (a new session, or a test). */
export function forgetInstructions(): void {
  redrawing?.cancel()
  redrawing = null
  follow(ledger, null)
}

function changed($: EngineInterface): void {
  if (redrawing !== null) return
  redrawing = $.clock.after(REDRAW_MS, () => {
    redrawing = null
    void $.ui.invalidate('ui.render')
  })
}

/** Follows the session id (a /clear starts the ledger over) and says which reply is under way. */
async function reply($: EngineInterface): Promise<number> {
  const id = await $.session.id()
  follow(ledger, id)
  const snap = soundSnap(await read($, snapAtom))
  return snap.sessionId === id ? snap.replies + 1 : 1
}

function forward(path: string): string {
  return path.replace(/\\/g, '/')
}

/** Where a file the context carries came from, in the page's words. */
function sourceOf(kind: string, path: string, parent: string | undefined): string {
  if (parent !== undefined) return 'imported'
  if (/\/rules\//.test(forward(path))) return 'rules'
  return ['managed', 'user', 'project', 'local', 'memory'].includes(kind) ? kind : 'project'
}

const SCOPED_REASONS = ['nested_traversal', 'path_glob_match']

function sameFile(a: string, b: string): boolean {
  return forward(a).toLowerCase() === forward(b).toLowerCase()
}

export const register: Register = on => {
  on('session.start', { cwd: /./ }, async ($, e, next) => {
    forgetInstructions()
    return next(e)
  })

  on('prompt.compose', async ($, e, next) => {
    const result = await next(e)
    try {
      if (e.traits.includes('analysis') || e.traits.includes('teammate')) return result
      const now = await reply($)
      // The first render is the main session's; agents compose their own prompts later.
      if (ledger.composed) return result
      ledger.composed = true
      for (const section of result.sections) {
        record(ledger, { id: `sys:${section.id}`, kind: 'system', name: section.id, chars: section.text.length, reply: now, from: 'engine', startup: true })
      }
      changed($)
    } catch (error) {
      $.ui.log(`claudeglass: instructions ${String(error)}`, { to: 'debug' })
    }
    return result
  })

  on('prompt.context', async ($, e, next) => {
    const result = await next(e)
    try {
      const now = await reply($)
      const files = result.instructionFiles ?? []
      for (const file of files) {
        record(ledger, {
          id: `file:${forward(file.path).toLowerCase()}`,
          kind: 'instructions',
          name: file.path,
          path: file.path,
          chars: file.content.length,
          reply: now,
          from: sourceOf(file.kind, file.path, file.parent),
          startup: true,
          measured: true,
          ...(file.parent !== undefined ? { parent: file.parent } : {}),
        })
      }
      for (const block of result.blocks) {
        if (block.name === 'claudeMd') {
          // The files are not known when a hook rewrote the text: count the block as it is.
          if (files.length === 0 && block.text !== '') {
            record(ledger, { id: 'file:claudemd-block', kind: 'instructions', name: 'CLAUDE.md', chars: block.text.length, reply: now, from: 'project', startup: true, measured: true })
          }
          continue
        }
        record(ledger, { id: `ctx:${block.name}`, kind: 'context', name: block.name, chars: block.text.length, reply: now, from: 'engine', startup: true })
      }
      changed($)
    } catch (error) {
      $.ui.log(`claudeglass: instructions ${String(error)}`, { to: 'debug' })
    }
    return result
  })

  on('tool.describe', async ($, e, next) => {
    const result = await next(e)
    try {
      const now = await reply($)
      const server = mcpServer(e.tool, e.provider.plugin)
      const deferred = (result.isDeferred ?? e.isDeferred) === true
      record(ledger, {
        id: `tool:${e.tool}`,
        kind: server !== '' ? 'mcp' : deferred ? 'deferred' : 'tools',
        name: e.tool,
        chars: result.description.length,
        reply: now,
        from: server !== '' ? 'mcp' : e.provider.plugin === 'engine' ? 'engine' : 'plugin',
        startup: true,
        deferred,
        ...(server !== '' ? { server } : {}),
      })
      changed($)
    } catch (error) {
      $.ui.log(`claudeglass: instructions ${String(error)}`, { to: 'debug' })
    }
    return result
  })

  on('skill.prompt', async ($, e, next) => {
    const result = await next(e)
    try {
      const now = await reply($)
      record(ledger, { id: `skill:${e.skill}`, kind: 'skills', name: e.skill, chars: result.text.length, reply: now, from: 'skill', startup: false })
      changed($)
    } catch (error) {
      $.ui.log(`claudeglass: instructions ${String(error)}`, { to: 'debug' })
    }
    return result
  })

  on('prompt.attachment', async ($, e, next) => {
    const result = await next(e)
    try {
      // An agent's own attachments are its context, not this session's.
      if (e.agentId !== undefined || result.text === null) return result
      const now = await reply($)
      const type = String(e.type)
      const chars = result.text.length
      if (type === 'nested_memory') {
        // The file's name comes from classic.InstructionsLoaded; the text here is what Claude read.
        const text = result.text
        const waiting = ledger.items.find(one => one.scoped === true && one.measured !== true && one.path !== undefined && forward(text).toLowerCase().includes(forward(one.path).toLowerCase()))
        if (waiting !== undefined) {
          waiting.chars = chars
          waiting.measured = true
        } else {
          const lone = ledger.items.find(one => one.scoped === true && one.measured !== true)
          if (lone !== undefined) {
            lone.chars = chars
            lone.measured = true
          } else {
            record(ledger, { id: `scoped:${ledger.seq + 1}`, kind: 'instructions', name: 'CLAUDE.md', chars, reply: now, from: 'scoped', startup: false, scoped: true, measured: true })
          }
        }
      } else if (type === 'skill_listing') {
        record(ledger, { id: 'att:skill_listing', kind: 'skills', name: 'skill listing', chars, reply: now, from: 'skill', startup: now <= 1 })
      } else {
        addAttachment(ledger, type, chars, now, isAttached(type, e.origin?.kind))
      }
      changed($)
    } catch (error) {
      $.ui.log(`claudeglass: instructions ${String(error)}`, { to: 'debug' })
    }
    return result
  })

  // The main conversation is summarised: the files, skills and reminders that loaded after the start are
  // no longer what the context holds. A subagent's own summary is not this session's, and a precompute
  // installs nothing.
  on('session.compact', { trigger: /^(manual|auto|plugin)$/ }, async ($, e, next) => {
    const result = await next(e)
    try {
      if (e.agentId === undefined && result.skip === undefined && compacted(ledger)) changed($)
    } catch (error) {
      $.ui.log(`claudeglass: instructions ${String(error)}`, { to: 'debug' })
    }
    return result
  }).catch(($, e, next) => next(e))

  // A file that loads when Claude reaches matching paths. The files read at the start
  // arrive with prompt.context; this names the ones that come later.
  on('classic.InstructionsLoaded', async ($, e, next) => {
    try {
      if (SCOPED_REASONS.includes(e.load_reason)) {
        const now = await reply($)
        const known = ledger.items.find(one => one.path !== undefined && sameFile(one.path, e.file_path))
        if (known === undefined) {
          let chars = 0
          try {
            const text = await $.fs.read(e.file_path)
            if (typeof text === 'string') chars = text.length
          } catch {
            // The size comes from what Claude read, if that arrives.
          }
          // A nested_memory attachment that arrived first has no name yet: this gives it one.
          const nameless = ledger.items.find(one => one.scoped === true && one.path === undefined && one.measured === true)
          if (nameless !== undefined) {
            nameless.path = e.file_path
            nameless.name = e.file_path
            nameless.id = `file:${forward(e.file_path).toLowerCase()}`
            if (e.globs !== undefined) nameless.globs = e.globs
          } else {
            record(ledger, {
              id: `file:${forward(e.file_path).toLowerCase()}`,
              kind: 'instructions',
              name: e.file_path,
              path: e.file_path,
              chars,
              reply: now,
              from: 'scoped',
              startup: false,
              scoped: true,
              ...(e.globs !== undefined ? { globs: e.globs } : {}),
              ...(e.parent_file_path !== undefined ? { parent: e.parent_file_path } : {}),
            })
          }
          changed($)
        }
      }
    } catch (error) {
      $.ui.log(`claudeglass: instructions ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // An agent's run ends: did its context open with a CLAUDE.md block?
  on('turn.complete', { agentId: /./ }, async ($, e, next) => {
    try {
      const id = e.agentId
      if (id !== undefined) {
        await reply($)
        if (!Object.prototype.hasOwnProperty.call(ledger.agents, id)) {
          const got = receivedClaudeMd(await $.session.messages({ as: 'api', agentId: id }))
          if (got !== null) {
            ledger.agents[id] = got
            changed($)
          }
        }
      }
    } catch (error) {
      $.ui.log(`claudeglass: instructions ${String(error)}`, { to: 'debug' })
    }
    return next(e)
  })
}
