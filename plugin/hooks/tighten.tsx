import { atom, read } from 'claude-code'
import type { PaneOpenArgs, Register } from 'claude-code'

import type { GlassSettings } from '../types'
import { filledDraft } from './draft'
import { amount, formatTokens } from './fmt'
import { COPY } from './generated/copy.js'
import { cleanRewrite, MAX_DRAFT_CHARS, TIGHTEN_SYSTEM, tightenMaxTokens, tightenPrompt, unifiedDiff, words } from './lib/tighten.js'
import { priceUsage } from './lib/pricing.js'
import { fill } from './lib/text.js'
import { fallbackSettings } from './settings'

// Claude Code reads a state reference only where its literal is written, so each file that
// reads one declares it (the contract is types/index.d.ts).
const settingsAtom = atom({ plugin: 'claudeglass', key: 'settings' } as const, null)

// /glass tighten: Haiku rewrites the draft shorter and more specific; a pane of its own shows the
// draft against the rewrite as a diff, with what the call cost, and two buttons. Use this puts the
// rewrite in the prompt box (nothing is sent); Keep mine puts the draft back and closes. It is opt-in
// by being a command: the call is made only when the person types it.
//
// The draft and the rewrite are the person's words: they live here, in module memory, for as long as
// the pane is open, and are never written to $.state, $.store or disk.

export const TIGHTEN_PANE = 'glass-tighten'

/** What opens the pane: focus is asked for, and granted only when the prompt box is empty. */
export const TIGHTEN_OPEN: PaneOpenArgs = { id: TIGHTEN_PANE, title: COPY['tighten.title'], closeOnEscape: true, focus: true }

/** The model asked, by its alias: Claude Code resolves it to the current Haiku and checks it against the person's allowlist. */
const HAIKU = 'haiku'

type Shown = { before: string; after: string; input: number; output: number; note: string | null }

let shown: Shown | null = null

/** The pane's contents (a test). */
export function currentTighten(): Readonly<Shown> | null {
  return shown
}

/** What /glass tighten needs from the engine, spelled out where `$` is held. */
export type TightenKit = {
  /** The prompt box's text now. */
  box: () => Promise<string>
  /** The draft kept from before the command took the box's place, or empty. */
  remembered: () => string
  /** Asks the model: its answer and the tokens it used, or why it could not. */
  complete: (system: string, prompt: string, maxTokens: number) => Promise<{ text: string; input: number; output: number } | { reason: string }>
  /** Opens the pane; false when it could not be placed. */
  open: () => Promise<boolean>
  /** Draws the pane again. */
  redraw: () => void
  debug: (text: string) => void
}

/** The command typed in the box takes the draft's place; it is not a draft. */
function isOwnCommand(text: string): boolean {
  return /^\s*\/glass\b/i.test(text)
}

/**
 * Runs /glass tighten: the line the command answers with. The draft is the box's text, or, when the
 * command has taken the box (or the box is empty), the last draft typed before it.
 */
export async function runTighten(kit: TightenKit): Promise<string> {
  let draft = ''
  try {
    const box = await kit.box()
    draft = box.trim() !== '' && !isOwnCommand(box) ? box : kit.remembered()
  } catch (error) {
    kit.debug(`claudeglass: tighten box ${String(error)}`)
    draft = kit.remembered()
  }
  if (draft.trim() === '') return COPY['tighten.empty']
  if (draft.length > MAX_DRAFT_CHARS) return COPY['tighten.long']
  const answer = await kit.complete(TIGHTEN_SYSTEM, tightenPrompt(draft), tightenMaxTokens(draft))
  if ('reason' in answer) {
    kit.debug(`claudeglass: tighten ${answer.reason}`)
    return answer.reason === 'unavailable' ? COPY['tighten.skip'] : COPY['tighten.failed']
  }
  const rewrite = cleanRewrite(answer.text)
  if (rewrite === '') return COPY['tighten.failed']
  if (words(rewrite) >= words(draft)) return COPY['tighten.same']
  shown = { before: draft, after: rewrite, input: answer.input, output: answer.output, note: null }
  kit.redraw()
  return (await kit.open()) ? COPY['tighten.opened'] : COPY['tighten.failed']
}

/** The newest Haiku in the rates, to price the call at (the answer does not name the model that gave it). */
function haikuRates(models: GlassSettings['models']): GlassSettings['models'][string] | null {
  const ids = Object.keys(models).filter(id => id.toLowerCase().includes('haiku'))
  ids.sort((a, b) => b.localeCompare(a, 'en', { numeric: true }))
  const best = ids[0]
  return best === undefined ? null : (models[best] ?? null)
}

/** What the call cost, in tokens and, when the rates price the model, in the billing mode's own words. */
export function costLine(current: { input: number; output: number }, settings: GlassSettings): string {
  const tokens = formatTokens(current.input + current.output)
  const rates = haikuRates(settings.models)
  if (rates === null) return fill(COPY['tighten.cost'], { tokens })
  return fill(COPY['tighten.cost.amount'], { tokens, amount: amount(priceUsage(rates, { input_tokens: current.input, output_tokens: current.output }, settings.ttl), settings) })
}

export const register: Register = on => {
  on('ui.render', { component: 'Pane', requestId: TIGHTEN_PANE }, async ($, e) => {
    const settings = (await read($, settingsAtom)) ?? fallbackSettings()
    const { Box, Text, Button, Code } = $.ui.resolve(e)
    const current = shown
    if (current === null) return <Text dimColor>{COPY['tighten.empty']}</Text>

    const close = async () => {
      shown = null
      try {
        await $.ui.close({ id: TIGHTEN_PANE })
      } catch (error) {
        $.ui.log(`claudeglass: tighten close ${String(error)}`, { to: 'debug' })
      }
    }
    const use = async () => {
      let filled = false
      try {
        filled = (await $.prompt.fill({ text: current.after, mode: 'replace' })).isFilled
      } catch (error) {
        $.ui.log(`claudeglass: tighten fill ${String(error)}`, { to: 'debug' })
      }
      if (!filled) {
        if (shown !== null) shown.note = COPY['tighten.nofill']
        void $.ui.invalidate('ui.render')
        return
      }
      filledDraft(current.after)
      await close()
    }
    const keep = async () => {
      // The command took the box's place; the draft goes back unless something has been typed since.
      try {
        const box = await $.prompt.read()
        if (box.text.trim() === '' || /^\s*\/glass\b/i.test(box.text)) {
          if ((await $.prompt.fill({ text: current.before, mode: 'replace' })).isFilled) filledDraft(current.before)
        }
      } catch (error) {
        $.ui.log(`claudeglass: tighten keep ${String(error)}`, { to: 'debug' })
      }
      await close()
    }

    return (
      <Box flexDirection="column">
        <Text bold>{COPY['tighten.headline']}</Text>
        <Text>{fill(COPY['tighten.counts'], { before: words(current.before), after: words(current.after) })}</Text>
        <Text dimColor>{COPY['tighten.legend']}</Text>
        <Code source={unifiedDiff(current.before, current.after)} format="diff" />
        <Text dimColor>{costLine(current, settings)}</Text>
        {current.note !== null ? <Text>{current.note}</Text> : null}
        <Box flexDirection="row" gap={1}>
          <Button key="use" label={COPY['tighten.use']} hotkey="u" onPress={() => void use()} />
          <Button key="keep" label={COPY['tighten.keep']} hotkey="e" onPress={() => void keep()} />
        </Box>
      </Box>
    )
  })
}
