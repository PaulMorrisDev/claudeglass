"""claudeglass: config-aware token and prompt-cache analytics for
Claude Code transcripts.
"""

from __future__ import annotations

__version__ = "0.14.0"

#: Bump when transcript-parsing logic changes in a way that could change
#: results computed from a previously cached file.
#:
#: Bumped to 2 by the wp11-cache/wp12a-fixtures merge: parse.py's
#: ttl_split_unknown/pre_split_turns fix (main's 709239a) and
#: workstyle.py's chat-only-before-single-model reorder (main's 0f892e3)
#: both landed on main after wp11-cache branched from 25c68c1 (before
#: PARSER_VERSION existed), so a cache entry written under the old
#: parsing behaviour must not be treated as still valid.
#:
#: Bumped to 3 by f99901f (fix(privacy): redact relative Windows paths
#: and @-tokens at parse time): ``_redact_paths`` in parse.py now catches
#: relative ``Users\``/``home/`` paths and any ``@``-bearing token that
#: previously survived redaction into ``cmd_prefix``, so a digest cached
#: from before that fix reflects a leakier ``cmd_prefix`` and must be
#: treated as stale.
#:
#: Bumped to 4 by the capture-improvements batch: parse.py now derives
#: seven new additive ``Turn`` fields from a transcript's raw lines
#: (``tool_wait_s``/``model_latency_s``/``tool_result_chars_by_tool``,
#: ``agent_brief_chars``/``tool_input_chars_by_tool``,
#: ``read_target_hashes``, ``human_prompt_chars``/``human_prompt_has_paste``
#: -- see model.py's module docstring), none of which a pre-batch digest
#: cache entry ever computed, so it must not be treated as still valid.
#:
#: Bumped to 5 by the v3-limits batch: parse.py now detects usage-limit
#: pauses (LIMIT_HIT/LIMIT_RESUME/AGENT_TERMINATED events,
#: Turn.synthetic_kind, Turn.gap_cause -- see model.py's module
#: docstring), none of which a pre-batch digest cache entry ever
#: computed, so it must not be treated as still valid.
#:
#: Bumped to 6 by the v4-wasted-turns batch: parse.py now derives two new
#: additive ``Turn`` fields, ``tool_error_count``/``tool_error_chars``,
#: from each turn's own tool_result blocks that carry ``is_error: true``
#: (see model.py's module docstring), which no pre-batch digest cache
#: entry ever computed, so it must not be treated as still valid.
#:
#: Bumped to 7 by the readability batch: events.py now measures an
#: attachment's ``Event.size_chars`` from the real ``rendered`` shape (a
#: list of ``{"content": str}`` blocks) or its content fields, where it
#: previously read only a bare string and so recorded ``None``; it also
#: adds per-source ``instructions`` sizes and ``prompt_snapshot``
#: system/tool sizes to ``Event.detail``.
#:
#: Bumped to 8 by the context-files batch: events.py now keeps one record
#: per instruction file (salted path hash, type, path-scoped, size) for
#: ``instructions`` and ``nested_memory``, and one per skill (name, size)
#: for ``skill_listing`` and ``invoked_skills``; parse.py adds
#: ``Turn.skills_invoked``.
#:
#: Bumped to 9 by the quality-signals batch: parse.py adds
#: ``Turn.stop_reason``, ``tool_calls_by_tool``, ``tool_errors_by_tool``, ``edit_target_hashes``
#: and ``human_correction``; events.py records task-notification and
#: agent-result outcomes in ``Event.detail``.
#:
#: Bumped to 10 by the same batch: discovery.py records a workflow
#: agent's end state on ``TranscriptMeta.workflow_agent_state``, which a
#: version-9 digest (already written by a development build) lacks.
#:
#: Bumped to 11: parse.py took a turn's usage from the first line of a
#: streamed reply, whose output_tokens is a partial count and which has no
#: thinking_tokens; it now keeps the most complete snapshot among the
#: reply's lines, so older digests undercount output and thinking tokens.
#:
#: Bumped to 12: parse.py records why each failed tool call failed
#: (``Turn.tool_errors_by_kind``), which waste.py needs to tell a failing
#: test or a hook block from a call that couldn't run.
#:
#: Bumped to 13 by the three-pricing-fixes batch: parse.py now records
#: ``Turn.speed`` from each turn's own ``usage.speed`` (see model.py's
#: module docstring), which pricing.py needs to apply a model's fast-mode
#: rate multiplier. A pre-13 digest has no ``speed`` recorded, so every
#: transcript is re-parsed once to pick it up.
#:
#: Bumped to 14 by the edit-capture batch: ``Turn.edit_target_hashes`` now
#: covers MultiEdit and the files a Bash or PowerShell command writes
#: (``shell_writes.py``), drops edits whose tool call failed, and paths
#: are normalised further before hashing (Git Bash's ``/c/`` form, ``.``
#: and ``..``), so older digests undercount edits and can't match a file
#: written by a shell command to the same file edited with Edit. The same
#: bump adds ``Turn.retry_marker``/``result_marker`` (the quality markers
#: Claude can be asked to write; see ``quality.MARKER_LINES``).
#:
#: Bumped to 15 by the metrics-capture batch: parse.py reads capture tags
#: (``Turn.cap``, ``spawn_marker``, ``retry_marker`` gains ``scope``) and
#: capture notes (``Turn.cap_note_chars``, ``TranscriptMeta.cap_*``), and
#: records ``agent_result_chars``, ``prompt_flags``, ``plan_stats`` and
#: ``commands_run``. Three miscounts are fixed at the same time:
#: ``read_target_hashes`` covered edits as well as reads, so every edited
#: file looked re-read; ``hook_system_message`` lines (shown to you, never
#: to the model) were counted as hook context; and task notifications
#: weren't sized, so a background agent's report had no size.
#:
#: Bumped to 16 by the feedback batch: parse.py reads your /cg-feedback
#: answers (``Turn.feedback``) from the skill's ``[cg-fb: ...]`` line or,
#: failing that, from the AskUserQuestion result itself. ``commands_run``
#: now names skills you ran with a slash too: they are written
#: ``<command-message>`` first, as your message, and were missed.
#:
#: Bumped to 17 by the P2 capture-integrity batch: capture_tags.py now
#: strips the exact reminder sentence before the tail match (a tag glued
#: to a reminder no longer swallows it); a ``[cg-fb: ...]`` line only
#: counts when the cycle's first turn actually ran ``/cg-feedback``, a
#: forged one is ignored; a captured tag key is kept only when the
#: session's note said to ask for it (``cap_injections > 0`` and the key
#: is a requested metric); ``reported_task`` counts once per cycle, not
#: once per line; ``Turn.skills_invoked`` now runs skill names through
#: ``SKILL_NAME_PATTERN`` and drops one whose ``Skill`` call errored, so a
#: skill can no longer self-authorise a capture note by name alone; and
#: capture.py's coverage denominator drops feedback/interrupted/max_tokens
#: cycles, merges a cycle's tags key by key instead of replacing the
#: whole set, sizes ``_big_output`` per call instead of per turn, and
#: ``_WRAP`` carries a ``:Tool`` suffix. None of this is recoverable from
#: an older digest, so every transcript is re-parsed once to pick it up.
#:
#: Bumped to 18 by the P3 hook-lifecycle batch: a HOOK_OUTPUT event's
#: ``Event.detail`` now carries the closed hook-event bucket it ran under
#: (``hookName``, e.g. ``"PreToolUse"``, never the matcher/tool-name
#: suffix -- SURV-HE, G7), its real ``durationMs`` when Claude Code
#: recorded one (CAP-9/F10: this used to be dropped), and, only when
#: ``True``, whether the call ran ClaudeGlass's own capture hook script
#: (``capture``, never the command string itself). ``hook_health.py``'s
#: new ``count_hook_errors``/``measure_deep_wait`` both read these
#: straight off already-parsed events; a pre-18 digest has none of them,
#: so every transcript is re-parsed once to pick them up.
#: Bumped to 19 by the parser-signals batch (SURV-4/5/6/7): ``thinking_drop``
#: joins the CACHE_SIGNAL family; ``task_status``/``structured_output``
#: attachments get their own ``EventKind``s instead of falling into the
#: generic ATTACHMENT catch-all; ``cost-state`` lines are read for their
#: own ``totalCostUSD``/``hasUnknownModelCost`` (``TranscriptMeta.
#: cc_cost_usd``/``cc_cost_has_unknown_model``) instead of falling
#: through to UNKNOWN; a tool_result's or human prompt's own image/
#: document content blocks are sized by the documented image-token rule
#: instead of silently counting as 0 chars; and a line type no detection
#: rule recognises at all is now counted separately
#: (``TranscriptResult.parser_notes["unknown_line_types"]``) from one
#: this parser knows about and deliberately ignores. None of this is
#: recoverable from an older digest, so every transcript is re-parsed
#: once to pick it up.
#:
#: Bumped to 20 by the docs-and-privacy sweep: ``Diagnostics.
#: ignored_line_types`` now keys on the sanitised line type (or
#: ``"other"``), like ``unknown_line_types`` already did, instead of the
#: raw ``type`` straight off the wire. A cached pre-20 digest still holds
#: the raw keys, so every transcript is re-parsed once to drop them.
#:
#: Bumped to 21: ``capture_tags.filter_tag`` now drops tag keys only the
#: other scope is asked for, and a subagent's ``[cg: ...]`` no longer
#: counts as a tag (it could set a whole prompt cycle's task or level);
#: a subagent's ``out=`` is kept when a large-output note asked for it.
#: A cached pre-21 digest still holds the unfiltered tags.
#:
#: Bumped to 22: hook events now carry the hook's label (its script's file
#: name), why a failing hook failed and whether its script path is
#: relative; turns carry the context your hooks added and the calls they
#: blocked (and any unchanged re-send). None of this is in a pre-22
#: digest.
#:
#: Bumped to 23: a hook event also says whether its command uses a
#: Windows ``%VAR%`` variable, and a quoted script path with a tab in it
#: is no longer read as relative. A pre-23 digest has neither.
#:
#: Bumped to 24: discovery.py gives an agent under a workflow run's folder
#: ``TranscriptMeta.kind == "workflow-agent"``; it was ``"subagent"``, so
#: every table split by kind folded workflow agents into subagents. A
#: pre-24 digest (the parse cache's and the service store's alike) still
#: holds the old kind.
#:
#: Bumped to 25: a coaching note (``cg-coach v``, the capture hook's live
#: hints) is its own ``coaching_note`` event, and counts to
#: ``Turn.cap_note_chars`` but not to ``cap_injections``; a capture note
#: sharing an attachment with one keeps the coaching part apart
#: (``detail["coach"]``/``["coach_chars"]``). Notes on a message you send
#: (``UserPromptSubmit``) keep that hook name. A pre-25 digest counted
#: neither.
#:
#: Bumped to 26: a top-level transcript skips the lines of another
#: session whose own file sits beside it (``Diagnostics.copied_lines``),
#: so a session ``/clear`` copied into an earlier one's file is priced
#: once. A pre-26 digest priced it twice. Each turn also keeps how many
#: tools tool search listed by name only, by MCP server
#: (``Turn.deferred_tools_by_server``, ``deferred_list_chars``), and each
#: transcript the size of every definition it loaded
#: (``TranscriptResult.tool_definition_chars``); a pre-26 digest has none.
#:
#: Bumped to 27: each compaction adds an estimated turn for the request
#: that wrote its summary (``Turn.estimated == "compaction"``), which
#: Claude Code bills but never logs. A pre-27 digest left it out of spend.
#:
#: Bumped to 28: each turn records what the message before it asked for
#: (``Turn.prompt_steps``/``prompt_plan_mode``/``human_vague``/
#: ``human_ack``/``human_repeat``) and whether its reply ended on a
#: question or showed a ClaudeGlass tip (``reply_asked``/``coach_tip``).
#: A pre-28 digest has none of them.
#:
#: Bumped to 29: a message whose reply was an API error, an overload or a
#: usage limit isn't an answered attempt, so sending it again isn't
#: ``human_repeat``. A pre-29 digest counted those resends as repeats.
#:
#: Bumped to 30: a ``cost-state`` line with a zero total and no model in
#: ``modelUsage`` is a blank record, not Claude Code's cost, so it no
#: longer sets ``TranscriptMeta.cc_cost_usd``, and its ``startTime`` is
#: kept (``cc_cost_since``). A pre-30 digest compared those sessions'
#: local pricing against $0. A message that opens with a question word
#: is no longer ``human_vague``.
#:
#: Bumped to 31: an estimated compaction call's output is twice the
#: summary Claude Code keeps, capped at ``postTokens`` (a
#: ``COMPACT_SUMMARY`` event now carries ``size_chars``). A pre-31 digest
#: priced every compaction's output at ``postTokens``.
#:
#: Bumped to 32: a tool hook's failed run keeps the tool it ran for
#: (``detail["tool"]``: a built-in tool's name, else ``mcp``), so the
#: hooks section can tell a hook that has stopped failing from one that
#: hasn't run since.
#:
#: Bumped to 33: a blocked tool call records who blocked it -- a known
#: token saver's redirect (``Turn.saver_redirects``), a Claude Code guard
#: or an unnamed hook (``Turn.guard_blocks``) -- and an auto mode
#: classifier that gave no verdict is ``denied``, not ``misfire``. A
#: pre-33 digest counted a saver's redirect as a plain block and a
#: classifier outage as a tool call that couldn't run.
#:
#: Bumped to 34: a tool-denial event whose denial was a known token
#: saver's redirect has subkind ``known_savers.REDIRECT_DENIAL_KIND``, not
#: Claude Code's own ``permission-rule``, so it no longer counts as a
#: request you turned down.
#:
#: Bumped to 35: each MCP server's share of the deferred name list, its
#: instructions' length, its tools' names, the tools sent in full and its
#: connection problems (model.py's MCP-servers addition), and a surfaced
#: tool no longer counts as kept out by tool search. A pre-35 digest has
#: none of these, so every server would look free.
#:
#: Bumped to 36: each agent's role word (a canonical word from
#: agent_roles.py, never the phase, label or description it came from),
#: whether its meta records a model at all, and the shell writes a turn
#: made outside the temp dir (model.py's Agent-roles addition). A pre-36
#: digest has none of these, so no agent could be told apart. It also
#: tests an edit tool's target against the temp dir the way a shell
#: write target is (a forward-slash or Git Bash path was a real edit
#: before), so a pre-36 digest counts those scratch writes as real
#: edits.
#:
#: Bumped to 37: how each message you typed reads -- a tweak, a repeat of
#: what you said, a bare go-ahead, a status check (``Turn.human_adjust``,
#: ``human_remind``, ``human_go``, ``human_status``) -- and the messages
#: you typed while Claude was working (``Turn.queued_prompts``,
#: ``queued_chars``, ``queued_steps`` and the flags beside them), which
#: no counter saw before. A queued message that is a copy of one also
#: written as a user line is dropped. An image message is no longer a
#: vague fix. Lines you didn't type are one list now, with the desktop
#: app's usage-limit and app-quit notes, another session's message and a
#: ``turnOrigin`` that rules a line out. An ``output_style`` attachment is
#: a reminder unless the style changed, and a ``permission-mode`` or
#: system ``informational`` line is ignored. A pre-37 digest has none of
#: these, counts every ``output_style`` as a cache change, and counts the
#: app's quit note as something you typed.
#:
#: Also in 37, from how each tool call that didn't run was answered
#: (model.py's Plan-feedback addition). Every denial event carries a
#: bucket word in ``detail["bucket"]``: a plan you sent back, a question
#: you declined, a hook's block, an auto mode block (or its being
#: unavailable), a dialog you closed, or a call you or a deny rule turned
#: down. Only that last bucket counts as a denial by habits, waste,
#: quality and prompting. An ``INTERRUPT`` has a subkind
#: (``tool_refusal`` or ``shutdown``) and, for a refusal, the bucket of
#: the denial it follows. A plan you sent back with feedback is a
#: ``PLAN_FEEDBACK`` event holding the feedback's length and one word for
#: how it reads (never its text), and ``ExitPlanMode`` answers no longer
#: add to ``Turn.tool_error_count``. ``PlanStats`` gains ``outcome``
#: ``approved_by_message`` (a go-ahead you typed, or leaving plan mode,
#: before the next plan), ``rejected`` and the feedback's length and
#: class; ``Turn`` gains ``ask_rounds`` and ``preceding_denials``. A
#: pre-37 digest has none of these, so it counts a plan or question you
#: answered as a denial and a stop, and a plan approved by typing as
#: never approved.
#:
#: Also in 37, from what Claude's replies and tool calls said (model.py's
#: Assistant-and-tool-signals addition), each kept as a yes/no, a count or
#: a size, never the words. ``Turn.reply_asked`` now means the reply ends
#: on a question to you: code, URLs, a ClaudeGlass tip and the tag are cut,
#: and the question mark must close one of the last two sentences or a
#: list item that ends it, not sit anywhere in the last 300 characters.
#: ``admit_candidate`` marks a reply that owns a mistake, with
#: ``admit_caught`` saying whether you had pushed back first (``user``) or
#: Claude noticed (``self``), and ``tip_disowned`` marks a reply that calls
#: a ClaudeGlass tip a misfire. ``read_target_chars`` gives the size of
#: each Read result beside ``read_target_hashes``. Reads made through
#: Bash or PowerShell (``grep``, ``sed -n``, ``cat``, ``git log`` and so
#: on) are counted in ``shell_read_count`` with their results' size in
#: ``shell_read_chars``. ``tests_run`` says whether the reply ran the
#: tests, ``targeted`` or ``full``, read from the whole command (an
#: interpreter path, an env or ``timeout`` prefix, a quoted path or
#: PowerShell no longer hides one) by the one matcher the parser, the
#: classifier and the capture hook share (``testrun.py``). A pre-37 digest
#: has none of these, so it reads a question mark anywhere in the last 300
#: characters as a question and finds a test run only by its command's
#: first words.
#:
#: And in 37, the join from a workflow's agents to the message that started
#: them. A ``Workflow`` call's result names the run (``runId``, the run's
#: directory name) and its task (``taskId``); ``Turn.workflow_runs`` keeps
#: the two ids under the call's tool_use id, nothing else of the result.
#: The agents of that run carry the same ``runId`` in their metadata, so
#: ``capture.prompt_cycles`` and ``habits`` now put each in the cycle of the
#: reply that launched (or resumed) its run: overlap, the agent list, a
#: cycle's cost and ``redo_cost`` count them. A resumed run keeps its
#: ``runId``, so its agents are split between the calls by time. A pre-37
#: digest has no ``workflow_runs``, so its workflow agents join the cycle of
#: the reply before the run's ``started`` time, or none. Also, a tag written
#: in reply to a background agent's (or workflow's) report counts for the
#: cycle whose call launched it, not the one open when the report arrived.
#:
#: And in 37, what counts as a vague correction or a repeated request
#: (``Turn.human_vague``, ``human_repeat``). A vague correction needs a
#: correction or bad-outcome phrase ("still failing", "that didn't work"):
#: a bare "fix this", anything ending in a question mark, a go-ahead and a
#: thank-you are no longer one. A poll ("how is it going"), a go-ahead and
#: an acknowledgement are never a repeat. A pre-37 digest from before
#: this change counts them.
#:
#: Also in 37: ``Turn.human_question``, ``config_edit_count`` and
#: ``agent_edit_files`` for the small-requests check, and
#: ``Turn.human_change`` (the message asked for a change: a change verb
#: opens one of its sentences, and it is no go-ahead, question, report or
#: explain request). The window between small requests is timed from your
#: own messages, a turn is credited only with the edits of the reply its
#: message started (a subagent's, with the turn that launched it; a reply
#: that began with a line you didn't type, ``Turn.preceding_not_typed``,
#: is not the message's), a plan is also a message of 2,000 characters or
#: more or one with five listed items, and a pasted log or code, or a merge
#: or release request (a verb that opens a sentence), has no steps. A
#: go-ahead also covers a merge, push, release, commit or run. A resume
#: note is a ``META`` event of subkind ``resume``, the other lines you
#: didn't type ``not_typed``.
#:
#: In 38, the tag's ``why`` and ``admit`` ride on the ``shift`` switch
#: (``found`` and ``fit`` are no longer asked for), and the transcript
#: settles what the words claim: ``Turn.edit_call_count``,
#: ``edit_doc_count`` and ``shell_change_count`` say what a reply changed
#: (edits to your work, those to documentation, and commands that move or
#: remove files), and ``capture.Cycle.settled`` is a cycle's tag with
#: ``capture_tags.settle`` applied, as the capture hook grounds Haiku's
#: words. A cycle with several tags keeps the highest ``level`` and
#: ``size`` among them.
#:
#: In 39, ``/cg-feedback`` asks new questions (``capture_catalogue.
#: FEEDBACK_QUESTIONS``) and ``model.Feedback`` gains ``why``, ``missed_in``,
#: ``plan``, ``tip``, ``tip_hint``, ``from_text``, ``other`` and
#: ``why_older``. A word Claude picked from a note typed under "Other"
#: counts only when the AskUserQuestion result shows a non-label answer
#: for that key, and a ticked answer always wins over the tag
#: (``capture_tags.settle_feedback``). An older run's ``slow`` gives
#: ``why``, and a run with no work since the previous one replaces that
#: run's answers (``capture.feedback_spans``). Also in 39, ``Turn.
#: plan_check`` holds your answer to the plan check (``model.PlanCheck``:
#: the id of the plan's ``ExitPlanMode`` call and a word of
#: ``capture_catalogue.PLAN_CHECK_WORDS``), and ``Turn.coach_reminder``
#: says a reply carries the /cg-feedback reminder line a hook note asked
#: for, so ``capture.usage`` prices it.
PARSER_VERSION = 39

#: Bump when the model.py contract changes in a way that invalidates the
#: on-disk digest cache (see model.py's module docstring for the contract
#: rules: fields may be added with defaults, never renamed or removed
#: without a SCHEMA_VERSION bump).
SCHEMA_VERSION = 1

__all__ = ["__version__", "PARSER_VERSION", "SCHEMA_VERSION"]
