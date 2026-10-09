# Product

## Register

product

## Users

Developers who use Claude Code every day, in the terminal or in the
Claude Desktop app's Code tab, on API billing or a Pro or Max
subscription. They want to spend fewer tokens (or less of their usage
limit) without slowing down.

They meet ClaudeGlass in two places:

- **The mod, mid-task.** They are in the middle of work with Claude. They
  glance at the band above the prompt between replies, and open the
  `/glass` pane only when something there makes them curious. They want an
  answer in a second and then to get back to work.
- **The dashboard, looking back.** They sit down to review a week or a
  month and decide what habit or setting to change.

## Product Purpose

ClaudeGlass shows where Claude Code's tokens go and the one change that
would save the most next. The dashboard is the history. The mod is the
live half: how full the context is, what each reply costs, what agents are
doing, one well-timed suggestion before a costly send, and a review of
what a conversation summary (compaction) kept.

Success is a user who changes what they do because of what they saw, and
an effect the dashboard can measure. A user who reads it and feels
lectured, or can't find the point, is a failure.

## Brand Personality

A calm instrument: quiet, plain, honest.

- Like a car's fuel gauge: always there, read at a glance, rarely asks
  for attention.
- Plain words, second person, short sentences. Says what happens, never
  what the code calls it.
- Honest about uncertainty: estimates say so, and a figure that can't be
  known is left out rather than guessed.
- Native to Claude Code. The mod should feel like part of the Desktop
  Code tab, not a panel bolted on.

## Anti-references

- **A data dump.** Every figure the tool knows, all at once, in internal
  names (prompt section IDs, server UUIDs, full scratchpad paths), every
  row a button with a hotkey chip. The first `/glass` pane looked like this.
- **A second `/usage`.** Rebuilding what Claude Code already shows instead
  of adding what it doesn't.
- **Vague labels in place of numbers** ("seems a bit high"), and numbers
  that disagree with Claude Code's own.
- **Nagging.** The same warning in three places, alerts mid-task, repeated
  identical tips.
- **SaaS dashboard tropes.** Hero metrics, identical card grids, gradient
  accents, gamified streaks and badges.
- **Acting on its own.** No Apply button: every fix is a prompt or a
  command the user runs.

## Design Principles

1. **Only what changes a decision.** Each visible element names the
   decision it supports. Anything else goes one press away, or to the
   dashboard.
2. **One answer first.** Each view opens with one plain sentence that
   answers its question, and offers at most one action.
3. **Speak at decision points, and rarely.** Before a send, at the end of
   a reply, at a summary. Never mid-turn, never the same thing twice.
4. **A good guest.** Look native, use the host's theme and controls, keep
   what Claude Code draws, change nothing by itself.
5. **Agree with Claude Code.** Use its figures where it has them; label
   ours as estimates; speak in the user's currency and compare with their
   own history.

## Accessibility & Inclusion

- WCAG 2.2 AA contrast in light and dark themes.
- Colour is never the only signal: a state carries a word and an icon.
- Every action is reachable from the keyboard; hotkeys go on tabs and the
  main action, not on every row.
- Every chart has alt text and a text summary.
- The terminal gets a plain-text fallback of every Desktop surface.
- No motion that matters; nothing flashes.
