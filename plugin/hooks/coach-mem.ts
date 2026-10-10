import type { GlassRestState, GlassSettings, GlassSnap } from '../types'
import { HOW, PATTERNS, THRESHOLDS, TIPS } from './generated/coaching.js'
import { COPY } from './generated/copy.js'
import {
  BACKGROUND_FORGET_S,
  backgroundLaunched,
  backgroundNotified,
  changesFile,
  charsPerToken,
  coldHint,
  compilePatterns,
  draftShape,
  dripHint,
  editDraft,
  emptyBackground,
  emptyDraft,
  endsOnQuestion,
  freshHint,
  isChangeRequest,
  isGo,
  isNotification,
  launchedInBackground,
  mergeRest,
  pasteHint,
  pickHint,
  pollHint,
  startingContext,
} from './lib/coach.js'
import type { Background, Draft, Exchange, Found, LastReply, Patterns, RawPatterns, Thresholds } from './lib/coach.js'
import { mayShow, recordShown } from './lib/rest.js'
import { fill } from './lib/text.js'
import { formatTokens } from './lib/units.js'
import { noteShown, seconds } from './sensor'
import { coachingApplies } from './settings'
import type { View } from './view'

// What the coaching that runs before a message is sent keeps in module memory
// (a render hook may not write $.state, and none of this is worth keeping past a
// reload): the draft's shape, the messages sent so far with what each came to,
// the background work still out, and the hint now on screen. Only lengths,
// counts and yes-or-no answers are kept: the words of a message are looked at
// where they arrive and dropped. coach.ts feeds this from the events; hint.ts
// and the draft hooks ask it what to say. The decisions are lib/coach.js.

const MAX_EXCHANGES = 12
const patterns: Patterns = compilePatterns(PATTERNS as unknown as RawPatterns)

/** A plan, as the tip about starting afresh needs it: its length, and the file it was saved to. */
export type PlanTip = { chars: number; file: string | null }

/** A hint the screen showed, waiting for the next event hook to put it in the snapshot's sensor list. */
type Shown = { kind: string; ts: number; ctx: number }

type Mem = {
  th: Thresholds
  cpt: number
  draft: Draft
  shape: string
  /** The kind of hint now on screen; it stays while it applies, without asking the rest rule again. */
  episode: string | null
  /** The rest rows the render hooks stamped, laid over the snapshot's. */
  overlay: GlassRestState
  shown: Shown[]
  exchanges: Exchange[]
  /** When the last typed message was sent, in ms. */
  submitAt: number | null
  /** Whether the last reply ended on a question. */
  asked: boolean
  background: Background
  /** Characters of the session's first message, for what a fresh start would carry. */
  firstPromptChars: number | null
  /** The plan the last ExitPlanMode call was refused (a go-ahead typed next approves it). */
  rejectedPlan: PlanTip | null
  /** The permission mode the message being submitted was sent under (classic.UserPromptSubmit carries it); null until known. */
  mode: string | null
  clearStatus: (() => void) | null
}

function fresh(): Mem {
  const th = THRESHOLDS as unknown as Thresholds
  return {
    th,
    cpt: charsPerToken(null, null),
    draft: emptyDraft(),
    shape: '',
    episode: null,
    overlay: { hints: {} },
    shown: [],
    exchanges: [],
    submitAt: null,
    asked: false,
    background: emptyBackground(),
    firstPromptChars: null,
    rejectedPlan: null,
    mode: null,
    clearStatus: null,
  }
}

let mem = fresh()
mem.shape = draftShape(mem.draft, mem.cpt, mem.th.big_paste_tokens)

/** Forgets everything (a new session, or a test). */
export function resetCoach(): void {
  clearPlanStatus()
  mem = fresh()
  mem.shape = draftShape(mem.draft, mem.cpt, mem.th.big_paste_tokens)
}

export function thresholdsOf(settings: GlassSettings): Thresholds {
  return { ...THRESHOLDS, ...settings.thresholds } as unknown as Thresholds
}

/** Takes the thresholds and the characters-per-token the settings call for. */
function learn(settings: GlassSettings, model: string | null): void {
  mem.th = thresholdsOf(settings)
  mem.cpt = charsPerToken(settings.charsPerToken, model)
}

// -- the draft ---------------------------------------------------------------

/** Notes an edit of the prompt box; true when what decides a hint changed, so it should be drawn again. */
export function editBox(edit: { text: string; start: number; end: number; inputText: string }, after: { text: string }): boolean {
  mem.draft = editDraft(mem.draft, edit, after, patterns, mem.th.drip_chars)
  const shape = draftShape(mem.draft, mem.cpt, mem.th.big_paste_tokens)
  if (shape === mem.shape) return false
  mem.shape = shape
  return true
}

// -- messages sent -----------------------------------------------------------

const SLASH_COMMAND = /^\/[A-Za-z][\w:-]*(\s|$)/

/**
 * The prompt origins that are a person typing: the composer, the Remote Control bridge (a phone or
 * web client), the SDK host's own turn (what Desktop sends for a message typed in its Code tab) and
 * the owner pinging from Slack. A notification, a schedule, a peer session, a plugin, a channel's
 * relay, a hand-off and the engine's own continuation are not.
 */
const TYPED_ORIGINS = ['composer', 'bridge', 'sdk', 'slack-ping']

export function isTypedOrigin(kind: string): boolean {
  return TYPED_ORIGINS.includes(kind)
}

/**
 * A message went in. A typed one (the composer, or the phone) joins the run of
 * messages drip_feed counts; a background task's notification ends the wait for that work.
 */
export function submitted(snap: GlassSnap, settings: GlassSettings, input: { text: string; origin: string; queued: boolean }, now: number): void {
  learn(settings, snap.model)
  mem.episode = null
  mem.draft = emptyDraft()
  mem.shape = draftShape(mem.draft, mem.cpt, mem.th.big_paste_tokens)
  const { text } = input
  if (input.origin === 'task-notification' || isNotification(text, patterns)) {
    mem.background = backgroundNotified(mem.background, text, patterns)
    return
  }
  if (!isTypedOrigin(input.origin)) return
  const trimmed = text.trim()
  if (trimmed === '') return
  if (SLASH_COMMAND.test(trimmed.slice(0, 80))) return
  mem.rejectedPlan = null
  if (mem.firstPromptChars === null && snap.replies === 0) mem.firstPromptChars = text.length
  if (input.queued) return
  const prev = mem.exchanges[mem.exchanges.length - 1]
  mem.exchanges.push({
    answer: mem.asked && !(prev?.edited ?? false),
    answered: false,
    change: isChangeRequest(text, patterns),
    small: trimmed.length <= mem.th.drip_chars,
    edited: false,
    since: mem.submitAt === null ? null : (now - mem.submitAt) / 1000,
  })
  if (mem.exchanges.length > MAX_EXCHANGES) mem.exchanges.splice(0, mem.exchanges.length - MAX_EXCHANGES)
  mem.submitAt = now
  mem.asked = false
}

/**
 * Whether a main-session call to a launching tool (Workflow) sent its work to the background: the call asked
 * for it, or its result says it launched that way (capture_hook._background_start).
 */
export function startedInBackground(input: Record<string, unknown>, output: unknown, text: string | undefined): boolean {
  if (input.run_in_background === true) return true
  if (output !== null && typeof output === 'object') {
    const found = output as { isAsync?: unknown; status?: unknown; taskType?: unknown }
    if (found.isAsync === true || found.status === 'async_launched' || found.taskType === 'local_workflow') return true
  }
  return launchedInBackground(text, patterns)
}

/** Whether a tool call changes a file of yours (an edit tool, or a shell command that writes one). */
export function editsFile(name: string, input: Record<string, unknown>): boolean {
  return changesFile(name, input, patterns)
}

/** Marks the reply now running as one that changed a file. */
export function markEdited(): void {
  const last = mem.exchanges[mem.exchanges.length - 1]
  if (last !== undefined) last.edited = true
}

/** A tool's result said its work went to the background. */
export function noteBackground(text: string | undefined, callId: string, now: number): void {
  if (launchedInBackground(text, patterns)) mem.background = backgroundLaunched(mem.background, callId, seconds(now))
}

function hasBackground(snap: GlassSnap, nowS: number): boolean {
  if (mem.background.items.some(one => nowS - one.at < BACKGROUND_FORGET_S)) return true
  return snap.agents.some(one => one.background && one.status === 'running')
}

// -- the end of a reply ------------------------------------------------------

/**
 * A reply of the main session ended: whether it ended on a question to you, for the
 * next message's hint. The plan_fresh line on the status bar has had its reply.
 */
export function turnEnded(snap: GlassSnap, settings: GlassSettings, end: { answer?: string; reason?: string }): void {
  learn(settings, snap.model)
  const last = mem.exchanges[mem.exchanges.length - 1]
  if (last !== undefined && (end.reason === undefined || end.reason === 'answer')) last.answered = true
  const answer = end.answer ?? ''
  mem.asked = answer !== '' && endsOnQuestion(answer, patterns)
  clearPlanStatus()
}

// -- the hint line -----------------------------------------------------------

function limitOf(snap: GlassSnap): { stopped: boolean; reset: number | null } {
  const resetsAt = snap.fiveHour?.resetsAt ?? null
  const reset = resetsAt === null ? Number.NaN : Date.parse(resetsAt)
  return { stopped: snap.limitHit, reset: Number.isFinite(reset) ? reset / 1000 : null }
}

/**
 * The one hint for the draft in the box now, or null (always null when coaching is
 * off or doesn't cover this project). In the capture hook's
 * order (drip_feed, big_paste, status_poll, cold_return) the first that
 * applies, isn't muted and isn't resting. A hint that was on screen stays
 * while it applies. When a new one is chosen it is stamped for the rest rule
 * and queued for the sensor (drainShown).
 */
export function chooseHint(view: View): Found | null {
  const { snap, settings, now } = view
  learn(settings, snap.model)
  // Coaching is for those who turned it on, in the projects it covers.
  if (!coachingApplies(settings)) {
    mem.episode = null
    return null
  }
  const th = mem.th
  const nowS = seconds(now)
  const draft = mem.draft
  const last: LastReply | null = snap.lastTs === null || view.tokens <= 0 ? null : { ctx: view.tokens, at: snap.lastTs / 1000, ttl: settings.ttl }
  const since = mem.submitAt === null ? null : (now - mem.submitAt) / 1000
  const previous = mem.exchanges[mem.exchanges.length - 1]
  const answer = mem.asked && !(previous?.edited ?? false)
  const compacted = snap.pendingCompaction || (snap.lastTs !== null && snap.compactions.some(one => one.ts >= (snap.lastTs ?? 0)))
  const rest = mergeRest(snap.rest, mem.overlay)
  const picked = pickHint(
    [
      dripHint(mem.exchanges, draft, since, answer, view.tokens, th),
      pasteHint(draft.len + draft.hidden, mem.cpt, th),
      pollHint(draft.status, last, nowS, hasBackground(snap, nowS)),
      coldHint(last, nowS, compacted, limitOf(snap), th),
    ],
    settings.muted,
    rest,
    nowS,
    th,
    mem.episode,
  )
  if (picked === null) {
    mem.episode = null
    return null
  }
  if (picked.kind !== mem.episode) {
    mem.overlay = recordShown(rest, picked.kind, picked.stake, nowS, th, settings.once.includes(picked.kind))
    mem.episode = picked.kind
    mem.shown.push({ kind: picked.kind, ts: now, ctx: view.tokens })
  }
  return picked
}

/** The words of a hint (not the cold-cache warning, which hint.ts writes with its costs). */
export function hintText(found: Found, surface: string): string {
  const variant = surface === 'terminal' ? 'terminal' : 'desktop'
  const tokens = (name: string) => formatTokens(found.fields[name] ?? 0)
  switch (found.kind) {
    case 'big_paste':
      return fill(COPY['hint.paste'], { tokens: tokens('tokens') })
    case 'drip_feed':
      return fill(COPY['hint.drip'], { count: found.fields.count ?? 0, tokens: tokens('ctx') })
    case 'status_poll':
      return fill(COPY['hint.poll'], { tokens: tokens('ctx'), how: HOW.poll_how[variant] })
    default:
      return ''
  }
}

/** Puts the hints the screen showed into the snapshot: the sensor list and the rest rows. */
export function drainShown(snap: GlassSnap): void {
  for (const one of mem.shown) noteShown(snap, one.kind, 'hint', one.ts, one.ctx)
  mem.shown = []
  snap.rest = mergeRest(snap.rest, mem.overlay)
}

/** The session is ending: what the screen showed goes into the snapshot first, and the status line is cleared. */
export function endCoach(snap: GlassSnap): void {
  drainShown(snap)
  clearPlanStatus()
}

// -- plan_fresh --------------------------------------------------------------

/** Clears the status line the plan tip is on, if it is. */
export function clearPlanStatus(): void {
  const clear = mem.clearStatus
  mem.clearStatus = null
  if (clear === null) return
  try {
    clear()
  } catch {
    // The line goes with the session.
  }
}

/** True while the plan tip is on the status line. */
export function planStatusShowing(): boolean {
  return mem.clearStatus !== null
}

/** Remembers how to clear the status line the plan tip is on. */
export function planStatusSet(clear: () => void): void {
  mem.clearStatus = clear
}

/**
 * The plan was approved: the tip for the status line when it carries enough
 * planning that building in a fresh session would save it, else null. Under
 * the same rest, mute and once rules as the other hints; null as well when coaching
 * is off, doesn't cover this project, or the user switched this tip off
 * (`plan_fresh: false` in coaching.json).
 */
export function planApproved(snap: GlassSnap, settings: GlassSettings, plan: PlanTip, now: number): string | null {
  learn(settings, snap.model)
  if (!coachingApplies(settings) || !settings.coaching.planFresh) return null
  const th = mem.th
  const nowS = seconds(now)
  const ctx = snap.reqs[snap.reqs.length - 1]?.ctx ?? snap.reading?.tokens ?? null
  const start = snap.startupFirst === null || mem.firstPromptChars === null ? null : startingContext(snap.startupFirst, mem.firstPromptChars, mem.cpt)
  const found = freshHint(ctx, start, plan.chars, snap.compactions.length > 0, mem.cpt, th)
  if (found === null || settings.muted.includes(found.kind)) return null
  const rest = mergeRest(snap.rest, mem.overlay)
  if (!mayShow(rest, found.kind, found.stake, nowS, th)) return null
  snap.rest = recordShown(rest, found.kind, found.stake, nowS, th, settings.once.includes(found.kind))
  noteShown(snap, found.kind, 'status', now, ctx ?? 0)
  const how = HOW.fresh_how[snap.surface === 'terminal' ? 'terminal' : 'desktop'].replace('`<file>`', plan.file === null ? 'the plan file' : `\`${plan.file}\``)
  return fill(TIPS.plan_fresh, { kept: formatTokens(found.fields.kept ?? 0), fresh_how: how })
}

/** The call to leave plan mode was refused: a go-ahead typed next approves this plan (the hook's _typed_plan_hint). */
export function planRefused(plan: PlanTip): void {
  mem.rejectedPlan = plan
}

/** A new plan, or one approved: nothing is waiting for a typed go-ahead. */
export function planSettled(): void {
  mem.rejectedPlan = null
}

/** The plan sent back and still waiting for a go-ahead, if any: read before `submitted`, which forgets it. */
export function refusedPlan(): PlanTip | null {
  return mem.rejectedPlan
}

/** The permission mode the message being sent was sent under; null forgets the last one. */
export function modeSent(mode: string | null): void {
  mem.mode = mode
}

/**
 * Whether the message just sent was sent outside plan mode. A plan sent back keeps the session in plan mode, so a
 * message sent in any other mode left it, and that approves the plan as a go-ahead does (the hook's left_plan_mode).
 */
export function leftPlanMode(): boolean {
  return mem.mode !== null && mem.mode !== '' && mem.mode !== 'plan'
}

/**
 * The plan a typed message approves: the refused plan, when the message is a go-ahead ("go ahead",
 * "implement the plan"). Asked before `submitted`, which forgets the refused plan.
 */
export function goAheadPlan(text: string, origin: string): PlanTip | null {
  const plan = mem.rejectedPlan
  return plan !== null && isTypedOrigin(origin) && isGo(text, patterns) ? plan : null
}
