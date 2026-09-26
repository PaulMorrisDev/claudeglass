"""Frozen data-model and table contract for claudeglass.

This module is the frozen contract referenced throughout the project plan
(Appendix A1). Every dataclass and enum here is what every later work
package (parsing, pricing, RE-CACHE, TTL, classification, rendering, the
service store, profiles) builds on top of. To keep that possible:

- Later work packages MAY add new fields, as long as they carry a default
  so existing call sites keep constructing valid instances.
- Later work packages MUST NOT rename or remove an existing field, or
  change its meaning, without a coordinated ``SCHEMA_VERSION`` bump in
  ``__init__.py`` (a bump invalidates the on-disk digest cache).
- This module imports only the standard library. It must never import
  from any other ``claudeglass`` submodule, so that every later
  package can depend on it without a cycle.
- No field here may ever hold message text, tool result content, a full
  file path, or a full shell command. See SECURITY.md; ``cmd_prefix`` and
  ``preceding_cmd_prefix`` are capped at 40 characters for this reason.

Deviations from the plan's Appendix A1 that were necessary to make it a
concrete, constructible contract (proposed here, not silently changed):

- ``ReportModel.meta`` is specified in the plan as a nested structure
  ``(tool_version, generated_at, window, projects, pricing {path,
  version, sha8, currency, coverage_pct}, thresholds, billing_mode)``
  without naming the nested dataclasses. Two small dataclasses,
  ``PricingMeta`` and ``ReportMeta``, are introduced to hold that
  structure so ``ReportModel`` can be constructed with defaults like
  every other contract type.

WP1 (parse.py, events.py, jsonl.py, discovery.py) additions, all with
defaults so every existing call site keeps working:

- ``Event.size_chars: int | None`` — length of the text an attachment
  line puts in front of the model: its ``rendered`` blocks, else its own
  content fields (``events._rendered_size_chars``). Requested by the WP1
  brief for CONTEXT_INJECT/REMINDER/CACHE_SIGNAL/HOOK_OUTPUT/ATTACHMENT
  sizing. ``None`` for ``prompt_snapshot``, whose sizes are in ``detail``.
- ``Event.detail: dict`` — small numeric/short-string detail a kind needs
  beyond kind/subkind (API_ERROR's ``status``/``retryAttempt``,
  MODEL_FALLBACK's ``originalModel``/``fallbackModel``, and the three
  delta attachment types' ``added``/``removed`` counts). Never message
  text, a full path, or a command.
- ``Turn.inference_geo: str | None`` — from ``usage.inference_geo``,
  needed for the documented 1.1x geo multiplier (plan Pricing section).
- ``Diagnostics.oversized_lines: int`` — lines ``jsonl.iter_lines`` skips
  for exceeding ``MAX_LINE_BYTES`` (8 MB), counted without being parsed.
  Not in Appendix A1's ``Diagnostics`` list; added because the WP1 brief
  requires counting them somewhere and no existing field fits.

- ``ReportMeta.assumptions`` (``list[str]``, default empty) is added by
  WP9 (renderers). The plan's Markdown/HTML layout requires an
  ``## Assumptions`` block (e.g. the TTL simulation's stated
  assumptions), and analytics packages (TTL, RE-CACHE, ...) need a place
  to push that text onto the report without the renderers inventing it.
  Defaulting to ``[]`` keeps every existing call site valid.

Independent-review follow-up fixes (post-WP1/WP2/WP7), all with defaults:

- ``Diagnostics.trailing_events: int = 0`` — events observed after the
  last finalised turn in a transcript (nothing left to attach them to).
  See ``parse.py``'s two-buffer rewrite.
- ``Diagnostics.replayed_lines: int = 0`` — non-blank lines skipped
  because their ``uuid`` was already seen earlier in the same file
  (transcripts replay whole blocks on rewind/resume).
- ``Diagnostics.copied_lines: int = 0`` (``PARSER_VERSION`` 26) — lines
  in a top-level transcript skipped because their ``sessionId`` names
  another session whose own file sits beside it (``/clear`` in a web or
  mobile session copies the new session into the earlier one's file).
- ``Diagnostics.timestamp_parse_failures: int = 0`` — a priced turn's
  ``timestamp`` field was present but could not be parsed.
- ``TranscriptMeta.tool_use_id: str | None = None`` — a subagent's
  ``.meta.json`` ``toolUseId``, linking the subagent back to the parent
  turn that spawned it (topology/workstyle, plan Appendix A1).
- ``Diagnostics.agent_settings: dict``, ``Diagnostics.modes: dict`` —
  ``agent-setting``/``mode`` lines stay ignored as ``Event``s (too
  harness-plumbing to attach to a turn) but their *value* is carried as a
  counter instead of being dropped outright, since it's evidence for
  archetype detection (which persona/mode a session ran under).
- ``Diagnostics.attachment_catch_all: dict`` — attachment types that
  fell into the generic ``ATTACHMENT`` kind, counted by type, so a new
  attachment type shows up in Diagnostics the moment it's seen rather
  than only via a manual scan of ``TranscriptResult.events``.

Batch C additive fields (all with defaults, per this module's own rule):

- ``Turn.tool_use_ids: tuple[str, ...] = ()`` — the ``id`` of every
  ``tool_use`` content block in this turn, in encounter order. Lets
  ``topology.py``'s skill roll-up join a subagent back to its parent turn
  from already-parsed ``Turn``s instead of re-scanning the raw JSONL (see
  ``topology.index_tool_use_ids``, now a fallback for transcripts parsed
  before this field existed).
- ``WorkflowRun.status: str | None = None`` — the run file's own
  ``status`` (``"completed"``/``"killed"`` observed), filled by
  ``workflows.parse_workflow_file``.
- ``WorkflowRun.phase_titles: tuple[str, ...] = ()`` — each phase entry's
  ``title`` only, never its ``detail`` (workflow source/prompt text) —
  filled by ``workflows.parse_workflow_file``.
- ``SessionRecord.entrypoint: str | None = None`` — carried through from
  ``top.meta.entrypoint`` by ``classify.build_session_record``; previously
  a caller-supplied ``extract_features`` keyword with no real data source
  (see ``classify.py``'s module docstring).
- ``TranscriptMeta.provider: str | None = None`` — the API surface a
  transcript's turns were billed through (``"anthropic"`` | ``"bedrock"``
  | ``"vertex"`` | ``"foundry"``), derived from model-id form.
- ``TranscriptMeta.entrypoint: str | None = None`` — the first non-empty
  ``entrypoint`` field seen anywhere in the transcript's raw lines
  (``cli``/``sdk-python``/... observed), set by ``parse.parse_transcript``
  alongside the existing ``claude_version`` field (also first-seen, from
  each line's own ``version`` field — previously declared but never
  populated by any module).

Discovery fix (workflow-nested subagents), also additive:

- ``TranscriptMeta.workflow_run_id: str | None = None`` — the run id of
  the ``<session_id>/subagents/workflows/<run_id>/agent-*.jsonl``
  directory a workflow-nested subagent lives under (see
  ``workflows.py``'s module docstring on this layout), set by
  ``discovery.load_meta`` along with ``kind="workflow-agent"`` (from
  ``PARSER_VERSION`` 24). ``None`` for every other transcript kind.

Capture-improvements batch (all additive, all defaulted -- see
``parse.py``'s module docstring for how each is computed):

- ``Turn.tool_wait_s: float | None = None`` / ``Turn.model_latency_s:
  float | None = None`` -- timing either side of this turn's own tool
  calls: ``tool_wait_s`` is how long the harness/tool took to answer
  (last tool_result timestamp minus this turn's own), ``model_latency_s``
  is how long the model then took to respond (the next turn's timestamp
  minus that same last tool_result timestamp). Both ``None`` when this
  turn made no tool calls, or the relevant timestamp is missing/
  unparsable, or (for ``model_latency_s``) there is no next turn.
- ``Turn.tool_result_chars_by_tool: dict = {}`` -- tool name -> total
  chars of tool_result content answering *this* turn's own tool_use_ids
  (a per-turn breakdown of the same lengths ``TranscriptResult.
  tool_result_chars`` totals for the whole transcript), so per-turn
  context composition is computable.
- ``Turn.agent_brief_chars: int | None = None`` -- total length of the
  ``prompt`` input string(s) of every ``Agent``/``Task`` tool_use block
  in this turn (the brief handed to a spawned agent) -- never the prompt
  text itself. ``None`` when this turn spawned no agent.
- ``Turn.tool_input_chars_by_tool: dict = {}`` -- tool name -> total
  chars of every tool_use block's JSON-encoded ``input`` in this turn
  (size only, via ``json.dumps`` on the already-parsed input -- the
  input itself is never retained).
- ``Turn.read_target_hashes: tuple[str, ...] = ()`` -- one salted HMAC-
  SHA256 hash (16 hex chars) per ``Read`` tool_use's own target path in
  this turn (``Edit``/``Write``/``NotebookEdit`` too before
  ``PARSER_VERSION`` 15), via
  ``parse.set_salt``/``parse.load_or_create_salt`` -- never the path
  itself, and empty for every turn until a salt has been set in this
  process (see ``parse.py``'s docstring on why the salt is threaded
  through a module-level setter rather than a ``parse_transcript``
  parameter).
- ``Turn.human_prompt_chars: int | None = None`` / ``Turn.
  human_prompt_has_paste: bool = False`` -- on the turn that follows a
  HUMAN_TEXT event: the summed length of that event's (or events') own
  text content, and whether any of it looks pasted (>2,000 chars, or
  contains a ``[Pasted text`` marker). ``human_prompt_chars`` is
  ``None`` when no HUMAN_TEXT event precedes this turn.

Usage-limits batch (v3-limits, all additive, all defaulted -- see
``events.py``/``parse.py``/``limits.py``'s module docstrings for how each
is detected and used):

- ``EventKind.LIMIT_HIT`` -- a synthetic assistant line (``model:
  "<synthetic>"``, ``isApiErrorMessage: true``) reporting "You've hit
  your session limit" or "...weekly limit". ``subkind`` is
  ``"session_limit"`` or ``"weekly_limit"``. Ranked above ``INTERRUPT``
  in ``events.PRECEDENCE`` (a usage-cap pause is a stronger explanation
  for a gap than a plain interrupt).
- ``EventKind.LIMIT_RESUME`` -- the desktop app's automatic resume
  prompt after a pause ("I hit my usage limit while you were working,
  but it has reset now"): a ``type=user`` line with ``promptSource:
  "sdk"`` and ``origin.kind == "human"``. Also ranked above
  ``INTERRUPT``. Marks the end of a limit-induced pause.
- ``EventKind.AGENT_TERMINATED`` -- a ``type=user`` task-notification
  line reporting a subagent killed mid-task ("Agent terminated early due
  to an API error: ..."). ``subkind`` is ``"rate_limit"`` when the
  reported reason is a usage-limit 429, else ``"other"``.
- ``Turn.synthetic_kind: str | None = None`` -- for a synthetic assistant
  turn (``is_synthetic=True``), which of the six known synthetic texts it
  is: ``"session_limit"``, ``"weekly_limit"``, ``"overloaded"``,
  ``"unsupported_model"``, ``"autocompact_thrash"``, or
  ``"other_api_error"``. ``None`` for a non-synthetic turn, or a
  synthetic turn whose text didn't match any of the six.
- ``Turn.gap_cause: str | None = None`` -- ``"limit"`` when the gap to
  the previous turn spans a ``LIMIT_HIT``/``LIMIT_RESUME`` pair (the
  harness was paused by a usage cap, not idle), else ``None``. Read by
  ``recache.py``/``ttl.py``/``classify.py`` to keep a limit pause from
  being counted as behavioural idle time.
- ``Event.detail`` additions for ``LIMIT_HIT``: ``reset_minutes_of_day``
  (``int | None``, minutes since local midnight the window resets),
  ``reset_tz`` (``str | None``, an IANA zone name only when it matches
  the single-slash form ``^[A-Za-z_]+/[A-Za-z_]+$`` -- a multi-part name
  like ``America/Argentina/Buenos_Aires`` is deliberately left as
  ``None`` rather than guessed at), and ``reset_ts`` (``str | None``, UTC
  ISO -- from the line's own ``quotaLimits.resetsAt`` epoch when present,
  else reconstructed from the parsed local time + zone via
  ``zoneinfo.ZoneInfo`` when that resolves, else ``None`` when neither
  source is usable, e.g. missing tzdata on a bare Windows install).
- ``Event.detail`` additions for ``API_ERROR``: ``source`` (``str |
  None``, the ``system.subtype=api_error`` line's own ``request_retry``/
  ``connection_retry`` value) alongside the existing ``status``/
  ``retryAttempt``, plus ``retryInMs`` (``int | None``).
- ``Diagnostics.limit_hits: int = 0`` / ``Diagnostics.limit_resumes: int
  = 0`` / ``Diagnostics.agents_terminated: int = 0`` -- corpus-wide
  counts of the three new event kinds, for the Diagnostics section.

Wasted-turns batch (v4-wasted-turns, additive, see ``parse.py``'s module
docstring for how each is computed and ``waste.py`` for how they are
used):

- ``Turn.tool_error_count: int = 0`` -- how many of this turn's own
  ``tool_use_ids`` came back with a ``tool_result`` block carrying
  ``is_error: true``. Counted only for tool_use ids belonging to this
  turn, mirroring ``tool_result_chars_by_tool``'s own attribution.
- ``Turn.tool_error_chars: int = 0`` -- the summed length of those
  erroring tool_result blocks' content (via the existing
  ``_tool_result_length`` helper). The LENGTH only -- never the error
  text itself, per this module's own rule against storing tool result
  content.

Context-files addition:

- ``Turn.skills_invoked: tuple[str, ...] = ()`` -- the ``skill`` input of
  each ``Skill`` tool_use in this turn: the skill's name, a label like
  ``attribution_skill``, never its arguments.

Quality-signals addition (see ``quality.py`` for how these are used):

- ``Turn.stop_reason: str | None = None`` -- the API's ``stop_reason``
  for this reply (``end_turn``, ``tool_use``, ``max_tokens``,
  ``stop_sequence``, ...): the last non-null value across the message's
  lines. A transcript whose last reply stopped on ``tool_use`` was cut
  off before it could answer.
- ``Turn.tool_calls_by_tool: dict = {}`` -- tool name -> how many
  ``tool_use`` blocks this turn made with it (``tool_names`` lists each
  name once).
- ``Turn.tool_errors_by_tool: dict = {}`` -- tool name -> how many of
  ``tool_error_count`` came from that tool.
- ``Turn.tool_errors_by_kind: dict = {}`` -- why each of
  ``tool_error_count`` failed, from the start of its error text (only
  the kind is kept): ``blocked`` (a hook or a Claude Code guard stopped
  it), ``denied`` (you or the permission classifier said no),
  ``failed`` (a command ran and reported failure: a failing test or
  build, a timeout) or ``misfire`` (it couldn't run as written: a wrong
  path, a malformed command, an edit whose text wasn't found). Empty on
  a digest from before this field existed.
- ``Turn.edit_target_hashes: tuple[str, ...] = ()`` -- the salted hashes
  of the files this turn edited: Edit/Write/MultiEdit/NotebookEdit
  targets and the files its Bash/PowerShell commands wrote with content
  they authored (``shell_writes.py``: ``sed -i``, ``Set-Content``, a
  heredoc redirected to a file; not a program's output captured to a
  log), so a file edited again later can be counted as a re-edit without
  keeping any path. An edit whose tool call failed, or a shell command
  that was blocked or denied, is left out (from parser version 14; before
  that, Edit/Write/NotebookEdit targets only, failed or not).
- ``Turn.human_correction: bool = False`` -- on the turn that follows a
  human message: whether that message looks like it corrects Claude
  ("that's wrong", "still broken", "why did you ..."). A yes/no from a
  fixed phrase list; the text is never kept.
- ``Event.detail`` on ``TASK_NOTIFICATION``/``AGENT_TERMINATED`` (and on
  a ``QUEUE_OPERATION`` that queues a task notification) gains
  ``task_id`` and ``status`` (``completed``/``failed``/``stopped``) from
  the notification's own tags, and on ``TOOL_RESULT`` gains ``agents``
  (``[[agent_id, status]]``) for a synchronous agent's result. A
  background agent's transcript is ``agent-<task_id>.jsonl``, so its
  outcome joins to ``TranscriptMeta.agent_id``.
- ``TranscriptMeta.workflow_agent_state: str | None = None`` -- for a
  workflow agent, its own end state from the run file's
  ``workflowProgress`` (``done``, ``error``, or ``progress`` when the
  workflow was killed while it ran), read by ``discovery.load_meta``
  only once the run has finished; ``None`` otherwise. Workflow agents
  get no task notification, so this is their only recorded outcome.

Fast-mode addition (three-pricing-fixes batch, ``PARSER_VERSION`` bump --
see ``parse.py``/``pricing.py``'s own module docstrings):

- ``Turn.speed: str | None = None`` -- the turn's own
  ``usage.speed`` (``"standard"`` or ``"fast"``, observed today; any
  other/missing value is kept verbatim or left ``None``). Drives
  ``pricing.price_turn``'s fast-mode rate multiplier for the handful of
  models that document one.
- ``CostBreakdown.fast_applied: bool = False`` -- whether ``price_turn``
  actually applied a fast-mode multiplier (the turn's ``speed`` was
  ``"fast"`` *and* the resolved model carries a ``[.fast]`` table). A
  turn with ``speed == "fast"`` and ``fast_applied is False`` was priced
  at standard rates because its model has no fast-mode rates on file --
  see ``PricingCoverage.fast_priced_as_standard``.
- ``Diagnostics.pricing_closest_match_turns`` / ``pricing_fast_priced_as_standard_turns``
  -- corpus-wide totals mirroring ``PricingCoverage.closest_matches``/
  ``fast_priced_as_standard`` (see ``pricing.py``), set once by
  ``report.build_report`` from the finished ``PricingCoverage``
  accumulator rather than merged per-transcript like the other
  ``Diagnostics`` fields (this data only exists once every turn has
  been priced against the rate card, not at parse time). Surfaced on
  the Data quality tab/``report --explain`` via the same generic
  ``dataclasses.fields(Diagnostics)`` walk every other counter here
  already gets.

Quality-markers addition (``PARSER_VERSION`` 14 -- see
``quality.MARKER_LINES`` for the CLAUDE.md lines that ask Claude for
them). Only the word is kept, never the text around it:

- ``Turn.retry_marker: str | None = None`` -- on the turn that follows
  a human message (for a subagent, its brief) starting
  ``[retry: model|brief|tools|other]``: the agent was started again
  because its last run's work wasn't good enough, and why. Also
  ``Event.detail["retry"]`` on that ``HUMAN_TEXT`` event.
- ``Turn.result_marker: str | None = None`` -- ``done``, ``partial`` or
  ``blocked`` when the turn's last text block ends ``[result: ...]``: a
  subagent's own account of whether it finished.

Metrics-capture addition (``PARSER_VERSION`` 15 -- see
``capture_catalogue.py`` for the vocabularies and ``capture_tags.py`` for
how tags are read). Every value is a word from a closed list, a count or
a flag, never text:

- ``CaptureTag`` / ``Turn.cap: CaptureTag | None = None`` -- the
  ``[tl: ...]`` tag (and the ``[result: ...]`` extras ``fit``, ``rules``,
  ``brief``, ``missing``) ending this turn's last text block. Unknown keys
  and words are dropped; ``skill_name`` survives only when it names a
  skill the transcript listed or used. ``chars`` is the tag's own length,
  for pricing the output it cost.
- ``Turn.cap_note_chars: int = 0`` -- characters of capture notes (a
  ``hook_additional_context`` attachment carrying ``tl-cap v``) put in
  front of the model just before this turn, measured from ``rendered``.
  From ``PARSER_VERSION`` 25 it counts coaching notes (``tl-coach v``)
  too: an ``Event`` of subkind ``coaching_note`` whose ``detail`` holds
  ``v``, ``kind`` (a ``capture_catalogue.COACHING_HINTS`` word or
  "other") and ``hook``; a capture note sharing an attachment with one
  carries ``detail["coach"]``/``["coach_chars"]`` and sizes only its own
  part.
- ``Turn.spawn_marker: str | None = None`` -- on the turn that follows a
  brief starting ``[spawn: parallel|isolate|cheaper|specialist|review]``:
  why the work was handed to an agent. ``retry_marker`` also takes
  ``scope`` now.
- ``Turn.agent_result_chars: dict = {}`` -- Agent/Task tool_use id ->
  characters of the report that agent handed back to this turn.
- ``Turn.prompt_flags: tuple[str, ...] = ()`` -- what the preceding human
  message (for a subagent, its brief) contained, as words from
  ``PROMPT_FLAGS``: a file path, a code block, an error or stack trace, a
  URL, done criteria, numbered steps, a cap on the report's length.
- ``PlanStats`` / ``Turn.plan_stats`` -- an ``ExitPlanMode`` call's plan:
  step and file counts, length, and whether it was approved or rejected.
- ``Turn.commands_run: tuple[str, ...] = ()`` -- the names of the slash
  commands you ran just before this turn (``compact``, ``grill-me``), so
  a skill you invoked can be told from one Claude invoked
  (``skills_invoked``). Also ``Event.detail["command"]`` on the
  ``SLASH_COMMAND`` event, and on the ``HUMAN_TEXT`` event of a skill run
  with a slash (written ``<command-message>`` first, since
  ``PARSER_VERSION`` 16). Names only, never arguments.
- ``Turn.read_target_hashes`` now covers ``Read`` only: an edit is not a
  read, and counting edits made every edited file look re-read. Edits
  stay in ``edit_target_hashes``.
- ``TASK_NOTIFICATION`` events are sized (``size_chars``): a background
  agent's notification carries the report it hands back.

Feedback addition (``PARSER_VERSION`` 16):

- ``Feedback`` / ``Turn.feedback: Feedback | None = None`` -- your
  /tl-feedback answers: outcome, what slowed the work, whether it was
  worth the tokens and what would have helped, as words from
  ``capture_catalogue.FEEDBACK_VOCAB``. Read from the skill's
  ``[tl-fb: ...]`` line ending this turn's reply (``source`` "tag"), or
  from the answers to its AskUserQuestion call made in this turn
  ("answers"); "skipped" when you declined the questions. Labels
  are matched to words, so free-text "Other" answers are never kept.
- ``TranscriptMeta.cap_version`` / ``cap_metrics`` / ``cap_injections``
  -- the capture note format version seen, the metric codes the notes
  asked for, and how many notes were injected.

Your-hooks addition (``PARSER_VERSION`` 22). Hook labels are a
script's file name (``quarantine-guard.ps1``), or a hook command's first
40 characters with paths redacted (the same rule as ``cmd_prefix``), or
``built-in`` for context Claude Code itself adds -- never a path or a
whole command:

- ``Event.detail`` on a hook ``HOOK_OUTPUT`` event gains ``script`` (the
  label), ``cause`` on an error (``not-found``, ``timeout`` or
  ``failed``), ``relative`` (only when ``True``: the command names its
  script by a relative path, which works only from the project root) and,
  from ``PARSER_VERSION`` 23, ``unexpanded`` (only when ``True``: the
  command uses a Windows ``%VAR%`` variable, which the shell a hook runs
  in leaves as it is).
- ``Turn.hook_context_chars: dict = {}`` -- label -> characters of
  context your hooks added just before this turn (``hook_additional_
  context``). ClaudeGlass's own capture note stays in ``cap_note_chars``.
- ``Turn.hook_blocks: dict = {}`` -- label -> this turn's tool calls a
  hook blocked (the ``<Event>:<Tool> hook error: [...]`` result).
- ``Turn.hook_resends: dict = {}`` -- label -> this turn's tool calls
  that repeat, with the same input, a call that hook blocked earlier in
  the transcript. Only a hash of the input is kept, in memory, while
  parsing.

Tool-search addition (``PARSER_VERSION`` 26). Claude Code lists deferred
tools by name (``deferred_tools_delta``) and records each definition it
loads (``deferred_tools_record``). Tool names and sizes only, never a
description or schema:

- ``Turn.deferred_tools_by_server: dict = {}`` -- MCP server, or
  ``built-in`` for Claude Code's own tools, -> tools listed by name only
  (definition not loaded) when this reply was requested.
- ``Turn.deferred_list_chars: int = 0`` -- characters of the name list
  sent in their place.
- ``TranscriptResult.tool_definition_chars: dict = {}`` -- tool name ->
  characters of the full definition loaded for it.

Compaction-call addition (``PARSER_VERSION`` 27). Claude Code bills the
request that writes a compaction's summary but never logs it as a reply,
so ``parse.py`` adds one estimated turn per ``compact_boundary``, right
after the reply before it (see that module's docstring for the sizing):

- ``Turn.estimated: str | None = None`` -- ``"compaction"`` on such a
  turn, else ``None``. The turn is priced (``turn_index > 0``) and also
  ``is_synthetic``, so every spend total counts it and every analysis of
  replies leaves it out, the same way it leaves out a synthetic reply.
- ``Diagnostics.compaction_calls: int = 0`` -- compactions priced this
  way; ``Diagnostics.compaction_calls_unsized: int = 0`` -- compactions
  left out because no earlier reply or summary size was recorded.

Cost-record addition (``PARSER_VERSION`` 27). A ``cost-state`` line is a
running total written now and then, not at the end, so a comparison has
to stop where it stops (``reconcile.claude_code_reported_costs``):

- ``TranscriptMeta.cc_cost_as_of: str | None = None`` -- the latest
  timestamp of any line before the last ``cost-state`` line.
- ``TranscriptMeta.cc_cost_by_model: dict = {}`` -- model id -> that
  line's ``modelUsage[model].costUSD``. Model ids and numbers only; an id
  outside the model-id alphabet is dropped.

Parser-signals addition (``PARSER_VERSION`` 19 -- plan SURV-4/5/6/7, see
``events.py``/``parse.py``'s own module docstrings). Every new value is a
count, a closed word (with an "other" fallback) or a raw number off a
``cost-state`` line -- never message text, a path or a command:

- ``EventKind.TASK_STATUS`` / ``EventKind.STRUCTURED_OUTPUT`` -- a
  ``task_status``/``structured_output`` attachment gets its own kind
  instead of falling into the generic ``ATTACHMENT`` catch-all.
  ``Event.detail`` carries ``status``/``task_type`` (closed words, never
  ``description``/``deltaSummary``/``outputFilePath``/``shell``) for the
  former, and ``size_chars`` only (the JSON-encoded length of ``data``,
  never ``data`` itself) for the latter.
- ``thinking_drop`` joins ``events._CACHE_SIGNAL_TYPES`` (a likely
  cache-bust): a model dropped its own prior extended-thinking blocks.
  ``Event.detail`` carries ``reason`` (closed word, else "other"),
  ``blockCount`` and ``turnCount`` -- never the dropped blocks'
  ``first``/``last`` text, ``blockHashes``, ``requestId``,
  ``querySource`` or ``model``.
- ``TranscriptResult.parser_notes: dict[str, dict[str, int]] = {}`` /
  ``ReportModel.parser_notes: dict = {}`` -- a side channel for counters
  that don't fit ``Diagnostics`` (whose field list is pinned 1:1 to
  ``helptext.DIAGNOSTIC_LABELS`` by ``tests/test_help_coverage.py``, and
  which this phase was told not to touch). Two keys so far:
  ``unknown_line_types`` -- top-level line types ``events.classify_line``
  had no rule for at all (kept apart from the long-standing
  ``Diagnostics.ignored_line_types``, which also holds types the parser
  recognises and deliberately drops), by sanitised type name (see
  ``events.sanitize_line_type``: a closed token pattern, capped length,
  else "other" -- a raw ``type`` field is attacker-controlled input, not
  a trusted enum). ``unsized_blocks`` -- ``image``/``document`` content
  blocks (in a tool_result or a human prompt) whose token count this
  parser did not attempt to estimate, by block type, because no
  documented deterministic rule covers them (a PDF page's cost is only
  documented as an approximate per-page range) or the image needs the
  high-resolution tier's downscaling this parser doesn't implement.
  Merged manually alongside (not inside) ``report._merge_diagnostics``,
  and rendered by ``helptext.diagnostics_table``'s own
  ``_PARSER_NOTE_LABELS`` (not ``DIAGNOSTIC_LABELS``).
- ``Event.size_chars`` on an ``image``/``document`` content block this
  parser CAN size (standard-tier image: ``events.image_token_estimate``)
  is the block's token estimate converted back to chars at the project's
  usual ``_CHARS_PER_TOKEN_APPROX`` (4), so it composes with the existing
  char-based totals (``tool_result_chars``, ``human_prompt_chars``)
  without a second unit system. ``parse._tool_result_length`` and
  ``events._human_text_metrics`` both now add this for ``image``/
  ``document`` blocks instead of silently treating them as 0 chars.
- ``TranscriptMeta.cc_cost_usd: float | None = None`` /
  ``cc_cost_has_unknown_model: bool = False`` -- a ``cost-state`` line's
  own ``totalCostUSD`` (numbers only; the last such line seen in the
  transcript, since the field is a running total) and its
  ``hasUnknownModelCost`` flag, so ``reconcile.claude_code_reported_costs``
  can hand P9's later Q1 gap metric "what Claude Code itself thinks this
  session cost" next to this tool's own per-turn pricing for the same
  session. ``cost-state`` joins ``events._IGNORABLE_TYPES`` (it is a
  known, deliberately-ignored-as-an-event type, like ``mode``/
  ``agent-setting`` before it) since its value is read directly in
  ``parse.parse_transcript`` rather than carried as an ``Event``. No
  OTel, no per-model breakdown -- just the one total and its flag.
  (``PARSER_VERSION`` 27 adds the breakdown and the cut-off below.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EventKind(StrEnum):
    """Every non-assistant JSONL line becomes one Event of one of these
    kinds. Ordered as in plan Appendix A2's detection table (roughly
    highest to lowest ``preceding_primary`` precedence, with the harness
    plumbing kinds — QUEUE_OPERATION through TOOL_RESULT — in the middle
    and the catch-alls last).
    """

    COMPACT_BOUNDARY = "compact_boundary"
    COMPACT_SUMMARY = "compact_summary"
    API_ERROR = "api_error"
    MODEL_FALLBACK = "model_fallback"
    LOCAL_COMMAND = "local_command"
    HOOK_OUTPUT = "hook_output"
    CACHE_SIGNAL = "cache_signal"
    REMINDER = "reminder"
    CONTEXT_INJECT = "context_inject"
    QUEUE_OPERATION = "queue_operation"
    #: Parser-signals addition (see module docstring): a ``task_status`` or
    #: ``structured_output`` attachment gets its own kind instead of the
    #: generic ATTACHMENT catch-all below.
    TASK_STATUS = "task_status"
    STRUCTURED_OUTPUT = "structured_output"
    ATTACHMENT = "attachment"
    META = "meta"
    TOOL_DENIAL = "tool_denial"
    TOOL_RESULT = "tool_result"
    TASK_NOTIFICATION = "task_notification"
    PEER_MESSAGE = "peer_message"
    SLASH_COMMAND = "slash_command"
    SCHEDULED_TASK = "scheduled_task"
    INTERRUPT = "interrupt"
    HUMAN_TEXT = "human_text"
    UNKNOWN = "unknown"
    #: Usage-limits batch (v3-limits, see module docstring): ranked above
    #: INTERRUPT in events.PRECEDENCE.
    LIMIT_HIT = "limit_hit"
    LIMIT_RESUME = "limit_resume"
    AGENT_TERMINATED = "agent_terminated"


@dataclass(slots=True)
class Event:
    """One non-assistant JSONL line, attached to the next finalised turn."""

    kind: EventKind = EventKind.UNKNOWN
    #: attachment.type | system.subtype | operation | toolDenialKind | None
    subkind: str | None = None
    ts: str | None = None
    # Compaction-only fields (COMPACT_BOUNDARY); None for every other kind.
    pre_tokens: int | None = None
    post_tokens: int | None = None
    dropped_tokens: int | None = None
    duration_ms: int | None = None
    trigger: str | None = None
    #: WP1 addition (plan deviation, see module docstring): length of the
    #: attachment's ``rendered`` field, when present. Never the rendered
    #: text itself.
    size_chars: int | None = None
    #: WP1 addition (plan deviation, see module docstring): small numeric
    #: detail for kinds whose meaning needs more than kind/subkind, e.g.
    #: API_ERROR's ``status``/``retryAttempt``, MODEL_FALLBACK's
    #: ``originalModel``/``fallbackModel``, or the delta attachment types'
    #: ``added``/``removed`` counts. Never message text or full paths.
    detail: dict = field(default_factory=dict)


#: Words ``Turn.prompt_flags`` may hold (see the module docstring).
PROMPT_FLAGS = ("path", "code", "error", "url", "done", "steps", "short")


@dataclass(slots=True)
class CaptureTag:
    """What a metrics-capture tag said (see the module docstring). Every
    field is a word from ``capture_catalogue.TAG_VOCAB`` or ``None``."""

    task: str | None = None
    brief: str | None = None
    level: str | None = None
    shift: str | None = None
    size: str | None = None
    missing: tuple[str, ...] = ()
    plan: str | None = None
    skill: str | None = None
    skill_name: str | None = None
    found: str | None = None
    fit: str | None = None
    rules: str | None = None
    prior: str | None = None
    detour: str | None = None
    check: str | None = None
    out: str | None = None
    useful: str | None = None
    #: A ``[tl: ...]`` tag was written, not only a ``[result: ...]``.
    has_tl: bool = False
    #: Length of the tag text, for pricing the output it cost.
    chars: int = 0


@dataclass(slots=True)
class Feedback:
    """Your /tl-feedback answers (see the module docstring). Every value is
    a word from ``capture_catalogue.FEEDBACK_VOCAB``."""

    outcome: str | None = None
    slow: tuple[str, ...] = ()
    worth: str | None = None
    helped: tuple[str, ...] = ()
    #: Whether the build could have started fresh from the plan: asked
    #: only after an approved plan.
    handoff: str | None = None
    #: "tag" | "answers" | "skipped".
    source: str = "tag"


@dataclass(slots=True)
class PlanStats:
    """An ``ExitPlanMode`` call's plan, as counts (see the module docstring)."""

    steps: int = 0
    files: int = 0
    chars: int = 0
    #: "approved" | "rejected" | None (no answer seen).
    outcome: str | None = None


@dataclass(slots=True)
class Turn:
    """One priced assistant turn: assistant lines grouped by ``message.id``
    (fallback ``request_id``, then ``uuid``), with the events that preceded
    it since the previous finalised turn.
    """

    message_id: str = ""
    request_id: str = ""
    #: 1-based index over priced turns in this transcript.
    turn_index: int = 0
    ts: str = ""
    gap_s: float | None = None
    model: str = ""
    service_tier: str | None = None
    is_synthetic: bool = False
    effort: str | None = None
    per_turn_effort: str | None = None

    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    #: cache_creation.ephemeral_5m_input_tokens / ephemeral_1h_input_tokens
    cc_5m: int = 0
    cc_1h: int = 0
    ctx: int = 0

    tool_names: tuple[str, ...] = ()
    cmd_prefix: str | None = None  # <= 40 chars
    edit_kind: str | None = None  # "real" | "scratch" | None

    attribution_mcp_server: str | None = None
    attribution_mcp_tool: str | None = None
    attribution_skill: str | None = None

    #: "Bash" | "PowerShell" | <tool name> | "none" | "n/a"
    preceding_tool: str = "none"
    preceding_cmd_prefix: str | None = None  # <= 40 chars
    preceding_event_kinds: tuple[EventKind, ...] = ()
    preceding_attachment_types: tuple[str, ...] = ()
    preceding_primary: EventKind = EventKind.UNKNOWN

    is_recache: bool = False
    #: "full-expiry" | "prefix-invalidated" | None
    recache_signature: str | None = None

    #: WP1 addition (plan deviation, see module docstring): from
    #: ``usage.inference_geo``. Drives the documented 1.1x geo multiplier.
    inference_geo: str | None = None

    #: Batch C addition (see module docstring): every ``tool_use`` block's
    #: ``id`` in this turn, in encounter order. Ids only — never the tool
    #: input or result content.
    tool_use_ids: tuple[str, ...] = ()

    #: Coordinator follow-up (WP12a diversity fixtures): set when this
    #: turn's ``usage`` had no nested ``cache_creation`` object at all
    #: (older, pre-5m/1h-split Claude Code JSONL) - ``cc_5m``/``cc_1h``
    #: are both 0 even though ``cache_creation_tokens`` may be nonzero,
    #: because the write's TTL split was never recorded, not because
    #: nothing was written. ``ttl.py``'s simulation functions normalize
    #: this by attributing the flat total to the transcript's own
    #: ``dominant_ttl`` (or "5m" when that's "mixed"/"none") before
    #: replaying any policy - see ``ttl.normalize_ttl_split``.
    ttl_split_unknown: bool = False

    #: Capture-improvements addition (see module docstring): timing either
    #: side of this turn's own tool calls.
    tool_wait_s: float | None = None
    model_latency_s: float | None = None
    #: Capture-improvements addition (see module docstring): tool name ->
    #: total chars of tool_result content answering this turn's own
    #: tool_use_ids.
    tool_result_chars_by_tool: dict = field(default_factory=dict)
    #: Capture-improvements addition (see module docstring): total length
    #: of this turn's Agent/Task tool_use ``prompt`` input string(s).
    agent_brief_chars: int | None = None
    #: Capture-improvements addition (see module docstring): tool name ->
    #: total chars of this turn's own tool_use ``input`` (JSON-encoded
    #: size only; the input itself is never retained).
    tool_input_chars_by_tool: dict = field(default_factory=dict)
    #: Capture-improvements addition (see module docstring): salted
    #: HMAC-SHA256 hashes (16 hex chars each) of this turn's own Read
    #: target paths - never the paths themselves. See
    #: ``parse.set_salt``/``parse.load_or_create_salt``.
    read_target_hashes: tuple[str, ...] = ()
    #: Capture-improvements addition (see module docstring): set on the
    #: turn that follows a HUMAN_TEXT event.
    human_prompt_chars: int | None = None
    human_prompt_has_paste: bool = False
    #: Usage-limits addition (see module docstring): which of the six
    #: known synthetic texts this turn is, when ``is_synthetic`` is True.
    synthetic_kind: str | None = None
    #: Usage-limits addition (see module docstring): "limit" when the gap
    #: to the previous turn spans a usage-cap pause, else None.
    gap_cause: str | None = None
    #: Wasted-turns addition (see module docstring): tool_result blocks
    #: with ``is_error: true`` answering this turn's own tool_use_ids.
    tool_error_count: int = 0
    #: Wasted-turns addition (see module docstring): summed length of
    #: those erroring tool_result blocks' content. Length only.
    tool_error_chars: int = 0
    #: Context-files addition (see module docstring): names of the skills
    #: this turn invoked with the ``Skill`` tool.
    skills_invoked: tuple[str, ...] = ()
    #: Quality-signals addition (see module docstring): why the API
    #: stopped this reply.
    stop_reason: str | None = None
    #: Quality-signals addition (see module docstring): tool name ->
    #: tool_use count.
    tool_calls_by_tool: dict = field(default_factory=dict)
    #: Quality-signals addition (see module docstring): tool name ->
    #: erroring tool_result count.
    tool_errors_by_tool: dict = field(default_factory=dict)
    #: Why each erroring tool_result failed, kind -> count (see module
    #: docstring). The kind only, never the error text.
    tool_errors_by_kind: dict = field(default_factory=dict)
    #: Quality-signals addition (see module docstring): salted hashes of
    #: the files this turn edited, by edit tool or shell command.
    edit_target_hashes: tuple[str, ...] = ()
    #: Quality-signals addition (see module docstring): the preceding
    #: human message looks like a correction. Flag only.
    human_correction: bool = False
    #: Fast-mode addition (see module docstring): from ``usage.speed``
    #: ("standard" | "fast", as observed today). Drives pricing.py's
    #: fast-mode rate multiplier for the models that document one.
    speed: str | None = None
    #: Quality-markers addition (see module docstring): why the preceding
    #: brief said this agent run is a retry. The word only.
    retry_marker: str | None = None
    #: Quality-markers addition (see module docstring): what this reply
    #: said at its end about finishing. The word only.
    result_marker: str | None = None
    #: Metrics-capture addition (see module docstring): the capture tag
    #: ending this reply.
    cap: CaptureTag | None = None
    #: Metrics-capture addition (see module docstring): characters of
    #: capture notes injected just before this turn.
    cap_note_chars: int = 0
    #: Metrics-capture addition (see module docstring): why the preceding
    #: brief said the work was handed to an agent. The word only.
    spawn_marker: str | None = None
    #: Metrics-capture addition (see module docstring): Agent/Task
    #: tool_use id -> characters of the report it handed back.
    agent_result_chars: dict = field(default_factory=dict)
    #: Metrics-capture addition (see module docstring): what the preceding
    #: human message or brief contained, as ``PROMPT_FLAGS`` words.
    prompt_flags: tuple[str, ...] = ()
    #: Metrics-capture addition (see module docstring): this turn's
    #: ``ExitPlanMode`` plan, as counts.
    plan_stats: PlanStats | None = None
    #: Metrics-capture addition (see module docstring): slash commands
    #: (and skills) you ran just before this turn, by name.
    commands_run: tuple[str, ...] = ()
    #: Feedback addition (see module docstring): your /tl-feedback answers.
    feedback: Feedback | None = None
    #: Your-hooks addition (see module docstring): hook label -> characters
    #: of context your hooks added just before this turn.
    hook_context_chars: dict = field(default_factory=dict)
    #: Your-hooks addition (see module docstring): hook label -> this
    #: turn's tool calls that hook blocked.
    hook_blocks: dict = field(default_factory=dict)
    #: Your-hooks addition (see module docstring): hook label -> this
    #: turn's tool calls that repeat, unchanged, a call that hook blocked.
    hook_resends: dict = field(default_factory=dict)
    #: Tool-search addition (see module docstring): MCP server (or
    #: ``"built-in"``) -> tools listed by name only, their definitions not
    #: loaded, when this reply was requested.
    deferred_tools_by_server: dict = field(default_factory=dict)
    #: Tool-search addition: characters of the name list sent in place of
    #: those definitions.
    deferred_list_chars: int = 0
    #: Compaction-call addition (see module docstring): ``"compaction"``
    #: when this turn is the estimated request that wrote a compaction's
    #: summary, not a reply Claude Code logged.
    estimated: str | None = None


@dataclass(slots=True)
class TranscriptMeta:
    """Identity and provenance of one transcript file."""

    path: str = ""
    kind: str = "top-level"  # "top-level" | "subagent" | "workflow-agent"
    session_id: str = ""
    agent_id: str | None = None
    agent_type: str | None = None
    description_len: int = 0
    spawn_depth: int = 0
    parent_agent_id: str | None = None
    agent_model_alias: str | None = None
    request_shape: str | None = None
    worktree_branch_present: bool = False
    stopped_by_user: bool | None = None
    project_slug: str = ""
    claude_version: str | None = None
    mtime_ns: int = 0
    size_bytes: int = 0
    #: Independent-review addition (see module docstring): the subagent's
    #: ``.meta.json`` ``toolUseId``, linking it to the parent turn that
    #: spawned it.
    tool_use_id: str | None = None
    #: Batch C addition (see module docstring): the API surface these
    #: turns were billed through, derived from model-id form.
    provider: str | None = None
    #: Batch C addition (see module docstring): the first non-empty
    #: ``entrypoint`` field seen anywhere in the transcript's raw lines.
    entrypoint: str | None = None
    #: Discovery fix addition (see module docstring): the workflow run id
    #: a workflow-nested subagent lives under.
    workflow_run_id: str | None = None
    #: Quality-signals addition (see module docstring): a workflow
    #: agent's end state in its finished run file.
    workflow_agent_state: str | None = None
    #: Metrics-capture addition (see module docstring): the capture note
    #: format version seen, the metric codes the notes asked for, and how
    #: many notes were injected.
    cap_version: int | None = None
    cap_metrics: tuple[str, ...] = ()
    cap_injections: int = 0
    #: Parser-signals addition (see module docstring): the last
    #: ``cost-state`` line's own ``totalCostUSD``/``hasUnknownModelCost``.
    cc_cost_usd: float | None = None
    cc_cost_has_unknown_model: bool = False
    #: Cost-record addition (see module docstring): when that total was
    #: written, as the latest line time before it, and its cost by model.
    cc_cost_as_of: str | None = None
    cc_cost_by_model: dict = field(default_factory=dict)


@dataclass(slots=True)
class Diagnostics:
    """Parse-quality counters surfaced in the Diagnostics report section."""

    lines: int = 0
    unparsable_lines: int = 0
    truncated_final_line: bool = False
    assistant_lines: int = 0
    distinct_turns: int = 0
    synthetic_turns: int = 0
    turns_missing_usage: int = 0
    ttl_sum_mismatch: int = 0
    late_duplicate_ids: int = 0
    ignored_line_types: dict = field(default_factory=dict)
    #: WP1 addition (plan deviation, see module docstring): lines skipped
    #: by ``jsonl.iter_lines`` for exceeding ``MAX_LINE_BYTES``, counted
    #: without being parsed.
    oversized_lines: int = 0
    #: Independent-review addition (see module docstring): events observed
    #: after the last finalised turn, attached to no turn.
    trailing_events: int = 0
    #: Independent-review addition (see module docstring): lines skipped
    #: because their ``uuid`` had already been seen earlier in this file.
    replayed_lines: int = 0
    #: ``PARSER_VERSION`` 26 (see module docstring): lines skipped because
    #: they belong to another session whose own file holds them too.
    copied_lines: int = 0
    #: ``PARSER_VERSION`` 27 (see module docstring): compactions whose
    #: summary request was estimated and priced, and those left out.
    compaction_calls: int = 0
    compaction_calls_unsized: int = 0
    #: Independent-review addition (see module docstring): a priced turn's
    #: timestamp was present but failed to parse.
    timestamp_parse_failures: int = 0
    #: Independent-review addition: ``agent-setting`` lines are ignored as
    #: events (see events.py) but their ``agentSetting`` value is useful
    #: for archetype detection, so it's counted here instead of dropped
    #: entirely: value -> occurrence count.
    agent_settings: dict = field(default_factory=dict)
    #: Independent-review addition: ``mode`` lines are ignored as events
    #: but their value (normal/plan/auto) is counted here: value -> count.
    modes: dict = field(default_factory=dict)
    #: Independent-review addition: attachment types that fell into the
    #: generic ATTACHMENT catch-all kind (A2's "any attachment type not
    #: listed above" row), counted by type for next-release triage.
    attachment_catch_all: dict = field(default_factory=dict)
    #: Coordinator follow-up (WP12a diversity fixtures): turns whose
    #: usage had no nested ``cache_creation`` object at all (see
    #: ``Turn.ttl_split_unknown``). This is a format difference, not an
    #: invariant breach, so it's counted separately from
    #: ``ttl_sum_mismatch`` rather than folded into it.
    pre_split_turns: int = 0
    #: Usage-limits addition (see module docstring): corpus-wide counts
    #: of the three new event kinds.
    limit_hits: int = 0
    limit_resumes: int = 0
    agents_terminated: int = 0
    #: Fast-mode addition (see module docstring): corpus-wide totals set
    #: once by ``report.build_report`` from the finished
    #: ``pricing.PricingCoverage`` accumulator (pricing-time, not
    #: parse-time, so unlike every other field here it is not merged
    #: per-transcript). Turns priced against another model's rate card
    #: because their own model id only prefix-matched, and turns flagged
    #: ``usage.speed == "fast"`` that were priced at standard rates for
    #: lack of a ``[.fast]`` table, respectively. See
    #: ``PricingCoverage.closest_matches``/``fast_priced_as_standard``.
    pricing_closest_match_turns: int = 0
    pricing_fast_priced_as_standard_turns: int = 0



def agent_type_label(result: "TranscriptResult") -> str:
    """How every per-agent table names a transcript's agent: ``"top-level"``
    for the main conversation, else the recorded agent type, or
    ``"unknown"`` for a subagent that recorded none (never
    ``"top-level"``, so untyped subagent cost is not filed under the main
    session)."""
    if result.meta.kind == "top-level":
        return "top-level"
    return result.meta.agent_type or "unknown"


def scheduled_main_session(result: "TranscriptResult") -> bool:
    """A main session a scheduled or looped task started, with no message
    of yours: usually a check that runs a command or two and stops, a
    different job from the work you steer. Left out of the quality setup
    comparisons, the compaction summary and the compaction replay, where a
    few dozen of them would dilute the per-session figures."""
    return (
        result.meta.kind == "top-level"
        and all(turn.human_prompt_chars is None for turn in result.turns)
        and any(event.kind == EventKind.SCHEDULED_TASK for event in result.events)
    )

@dataclass(slots=True)
class TranscriptResult:
    """The parsed output of one transcript file: its turns and events plus
    parse diagnostics and raw tool-result size accounting.
    """

    meta: TranscriptMeta = field(default_factory=TranscriptMeta)
    turns: list[Turn] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    tool_result_chars: dict = field(default_factory=dict)
    tool_result_calls: dict = field(default_factory=dict)
    #: Parser-signals addition (see module docstring): counters that don't
    #: fit ``Diagnostics`` -- ``unknown_line_types``, ``unsized_blocks``.
    parser_notes: dict = field(default_factory=dict)
    #: Tool-search addition (see module docstring): tool name -> characters
    #: of its full definition, for each deferred tool this transcript loaded.
    tool_definition_chars: dict = field(default_factory=dict)


@dataclass(slots=True)
class WorkflowRun:
    """One ``<session>/workflows/wf_*.json`` run."""

    run_id: str = ""
    session_id: str = ""
    agent_count: int = 0
    phases: int = 0
    started: str | None = None
    finished: str | None = None
    cost: float = 0.0
    #: Batch C addition (see module docstring): the run file's own
    #: ``status`` (``"completed"``/``"killed"`` observed).
    status: str | None = None
    #: Batch C addition (see module docstring): each phase's ``title``
    #: only — never ``detail``, which carries workflow source/prompt text.
    phase_titles: tuple[str, ...] = ()


@dataclass(slots=True)
class Classification:
    """Mode/purpose classification for one session, with the evidence that
    produced it.
    """

    mode: str = ""
    mode_source: str = ""
    mode_evidence: dict = field(default_factory=dict)
    purpose: str = ""
    purpose_source: str = ""
    purpose_evidence: dict = field(default_factory=dict)


@dataclass(slots=True)
class SessionRecord:
    """One top-level session: its own transcript, its subagent and
    workflow transcripts, and the classification derived from them.
    """

    session_id: str = ""
    slug: str = ""
    first_ts: str | None = None
    last_ts: str | None = None
    span_s: float = 0.0
    top: TranscriptResult | None = None
    subs: list[TranscriptResult] = field(default_factory=list)
    workflows: list[WorkflowRun] = field(default_factory=list)
    classification: Classification | None = None
    archetype: str | None = None
    snapshot_id: str | None = None
    profile_id: str | None = None
    #: Batch C addition (see module docstring): carried through from
    #: ``top.meta.entrypoint`` by ``classify.build_session_record``.
    entrypoint: str | None = None
    #: The key this session's project's config snapshots carry (see
    #: ``snapshots.snapshot_project_key``); set by the report builder.
    project_key: str | None = None


@dataclass(slots=True)
class CostBreakdown:
    """Priced cost for one turn or one aggregate, split by token type."""

    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_write_cost: float = 0.0
    cache_read_cost: float = 0.0
    #: ``turn.web_search_requests`` priced at the rate card's
    #: ``[server_tools].web_search_per_1000`` (0.0 when the rate card
    #: sets no rate, or the model didn't resolve). Included in ``total``.
    #: ``web_fetch_requests`` has no documented per-request rate, so it
    #: is counted but never priced (see pricing.toml's ``[server_tools]``
    #: comment).
    server_tool_cost: float = 0.0
    total: float = 0.0
    long_context_applied: bool = False
    model_known: bool = False
    #: Fast-mode addition (see module docstring): the turn's own
    #: ``speed == "fast"`` AND the resolved model carries a ``[.fast]``
    #: rate table, so the fast multiplier was actually applied. A turn
    #: with ``speed == "fast"`` and this False was priced at standard
    #: rates for lack of a fast-mode rate on file.
    fast_applied: bool = False


@dataclass(slots=True)
class Column:
    """One table column: what it is called and how its values render."""

    key: str = ""
    label: str = ""
    #: "str" | "int" | "float" | "pct" | "money" | "tokens" | "secs"
    kind: str = "str"
    align: str | None = None
    #: Readability addition: one plain-English sentence saying what the
    #: column's number means. Empty when none has been written yet.
    help: str = ""


@dataclass(slots=True)
class Help:
    """Readability addition: the "how to read this" block for a table or
    section, in the house style of ``docs/writing-help.md``."""

    #: What the table shows, in one or two sentences.
    shows: str = ""
    #: How to read the numbers.
    read: str = ""
    #: When to act, and what to do. Empty when there is nothing to do.
    act: str = ""


#: ``Table.dashboard`` values: shown on the dashboard, shown collapsed
#: under "Advanced detail", or kept out of the dashboard (still in the
#: CLI/JSON/CSV report).
DASHBOARD_PLACEMENTS = ("keep", "advanced", "report")


@dataclass(slots=True)
class Table:
    """A renderer-agnostic table: columns plus rows of raw values (not yet
    formatted — ``render.tables.format_cell`` does that per-column at
    render time).

    Readability additions (all defaulted): ``help``, ``value_labels``,
    ``row_groups``, ``row_kinds``, ``lead_columns`` and ``dashboard``.
    ``value_labels`` maps a raw string cell value (a row
    key such as ``"full-expiry"``) to its display label; it is display
    only -- ``rows`` keep their raw values, which ``recommend.py``'s
    evidence lookups and ``tests/test_recommend_contract.py`` rely on.
    """

    name: str = ""
    title: str = ""
    columns: list[Column] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    help: Help | None = None
    value_labels: dict[str, str] = field(default_factory=dict)
    #: Display only: first-column row key -> the heading of the group the
    #: row starts or belongs to, for a long "metric / value" table whose
    #: rows mix counts, tokens and money. Consecutive rows share a group.
    row_groups: dict[str, str] = field(default_factory=dict)
    #: Display only: first-column row key -> the ``Column.kind`` to format
    #: that row's "str"-kind cells with (a "metric / value" table whose one
    #: value column holds counts, tokens, money and percentages).
    row_kinds: dict[str, str] = field(default_factory=dict)
    #: Display only: column keys, in the order the dashboard shows them
    #: first. A wide table lists at most 7 (the row key first; the rest
    #: stay behind the grid's column chooser); a one-row summary table
    #: lists at most 4 headline values, shown as tiles. Empty means the
    #: grid's own default. The CLI's Markdown and HTML renderers ignore it.
    lead_columns: list[str] = field(default_factory=list)
    #: One of :data:`DASHBOARD_PLACEMENTS`.
    dashboard: str = "keep"


@dataclass(slots=True)
class Section:
    """One report section: a heading, its tables, and free-text notes.

    Readability additions (defaulted): ``intro``, a one-line summary shown
    under the heading, and ``help``, its "how to read this" block.
    """

    key: str = ""
    title: str = ""
    tables: list[Table] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    intro: str = ""
    help: Help | None = None


@dataclass(slots=True)
class SettingChange:
    """One concrete edit a recommendation proposes: which key, where, and
    to what. ``target`` is ``"settings"`` (a ``settings.json`` key) or
    ``"agent"`` (a ``.claude/agents/<agent>.md`` frontmatter field).
    ``value`` is ``None`` when the right value needs your judgement (a
    narrower ``tools`` list, say); ``suggested`` then describes what to
    choose. ``unconfirmed`` marks a change whose effect Claude Code's
    docs don't state, so the explainer says so.
    """

    target: str = "settings"  # "settings" | "agent"
    key: str = ""
    agent: str | None = None
    value: object = None
    suggested: str = ""
    note: str = ""
    unconfirmed: bool = False
    #: The value in effect now, from the latest config snapshot (``None``:
    #: not set, or no snapshot).
    current: object = None
    #: ``agent`` is a built-in agent type with no file: the change means
    #: writing a same-named agent file, which needs judgement, so it is
    #: offered as a prompt only.
    new_agent_file: bool = False
    #: Where this one change is made, when it differs from the
    #: recommendation's own ``scope`` (a recommendation that groups
    #: several agents, some user-level and some project-level).
    scope: str = ""
    #: This change's own share of the saving, when the recommendation
    #: groups several changes; the explainer falls back to the
    #: recommendation's ``estimated_saving``.
    saving: str = ""


@dataclass(slots=True)
class Recommendation:
    """One actionable finding, with evidence traceable back into a
    specific report table so a test can assert every value it cites
    actually exists there.
    """

    id: str = ""
    severity: str = "info"  # "info" | "advice" | "action"
    category: str = "workflow"  # "settings" | "workflow" | "data"
    archetypes: tuple[str, ...] = ()
    title: str = ""
    action: str = ""
    lever: str | None = None  # settings key / frontmatter path / None
    #: list of (label, value, source_table, row_key)
    evidence: list = field(default_factory=list)
    #: WP10-merge addition (additive, defaulted): where ``lever`` applies —
    #: "user" (``~/.claude/settings.json``), "repo" (project
    #: ``.claude/settings.json`` / frontmatter, checked in), "project-local"
    #: (COV-01 addition: project ``.claude/settings.local.json``, per-
    #: machine, never checked in — no per-agent-file equivalent), or
    #: "managed" (an org-pushed managed-settings key, which the user
    #: cannot change locally). Replaces the earlier ``"[managed] "``
    #: string prefix on ``title`` that ``recommend.py`` used to encode
    #: the same fact.
    scope: str = "user"
    #: Fix R13 (additive): the agent type this recommendation is about,
    #: e.g. "claude-implementer", or "top-level" for the main session
    #: itself -- None when the recommendation is not agent-scoped (a
    #: corpus-wide rule like baseline-bloat or pricing-coverage).
    #: ``render_patch_set`` uses this to merge every per-agent lever for
    #: the same agent type into a single ``.claude/agents/<type>.md``
    #: stanza instead of guessing the agent type back out of ``lever``'s
    #: text.
    agent_type: str | None = None
    #: Readability additions (defaulted): the concrete edits this
    #: recommendation proposes (``fixes.build_fix`` turns each into an
    #: explainer, a command and a prompt), the estimated saving already
    #: phrased for the billing mode (``units.Units``), and a plain
    #: sentence on why it matters.
    changes: list[SettingChange] = field(default_factory=list)
    estimated_saving: str = ""
    #: How ``estimated_saving`` was worked out, for the explainer.
    saving_basis: str = ""
    #: The saving as a number (USD at list price), used only to order
    #: recommendations of the same severity; ``None`` when not estimated.
    saving_usd: float | None = None
    why: str = ""
    #: ``fixes.build_fix`` output per change, filled by ``report.build_report``:
    #: dicts with ``explainer`` (list of (heading, text)), ``command`` and
    #: ``prompt``.
    fixes: list = field(default_factory=list)
    #: Additive: a deterministic, URL-safe (``[a-z0-9._:-]``) identity for
    #: this recommendation -- ``id`` alone repeats across agent types (the
    #: same rule fires once per subagent type), so ``recommend.recommend``
    #: fills this in as its very last step from ``id`` plus a slug of
    #: ``agent_type`` when one is set. Stable across two runs of the same
    #: corpus; a dashboard link uses it as
    #: ``#/actions/recommendations?id=<key>``.
    key: str = ""


@dataclass(slots=True)
class PricingMeta:
    """Pricing provenance printed in the report header. Not itself named
    in plan Appendix A1 — see the module docstring's deviation note.
    """

    path: str | None = None
    version: str | None = None
    sha8: str | None = None
    currency: str = "USD"
    coverage_pct: float = 0.0


@dataclass(slots=True)
class ReportMeta:
    """``ReportModel.meta``: run identity, window and pricing provenance.
    Not itself named in plan Appendix A1 — see the module docstring's
    deviation note.
    """

    tool_version: str = ""
    generated_at: str = ""
    window: str = ""
    projects: tuple[str, ...] = ()
    pricing: PricingMeta = field(default_factory=PricingMeta)
    thresholds: dict = field(default_factory=dict)
    billing_mode: str = "api"  # "api" | "subscription"
    #: Why ``billing_mode`` has its value (``Config.billing_source``).
    billing_source: str = ""
    #: What amounts mean in this billing mode (``units.Units.basis``):
    #: list price, a share of the weekly limit, or list-price
    #: equivalents when that share can't be worked out.
    amounts_basis: str = ""
    #: UX-1: ``{mode, share_per_usd, period_label, basis}`` -- the same
    #: billing-mode facts as ``billing_mode``/``amounts_basis`` above, in
    #: the shape ``units.Units.money``'s JS mirror (format.js's
    #: ``money()``) needs to phrase an arbitrary amount client-side
    #: without a round trip through a table cell: ``mode`` is
    #: ``billing_mode``; ``share_per_usd`` is the percentage points of
    #: the weekly usage limit one list-price dollar is worth
    #: (``elasticity.express_in_window(1.0, ...)``), or ``None`` when
    #: there's no accepted fit yet; ``period_label`` is what that share
    #: is "of" ("weekly usage limit"); ``basis`` is ``amounts_basis``
    #: again, kept alongside so a consumer of ``meta.units`` alone (no
    #: other ``meta`` field) still has the caveat text.
    units: dict = field(default_factory=dict)
    #: TTL/RE-CACHE/etc. assumption text, rendered as the report's
    #: "## Assumptions" block. See the module docstring's deviation note.
    assumptions: list[str] = field(default_factory=list)
    #: Additive: every priced model's own rates and derived ratios
    #: (``pricing.Pricing.rates_meta``) -- the dashboard's own rate card,
    #: keyed by canonical model id, only models ``pricing.toml`` prices.
    rates: dict = field(default_factory=dict)


@dataclass(slots=True)
class ReportModel:
    """The whole assembled report: what every renderer (Markdown, JSON,
    CSV, HTML) consumes.
    """

    meta: ReportMeta = field(default_factory=ReportMeta)
    sections: list[Section] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    #: Per-file CLAUDE.md-family and per-skill usage
    #: (``context_files.ContextFileStats.to_dict``): hashes, names, sizes,
    #: counts and estimated costs only.
    context_files: dict = field(default_factory=dict)
    #: Parser-signals addition (see module docstring): corpus-wide totals
    #: of ``TranscriptResult.parser_notes``, merged the same way as
    #: ``context_files`` -- alongside, not inside, ``_merge_diagnostics``.
    parser_notes: dict = field(default_factory=dict)
    #: How amounts are phrased for this report's billing mode
    #: (``units.Units``), for routes that phrase amounts after the fact.
    #: Typed loosely because this module imports nothing from the
    #: package; left out of JSON output.
    units: object = field(default=None, metadata={"json": False})


__all__ = [
    "EventKind",
    "Event",
    "CaptureTag",
    "PlanStats",
    "PROMPT_FLAGS",
    "Turn",
    "TranscriptMeta",
    "Diagnostics",
    "TranscriptResult",
    "WorkflowRun",
    "Classification",
    "SessionRecord",
    "CostBreakdown",
    "Column",
    "Table",
    "Section",
    "Recommendation",
    "SettingChange",
    "PricingMeta",
    "ReportMeta",
    "ReportModel",
]
