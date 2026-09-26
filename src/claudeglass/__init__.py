"""claudeglass: config-aware token and prompt-cache analytics for
Claude Code transcripts.
"""

from __future__ import annotations

__version__ = "0.9.0"

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
#: Bumped to 16 by the feedback batch: parse.py reads your /tl-feedback
#: answers (``Turn.feedback``) from the skill's ``[tl-fb: ...]`` line or,
#: failing that, from the AskUserQuestion result itself. ``commands_run``
#: now names skills you ran with a slash too: they are written
#: ``<command-message>`` first, as your message, and were missed.
#:
#: Bumped to 17 by the P2 capture-integrity batch: capture_tags.py now
#: strips the exact reminder sentence before the tail match (a tag glued
#: to a reminder no longer swallows it); a ``[tl-fb: ...]`` line only
#: counts when the cycle's first turn actually ran ``/tl-feedback``, a
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
#: other scope is asked for, and a subagent's ``[tl: ...]`` no longer
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
#: Bumped to 25: a coaching note (``tl-coach v``, the capture hook's live
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
PARSER_VERSION = 27

#: Bump when the model.py contract changes in a way that invalidates the
#: on-disk digest cache (see model.py's module docstring for the contract
#: rules: fields may be added with defaults, never renamed or removed
#: without a SCHEMA_VERSION bump).
SCHEMA_VERSION = 1

__all__ = ["__version__", "PARSER_VERSION", "SCHEMA_VERSION"]
