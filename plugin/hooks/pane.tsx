import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, PaneOpenArgs, Register, RenderElement, RenderInput } from 'claude-code'

import type { GlassPin, GlassReview } from '../types'
import { emptyReview, endSummarise, memory, noteReadd, noteSummarise, noteTweak, readdMissing, sound, tickedItems } from './compaction-memory'
import { COPY, GLOSSARY } from './generated/copy.js'
import { fill } from './lib/text.js'
import { emptySnap } from './live'
import { dashboardAddress, render as checkpoint } from './page-checkpoint'
import { contextLink, render as startup } from './page-instructions'
import { drawView } from './view'
import type { View } from './view'

// The Glass panes: Checkpoint (what the next summary must keep, and what the last one lost) and Startup (what
// loads before you type). Each is its own pane id, so with both open Claude Code shows them as native tabs; there
// is no page bar. A pane is a file with a render(context) that this file draws and wraps in its footer. Hotkeys
// belong to a page's one main action; every other control is a quiet Button the focus ring still reaches.
// The footer's dashboard link is the page's own address (page-checkpoint.tsx, page-instructions.tsx).

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)
const explainAtom = atom({ plugin: 'claudeglass', key: 'explain' } as const, null)
const pinsAtom = atom({ plugin: 'claudeglass', key: 'pins' } as const, [] as GlassPin[])
const reviewAtom = atom({ plugin: 'claudeglass', key: 'review' } as const, emptyReview())

export const PANE_ID = 'glass'
/** A Summarise press is on its way (the press that began it has not finished): a second press before the pane redraws does nothing. */
let summarising = false
export const STARTUP_ID = 'glass-startup'

export type PaneInput = RenderInput<'Pane'>

/**
 * What a page can do beyond drawing, spelled out here because Claude Code follows
 * `$` only within the file that holds it.
 */
export type PageKit = {
  /** Changes the pins. */
  updatePins: (change: (pins: GlassPin[]) => GlassPin[]) => Promise<void>
  /** Changes the compaction review's state. */
  updateReview: (change: (review: GlassReview) => GlassReview) => Promise<void>
  /**
   * Puts back the things the last summary lost (those it marked missing or partly): Claude is told them in a system
   * prompt section from its next request on (readd.ts). Returns how many it added; 0 when nothing was left to add.
   */
  readd: () => Promise<number>
  /**
   * Starts a summary now, as /compact would. The review's `ui.action` says `summarise` while it runs; a summary
   * that is held, or a reply still running, is said in `ui.note` (`noteFor` `summarise`). A press while one is on its way
   * does nothing. Never throws.
   */
  summarise: () => Promise<void>
  /** Turns an opt-in on or off and keeps the choice. */
  setOption: (key: string, enabled: boolean) => Promise<void>
  /** Asks a model a question: its answer and the tokens it used, or why it could not. */
  ask: (model: string, prompt: string, maxTokens: number) => Promise<{ text: string; input: number; output: number } | { reason: string }>
  /** The rows of Claude Code's own context breakdown (what /context lists) as of now; empty when it can't say. */
  categories: () => Promise<{ name: string; tokens: number; kind: string; isDeferred: boolean }[]>
  /**
   * Each MCP server's name as /mcp lists it, keyed by the server part of its tools' names
   * (`mcp__<server>__tool`); empty when Claude Code can't say.
   */
  mcpNames: () => Promise<Record<string, string>>
  /** Claude Code's count of each MCP tool as of now (what /context lists); empty when it can't say. */
  mcpTools: () => Promise<{ name: string; serverName: string; tokens: number; isLoaded: boolean }[]>
}

/**
 * What a page draws from. Claude Code follows `$` only within the file that
 * holds it, so a page gets the element table and the actions it needs.
 */
export type PageContext = {
  e: PaneInput
  table: Elements[keyof Elements]
  view: View
  /** The pins, and the compaction review as the page was drawn. */
  pins: GlassPin[]
  review: GlassReview
  kit: PageKit
}

/** What opens the Checkpoint pane: the band's button and /glass both open it with these. */
export const OPEN_ARGS: PaneOpenArgs = { id: PANE_ID, title: COPY['checkpoint.title'], closeOnEscape: true, focus: true }

/** What opens the Startup pane: the Checkpoint footer's button and /glass startup. */
export const STARTUP_OPEN_ARGS: PaneOpenArgs = { id: STARTUP_ID, title: COPY['checkpoint.startup.title'], closeOnEscape: true, focus: true }

/** Opens the meanings of this pane, or puts them away. Each pane has its own, so one does not show in the other. */
async function toggleHelp($: EngineInterface, id: string): Promise<void> {
  const open = (await read($, explainAtom)) === id
  await update($, explainAtom, () => (open ? null : id))
}

/** Claude Code's own breakdown of the context (the summary: local and free); undefined when it can't give one. */
async function breakdownOf($: EngineInterface) {
  try {
    return (await $.session.usage({ breakdown: 'summary' })).context.breakdown
  } catch {
    return undefined
  }
}

/** The desktop opens http links only for localhost, so the dashboard's loopback address is written that way for the Link. */
function linkTarget(link: string): string {
  return link.replace(/^http:\/\/127\.0\.0\.1(?=[:/])/, 'http://localhost')
}

/** What a page does beyond drawing; one set per draw, so Claude Code's breakdown is asked for once however many figures a page reads. */
function buildKit($: EngineInterface): PageKit {
  let asked: ReturnType<typeof breakdownOf> | undefined
  const summary = () => {
    if (asked === undefined) asked = breakdownOf($)
    return asked
  }

  return {
    updatePins: async change => {
      await update($, pinsAtom, was => change(was))
    },
    updateReview: async change => {
      await update($, reviewAtom, was => change(sound(was)))
    },
    readd: async () => {
      const now = await $.clock.now()
      let added = 0
      let ids: string[] = []
      await update($, reviewAtom, was => {
        const review = sound(was)
        const next = readdMissing(review, now)
        added = next.readded.length - review.readded.length
        ids = next.readded.slice(review.readded.length).map(item => item.id)
        return next
      })
      // The sensor counts these as added back when the next reply ends (drainSensor).
      memory.added += added
      noteReadd(ids, now)
      return added
    },
    summarise: async () => {
      if (summarising) return
      summarising = true
      try {
        // Another press (the band's Summarise, a Check) is already working: this one does nothing.
        if (sound(await read($, reviewAtom)).ui.action !== null) return
        const say = (action: 'summarise' | null) => update($, reviewAtom, was => ({ ...sound(was), ui: { ...sound(was).ui, action, note: null, noteFor: null } }))
        await say('summarise')
        // The sensor counts the press, with the items the summary is told to keep; the summary it starts fills in what was kept (compaction.ts).
        const mine = noteSummarise(tickedItems(sound(await read($, reviewAtom)), await read($, pinsAtom)).length, await $.clock.now())
        let note: string | null = null
        try {
          const result = await $.session.compact()
          // A hook held the summary: say why, as the hook gave it.
          if (result.skip !== undefined) {
            note = fill(COPY['checkpoint.summarise.skipped'], { reason: result.skip })
          }
        } catch {
          // Claude Code refuses while a reply is running.
          note = COPY['health.busy.reply']
        } finally {
          // A summary that went through filled in the press's `after` during compact() (compaction.ts); none is coming otherwise.
          endSummarise(mine)
          // A summary that went through already cleared the note when it was recorded (compaction.ts); a held or refused one says why.
          await update($, reviewAtom, was => ({
            ...sound(was),
            ui: { ...sound(was).ui, action: null, note: note ?? sound(was).ui.note, noteFor: note === null ? (sound(was).ui.noteFor ?? null) : ('summarise' as const) },
          }))
        }
      } finally {
        summarising = false
      }
    },
    setOption: async (key, enabled) => {
      await $.store.set(key, enabled)
      // The sensor counts the switch: 1 for off to on, 0 for on to off.
      if (key === 'hold') noteTweak('hold', enabled ? 1 : 0, undefined, await $.clock.now())
      await update($, reviewAtom, was => ({ ...sound(was) }))
    },
    categories: async () => {
      const breakdown = await summary()
      return (breakdown?.categories ?? []).map(one => ({ name: one.name, tokens: one.tokens, kind: one.kind, isDeferred: one.isDeferred }))
    },
    mcpNames: async () => {
      const names: Record<string, string> = {}
      for (const tool of (await summary())?.mcpTools ?? []) {
        const wire = /^mcp__(.+?)__/.exec(tool.name)?.[1]
        if (wire !== undefined && tool.serverName !== '') names[wire] = tool.serverName
      }
      return names
    },
    mcpTools: async () => (await summary())?.mcpTools?.map(one => ({ name: one.name, serverName: one.serverName, tokens: one.tokens, isLoaded: one.isLoaded })) ?? [],
    ask: async (model, prompt, maxTokens) => {
      try {
        const answer = await $.model.complete({ model, prompt, maxTokens, effort: 'low', timeoutMs: 60000 })
        if (!answer.isAnswered) return { reason: answer.reason }
        return { text: answer.text, input: answer.usage.input_tokens, output: answer.usage.output_tokens }
      } catch {
        // No model call in this Claude Code, or it refused the request.
        return { reason: 'unavailable' }
      }
    },
  }
}

/** Draws one pane: the page's body (or the meanings), then the footer. */
async function drawPane($: EngineInterface, e: PaneInput, id: string): Promise<RenderElement> {
  const view = drawView(await read($, snapAtom), await read($, settingsAtom), await read($, detailedAtom), await $.clock.now(), false)
  const help = (await read($, explainAtom)) === id
  const table = $.ui.resolve(e)
  const { Box, Text, Button, Link } = table
  const kit = buildKit($)
  const context: PageContext = { pins: await read($, pinsAtom), review: sound(await read($, reviewAtom)), kit, e, table, view }
  const isStartup = id === STARTUP_ID
  const body = help ? null : await (isStartup ? startup(context) : checkpoint(context))
  const link = isStartup ? contextLink(view.settings) : dashboardAddress(view.settings.dashboardUrl)
  const hold = !isStartup && (await $.store.get('hold')) === true

  return (
    <Box flexDirection="column" gap={1}>
      {help ? (
        <Box flexDirection="column">
          {Object.values(GLOSSARY).map(entry => (
            <Box key={entry.title} flexDirection="column">
              <Text bold>{entry.title}</Text>
              <Text dimColor>{entry.body}</Text>
            </Box>
          ))}
        </Box>
      ) : (
        body
      )}
      {/* Quiet controls the panes share: no hotkeys, which belong to a page's one main action. */}
      {isStartup ? null : (
        <Box key="hold" flexDirection="column">
          <Button key="hold" label={fill(COPY['checkpoint.hold'], { state: COPY[hold ? 'checkpoint.hold.on' : 'checkpoint.hold.off'] })} dimColor onPress={() => void kit.setOption('hold', !hold)} />
          {hold ? <Text dimColor>{COPY['checkpoint.hold.detail']}</Text> : null}
        </Box>
      )}
      <Box flexDirection="row" gap={2} alignItems="center">
        {isStartup ? null : <Button key="startup" label={COPY['checkpoint.startup']} dimColor onPress={() => void $.ui.open(STARTUP_OPEN_ARGS)} />}
        {link !== null ? <Link key="dashboard" href={linkTarget(link)} label={COPY['pane.dashboard']} /> : null}
        <Button key="help" label={help ? COPY['pane.help.hide'] : COPY['pane.help']} dimColor onPress={() => void toggleHelp($, id)} />
      </Box>
    </Box>
  )
}

export const register: Register = on => {
  on('ui.render', { component: 'Pane', requestId: PANE_ID }, async ($, e) => drawPane($, e, PANE_ID))
  on('ui.render', { component: 'Pane', requestId: STARTUP_ID }, async ($, e) => drawPane($, e, STARTUP_ID))
}
