import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, PaneOpenArgs, Register, RenderElement, RenderInput } from 'claude-code'

import { COPY, GLOSSARY } from './generated/copy.js'
import { emptySnap } from './live'
import { drawView } from './view'
import type { View } from './view'

// The Glass pane: one pane, a bar of pages, and a footer every page shares. A
// page is a file that calls addPage() from its own register(); the pane draws
// whichever is selected. The selected page and the detailed view persist in $.store.

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const snapAtom = atom({ plugin: 'claudeglass', key: 'snap' } as const, emptySnap())
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)
const detailedAtom = atom({ plugin: 'claudeglass', key: 'detailed' } as const, false)
const pageAtom = atom({ plugin: 'claudeglass', key: 'page' } as const, 'now')
const explainAtom = atom({ plugin: 'claudeglass', key: 'explain' } as const, null)
const helpAtom = atom({ plugin: 'claudeglass', key: 'help' } as const, false)
const copiedAtom = atom({ plugin: 'claudeglass', key: 'copied' } as const, false)

export const PANE_ID = 'glass'

export type PaneInput = RenderInput<'Pane'>

export type PageState = { explain: string | null; help: boolean; copied: boolean }

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
  copy: (text: string, surface: PaneInput['surface']) => void
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

/** Shows `copied` for a few seconds after a copy. */
async function copyText($: EngineInterface, text: string, surface: PaneInput['surface']): Promise<void> {
  const done = await $.ui.copy({ text, surface })
  if (!done.isCopied) return
  await update($, copiedAtom, () => true)
  $.clock.after(3000, () => {
    void update($, copiedAtom, () => false)
  })
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
    const state: PageState = { explain: await read($, explainAtom), help: await read($, helpAtom), copied: await read($, copiedAtom) }
    const page = pages.find(one => one.id === selected) ?? pages[0]
    const table = $.ui.resolve(e)
    const { Box, Text, Button, Link } = table

    const context: PageContext = {
      e,
      table,
      view,
      state,
      explain: key => void update($, explainAtom, was => (was === key ? null : key)),
      copy: (text, surface) => void copyText($, text, surface),
    }
    const body = page === undefined ? null : state.help ? null : await page.render(context)
    const link = dashboardLink(view)

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={1}>
          <Text bold>{COPY['pane.title']}</Text>
          {pages.map(one => (
            <Button key={`page:${one.id}`} label={COPY[one.labelKey]} hotkey={one.hotkey} onPress={() => selectPage($, one.id)} />
          ))}
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
        <Box flexDirection="row" gap={1}>
          <Button key="more" label={view.detailed ? COPY['pane.less'] : COPY['pane.more']} hotkey="m" onPress={() => void toggleDetailed($)} />
          {link !== null ? <Link key="dashboard" href={linkTarget(link)} label={COPY['pane.dashboard']} /> : null}
          {link !== null ? (
            <Button key="link" label={COPY['pane.link']} hotkey="l" onPress={press => void copyText($, link, press.surface)} />
          ) : null}
          <Button key="help" label={COPY['pane.help']} hotkey="h" onPress={() => void update($, helpAtom, shown => !shown)} />
        </Box>
      </Box>
    )
  })
}
