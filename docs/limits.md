# Usage-limit tracking (v3-limits)

When an account hits its usage cap, Claude Code's harness pauses. If
the stop outlasts the cache's hour, the first reply after it writes the
conversation to the cache again; a reply within the hour reads the cache
as usual. That rewrite is the price of carrying on, not a caching habit
to fix. Left unattributed, that pause looks
exactly like an ordinary long idle gap — inflating "gaps > 5 min" counts,
masquerading as a routine full-expiry re-cache, driving long-tool-wait
recommendations, and occasionally pushing a session into "overnight" mode
purely because a cap pause, not real overnight work, made the span or the
gap between your messages long enough. v3-limits turns a usage-cap pause, a harness-forced subagent
termination, and the desktop app's automatic resume ping into first-class,
attributable facts, and threads that attribution through every downstream
table that would otherwise misread it.

`events.py`/`parse.py` do the actual detection; `limits.py` is purely a
*reader* of their already-parsed state — it detects nothing new. A
synthetic assistant line's text becomes `EventKind.LIMIT_HIT` (subkind
`session_limit`/`weekly_limit`), the desktop app's resume ping becomes
`EventKind.LIMIT_RESUME`, and a harness-killed subagent's task
notification becomes `EventKind.AGENT_TERMINATED` (subkind
`rate_limit`/`other`). Every turn whose gap to the previous one spanned
one of these events carries `Turn.gap_cause == "limit"`.

## What `limits.py` provides

- `limit_pause_intervals(top)` — every usage-cap pause in a transcript's
  own top-level turns, as `(start, end)` UTC-aware `datetime` pairs, read
  off `Turn.gap_cause`/`Turn.gap_s`/`Turn.ts` (not re-derived by matching
  raw `LIMIT_HIT`/`LIMIT_RESUME` events up by hand — see the deviation
  note below). A pause ends at the earlier of the first reply after it
  and the latest `reset_ts` among the `LIMIT_HIT` events in the wait, so
  the time after the reset, which is yours, is not the limit's. Consumed
  by `classify.py` to discount pause time out of its gap/span statistics
  (see "Downstream attribution" below) and by `pieces.py`, which leaves
  a pause out of the silence a tag-free piece start measures.
- `pause_overlap_s(start, end, pauses)` — the seconds of `[start, end]`
  inside those pauses.
- `limit_markers(result)` — every `LIMIT_HIT`/`LIMIT_RESUME`/
  `AGENT_TERMINATED` event as a `(ts, kind, detail)` triple, sorted by
  `ts`, ready for a session-timeline API to render as markers (see
  "Session-timeline marker contract" below).
- `LimitStats`/`build_section` — the corpus-wide `limits` report section
  (nine tables, described below). `LimitStats.episodes()` returns one
  `Episode` per limit stop (see "Limit stops" below), `stop_spend()` the
  list-price spend before one and `rollup()` the roll-up over them (see
  "Spend before a stop" below).
- `csv_cross_check` — cross-checks the transcript-derived stop count
  against `usage-log.csv` (`tools/log_usage.py`'s own ground-truth log,
  when one exists) as a sanity check, not a second detector. The report
  reads the rows for its sessions from `<config_dir>/usage-log.csv`
  (`read_usage_log_rows`), because the status-line reader keeps only
  context-window rows, and builds the table only when they include
  `five_hour`/`seven_day` rows (`has_rate_limit_rows`): a setup with no
  status line logs none, and the table would show zero against a real
  count.
- `wake_class(kinds)` and `busy_reset_hour(counts)` — the two small rules
  behind the "what woke the session" table and the reset-hour advice.

## Limit stops

One stop writes a storm of limit lines: a retry, a cut-off agent's
notice, and more retries. Counting lines would read one stop as dozens,
so the section counts **stops** and keeps the line count as "limit
messages".

A stop (`Episode`) is keyed by the line's event subkind and its
`reset_ts` floored to the minute. The subkind is used rather than
`rateLimitType`, which some real lines lack.

- Same-subkind keys whose resets fall within two hours merge into one
  stop.
- A line with no `reset_ts` joins the same-subkind stop whose reset lies
  0-5 hours (five-hour) or 0-7 days (weekly) after it, the soonest one if
  several qualify. A line that joins none makes a stop of its own, one
  per storm: lines within one window length of the first are one stop.
- Replayed lines are already dropped by `parse.py` by `(file, uuid)`, so
  a replay never adds a message or a stop.
- A weekly stop is marked "didn't stop work" when a main-session reply,
  from any session, lands more than ten minutes after its first line and
  before its reset. The ten-minute margin absorbs agent replies already
  in flight. A weekly stop with no reset, or with no such reply, counts as
  having stopped work.
- A subagent transcript is **cut off** when it made at least one priced
  reply and its last limit line is later than its last reply. It is
  counted on the stop that line belongs to and in the summary, split
  into direct agents (`meta.kind == "subagent"`) and workflow agents
  (`"workflow-agent"`), with the list-price spend of its replies. An
  agent with no reply is not counted.
- Sessions affected is the number of distinct top-level sessions with a
  limit line in any stop; a subagent's line counts for the session that
  ran it.
- The window is the days from the earliest to the latest timestamp the
  corpus holds, rounded up and never below one. The report's own window
  is only a label.
- In a main session, each pair of consecutive limit lines is filed by
  what lay between them (`wake_class`): something you typed, the
  desktop app's resume, a scheduled task, a background agent's notice,
  or only `isMeta` user lines, which are their own class. Anything else,
  including nothing, is "other". Only the kind of line is read.

## Spend before a stop

The report asks what you spent in the window before each stop, and who
spent it, so the stops table can say where a limit went. Nothing new is
stored: `LimitStats.add` prices each reply once and keeps it in a UTC
quarter-hour bucket of the same shape `turns_agg` holds, so the figures
need no extra table.

- **Window.** From 5 hours before the reset (7 days for a weekly stop) to
  the stop's first limit line. When its lines name more than one reset,
  the earliest opens the window. Quarter hours at either edge are counted
  whole. A stop with no reset has no window, so its spend and shares are
  empty.
- **Source.** A main-session transcript is "main"; a `workflow-agent`
  transcript is "workflow"; any other subagent is "direct". Shares are
  each source's part of the list-price spend in the window. The copy
  always says "share of list-price spend; the limit may weigh models
  differently", and never "% of your window": the account's own limit
  maths is not known here.
- **Top two spenders.** The two agent type and model family pairs
  (haiku, sonnet, opus, fable, or other; the main session's type is
  "top-level") with the largest spend, each with its share.
- **Burst.** The share of spend in quarter hours when 3 or more direct or
  workflow agent transcripts were active (`BURST_AGENTS`). An agent is
  active from its first to its last priced quarter hour, so one that
  waited between replies still counts while it waited. An agent with one
  priced reply is active for that quarter hour only. Main sessions never
  count, however many reply at once.
- **Roll-up.** `limits_stops_rollup` covers every stop that began on or
  after `current_since` (18 Sep 2026 by default), that stopped work and
  that has a window. A weekly stop you worked through is left out of it,
  though the stops table still lists it. The roll-up adds up the 5-hour
  stops' windows, taking the union of their quarter hours, so a quarter
  hour inside two windows counts once. A weekly window holds the whole
  week before the stop, so it would set the burst share against itself:
  the weekly stops' windows are used only when no 5-hour stop counts.
  The first note says which limit it describes. The roll-up names the
  largest cost centre (main, direct agents or workflow agents) and gives
  the burst share in these stops beside the same share across all spend
  since `current_since`.
- **The card.** The `limit-pressure` card follows the roll-up. The main
  session gives plan and `/clear` advice, direct agents give "run fewer
  agents at once", workflow agents give the workflow script's
  concurrency. The card adds the burst sentence only when the burst share
  in your stops is at least 10 points above the share across all your
  work (`BURST_GAP_POINTS`). Its link is `{{page:cache/rebuilds}}`, the
  page the dashboard maps the limits section to (`SECTION_PAGE_MAP` in
  `links.js`); a test keeps every link in the limit copy on that page.

`LimitThresholds.current_since` (`[thresholds] current_since`, a
`YYYY-MM-DD` day in UTC; empty counts every run) marks the day the
settings you run now began. It limits only the dollar figures: the spend
of cut-off agents, the cost of cache writes after a pause and the stops
roll-up. Counts and tokens stay all-time, and the thresholds note in the
report names the day.

## The `limits` report section

| Table | What it shows |
|---|---|
| `limits_summary` | One "all" row, leading with 5-hour and weekly **stops** (and how many weekly stops stopped work), days covered, sessions affected, agents cut off (direct and workflow, with their spend), then limit messages (session + weekly split), resumes, agents terminated (and by rate limit specifically), pause count/total time, and the cache-creation tokens/write cost paid by the turn immediately following each pause (the dollar figures count runs since `current_since`). `limit_hits` is shown as "Limit messages". |
| `limits_stops_rollup` | One "all" row above the stops table: 5-hour stops counted since `current_since` (weekly stops only when there are none), the list-price spend in their windows, the main, direct-agent and workflow-agent shares, the largest cost centre and its share, and the burst share beside the same share across all work. Its first note is the roll-up sentence naming the largest cost centre. |
| `limits_stops` | "Your recent limit stops", newest first and capped at 50: kind (5-hour or weekly), reset time in your zone, minutes before the reset at the first stop, list-price spend in the window, the three source shares, the burst share, the top two agent type and model family pairs, whether the stop stopped work, and how many agents it cut off. |
| `limits_hits_by_kind` | `session_limit`/`weekly_limit` message counts and share. |
| `limits_agent_terminated` | `rate_limit`/`other` termination counts and share. |
| `limits_pauses` | Corpus-wide pause count/total/mean duration (a pause ends at the limit's reset when you came back later; a corpus-wide *median* can't be derived from already-aggregated per-agent-type medians, so it isn't reported here — see the by-agent-type table). |
| `limits_reset_hour_histogram` | Count and share of limit **stops** by local hour of day (0-23): one per stop however many lines it wrote. Its advice shows only when one hour holds 3 or more stops and 30% or more of them. |
| `limits_wake_gaps` | What lay between consecutive limit lines in main sessions: typed, resume, scheduled, agent notice, only `isMeta` lines, other. |
| `limits_by_agent_type` | "Who got the limit message": per-agent-type roll-up of limit messages received (the main session relays each cut-off agent's notice), resumes, terminations, pause count/total/median/max, and the post-pause cache-creation tokens/cost. |
| `limits_csv_cross_check` (`csv_cross_check`, called separately) | Transcript-derived stop counts vs. `usage-log.csv`'s own exhaustion-row counts for `five_hour`/`seven_day`. Left out when the log has no such rows. |

### Reconciling the two "cost of a limit pause" figures (N2)

Two tables both put a dollar figure on usage-cap pauses, and they are
**related but not equal** — reading one as a check on the other will
look like a discrepancy unless the population difference is understood:

| Figure | Table | Population |
|---|---|---|
| `limit_turn_write_cost_usd` | `limits_summary` (this section) | Every turn with `Turn.gap_cause == "limit"` and `turn_index > 0` — i.e. every turn that immediately followed a usage-cap pause, full stop. |
| `unavoidable_limit_expiry_cost_usd` | `recache_summary` (`docs/sections-reference.md`'s "recache" section) | The subset of the above that *also* clears `recache.py`'s ordinary re-cache thresholds (`ctx > ctx_floor` and `cache_read_tokens < cr_ratio * ctx` — see `recache.detect`). A post-pause turn with a small context, or one whose cache happened to still hold enough to clear `cr_ratio`, is counted here but not there. |

Both are legitimate: `limits_summary`'s figure answers "what did every
post-pause turn cost for the cache writes it recorded", unconditionally,
whatever `recache.py`'s thresholds say (see "Assumptions" below). It
counts runs since `current_since` only. `recache_summary`'s
figure exists to make sure `avoidable_cost_usd` on that same table only
ever totals genuinely avoidable causes — a limit-expiry turn's cost is
reported there as `unavoidable_limit_expiry_cost_usd`, a separate column,
specifically so it is never summed into `avoidable_cost_usd` (review B5)
and never double-counted as caching behaviour to fix. Neither figure is
wrong; they simply answer different questions over overlapping but
distinct populations.

## Downstream attribution

- **`classify.py`**: `SessionFeatures.limit_pause_s` sums every pause
  interval's duration (each pause ends at its reset when you came back
  later). `_median_and_max_gap` and `_max_gap_pair` subtract per-gap pause
  overlap (clamped at zero) so a usage-cap pause no longer counts as a
  behavioural gap, and the longest gap names the pair of messages it
  really is once pauses are out. `classify_mode`'s overnight rule asks how
  long Claude worked at night while you were away
  (`unattended_night_s`, two hours or more). Away time is the gap between
  two messages of yours of over an hour with the pauses in it taken out,
  and nothing is work during a pause, so a pause alone can no longer
  misclassify a session as overnight. The `multi_day` flag uses
  `active_span_s`, the span less any silence of four hours or more you
  came back from (measured net of pauses, so a return three hours after a
  reset is not a resumed session); a pause inside a shorter gap stays in
  the span, so one spanning midnight is still, correctly, multi-day.
- **`recache.py`/`ttl.py`**: a re-cache that follows a pause is priced
  the same way any other re-cache is, and kept apart from the
  avoidable ones by `Turn.gap_cause == "limit"`; `limits.py` reads that
  same fact rather than re-deriving it, so the two can never drift apart.
  If the stop outlasts the cache's hour, that reply writes the
  conversation to the cache again. That rewrite is the price of carrying
  on, not a caching habit to fix.
- **`recommend.py`**: the `limit-pressure` rule (`_rule_limit_pressure`)
  counts stops, not messages. It fires when the `limits_summary` table
  reports five-hour stops at a rate of `limit_pressure_min_episodes`
  (default 2) or more per 7 days (the window is read as at least a week,
  so one stop is never a rate), any weekly stop that stopped work, or at
  least `limit_pressure_min_terminated_rate_limit` (default 1) subagent
  cut off by a limit. A single six-line storm does not fire it. The old
  `limit_pressure_min_hits` key counted messages and is read but ignored.
  This surfaces repeated usage-cap pressure as its own finding, since it
  is the root cause behind several other tables' downstream symptoms.
  Its evidence also carries the roll-up's stops counted, the three
  source shares and the two burst shares, which `advice.py` reads to name
  the largest cost centre and, when it stands out, the burst (see "Spend
  before a stop").
- **`scorecard.py`**: `ScorecardInputs.limit_recache_share_pct` (portion
  of `recache_share_pct` already known to be pause-forced) is subtracted
  from the raw re-cache share before `cache_efficiency` scores it — an
  account-level pause is not a workflow choice, and scoring it as one
  would be misleading — reported via a note rather than silently.
  `ScorecardInputs.limit_pause_sessions`, when non-zero, adds a
  non-scoring `data_quality` note pointing at this section.
- **`statusline.py`**: a `5h`/`7d` rate-limit segment gets a trailing
  `!` marker once its `used_percentage` clears 90%, warning live that a
  harness pause may follow shortly; a `usage-log.csv` row for
  `five_hour`/`seven_day` read back at or above 100% used is tagged
  `source=limit_hit` instead of the usual `source=statusline`.

## Session-timeline marker contract

`limit_markers(result) -> list[tuple[str, str, dict]]` is the basis for
`service/api.py`'s `GET /api/session/<id>` usage-limit markers: each
triple is `(ts, kind, detail)`, sorted by `ts` (ascending, ISO-8601
string comparison; an event with no timestamp sorts first as `""`).

- `ts`: the event's own `Event.ts` (an ISO-8601 string), or `""`.
- `kind`: the event kind's own string value — one of `"limit_hit"`,
  `"limit_resume"`, `"agent_terminated"`.
- `detail`: a shallow copy of the event's own `Event.detail` dict (already
  privacy-clean — counts, enum-like strings, an hour-of-day integer, no
  message text or paths), plus a `"subkind"` key when the event carries
  one (`session_limit`/`weekly_limit` for `limit_hit`;
  `rate_limit`/`other` for `agent_terminated`).

A consumer can render one marker per triple without importing `EventKind`
or reaching into `TranscriptResult.events` directly.

Wired: `service/store.py`'s `Store.turns_for_session` calls this
function against the session's stored top-level transcript digest and
reshapes each triple into a `{"ts", "kind", "detail"}` object;
`service/api.py`'s `route_session` forwards the result as
`GET /api/session/<id>`'s `limit_markers` field (see
[`docs/api.md`](api.md)). The dashboard's session timeline (chart 5 in
the session drawer on Spend › Sessions, drawn by `buildSessionTimeline`
in `service/static/charts-types.js`) renders them as their own marker
kinds (a shape and a legend entry per kind, one lane per kind above the
context line, a tooltip naming `kind`/`detail.subkind`), positioned
along the chart's time axis by `ts` rather than by turn index — unlike the
compaction/spawn/human markers, a usage-limit event's timestamp falls
*inside* the pause gap between two turns, not at a turn index of its
own, so it cannot be pinned to one of `turn_series`'s existing points
(see `docs/ui.md`).

## Assumptions and what isn't attributable

- A pause's `(start, end)` interval is read off the turn that carries
  `Turn.gap_cause == "limit"` (`start = turn.ts - turn.gap_s`,
  `end = turn.ts`), not re-derived by matching raw `LIMIT_HIT`/
  `LIMIT_RESUME` events up by hand. The two are equivalent by
  construction (`parse.py`'s two-buffer scheme guarantees the limit
  event(s) precede exactly the turn that carries `gap_cause ==
  "limit"`), and this is far simpler. The one thing read off the events
  is the reset: `end` is `turn.ts` or, when earlier, the latest
  `reset_ts` among the `LIMIT_HIT` events between `start` and `turn.ts`.
  A limit line with no `reset_ts` adds none, so a wait whose lines have
  none ends at the return. Typing back 3 hours after a reset therefore
  removes only the time up to the reset from the gap, not the 3 hours
  after it.
- A `LIMIT_HIT`'s `reset_ts` is the line's own `quotaLimits.resetsAt`
  when it has one. Otherwise it is read from the "resets 3pm
  (Europe/London)" text in the zone it names, or in the machine's own
  zone when that name can't be resolved (every named zone on a Windows
  install with no `tzdata`). The weekly form names the day ("resets
  Oct 3, 9am") and the reset lands on that day; the five-hour form
  lands on the next time it is that time of day. A zone with more than
  one slash in its name gets no `reset_ts` from the text.
- A turn immediately following a usage-cap pause is priced via
  `pricing.price_turn`'s default observed-split path from the cache split
  it recorded, not re-detected against `recache.py`'s `ctx_floor`/
  `cr_ratio` thresholds. Whether the stop outlasted the cache's hour is
  not checked: if it did, that reply wrote the conversation to the cache
  again, and a reply within the hour read the cache as usual. That
  rewrite is the price of carrying on, not a caching habit to fix.
- Spend before a stop is the list-price spend of the replies priced in
  the window, in quarter-hour buckets, so quarter hours at the window's
  edges are counted whole. It is not the limit's own measure, which may
  weigh models differently. A stop with no reset has no window.
- The day `current_since` defaults to (18 Sep 2026) is assumed to mark
  the start of the settings you run now. The help copy names it as a
  date, so changing the setting changes the report's note and figures but
  not that copy.
- The reset time the stops table shows is in the machine's own zone,
  floored to the minute.
- Reset-hour-of-day prefers `LIMIT_HIT`'s own `reset_minutes_of_day` (the
  literal local hour named in the synthetic text, e.g. "resets 3:00pm")
  over converting `reset_ts` (always UTC) through the machine's own local
  zone, which is used only as a fallback when the text carried no
  parseable "resets ..." clause.
- `csv_cross_check` counts a `usage-log.csv` row as an exhaustion signal
  purely on `used_percentage >= csv_exhaustion_pct` (default 100%): a bare
  `resets_at` value is present on nearly every row regardless of
  exhaustion, so it is never treated as a signal on its own. It compares
  those rows with stops, and the report leaves it out when the log has no
  `five_hour`/`seven_day` row at all.
- Pause intervals are computed only from a transcript's own top-level
  turns, never a subagent's — a pause is an account-wide event, but the
  gap/span statistics it feeds (`classify.py`'s median/max gap, its
  away time) are themselves top-level-only. Claude's own activity, which
  the overnight rule measures against that away time, joins the main
  session's replies and every subagent's.
- A stop whose lines have neither `reset_minutes_of_day` nor a parseable
  `reset_ts` is excluded from the reset-hour histogram (but still counted
  in `limits_summary`). A stop's hour is the one its latest-reset line
  names.
- This module does not attempt to distinguish a usage-cap pause from an
  ordinary long idle gap that merely happens to end near a `LIMIT_HIT`
  event with no matching turn (e.g. the transcript ends mid-pause) — such
  a case simply produces no `gap_cause == "limit"` turn and is not
  double-counted or guessed at.
