import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, PaneOpenArgs, Register, RenderElement, RenderInput } from 'claude-code'

import type { GlassPin, GlassReview } from '../types'
import { emptyReview, sound } from './compaction-memory'
import { COPY, GLOSSARY } from './generated/copy.js'
import { emptySnap } from './live'
import { drawView } from './view'
import type { View } from './view'

// The Glass pane: one pane, a bar of pages, and a footer every page shares. A
// page is a file that calls addPage() from its own register(); the pane draws
// whichever is selected. The selected page and the detailed view persist in $.store.
// Hotkeys go on the page bar and on each page's one main action; every other
// control is a quiet Button the focus ring still reaches, and rows are text.

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
  explain: string | null
  help: boolean
  /** The key of the copy button pressed a moment ago, so only that one says it copied. */
  copied: string | null
}

/**
 * What a page can do beyond drawing, spelled out here because Claude Code follows
 * `$` only within the file that holds it. The compaction page uses these.
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
  /** Opens or closes the explanation under a row. */
  explain: (key: string) => void
  /** Copies `text` to the surface the press came from. */
  copy: (text: string, surface: PaneInput['surface'], key: string) => void
  /** The pins, and the compaction review as the page was drawn. */
  pins: GlassPin[]
  review: GlassReview
  /** The opt-ins: the compaction review's two, and the guard before a costly send (draft.ts). */
  options: { hold: boolean; readd: boolean; guard: boolean }
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
  try {
    await $.store.set('detailed', next)
  } catch {
    // The choice still holds for this session.
  }
  return next
}

async function selectPage($: EngineInterface, id: string): Promise<void> {
  await update($, pageAtom, () => id)
  await update($, explainAtom, () => null)
  await update($, helpAtom, () => false)
  try {
    await $.store.set('page', id)
  } catch {
    // The page still holds for this session.
  }
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
        try {
          const usage = await $.session.usage({ breakdown: 'summary' })
          return (usage.context.breakdown?.categories ?? []).map(one => ({ name: one.name, tokens: one.tokens, kind: one.kind, isDeferred: one.isDeferred }))
        } catch {
          return []
        }
      },
      mcpNames: async () => {
        try {
          const usage = await $.session.usage({ breakdown: 'summary' })
          const names: Record<string, string> = {}
          for (const tool of usage.context.breakdown?.mcpTools ?? []) {
            const wire = /^mcp__(.+?)__/.exec(tool.name)?.[1]
            if (wire !== undefined && tool.serverName !== '') names[wire] = tool.serverName
          }
          return names
        } catch {
          return {}
        }
      },
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
      options: { hold: (await $.store.get('hold')) === true, readd: (await $.store.get('readd')) === true, guard: (await $.store.get('guard')) === true },
      kit,
      e,
      table,
      view,
      state,
      explain: key => void update($, explainAtom, was => (was === key ? null : key)),
      copy: (text, surface, key) => void copyText($, text, surface, key),
    }
    const body = page === undefined ? null : state.help ? null : await page.render(context)
    const link = dashboardLink(view)

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
        {/* Quiet controls every page shares: no hotkeys, which belong to the pages and each page's one main action. */}
        <Box flexDirection="row" gap={2} alignItems="center">
          <Button key="more" label={view.detailed ? COPY['pane.less'] : COPY['pane.more']} dimColor onPress={() => void toggleDetailed($)} />
          {link !== null ? <Link key="dashboard" href={linkTarget(link)} label={COPY['pane.dashboard']} /> : null}
          {link !== null ? (
            <Button key="link" label={state.copied === 'link' ? COPY['pane.copied'] : COPY['pane.link']} dimColor onPress={press => void copyText($, link, press.surface, 'link')} />
          ) : null}
          <Button key="help" label={state.help ? COPY['pane.help.hide'] : COPY['pane.help']} dimColor onPress={() => void update($, helpAtom, shown => !shown)} />
        </Box>
      </Box>
    )
  })
}
