import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { elements, faults, prepare, reply, SITE, START, textOf } from './support'

const PANE = { title: 'Glass', isFocused: true, bodyColumns: 110, placement: 'dock', ...SITE } as const

// One reply that leaves the main context at 120k of a 160k summarising point, $0.50 spent.
const FIRST = { steps: [{ input: 2000, cacheRead: 0, cacheWrite: 118000, output: 0 }], usd: 0.5 }
const SECOND = { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 }

const USER_MD = '/home/me/.claude/CLAUDE.md'
const PROJECT_MD = '/work/CLAUDE.md'
const RULES_MD = '/work/.claude/rules/style.md'
const SCOPED_MD = '/work/src/CLAUDE.md'

type Section = { id: string; text: string; scope: 'shared' | 'session' }
type File = { path: string; kind: 'managed' | 'user' | 'project' | 'local' | 'memory'; content: string; parent?: string }

/** Text of `chars` characters: at the fallback 4 characters per token it is chars / 4 tokens. */
function text(chars: number): string {
  return 'x'.repeat(chars)
}

/** A nested_memory attachment's text, `chars` characters long. */
function nested(chars: number): string {
  const head = `Contents of ${SCOPED_MD}:
`
  return head + text(chars - head.length)
}

/** What sits under the observed events: each answers as the engine would, and the mod must hand it on unchanged. */
function bottoms(on: On): void {
  on('prompt.compose', () => ({ sections: SECTIONS }) as never)
  on('prompt.context', (_$, e) => ({ blocks: e.blocks, instructionFiles: e.instructionFiles }) as never)
  on('tool.describe', (_$, e) => ({ description: e.description, ...(e.isDeferred === true ? { isDeferred: true } : {}) }) as never)
  on('skill.prompt', (_$, e) => ({ text: e.text }) as never)
  on('prompt.attachment', (_$, e) => ({ text: e.text }) as never)
  on('classic.InstructionsLoaded', () => ({}) as never)
}

const SECTIONS: Section[] = [
  { id: 'intro', text: text(8000), scope: 'shared' },
  { id: 'doing_tasks', text: text(4000), scope: 'shared' },
]

const FILES: File[] = [
  { path: USER_MD, kind: 'user', content: text(4000) },
  { path: PROJECT_MD, kind: 'project', content: text(24000) },
  { path: RULES_MD, kind: 'project', content: text(2000) },
]

const COMPOSE = { model: 'claude-sonnet-4-6', promptModel: 'claude-sonnet-4-6', surfaces: ['terminal'], tools: [], outputStyle: null, traits: [] }
const ENGINE = { plugin: 'engine', tier: 'core' }
const GITHUB = { plugin: 'mcp:github', tier: 'user' }
const DOCS = { plugin: 'mcp:docs', tier: 'user' }

/** What a session loads before its first message: the system prompt, the context blocks, the tools and the skill list. */
async function loadStartup($: Engine, over: { github?: number; files?: File[] } = {}): Promise<void> {
  await $.prompt.compose(COMPOSE as never)
  await $.prompt.context({
    blocks: [
      { name: 'claudeMd', text: text(30000) },
      { name: 'currentDate', text: text(40) },
    ],
    instructionFiles: over.files ?? FILES,
  } as never)
  await $.tool.describe({ tool: 'Bash', description: text(12000), provider: ENGINE } as never)
  await $.tool.describe({ tool: 'Read', description: text(8000), provider: ENGINE } as never)
  await $.tool.describe({ tool: 'mcp__github__create_issue', description: text(over.github ?? 44000), provider: GITHUB } as never)
  await $.tool.describe({ tool: 'mcp__docs__search', description: text(20000), isDeferred: true, provider: DOCS } as never)
  await $.tool.describe({ tool: 'NotebookEdit', description: text(4000), isDeferred: true, provider: ENGINE } as never)
  await $.prompt.attachment({ type: 'skill_listing', text: text(6000), origin: { kind: 'engine' } } as never)
}

async function pane($: Engine, surface: 'terminal' | 'desktop' = 'terminal') {
  const mounted = await $.ui.mount({ plugin: 'claudeglass', surface, component: 'Pane', requestId: 'glass', props: PANE as never })
  await mounted.press({ key: 'page:instructions' })
  return mounted
}

function lines(tree: unknown): string[] {
  return elements(tree)
    .filter(one => one.type === 'Text' || one.type === 'Button' || one.type === 'Link')
    .map(textOf)
    .filter(line => line !== '')
}

function buttons(tree: unknown) {
  return elements(tree).filter(one => one.type === 'Button')
}

test('nothing is shown until something has loaded', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  const mounted = await pane($)
  expect(lines(await mounted.drawn())).toContain('Nothing has loaded yet. What Claude reads for this session shows here once it starts.')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('each event is observed and answered unchanged', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)

  const composed = await $.prompt.compose(COMPOSE as never)
  expect(composed.sections.map(one => one.id)).toEqual(['intro', 'doing_tasks'])
  const blocks = [{ name: 'claudeMd', text: 'rules' }]
  const context = await $.prompt.context({ blocks, instructionFiles: FILES } as never)
  expect(context.blocks).toEqual(blocks)
  expect(context.instructionFiles).toEqual(FILES)
  expect(await $.tool.describe({ tool: 'Bash', description: 'Runs it.', provider: ENGINE } as never)).toEqual({ description: 'Runs it.' })
  expect(await $.tool.describe({ tool: 'mcp__docs__search', description: 'Finds.', isDeferred: true, provider: DOCS } as never)).toEqual({ description: 'Finds.', isDeferred: true })
  expect(await $.skill.prompt({ skill: 'commit', text: 'Write a commit.' } as never)).toEqual({ text: 'Write a commit.' })
  expect(await $.prompt.attachment({ type: 'todo_reminder', text: 'Update the todos.', origin: { kind: 'engine' } } as never)).toEqual({ text: 'Update the todos.' })
  expect(faults(world)).toEqual([])
})

test('Simple: the startup total, the five largest items and one line of advice', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)

  for (const surface of ['terminal', 'desktop'] as const) {
    const mounted = await pane($, surface)
    const tree = await mounted.drawn()
    const shown = lines(tree)

    // 2k + 1k (system) + 1k + 6k + 500 (files) + 10 (date) + 5k (built-in tools) + 11k (github) + 1.5k (skill list), less the deferred tools.
    expect(shown).toContain('Startup context is about 28k (est.), from 9 items.')
    expect(shown).toContain('Startup context: about 28k (est.)')
    expect(shown).toContain('The github MCP server puts about 11k (est.) of tool descriptions in every request. If this work doesn\'t need it, turn it off.')

    const rows = buttons(tree).filter(one => String(one.props?.key ?? '').startsWith('item:'))
    expect(rows.map(textOf)).toEqual([
      'github (1 tool) · 11k (est.)',
      'work/CLAUDE.md · 6k (est.)',
      'Built-in tools (2) · 5k (est.)',
      'intro · 2k (est.)',
      'skill listing · 1.5k (est.)',
    ])
    expect(rows.map(one => one.props?.hotkey)).toEqual(['1', '2', '3', '4', '5'])
    // The deferred tools are not part of what the session starts with, and the detail waits for [m].
    expect(shown.join('\n')).not.toMatch(/Deferred tools|docs/)
    expect(shown.join('\n')).not.toMatch(/path-scoped/)
    await mounted.unmount()
  }
  expect(faults(world)).toEqual([])
})

test('the advice appears only when a fixable item is large enough, and names the larger one', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  // An MCP server just under 10k tokens, and a CLAUDE.md of 6k: the file is the item to fix.
  await loadStartup($, { github: 39996 })
  let mounted = await pane($)
  let shown = lines(await mounted.drawn())
  expect(shown).toContain('work/CLAUDE.md is about 6k (est.), and Claude re-reads it with every request. Trimming it makes every reply cheaper.')
  expect(shown.filter(line => /every request/.test(line))).toHaveLength(1)
  await mounted.unmount()

  // The server reaches 10k: it is the larger of the two.
  await $.tool.describe({ tool: 'mcp__github__create_issue', description: text(40000), provider: GITHUB } as never)
  await world.clock.advance(1000)
  mounted = await pane($)
  shown = lines(await mounted.drawn())
  expect(shown).toContain('The github MCP server puts about 10k (est.) of tool descriptions in every request. If this work doesn\'t need it, turn it off.')
  expect(shown.filter(line => /every request/.test(line))).toHaveLength(1)
  await mounted.unmount()

  // Smaller files and a smaller server: nothing to say.
  await $.session.start(START)
  await loadStartup($, { github: 8000, files: [{ path: PROJECT_MD, kind: 'project', content: text(4000) }] })
  mounted = await pane($)
  shown = lines(await mounted.drawn())
  expect(shown.some(line => /every request/.test(line))).toBe(false)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Detailed lists everything in load order, grouped by kind', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  await $.skill.prompt({ skill: 'commit', text: text(2000) } as never)

  for (const surface of ['terminal', 'desktop'] as const) {
    const mounted = await pane($, surface)
    // The detailed choice persists, so only the first mount turns it on.
    if (surface === 'terminal') await mounted.press({ key: 'more' })
    const shown = lines(await mounted.drawn())
    const at = (line: string) => shown.indexOf(line)

    // Groups in the order their first item loaded: system prompt, instructions, session details, tools, skills.
    const order = ['System prompt', 'CLAUDE.md and rules', 'Session details', 'Built-in tools', 'MCP tools', 'Deferred tools', 'Skills'].map(at)
    expect(order.map(index => index >= 0)).toEqual([true, true, true, true, true, true, true])
    expect(at('System prompt')).toBeLessThan(at('CLAUDE.md and rules'))
    expect(at('CLAUDE.md and rules')).toBeLessThan(at('Session details'))
    expect(at('Session details')).toBeLessThan(at('Built-in tools'))
    expect(at('Built-in tools')).toBeLessThan(at('MCP tools'))
    expect(at('MCP tools')).toBeLessThan(at('Deferred tools'))
    expect(at('Deferred tools')).toBeLessThan(at('Skills'))

    // Within a group, in load order, and with every item: the files, the deferred server, the skill that ran.
    expect(shown.indexOf('.claude/CLAUDE.md · 1k (est.)')).toBeLessThan(shown.indexOf('work/CLAUDE.md · 6k (est.)'))
    expect(shown.indexOf('work/CLAUDE.md · 6k (est.)')).toBeLessThan(shown.indexOf('rules/style.md · 500 (est.)'))
    expect(shown).toContain('doing tasks · 1k (est.)')
    expect(shown).toContain('currentDate · 10 (est.)')
    expect(shown).toContain('docs (1 tool) · 5k (est.) · loads when asked for')
    expect(shown).toContain('Deferred tools (1) · 1k (est.) · loads when asked for')
    expect(shown).toContain('commit · 500 (est.)')
    // The simple list is replaced by this one.
    expect(shown).not.toContain('Largest items')
    await mounted.unmount()
  }
  expect(faults(world)).toEqual([])
})

test('a path-scoped file is marked with the reply it loaded in', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  await reply($, world, FIRST)
  await world.clock.advance(1000)

  // Reply 2: Claude reaches a path, the file loads, and its text arrives as an attachment.
  await $.classic.InstructionsLoaded({ file_path: SCOPED_MD, memory_type: 'Project', load_reason: 'path_glob_match', globs: ['src/**'] } as never)
  await $.prompt.attachment({ type: 'nested_memory', text: nested(8000), origin: { kind: 'engine' } } as never)
  await reply($, world, SECOND)
  await world.clock.advance(1000)

  const mounted = await pane($)
  // Simple: not part of the startup context, so not among the five largest.
  expect(lines(await mounted.drawn()).join('\n')).not.toMatch(/src\/CLAUDE\.md/)
  await mounted.press({ key: 'more' })
  const row = buttons(await mounted.drawn()).find(one => /^src\/CLAUDE\.md/.test(textOf(one)))
  expect(row).toBeDefined()
  expect(textOf(row)).toBe('src/CLAUDE.md · 2k (est.) · path-scoped, reply 2')
  // The startup total did not move.
  expect(lines(await mounted.drawn())).toContain('Startup context: about 28k (est.)')

  // Pressing it says what it is, where it came from, how big, and why it loaded.
  await mounted.press({ key: String(row?.props?.key) } as never)
  const shown = lines(await mounted.drawn())
  expect(shown).toContain(SCOPED_MD)
  expect(shown).toContain('Kind: CLAUDE.md and rules')
  expect(shown).toContain('From: Loaded when Claude reached matching paths')
  expect(shown).toContain('Size: about 2k (est.), 8,000 characters')
  expect(shown).toContain('Loaded in reply 2 because Claude reached a path matching src/**.')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('pressing a CLAUDE.md file shows where it came from and a dashboard link with a copy button', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)

  const mounted = await pane($)
  await mounted.press({ key: 'item:file:/work/claude.md' } as never)
  const tree = await mounted.drawn()
  const shown = lines(tree)
  expect(shown).toContain(PROJECT_MD)
  expect(shown).toContain('Kind: CLAUDE.md and rules')
  expect(shown).toContain("From: The project's instructions")
  expect(shown).toContain('Size: about 6k (est.), 24,000 characters')

  const link = elements(tree).find(one => one.type === 'Link')
  expect(link?.props?.href).toBe('http://localhost:8792/#/agents/context')
  expect(link?.props?.label).toBe('CLAUDE.md files in the dashboard')
  const copy = buttons(tree).find(one => one.props?.label === 'Copy link' && String(one.props?.key) === 'dashboard-copy')
  expect(copy?.props?.hotkey).toBe('y')
  await mounted.press({ key: 'dashboard-copy' })
  expect(world.copied).toContain('http://127.0.0.1:8792/#/agents/context')

  // A tool group shows no dashboard link, but names its largest tools.
  await mounted.press({ key: 'item:tools' } as never)
  const group = lines(await mounted.drawn())
  expect(group).toContain('Kind: Built-in tools')
  expect(group).toContain('Largest: Bash 3k, Read 2k')
  expect(elements(await mounted.drawn()).some(one => one.type === 'Link' && one.props?.label === 'CLAUDE.md files in the dashboard')).toBe(false)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('every Button has a hotkey and none is used twice', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)

  const mounted = await pane($)
  const check = async () => {
    const keys = buttons(await mounted.drawn()).map(one => String(one.props?.hotkey ?? ''))
    expect(keys.every(key => key !== '')).toBe(true)
    expect(new Set(keys).size).toBe(keys.length)
  }
  await check()
  await mounted.press({ key: 'item:file:/work/claude.md' } as never)
  await check()
  await mounted.press({ key: 'more' })
  await check()
  await mounted.press({ key: 'item:file:/work/claude.md' } as never)
  await check()
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('agent types that received CLAUDE.md are listed in the detailed view', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  const state = { count: 0 }
  on('agent.spawn', (_$, e) => {
    state.count += 1
    return { model: e.model ?? 'claude-haiku-4-5', agentId: `agent-${state.count}` } as never
  })
  on('agent.list', () => ({ value: [] }) as never)
  await $.session.start(START)
  await loadStartup($)
  await reply($, world, FIRST)

  const run = async (type: string, transcript: unknown[]): Promise<void> => {
    const answer = await $.agent.spawn({ tool_use_id: 'tu1', prompt: 'Look.', description: 'Look', subagentType: type, model: 'claude-haiku-4-5', parentModel: 'claude-opus-4-6', background: false, fork: false } as never)
    const id = String((answer as { agentId?: string }).agentId)
    await world.clock.advance(300)
    world.transcript = transcript
    await $.turn.complete({ turnId: `turn-${id}`, reason: 'answer', answer: 'done', isAborted: false, durationMs: 1000, agentId: id } as never)
    await world.clock.advance(1000)
  }
  await run('general-purpose', [{ role: 'user', content: [{ type: 'text', text: '<system-reminder>\n# claudeMd\nCodebase and user instructions are shown below.\n</system-reminder>' }, { type: 'text', text: 'Look.' }] }])
  await run('Explore', [{ role: 'user', content: 'Look.' }])

  const mounted = await pane($)
  // Simple stays short.
  expect(lines(await mounted.drawn()).join('\n')).not.toMatch(/Received it/)
  await mounted.press({ key: 'more' })
  const shown = lines(await mounted.drawn())
  expect(shown).toContain('Agents and CLAUDE.md')
  expect(shown).toContain('Received it: general-purpose')
  expect(shown).toContain('Did not receive it: Explore')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the detailed view sets the estimate beside Claude Code\'s own count and says why they differ', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  await reply($, world, FIRST)

  const mounted = await pane($)
  await mounted.press({ key: 'more' })
  const shown = lines(await mounted.drawn())
  // The support's /context gives 18k for the startup part.
  expect(shown).toContain('Claude Code counts 18k of startup context on the Now page. This list adds up to about 28k (est.).')
  expect(shown).toContain('The list estimates from text at a fixed rate. It leaves out the input schemas of tools, which are most of their size.')
  await mounted.unmount()
})

test('a new session starts the list over, and nothing about it is written to disk', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  await $.skill.prompt({ skill: 'commit', text: text(2000) } as never)
  await reply($, world, FIRST)
  await world.clock.advance(5000)

  // Nothing the mod keeps on disk names a file, a skill, a server or a section; none carries a size.
  const disk = [...world.files.entries()].map(([path, body]) => `${path}\n${body}`).join('\n')
  for (const word of ['CLAUDE.md', 'style.md', 'github', 'docs', 'commit', 'intro', 'doing_tasks', 'skill_listing', '/work', 'Instructions']) {
    expect(disk).not.toContain(word)
  }

  await $.session.start(START)
  const mounted = await pane($)
  expect(lines(await mounted.drawn())).toContain('Nothing has loaded yet. What Claude reads for this session shows here once it starts.')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

