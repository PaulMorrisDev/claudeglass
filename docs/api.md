# v0.2 service JSON API

`claudeglass serve` runs a local, read-only HTTP service
(`http.server`, stdlib only — plan Milestone v0.2, "Docker service and
web UI") in front of the SQLite store `service/store.py`'s `Store`
maintains. This file is the frozen contract every `/api/*` route
implements against `service/contracts.py`'s `ApiHandler` shape — the
UI (`docs/ui.md`) and any third-party client are built against this
document, not against `store.py` directly.

No route ever calls out to the network: the service reads transcripts
and the store under `--projects-root`/`--config-dir` and nothing else
(see the "Local only" section below).

## Envelope

Every response is JSON with a `Content-Type: application/json`
header and one of two top-level shapes:

```json
{"ok": true, "data": ...}
```

```json
{"ok": false, "error": {"code": "not_found", "message": "session not found"}}
```

`error.code` is a short, stable, machine-matchable string:
`bad_request` (`400`), `forbidden` (`403`), `not_found` (`404`),
`method_not_allowed` (`405`), `conflict` (`409`), `payload_too_large`
(`413`), `internal_error` (`500`) or `restart_needed` (`503`: part of
the package's code couldn't be imported, almost always because it
changed on disk under a running `serve`; see `code` under
`GET /api/health`). `error.message` is a one-line
human-readable explanation. The
HTTP status code carries the same information for clients that don't
want to parse the body: `200` for `ok: true` on every route except
`POST /api/profiles` and `POST /api/profiles/from-current`, which are
`201` on success. A managed setting is never an error code; see
"Managed-settings routes" below. This is exactly
`service.contracts.ApiError.to_envelope()`'s shape.

Commands inside a response (a `connect_command`, a fix's `command`, help
text that says "run `claudeglass baseline`") come in the form that
runs this install, since the short `claudeglass` needs pip's
Scripts folder on `PATH`: `python -m claudeglass ...`, the `.pyz`
path, or the interpreter's full path when the `python` on `PATH` is
another one (`invocation.py`). `CLAUDEGLASS_COMMAND`, set where
`serve` runs, overrides it word for word. `/api/report.json` gets the
same swap; the Markdown and HTML reports keep the short form, like the
CLI's own output.

## Security headers

Every response from every route carries the same three headers
regardless of method or outcome (`api.py`'s `_SECURITY_HEADERS`,
written once and applied by the single `_write_headers` helper every
response path goes through — including a `404`/`405`/`500` error and a
static-file response, not just a successful `{"ok": true, ...}` one):

- `Cache-Control: no-store` — nothing served here (including a session's
  cost/usage figures) should ever be cached by an intermediary or the
  browser's own disk cache. The one exception is a file under
  `/static/vendor/` or `/static/fonts/` (the vendored d3 and fonts):
  those are pinned by sha256 in `static/THIRD_PARTY.sha256`, and each
  name carries its release (`d3-7.9.0.min.js`). New bytes therefore
  always arrive under a new URL, so they are sent with
  `Cache-Control: public, max-age=31536000, immutable` instead. Every
  other static file, the first-party modules included, stays
  `no-store`.
- `X-Content-Type-Options: nosniff` — stops a browser from
  MIME-sniffing a JSON or static-asset response into something else.
- `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'`
  — matches the UI's own "No external reference" constraint
  (`docs/ui.md`): nothing may load from another origin, inline `<img>`
  data URIs are allowed (the inline-SVG charts), and inline `<style>`
  is allowed (the UI's static `app.css` plus small inline style
  attributes) but inline `<script>` is not. Fonts fall back to
  `default-src 'self'`, so only the vendored ones load.

Every request method is routed through this same path: `GET`/`HEAD`
succeed or fail through the normal envelope, and `PUT`/`DELETE`/
`PATCH`/`OPTIONS` (nothing in this API accepts them) return a `405`
`method_not_allowed` error built the same way, with the same headers —
never a bare stdlib error page (review finding 9).

## Privacy

**No response body from any route below may ever contain message text,
a path found inside a transcript, or a shell command longer than the
40-character `cmd_prefix` contract `model.py` already enforces** — the
same rule `service/schema.py` and `service/__init__.py` state for the
store itself. Concretely: no route returns `transcripts.path`,
`projects.root_path`, or `profiles.toml_path`, and no route ever passes
a `Store` read query's output through un-redacted if that query's own
contract allows a path-shaped field (none of `Store`'s read queries do,
by construction — see `service/store.py`'s docstrings and
`tests/test_service_store.py`'s path-leak guard). Every table/`Section`
value embedded in a `report.*` route already went through this same
audit for the CLI's own JSON output (`render/json_out.py`) and is
reused verbatim here.

The context-file routes are the one deliberate exception.
`/api/claude-md`, `/api/claude-md/<id>` and `/api/skills` return the
files' paths with your home folder written as `~`, short excerpts of
CLAUDE.md text (repeated lines) and skill descriptions. That text is
read from disk (or, for skill descriptions, the newest transcript's
skill listing) when the request arrives and is never stored. It is the
content of your own instruction files, never a message or tool
result.

## Local only

The service opens exactly one listening socket, on `--bind` (default
`127.0.0.1`) `--port` (default `8765`), and never opens an outbound
connection — no route proxies to another host, resolves a hostname, or
fetches a URL. This is the same guarantee `SECURITY.md` documents for
the CLI, extended to the service: a test (`tests/test_service_egress.py`,
built alongside `api.py`) asserts no `socket.connect` call targets
anything outside the bound address for the lifetime of a test server.

## `serve` command-line flags

Flags beyond `--projects-root`/`--config-dir`/`--port`/`--bind`/
`--allow-remote`/`--poll-interval`/`--retention-days`/`--exclude-project`/
`--once` (see `--help`) and `--allowed-host` (see "Host allowlist"
below):

- **`--billing-mode {api,subscription}`** (S1-integration fix 1.a) is
  stamped onto every session's `billing_mode` field (see `/api/sessions`
  below). Defaults to `<config-dir>/config.toml`'s own `billing` setting
  when omitted (itself `"auto"` by default, resolved at start-up by
  `config.resolve_billing`: `"subscription"` when `usage-log.csv` holds
  a usage-limit reading, `"api"` otherwise),
  so a subscription user only has to say so once, in one place, rather
  than on every `serve` invocation.
- **`--monthly-report DIR`** sets `ServeOptions.monthly_report_dir`.
  While `serve` runs, it writes the previous calendar month's report
  into `DIR` when either of its two files is missing — the same report
  `claudeglass monthly-report --out DIR` writes
  ([docs/exports.md](exports.md#serve---monthly-report-dir)). It checks
  at startup and hourly, on its own thread, so requests are never held
  up; `--once` checks once after its watcher tick.
- **`--store PATH`** sets `ServeOptions.store_path`: the SQLite
  database (default `<config-dir>/service.db`). `serve` locks it for as
  long as it runs, so a second `serve` on the same file is refused (exit
  `1`, naming the holder); `--store` gives a second copy its own. See
  "One `serve` per store" in [docs/deploy.md](deploy.md).
- **`--purge`** deletes `<config-dir>/service.db` (or `--store`'s file)
  and its `-wal`/`-shm` sidecars and exits (S1-integration fix 2.e) —
  never starts the watcher or the API, and refuses (exit `1`) while a
  `serve` holds the store. Always prints exactly which files it would delete first;
  only actually deletes them when `--yes` is also given. Safe at any
  time: the store is always a derived cache (`service/store.py`'s module
  docstring), so the next `serve` run simply rebuilds it from the
  transcripts already on disk. Exits `2` (and deletes nothing) if
  `--yes` is missing, `0` otherwise (including when there is nothing to
  delete).
- **`--exit-on-code-change`** sets `ServeOptions.exit_on_code_change`.
  After each watcher tick `serve` checks whether this package's own
  files on disk still match the code it loaded (`service/codewatch.py`;
  see `code` under `GET /api/health`). Without the flag a change is only
  reported. With it, once the change has settled (one tick with no
  further writes), `serve` exits with status `3` so the service starts
  again on the new code: systemd's `Restart=on-failure` and launchd's
  `KeepAlive` do that themselves; Task Scheduler reruns nothing by exit
  status, so on Windows `serve` first starts a hidden helper that waits
  for it to exit and starts the `ClaudeGlass` task again
  (`installer.relaunch_after_exit`). When that task isn't registered,
  `serve` stays up and only reports the change. `install-service`
  registers `serve` with this flag.

## Host allowlist (DNS rebinding)

Every request's `Host` header is checked before routing. A page on
another site that re-points its own domain name at `127.0.0.1` (DNS
rebinding) reaches this server as "same origin", but its requests still
carry `Host: <that domain>`, so they get `403 forbidden` on every
`GET`, `HEAD` and `POST` route, the static files included.

Allowed names: `127.0.0.1`, `localhost`, `::1`, the `--bind` address
when it is a specific address (not `0.0.0.0`/`::`), and each
`serve --allowed-host NAME` (repeatable, `ServeOptions.allowed_hosts`).
The port is ignored, so a container published on another host port
still works. A request with no `Host` header at all (HTTP/1.0, never a
browser) is allowed.

## Cross-site protection (review S3)

The `POST` routes below are mutating (all but `POST /api/whatif`), and
— without a same-origin check — a `Content-Type: text/plain` POST is a preflight-free "simple"
cross-site request a browser will send blind. The response is opaque to
a cross-site attacker (no CORS headers are ever sent, so it can't read
`ok`/`data` back), but a profile written this way is exactly what
`apply` later reads and acts on, so the write itself is the risk, not
exfiltration. Every `POST` request, `POST /api/whatif` included, is
checked before its body is even parsed:

- **`Content-Type` must be `application/json`** (a parameter such as
  `; charset=utf-8` is ignored) — `400 bad_request` otherwise. This
  alone forces a real browser to preflight the request, which this
  service already fails for a cross-origin caller (no
  `Access-Control-Allow-Origin` is ever sent).
- **`Origin`, when the request carries one, must match this server's own
  `Host`** (compared as `http://<Host>` — this service is `http`-only) —
  `403 forbidden` otherwise.
- **`Sec-Fetch-Site`, when the request carries one, must be
  `same-origin` or `none`** — `403 forbidden` otherwise.

A request carrying neither `Origin` nor `Sec-Fetch-Site` (e.g. a
same-machine CLI tool such as `curl`) is allowed — this API has no
authentication of its own (see "Local only" above), so that posture is
unchanged; the guard targets a *browser* silently issuing the request on
a victim's behalf, not a deliberate local caller. The dashboard sends
`Content-Type: application/json` on every one of its own `POST` calls
(through `fetchJson` in `service/static/api.js`, directly or via
`postJson`), so the UI itself is unaffected. A `POST` whose `Host` is not on
the allowlist above is also `403 forbidden`.

## Body size limit (G5)

Every `POST` route caps its request body at 64 KB, checked against
`Content-Length` before the body is parsed — `413 payload_too_large`
otherwise. An oversized body is still read off the socket and discarded
(in bounded chunks, never as one allocation sized to the declared
length), so the connection stays open for a next request exactly like
every other rejection above. This service has no authentication (see
"Local only" above), so the cap bounds the memory and JSON-parse cost
any local process can force per request, regardless of the cross-site
checks above. Every route's actual body (a profile, a tag, a feedback
payload) is small hand-typed or hand-picked JSON, well under the cap.

## Routes

All `GET` routes accept query-string parameters; all `POST` routes
accept a JSON request body (`Content-Type: application/json`, now
enforced — see "Cross-site protection" above). A route not listed here
returns `404` with `error.code: "not_found"`.

### `GET /api/health`

Liveness/diagnostics probe (also the Docker healthcheck target — plan:
"healthcheck on `/api/health`"). Never fails once the process is up, and
`serve` binds its port before its first scan, so it answers from the
first second.

`data`: `{"status": "ok"|"outdated"|"starting"|"degraded"|"stale", "message": str|null, "scan": WatcherState-as-dict|null, "version": str, "code": {"changed": bool, "changed_at": str|null, "version_on_disk": str|null, "id": str}|null, "schema_version": int, "transcripts_missing": int, "watcher": WatcherStats-as-dict, "service_registered": true|false|null, "capture": {...}|null}`.

`status` says whether the figures are keeping up, and `message` says
what it means in plain words (`null` when `"ok"`). The HTTP status is
`200` in every case: the process is up, and a healthcheck that restarts
a container mid-scan would only make things worse.

| `status` | When |
|---|---|
| `ok` | The last scan finished without failing outright, recently. |
| `outdated` | This package's code changed on disk since the process started (`code.changed`), so routes that import code lazily may fail with `503 restart_needed`. Comes before every other status: only a restart helps. `message` gives the time, both versions when they differ, and the restart command, or says the service restarts by itself (`serve --exit-on-code-change` with a restart arranged). |
| `starting` | No scan has finished since the process started: the first scan is still running (`message` gives its progress). |
| `degraded` | The last scan failed outright, such as `OperationalError: database is locked` (the reason is in `message` and in `watcher.error_messages`). It retries every poll interval. |
| `stale` | The background scanner is no longer running, no scan has finished for ten minutes (or ten poll intervals, if longer), or one scan has run for over an hour. Figures are frozen; `message` names the time they are from and how to restart. |

`scan` (`null` when no watcher is wired in, as in tests) is where the
scanner is right now, as opposed to `watcher`, which is what its last
finished scan did: `running` (the background thread is alive),
`scanning` and `scan_started_at`, `last_success_at`,
`last_tick_failed`, and the running scan's `phase` with `done` of
`total`: `"finding"` (walking the projects folders; `done` files seen,
`total` 0), `"reading"` (parsing changed files from the parse cache or
in parallel; only on a scan with many changed files) and `"storing"`
(sessions folded into the store). `phase` is `null` between scans.

`version` is the running code's version (`claudeglass
--version`), also shown on the dashboard's status line: after an update, a
dashboard still showing the old one hasn't been restarted, or runs from
another Python install.

`code` (`null` without a code watch wired in, as in tests and
`serve --once`) says whether the package's files on disk still match
the code this process loaded. An editable install (`pip install -e`)
runs straight from the checkout, so a pull or a release landing there
changes the files under a running `serve`; a module it then imports
lazily comes from the new files while the ones already loaded are
old, and fails. After every watcher tick `service/codewatch.py`
compares the modules' and data files' names, sizes and modification
times (the dashboard's own `static/` files are left out, as they are
read from disk on every request), and hashes their contents only when
those moved, so a checkout switched away and back is no change.
`changed` stays `true` once seen, as a lazy import may have loaded the
other code meanwhile. `changed_at` is when it was first seen and
`version_on_disk` the `__version__` the files now declare (`null` when
unreadable). `id` names the loaded code (12 hex characters of its
hash): the dashboard offers to reload the page when it differs after a
restart. A route whose code fails to import (`ImportError`) checks at
once and answers `503` `restart_needed` instead of `internal_error`,
naming the exception's type only, never its message, which can hold
a path. See `serve --exit-on-code-change` above for restarting by
itself.

`service_registered` (v3) is whether `serve` is currently registered to
start at logon/boot (`claudeglass install-service` — see
[docs/deploy.md](deploy.md)): `true`/`false` when the platform's own
query command (`schtasks`/`systemctl --user is-enabled`/`launchctl
print`) ran and gave a clear answer, `null` when it couldn't be run at
all (no probe wired up — e.g. `serve --once` — an unsupported platform,
or the query tool itself missing). `null` always means "unknown", never
"not registered". Computed at most once every ten minutes and cached
in-process — the probe shells out to a real system command, so a UI
polling `/api/health` doesn't spawn one on every refresh.

`transcripts_missing` (review finding 3) is `Store.count_missing_transcripts()`
— the current count of transcript rows whose backing file the watcher
can no longer find on disk. A transcript in this state is *marked*, not
deleted: its `digest_blob` keeps serving `report.*`/rebuild until it is
actually removed by `--retention-days`/`serve --purge` (see "Retention
and purge" in [docs/deploy.md](deploy.md)). This is also why a report
can still include a session whose transcript file Claude Code's own
`cleanupPeriodDays` retention has already removed — see "Store rebuild"
below.

`watcher` (S1-perf) additionally carries a per-tick timing breakdown of
its own `duration_s`: `discovery_s` (filesystem walk + diffing against
the store's known files), `parse_s` (cache lookups, on-miss parsing,
and the parallel bulk-prewarm pool's own wall-clock time — see
"Performance" in [docs/deploy.md](deploy.md)) and `store_s` (every
SQLite reader/writer call the tick made). The three don't sum to
exactly `duration_s` — session/workflow folding and fixed per-tick
overhead are counted in none of them — but each is a real,
non-overlapping measurement of its own phase, so a slow tick's
dominant cost is visible here rather than only as one opaque total.
See `service/contracts.py`'s `WatcherStats` for the exact field list.
A scan that fails outright records `tick failed: <reason>` in
`watcher.error_messages`. The reason is the exception's type, plus
SQLite's own message for a database error (it names no path or
transcript text); other errors give their type only, since their
messages can carry a path.

`capture` is metrics capture's setting, cheap enough to read on every
poll (`config.toml` and `settings.json` only): `level`, `title`,
`describe` (such as `"Essentials (since 2026-09-20, 25% of sessions)"`),
`on`, `expired` (its end time has passed), `effective` (on and not
expired), `enabled_at`, `until`, `timebox_days` (how many days a
switch from off to on runs before it ends, when no end is given),
`sample`, `metrics`, `feedback`,
`coaching`, `projects_limited` (only whether `[capture] projects` is
set, never the patterns) and `hooks_ok` (whether `settings.json` runs
every hook the chosen metrics need; `true` when they need none). The
dashboard fetches `GET /api/capture` for its banner when this block
changes. `capture` is `null` when `config.toml` can't be read, or when
this part fails for any other reason (its code changed on disk, say):
`/api/health` still answers.

### `GET /api/summary`

Corpus-wide totals — `Store.summary`.

Query: `window_days` (int, optional; no default, so all time when
omitted), `window` (a named window, as for the report-backed routes
below; it takes precedence, and `window_days` is then `null` in the
response), or, additively, explicit `since`/`until` (ISO 8601) — the same
four params `/api/sessions`/`/api/compactions` accept, and, like those two
(unlike the report-backed routes below), *no* params at all still means
all time rather than a 30-day default. An explicit `since`/`until` is
rounded down to the minute the same way a named window's own resolved
`since` already is, so two requests for "the same" bound issued a few
seconds apart — the dashboard's own current-period and previous-period
calls, for instance — agree on exactly the same window.

`data`: `{"window_days": int|null, "sessions": int, "transcripts": int, "total_cost": float, "total_tokens": int, "cache_read_tokens": int, "cache_saved": float}`.
`total_cost` is at list price, whatever the billing mode. Additive:
`cache_read_tokens` is `turns_agg.cache_read_tokens` summed across every
model over the sessions the window counts, whole (the same rule as
`sessions` and `total_cost` below, so a bound inside a day never pulls
in another session's reads); `cache_saved` is what those cache reads actually saved against
sending the same tokens fresh as input instead — per model,
`cache_read_tokens × (input_price − cache_read_price)`, summed, in USD at
list price (a model the rate card doesn't price is left out, the same
"priced models only" rule every other per-model pricing loop in this
project follows). `cache_saved` is `0.0`, never an error, when the rate
card itself can't be loaded.

With a window given, a session qualifies when its last reply (from its
main transcript or any subagent's) falls in the window — the same
`window_by="last-reply"` rule `corpus.load_corpus` and
`service.rebuild.corpus_from_store` share — and every transcript
belonging to a qualifying session (top-level and every subagent) counts
once the session itself qualifies. A transcript file's `mtime` is not
used: Claude Code appends titles and other metadata to old transcripts,
which moved a session with no replies in the window into it. This is the same
windowing the CLI's `report` overview section uses, so
`sessions`/`transcripts` here always agree with a fresh
`report --days <window_days>`'s own `sessions`/
`top_level_transcripts + subagent_transcripts` totals for the identical
window (v0.3 fix — this route used to window `sessions` by the session
row's own `last_ts` and never window `transcripts` at all).

### `GET /api/sessions`

Recent sessions — `Store.sessions`.

Query: `limit` (default 50), `offset` (default 0), plus the optional
`window`/`window_days`/`since`/`until` (below): with one, only the
sessions a report over that window counts (last reply in the window).
Without one, every session. Newest first (by `first_ts`).

`data`: `[{"id", "slug", "first_ts", "last_ts", "span_s", "archetype", "mode", "purpose", "entrypoint", "billing_mode", "profile_id", "total_cost", "total_tokens", "source"}, ...]`.

`source` says where the session ran: `"This computer"`, or
`"WSL: <distro>"` for one read from a WSL distro's folder (see
`extra_projects_roots` in [configuration](first-run.md#using-claude-code-in-wsl-too)).
It never carries the path itself.

### `GET /api/session/<id>`

One session's detail — `Store.session`. `404` (`error.code: "not_found"`)
if `<id>` is unknown.

`data`: the session-summary fields above, plus `transcripts` (list of
`{"id", "kind", "agent_id", "agent_type", "spawn_depth", "parent_agent_id"}`
— no `path`), `tags` (`{key: value}`) and `feedback`: your rating
from Spend › Sessions (`{"outcome", "slow", "worth", "helped",
"set_at"}`, words only; `null` when unrated). While the dashboard
rating is switched on (`[capture] feedback` holds `dashboard_rating`),
`data` also carries `feedback_questions`: the `/tl-feedback` questions
to rate it with, each `{"key", "question", "multi", "options": [{"word",
"label"}]}`.

If the session has a stored top-level transcript digest, `data` also
carries `turn_series` and `markers` (S1-integration fix 1.g), sourced
from `Store.turns_for_session` — the timeline chart's exact input
shape, no client-side reconstruction needed:

- `turn_series`: a list of `[turn_index, ctx, cache_creation_tokens,
  is_recache, preceding_primary]` per priced turn (`turn_index >= 1`),
  in turn order. `preceding_primary` is the `EventKind` string value
  (`"human_text"`, `"compact_boundary"`, `"tool_result"`, ...) of the
  event immediately preceding that turn.
- `markers`: `{"compactions": [turn_index, ...], "spawns": [turn_index, ...], "human": [turn_index, ...]}`
  — turn indices where a compaction boundary, an agent spawn
  (`agent_brief_chars` set), or a human prompt (`human_prompt_chars`
  set) preceded that turn. Always computed from every priced turn, never
  thinned by the downsampling below.
- `truncated`: `bool` (review finding 11) — `true` when the session has
  more than `Store.MAX_TURN_SERIES_POINTS` (5,000) priced turns and
  `turn_series` above was downsampled to that cap (every marker turn is
  kept; the rest are evenly sampled across the full session). `false`
  for every session at or under the cap.
- `limit_markers` (v3-limits wiring): `[{"ts", "kind", "detail"}, ...]`
  — every `LIMIT_HIT`/`LIMIT_RESUME`/`AGENT_TERMINATED` event on this
  session's top-level transcript, sourced from `limits.limit_markers`
  (see [`docs/limits.md`](limits.md#session-timeline-marker-contract)
  for the exact contract) and never downsampled. `kind` is one of
  `"limit_hit"`, `"limit_resume"`, `"agent_terminated"`; `detail` carries
  a `subkind` key when the event has one (`session_limit`/`weekly_limit`
  for `limit_hit`; `rate_limit`/`other` for `agent_terminated`).

All four fields are omitted entirely (never present as an empty list)
when no top-level transcript digest is stored yet, or the stored digest
can't be decoded — never fabricated.

### `GET /api/session/<id>/explain`

"Why was this session expensive?" for the session detail view
(`service/explain.py`). Every sentence is a fixed template filled with
this session's own aggregates from `Store.session_parts`; no model is
asked. `404` if `<id>` is unknown.

`data`: `{"session_id": str, "headline": str, "sentences": [str, ...], "cost_split": [{"part", "label", "cost", "share_pct"}, ...]}`.

- `headline`: the session's cost (in the billing mode's units), replies
  and tokens.
- `sentences`: how it compares with your median session, which part of
  the cost led and what that means, how much went on subagents (workflow
  agents included) and the costliest agent type, cache rebuilds and their
  commonest cause, and conversation summaries. A sentence is left out
  when its data is. An agent type a workflow also started counts its
  workflow runs and its own together.
- `cost_split`: `part` is `cache_read`, `cache_write`, `output` or
  `input`, always in that order. `cost` is at list price from the rate
  card, whatever the billing mode; models the rate card doesn't know
  are left out.

### `GET /api/recache`

Corpus-wide RE-CACHE breakdown — `Store.recache`.

`data`: `{"by_signature": {"full-expiry": {"turns", "cache_creation_tokens"}, "prefix-invalidated": {...}, "limit-expiry": {...}}}`.
A signature with no rebuilds is absent, not zero. Always all history:
this route takes no window. The dashboard doesn't fetch it: Cache ›
Rebuilds draws the window's breakdown from `report.json`'s
`recache_signature_split`.

### `GET /api/daily-usage`

Per-day, per-model token and cost totals — `Store.daily_usage`. The
dashboard's Overview draws its daily spend chart from it, with the
window and `split=agent`.

Query: `days` (int, default 30, at least 1; unchanged for existing
callers). Days are UTC calendar days. Additive: the same `window`/
`window_days`/`since`/`until` params the report-backed routes below
accept (see "Report-backed routes: windowing query params") take
precedence over `days` when any of the four is given, so `?window=all`
or an explicit `since`/`until` isn't also clamped to a trailing `days`
window; with none of them, `days` (default 30) applies exactly as
before. Additive: `split` — `agent` breaks each day/model row into the
main session and every subagent (`transcripts.kind` joined in from
`turns_agg.transcript_id`: `"top-level"` is `"main"`, `"subagent"`/
`"workflow-agent"` (an agent a workflow run started) are `"subagent"`),
adding an `"agent"` key; `model`, or omitting `split`, keeps the
original, unsplit shape. Any other `split` value is `400`.

`data`: `[{"day", "model", "turns", "input_tokens", "cache_creation_tokens", "cache_read_tokens", "output_tokens", "thinking_tokens", "cc_5m", "cc_1h", "cost"}, ...]`
(with `split=agent`, each row additionally carries `"agent"`:
`"main"`|`"subagent"`), ordered by day, then (with `split=agent`) agent,
then model. `cost` is at list price.

### Report-backed routes: windowing query params

`/api/ttl`, `/api/carry`, `/api/compaction-sim`, `/api/plan-handoff`,
`/api/model-swap`, `/api/waste`, `/api/config-diff`, `/api/recommendations`,
`/api/diagnostics`, `/api/claude-md`, `/api/claude-md/<id>`,
`/api/skills`, `/api/profile-goals` (with `goal`), `/api/quick-actions`,
`/api/quick-actions/<id>`, `POST /api/whatif` and
`/api/report.md`/`.html`/`.json` (below) all accept the same windowing
query params, mirroring the CLI `report` subcommand's own
`--days`/`--since`/`--until` (`discovery._resolve_window`'s exact
resolution). `/api/summary` takes the same four params, but with no
params it means all time rather than a 30-day default.
`/api/sessions` and `/api/compactions` accept them all but, unlike the
report routes, list everything when none is given. Every other route
(`/api/health`, `/api/session/<id>`, `/api/recache`, `/api/baseline`,
`/api/profiles*`, `/api/impact`, `/api/setup`, `/api/setup/status`, `/api/capture`) ignores them.

**Caching.** The service builds each window's report once per store
change (`Store.change_token()`) or `config.toml` change (its
modification time: a new billing mode or capture setting changes the
figures) and keeps the last eight. When the store
has changed since a window's report was built (a live session writes
every few seconds), a request is answered from the kept report at once
and a rebuild starts in the background (one at a time), so a page never
waits on a whole report build just because a transcript grew. A named
`window` keeps its report across the minute-by-minute moves of its
start the same way. A request waits for a build only when nothing is
kept for its window, when the kept report is over ten minutes old, or
when the list of change points behind `/api/impact` has changed; and
requests for a window already being built wait on that one build.
`/api/impact` is cached the same way.

Every response built from a kept report carries **`X-Figures-As-Of`**
(ISO 8601, UTC): when that report's figures were read from the store.
While a newer one is being built it also carries
**`X-Figures-Refreshing: 1`**. The dashboard shows the time on its
status line, at the foot of the sidebar.

- **`window`** (optional) — a named window, used by the dashboard's
  header picker: `1h` (the last hour), `today` (since midnight in
  `config.toml`'s `tz`, else the machine's zone), `24h`, `change` (since
  your latest `apply`, its undo, a settings change the config hook saw,
  a change to metrics capture, or a model, effort or CLAUDE.md size
  change your sessions show; the same newest change `/api/impact`
  lists; `400` when there is none yet) or
  `all` (no limit). Anything else is `400`. A named window takes
  precedence over the other three params. It is turned into a `since`
  rounded down to the minute, so repeat requests share one cached report.
  A session counts when its last reply falls inside the window (so it
  was active then), and it then counts in full. `change` is the
  exception: it counts the sessions whose *first* reply falls inside it
  (the ones that started on the new settings), and `/api/daily-usage`
  and `/api/compactions` then keep only those sessions' rows, so the
  daily figures add up to `/api/summary`'s total.
- **`window_days`** (int, at least 1, optional) — the last N days;
  defaults to 30 when neither `since` nor `until` is given.
- **`since`** / **`until`** (ISO 8601, optional) — when either is
  present, `window_days` is *not* defaulted to 30 (matching the CLI's
  own `--days`/`--since` mutually-exclusive argparse group), so a
  `since`/`until` request windows the report exactly the way
  `report --since ... --until ...` does rather than being silently
  additionally clamped to the last 30 days. A malformed `since`/`until`
  is a `400 bad_request`. `report.meta.window` in the response is
  rendered identically to the CLI's own `_window_description` (`"since
  <since> until <until>"`, `"since the beginning until <until>"`, etc.)
  so the two are byte-equivalent for the same window, not just
  numerically equal.

### `GET /api/ttl`

TTL simulation summary (per agent type: observed vs. simulated 5m/1h
cost, fidelity, recommendation) — same shape as the CLI's `ttl` section
tables (`render/json_out.py`'s `Section`/`Table` encoding), sourced by
re-running `ttl.py`'s simulation over the store's `turns_agg`/
`recache_turns` rows rather than a fresh parse.

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

### `GET /api/carry`

Context carry-cost summary (per tool and per agent type: carried-result
count, tokens entered, mean turns carried, carry tokens/cost, cache-volume
share; plus the top individually-carried results and the truncation-cap
savings table) — same shape as the CLI's `carry` section tables, sourced
from the assembled report's `"carry"` section (`carry.py`).

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

### `GET /api/compaction-sim`

`autoCompactWindow` sweep summary (per candidate window: simulated
compactions/session, mean ctx, total cost and delta vs. observed; plus
the per-agent-type best window and the fidelity check against each
session's actually-configured window) — same shape as the CLI's
`compaction-sim` section tables, sourced from the assembled report's
`"compaction_sim"` section (`compaction_sim.py`).

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

### `GET /api/plan-handoff`

The "building in a fresh session after a big plan" tables
(`plan_handoff_summary`, `plan_handoff_by_session`), sourced from the
assembled report's `"plan_handoff"` section (`handoff.py`, see
[`plan-handoff.md`](plan-handoff.md)); `null` when the section is absent.

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

### `GET /api/model-swap`

Model-swap counterfactual summary (per agent type: observed cost, cost
at every model the rate card carries, the best cheaper alternative and
the ceiling saving; plus the corpus-wide summary if every eligible
Fable/Opus subagent type moved one tier down) — same shape as the CLI's
`model-swap` section tables, sourced from the assembled report's
`"model_swap"` section (`model_swap.py`). A subagent's saving covers
only the runs its agent file's model decides; the per-type table's
additive `lever_runs`, `lever_priced_turns`, `lever_model`,
`lever_cost`, `workflow_runs` and `spawn_model_runs` columns say which,
and a third table, `model_swap_agent_file_runs`, reprices those runs at
every model (see [`model-swap.md`](model-swap.md)).

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

### `GET /api/waste`

Wasted-turn spend summary (total wasted turns/cost and their share of
the corpus, a per-cause breakdown with each cause's lever, a
per-agent-type roll-up, and the top wasted-cost sessions by a salted
session hash) — same shape as the CLI's `waste` section tables, sourced
from the assembled report's `"waste"` section (`waste.py`).

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

### `GET /api/compactions`

Every recorded compaction — `Store.compactions` — oldest first. With
`window`/`window_days`/`since`/`until`, only those in the window.

`data`: `[{"transcript_id", "ts", "pre_tokens", "post_tokens", "dropped_tokens", "trigger", "join_delta_s"}, ...]`.

### `GET /api/config-diff`

Effective-config comparison across projects (plan "Configuration
layers" section: `config_groups`/`config_drift`), computed from the
latest `snapshots` row per project.

Query: `key` (a specific settings key) or `auto_keys=1` (the whole
config section; `400` when neither is given), plus `window`/`window_days`/`since`/
`until` (see "Report-backed routes: windowing query params" above).
Mirrors the CLI's `config-diff` subcommand.

`data`: with `auto_keys=1`, a list of every `config` section table (the
dashboard's Setup › Settings uses this); with `key`, that key's
`config-diff-<key>` `Table`, or `[]` when it didn't change in the
window.

The watcher files every snapshot under the store's internal
machine-wide bucket, and `Store.snapshots()` reports that bucket's
`project_slug` as `null` rather than a synthetic project name
(S1-integration fix 1.c). A schema-2 snapshot still names its own
project (the hook's `slug:<hash>` of its working directory), and the
report groups by that, as the CLI does. Only a snapshot without one
(schema 1) falls under `snapshots.py`'s "(unknown project)" label, as a
user-level configuration layer, not a project's.

### `GET /api/recommendations`

The same `Recommendation` list `recommend.recommend()` produces for the
CLI's `report`, computed from the store's latest snapshot and session
window rather than a fresh corpus scan.

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

`data`: `[{"id", "severity", "category", "title", "action", "lever", "scope", "evidence": [[label, value, source_table, row_key], ...], "agent_type", "why", "estimated_saving", "saving_basis", "saving_usd", "changes": [{"target", "key", "agent", "value", "suggested", "note", "unconfirmed", "current", "new_agent_file"}, ...], "fixes": [{"key", "agent", "explainer": [[heading, text], ...], "command", "command_warning", "prompt"}, ...], "key"}, ...]` —
exactly `render/json_out.py`'s existing `Recommendation` encoding.
`fixes` (from `fixes.py`) holds, per change, the six-part explainer, an
`apply --set ... --dry-run` command (`null` when the value needs
judgement) and a prompt for Claude. `saving_usd` is `estimated_saving`
as a plain number (USD at list price, `null` when not estimated) —
used to order recommendations of the same severity, and safe for a
client to format or sort by directly. `key` (additive) is a
deterministic, URL-safe id for this recommendation: `id` alone repeats
across agent types (the same rule can fire once per subagent type), so
`key` adds a slug of `agent_type` when one is set, and stays the same
across two runs of the same corpus. A dashboard link can use it as
`#/actions/recommendations?id=<key>`.

Each row also says whether you ignored it (`ignores.py`, see
`POST /api/recommendations/ignore`), for the `project` asked about and
the profile `apply` last marked active: `ignored` (bool), `ignored_at`
(ISO-8601, or `null`), `ignored_in` (`"project"`, `"all"` for every
project, or `null`) and `ignored_before` (`{"ignored_at", "changes":
[{"agent", "key", "value"}, ...]}` when it was ignored here but now
suggests something else, so it shows again; else `null`). The list
itself is never filtered, and `/api/report.json` and the CLI reports
carry no ignore fields.

### `POST /api/recommendations/ignore`

Ignore recommendations, or stop ignoring them. Query: the same window
and `project` as `GET /api/recommendations`. With a `project`, the
ignore applies in that project only; without one, in every project.
Kept per profile: under the profile `apply <profile>` last marked
active (`<config_dir>/active-profile`), or `none`.

Body: `{"keys": [str, ...], "ignored": bool}` — 1 to 100 recommendation
`key`s from this window's list (a rule for several agent types is one
dashboard item, sent as one request). `400` when the body isn't that
shape or a key isn't `[a-z0-9._:-]`; `404` when a key isn't in this
window's list. The fingerprint that decides whether a recommendation
has changed since (its rule and the changes it suggests, never its
saving or wording) is worked out by the service from its own list,
never taken from the request. Stopping removes whichever ignore
applied, so stopping an every-project ignore from one project's view
stops it everywhere.

Stored in `<config_dir>/ignored-recommendations.json`.

`data`: `{"keys", "ignored", "active_profile_id"}` (`null` when no
profile has been applied).

### `GET /api/diagnostics`

The report's parse-quality counters (`ReportModel.diagnostics`) as one
plain-English `Table` — `helptext.diagnostics_table`. Used by the
dashboard's Data quality page.

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed
routes: windowing query params" above).

`data`: a `Table` (`name: "data_quality"`). Each row is `[field, value,
meaning]`; `field` is the raw `Diagnostics` field name and
`value_labels` maps it to its display label. Dict counters are joined
into one `"key: count, ..."` string. The first row, `snapshot_hook`, is
`hook_health.check` on `<config-dir>/../settings.json`: whether a
SessionStart hook runs `snapshot-config.py`, whether its path exists,
and how long ago the last snapshot was taken. The second, `statusline`,
is `hook_health.statusline_check`: whether the statusline that records
usage limits is running, given where your sessions run. Both have the
value `working` or `needs attention`.

### `GET /api/profiles`

Every profile the service knows about (v0.3): the eight shipped
catalogue profiles (`profiles.catalogue`, package data — never a row in
the store) plus every user profile written under
`<config_dir>/profiles/*.toml` (`Store.profiles`, ingested by the
watcher's `_scan_profiles`), each tagged with which of the two it came
from.

`data`: `{"profiles": [{"id", "name", "source": "catalogue"|"user", "archetype": str|null, "for": [str, ...], "tasks": [str, ...], "updated_at": str|null}, ...], "suggested_profile_id": str|null, "active_profile_id": str|null, "active_profile_name": str|null}`.

A catalogue entry's `archetype`/`for` come straight from its shipped
TOML document; `tasks` is `for` normalised to the capture task
vocabulary (`profiles.catalogue.tasks_for`: `implementation` becomes
`feature`, `bugfix`, `debug`; a way of running such as `fanout` covers
none), the form `POST /api/whatif`'s `task` takes. A user entry never
carries them (the `profiles` table
only indexes `id`/`name`/`updated_at` — no `toml_path`, never
API-returned). `updated_at` is `null` for a catalogue entry (nothing to
timestamp). `suggested_profile_id` is the latest recorded baseline's own
`suggested_profile` field (`null` if no baseline has been captured yet),
so the UI can mark that entry in the list without a second round trip.
`active_profile_id` is the profile `apply <profile>` last marked active
(`<config_dir>/active-profile`), `null` when none has been; ignored
recommendations are kept under it. `active_profile_name` is its name
(its id when it's no longer in the list).

### `GET /api/profile-schema`

Every key a profile may set, for the dashboard's profile form.

`data`: `{"settings": [Lever, ...], "agents": [Lever, ...], "env": [str, ...], "archetypes": [str, ...], "scopes": [{"key", "label"}, ...]}`,
where a `Lever` is `{"key", "label", "kind", "values", "min", "max", "description", "tradeoff"}`.
`kind` is `str`, `enum`, `int`, `bool` or `list[str]`; `values` is the
allowed list for an `enum` (else `null`), `min`/`max` the range for an
`int`. `label`, `description` and `tradeoff` come from
`fixes.LEVER_LABELS`/`fixes.SETTING_TEXT`, the same text the
recommendation explainers use. `env` lists the environment variable
names a profile may set (`profiles.schema.ENV_ALLOWLIST`).

### `GET /api/profiles/<id>`

One profile's contents. `404` if `<id>` names neither a catalogue id
nor an existing `<config_dir>/profiles/<id>.toml`.

`data`: `{"id", "name", "source": "catalogue"|"user", "archetype", "for": [str, ...], "tasks": [str, ...], "notes", "settings": {key: value}, "agents": {name: {key: value}}, "env": {NAME: value}, "setting_count": int}`.
`setting_count` counts settings, agent keys and environment variables
together.

### `GET /api/profiles/<id>/diff`

The real diff (v0.3, `profiles.diff.diff_against_effective`/
`render_unified_diff`) between profile `<id>` (a catalogue id or a user
profile written by `POST /api/profiles`) and the store's own *latest*
recorded config snapshot's effective config (`--dry-run` equivalent,
read-only — this route never writes anything, matching the plan's "the
service never calls `apply`; it renders the diff and the command"). A
store with no snapshot at all diffs against an empty effective config
(nothing currently set, nothing managed) and says so in `notes`, rather
than erroring. `404` if `<id>` names neither a catalogue id nor an
existing `<config_dir>/profiles/<id>.toml`.

Query: `scope` — one of `user` (default), `project-local`, `repo` (same
three scopes `profiles.diff`/`apply` use); `400` for anything else. This
route never accepts a client-supplied project directory — doing so
would put a raw filesystem path in the response body, which this
service's privacy rule forbids regardless of who supplied it — so a
`project-local`/`repo` scope's `apply_command` always omits
`--project-dir`; fill it in yourself when you run the command.

`data`: `{"profile_id": str, "scope": str, "diff": str, "settings": [DiffRow, ...], "agents": [DiffRow, ...], "env": [DiffRow, ...], "apply_command": str, "dry_run_command": str, "launch_command": str, "prompt": str, "notes": [str, ...]}`,
where a `DiffRow` is `{"key", "setting", "agent", "label", "description", "where", "current_value", "current_provenance", "proposed_value", "target_file", "managed"}`
(`profiles.diff.DiffRow`'s own fields, split by key prefix into the
three lists rather than left as one flat `rows` array — `settings.*` /
`agents.<name>.*` / `env.*`). The display fields: `setting` is the key
without its prefix (dotted agent keys such as `experimental.cacheTtl`
stay whole), `agent` the agent's name for an `agents.*` row (else
`null`), `label`/`description` its plain name and what it controls, and
`where` the file the change is written to under the chosen `scope`
(`fixes.profile_change_where`). `target_file` is where the key is set
today.

`apply_command`/`launch_command` are the two lines
`profiles.diff.apply_command` returns, split apart — the exact
host-side `claudeglass apply` invocation and the
`claudeglass apply <id> --launch` one-session-overlay alternative
respectively (it writes the overlay, then prints the
`claude --settings <path>` command with the real path). `dry_run_command` is
`apply_command` plus `--dry-run`, which the dashboard shows first.
`prompt` (`fixes.profile_prompt`) asks Claude to make the same changes
by hand: one line per changed, unmanaged key, naming the file and the
old and new values, and asking Claude to show the diff before saving.

"Latest snapshot" here and in `POST /api/profiles/from-current` means
the newest snapshot that records config: `apply` writes a
`{ts, schema_version, profile_id}` stamp into the snapshots folder to
mark the active profile, and the service skips those stamps
(`snapshots.records_config`) wherever it reads snapshots. Here, and in
`/api/profile-goals` and `/api/quick-actions`, its agents are widened
to every project's latest snapshot (`snapshots.with_every_project_agents`):
the config hook records only the agents of the project a session
started in, so the newest snapshot alone would show another project's
agents as not set.

### `GET /api/baseline`

The latest stored baseline capture, its full history, and the
onboarding capture window's own status (v0.3,
`baseline.capture_status`/`format_capture_status`) — so the UI can mark
a baseline-derived suggestion as provisional while a capture window is
still open.

`data`: `{"baseline": Baseline|null, "history": [Baseline, ...], "capture_status": {"started": bool, "window_days": int|null, "elapsed_days": float|null, "remaining_days": float|null, "complete": bool, "summary": str}}`,
where a `Baseline` is `{"id", "project_slug", "window_start", "window_end", "archetype", "created_at", "record": dict|null}`
(`"record"` — only present on `"baseline"`, not on `history` entries —
is the captured baseline JSON record itself, already redacted the same
way `claudeglass baseline`'s own on-disk record is: no message
text, no raw paths, `projects` a list of already-redacted slugs).
`"baseline"` is `null` and `"history"` is `[]` when no baseline has ever
been captured. `capture_status.summary` is the same one-line status
`init`/`baseline` print to the terminal.

### `GET /api/quick-actions`

One answer per way of saving tokens (`quick_actions.CHECKS`): models,
effort, compaction, cache, tools, skills, claude-md, tool-output,
hooks, tool-search, habits, quality and cost-record. Each check always answers, including "nothing to
do". The last, cost-record, checks ClaudeGlass's own figures against the
cost Claude Code records for a session.

Query: the windowing params above.

`data`: `{"period", "checks": [{"id", "question", "why", "status", "summary", "rule_ids", "fix_count", "tip_count"}, ...]}`.
`period` is the window as a phrase ("over the last 30 days", "in the
last hour"). `status` is `act` (worth a look), `ok` (nothing to do) or
`no_data`. `rule_ids` (additive) lists the `/api/recommendations` rule
ids this check draws on -- `[]` for a check with no rule behind it.

### `GET /api/quick-actions/<id>`

One check in full. `404` for an unknown id.

Query: the windowing params above.

`data`: `{"id", "question", "why", "period", "rule_ids", "status", "summary", "table": {"columns": [{"key", "label"}, ...], "rows": [[cell, ...], ...]}|null, "fixes": [Fix, ...], "tips": [{"title", "text"}, ...]}`,
where each row is a list of display values in column order, `table` is
`null` when there is nothing to show, and a `Fix` is the `fixes.py`
shape `/api/recommendations` uses, plus an optional `title`. Environment-variable fixes (`BASH_MAX_OUTPUT_LENGTH`,
`MAX_MCP_OUTPUT_TOKENS`) carry a prompt and no command: this tool never
writes the `env` block. `rule_ids` (additive, same list as
`/api/quick-actions`'s own) names the recommendation rule ids this
check relates to; `[]` when none does.

### `GET /api/claude-md`

Every CLAUDE.md-family file on disk (user, project, local, `.claude/rules`
and nested files seen in transcripts), with how often it was sent in the
window and what that cost. File text is read now and never stored.

Query: the windowing params above.

`data`: `{"period", "transcripts", "files": [{"id", "path", "name", "level", "project", "who", "tokens", "scoped", "sections", "seen", "sends", "reach", "reach_text", "cost_usd", "cost_text", "findings": [str, ...], "fix_count"}, ...]}`.
`id` is a 16-character hex hash of the path. `path` is the file's path
with your home folder written as `~` (`footprint.home_label`): the one
kind of path this API returns (see "Privacy" above).

### `GET /api/claude-md/<id>`

One file's sections, duplicates, stale references and fixes. `404` for
an unknown id, and for an id that is not 16 hex characters.

Query: the windowing params above.

`data`: `period`, the list entry, plus `section_rows` (`heading`,
`level`, `line`, `tokens`, `share` as a fraction of the file, `cost_text`,
`agents`), `imports` (paths, `~`-relative), `duplicates` (`line`,
`excerpt`, `tokens`, `also_in: [{"file", "line"}]`), `stale` (`line`,
`reference`, `kind`), `cost_by_reach` and `fixes`.

### `GET /api/skills`

Every skill Claude Code listed in the window: its description (read now
from the newest transcript's skill listing, never stored), where it
comes from, how often it was listed and used, and what the listing cost.

Query: the windowing params above.

`data`: `{"period", "skills": [{"name", "description", "source", "source_label", "path", "listing_tokens", "listed", "listed_text", "invoked", "invoked_by", "listing_cost_usd", "listing_cost_text", "use_cost_text", "use_text", "resent_tokens", "status", "hidden", "needed_by", "fixes"}, ...], "listing_tokens", "listing_cost_text", "unused", "needed_by_a_tool", "fixes"}`.
Skills come unused first, then by listing cost. `status` is `unused`,
`used`, `listed`, `not listed`, `needed by a tool`, `no longer listed` or `hidden`:
`~/.claude/settings.json` already keeps a `hidden` skill out of the
listing, and `hidden` says why ("skillOverrides sets it to off", "its
plugin is turned off"); such a skill gets no fixes. `needed_by` names
the Claude Code tool (`Artifact`, `Workflow`) that tells Claude to load
a built-in skill, or `""`; one Claude never used is `needed by a tool`,
never counted as unused or hidden, and its one fix lists it by name
only. A skill with no file on disk that no listing has named for 14
days before the newest one in the window (one you deleted) has
`source` `removed`, is `no longer listed` and gets no fixes. `path` is
`~`-relative, or `""` when the skill has no file on disk. Each skill's own `fixes` hide it or
shorten its description; the top-level `fixes` holds one change that
hides every unused skill at once, when there are two or more.

### `GET /api/profile-goals`

Without `goal`: `{"goals": [{"id", "title", "what"}, ...]}`, the goals a
profile can start from (`profiles/goals.py`): `recommendations`,
`subagents`, `models`, `cache`, `compaction`, `thinking`, `tasks` and
`current`. With `goal=<id>`: that goal's draft. An unknown goal is `400`.

Query: `goal`, `task` (for `tasks`: a kind of task from the capture
vocabulary, `feature` ... `chat`; any other value is `400`), plus the
windowing params above (used only with `goal`).

`data` (with `goal`): `{"goal": {"id", "title", "what"}, "period", "from_current", "tasks", "task_labels", "task", "note", "candidates": [{"key", "agent", "label", "now", "value", "ticked", "evidence", "what", "tradeoff", "note", "estimate"}, ...], "profile": {"settings", "agents"}, "whatif"}`.
For `tasks`: `tasks` lists the kinds of task in the Work habits
section's `habits_setups` table, `task_labels` maps each to its plain
name (`"bugfix"` to `"Bug fix"`, from `capture_catalogue.TASK_LABELS`),
`task` is the one drafted (the one asked for when it's there, else the
first with a cheaper setup) and `note` says what was found; other goals
return `[]`, `{}`, `null` and `null`.
A candidate is ticked only when the data supports it; the main model is
never pre-ticked. The goals that start from recommendations
(`recommendations`, `subagents`, `models`) leave out the ones ignored in
this `project` under the active profile, unless they now suggest
something else (`ignores.skip_keys`); so do the quick actions' fixes. `tasks` also drafts a cheaper-model candidate (`key`
`"model"`, `agent` the subagent type) for each agent type that most
answered that kind of task, from the Work habits section's
`habits_agents_by_task` table, vetoed the same way as the `models`
goal's own draft. `estimate` is that one change's `POST /api/whatif`
row; for `tasks`, its `saving_usd` and `effect_text` are then scaled to
that task's own share of the window (`habits_by_task`, or the agent's
task share of its own cost for an agent candidate) — `saving_usd` is
`null` and `basis` says why when there's no clean share to scale by.
`profile` holds the ticked changes and `whatif` their combined
estimate, scaled the same way for `tasks`. `current` returns no
candidates (`from_current: true`): the dashboard saves your current
settings with `POST /api/profiles/from-current` instead.

### `GET /api/impact`

Each change you made (an `apply`, its undo, a settings change the
config hook saw, or a change to metrics capture), and each model,
effort or CLAUDE.md size change your sessions show, with the sessions
before it against those after it, on the measures that change should
move.

Takes no window: each change is compared over its own before and after
periods, looking back at most `lookback_days`.

`data`: `{"changes": [{"change": {"ts", "source", "label", "keys", "changes", "backup_ts", "reverted", "project", "project_name", "summary"}, "before_sessions", "after_sessions", "enough", "gate", "verdict", "measures": [{"key", "label", "kind", "better", "before", "after", "before_value", "after_value", "before_n", "after_n", "change_pct", "direction", "p", "label_key", "label_text"}, ...], "quality": [{"group", "label", "before_runs", "after_runs", "verdict", "judged", "min_runs", "signals": [{"key", "label", "kind", "worse_when", "unit", "before", "after", "before_text", "after_text", "before_counts", "after_counts", "before_runs", "after_runs", "p", "label_key", "verdict"}, ...]}, ...], "without": {"paid_usd", "without_usd", "saved_usd", "fidelity", "fidelity_text", "basis", "sessions", "text", "since_text", "per_key": [{"key", "agent", "fidelity", "fidelity_text", "saved_usd", "saved_text", "basis"}, ...]} | null}, ...], "caveat", "min_sessions", "lookback_days"}`.
Newest change first, at most ten. `change.source` is `apply`, `revert`,
`config` (a settings change the hook saw), `capture` (a metrics
capture change from `capture-log.jsonl`, whose keys are `capture.<field>`
and are measured by capture's own tokens per session and the share of
messages Claude tagged) or `transcript` (a change only the sessions
show). `changes` lists `{"key", "agent", "old", "new"}` where the values
are known: a `config` change records a setting's values only when both
are plain values of at most 80 characters. `summary` is those changes
in one line ("model: opus → sonnet"), then any other changed key by
name. `project` is empty for a change that applies in every project;
otherwise the change was made in one project's own settings files, it
is judged on that project's sessions only, and `project_name` is
that project's value in the project filter (empty when no session
loaded is from it). `label_key` is a measure's
ratio-test reading (`lower`, `possibly_lower`, `higher`,
`possibly_higher`, `no_clear_change` or `too_little_data`) and
`label_text` the same in words. `kind` is the measure's unit (`money`,
`pct`, `tokens` or `count`), `before_value`/`after_value` its raw
figures (USD for `money`), and `better` which way is good: `lower`, or
`null` for a measure where neither is (the share of messages tagged). `enough` is false until each side has
`min_sessions` sessions; `gate` is the same check as a structured
`{"reason": "min_sessions", "have", "need"}` object for a UI empty
state, or `null` once `enough` is true. `before`/`after` are display
text in the billing mode's units; `direction` is `lower`, `higher`,
`same` or `null`. For an `apply` that is not yet undone, `backup_ts` is
what `claudeglass apply --revert <backup_ts>` takes.

`without` is what the sessions after the change would have cost
without it (`counterfactual.py`), or `null` with fewer than
`min_sessions` sessions after it. `fidelity` says how:

| `fidelity` | For | How |
|---|---|---|
| `repriced` | `model`, `fastMode` | The same replies at the old model's prices, or with fast mode the other way. An old model that wasn't set is the one the sessions before ran on. |
| `simulated` | cache lifetime keys; an `autoCompactWindow` the change raised | The same replies replayed under the old lifetime, or the old, smaller window. A lowered window can't be undone: its compactions happened. |
| `approximate` | a CLAUDE.md size change, MCP servers, plugins, skills | The context the change removed or added, carried on every main-session reply. |
| `before` | anything else, and any change to several settings at once | Each session after the change at the cost per reply of the sessions before it that did the same kind of work. |

`paid_usd` and `without_usd` are list prices over the whole of each
session after the change; `saved_usd` is their difference (negative
when the change cost more). `text` is the card's first line ("Without
this change: about X. You paid Y, so it saved about Z."), `since_text`
the end of the Overview's "Without your last change (…), " sentence,
and `basis` what was priced. `per_key` has a row per setting a method
above covers; with several settings the rows overlap, so they don't add
up to the headline, which then uses `before`.

`quality` judges the change on the runs of each agent it changed (or
the main session, for any other setting): one entry per group, with a
one-line `verdict` and every quality signal
([concepts](concepts.md#7-quality-signals)). `judged` is false when
every signal had too little data (fewer than `min_runs` runs on a
side); Setup › Settings folds those groups into one line. `kind` is
`pct` for a share or `per_run` for a mean per run. `label_key` is `worse`,
`better`, `possibly_worse`, `possibly_better`, `higher`, `lower`,
`possibly_higher`, `possibly_lower` (the last four for neutral measures
such as replies per run), `no_clear_change` or `too_little_data`; `p` is
the two-sided p-value before the Holm correction, `null` with too little
data. `worse_when` is `"higher"`, or `null` for a neutral measure.

### `GET /api/backtest`

Did your estimates come true? Every prediction `POST /api/whatif` has
logged (with `"log": true`), matched to the change point it turned
into and judged against the sessions before and after that change —
the same before/after windowing and ratio test `/api/impact` uses
(`backtest.py`). Takes no window: each prediction is judged over its
own before and after periods.

`data`: `{"predictions": [{"id", "ts", "source", "measure_key", "agent", "predicted_usd", "predicted_pct", "fidelity", "seen_at", "change_ts", "judged_at", "verdict", "measured_usd", "measured_pct", "predicted_text", "measured_text", "verdict_text"}, ...], "judged_just_now", "verdicts"}`.
Newest prediction first. `verdict` is `null` until a matching change
point closes the window enough to judge it, then one of the closed set
`as_estimated`, `smaller`, `larger`, `opposite` or `too_little_data`
(`verdicts` lists them). `predicted_text`, `measured_text` and
`verdict_text` are server-formatted, billing-mode-aware sentences
(`backtest.present`) — the dashboard never formats a dollar amount or a
verdict itself. `measured_usd`/`measured_text` stay `null` until
judged. `judged_just_now` is how many predictions this call judged for
the first time (a stale answer can be served while a change is worked
out in the background, as with `/api/impact`).

### `GET /api/setup`

What this tool installed and changed on this machine, what each piece
costs in tokens and how to undo it, plus what to expect
(`footprint.py`). Used by the dashboard's Data quality page.

`data`: `{"items": [{"key", "title", "status", "where", "what_it_does", "token_cost", "undo"}, ...], "expectations": [{"title", "text"}, ...], "uninstall_command"}`.

### `GET /api/setup/status`

Whether each part of the setup works (`setup_status.check_setup`), for
the Overview's Setup card and Data quality: the same checklist
`claudeglass status` prints. This dashboard answering is proof it
runs, so only whether it starts at logon is asked, through the same
cached probe as `/api/health`'s `service_registered`; `null` from that
probe means "couldn't tell", never a problem.

`data`: `{"items": [{"key", "label", "state", "word", "detail", "fix", "essential"}, ...], "done", "needs_attention", "verdict"}`.

- `items`, in the order `init` sets them up: `billing`, `hook`,
  `service`, `capture`, `skill` and `statusline`. `state` is `ok`,
  `waiting` (set up, but nothing has happened yet to prove it works),
  `problem` or `off` (left off by choice), and `word` says it the way
  the CLI does (`Done`, `Waiting`, `Needs attention`, `Off`). `detail` is
  one or two plain sentences, with the home folder written as `~` and no
  other path. `fix` is a command to copy, or `null`.
- `done`: every `essential` item is `ok`. The Setup card shows until it
  is.
- `needs_attention`: how many items are a `problem`.
- `verdict`: "Everything's set up." or "N things need attention."

### `GET /api/capture`

Metrics capture for Setup › Capture and the banner on every page
(`capture_view.view`): the setting, each level and metric with what it
captures, why and what it costs on your own usage, and what capture
has cost since it was turned on. Built from `[capture]` in
`config.toml`, the metric catalogue (`capture_catalogue.py`), the
last 14 days replayed as if capture had been on (`capture.estimate`),
and the notes and tags measured in transcripts since `enabled_at`
(`capture.usage`). The replay is kept for 30 minutes and the measured
part until the store changes; an older copy is served while a fresh
one is built in the background.

`data`: `{"config", "warning", "samples", "levels", "sections", "measured", "history", "hooks", "billing", "roi", "banner", "feedback", "commands"}`:

- `config`: the same block as `/api/health`'s `capture`, without `hooks_ok`.
- `warning`: the cost warning the dashboard repeats before any change
  that uses more tokens.
- `samples`: the allowed sampling percentages, `[100, 50, 25, 10]`.
- `levels`: one card each for `off`, `free`, `essentials`, `standard`,
  `deep` and `custom`: `title`, `summary`, `adds` (metric titles over
  the level before), `metrics`, `asks_claude`, `current`, `rough`
  (token sizes from the catalogue) and `estimate` (`tokens_per_week`,
  `tokens_text`, `usd`, `text`, `share_pct`, `share_text`; `null`
  when it costs nothing or there is no history).
- `sections`: the metrics grouped as on the page. Each has `id`,
  `kind` (`level`, `derived`, `feedback` or `coaching`), `group`,
  `title`, `what`, `why`, `powers`, `tag` (what Claude writes),
  `hooks`, `requires`, `on`, `toggle` (`false` for metrics that are
  always measured), `asks_claude`, `needs_hook` (on, but its hook
  entry is missing), `needs_install` with `install_note` and
  `install_command` (the `/tl-feedback` skill is on but its file is
  missing, out of date or someone else's: the dashboard never writes
  Claude Code's folder, so it names the CLI command), `statusline_note`
  (a status-line toggle is on but Claude Code's status line isn't this
  tool's), `estimate` and `actual` (`{usd, text}` a week, and over
  `actual_label`: since it was turned on, or the last 14 days for the
  skill and, with how many there were, for coaching notes), and `answers`/`target`/`enough` (whether enough has been
  collected for firm suggestions; for the skill and the dashboard
  rating, the runs answered and the sessions rated).
- `measured`: `null` while off; otherwise `since`, `sessions`,
  `subagents`, `notes`, `note_tokens`, `tag_tokens`, the amount and
  share of spend, coverage (`coverage_pct`: the share of messages
  Claude tagged; `report_coverage_pct` for agent reports), `scopes`
  (`main`, `subagent`, `tool`, `brief`) and a `daily` series.
- `history`: what the estimates replay (`days`, `sessions`,
  `subagents`, `cycles`), `null` with no history.
- `hooks`: `ok` (`true` when nothing is missing and no settings policy
  stops the hooks running), `summary`, `missing`, `missing_events`,
  `problems` (a count: problem text can hold a path),
  `connect_command`, and `blocked_by`: `null`, or why Claude Code won't
  run the user's own hooks at all — `managed_only` (managed
  `allowManagedHooksOnly`), `all_off_managed` (managed
  `disableAllHooks`) or `all_off` (`disableAllHooks` in the user
  settings.json). `connect_command` can't fix a `blocked_by`.
- `billing`: `mode` and `basis` (what the amounts are).
- `roi`: what capture is costing against what depends on it, both
  spread over a week (`capture.weekly_cost`,
  `habits.capture_dependent_value`): `{"cost", "value", "measured"}`,
  `cost` and `value` each `{usd, text}` amounts in billing units
  (`units.Units.money`). `null` while there's no start time to price a
  weekly cost from (capture off, or turned on too recently). `value` is
  `null` and `measured` is `false` while nothing measured yet — no habit
  worth trying whose evidence needs capture's reports or your feedback —
  depends on either; the dashboard says so instead of showing a zero.
- `banner`: `on`, `headline`, `notes` (end time passed, hook entries
  missing, no notes seen, low coverage, enough collected, the skill
  needs installing, what capture costs a week against what depends on
  it) and `feedback_note`.
- `feedback`: `skill` (`installed`, `outdated`, `foreign`, `missing`,
  or `null` while the skill is off), `runs` and `answered` (its runs
  over the last `days` days), `ratings` (sessions rated on the
  dashboard, `null` while that is off), `questions` (as in
  `GET /api/session/<id>`'s `feedback_questions`) and `brief_skill`
  (the `/tl-brief` skill's file, in the same words as `skill`, or
  `null` while brief templates are off).
- `commands`: the `status`, `connect`, `feedback` and `brief` CLI
  commands.

`409` with the `claudeglass capture status` command in
`error.commands` when `config.toml` can't be read.

### `GET /api/report.md` / `GET /api/report.html` / `GET /api/report.json`

The full report in each format, built from the store instead of a fresh
parse — byte-equivalent in content to running the CLI's `report
--phases` subcommand with `--json`/`--html`/(default) over the same
window (the service always builds the `phases` section),
modulo the "verified against CLI JSON" test the plan's Milestone v0.2
Tests bullet requires (`tests/test_service_api.py`, built alongside
`api.py`). All three return the raw rendered document on success, not
the envelope above: `report.json` as `application/json`, and
`report.md`/`report.html` as `text/markdown`/`text/html` (UTF-8),
matching the CLI's own stdout. Errors still use the JSON envelope.

Query: `window`, `window_days`, or `since`/`until` (see "Report-backed routes:
windowing query params" above) — this is what makes `/api/report.json?
since=...&until=...` byte-equivalent to `report --since ... --until
...`, not just to `report --days N`.

`report.json`'s `meta` carries `billing_mode`/`amounts_basis` (the
report's own headline billing-mode facts) and, alongside them,
`meta.units`: `{mode, share_per_usd, period_label, basis}` (UX-1) --
the same facts in the shape `Units.money`'s JS mirror (`format.js`'s
`money()`) needs to phrase an arbitrary amount client-side without a
round trip through a table cell. `mode` is `billing_mode`;
`share_per_usd` is the percentage points of the weekly usage limit one
list-price dollar is worth, or `null` without an accepted elasticity
fit yet; `period_label` is what that share is "of" (`"weekly usage
limit"`); `basis` repeats `amounts_basis` so a consumer of `meta.units`
alone still has the caveat text.

`meta.rates` (additive): every priced model's own rates and a few
derived ratios, keyed by canonical model id -- the dashboard's own
rate card, without a second round trip to read `pricing.toml` itself.
Only models `pricing.toml` prices are keys here. Each entry carries
`input`, `output`, `cache_write_5m`, `cache_write_1h`, `cache_read`
(USD per million tokens, `pricing.toml`'s own field names);
`cache_read_ratio`, `cache_write_5m_ratio`, `cache_write_1h_ratio`
(each of those rates divided by that model's own `input` rate, or
`null` if `input` is zero); and `input_ratio_to`, a `{model_id:
ratio}` map of that model's `input` rate as a multiple of every other
priced model's `input` rate.

`meta.projects`: every project slug with a session in this window
(already redacted -- see "Privacy" above), sorted by this window's cost
descending, ties broken alphabetically (the same `(-cost, slug)` order
`usage.by_project`'s rows already sort by). This is the list a `project`
filter (below) accepts and the dashboard's project picker can render
without a second request.

### Filtering by project

Every report-backed route (`/api/report.json`/`.md`/`.html` and every
per-section route: `/api/ttl`, `/api/carry`, `/api/recommendations`,
`/api/quick-actions[/<id>]`, `/api/compaction-sim`, `/api/model-swap`,
`/api/waste`, `/api/config-diff`, `/api/diagnostics`,
`/api/claude-md[/<id>]`, `/api/skills`, `/api/profile-goals`, `/api/whatif`),
plus `/api/summary`, `/api/sessions`, `/api/daily-usage` and
`/api/compactions`, additionally accept a `project=<slug>` query param
(additive). `<slug>` is one of `meta.projects`'/`/api/sessions`'
already-redacted slugs -- never the raw, unredacted slug a filesystem
path could embed a username in, since the API never hands one out
(see "Privacy" above). The route narrows to that project's sessions
only: fewer sessions, fewer transcripts, and (for report-backed routes)
a report built from just that subset -- the same shape as an unfiltered
response, just scoped.

An unrecognized or malformed `project` (a slug redacting to no known
project in the current store) is a `400 bad_request`, same envelope as
every other malformed query param above -- the message never echoes the
given value back, only that `project` was the problem. The report cache
(below) keys on `project` alongside the window, so two different
`project` values for the same window never share a cache entry.

The dashboard's project picker lists every project's
`report.json` `meta.projects` for the window, then sends the picked
slug as `project=` with every window-aware request, the Overview's
previous-window `/api/summary?since=&until=` included. Routes that
don't take the filter (`/api/impact`, `/api/backtest`,
`/api/baseline`, `/api/recache`) keep covering every project,
and the dashboard's "All time" chip beside what the first three draw reads
"All time, all projects" while a project is picked. It checks an unknown slug once with
`/api/sessions?limit=1&project=` and, on the `400`, falls back to every
project.

## Mutating routes

The `POST` routes. All but `POST /api/whatif` write something, each
scoped to a single row or file and never touching `~/.claude` proper
(plan: "neither touches `~/.claude` proper"). Nothing here changes
Claude Code's settings: a saved profile takes effect only when you run
the `apply` command or give Claude the prompt that
`GET /api/profiles/<id>/diff` returns.

### `POST /api/sessions/<id>/tags`

Body: `{"key": "mode"|"purpose", "value": str}`. Calls `Store.set_tag`
(the same override `config.sessions.toml` holds for the CLI). `404` if
`<id>` is unknown; `400` if the body is not a JSON object, `key` isn't
`mode`/`purpose` or `value` isn't a string (the cross-site checks above
run first — see "Cross-site protection"). The tag is merged into the
session overrides every report-backed route classifies sessions with,
taking precedence over `sessions.toml`.

`data`: `{"session_id": str, "tags": {key: value}}` (the session's full
tag set after the write).

### `POST /api/sessions/<id>/feedback`

Your rating of a session: the `/tl-feedback` questions as checkboxes,
kept in this tool's own store (the `session_feedback` table), so it
costs no tokens. The session drawer on Spend › Sessions shows the form
while the dashboard rating is switched on; the route itself works either
way.

Body: `{"outcome": word|null, "slow": [word], "worth": word|null,
"helped": [word]}`, any key left out counting as nothing ticked. The
words are `capture_catalogue.FEEDBACK_VOCAB`'s, never free text:
`outcome` is `met`, `partly`, `missed` or `stopped`; `slow` any of
`unclear`, `rework`, `tools`, `none`; `worth` is `yes`, `fair` or
`no`; `helped` any of `context`, `plan`, `smaller`, `none`. A body
with nothing ticked clears the rating. `404` if `<id>` is unknown;
`400` if the body is not a JSON object, has another key, or a word
isn't one of these (the cross-site checks above run first).

`data`: `{"session_id": str, "feedback": {...} | null}` (as in
`GET /api/session/<id>`).

### `POST /api/capture`

Changes metrics capture in this tool's own `config.toml` (`[capture]`,
through `config.set_capture`, which writes atomically and logs the
change to `capture-log.jsonl`). It never touches Claude Code's
`settings.json`: when a chosen metric needs a hook entry that isn't
there, the answer's `hooks` block names the `capture connect` command
to run.

Body: a JSON object with any of these keys:

- `level`: `off`, `free`, `essentials`, `standard` or `deep`.
- `metrics`: the level metrics to capture (a custom set). Not with
  `level`.
- `feedback`, `coaching`: the feedback and live-coaching items to
  switch on (the list replaces the current one).
- `sample`: `100`, `50`, `25` or `10` (percent of sessions).
- `until`: an ISO 8601 time in the future when capture switches
  itself off, or `""` for no end.

Only from this machine: `403` unless the request comes from a
loopback address, on top of the cross-site checks above (a service
bound to `0.0.0.0` for a container still can't be switched from
another machine). `400` on an unknown key or value, both `level` and
`metrics`, or a past `until`. `409` when `config.toml` can't be read
or written (such as a read-only file system); its `error.commands`
lists the `claudeglass capture ...` commands that make the
same change from a terminal. The message never quotes the error,
which can hold a path.

`data`: `GET /api/capture`'s answer after the change, plus
`changed` (`false` when it was already set that way).

### `POST /api/profiles`

Body: a profile document's JSON form (the same shape a TOML profile
round-trips to — `id`, optional `name`/`for`/`archetype`/`notes`,
optional `settings`/`agents`/`env` tables), validated by
`profiles.schema.load_dict` before anything is written. The
cross-site checks above run first (`403 forbidden` for a cross-site
request, `400 bad_request` for a wrong `Content-Type` — see "Cross-site
protection"). `400` (`error.code: "bad_request"`) if the body is not a
JSON object, or if the schema rejects an unknown key or
an out-of-range value — the schema's own problem text, joined with
`"; "` (plan: "the schema rejects anything else so a profile can never
promise an effect the harness cannot deliver"). `409`
(`error.code: "conflict"`) if `id` names one of the eight shipped
catalogue profiles — a catalogue id can never be created or overwritten
this way, regardless of `?replace=1` — or if a user profile with that
`id` already exists and `?replace=1` was not given.

On success, writes `<config_dir>/profiles/<id>.toml` atomically (temp
file + rename — never a half-written file) and re-ingests it into the
store immediately via `Store.upsert_profile`, so the very next
`GET /api/profiles` reflects the write without waiting for the
watcher's next tick.

Query: `replace` — `1` allows overwriting an existing *user* profile's
file (never a catalogue one).

`data`: `{"id": str, "name": str, "source": "user", "updated_at": str}` — `201` on success.

### `POST /api/profiles/from-current`

Saves your current settings as a user profile ("Save my current
settings as a profile" on Setup › Profiles). It reads the latest config
snapshot's `effective` settings and `effective_agents`, keeps only the
keys a profile may set (each checked on its own with
`profiles.schema.validate`, so one out-of-range value drops only
itself), leaves out keys your organisation's managed settings control,
and writes the result exactly as `POST /api/profiles` does. It writes
only this tool's own profile folder, never Claude Code's config.

Body (optional): `{"id": str, "name": str}`. Defaults:
`my-current-settings` and "My current settings".

Query: `replace` — `1` overwrites an earlier save with the same `id`.
Without it, a second save is `409` (`error.code: "conflict"`, message
"... already exists ..."); the dashboard then asks before replacing.
`409` also when no config snapshot has been recorded yet.

`data`: the `POST /api/profiles` result plus `skipped_managed`: the
allowlisted setting names left out because managed settings control
them. `201` on success.

### `POST /api/whatif`

The estimated effect of a set of changes on the window, looked up in the
report's own tables (`whatif.py`). It writes nothing; it is a POST only
because the changes travel in the body. Behind the cross-site guard like
the other POST routes.

Body: `{"settings": {...}, "agents": {"<agent>": {...}}}`, checked with
`profiles.schema.validate` (`400` on a bad key or value, or when the
body, `settings` or `agents` is not a JSON object).

Query: the windowing params above, and `task`: one kind of task from
the capture vocabulary, or several comma-separated. Every row is then
scaled to that task's share of the window (several tasks' shares add up),
and a row with no per-task cost to scale by is not estimated. An unknown
task is a `400`.

`data`: `{"period", "rows": [{"key", "agent", "value", "saving_usd", "fidelity", "fidelity_text", "basis", "effect_text", "uncalibrated_usd", "uncalibrated_fidelity"}, ...], "total_usd", "total_text", "estimated", "not_estimated", "total_note"}`.
`saving_usd` is `null` when a change is not estimated. `fidelity` says
how it was worked out (`fidelity_text` in plain words) and `basis`
explains it in a sentence: `"ceiling"` (a `model` change -- the same
tokens repriced at the new model's rate, same "ceiling" sense as
`/api/model-swap`'s own saving column, not a real simulation since a
different model may need more or fewer replies for the same work; for
a subagent, only its runs started without a model of their own, and
`"none"` when every run's model came from a workflow script or the
spawn),
`"simulated"` (`autoCompactWindow`, a cache-TTL change -- real sessions
replayed with the new value), `"measured"` (`omitClaudeMd` -- the
greater of per spawn times the spawns in the window, and the carry cost
of the turns it's read back from cache until re-sent, EST-P10),
`"estimated"` (`skillOverrides`,
`enabledPlugins` -- from the size of what stops being sent), `"none"`
(not estimated) or `"calibrated"` (EST-P6: scaled by how this same kind
of change actually turned out for you before, see below).
`effect_text` and `total_text` are in the billing mode's units;
`estimated`/`not_estimated` are counts of rows.

**EST-P6 calibration.** Once at least
`backtest.MIN_JUDGED_FOR_CALIBRATION` (3) of your own past predictions
for the same `(agent, key)` have been judged (`GET /api/backtest`), a
row's `saving_usd` here is scaled by the mean of those predictions'
`measured_usd / predicted_usd` ratios (`backtest.calibration_multipliers`)
and its `fidelity` becomes `"calibrated"`. `uncalibrated_usd` and
`uncalibrated_fidelity` hold the value and fidelity calibration
replaced — `null` on every row where calibration wasn't applicable or
didn't apply.

**EST-P5 logging.** Body may also carry `"log": true`. Every row whose
saving could be estimated is then appended to this tool's own
`prediction-log.jsonl` (`config.append_prediction_log`) — always the
*uncalibrated* estimate (`uncalibrated_usd`/`uncalibrated_fidelity`
when present, `saving_usd`/`fidelity` otherwise), so calibrating an
already-calibrated number never compounds. The dashboard sets this
only for a change you mean to track, not for interactive "what if"
exploration. A logged row reaches `GET /api/backtest` once the file
watcher's next tick ingests it (`service.watcher._scan_predictions`)
and `POST /api/predictions/seen` marks it shown.

### `POST /api/predictions/seen`

Records that the dashboard has actually shown you one logged
prediction (`Store.mark_prediction_seen`).

Body: `{"id": str}` — the `predictions` row id from `GET /api/backtest`.
`400` when `id` is missing or not a non-empty string.

`data`: `{"id", "seen"}`. `seen` is `false` when the id doesn't match
any prediction, or it was already marked seen.

## Managed-settings routes

Any route whose `data` would include a recommendation or a diff whose
`lever` targets a managed-settings key still returns `200`/`ok: true` —
the managed-ness is carried in the payload (`scope: "managed"`, per
`model.py`'s `Recommendation.scope`) rather than as an HTTP error, so
the UI can render "managed by policy, raise with your administrator"
inline (plan "Enterprise use"). No route writes a managed key into
Claude Code's config, because no route writes Claude Code's config at
all. `POST /api/profiles` may save a managed key into a profile file;
`apply` skips it when the profile is applied, and the profile diff
marks the row `managed`. `POST /api/profiles/from-current` leaves
managed keys out and lists them in `skipped_managed`.

## Report routes: how they are computed

Implementation notes for `service/api.py` (S1-api), for a future reader
of this frozen contract who needs to know how the report-backed routes
(`/api/ttl`, `/api/carry`, `/api/compaction-sim`, `/api/model-swap`,
`/api/waste`, `/api/config-diff`, `/api/recommendations`,
`/api/diagnostics`, `/api/claude-md*`, `/api/skills`,
`/api/profile-goals`, `/api/quick-actions*`, `POST /api/whatif`,
`/api/report.md`/`.html`/`.json`) get their data, and where the
implementation had to make a call this document didn't spell out.

**Rebuild, not re-parse.** Every report-backed route rebuilds a
`Corpus` via `service.rebuild.corpus_from_store(store, days=window_days,
since=since, until=until)` (S1-watcher's module — see
`service/__init__.py`) and runs it through the same
`report.build_report()` → `recommend.recommend()` →
`render/{json_out,markdown,html}.py` pipeline the CLI's own `report`
subcommand uses. `/api/ttl`, `/api/carry`, `/api/compaction-sim`,
`/api/model-swap`, `/api/waste`, `/api/config-diff` and
`/api/recommendations` all build the *same* full report for the
requested window (`window_days`, or `since`/`until` — see "Report-backed
routes: windowing query params" above) and read one section/field back
out of it
(`/api/ttl` returns the assembled report's `"ttl"` `Section`;
`/api/carry`, `/api/compaction-sim`, `/api/model-swap` and `/api/waste`
likewise each return their own like-named `Section` (`"carry"`,
`"compaction_sim"`, `"model_swap"`, `"waste"`) — all four `null` rather
than an error when the section is absent from the assembled report;
`/api/config-diff` returns its `"config"` section's
`config-diff-<key>` table(s) — `report.py`'s own
`_build_config_section`, capped at 20 changed keys — rather than
recomputing `snapshots.build_config_diff_table` a second time with a
service-specific session-metrics rebuild the way the CLI's own
`config-diff` subcommand does; `/api/recommendations` returns
`model.recommendations`) rather than each running an independent,
narrower computation. A `key` that names a config key which didn't
change in the requested window returns `{"ok": true, "data": []}`, not
an error.

`_build_report_model` passes `phases=True`, so Spend › Usage can show
cost by phase; on a 30-day store it adds about 1% to the build. The
CLI keeps the section behind `--phases`.

`_build_report_model` also passes `config_dir=options.config_dir` to
`build_report()` (v4 wiring round) so that `waste.py`'s salted
session-id hash reads/writes its salt file inside this service's own
`config_dir` rather than falling back to `report.py`'s
`_default_waste_config_dir()` OS-temp-directory default — the fallback
exists only for callers (tests, `baseline.py`, `team.py`) that never
had a `config_dir` of their own to give it.

**Memoization key: `Store.change_token()`.** Rebuilding a full report on
every request would make every page switch in the UI (`docs/ui.md`)
re-parse the whole corpus. The implementation caches the assembled
`ReportModel` in-process, keyed by `(window_days, since, until,
change_token)` (a named `window` is first turned into its `since`,
rounded to the minute), where `change_token` is `Store.change_token()` (S1-integration fix 1.f),
paired with `config.toml`'s modification time — a
single string combining `(COUNT(*), MAX(updated_at))` over `transcripts`
and `(COUNT(*), MAX(ts))` over `snapshots`. A cache hit only requires
this token to be unchanged since the entry was built; any transcript or
snapshot insert/update moves it, forcing a rebuild on the next request.

**`/api/report.json`/`.md`/`.html` are unwrapped on success.** Their
body on `200` is the renderer's own native output (`render_json`/
`render_markdown`/`render_html`), not the `{"ok": ..., "data": ...}`
envelope — this is what makes `/api/report.json` byte-equivalent to
`claudeglass report --json` for the same window, and matches this
document's own "the raw rendered document" language for `.md`/`.html`.
A request error on one of these three routes (a bad `window_days`,
`since` or `until`, or an unexpected exception) still falls back to the
normal JSON error envelope; only the success path is raw.

**`{{page:<page>}}`/`{{page:<page>/<segment>}}` tokens (`pages.py`) survive
into every JSON response**, `/api/report.json` included: help text, table
notes, recommendation `action`/`estimated_saving` and similar fields can
carry one, and the dashboard's own `links.js` turns it into a link. They
never appear in a recommendation's `why`/`title` or in `fixes[].prompt`/
`fixes[].command` (those feed a prompt or a standalone command, never
dashboard markup). `render_json`/`to_jsonable` never call `pages.plain()`.
`render_markdown`/`render_html` do, on every field that can carry a
token, so `/api/report.md` and `/api/report.html` -- unwrapped native
output from the very same renderers `report --format md`/`--format
html` calls -- show the plain label ("Spend › Usage"), not the token;
this keeps the byte-equivalence above, since both callers still run the
identical renderer. `report --json`/`/api/report.json` are the one pair
that stay byte-equivalent *with* the token still in place, for the same
reason: both call the same `render_json`.

**Tables carry display-only fields.** Every `Table` in a report-backed
response (`/api/report.json`, each section route, `/api/config-diff`,
`/api/diagnostics`) carries, beside `name`, `title`, `columns`, `rows`
and `notes`, the fields `helptext.annotate` fills in for the dashboard:
`help` (`{shows, read, act}`), `value_labels` (raw cell value -> display
label), `row_groups` and `row_kinds` (for a long "metric / value"
table), `dashboard` (`keep`, `advanced` or `report`) and, additive,
`lead_columns`. `lead_columns` lists column keys in the order the
dashboard shows them first: on a wide table at most 7, the row key
first, with the rest behind the grid's column chooser; on a one-row
summary table (such as `waste_summary`) at most 4 headline values, shown
as tiles. An empty list means the grid's own default, the first 7
columns. None of these fields changes `rows`: recommendation evidence
and the CSV export keep the raw values, and the Markdown and HTML
renderers ignore `lead_columns`.

**`GET /api/session/<id>` returns a superset of the listed fields.**
`Store.session()`'s dict includes `mode_source`/`purpose_source`
alongside every field `/api/sessions` lists — a non-breaking addition,
not a contradiction of the field list above (which describes the
session-summary fields plus `transcripts`/`tags`, not an exact field
count), and dropping fields `Store` already computes for no privacy
reason would only lose information a client might want.

**`/api/profiles/<id>/diff` and `POST /api/profiles` are real routes as
of v0.3**, no longer the `501 not_implemented` stubs an earlier version
of this document described at S1-api's own delivery time (before
`profiles/schema.py`/`profiles/diff.py` existed). See their own
sections above for the shipped shapes.

**`report.meta.projects` can differ from the CLI's for the identical
window (release-verification finding, accepted, not a bug to fix
here).** The CLI's `--all-projects` passes `build_report` every project
*directory it resolved on disk* (`discovery`'s own directory scan,
filtered by `--project-family`/`exclude_projects` but never by whether
that project has any sessions at all), while a report-backed route
derives `projects` from `{bundle.slug for bundle in corpus.sessions if
bundle.slug}` — only projects the store actually has session rows for.
A project directory that exists under `--projects-root` but has never
had a single parseable transcript in it (an empty/leftover directory —
confirmed against a real corpus during v0.2 release verification, e.g.
a stray directory containing no `.jsonl` files at all) shows up in the
CLI's `meta.projects` and never in the API's, for *any* window,
independent of `since`/`until`/`window_days`. Matching this exactly
would mean a report-backed route reading live directory names from
`options.projects_root` — a live-filesystem dependency the whole
store-rebuild design (`service/rebuild.py`'s module docstring) exists
to avoid, for one purely cosmetic field. Left as-is rather than
special-cased.

**Static file serving.** `/` and `/static/*` serve
`service/static/index.html`/assets (the dashboard's own files, served
as they are with no build step, per `docs/ui.md`) when present, guarded
against path traversal
(`Path.resolve()` plus a parent-containment check — a `..` segment or
an escaping resolved path is `404`, not an error). When
`service/static/index.html` is missing or unreadable, `/` falls back to
a small, non-persisted placeholder page generated at request time
rather than anything written to disk or committed to the repository. `make_handler()` accepts an additional
keyword-only `static_dir` parameter (default: the package's own
`service/static/`) so a test can point it at a directory with real
files without writing into the source tree.

**`make_handler()`/`serve.run()` accept parameters beyond their frozen
signatures.** `service.contracts.MakeHandler` is `(store, options) ->
type[BaseHTTPRequestHandler]`; `make_handler()` additionally accepts
keyword-only parameters with defaults — `watcher_stats` (a
zero-argument callable returning the current `WatcherStats`, used by
`/api/health`), `watcher_state` (likewise for the current
`WatcherState`, from which `/api/health` works out its `status`),
`service_registered` (a zero-argument probe for
`/api/health`'s field of that name; omitted, it reports `null`),
`code_watch` (the `codewatch.CodeWatch` behind `/api/health`'s `code`
and the `503 restart_needed` answer; omitted, `code` is `null`),
`restarts_itself` (a zero-argument callable: whether this process will
exit and be started again on changed code; omitted,
`options.exit_on_code_change` answers) and
`static_dir` (above) — which is still a valid
`MakeHandler` implementation (a Protocol callable is satisfied by
something that accepts extra optional parameters). Similarly,
`service.serve.run(options, *, once=False)` gains `allow_remote:
bool = False`: `ServeOptions` itself carries no such flag, but the
plan's "port bound to localhost only" default posture needs an
explicit opt-in for anything else, so `run()` refuses to bind a
non-loopback `options.bind` unless `allow_remote=True`. `service/serve.py`
also picks `<config_dir>/service.db` as the SQLite store's filename —
`ServeOptions` has no field for it, only `config_dir`.

**Watching a background-thread watcher's stats.** `service.contracts.Watcher`
documents `last_stats: WatcherStats | None` (S1-integration fix 1.e) as
a Protocol attribute every concrete `Watcher` keeps current — `FileWatcher`
sets it at the end of every `run_once()`, including the ones its own
background poll thread runs after `start()`. `/api/health` always has a
real answer: `serve.run()` passes `make_handler` a `watcher_stats`
callable that simply reads `watcher.last_stats`. `serve` binds its port
before the first scan, which runs on the watcher's thread, so until
that scan finishes `last_stats` is `None`, `watcher` reports zeros and
`status` is `"starting"`. After each tick of that thread, `serve.run()`
also checks the package's code on disk (`FileWatcher`'s `after_tick`
hook; see `code` under `GET /api/health`).

## Store rebuild

`GET /api/report.*` above is built from the store instead of a fresh
parse, via `service/rebuild.py`'s `corpus_from_store(store, *, days=None,
since=None, until=None, window_by="last-reply") -> Corpus`. This is what lets
a report be served for a session whose transcript file has already been
removed by Claude Code's own `cleanupPeriodDays` retention: the watcher
(`service/watcher.py`) folds every parsed transcript's full
`TranscriptResult` into `transcripts.digest_blob` (the same lossless
JSON encoding `cache.py`'s on-disk digest cache uses, zlib-compressed
before storage — S1-perf), and `corpus_from_store` decodes those
digests straight back into a `Corpus` shaped exactly as
`corpus.load_corpus` would have produced from the live files, so
`report.build_report(corpus, ...)` runs unmodified against either one.
`days`/`since`/`until`/`window_by` mirror `discovery.find_sessions`'s own
parameters and windowing semantics.

**A file Claude Code removed is marked, not deleted, in the store**
(review finding 3). `Store.remove_missing` notices its transcript is no
longer on disk and sets `transcripts.missing_since`; the row and its
`digest_blob` are left alone, so `corpus_from_store` keeps including it
exactly like a transcript that is still there, and clears the mark again
if a file at the same path reappears. Only `--retention-days`/`serve
--purge` (see [docs/deploy.md](deploy.md)) actually delete a row — the
service store is designed to outlive Claude Code's own retention window,
not mirror it.

**Workflow runs round-trip (S1-integration fix 1.d).** The watcher
persists each `<session>/workflows/wf_*.json` run to a `workflow_runs`
table (`run_id`, `agent_count`, `phases` — phase *titles* only, never
`detail` — `started`, `finished`, `cost`, `status`), and
`corpus_from_store` reads it back into `SessionBundle.workflows`, so a
rebuilt report's `"workflows"`/`"phases"` sections and
`overview.workflow_runs` match a fresh parse. One approximation:
`WorkflowRun.phases` (an int count) is reconstructed as
`len(phase_titles)`, which can differ from a fresh parse's raw phase
count if some phase entries in the source JSON lack a `title` — an
accepted, documented trade-off (`service/schema.py`'s
`CREATE_WORKFLOW_RUNS` comment), not a privacy or correctness concern.

One field still does not survive the round trip, a store-schema gap
rather than a bug in `corpus_from_store` itself: **`SessionBundle.project_dir`**
is always the empty string once rebuilt from the store — nothing in
`report.build_report`'s own code path reads it, so this has no effect
on any route's output.
