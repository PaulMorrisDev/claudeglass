# The dashboard

The dashboard is the service's web page: `static/index.html`, `app.css`
and a set of native ES modules with `app.js` as the entry point. The
same `http.server` process serves it and the JSON API in
[`docs/api.md`](api.md), on `http://localhost:8765` by default.

This document says what the dashboard is for, the rules every file under
`static/` keeps, and how each part works. The code is the final word:
where this page and the code disagree, fix this page.

## Purpose and constraints

The dashboard answers one question first: what should I change next to
spend fewer tokens? Every page after the Overview is the evidence behind
that answer. It reads your Claude Code history on this machine and never
changes Claude Code itself.

### Constraints (binding on every file under `static/`)

- **Desktop only.** The dashboard is built for a desktop browser window,
  1280px wide or more. There is no phone or tablet layout and none is
  planned, so don't add narrow-screen breakpoints or report sideways
  scrolling on a narrow screen as a bug.
- **No framework and no build step.** Plain DOM APIs
  (`document.createElement`, `fetch`, `addEventListener`), no bundler or
  transpiler. `static/` is served as it is in the repo.
- **Native ES modules.** `index.html` loads the classic `theme-boot.js`
  in `<head>`, then `<script type="module" src="/static/app.js">`.
  Imports are static only (the scan rejects `import()`) and never form a
  cycle: a module opens another view through `core.js`'s `goTo`, which
  `app.js` wires to its router. `.js` is sent as `text/javascript`, as a
  module needs under `nosniff`.
- **A strict Content Security Policy.** Every response carries
  `default-src 'self'; img-src 'self' data:; style-src 'self'
  'unsafe-inline'; script-src 'self'`. So there are no inline script
  bodies and no code built from strings.
- **No external reference.** No off-origin `<script src>` or
  `<link href>`, no `@import`, and no bare `http://` or `https://`
  literal in a first-party file. `url(...)` names only a vendored font
  or an in-page `#` fill. The CLI's HTML report keeps the same rule
  (`render/html.py`).
- **Vendored, pinned third-party files.** `static/vendor/` holds
  `d3-7.9.0.min.js`; `static/fonts/` holds `InterVariable-4.1.woff2` and
  JetBrains Mono 2.304 Regular and Medium, each with its licence.
  `static/THIRD_PARTY.sha256` pins them all, and each name carries its
  release, so a new release is a new address. They are sent with
  `Cache-Control: public, max-age=31536000, immutable`; every other
  response is `no-store`. `.gitattributes` keeps git from rewriting their
  bytes, and the wheel ships them. Nothing is fetched at runtime.
- **One door to d3.** Chart code reaches d3 only through `d3.js`
  (`import d3 from "./d3.js"`), a small shim over the vendored file.
  Nothing calls d3's CSV or TSV parsers: they build code with
  `new Function`, which `script-src 'self'` refuses.
- **No Apply button.** Every fix is a prompt to paste into Claude Code or
  an `apply ... --dry-run` command to run yourself, each with a Copy
  button. `button()` refuses a label that starts with "Apply". The
  dashboard writes only this tool's own things: profile files, session
  tags and ratings in its store, and the `[capture]` table in its
  `config.toml` (Setup › Capture).
- **Amounts follow the billing mode.** Dollars on the API. On Pro or Max,
  a share of the weekly usage limit when the service can work one out,
  else the list-price equivalent. See "One number format" and
  [`docs/writing-help.md`](writing-help.md), "Amounts".
- **Commands in this install's form.** The short `claudeglass`
  runs only when pip's Scripts folder is on `PATH`. The service writes
  the form that runs its own install into `index.html`'s
  `<meta name="tl-command">` and into the commands in every API
  response (`invocation.py`, [`docs/api.md`](api.md#envelope)). A command
  the page writes itself goes through `core.js`'s `cli("capture off")`;
  fixed text that names one (the glossary) through `withCli`.
- **Privacy.** The service binds to localhost and has no login, so the
  page stores no token and sets no cookie. File text (CLAUDE.md sections,
  skill descriptions) is fetched when a drawer asks and never stored. No
  page shows a transcript path.

### Browser storage

`localStorage` holds per-viewer choices only, never data the server
owns. The helpers in `core.js` catch every error, so a private window or
blocked storage still works.

| Key | Holds |
|---|---|
| `tls:view` | the last view shown, opened when the address names none |
| `tls:window` | the chosen window |
| `tls:theme` | `light` or `dark`; missing means follow the system |
| `tls:sidebar` | `rail` or `full` |
| `tls:sort:<table>` | a table's sort |
| `tls:cols:<table>` | the columns chosen for a wide table |
| `tls:captureNotesHidden` | when the capture banner's notes were dismissed, and which |

Two older keys are read once: `tls:activeTab` (the old tab bar's last
tab, removed after) and `tls:overviewWindow` when `tls:window` is unset.
The picked project is never stored. It lives in the address only
(`?project=`), so a filter never quietly narrows the next visit.

## The design system

`app.css` opens with every colour, type size, space, radius, shadow,
layer and motion value as a custom property on `:root`. Nothing below
the token blocks uses a raw colour.

### Colour

Light values are on `:root`. Dark values are declared twice with the
same content: under `@media (prefers-color-scheme: dark)` guarded by
`:root:not([data-theme="light"])`, and under `:root[data-theme="dark"]`.
`color-scheme: light dark` lets form controls and scrollbars follow.

| Token | Light | Dark | Used for |
|---|---|---|---|
| `--surface-canvas` | `#fbfbfc` | `#121316` | the page behind everything |
| `--surface-sidebar` | `#f3f4f6` | `#0e0f12` | the sidebar |
| `--surface-panel` | `#ffffff` | `#18191d` | panels, tiles, the grid |
| `--surface-raised` | `#ffffff` | `#1f2025` | menus, popovers, tooltips, toasts, the drawer, search |
| `--surface-sunken` | `#f3f4f6` | `#0c0d10` | code, command blocks and other wells |
| `--surface-hover` | `#f1f2f5` | `#202127` | a row or item under the pointer |
| `--border-subtle` / `--border` | `#e8e9ed` / `#dcdee3` | `#23252b` / `#2e3037` | hairlines and panel edges |
| `--border-control` | `#858b97` | `#6a707c` | inputs and checkboxes (3:1) |
| `--ink-1` / `-2` / `-3` | `#16181d` / `#4a4f5a` / `#626875` | `#ecedf0` / `#b3b7c1` / `#8d929e` | text, strongest to quietest |
| `--accent` / `--accent-ink` | `#5e6ad2` / `#4b53c4` | `#7c86ee` / `#9aa2f6` | the one accent, and links |
| `--accent-soft` / `--on-accent` | `#eef0fc` / `#ffffff` | `#23264a` / `#0e0f12` | the selected item; text on the accent |
| `--focus` | `#5e6ad2` | `#7c86ee` | the focus ring |
| `--good`, `--warn`, `--serious`, `--critical` | `#16734a`, `#8a5a00`, `#b4461c`, `#b42323` | `#4cc38a`, `#e6b04a`, `#f08a5d`, `#f07070` | status, always with an icon and a word |
| `--*-soft` | pale tints | dark tints | status backgrounds for callouts and chips |
| `--chart-1` to `--chart-8` | `#2a78d6` ... `#e34948` | `#3987e5` ... `#e66767` | chart series, fixed per entity |
| `--chart-other` | `#8d929e` | `#6a707c` | anything without its own slot |
| `--seq-100` to `--seq-700` | pale to deep blue | deep to pale blue | a heat grid's tint |
| `--div-neg`, `--div-mid`, `--div-pos` | red, grey, blue | the same, darker | the diverging chart |
| `--grid-line`, `--axis-line` | `#eceef1`, `#c9ccd3` | `#26282e`, `#3a3d45` | chart gridlines and axes |
| `--backdrop` | ink at 32% | black at 50% | behind the drawer and search |

Every text and surface pair reaches 4.5:1 in both themes, and every
focus ring and control outline 3:1 (`tests/test_ui_tokens.py`).

### Themes

`theme-boot.js`, a classic script in `<head>`, reads `tls:theme` before
the first paint and sets `data-theme` on `<html>` to `light`, `dark` or
`system`. So a chosen theme never flashes the other one first. The
header's toggle cycles system, light, dark; picking system removes the
key. Search has the same three as commands. The CLI's HTML report keeps
its own palette (`render/html.py`'s `_STYLE`).

### Type

Inter is the sans face and JetBrains Mono the mono face, both vendored.
Each has a local fallback with metric overrides ("Inter Fallback" is
Arial at 107.12%), so the swap on load doesn't move the layout. Body
text is 14px on 1.55, with Inter's `cv11` and `ss01` and optical sizing.
Table numbers use `tnum` and `zero`, so digits line up. Titles use
`text-wrap: balance`, prose `pretty`, and prose stops at 72 characters
(`--measure`). Weights are 400, 500 and 600 only.

| Token | Size | Where |
|---|---|---|
| `--text-xs` | 11px | chart axis text, the Actions count, search's group labels |
| `--text-sm` | 12px | chips, table headers, notes, legends, tooltips, the status line |
| `--text-md` | 13px | tables, menus, popovers, toasts, the sidebar's pages, `h4` |
| `--text-base` | 14px | body text, `h3` |
| `--text-lg` | 16px | `h2` |
| `--text-xl` | 18px | the Overview's summary sentence |
| `--text-2xl` | 20px | the page title (`h1`, -0.011em tracking) |
| `--text-3xl` | 28px | a tile's value |

### Space, radius, elevation and layers

- **Space:** `--space-1` to `--space-12` are 2, 4, 6, 8, 12, 16, 20, 24,
  32, 40, 48 and 64px. Sections sit 40px apart with a hairline between.
- **Radius:** `--radius-xs` 4px, `-sm` 6px, `-md` 8px, `-lg` 12px,
  `-full` 999px.
- **Elevation:** `--elev-1` (a 1px hint on panels), `--elev-2` (menus,
  popovers, toasts and tooltips) and `--elev-3` (the drawer and search).
  In dark mode the shadows are black at 20%, 28% and 40%, and the
  lighter surfaces carry the depth.
- **Layers,** lowest first: `--z-sticky` 10 (a tall grid's header),
  `--z-sidebar` 20, `--z-topbar` 30 (the page header), `--z-popover` 40
  (menus and popovers), `--z-backdrop` 50, `--z-drawer` 60, `--z-palette`
  70 (search), `--z-toast` 80, `--z-tooltip` 90.

### Focus

One global rule draws the ring:
`:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px }`.
Table cells and rows use a -2px offset, so the ring stays inside the
grid's box. Nothing removes it.

### Motion tokens

Durations: `--dur-instant` 80ms (a press), `--dur-fast` 140ms (menus,
popovers, tooltips), `--dur-base` 200ms (toasts), `--dur-drawer` 240ms,
`--dur-page` 220ms and `--dur-page-out` 120ms (a view in and out),
`--dur-chart` 600ms and `--dur-count` 700ms. Easings: `--ease-out`
`cubic-bezier(0.16, 1, 0.3, 1)` for arriving, `--ease-in`
`cubic-bezier(0.7, 0, 0.84, 0)` for leaving, `--ease-inout`
`cubic-bezier(0.65, 0, 0.35, 1)` for moves. See "Motion".

### Icons

`icons.js` draws every icon as inline SVG on a 16px grid in
`currentColor` (`icon(name, opts)`). The scans ban emoji, arrow and
check-mark characters, so those glyphs are icons too. Status is always
an icon and a word, and each icon keeps one meaning: the octagon
(`critical`) means act on this ("Do this", a check's "Worth a look"),
the triangle (`warning`) worth considering, the circled check
(`success`) nothing to do, and the circled i (`info`) for your
information. A recommendation's severity chip sits inside its heading,
so a screen reader moving by headings hears it before the title.

### One number format

`format.js` owns every rule, so a value reads the same on every page.

| Kind | Rule | Example |
|---|---|---|
| Money | 2 decimals under 10, 1 under 100, none above; "<$0.01" for a tiny positive amount | "$12.34", "$56.7", "$1,962" |
| Share | 1 decimal; of the weekly limit, from 200% up as weeks of it | "12.4%", "about 2.5 weeks' worth of your usage limit" |
| Whole number | no decimals, even in a float column that holds only whole numbers | "24", "3" |
| Tokens | 3 significant figures, compacted, full count in the tooltip | "1.24M" |
| Duration | hours and minutes | "2h 14m" |
| Time | one absolute form (`shortTs`), or relative where freshness matters (`relativeTime`, absolute on hover) | "2026-09-23 10:44 UTC", "5 min ago" |
| Signed change | `signedPercent`, with a true minus sign (U+2212) as wide as the plus | "+12%", "−3%" |

Every amount outside a grid goes through `money`, `moneyText`,
`moneyNode` or `moneyParts`, the mirror of `units.Units.money`. A tile
puts the unit on its own line under the number ("248.7%" over "of your
weekly usage limit"). A table cell that quotes an amount uses the short
form, `units.Units.money_cell` ("222.4% ($202.33)"), always a share so a
column's rows compare. A money chart steps its ticks in the unit its
axis is written in (`moneyTicks`): 5%, 10%, 15% of the weekly limit, not
the 5.5%, 11%, 16% that round dollars come to. A grid's
money column stays a plain, sortable number with its unit once in the
header (`moneyUnit`: "$", or "list-price $" on a plan). The service
writes amounts the CLI's way ("1,962.05 USD"); `fetchJson` runs every
response through `readableAmounts`, so it reads "$1,962.05", and a unit
in a label reads "($)". Another currency reads the same both ways
("12.34 EUR"). No page writes "USD" itself.

### Readable names

A project slug (`C--Dev-claudeglass`) reads as its folder
(`projectName`: "claudeglass"). It drops the drive, a Windows home
folder and the one parent your projects share, and a worktree reads
after its project ("claudeglass / ui-redesign"). The full slug
stays in the tooltip. A slug can't tell a hyphen from a path separator,
so the name is a best guess. Model ids read as names wherever they
show: `modelName` for one id, and `modelNames` for any text (a grid
cell, a table's value, a changes table), with the id in the tooltip.
`claude-sonnet-5[1m]` reads "Sonnet 5 (1M context)". A long name
ends in an ellipsis rather than wrapping in a grid cell.

## Navigation

### Pages and segments

The sidebar opens with the three pages that answer what people come
with (Overview, Your changes, Actions), then, under a **Details**
heading (a rule in the rail), the five pages with the evidence behind
them (`group: "details"`), then two at its foot. A page with more than
one part shows them as segments: a segmented control beside the page
title. Each page, or page and segment, is a *view* with its own
address. `PAGES` in `links.js` is the one list; the sidebar, the router,
search and the README's page table all follow it.

| Page | Segments | Window |
|---|---|---|
| Overview | none | follows |
| Your changes | none | the chart follows; the cards cover all time |
| Actions | Recommendations, Checks | follows |
| Spend | Usage, Savings, Sessions | follows |
| Cache | Rebuilds, Lifetime (TTL) | follows |
| Agents & context | Subagents, Quality, Context, Hooks | follows |
| Work habits | none | follows |
| Setup | Settings, Profiles, Capture | follows, except Capture |
| Data quality (foot) | none | follows |
| Glossary (foot) | Terms, How costs work | follows, except Terms |

That makes twenty views. In text a place is written "Page › Segment"
with U+203A (`viewLabel`), such as "Spend › Sessions".

### The sidebar

216px wide, 232px from 1440px. It holds the brand, the main pages with
an icon each, and on Actions a count of "Do this" recommendations for
the window and project, counted as Actions lists them. The foot holds Data quality, the Glossary and
the status line. A toggle narrows it to a 56px rail of icons
(`tls:sidebar`), where each page's name is a tooltip on hover and on
focus. Below 1024px it is always the rail and the toggle hides. A "Skip
to the page" link before the sidebar moves focus to the page title
(`#page-title`, the one `h1`).

**The status line** gives the service's state in words beside a dot: Up
to date, Scanning your history, Checking for new sessions (a later scan
running while the figures are up to date), Last scan failed, Not
updating, Restart needed (its code changed on disk), or Can't reach the
service. While a scan runs, a line under it says how far it has got
(`scanProgressText`, from `/api/health`'s `scan`): "Found 1,204
transcript files so far", "Read 12 of 40 changed files" or "Stored 30
of 40 sessions". Under that: "Last scan", when the last scan finished;
"Figures updated", the time of the oldest figures drawn
(`X-Figures-As-Of`), both as "5 min ago" with the time on hover
(`timeNode`), moved on at each health poll; the capture level as a link
to Setup › Capture ("Capture: off" when off); and "claudeglass
<version>. Your data stays on this machine." It is a group named
"Service status", not a live region: the health banner says what
changes.

### The page header

The header stays pinned while the view scrolls, with a hairline once
content passes under it (`.is-stuck`, from an `IntersectionObserver` on
a sentinel). Left to right: the page title and its segments; **Search**
with its shortcut "Ctrl K" (added by `palette.js`); the project picker;
the window picker; the theme toggle.

Both pickers are one component, `menuControl` in `app.js`: a menu button
with radio rows. Up, Down, Home and End move through it, and a row's
first letter jumps to it. Esc closes it and gives focus back; Tab or a
click outside closes it.

### Addresses

```
#/<page>[/<segment>][?w=<window>&project=<slug>&id=&t=&row=&term=&card=&day=&split=]
```

`formatHash` writes `w` first, then `project` (left out for all
projects), then the rest in alphabetical order. `parseHash` accepts ids
of lower-case words joined by hyphens.

| Parameter | Means |
|---|---|
| `w` | the window: `1h`, `today`, `24h`, `7`, `30`, `90`, `all` or `change` |
| `project` | the one project shown; absent for all projects |
| `id` | the inbox item picked: a recommendation's `key` (its id, plus the agent type for a per-agent rule) or a check id |
| `t`, `row` | a report table (`section.table`) and the row to open at |
| `term`, `card` | a glossary term or a How costs work card |
| `day` | a UTC day: Spend › Sessions lists its sessions, Setup › Settings pulses its change |
| `split` | `model` on Spend › Usage's daily chart; absent means by agent |

**The router** (`app.js`) reads the address on load and on every
`hashchange`. Every in-app link goes through `goTo`, which adds one
history entry, so Back, Forward and bookmarks work. An address missing
the segment or window is rewritten in place to the full form. A page
named alone opens the segment last used there in this visit, else its
first. An address naming no view opens `tls:view`, else the Overview.
Picking an inbox item or a picker's choice rewrites the address in place
(`replaceParams`), so it always names what is on screen. A view hears a
new `id` through `onParams`, so a link to the view already open selects
without redrawing. Every address written carries the window and project
(`scopeParams()`).

A view is drawn the first time it is opened and kept until the window or
project changes. Views have no background poll. `history.scrollRestoration`
is `manual`: each view keeps its scroll for Back and Forward, and a link
to another view opens at the top.

**From the old tab bar.** The first visit after the tab bar was replaced
opens the view its last tab maps to in `OLD_TAB_VIEWS` (`quick` to
Actions › Checks, `diagnostics` to Data quality, `config` to Setup ›
Settings, and so on). `tls:activeTab` is then removed.

### The window picker

Its choices (`WINDOW_OPTIONS`) are Last hour, Today, Last 24 hours, Last
7, 30 or 90 days (30 by default), All time, and Since my last change.

- It is sent to every report-backed route as `window=<name>` or
  `window_days=N` (`withWindow()`), carried as `?w=`, and kept in
  `tls:window`.
- A change drops every drawn view that follows the window and redraws
  the one on screen, so no view keeps old numbers. It keeps `id` and
  drops `t` and `row`.
- The menu's note: a window counts every session with a reply in it, in
  full, so a long session that started earlier counts whole.

A view has one of three window modes (`windowMode`). It *follows* the
window by default. A segment marked `window: false` is *fixed*: the
pickers hide and the chip "Same for every window" takes their
place. Setup › Capture and Glossary › Terms are fixed. A page without
segments marked the same way would show neither; none ships.

Some panels on views that follow the window cover all history: on Your
changes, each change's card and the estimates check; on Setup ›
Settings, the baseline. Each carries an "All time" chip ("All time, all
projects" while a project is picked).

### The project picker

All projects (the default), then every project with a session in the
window, the most expensive first (`report.meta.projects` from the
all-projects report, `loadProjects()`).

- Each row reads as its folder, cut to 42 characters with both ends
  kept, with the full slug on hover. A project with no session in the
  window stays listed, marked "No sessions in this window".
- Picking one sends `project=<slug>` with every window-aware request
  (`withWindow()`, and `withProject()` for the Overview's previous
  period), and the button keeps the accent (`.is-filtered`). It lives in
  the address only and hides wherever the window picker does.
- An unknown project in the address (an old bookmark, a moved folder) is
  checked once (`checkProject`, asking `/api/sessions?limit=1&project=`).
  The dashboard then shows every project, and a toast says why.
- Panels that cover every project whatever the picker says say so while
  one is picked: Settings' changes, estimates and baseline read "All
  time, all projects".

### Banners

**The health banner** sits under the page header, from `/api/health`.
`pollHealth()` asks every 3 seconds while `status` is `"starting"` or a
later scan is running, and every minute otherwise. The banner hides
while the status is `"ok"`. Otherwise it shows the route's `message`:
the first scan's progress with a bar, or a warning with the restart
command when `"degraded"`, `"stale"` or `"outdated"` (the package's code
changed on disk; see
[docs/deploy.md](deploy.md#updating-under-a-running-serve)). When the
service answers `/api/health` with an error rather than not at all, the
banner says the service is running but can't report its state, with the
error's message, not "Can't reach". When `code.id` changes between polls
(the service restarted on new code), the banner says so and offers
**Reload page**, so the page's own scripts match. When a scan finishes
(the first one, or a later one that stored sessions), the banner and the
status line offer **Redraw figures**, which drops every drawn view and the report cache
and redraws the view on screen. Views never redraw under the reader.

**The capture banner** sits under it (`#capture-banner`,
`role="status"`). It gets `/api/health`'s `capture` block and fetches
`/api/capture` when that changes, or every five minutes. It shows only
when there is something to act on: the end time has passed, a hook
entry is missing, no notes have been seen, Claude tags too few messages,
enough has been collected to lower the level, or capture's weekly cost
can be weighed against what depends on it. It also carries the
`/tl-feedback` reminder while that item is on, and the `capture feedback
on` or `capture brief on` command while a skill's file needs installing.
It leads with a headline ("Metrics capture: Essentials · since <date> ·
N tokens · <amount> (x% of spend) · tagged on P% of messages") and links
to Setup › Capture and Work habits. **Dismiss for a week** hides the
notes until they change.

**The Setup card** tops the Overview while a part that matters isn't
working yet (`/api/setup/status`, `renderSetupCard`): how you pay, the
connection to Claude Code, the dashboard at logon, and capture when it's
on. It is one line naming those parts, so the Overview's own answer
still comes first; "Show the steps" opens each part's problem and the
command that fixes it to copy. The dashboard at logon says why it
matters: Claude Code deletes transcripts after `cleanupPeriodDays`. It
shows before any session is read too, and says "Setup is almost done"
when all that's left is waiting for the first Claude Code session. Data
quality has the whole checklist.

## Pages

Each view opens with its one-line intro from `PAGES` (`viewIntro`).

### Overview

**Answers:** "Is anything wrong, did my changes work, and where do my
tokens go?" The three questions people open the dashboard with, in
that order, each as a heading with a one-line answer beside it.

1. **The summary sentence** (18px): what the window cost, the change on
   the period of the same length before, and what the changes worth
   making come to, in the billing mode ("you spent $2,663", or "you used
   about 38% of your weekly usage limit"). The ways to save overlap, so
   `availableSaving` doesn't add them up: each is a share of the spend it
   comes from (an agent type's, from `model_swap_by_agent_type`, or all
   of it), taken from what the others leave (`combinedSaving`). A cheaper
   model prices the fewer tokens earlier summaries leave, so the two
   multiply. An action a Savings lever already counts (`LEVER_RULES`) is
   left out. The total is still "at most", and never above the spend. The previous period comes
   from `/api/summary?since=&until=`, so a change compares a summary
   with a summary. "All time" and "Since my last change" have none.
   Other forms cover no sessions in the window or project, no change
   recorded yet, and, before any session is read, **What ClaudeGlass
   does for you** (saying the first scan is running while
   `scan.scanning` is true).
2. **Anything wrong?** One checklist (`checklistRows`): a row for each
   of Actions' checks (`/api/quick-actions`), carrying the Actions items
   (`groupRecommendations`) its `rule_ids` raised, so a check and the
   recommendation it leads to are one row; an item no check draws on is
   a row of its own. Each row has its state in Actions' icon and word
   (Do this, Worth a look), its area and what's wrong, what fixing it
   saves ("Saving not worked out" when nothing priced it), a **Copy
   prompt** named for it when it has one prompt, and a link to the fix
   or the check. Rows to fix come first, then those worth a look, by
   saving. The checks with nothing to do, and those with too little
   data, fold into one line each. Beside the heading: "1 thing to fix ·
   3 worth a look · 5 checks fine · 3 without enough data".
3. **Did your changes work?** The latest two changes from `/api/impact`
   as short cards (`renderChangeCards` with `compact`): what changed and
   where, the lead measure before against after, how sure the
   difference is, and what it saved so far. Beside the heading, a link
   to all of them on Your changes.
4. **Where do your tokens go?** Three tiles, each linking to its page:
   Spend, every session with a reply in the window at its whole cost,
   with its change and a daily sparkline; Saved by cache reads
   (`cache_saved`, an estimate, before paying for the cache writes:
   Cache › Rebuilds leads with the saving after them, so its figure is
   smaller and its label says so); and Sessions, with the subagent runs.
   Then **Daily spend** (chart 1) at the page's width, with your
   settings changes from `/api/impact` as labelled rules. It counts
   replies by the UTC day they were sent, over every day of the window
   (`windowDays`); when the window's whole sessions come to a different
   figure, its reading gives that too and says why. A day opens Spend ›
   Sessions and a change Your changes. Under it, spend **by project**
   (the usage section's `by_project` table) and **by model** (the daily
   rows summed), side by side, each left out when it has one row.
5. **Scores, totals, and how amounts are counted** (folded): the billing
   mode and why (`report.meta`), the scorecard's five areas as a table
   (`scorecard.dimensions`, which a recommendation's evidence can point
   at), and `overview.totals`.

Every load starts at once; the drawing waits for the report, which sets
the billing mode. A newer draw drops an older one's answers.

### Your changes

**Answers:** "Did each change I made work, by how much, and how sure is
that?"

1. **Is each reply cheaper since your changes?** (chart 9): cost per
   reply a day over the window, as dots, with each change from
   `/api/impact` as a labelled rule and a flat line at the average of
   each period between two changes. A change's label carries its step
   ("Model changed: −21% a reply") and opens its card; a change made in
   another project than the one picked names it. The lines are the
   chart's own sums over the projects shown, and a change's own day
   counts after it. A day opens Spend › Sessions.
2. **Each change, before and after** (All time): a card per change
   (`changeCard`), newest first. What changed, when and where
   (`modelNames` for the values); its lead measure's reading as a chip;
   each measure the change should move as two bars on one scale
   (`before_value` and `after_value`), the change as a signed percent,
   and the ratio test's reading coloured by the measure's `better`
   (Lower is good news for a cost; the share of messages tagged has no
   better side and reads neutral). Then **Saved so far** from
   `without.saved_usd` ("Cost more so far" when it's negative) with how
   it was priced, a row per setting when several changed at once, the
   quality verdicts with every signal folded, and the command that
   undoes an `apply` or a capture change. Until each side has
   `min_sessions`, the card is the "not enough data yet" box with how
   many it has (`item.gate`). A `?day=` pulses that day's card.
3. **Did your estimates come true?** (`/api/backtest`, All time): each
   estimate Profiles showed, against what happened.

### Actions › Recommendations

**Answers:** "Which changes are worth making, and how do I make them?"

An inbox from `/api/recommendations`: the list (360px, in view while the
detail scrolls) and the one picked.

- **Filters** narrow the list by importance (Do this, Worth considering,
  For your information) and by area (Models, Cache, Context, Agents,
  Habits, Data and settings), each with its count. The area comes from
  `RULE_AREA` in `page-actions.js`. Once you've ignored something, a
  **Show: To do · Ignored** row comes first, and the other filters count
  what it leaves.
- **Groups.** A rule that fires per agent type (`ttl-switch`, `spawn-*`
  and the rest) is one item for all of them ("7 agent types are sent
  your CLAUDE.md files every time they start").
- **The detail** has the severity chip inside the `h2`, then chips for
  who it's for, its area and where the change lands. **How this saves
  you money** gives what it costs now, what the change does to the price
  with your multiplier ("Reading from the cache costs a tenth of the
  input price"), and the saving with its basis chip. **What to do** gives
  the action, with a table when there is more than one change; in a
  group, picking a row shows that agent's change. Then the fixes as
  command blocks (a `scope: "managed"` card says your organisation's
  policy sets it), **The numbers behind this** as evidence links,
  **Not for you?** with **Ignore this recommendation**, and **The check
  this answers**.
- **Ignoring** (`ignores.py`, `POST /api/recommendations/ignore`) hides
  an item in the project on screen, or in every project from the
  all-projects view, while the profile `apply` last marked active stays
  active. It shows again when it starts suggesting something else, and
  its detail then says why ("It shows again because it now suggests
  120000 (you ignored 100000)"). An ignored item's detail says when,
  where and under which profile it was ignored, with **Stop ignoring**
  (**Stop ignoring in every project** when an every-project ignore is
  seen from one project). Ignored items leave every other list: the
  Overview's checklist, the Actions badge, search, "Feeds N
  actions" and the checks' links (`groupRecommendations` leaves them out
  unless asked), and **Start from my recommendations**. The Overview's
  available saving comes from the report's tables, so it still counts
  them.
- A notice above the inbox says the figures are provisional while a
  baseline capture window is open. An id the window doesn't have opens
  the first item with a note.

### Actions › Checks

**Answers:** "For each way of saving tokens, is there anything to do?"

`/api/quick-actions` in the same inbox. Each check is a question with a
status (Worth a look, Nothing to do, Not enough data), filtered by
status. The detail gives why it matters and the answer, then loads
`/api/quick-actions/<id>`: **The numbers**, the fixes, **Habits that
help**, and **The recommendation it leads to**. A check with not enough
data says why. The same checks run as `claudeglass check`.

### Spend › Usage

**Answers:** "Is spend rising, and on what?"

Chart 1 with **Split by**: main session and subagents, or model tier
(`?split=model`, kept through a window change and a visit elsewhere). A
day leads to its sessions and a change marker to what it did. Then cost
by model (`overview.by_model`, placed by `TABLE_PAGE_MAP`), the
`usage`, `elasticity`, `compactions` and `phases` sections, and
`/api/compactions`, newest first. `elasticity` shows only under
subscription billing with usage-limit readings. `phases` ("Where the
work went") is always built for the dashboard (the CLI needs
`--phases`): Cost by phase up front, and the main session, subagent and
workflow-agent split and the per-agent-type split under More tables.

### Spend › Savings

**Answers:** "Which change saves the most, and how sure is it?"

Chart 2 heads the page: the four ways to save side by side, hatched
unless measured. A bar leads to the row its figure comes from. Then five
sections, each from its own route rather than the full report:

- `/api/carry`: what tool output kept in context costs, and what a cap
  would save;
- `/api/compaction-sim`: the conversation-summary sweep, the best size
  per agent type and the fidelity check, with chart 3;
- `/api/plan-handoff`: what building in a fresh session after each big
  approved plan could have saved (no bar in chart 2: it overlaps with
  the conversation-summary saving);
- `/api/model-swap`: the most a one-tier-cheaper model could save,
  counting for each subagent only the runs its agent file's model
  decides;
- `/api/waste`: spend on replies whose output was never used.

Their recommendations show on Actions › Recommendations, not here.

### Spend › Sessions

**Answers:** "Which sessions were expensive, and why?"

Chart 4 plots every session in the window (the newest 2,000 at most,
with a note when there are more) by start time and cost on a log scale,
coloured by work mode. Its reading says how many cost nothing, or have no
start time, and so aren't plotted. Dragging across it, or Shift with the arrow keys,
lists only the sessions that started then. A `?day=` lists the sessions
active that UTC day. A line above the list says what it is narrowed to,
with **Show all sessions**. A row and its dot light up together. The
report's `sessions` section follows.

**Detail:** a row, Enter or a dot opens the session drawer
(`openSessionDrawer`, from `/api/session/<id>`): a summary; **Why was
this session expensive?** (`/explain`); "Mode override" and "Purpose
override" with **Save tags**; **Rate this session** while the dashboard
rating is on; chart 5; and **Transcripts**, with no path. Chart 5 draws
context size over turns with a marker shape per event (cache rebuild,
conversation summary, subagent start, your message). Usage-limit events
sit in lanes above, placed by time because they fall between turns. It
says so when there is no stored transcript digest, or when the service
thinned a long session (`truncated`).

### Cache › Rebuilds

**Answers:** "Why did Claude Code rebuild the cache, and what did it
cost?"

**What the cache does for you**: three tiles, each with its price
multiplier from `report.meta.rates` and a link to its card in Glossary ›
How costs work. What cache reads saved (an estimate); what avoidable
rebuilds cost and how many of the window's rebuilds that covers ("431
of the 457 ... were avoidable"), leaving out the usage-limit pause as
the cost does (`avoidableRebuilds`, shared with the Glossary); and how
many agent types a 1-hour lifetime would help. Then the `recache` and
`limits` sections for the window; `recache` draws chart 6. Every figure
follows the window: the rebuilds by cause are `recache`'s "Why the cache
was rebuilt", named as the server names them (Cache expired, Cache
broken by a change, Expired during a usage-limit pause), and All time
in the window picker gives the whole history.

### Cache › Lifetime (TTL)

**Answers:** "Would a 1-hour cache lifetime pay for itself?"

`/api/ttl`, with chart 7 over its tables: observed and simulated cost
per agent type, the 5-minute or 1-hour recommendation and its fidelity,
as the CLI's `ttl` command shows them.

### Agents & context › Subagents

**Answers:** "What do my subagents cost, what are they given, and should long runs be split?"

The `agent_startup`, `agents` and `run_split` sections. `agent_startup`
draws chart 8. Then cost per run, skills and MCP cost, effort, and what
each agent never used. `run_split` ([run splits](run-split.md)) gives
each agent type's best split interval and what splitting its long runs
there would save.

### Agents & context › Quality

**Answers:** "Is the work going well?"

The `quality`, `workflows` and `workstyle` sections. `quality` comes
first: signals per agent type, then per model and effort against the
setup that agent used most ([concepts](concepts.md#7-quality-signals)),
then **Agent runs retried on a larger model** and **Why agents were run
again**. The per-agent grid is a heat grid (`TINT_TABLES`): each share
is shaded against its column's largest, with the value shown.

### Agents & context › Context

**Answers:** "What does Claude read at the start, and what can go?"

- **CLAUDE.md files** (`/api/claude-md`): one row per file with who reads
  it, its size, how often it was sent, the cost and its fixes. A row
  opens a drawer (`/api/claude-md/<id>`): sections by size, duplicates,
  stale references and fix prompts.
- **Skills** (`/api/skills`): the listing's size and cost, one fix that
  hides every unused skill when there are two or more, and a grid with
  "Show only skills Claude never used". A row opens a drawer with the
  skill's description, facts and fixes.
- The `context_budget` section.
- The `tool_search` section ([tool search](tool-search.md)): what MCP
  tool search saved by keeping tool definitions out of each request,
  overall and by MCP server. The same question is a check on
  Actions › Checks.

The terminal equivalent is `claudeglass review claude-md|skills`.

### Agents & context › Hooks

**Answers:** "Do my hooks work, and what do they cost?"

The `hooks` section ([hooks](hooks.md)): one summary row, then each hook
by its script's file name, costliest first. For each: the events it runs
on, failed runs and why (a script not found by a relative path is
marked), runs seen working, calls it blocked and how many Claude sent
again unchanged, the context it added and what keeping it cost, and time
waited. The same question is a check on Actions › Checks.

### Work habits

**Answers:** "Which ways of working would save the most?"

The `habits` section: the **Weekly pace** digest; **Habits worth
trying** as cards (saving a week, what your sessions show, an example to
copy, how often it was seen, its source, confidence, a weekly pace line
and how the saving is worked out); the brief templates with Copy
buttons; **Kinds of task**; the other breakdowns under More tables; and
the notes. Nothing here changes a setting.

### Setup › Settings

**Answers:** "What is set, where, and how does this window compare with
my baseline?" What each change did is on Your changes, which a line at
the top links to.

1. `/api/config-diff?auto_keys=1`: which layer supplied each key, which
   projects share one effective config, and the per-key diffs.
2. **Latest baseline** (`/api/baseline`): the capture window's status,
   the latest baseline and the history, marked provisional while a
   capture window is open.

### Setup › Profiles

**Answers:** "Which group of settings fits a goal, and what would it
change?"

**Save my current settings as a profile** comes first. **Create a
profile** turns a goal (`/api/profile-goals`) into a table of changes
with the ones your data supports ticked; each tick asks
`POST /api/whatif` again. **Your profiles and the built-in ones** marks
the one the latest baseline suggests. **Show what it changes** opens a
drawer (`/api/profiles/<id>/diff`) with a Setting / Now / After / Set in
table, **Estimated effect**, and "Ask Claude to do it", "Or run this
command" and "Or try it for one session", each with Copy. Then **Best
setup for each kind of task** (`habits.habits_setups`) and, folded,
**Edit settings directly**. The page never runs a command and never
fills in a project folder.

### Setup › Capture

**Answers:** "How much should Claude tell ClaudeGlass, and what does that
cost?" The same for every window.

`/api/capture`: the cost warning; where capture stands (setting, cost so
far, how often Claude tagged); its weekly cost against what depends on
it (`roi`); a warning with `capture connect` when a hook entry is
missing; the level cards (Off, Free, Essentials, Standard, Deep, Custom)
with weekly estimates; sampling and end time; and every metric grouped
by where it is captured.

The end-time menu's first entry is the end already set ("In 12 days:
2026-10-07 09:00 UTC", or "Ended: ..."). A choice saves the moment it is
picked, and its days count from then, not from when capture started.
While capture is off the menu is hidden and a note says the first switch
on ends by itself after `timebox_days` (14).

A group folds ("Main session (3 of 12 on)") unless a metric in it needs
a hook entry or an install. The feedback and brief skill rows show
**Needs installing** with their `capture ... on` command: the dashboard
never writes Claude Code's folder. A change that asks Claude for more
repeats the cost warning in a dialog first. Changes go to
`POST /api/capture`; when the file can't be written, the view shows the
CLI commands instead.

### Data quality

**Answers:** "What did ClaudeGlass install, and can I trust its figures?"

1. **Your setup** (`/api/setup/status`, `renderSetupList`): each part
   `Done`, `Waiting`, `Off` or `Needs attention`, with the command that
   fixes it, as `claudeglass status` prints it.
2. **What this tool installed, and what to expect** (`/api/setup`): each
   thing installed, what it does, its token cost and how to undo it, and
   "Remove everything".
3. **Service health** (`/api/health`, `renderHealth`): status, version,
   "Code on disk" (the same as running, or when it changed and to which
   version), last scan, the watcher's counts and recent errors.
4. **Claude Code's own cost record** (`cost_record`, mapped here): its
   cost for each session against ClaudeGlass's for the same replies, with
   the stopped replies and unlogged requests that explain part of any
   difference, as `claudeglass check cost-record` reports it. Then any
   report section no other view claims (`SECTION_PAGE_MAP`'s fallback).
5. `/api/diagnostics`: whether the hook and the status line work, then
   the parse-quality counters, matching the CLI report's Diagnostics.

### Glossary › Terms

**Answers:** "What does this word mean?" The same for every window.

`GLOSSARY` in `links.js`, word for word the README's glossary. **Find a
term** above the list narrows it as you type: an entry stays when every
word typed is in its term or definition. A status line counts the
matches; with none, the page says so and offers **Show every term**.
Esc empties the field. A `?term=` link clears a filter that hides its
entry.
`?term=<slug>` scrolls to an entry and pulses it. A term a How costs
work card names also carries "Why it matters": the card's rule sentence
(`cardRuleText`), with a link to the card.

### Glossary › How costs work

**Answers:** "How is each kind of token priced, and what does each
change save?"

One card per `COST_CARDS` entry: cache reads, cache writes and lifetime
(TTL), cache rebuilds, model choice, startup context, tool output kept,
conversation summaries, and billing mode. Each states the rule with the
multiplier from your pricing (`priced()`, never a typed number), your
figures for the window, and a link to what acts on it. `?card=<slug>`
scrolls to and pulses a card.

## Components

`ui.js` holds the pieces every page is built from, and `grid.js` the
data grid. Each has a loading, an empty, an error and a stale state.
Every helper builds nodes with `textContent`; server text goes through
`prose()` (see "Linking").

| Component | Helper | What it does |
|---|---|---|
| Button | `button` | a label that says what happens ("Copy prompt"). Primary, quiet, icon-only (with a name) or link. Refuses "Apply ..." |
| Severity chip | `severityChip` | Do this, Worth considering, For your information: an icon and a word |
| Status badge | `statusBadge` | a check's status, the same way. "Worth a look" takes the amber of Worth considering, not the red of Do this |
| Basis chip | `basisChip` | Estimate, At most, Simulated, Calibrated; a measured figure has none |
| Delta chip | `deltaChip` | a change on the previous period, coloured by whether up is good, neutral within 1%. Three times or more reads "3.2 times"; an empty earlier period "None before" |
| Tile | `tile`, `tileRow` | a label, the value at 28px with its unit quieter, then an optional basis chip, delta, hint and sparkline |
| Panel | `panel` | a surface with a hairline border, a header and a body. Never nested |
| Callout | `callout`, `errorNotice` | info, success, warning or critical: a tint, an icon and a label. An error says what happened and offers "Try again" when a retry can help; a `restart_needed` error is titled "ClaudeGlass needs a restart." |
| Empty state | `emptyState` | what happened, why, and what would fill it. Never "No data" |
| Skeleton | `skeleton`, `loadingNode` | grey bars in the shape of what is loading, with a 1.4-second shimmer. `loadingNode` puts what is loading above them in words ("Loading the report…"), which stay when reduced motion stops the shimmer |
| Command block | `commandBlock`, `renderFix` | the ways to make a change as a tab list (a prompt, a dry-run command, a one-session trial), each with Copy, then `fixes.build_fix`'s explainer and `fixes.RESTART_NOTE` |
| Popover | `popoverButton`, `helpButton` | the (i) and (?) help. Closes on Esc, a click outside, or a link inside |
| Tooltip | `attachTooltip` | on hover and focus after 40ms, value first; a text tip is also the element's `aria-describedby` |
| Drawer | `drawer` | a modal `<dialog>` from the right, 560px or 720px, with a title, close and Esc, and optionally "Copy a link to this". Focus stays inside and returns to the opener |
| Toast | `toast` | one at a time, bottom right, `role="status"`. Goes after 3.5 seconds, waiting while hovered or focused |
| Confirm dialog | `confirmDialog` | a native `<dialog>` before a change with a warning |

### Data grid

Every table on every page is `dataGrid`.

- **Cells and sort.** Numbers right-aligned in even-width digits; short
  text stays on one line and sentences wrap. The sort is kept per table
  (`tls:sort:<table>`). Until one is picked, a table the server lists
  A to Z opens biggest first on its lead measure (a total row stays
  last); dated, bucketed and grouped tables keep their order.
- **Lead columns.** A table of more than 8 columns shows its first 7 (or
  its `lead_columns`), with a chooser for the rest (`tls:cols:<table>`)
  and a pinned first column while it scrolls sideways. Headings wrap to
  three lines, a long unit ("list-price $") on a line of its own, so a
  heading grows down rather than across. A table still too wide for its
  box first draws tighter (`.grid-snug`: less padding, a shorter bar),
  which fits every report table at 1280px; only then does it scroll,
  with a shadow at each edge with more to scroll to.
- **Inline bars.** The lead measure carries a thin bar, so a ranking
  needs no chart. A table in `TINT_TABLES` shades values instead.
- **Long tables.** A report table of more than 12 rows opens on its
  first 10, with "Show all N rows"; a grouped table stays whole. A dated
  table listed oldest first (`NEWEST_LAST`) opens on its latest 10 until
  sorted. The fold always takes in the rows an action cites, and an
  evidence link to a hidden row shows them all first.
- **Tall tables.** Past 20 rows a table scrolls in its own box with the
  header pinned. Past 200 only the visible rows are drawn.
- **Row pulse.** An evidence link's row scrolls into view and glows for
  1.2 seconds (`pulseRow`). Under reduced motion the glow holds still.
- **Summaries and notes.** A one-row table with `lead_columns` reads as
  up to four tiles, with **All figures (N)** under them. One or two short
  notes stay in view; more fold into **How these figures are worked out
  (N notes)**, or **How this table is worked out (N notes)** for one
  table's own, so the two don't read alike a few lines apart. A section
  intro the page already opens with isn't repeated, and an empty chart
  says why once, in its box (its summary stays for a screen reader).
- **Placement.** `Table.dashboard`: `keep` shows a table, `advanced` puts
  it in one folded **More tables (N)** per section, and `report` leaves
  it to the CLI report with a note whose buttons open it in the table
  drawer. A table empty for a known reason says it (five-hour blocks on
  API billing).
- **Nothing to show.** A table whose every figure is zero or blank (a
  summary is judged by its tiles) shows the grid's "Nothing to show"
  note, not tiles or rows of zeros (`tableIsEmpty`). A section whose
  tables are all like that is its heading, intro and that one note; one
  whose shown tables are, but whose **More tables** aren't, says it once
  above them.
- **Help and labels.** A column's (?) is a real `<button>` that never
  sorts; on a heading that sorts it leaves the Tab order and **?** opens
  it. The heading's name is its label and unit. Controls every grid has
  end their names with the table's title (an evidence mark, its row's).
  `Table.value_labels` turns raw values such as `top-level` into
  "Main session", with the raw value in `data-raw`.

A section whose table a catalogue chart reads draws that chart between
its intro and its tables. `grid.js` can't import the charts (they draw
their table view with it), so `app.js` hands it `sectionChart` through
`setSectionChart`.

### Search and keyboard shortcuts

**Search** (`palette.js`; Ctrl+K or the Search button) is a modal
`<dialog>` near the top of the window. Its groups:

| Group | Opens |
|---|---|
| Pages | every page and segment |
| Commands | Set window: …, Show all projects, the three themes, Show keyboard shortcuts, Copy prompt: … |
| Recommendations, Checks | the item, selected in its inbox (`?id=`) |
| Sections and tables | a section at its first table; a table where it is shown, or in the table drawer |
| Glossary | terms (`?term=`) and How costs work cards (`?card=`) |
| Recent sessions | the window's 20 newest, each in its drawer |
| Projects | each project, shown on its own |

With nothing typed it lists the pages, then the commands. Each typed
word must match a name or its other words: at the start first, then the
start of a word, inside a word, and last as letters in order with at
most two gaps ("rbld" finds "rebuild"). Each group shows its 8 best, the
best group first, with the typed words in bold. If the clipboard is
refused, Copy prompt opens the recommendation. Search never changes
Claude Code.

The box is an ARIA combobox: focus stays in it while Up, Down, Page Up
and Page Down move `aria-activedescendant` through a `listbox` of
`group`s. Enter opens; Esc or a click outside closes, and focus returns.
The list is `aria-busy` until the window's actions, tables and sessions
arrive, fetched once per window and project. A polite status line gives
the count.

**Shortcuts** (`?` shows them in a sheet):

| Keys | What they do |
|---|---|
| Ctrl+K | Search |
| G, then O, Y, A, S, C, E, H or U | Overview, Your changes, Actions, Spend, Cache, Agents & context, Work habits, Setup (`GO_KEYS`, within 1.5 seconds) |
| `[` and `]` | The previous or next segment |
| J and K | The next or previous item: the Actions inbox, or the first grid whose rows open something. Enter opens it |
| `?` | The shortcut sheet |
| Esc | Closes a drawer, menu, popover, search or the sheet |

A key typed in a text box, pressed while a dialog, menu or popover is
open, or pressed with Ctrl, Alt or the Windows key is left alone (Ctrl+K
apart).

### Service unreachable

When the service stops answering, a callout under the header says so and
retries after 2, 4, 8 and 16 seconds, then every 30 (`RETRY_SECONDS` in
`shell.js`). The last figures stay, dimmed and marked stale. Once the
service answers, the failed loads run again (`retryOnReconnect`) and the
callout goes.

## Charts

`charts.js` is the frame every chart shares; `charts-types.js` draws the
marks. A page draws a chart with one call,
`renderChart(container, key, data, opts)`, and nothing else builds one.
Every chart is inline SVG drawn by d3 from JSON the API already returns.

### The catalogue and the rule for adding a chart

`CHART_SPECS` in `charts.js` is closed at these nine rows.
`tests/test_service_static.py` fails if a row is added or removed without
its own list changing too.

| # | Key | Question (the title) | Data | Form | Where |
|---|---|---|---|---|---|
| 1 | `daily-spend` | Is spend rising, and did my changes move it? | `/api/daily-usage` | stacked columns by agent or model tier, a rule per change | Overview, Spend › Usage |
| 2 | `savings-levers` | Which change saves the most, and how sure is it? | the `carry`, `compaction_sim`, `model_swap` and `waste` totals | horizontal bars, hatched when not measured | Spend › Savings |
| 3 | `summary-point` | Would summarising conversations at a different size cost less? | `compaction_sim.compaction_sim_by_window` | dashed line, "Now" and "Cheapest" marked | Spend › Savings |
| 4 | `session-outliers` | Which sessions are the expensive outliers? | `/api/sessions` | log-scale scatter by work mode, with a time brush | Spend › Sessions |
| 5 | `session-context` | Where in this session did context grow or reset? | `/api/session/<id>` | a line with a marker shape per event, limit events above | the session drawer |
| 6 | `idle-gaps` | Do idle gaps outlast the cache? | `recache.recache_gap_buckets` | histogram with 5-minute and 1-hour rules | Cache › Rebuilds |
| 7 | `lifetime-by-agent` | Which agent types are cheaper on a 1-hour cache lifetime? | `ttl.ttl_break_even_share` | diverging bars around zero | Cache › Lifetime (TTL) |
| 8 | `startup-context` | What fills each agent's context before it starts? | `agent_startup.agent_startup_breakdown` | stacked bars, at most 12 agent types | Agents & context › Subagents |
| 9 | `change-timeline` | Is each reply cheaper since your changes? | `/api/daily-usage` and `/api/impact` | cost per reply a day as dots, a flat line at each period's average between changes, a rule per change with its step | Your changes |

A new chart needs a new row, and a row must pass both tests:

- it shows at a glance what a sorted grid with inline bars can't: a trend
  over time, a distribution against a threshold, the sign across
  entities, a part-to-whole of 8 or fewer parts, or outliers in two
  dimensions;
- it leads somewhere: an action, a drawer or a filtered grid.

A ranking is a grid with an inline bar, never a chart. There are no
pies, donuts, treemaps, gauges, calendar heatmaps, 3D or dual-axis
charts. A chart with fewer than 3 points (`MIN_POINTS`) says so instead
of drawing, except daily spend, where one day is still a reading.

### The frame

Every chart has the same parts, top to bottom:

- the question as its title (`h3`) and a **Show as table** toggle
  (`aria-pressed`) that swaps the plot for a grid of the same rows;
- a summary sentence from the data (`fillSummary`), which is also the
  plot's accessible name;
- a legend when there are 2 or more series, each entry a swatch or the
  marker's shape, so no series relies on colour alone;
- the plot: bars at most 24px thick (`MAX_BAR`) with a 4px rounded end,
  2px lines, hairline gridlines, a 2px gap between stacked parts;
- a note for what the reader needs to trust it, such as "Days run
  midnight to midnight UTC" or why a bar is hatched.

On daily spend, today's column is marked "so far" in the margin above
the plot, over the change labels; each change label sits right of its
rule, else left, else is cut to fit, so labels never overlap.

Money axes use `moneyAxis`, the chart mirror of `Units.money`: "% of
your weekly usage limit", "list-price $", or plain "$" on the API. Token
axes compact ("1.2M").

### Colour follows the thing, not its place

`ENTITY_COLOURS` fixes a colour per entity, so a new window never
repaints what stays on screen:

| Entity | Colours |
|---|---|
| Agent | main session `--chart-1`, subagents `--chart-3` |
| Model tier | Opus (and Fable) `--chart-1`, Sonnet `--chart-2`, Haiku `--chart-3` |
| Work mode | interactive `--chart-1`, long agentic `--chart-2`, overnight `--chart-3` |
| Startup part | system prompt, tool definitions, CLAUDE.md, skills listing, tool lists, task prompt and hook context take `--chart-1` to `--chart-7` |

Anything else is `--chart-other`. A chart never picks colours by rank.
Chart 7 shows a sign, not an entity: an agent type that saves with a
1-hour lifetime is `--div-pos`, one that costs more `--div-neg`.

### Reading a chart

- **Pointer.** A tooltip shows the value, then the label. Every mark has
  a hit area at least 24px across. Clicking a mark that leads somewhere
  (`opts.open`) opens it. On a section chart, `markLeads` sends a mark to
  the first action whose evidence is its row, else to the row below.
- **Keyboard.** The plot is one tab stop. Arrow keys move a cursor from
  mark to mark and read it in the tooltip; Home and End jump to the
  ends; Enter opens the mark; Esc lets go. On the session scatter, Shift
  with the arrows picks a range. A change label on a daily spend chart is
  its own link, the next Tab stop ("<change>, changed on <day>: see what
  it did"). Every other layer is `aria-hidden`.
- **Brush.** On the session scatter, dragging across a time range calls
  `opts.brushed`, so the grid below lists only those sessions.
- **Linked highlight.** A grid given `link: {scope, key(row)}` shares
  hover and focus with the chart's marks of the same scope (a day, a
  session, an agent type) through `highlight` and `listenHighlight` in
  `core.js`. `swatch(row)` puts the entity's colour in the first cell.

### Micro-forms

Not in the catalogue, because each sits inside another component:
`sparkline(values)`, a trend in a tile with no axes; `meter(level)`, a
level out of 5 as segments (`role="meter"`) with the level and status
written beside it; and `habitSparkline(weeks, label)`, the weekly pace
line on a habit card.

## Linking

Every number leads to its evidence, and every table says what it feeds.

- **Evidence links** (`evidence.js`). A recommendation's evidence names a
  report table and a row (`[label, value, "section.table", row_key]`).
  The link opens the view `TABLE_PAGE_MAP` names for the table, else the
  one `SECTION_PAGE_MAP` names for its section, with `?t=&row=`.
  `revealEvidence` waits for the table, opens the More tables or detail
  that hides it, scrolls to the row and pulses it. The scorecard is a
  table in the Overview's folded details, so its areas pulse the same
  way.
- **The table drawer.** A table no page shows (placed `report`, a section
  no page shows such as `savers`, or a table not drawn for this window)
  opens in a drawer (`tableDrawer`): the table, the row pulsed, and
  where else it appears. A test checks every evidence table a rule can
  name resolves one of these ways.
- **Feeds N actions.** `actionIndex()` (`api.js`) indexes the window's
  recommendations by the tables and rows they cite. It reads
  `loadRecommendations()`, one fetch per window and project, shared with
  the badge, the inbox and the Overview. A cited table shows **Feeds N
  actions** by its heading, and each cited row a mark in its first cell;
  both list those actions, each linking to its detail (`?id=<key>`). N
  counts inbox items, so a per-agent rule is one action.
- **Page links in text.** Server text may point at a page with
  `{{page:<page>}}` or `{{page:<page>/<segment>}}`
  ([`docs/writing-help.md`](writing-help.md), "Linking to another
  page"). `prose(text, seen)` renders each as a link named as the
  sidebar names the view, and every place server text shows uses it.
  Where a link can't go (a tooltip, a toast, a grid cell), `plainText`
  gives the page's name, as `pages.plain()` does for the CLI. A token
  naming no page is left as written.
- **Glossary terms.** With a `seen` set, `prose()` finds the words in
  `JARGON` (each a `GLOSSARY` term with its other forms). The first use
  in a card or section becomes a button with a dotted underline that
  opens the definition, with a link to its entry. Page intros, the
  Glossary and popovers show none.
- **How costs work cards.** A section whose figures rest on a price the
  Glossary explains (`SECTION_CARDS` in `grid.js`) ends its help popover
  with a link to that card. The Cache › Rebuilds tiles link there too.
- **Popovers and drawers close on a link** inside them.

## Help and labels

The page title is the one `h1`. Sections are `h2`, tables `h3`, and
nothing goes deeper than `h4`; a table moved to another view takes the
`h2`. The words come from `helptext.py`, applied by `helptext.annotate`:
`Section.intro` under the heading; `Section.help` and `Table.help` in
one (i) popover ("What it shows", "How to read it", "When to act");
`Column.help` behind a column's (?).

## Motion

Motion explains a change and never decorates. Only `transform` and
`opacity` move, and none of it runs under reduced motion or in a hidden
browser tab.

### A new view

`app.js`'s `changeView` runs the route change inside
`document.startViewTransition` only when the browser has it, the view
really changes, motion is welcome (`motionOK()`), the page is visible,
and no dialog is open.

- The old view fades out over 120ms (`--dur-page-out`, `--ease-in`); the
  new one fades in over 220ms (`--dur-page`, `--ease-out`), rising 6px.
- While it runs, `html` has `.view-changing`, which names the views, the
  sidebar, the page header and the toasts. Only the views fade; the rest
  changes at once.
- The old view keeps its place however far the page scrolls for the new
  one (`--view-shift`). Focus and scroll are still `showView`'s.
- While a transition runs, Chromium hit-tests only the page root. So a
  click in those 220ms ends the transition and goes to what is under
  the pointer (`passClickThrough`).
- `html { scroll-behavior: smooth }` applies only when motion is welcome.

### The Overview's entrance

- The headline figures count up over 700ms (`countUp`, expo out): from 0
  the first time, from the last figures shown after a window change.
  Each tile is drawn with its final text first. A timer 100ms after the
  end, or the browser tab being hidden, finishes the count, so the final
  text always stays.
- The daily spend chart draws in 80ms after the figures start.
- The checklist's rows arrive 24ms apart from 160ms (`enterInTurn`,
  `.is-entering` with `--enter-delay`). A seventh row or later arrives
  with the sixth.
- The count-up and the stagger do nothing on a view with no layout box.
  Figures that land after you have left the Overview show as they are.

### A new window: hold and morph

While a new window or project loads, `holdChart` keeps each chart's
last drawing at 0.55 opacity, so nothing jumps. Then `renderChart` keeps
the frame and moves the marks to their new places over 600ms
(`MORPH_MS`), unless there are more than 1,500 (`MAX_MORPH_MARKS`). A
first drawing draws in once over 600ms (`DRAW_IN_MS`). `opts.delay`
holds a draw-in or morph back. A chart resized mid-draw carries on to
the new size in the time it had left; a settled chart redraws at once.
A session timeline resized mid draw-in carries on from as much of its
line as was drawn.

### Reduced motion

Under `prefers-reduced-motion: reduce`:

- every animation and transition takes 0.01ms, and scrolling jumps;
- the skeleton's shimmer stops;
- a pulsed row or block holds at full opacity until the next click or
  key;
- the drawer, toasts and popovers appear without moving;
- view transitions and `.is-entering` are off;
- charts draw and morph with zero duration, and `countUp` leaves the
  final text as drawn.

## Forced colours

Under Windows High Contrast (`@media (forced-colors: active)`) the
system's colours replace the theme, and one block in app.css keeps the
dashboard readable.

Charts keep their own colours (`forced-color-adjust: none` on
`.chart-svg`, `.sparkline` and `.swatch`): a mark's colour and hatching
are what the legend names, and the system's few colours can't keep the
series apart. Around them, axis text and rules take CanvasText,
gridlines GrayText, a linked rule label LinkText, and the keyboard
cursor and brush Highlight. Focus rings are Highlight. Chips, tiles,
panels, menus, popovers, the drawer, tooltips and toasts keep a
CanvasText border. A meter's lit segments are filled and the rest
outlined.

## Copy

Every word the dashboard shows follows
[`docs/writing-help.md`](writing-help.md): short sentences, plain words,
"you", the Words to use table, and places written "Page › Segment".
Nothing says "tab". `tests/test_ui_copy.py` checks the dashboard's own
strings; `tests/test_help_coverage.py` checks the server's help text.

## Data flow

Every view's data comes from `fetch('/api/...')` and the envelope
`docs/api.md` describes. `fetchJson` unwraps `{"ok": true, "data": ...}`,
or the view shows `error.message` inline on `{"ok": false, ...}`; the
API never sends a stack trace. `loadInto` calls a view's render with the
data first and the container second. The first load shows a
`loadingNode`; a refetch (a new window, project or redraw) keeps the
last figures in place, dimmed under an "Updating…" label
(`.is-refreshing`, with `aria-busy`), until the new ones arrive. `postJson` sends ratings, profiles
and capture changes; session tags go through `fetchJson` with `POST`. No
view holds state the server doesn't have, so a reload is always safe.

`loadReport()`, `loadRecommendations()` and `loadQuickActions()` (the
checks) are cached per window and project, keyed by `scopeKey()`.
Search's entries (`loadEntries` in `palette.js`) are cached the same
way, in `state.searchPromises`. A new window or project (`scopeChanged`
in `app.js`) or **Redraw figures** (`redrawEverything` in `shell.js`)
clears them all.

## Performance

The budget, measured in Chrome at 1440px against a local service with
its report already built:

| Moment | Budget | Measured (median of 9 loads) |
|---|---|---|
| First contentful paint (the `first-contentful-paint` entry) | under 300ms | 44ms |
| Shell ready (`performance.mark("tl-shell-ready")` at the end of `init()`) | under 500ms | 66ms |
| A view already drawn, to its first frame | under 100ms | 19ms to 36ms, the fade included |
| Actions, from the idle prefetch | under 100ms | 23ms to 35ms |

These were taken when the motion work merged. A busy machine reads
slower, so measure on a quiet one: load the page nine times from
`about:blank` (a hash change alone doesn't reload), read the paint entry
and the mark, then time a switch between two views already drawn.

Once the Overview's chart is drawn, `prefetchActions` asks for the
recommendations and the checks while the browser is idle
(`requestIdleCallback` with a 2-second timeout, else half a second
later). Both land in the caches above, so Actions opens without a
fetch.

## Testing

The suite has no headless browser: `urllib.request` and string checks
are enough for a stdlib-only suite. Screenshots and timings are taken by
hand with Playwright against a dev service.

| Test file | Guards |
|---|---|
| `tests/test_service_static.py` | The first-party scans (forbidden substrings, emoji, control characters, inline scripts, "Apply"). A real service with canned JSON: content types, and that the modules fetch every `GET /api/...` route in `docs/api.md`. Imports and no cycles. `PAGES` against the renderers, the heading policy, the section map against `report._SECTION_ORDER`, and the README's page table and glossary against `PAGES` and `GLOSSARY`. The chart catalogue, sources, bar widths, colours and money axes. Evidence links, "Feeds N actions", `prose()`, no "tab" in dashboard text. The pickers, search, shortcuts and many page behaviours |
| `tests/test_ui_tokens.py` | contrast in both themes, the two dark blocks agree, reduced-motion alternatives, `url()` only for fonts, no side-stripe accents, one global focus ring |
| `tests/test_ui_motion.py` | "Motion", "Forced colours" and "Performance" above: the View Transition's guards and timing, the count-up's final text, the stagger's cap, the resize carry-on, the reduced-motion and forced-colours rules, the shell-ready mark and the idle prefetch |
| `tests/test_ui_copy.py` | the dashboard's own words: no banned words, no snake_case, sentences of 25 words at most |
| `tests/test_static_vendor.py` | the vendored d3 and fonts: hashes, nothing unlisted, `.gitattributes`, the wheel, and no call to d3's parsers |

## Modules

| File | Holds |
|---|---|
| `index.html` | the shell: skip link, sidebar, page header, banners and `#views` |
| `theme-boot.js` | a classic script, not a module: sets `data-theme` before the first paint |
| `app.css` | the tokens, the component and page styles, then motion, reduced motion and forced colours |
| `app.js` | the entry point: the router (`resolveRoute`, `changeView`, `showView`, `VIEW_RENDERERS`), the sidebar, the page header, `menuControl`, the theme toggle and `init()` |
| `core.js` | `el`, `clear`, the storage helpers, `state`, `WINDOW_OPTIONS`, `renderedViews`, the `goTo` and project hooks, `onParams`, `highlight` and `listenHighlight` |
| `links.js` | `PAGES`, `viewLabel`, `viewIntro`, `parseHash`, `formatHash`, `scopeParams`, `OLD_TAB_VIEWS`, `SECTION_PAGE_MAP`, `TABLE_PAGE_MAP`, `pageLink`, `GLOSSARY`, `JARGON`, `COST_CARDS`, `termLink`, `cardLink`, the `{{page:}}` pattern |
| `format.js` | `formatCell`, `money`, `moneyText`, `moneyNode`, `moneyParts`, `moneyUnit`, `moneyAxis`, `readableAmounts`, `compactNumber`, `signedPercent`, `fraction`, `shortTs`, `relativeTime`, `modelName`, `modelNames`, `projectName`, `setKnownProjects` |
| `api.js` | `fetchJson`, `loadInto`, `postJson`, `withWindow`, `withProject`, `scopeKey`, `loadReport`, `loadProjects`, `loadRecommendations`, `loadQuickActions`, `prefetchActions`, `actionIndex`, `findSection`, the figures-as-of stamp, the connection state |
| `ui.js` | the components in the table above, plus `prose`, `countUp`, `enterInTurn` and `motionOK` |
| `grid.js` | `dataGrid`, `pulseRow`, `pulseNode`, `renderTable`, `renderPlacedTables`, `renderMappedSections`, `renderReportBackedSection`, `simpleTable`, `setSectionChart`, `NEWEST_LAST`, `formatEvidenceValue` |
| `evidence.js` | `openEvidence`, `evidenceList`, `revealEvidence`, `tableDrawer` |
| `charts.js` | `CHART_SPECS`, `fillSummary`, `ENTITY_COLOURS`, axes, tooltip, keyboard reading, the table view, resize, `drawChart`, `holdChart`, `chartError` |
| `charts-types.js` | the eight forms, `renderChart`, `sectionChart`, `sessionContextChart`, `savingsLevers`, `dailyChanges`, `sparkline`, `meter`, `habitSparkline` |
| `costs.js` | pricing helpers for Actions, Cache and the Glossary: `pricingFacts`, `priced`, `modelSentence`, `avoidableRebuilds`, `cardRuleText` |
| `shell.js` | on every view: the health banner, the status line, the capture banner, `RETRY_SECONDS`, `renderHealth`, the setup checklist (`renderSetupCard`, `renderSetupList`) |
| `icons.js` | `icon(name, opts)` and `ICON_NAMES` |
| `palette.js` | `openPalette`, `matchScore`, `GO_KEYS`, `showShortcuts`, `initPalette` |
| `d3.js` | the one door to the vendored d3 |
| `page-*.js` | one renderer per view: `page-overview.js`, `page-actions.js` (with `RULE_AREA`), `page-spend.js` (with the session drawer), `page-cache.js`, `page-agents.js`, `page-habits.js`, `page-setup.js` (Settings and Profiles), `page-capture.js`, `page-data.js`, `page-glossary.js` |
