# What MCP tool search saves

With tool search on (Claude Code's default on the Anthropic API), Claude
Code sends deferred tools, most of them MCP tools, by name only. When
Claude needs one, it calls `ToolSearch`, and Claude Code loads that
tool's full definition into the conversation. Without tool search, every
definition would sit at the front of every request.

The `tool_search` section (`tool_search.py`) puts a number on that, and
lists every MCP server your sessions were offered, whether Claude used
it and what keeping it cost. It appears on the dashboard under Agents &
context › Context, and in the terminal as the `tool-search` check:

```powershell
python -m claudeglass check tool-search --all-projects
```

## What the transcripts record

Claude Code writes these notes into each transcript:

- `deferred_tools_delta` lists the tools added to, or removed from, the
  deferred list (`addedNames`, `removedNames`). The list shrinks while an
  MCP server reconnects and comes back after, and is sent again in full
  after a conversation summary. `surfacedNames` are listed tools also
  sent with their full definition. `pendingMcpServers`,
  `needsAuthMcpServers` and `failedMcpServers` name the servers still
  connecting, waiting for you to sign in, or failing to connect.
- `deferred_tools_record` lists each full definition Claude Code loaded
  (`entries`: name, description, input schema).
- `mcp_instructions_delta` adds or removes an MCP server's own
  instructions to Claude (`addedNames` with `addedBlocks`,
  `removedNames`).
- `prompt_snapshot` (in the Claude Code versions that write its `tools`)
  lists the tools sent with their full definition: surfaced ones, and
  those of a server loaded up front.

`parse.py` keeps, per reply, how many listed tools had no definition
loaded when the reply was requested, by MCP server
(`Turn.deferred_tools_by_server`, surfaced tools left out), the size of
the name list (`Turn.deferred_list_chars`) and each server's share of it
(`Turn.deferred_list_chars_by_server`), the length of each server's
instructions (`Turn.mcp_instruction_chars_by_server`), and the servers
whose resources Claude read (`Turn.mcp_resource_servers`, from the
`server` input of `ReadMcpResourceTool` and `ListMcpResourcesTool`).
Per transcript it keeps each loaded definition's size by tool name
(`TranscriptResult.tool_definition_chars`), each server's tool names
after `mcp__<server>__` (`mcp_tool_suffixes_by_server`, each cut to 64
characters), the size of the tools a `prompt_snapshot` shows sent in
full (`upfront_definition_chars_by_server`), and each server's last
connection problem (`mcp_connection_status`).

A server's raw name ("claude.ai Claude Docs", "plugin:playwright:playwright")
is keyed the way its tools carry it (`parse.mcp_name`: every character
outside letters, digits, `_` and `-` made `_`, at most 64 characters).
It never keeps a description, a schema or instruction text, and a tool
name outside the API's tool-name alphabet is dropped.

## The model

For each reply requested with tools deferred:

- **Kept out.** Each deferred tool whose definition wasn't loaded is
  counted at the average definition size of its own MCP server, measured
  from the definitions loaded anywhere in the window. A server none of
  whose tools was loaded takes the average of every server. Sizes are
  characters / 4, as everywhere else in the report.
- **Priced** at that reply's own rate for the front of the prompt: its
  cache read rate when it read the cache, its cache write rate when it
  wrote the cache from the start, its input rate when it cached nothing.
  Rates come from `price_turn`, so geo and long-context multipliers
  apply.
- **Taken off.** The name list sent in the definitions' place, priced
  the same way. And every reply whose only tool call was `ToolSearch`,
  in full: without tool search, Claude would have called the tool
  directly.

A definition that was loaded counts as sent either way, so it adds
nothing to the saving and nothing to the cost.

## Reading it

- **Net saving** is the headline. It is an estimate: most deferred tools
  are never loaded, so their size comes from the ones that were.
- **Sized from** says whether a server's size came from its own loaded
  tools or from every server's. A server whose tools you never use is
  always sized from every server's.
- When no definition was loaded in the window, nothing can be sized:
  the token and money columns stay blank and the check reports "no
  data".

On one real set of cloud sessions (Claude Code 2.1.283, five MCP servers
with up to 154 deferred tools), tool search kept about 57,500 tokens out
of each of 588 replies. That saved $6.77 at list price, less $0.16 for
the name list and $1.03 for 16 replies that only searched: $5.58 net.

## Each MCP server

`tool_search_servers` lists every MCP server the window's transcripts or
your config snapshots name, whatever its status, uncapped. Nothing in
the code names a server: what a server is, and whether it was used,
comes from generic evidence only.

- **Offered** when a reply's name list, instructions or tools sent in
  full include it. Your config alone never counts, so a server that
  failed to connect can't look unused.
- **Used** when Claude called one of its tools, read one of its
  resources, ran one of its prompts (`/mcp__<server>__<prompt>`), a
  reply is attributed to it (`attributionMcpServer`), or a known token
  saver's hook pointed Claude at its tools.
- **Two names, one server.** The desktop app names a claude.ai
  connector by an ID where Claude Code on its own calls it
  `claude_ai_<name>`. An ID-named server with exactly the same tools as
  a named one is merged into it, and a use under either name counts.
- **Kind**, from positive evidence only: a claude.ai connector (by name,
  and in the desktop app by an ID matched to that name), a plugin's
  server (`plugin_...`), or a server in a config snapshot's local
  (`~/.claude.json`, per project), project (`.mcp.json`), managed or
  user list. With `managedMcpServers` set, a server only the merged
  list has may be your organisation's, so it counts as managed.
  Anything else is of unknown kind.
- **Priced** per reply at the front-of-prompt rate: its share of the
  name list by length, its instructions, and its tools sent in full.
  That is a lower bound: a reply that rebuilt the cache paid its write
  rate.

Its status is one of: `remove` (the card below), `unused` (never used,
below one of the bars), `all-projects view only` (a server every
project loads, or one the desktop app names by ID, whose claude.ai
name may only show up in another project's sessions: judged only in
the all-projects view), `kind unknown`,
`managed`, `subagents only` (left to `spawn-unused-mcp`), `used`,
`needs sign-in`, `failed to connect`, `pending`, and `configured, not
seen`.

### The `mcp-unused-server` recommendation

One card (severity `advice`, category `workflow`) lists every server
whose status is `remove`: offered in at least 10 main sessions
(`tool_search_unused_min_sessions`), first at least 7 days
(`tool_search_unused_min_age_days`) and last at most 3 days
(`tool_search_unused_max_idle_days`) before the window's end, never
used, of a kind you can turn off, and costing at least $0.50
(`tool_search_unused_min_server_usd`). The card needs $1.00 in all
(`tool_search_unused_min_total_usd`). Each server gets the fix for its
kind; servers whose fix doesn't name them (connectors, plugins) share
one sentence:

| Kind | Fix |
|---|---|
| claude.ai connector in the desktop app | switch it off under + > Connectors, or, if you added it, disconnect it at claude.ai/customize/connectors (which also removes it from claude.ai chat; a connector Anthropic provides itself, such as Claude Docs, has nothing to disconnect there) |
| claude.ai connector | `/mcp` → disable (this project only), or, if you added it, disconnect it at claude.ai |
| plugin | `/mcp` → disable (this project only), or turn off the plugin with `/plugin` |
| user | `claude mcp remove <name> --scope user` |
| local | in the named project, `claude mcp remove <name> --scope local` |
| project | `disabledMcpjsonServers` in `.claude/settings.local.json` (just you); `claude mcp remove <name> --scope project` edits the shared `.mcp.json`, so only if your team agrees |

It has no lever and no setting change; ignoring it holds until the set
of servers changes (`Recommendation.subject`). The saving starts with
your next new session, or once a conversation is summarised: removing a
server doesn't change what a running conversation already holds. The
`tool-search` check offers its fix even when no reply deferred a tool,
since a server loaded up front isn't deferred.

## What it doesn't cover

- Third-party "token saver" MCP servers (compressors, code-search
  replacements, memory servers) save tokens by changing how Claude works,
  not by keeping definitions out. Measuring them needs a comparison of
  sessions with and without them: see [`savers.md`](savers.md).
- When tool search is off (for example behind a proxy that doesn't
  forward `tool_reference` blocks), the transcripts carry no deferred
  list, so there is nothing to measure. The `env-tool-search`
  recommendation covers that case. The servers table still sizes a
  server loaded up front, where the Claude Code version writes a
  `prompt_snapshot` with its tools.
- A server's `@` resource mentions leave no mark in a transcript, so a
  server you use only that way looks unused.
- In the desktop app, a connector is recognised only once it has also
  appeared under its `claude_ai_<name>`. Until then its ID-named server
  is of unknown kind and gets no card: ID-named servers in the desktop
  app are usually connectors, listed under + > Connectors.
- Two raw names that normalise to the same key are counted as one
  server.
- "Saved by keeping them out" falls when you turn a server off: there
  is less left to keep out.
