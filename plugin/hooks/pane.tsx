import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, PaneOpenArgs, Register, RenderElement, RenderInput } from 'claude-code'

import type { GlassPin, GlassReview } from '../types'
import { emptyReview, sound } from './compaction-memory'
import { COPY, GLOSSARY } from './generated/copy.js'
import { emptySnap } from './live'
import { heading } from './rows'
import { drawView } from './view'
import type { View } from './view'

// The Glass pane: one pane, a bar of pages, and a footer every page shares. A
// page is a file that calls addPage() from its own register(); the pane draws
// whichever is selected. The selected page and the detailed view persist in $.store.
// Hotkeys go on the page bar and on each page's one main action; every other
// control is a quiet Button the focus ring still reaches, and rows are text.
// The footer's dashboard link is the page's own (its `dashboard`), and Detailed's
// Settings button opens the two on/off options under whichever page is open:
// no page holds an option of its own.

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)
const pageAtom = atom({ plugin: 'claudeglass', key: 'page' } as const, 'now')
const explainAtom = atom({ plugin: 'claudeglass', key: 'explain' } as const, null)
const helpAtom = atom({ plugin: 'claudeglass', key: 'help' } as const, false)
const copiedAtom = atom({ plugin: 'claudeglass', key: 'copied' } as const, null as string | null)
const pinsAtom = atom({ plugin: 'claudeglass', key: 'pins' } as const, [] as GlassPin[])
const reviewAtom = atom({ plugin: 'claudeglass', key: 'review' } as const, emptyReview())

export const PANE_ID = 'glass'

export type PaneInput = RenderInput<'Pane'>

export type PageState = {
  /** What the pane has open under the page: "settings" while the Settings view is shown. */
  explain: string | null
  help: boolean
  /** The key of the copy button pressed a moment ago, so only that one says it copied. */
  copied: string | null
}

/**
 * What a page can do beyond drawing, spelled out here because Claude Code follows
 * `$` only within the file that holds it. The compaction page uses these; the pane's
 * Settings view uses `setOption`.
 */
export type PageKit = {
  /** Puts `text` in the prompt box as the person's draft; false when the box can't take it. */
  fill: (text: string) => Promise<boolean>
  /** Changes the pins. */
  updatePins: (change: (pins: GlassPin[]) => GlassPin[]) => Promise<void>
  /** Changes the compaction review's state. */
  updateReview: (change: (review: GlassReview) => GlassReview) => Promise<void>
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
 * holds it, so a page gets the element table and the two actions it needs.
 */
export type PageContext = {
  e: PaneInput
  table: Elements[keyof Elements]
  view: View
  state: PageState
  /** Copies `text` to the surface the press came from. */
  copy: (text: string, surface: PaneInput['surface'], key: string) => void
  /** The pins, and the compaction review as the page was drawn. */
  pins: GlassPin[]
  review: GlassReview
  kit: PageKit
}

export type CopyKey = keyof typeof COPY

export type GlassPage = {
  /** Names the page in $.store and the page bar's keys. */
  id: string
  /** The page bar's key: one lower-case letter or digit. */
  hotkey: string
  /** The COPY entry that names the page in the page bar. */
  labelKey: CopyKey
  /** The page's body; the pane adds the page bar and the footer around it. */
  render: (context: PageContext) => RenderElement | Promise<RenderElement>
  /**
   * Where the dashboard shows what this page is about (the footer's "Open in dashboard" and "Copy link" go
   * there), or null when its address isn't known. A page that leaves it out gets the session.
   */
  dashboard?: (view: View) => string | null
}

const pages: GlassPage[] = []

/** Adds a page to the pane's page bar, after the ones already there. A page added twice replaces itself. */
export function addPage(page: GlassPage): void {
  const at = pages.findIndex(one => one.id === page.id)
  if (at >= 0) pages[at] = page
  else pages.push(page)
}

export function pageList(): readonly GlassPage[] {
  return pages
}

/** What opens the pane: the band's button and /glass both open it with these. */
export const OPEN_ARGS: PaneOpenArgs = { id: PANE_ID, title: COPY['pane.title'], closeOnEscape: true, focus: true }

/** Flips the detailed view and keeps the choice. */
async function toggleDetailed($: EngineInterface): Promise<boolean> {
  const next = !(await read($, detailedAtom))
  await update($, detailedAtom, () => next)
  // The Settings view belongs to Detailed: leaving Detailed puts it away, so it doesn't come back unasked.
  if (!next) await update($, explainAtom, () => null)
  try {
    await $.store.set('detailed', next)
  } catch {
    // The choice still holds for this session.
  }
  return next
}

async function selectPage($: EngineInterface, id: string): Promise<void> {
  await update($, pageAtom, () => id)
  await update($, helpAtom, () => false)
  try {
    await $.store.set('page', id)
  } catch {
    // The page still holds for this session.
  }
}

/** Opens the Settings view under the page, or closes it; opening it puts the meanings away. */
async function toggleSettings($: EngineInterface): Promise<void> {
  const open = (await read($, explainAtom)) === 'settings'
  await update($, explainAtom, () => (open ? null : 'settings'))
  if (!open) await update($, helpAtom, () => false)
}

/** Opens the meanings, or puts them away; opening them puts the Settings view away, so it isn't waiting when they close. */
async function toggleHelp($: EngineInterface): Promise<void> {
  const open = await read($, helpAtom)
  await update($, helpAtom, () => !open)
  if (!open) await update($, explainAtom, () => null)
}

/** Marks the copy button `key` as copied for a few seconds after a copy. */
async function copyText($: EngineInterface, text: string, surface: PaneInput['surface'], key: string): Promise<void> {
  const done = await $.ui.copy({ text, surface })
  if (!done.isCopied) return
  await update($, copiedAtom, () => key)
  $.clock.after(3000, () => {
    // A later copy owns the mark now.
    void update($, copiedAtom, was => (was === key ? null : was))
  })
}

/** The copied mark as kept in state; an older version kept a boolean, which marks no button. */
function copiedKey(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

/** Claude Code's own breakdown of the context (the summary: local and free); undefined when it can't give one. */
async function breakdownOf($: EngineInterface) {
  try {
    return (await $.session.usage({ breakdown: 'summary' })).context.breakdown
  } catch {
    return undefined
  }
}

/** Where the dashboard shows this session, or null when the dashboard's address isn't known. */
export function dashboardLink(view: View): string | null {
  const base = view.settings.dashboardUrl
  const id = view.snap.sessionId
  if (base === null || id === null) return null
  return `${base}/#/spend/sessions?session=${encodeURIComponent(id)}`
}

/** The desktop opens http links only for localhost, so the dashboard's loopback address is written that way for the Link. */
function linkTarget(link: string): string {
  return link.replace(/^http:\/\/127\.0\.0\.1(?=[:/])/, 'http://localhost')
}

export const register: Register = on => {
  on('ui.render', { component: 'Pane', requestId: PANE_ID }, async ($, e) => {
    const view = drawView(await read($, snapAtom), await read($, settingsAtom), await read($, detailedAtom), await $.clock.now(), false)
    const selected = await read($, pageAtom)
    const state: PageState = { explain: await read($, explainAtom), help: await read($, helpAtom), copied: copiedKey(await read($, copiedAtom)) }
    const page = pages.find(one => one.id === selected) ?? pages[0]
    const table = $.ui.resolve(e)
    const { Box, Text, Button, Link } = table

    // Claude Code's breakdown of the context, asked for once in a draw however many of the kit's figures a page reads; none when it can't say.
    let asked: ReturnType<typeof breakdownOf> | undefined
    const summary = () => {
      if (asked === undefined) asked = breakdownOf($)
      return asked
    }

    const kit: PageKit = {
      fill: async text => {
        try {
          return (await $.prompt.fill({ text, mode: 'replace' })).isFilled
        } catch {
          return false
        }
      },
      updatePins: async change => {
        await update($, pinsAtom, was => change(was))
      },
      updateReview: async change => {
        await update($, reviewAtom, was => change(sound(was)))
      },
      setOption: async (key, enabled) => {
        await $.store.set(key, enabled)
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
    const context: PageContext = {
      pins: await read($, pinsAtom),
      review: sound(await read($, reviewAtom)),
      kit,
      e,
      table,
      view,
      state,
      copy: (text, surface, key) => void copyText($, text, surface, key),
    }
    const body = page === undefined ? null : state.help ? null : await page.render(context)
    const link = (page?.dashboard ?? dashboardLink)(view)
    // The two on/off options, in one place: each a toggle with its one-line detail directly under it.
    const settingsOpen = view.detailed && !state.help && state.explain === 'settings'
    const stored = { guard: (await $.store.get('guard')) === true, hold: (await $.store.get('hold')) === true }
    const options = [
      { id: 'guard', label: COPY['guard.opt'], detail: COPY['guard.opt.detail'], on: stored.guard, words: [COPY['guard.opt.on'], COPY['guard.opt.off']] },
      { id: 'hold', label: COPY['compaction.opt.hold'], detail: COPY['compaction.opt.hold.detail'], on: stored.hold, words: [COPY['compaction.on'], COPY['compaction.off']] },
    ]

    return (
      <Box flexDirection="column" gap={1}>
        {/* The pane's own title already says Glass. The page shown is marked as text; the others are its keys. */}
        <Box flexDirection="row" gap={2} alignItems="center">
          {pages.map(one =>
            one.id === page?.id ? (
              <Text key={`page:${one.id}`} bold underline>
                {COPY[one.labelKey]}
              </Text>
            ) : (
              <Button key={`page:${one.id}`} label={COPY[one.labelKey]} hotkey={one.hotkey} dimColor onPress={() => selectPage($, one.id)} />
            ),
          )}
        </Box>
        {state.help ? (
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
        {settingsOpen ? (
          <Box key="settings" flexDirection="column">
            {heading(table, 'settings-title', COPY['pane.settings'])}
            {options.map(one => (
              <Box key={one.id} flexDirection="column">
                <Button key={`opt:${one.id}`} label={`${one.label}: ${one.on ? one.words[0] : one.words[1]}`} dimColor onPress={() => void kit.setOption(one.id, !one.on)} />
                <Text dimColor>{one.detail}</Text>
              </Box>
            ))}
          </Box>
        ) : null}
        {/* Quiet controls every page shares: no hotkeys, which belong to the pages and each page's one main action. */}
        <Box flexDirection="row" gap={2} alignItems="center">
          <Button key="more" label={view.detailed ? COPY['pane.less'] : COPY['pane.more']} dimColor onPress={() => void toggleDetailed($)} />
          {link !== null ? <Link key="dashboard" href={linkTarget(link)} label={COPY['pane.dashboard']} /> : null}
          {link !== null ? (
            <Button key="link" label={state.copied === 'link' ? COPY['pane.copied'] : COPY['pane.link']} dimColor onPress={press => void copyText($, link, press.surface, 'link')} />
          ) : null}
          {view.detailed ? <Button key="settings" label={settingsOpen ? COPY['pane.settings.hide'] : COPY['pane.settings']} dimColor onPress={() => void toggleSettings($)} /> : null}
          <Button key="help" label={state.help ? COPY['pane.help.hide'] : COPY['pane.help']} dimColor onPress={() => void toggleHelp($)} />
        </Box>
      </Box>
    )
  })
}
