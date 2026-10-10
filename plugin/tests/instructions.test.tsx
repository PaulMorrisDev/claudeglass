import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { elements, faults, prepare, reply, SITE, START, textOf } from './support'
import type { World } from './support'

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
  return $.ui.mount({ plugin: 'claudeglass', surface, component: 'Pane', requestId: 'glass-startup', props: PANE as never })
}

/** Turns the Detailed view on, as /glass detail does. */
async function detailed($: Engine): Promise<void> {
  await $.command.run({ command: 'glass', args: 'detail' } as never)
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

/** The lead sentence, which follows the page bar, and the line under it. */
function lead(shown: string[]): string[] {
  const at = shown.findIndex(line => line.startsWith('Every request starts'))
  return at < 0 ? [] : shown.slice(at, at + 2)
}

/** The lines of `shown` from `from` on, as many as `count`: a heading or a row is its name, then its figure. */
function run(shown: string[], from: string, count: number): string[] {
  const at = shown.indexOf(from)
  return at < 0 ? [] : shown.slice(at, at + count)
}

for (const surface of ['terminal', 'desktop'] as const) test(`Simple: the startup total in a sentence, one line of advice, and three groups of plain rows (${surface})`, async ($, on) => {
  const world = prepare(on)
  world.surfaces = [surface]
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)

  const mounted = await pane($, surface)
  const tree = await mounted.drawn()
  const shown = lines(tree)

  // 2k + 1k (system) + 1k + 6k + 500 (files) + 10 (date) + 5k (built-in tools) + 11k (github) + 1.5k (skill list), less the deferred tools.
  expect(lead(shown)[0]).toBe('Every request starts with about 28k tokens of setup (est.).')
  expect(lead(shown)[1]).toBe("The github tools add 11k to every request. Turn the server off in /mcp when you don't need it.")

  // You control: 11k + 5k + 7.5k + 1.5k. Claude Code's own is what is left of the total, in one line.
  // Two servers are listed: github, and docs behind tool search. Three built-in tools: Bash and Read, and NotebookEdit behind tool search.
  expect(run(shown, 'You control', 10)).toEqual(['You control', '25k', 'MCP servers (2)', '11k', 'Built-in tools (3)', '5k', 'CLAUDE.md and memory', '7.5k', 'Skills list', '1.5k'])
  // The desktop's note adds the servers the app brings; the terminal has none to tell apart.
  const note = surface === 'desktop' ? "Claude Code's built-in instructions and the servers the desktop app brings. You can't change these." : "Claude Code's built-in instructions. You can't change these."
  expect(run(shown, "Claude Code's own", 3)).toEqual(["Claude Code's own", '3.01k', note])
  expect(shown).toContain('Sizes are estimates, shared out by size of text.')
  // Nothing loaded after the start, so there is no third group; and no section, tool or file is named, nor the advice on dropping a tool.
  expect(shown).not.toContain('Added as you work')
  expect(shown.join('\n')).not.toMatch(/intro|doing|currentDate|skill_listing|Deferred|docs|Bash|Read|NotebookEdit|permissions\.deny|CLAUDE\.md ·|path-scoped/)
  // "(est.)" is said once.
  expect(shown.filter(line => line.includes('(est.)'))).toHaveLength(1)

  // The only Button is the pane's quiet "What these mean": rows are text, and nothing here has a key of its own.
  expect(buttons(tree).map(button => String(button.props?.key))).toEqual(['help'])
  for (const button of buttons(tree)) {
    expect(button.props?.hotkey).toBeUndefined()
    expect(button.props?.variant).toBeUndefined()
  }
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Startup is its own pane, glass-startup: it holds no tab bar and no hotkey, and Checkpoint is not drawn in it', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  const mounted = await pane($)
  const tree = await mounted.drawn()
  expect(buttons(tree).some(one => one.props?.hotkey !== undefined || String(one.props?.key).startsWith('page:'))).toBe(false)
  expect(lines(tree)).not.toContain('Keep through the next summary')
  expect(lines(tree)).not.toContain('Open Startup')
  await mounted.unmount()

  // The pane named glass is Checkpoint, and it shows no startup rows.
  const checkpoint = await $.ui.mount({ plugin: 'claudeglass', surface: 'terminal', component: 'Pane', requestId: 'glass', props: PANE as never })
  expect(lines(await checkpoint.drawn())).not.toContain('Built-in tools (3)')
  await checkpoint.unmount()
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
  expect(shown).toContain('work/CLAUDE.md adds 6k to every request. Trim it if parts are out of date.')
  expect(shown.filter(line => /to every request/.test(line))).toHaveLength(1)
  await mounted.unmount()

  // The server reaches 10k: it is the larger of the two.
  await $.tool.describe({ tool: 'mcp__github__create_issue', description: text(40000), provider: GITHUB } as never)
  await world.clock.advance(1000)
  mounted = await pane($)
  shown = lines(await mounted.drawn())
  expect(shown).toContain("The github tools add 10k to every request. Turn the server off in /mcp when you don't need it.")
  expect(shown.filter(line => /to every request/.test(line))).toHaveLength(1)
  await mounted.unmount()

  // Smaller files and a smaller server: nothing to say.
  await $.session.start(START)
  await loadStartup($, { github: 8000, files: [{ path: PROJECT_MD, kind: 'project', content: text(4000) }] })
  mounted = await pane($)
  shown = lines(await mounted.drawn())
  expect(shown.some(line => /to every request/.test(line))).toBe(false)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a server is named as /mcp lists it, and one with only an id is a connector named by its tools', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  const ID = '5f3a2c1e-9b4d-4e8a-8c3d-1a2b3c4d5e6f'
  for (const tool of ['batch', 'guide', 'update', 'query']) {
    await $.tool.describe({ tool: `mcp__${ID}__${tool}`, description: text(2000), provider: { plugin: `mcp:${ID}`, tier: 'user' } } as never)
  }
  await $.tool.describe({ tool: 'mcp__plugin_playwright_playwright__click', description: text(1000), provider: { plugin: 'mcp:plugin_playwright_playwright', tier: 'user' } } as never)
  // /mcp lists a plugin's server as plugin:<plugin>:<server>.
  world.shape = {
    mcpTools: [
      { name: 'mcp__github__create_issue', serverName: 'GitHub' },
      { name: 'mcp__plugin_playwright_playwright__click', serverName: 'plugin:playwright:playwright' },
    ],
  }
  await reply($, world, FIRST)
  await world.clock.advance(1000)

  const mounted = await pane($)
  await detailed($)
  const shown = lines(await mounted.drawn())
  // Largest first, each by its name, never its id.
  // Four are listed: github, the connector, playwright, and docs behind tool search. Each server's tools follow it, by their short names.
  // Claude Code counts the github and playwright tools; the connector's are not in its count, so they share the connector's 2k by text.
  expect(run(shown, 'MCP servers (4)', 21)).toEqual([
    'MCP servers (4)',
    '13.3k',
    'GitHub',
    '11k',
    'create issue',
    '11k',
    'Connector (batch, guide, update, …)',
    '2k',
    'batch',
    '500',
    'guide',
    '500',
    'update',
    '500',
    'query',
    '500',
    'playwright',
    '250',
    'click',
    '250',
    '1 more server loads only when asked for: docs',
  ])
  expect(shown.join('\n')).not.toContain(ID)
  expect(shown.join('\n')).not.toMatch(/mcp__|plugin[_:]/)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test("the Desktop app's own servers are part of Claude Code's own, not a server to turn off", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($, { github: 8000, files: [{ path: PROJECT_MD, kind: 'project', content: text(4000) }] })
  await $.tool.describe({ tool: 'mcp__ccd_session__spawn', description: text(120000), provider: { plugin: 'mcp:ccd_session', tier: 'user' } } as never)

  const mounted = await pane($)
  const shown = lines(await mounted.drawn())
  expect(shown.join('\n')).not.toMatch(/ccd/)
  expect(shown.some(line => /to every request/.test(line))).toBe(false)
  // It still counts in what every request starts with, and the github server is the one server listed.
  expect(lead(shown)[0]).toBe('Every request starts with about 42.5k tokens of setup (est.).')
  expect(shown).toContain('MCP servers (2)')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

for (const surface of ['terminal', 'desktop'] as const) test(`Detailed lists each server and file under its row, and none of Claude Code's own sections (${surface})`, async ($, on) => {
  const world = prepare(on)
  world.surfaces = [surface]
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  await $.skill.prompt({ skill: 'commit', text: text(2000) } as never)

  const mounted = await pane($, surface)
  await detailed($)
  const shown = lines(await mounted.drawn())

  // Servers largest first, the one that waits behind tool search last and without a size.
  expect(run(shown, 'MCP servers (2)', 7)).toEqual(['MCP servers (2)', '11k', 'github', '11k', 'create issue', '11k', '1 more server loads only when asked for: docs'])
  // Files in the order they loaded, shortened as the page always has.
  expect(run(shown, 'CLAUDE.md and memory', 8)).toEqual(['CLAUDE.md and memory', '7.5k', '.claude/CLAUDE.md', '1k', 'work/CLAUDE.md', '6k', 'rules/style.md', '500'])
  // The skill that ran loaded after the start: it is in the third group, as one line.
  expect(run(shown, 'Added as you work', 5)).toEqual(['Added as you work', '500', '/clear and /compact empty these.', 'Skills Claude used', '500'])
  expect(shown.join('\n')).not.toMatch(/intro|doing|currentDate|skill_listing|Deferred|System prompt|mcp__/)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a path-scoped file is in the third group and does not move the startup total', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  // Claude Code counts no startup rows here, so every size on the page is the estimate.
  world.shape = { startupRows: [] }
  await reply($, world, FIRST)
  await world.clock.advance(1000)

  // Reply 2: Claude reaches a path, the file loads, and its text arrives as an attachment.
  await $.classic.InstructionsLoaded({ file_path: SCOPED_MD, memory_type: 'Project', load_reason: 'path_glob_match', globs: ['src/**'] } as never)
  await $.prompt.attachment({ type: 'nested_memory', text: nested(8000), origin: { kind: 'engine' } } as never)
  await reply($, world, SECOND)
  await world.clock.advance(1000)

  const mounted = await pane($)
  let shown = lines(await mounted.drawn())
  expect(lead(shown)[0]).toBe('Every request starts with about 28k tokens of setup (est.).')
  expect(run(shown, 'Added as you work', 5)).toEqual(['Added as you work', '2k', '/clear and /compact empty these.', 'Files loaded as Claude reached them', '2k'])
  // The file itself waits for Detailed, marked as loading later.
  expect(shown.join('\n')).not.toMatch(/src\/CLAUDE\.md/)
  await detailed($)
  shown = lines(await mounted.drawn())
  expect(run(shown, 'Files loaded as Claude reached them', 3)).toEqual(['Files loaded as Claude reached them', '2k', 'src/CLAUDE.md (loads later)'])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test("Detailed has no dashboard link of its own: the footer's goes to the CLAUDE.md files, and no row is pressable", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)

  await detailed($)
  const mounted = await pane($)
  const tree = await mounted.drawn()
  const links = elements(tree).filter(one => one.type === 'Link')
  expect(links).toHaveLength(1)
  expect(links[0]?.props?.href).toBe('http://localhost:8792/#/agents/context')
  expect(buttons(tree).some(one => String(one.props?.key) === 'dashboard-copy')).toBe(false)

  // Rows are text: the only Button is the footer's, and it has no key.
  const keys = buttons(tree).filter(one => one.props?.hotkey !== undefined).map(one => String(one.props?.key))
  expect(keys).toEqual([])
  expect(buttons(tree).some(one => /^(item|row|explain)/.test(String(one.props?.key)))).toBe(false)
  expect(buttons(tree).some(one => one.props?.variant === 'primary')).toBe(false)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a long name keeps its figure on one line: the name is cut in a Box that may shrink, the figure sits in one that may not', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  const long = 'x'.repeat(100)
  await $.tool.describe({ tool: `mcp__${long}__go`, description: text(2000), provider: { plugin: `mcp:${long}`, tier: 'user' } } as never)

  const mounted = await pane($)
  await detailed($)
  const tree = await mounted.drawn()
  const row = elements(tree).find(one => one.type === 'Box' && String(one.props?.key).startsWith('item:mcp:') && textOf(one).includes(long))
  expect(row).toBeDefined()
  const [name, figure] = (row?.children ?? []) as { props?: Record<string, unknown> }[]
  expect(name?.props?.minWidth).toBe(0)
  expect(name?.props?.flexShrink).toBe(1)
  expect(figure?.props?.flexShrink).toBe(0)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the built-in tools list the ten largest, then one dim row with the rest counted and summed', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  // Twelve more tools of 400 to 4400 characters: with Bash and Read, fourteen are in the prompt.
  for (let n = 1; n <= 12; n += 1) await $.tool.describe({ tool: `Tool${n}`, description: text(400 * n), provider: ENGINE } as never)

  const mounted = await pane($)
  await detailed($)
  const tree = await mounted.drawn()
  const shown = lines(tree)
  const at = shown.indexOf('Built-in tools (15)')
  expect(at).toBeGreaterThanOrEqual(0)
  const rows = elements(tree).filter(one => one.type === 'Box' && /^item:tools:\d+$/.test(String(one.props?.key)))
  expect(rows).toHaveLength(10)
  // The smallest four of the fourteen (Tool1 to Tool4) are the "more" row, dim, with their sum as its figure.
  expect(shown).not.toContain('Tool1')
  expect(shown).toContain('Tool12')
  const more = elements(tree).find(one => one.type === 'Box' && one.props?.key === 'item:tools:more')
  expect(textOf(more)).toMatch(/^4 more\d/)
  expect(elements(more).find(one => one.type === 'Text' && textOf(one) === '4 more')?.props?.dimColor).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('tools behind tool search are one dim line, cut to the row width with an ellipsis', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  for (let n = 1; n <= 8; n += 1) await $.tool.describe({ tool: `AVeryLongDeferredToolNameThatGoesOnAndOn${n}`, description: text(400), isDeferred: true, provider: ENGINE } as never)

  const mounted = await pane($)
  await detailed($)
  const shown = lines(await mounted.drawn())
  const line = shown.find(text => /^9 more load only when asked for: /.test(text))
  expect(line).toBeDefined()
  expect(String(line).length).toBeLessThanOrEqual(82)
  // The long names do not fit the row and none is cut: the one short name fits whole, then the ellipsis.
  expect(line).toBe('9 more load only when asked for: NotebookEdit, …')
  expect(String(line)).not.toMatch(/AVery/)
  // No row for any of them.
  expect(shown.filter(text => /^AVeryLong/.test(text))).toEqual([])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the tool search line names as many whole tools as the row holds and ends with ", …" for the rest; never a cut inside a name', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  // Larger than NotebookEdit's 4000 characters, so they come first and in this order.
  for (const [at, letter] of ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].entries()) await $.tool.describe({ tool: `Deferred${letter}`, description: text(9000 - at * 100), isDeferred: true, provider: ENGINE } as never)

  const mounted = await pane($)
  await detailed($)
  const waiting = lines(await mounted.drawn()).find(line => /^9 more load only when asked for: /.test(line))
  // A fifth name would run past the line's width, so four names and the ellipsis.
  expect(waiting).toBe('9 more load only when asked for: DeferredA, DeferredB, DeferredC, DeferredD, …')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('the servers line fits whole names too', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  for (const name of ['alphaserver', 'bravoserver', 'charlieserver', 'deltaserver']) {
    await $.tool.describe({ tool: `mcp__${name}__go`, description: text(400), isDeferred: true, provider: { plugin: `mcp:${name}`, tier: 'user' } } as never)
  }

  const mounted = await pane($)
  await detailed($)
  const waiting = lines(await mounted.drawn()).find(line => /^\d+ more servers? load/.test(line))
  expect(waiting).toBeDefined()
  expect(String(waiting).length).toBeLessThanOrEqual(82)
  // Every name in it is whole, and the rest are the ellipsis.
  const names = String(waiting).split(': ')[1]?.split(', ') ?? []
  expect(names.at(-1)).toBe('…')
  for (const name of names.slice(0, -1)) expect(['alphaserver', 'bravoserver', 'charlieserver', 'deltaserver', 'docs']).toContain(name)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

/** The startup of the desktop tests: github, one file, and the Desktop app's browser server (15k). */
async function loadWithBrowser($: Engine): Promise<void> {
  await loadStartup($, { github: 8000, files: [{ path: PROJECT_MD, kind: 'project', content: text(4000) }] })
  await $.tool.describe({ tool: 'mcp__Claude_Browser__navigate', description: text(60000), provider: { plugin: 'mcp:Claude_Browser', tier: 'user' } } as never)
}

/** The first number of a row's figure, in thousands ("3.01k" is 3.01, "500" is 0.5). */
function thousands(figure: string | undefined): number {
  const value = Number.parseFloat(String(figure))
  return String(figure).endsWith('k') ? value : value / 1000
}

test("on the terminal the servers the app brings stay in You control, named and advised on", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadWithBrowser($)

  const terminal = await pane($, 'terminal')
  await detailed($)
  const shown = lines(await terminal.drawn())
  expect(shown).toContain('MCP servers (3)')
  expect(shown).toContain('Claude Browser')
  expect(shown.indexOf('Claude Browser')).toBeLessThan(shown.indexOf("Claude Code's own"))
  expect(shown.some(line => /^The Claude Browser tools add 15k to every request/.test(line))).toBe(true)
  // Claude Code's own lists nothing under it.
  expect(shown.filter(line => line === 'Instructions and system prompt')).toEqual([])
  await terminal.unmount()
  expect(faults(world)).toEqual([])
})

test("in the Desktop app the servers it brings count as Claude Code's own: listed under it by the label the MCP rows use, led by Claude Code's instructions so the rows add up", async ($, on) => {
  const world = prepare(on)
  world.surfaces = ['desktop']
  bottoms(on)
  await $.session.start(START)
  await loadWithBrowser($)

  const desktop = await pane($, 'desktop')
  await detailed($)
  const shown = lines(await desktop.drawn())
  expect(shown).toContain('MCP servers (2)')
  // Listed under Claude Code's own by name and size, and not advised against.
  const own = shown.indexOf("Claude Code's own")
  expect(shown.indexOf('Claude Browser')).toBeGreaterThan(own)
  expect(shown.some(line => /Turn the server off/.test(line))).toBe(false)
  // The first row is Claude Code's instructions with the rest of the group, so the rows add up to the group.
  expect(shown[own + 3]).toBe('Instructions and system prompt')
  expect(shown[own + 5]).toBe('Claude Browser')
  expect(shown[own + 6]).toBe('15k')
  expect(Math.abs(thousands(shown[own + 4]) + thousands(shown[own + 6]) - thousands(shown[own + 1]))).toBeLessThan(0.06)
  // The app's servers are in the total: 27.5k of setup, the same as with the browser server on the terminal.
  expect(lead(shown)[0]).toBe('Every request starts with about 27.5k tokens of setup (est.).')
  await desktop.unmount()
  expect(faults(world)).toEqual([])
})

test('a server /mcp names by its wire name reads as words, in the own rows and the MCP rows alike', async ($, on) => {
  const world = prepare(on)
  world.surfaces = ['desktop']
  world.shape = { mcpTools: [{ name: 'mcp__Claude_Browser__navigate', serverName: 'Claude_Browser' }, { name: 'mcp__github__create_issue', serverName: 'github' }] }
  bottoms(on)
  await $.session.start(START)
  await loadWithBrowser($)
  world.figures(1000, 0)

  const mounted = await pane($, 'desktop')
  await detailed($)
  const shown = lines(await mounted.drawn())
  expect(shown.filter(line => line.includes('Claude_Browser'))).toEqual([])
  expect(shown.indexOf('Claude Browser')).toBeGreaterThan(shown.indexOf("Claude Code's own"))
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test("with no server from the app, Claude Code's own has no rows under it on the desktop either", async ($, on) => {
  const world = prepare(on)
  world.surfaces = ['desktop']
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  await $.skill.prompt({ skill: 'commit', text: text(2000) } as never)

  const desktop = await pane($, 'desktop')
  await detailed($)
  const shown = lines(await desktop.drawn())
  const own = shown.indexOf("Claude Code's own")
  expect(shown.filter(line => line === 'Instructions and system prompt')).toEqual([])
  // The heading, its figure and its note, then the next group.
  expect(shown[own + 3]).toBe('Added as you work')
  await desktop.unmount()
  expect(faults(world)).toEqual([])
})

for (const surface of ['vscode', 'mobile']) test(`only the Desktop app brings those servers: on ${surface} they stay in You control`, async ($, on) => {
  const world = prepare(on)
  world.surfaces = [surface]
  bottoms(on)
  await $.session.start(START)
  await loadWithBrowser($)
  // Whatever the pane is drawn on, the surface the session runs on decides.
  const mounted = await pane($, 'desktop')
  const shown = lines(await mounted.drawn())
  expect(shown).toContain('MCP servers (3)')
  expect(shown.indexOf('Claude Browser')).toBeLessThan(shown.indexOf("Claude Code's own"))
  expect(shown.some(line => /^The Claude Browser tools add 15k to every request/.test(line))).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

/** Replies of the main session, each a little past the last. */
async function replies($: Engine, world: World, count: number): Promise<void> {
  for (let n = 0; n < count; n += 1) {
    await reply($, world, n === 0 ? FIRST : { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200 }], usd: 0.05 })
    await world.clock.advance(1000)
  }
}

test('the advice can be a built-in tool in the prompt that Claude has not called this session', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  // 60000 characters is 15k tokens: larger than the github server's 11k.
  await $.tool.describe({ tool: 'Monitor', description: text(60000), provider: ENGINE } as never)
  await replies($, world, 3)

  let mounted = await pane($)
  let shown = lines(await mounted.drawn())
  expect(shown).toContain("The Monitor tool adds 15k to every request and Claude hasn't used it this session. If you never use it, add it to permissions.deny.")
  expect(shown.filter(line => /to every request/.test(line))).toHaveLength(1)
  await mounted.unmount()

  // Once Claude has called it, the next largest fixable item is advised instead.
  await reply($, world, { steps: [{ cacheRead: 120000, cacheWrite: 1000, output: 200, tools: ['Monitor'] }], usd: 0.05 })
  await world.clock.advance(1000)
  mounted = await pane($)
  shown = lines(await mounted.drawn())
  expect(shown.some(line => /^The Monitor tool/.test(line))).toBe(false)
  expect(shown).toContain("The github tools add 11k to every request. Turn the server off in /mcp when you don't need it.")
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a built-in tool is not advised against before Claude has had three replies to use it', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  await $.tool.describe({ tool: 'Monitor', description: text(60000), provider: ENGINE } as never)

  // A fresh session, and a resumed one, have called nothing yet: the server is advised, the tool is not.
  for (const count of [0, 1, 2]) {
    if (count > 0) await replies($, world, 1)
    const mounted = await pane($)
    const shown = lines(await mounted.drawn())
    expect(shown.some(line => /^The Monitor tool/.test(line))).toBe(false)
    expect(shown).toContain("The github tools add 11k to every request. Turn the server off in /mcp when you don't need it.")
    await mounted.unmount()
  }
  await replies($, world, 1)
  const mounted = await pane($)
  expect(lines(await mounted.drawn()).some(line => /^The Monitor tool adds 15k/.test(line))).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('a tool a subagent called counts as used', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  on('agent.spawn', (_$, e) => ({ model: e.model ?? 'claude-haiku-4-5', agentId: 'agent-1' }) as never)
  on('agent.list', () => ({ value: [] }) as never)
  on('tool.call', () => ({ result: {} }) as never)
  await $.session.start(START)
  await loadStartup($)
  await $.tool.describe({ tool: 'Monitor', description: text(60000), provider: ENGINE } as never)
  await replies($, world, 3)
  await $.agent.spawn({ tool_use_id: 'tu1', prompt: 'Watch.', description: 'Watch', subagentType: 'general-purpose', model: 'claude-haiku-4-5', parentModel: 'claude-opus-4-6', background: false, fork: false } as never)
  await world.clock.advance(300)

  let mounted = await pane($)
  expect(lines(await mounted.drawn()).some(line => /^The Monitor tool adds 15k/.test(line))).toBe(true)
  await mounted.unmount()

  await $.tool.call({ tool: 'Monitor', agentId: 'agent-1', command: 'x' } as never)
  await world.clock.advance(1000)
  mounted = await pane($)
  const shown = lines(await mounted.drawn())
  expect(shown.some(line => /^The Monitor tool/.test(line))).toBe(false)
  expect(shown).toContain("The github tools add 11k to every request. Turn the server off in /mcp when you don't need it.")
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
  await detailed($)
  const shown = lines(await mounted.drawn())
  expect(shown).toContain('Agents and CLAUDE.md')
  expect(shown).toContain('Received it: general-purpose')
  expect(shown).toContain('Did not receive it: Explore')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test("with Claude Code's own count the total is its figure, and each row is its share", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  // What /context counts for this startup: the rows are shared out among the items that belong to them.
  world.shape = {
    startupRows: [
      { name: 'System prompt', tokens: 4000 },
      { name: 'Memory files', tokens: 9000 },
      { name: 'System tools', tokens: 7000 },
      { name: 'MCP tools', tokens: 15000 },
      { name: 'Skills', tokens: 2000 },
    ],
  }
  await reply($, world, FIRST)

  let mounted = await pane($)
  let shown = lines(await mounted.drawn())
  // The total is Claude Code's, so the lead carries no estimate mark and no "about"; the rows are shares, said once in the footnote.
  expect(lead(shown)[0]).toBe('Every request starts with 37k tokens of setup.')
  expect(lead(shown)[1]).toBe("The github tools add 15k to every request. Turn the server off in /mcp when you don't need it.")
  // You control: 15k + 7k + 9k + 2k; Claude Code's own is the rest of the 37k.
  expect(run(shown, 'You control', 10)).toEqual(['You control', '33k', 'MCP servers (2)', '15k', 'Built-in tools (3)', '7k', 'CLAUDE.md and memory', '9k', 'Skills list', '2k'])
  expect(run(shown, "Claude Code's own", 2)).toEqual(["Claude Code's own", '4k'])
  expect(shown).toContain('Sizes are estimates, shared out by size of text.')
  expect(shown.some(line => line.includes('(est.)'))).toBe(false)
  await mounted.unmount()

  // Detailed: the groups add up to the total, so there is no second figure to explain.
  mounted = await pane($)
  await detailed($)
  shown = lines(await mounted.drawn())
  expect(shown.some(line => /^Claude Code counts/.test(line))).toBe(false)
  expect(run(shown, 'CLAUDE.md and memory', 8)).toEqual(['CLAUDE.md and memory', '9k', '.claude/CLAUDE.md', '1.2k', 'work/CLAUDE.md', '7.2k', 'rules/style.md', '600'])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test("when none of Claude Code's rows fits what loaded, the estimate stands beside its count and says why they differ", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  world.shape = { startupRows: [{ name: 'Slash commands', tokens: 18000 }] }
  await reply($, world, FIRST)

  const mounted = await pane($)
  await detailed($)
  const shown = lines(await mounted.drawn())
  expect(shown).toContain('Claude Code counts 18k of startup context on the Now page. This list adds up to about 28k.')
  expect(shown).toContain('The list estimates from text at a fixed rate. It leaves out the input schemas of tools, which are most of their size.')
  expect(lead(shown)[0]).toBe('Every request starts with about 28k tokens of setup (est.).')
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test("a count too small for the rows it left out gives way to the estimate, so the lead is what the groups add up to", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  // Claude Code counts the system prompt alone: the servers, files and skills it left out keep their estimates, which it can't hold.
  world.shape = { startupRows: [{ name: 'System prompt', tokens: 3000 }] }
  await reply($, world, FIRST)

  const mounted = await pane($)
  let shown = lines(await mounted.drawn())
  expect(lead(shown)[0]).toMatch(/^Every request starts with about [\d.]+k tokens of setup \(est\.\)\.$/)
  const figure = (line: string | undefined) => Number(String(line).replace(/k$/, '')) * (String(line).endsWith('k') ? 1000 : 1)
  const total = Number(/about ([\d.]+)k/.exec(lead(shown)[0] ?? '')?.[1]) * 1000
  const groups = figure(run(shown, 'You control', 2)[1]) + figure(run(shown, "Claude Code's own", 2)[1])
  expect(Math.abs(total - groups)).toBeLessThanOrEqual(1000)
  await detailed($)
  shown = lines(await mounted.drawn())
  expect(shown.some(line => line.startsWith('Claude Code counts 3k of startup context on the Now page.'))).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('before Claude Code has counted anything the page shows the estimate alone', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  const mounted = await pane($)
  await detailed($)
  const shown = lines(await mounted.drawn())
  expect(lead(shown)[0]).toBe('Every request starts with about 28k tokens of setup (est.).')
  expect(shown.some(line => /^Claude Code counts/.test(line))).toBe(false)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test("the instructions your servers gave Claude at the start are part of the MCP servers row, not Claude Code's own", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($, { github: 8000, files: [{ path: PROJECT_MD, kind: 'project', content: text(4000) }] })
  await $.prompt.attachment({ type: 'mcp_instructions_delta', text: text(8000), origin: { kind: 'engine' } } as never)

  const mounted = await pane($)
  await detailed($)
  const shown = lines(await mounted.drawn())
  // github 2k + the servers' instructions 2k, the built-in tools 5k; Claude Code's own stays the system prompt (3k + 10).
  expect(run(shown, 'You control', 4)).toEqual(['You control', '11.5k', 'MCP servers (2)', '4k'])
  expect(run(shown, 'Built-in tools (3)', 2)).toEqual(['Built-in tools (3)', '5k'])
  expect(run(shown, "Claude Code's own", 2)).toEqual(["Claude Code's own", '3.01k'])
  expect(run(shown, 'MCP servers (2)', 9)).toEqual(['MCP servers (2)', '4k', 'github', '2k', 'create issue', '2k', 'Instructions the servers gave Claude', '2k', '1 more server loads only when asked for: docs'])
  expect(shown.some(line => /mcp_instructions/.test(line))).toBe(false)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test('Detailed lists each built-in tool under its row, largest first, those behind tool search after and dim, and says how to drop one', async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)

  const mounted = await pane($)
  let tree = await mounted.drawn()
  // Simple: the row alone, with no tool named and no advice.
  expect(run(lines(tree), 'Built-in tools (3)', 2)).toEqual(['Built-in tools (3)', '5k'])
  expect(lines(tree).some(line => /Bash|NotebookEdit|permissions\.deny/.test(line))).toBe(false)

  await detailed($)
  tree = await mounted.drawn()
  const shown = lines(tree)
  // Bash is 12000 characters and Read 8000, so they share the row's 5k as 3k and 2k; NotebookEdit waits behind tool search and costs nothing, so it is one dim line.
  const waitingLine = '1 more loads only when asked for: NotebookEdit'
  expect(run(shown, 'Built-in tools (3)', 7)).toEqual(['Built-in tools (3)', '5k', 'Bash', '3k', 'Read', '2k', waitingLine])
  const waiting = elements(tree).find(one => one.type === 'Text' && textOf(one) === waitingLine)
  expect(waiting?.props?.dimColor).toBe(true)
  const live = elements(tree).find(one => one.type === 'Text' && textOf(one) === 'Bash')
  expect(live?.props?.dimColor).toBeFalsy()
  // The advice is one dim line under the list, said once.
  const advice = 'To drop a tool you never use, add its name to permissions.deny in your settings. It takes effect in your next session.'
  expect(shown.filter(line => line === advice)).toHaveLength(1)
  expect(shown.indexOf(advice)).toBe(shown.indexOf(waitingLine) + 1)
  expect(elements(tree).find(one => one.type === 'Text' && textOf(one) === advice)?.props?.dimColor).toBe(true)
  // The tools are text rows, not controls.
  expect(buttons(tree).some(one => /tool/.test(String(one.props?.key)))).toBe(false)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test("with Claude Code's count each built-in tool is its share of the row, and the shares add up to it", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  // 701 does not split evenly by 12000 and 8000 characters: 420.6 and 280.4 would come to 420 and 280, so the spare token goes to Bash.
  world.shape = {
    startupRows: [
      { name: 'System prompt', tokens: 4000 },
      { name: 'System tools', tokens: 701 },
    ],
  }
  await reply($, world, FIRST)

  const mounted = await pane($)
  await detailed($)
  const shown = lines(await mounted.drawn())
  expect(run(shown, 'Built-in tools (3)', 6)).toEqual(['Built-in tools (3)', '701', 'Bash', '421', 'Read', '280'])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test("a count with no row for the built-in tools does not cover them: the estimate stands and Claude Code's own is not left at nothing", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($, { github: 8000 })
  // Claude Code counts 17.5k, which is exactly the rows You control would hold (github 2k, the tools' estimate 5k, files 9k, the skill list 1.5k).
  // Were the tools' estimate taken out of that total, Claude Code's own would come out as nothing.
  world.shape = {
    startupRows: [
      { name: 'System prompt', tokens: 8500 },
      { name: 'Memory files', tokens: 9000 },
    ],
  }
  await reply($, world, FIRST)

  const mounted = await pane($)
  let shown = lines(await mounted.drawn())
  expect(lead(shown)[0]).toMatch(/^Every request starts with about [\d.]+k tokens of setup \(est\.\)\.$/)
  expect(run(shown, 'Built-in tools (3)', 2)).toEqual(['Built-in tools (3)', '5k'])
  expect(run(shown, "Claude Code's own", 2)).toEqual(["Claude Code's own", '8.5k'])
  await detailed($)
  shown = lines(await mounted.drawn())
  expect(shown.some(line => /^Claude Code counts 17\.5k of startup context on the Now page\./.test(line))).toBe(true)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test("Claude Code's own is what is left once the built-in tools are in You control, and they are not named there", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  // Only built-in tools and the system prompt: the tools are all of You control.
  await $.prompt.compose(COMPOSE as never)
  await $.tool.describe({ tool: 'Bash', description: text(12000), provider: ENGINE } as never)
  const mounted = await pane($)
  const shown = lines(await mounted.drawn())
  expect(run(shown, 'You control', 4)).toEqual(['You control', '3k', 'Built-in tools (1)', '3k'])
  expect(run(shown, "Claude Code's own", 3)).toEqual(["Claude Code's own", '3k', "Claude Code's built-in instructions. You can't change these."])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

test("Detailed lists an MCP server's tools under it by the sizes Claude Code gives, five at most and then a count", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  await $.session.start(START)
  await loadStartup($)
  for (const tool of ['list_issues', 'get_issue', 'add_comment', 'search_code', 'get_pull_request']) {
    await $.tool.describe({ tool: `mcp__github__${tool}`, description: text(4000), provider: GITHUB } as never)
  }
  // Claude Code counts the github tools at 12k together; the docs server waits behind tool search and has no tool listed.
  world.shape = {
    startupRows: [
      { name: 'System prompt', tokens: 4000 },
      { name: 'MCP tools', tokens: 12000 },
    ],
    mcpTools: [
      { name: 'mcp__github__create_issue', serverName: 'GitHub', tokens: 6000 },
      { name: 'mcp__github__list_issues', serverName: 'GitHub', tokens: 3000 },
      { name: 'mcp__github__get_issue', serverName: 'GitHub', tokens: 1500 },
      { name: 'mcp__github__add_comment', serverName: 'GitHub', tokens: 900 },
      { name: 'mcp__github__search_code', serverName: 'GitHub', tokens: 450 },
      { name: 'mcp__github__get_pull_request', serverName: 'GitHub', tokens: 150 },
      { name: 'mcp__docs__search', serverName: 'docs', tokens: 777, isLoaded: false },
    ],
  }
  await reply($, world, FIRST)

  const mounted = await pane($)
  // Simple: servers only.
  expect(lines(await mounted.drawn()).join('\n')).not.toMatch(/create issue|and 1 more/)
  await detailed($)
  const tree = await mounted.drawn()
  const shown = lines(tree)
  expect(run(shown, 'MCP servers (2)', 16)).toEqual([
    'MCP servers (2)',
    '12k',
    'GitHub',
    '12k',
    'create issue',
    '6k',
    'list issues',
    '3k',
    'get issue',
    '1.5k',
    'add comment',
    '900',
    'search code',
    '450',
    'and 1 more',
    '1 more server loads only when asked for: docs',
  ])
  expect(shown).not.toContain('get pull request')
  expect(elements(tree).find(one => one.type === 'Text' && textOf(one) === 'and 1 more')?.props?.dimColor).toBe(true)
  // The server waiting behind tool search shows no tools; nothing shows a wire name.
  expect(shown.join('\n')).not.toMatch(/mcp__|github__|docs__/)
  await mounted.unmount()
  expect(faults(world)).toEqual([])
})

/** What a summary of the main conversation answers: the new messages. */
function summarising(on: On, answer: { skip: boolean }): void {
  on('session.compact', () => (answer.skip ? { skip: 'Not now.' } : { messages: [{ role: 'user', text: 'Summary of the conversation.', toolUses: [] }], tokensBefore: 150000, tokensAfter: 30000 }) as never)
}

const SAID = [{ role: 'user', text: 'Thanks.', toolUses: [] }]

test("files and hook notes added to your messages are apart from Claude Code's reminders, and a summary empties the third group", async ($, on) => {
  const world = prepare(on)
  bottoms(on)
  const answer = { skip: false }
  summarising(on, answer)
  await $.session.start(START)
  await loadStartup($)
  await reply($, world, FIRST)
  await world.clock.advance(1000)

  // Reply 2: a reminder from Claude Code, a file the person @-mentioned, a settings hook's note, a skill, a scoped CLAUDE.md.
  await $.prompt.attachment({ type: 'todo_reminder', text: text(400), origin: { kind: 'engine' } } as never)
  await $.prompt.attachment({ type: 'file', text: text(2000), origin: { kind: 'engine' } } as never)
  await $.prompt.attachment({ type: 'hook_additional_context', text: text(800), origin: { kind: 'hook', event: 'UserPromptSubmit' } } as never)
  await $.skill.prompt({ skill: 'commit', text: text(2000) } as never)
  await $.classic.InstructionsLoaded({ file_path: SCOPED_MD, memory_type: 'Project', load_reason: 'path_glob_match', globs: ['src/**'] } as never)
  await $.prompt.attachment({ type: 'nested_memory', text: nested(8000), origin: { kind: 'engine' } } as never)
  await reply($, world, SECOND)
  await world.clock.advance(1000)

  let mounted = await pane($)
  let shown = lines(await mounted.drawn())
  const total = lead(shown)[0]
  // Reminders are one row; the file and the hook's note are another. Neither is called a reminder when it is not.
  expect(run(shown, 'Added as you work', 9)).toEqual([
    'Added as you work',
    '3.3k',
    '/clear and /compact empty these.',
    'Files loaded as Claude reached them',
    '2k',
    'Skills Claude used',
    '500',
    'Reminders from Claude Code',
    '100',
  ])
  expect(run(shown, 'Files and hook notes added to your messages', 2)).toEqual(['Files and hook notes added to your messages', '700'])
  await mounted.unmount()

  // A subagent's summary and a summary that was skipped change nothing.
  await $.session.compact({ trigger: 'auto', agentId: 'agent-7', messages: SAID } as never)
  answer.skip = true
  await $.session.compact({ trigger: 'manual', messages: SAID } as never)
  await world.clock.advance(1000)
  mounted = await pane($)
  shown = lines(await mounted.drawn())
  expect(shown).toContain('Added as you work')
  expect(shown).toContain('Files and hook notes added to your messages')
  await mounted.unmount()

  // The main conversation is summarised: what loaded after the start is gone, what the session starts with is not.
  answer.skip = false
  await $.session.compact({ trigger: 'manual', messages: SAID } as never)
  await world.clock.advance(1000)
  mounted = await pane($)
  shown = lines(await mounted.drawn())
  expect(shown).not.toContain('Added as you work')
  expect(shown.some(line => /Reminders from Claude Code|Files and hook notes|Skills Claude used|reached them/.test(line))).toBe(false)
  expect(lead(shown)[0]).toBe(total)
  expect(shown).toContain('MCP servers (2)')
  await mounted.unmount()

  // What comes after the summary is counted from nothing.
  await $.prompt.attachment({ type: 'todo_reminder', text: text(40), origin: { kind: 'engine' } } as never)
  await world.clock.advance(1000)
  mounted = await pane($)
  shown = lines(await mounted.drawn())
  expect(run(shown, 'Added as you work', 5)).toEqual(['Added as you work', '10', '/clear and /compact empty these.', 'Reminders from Claude Code', '10'])
  await mounted.unmount()
  expect(faults(world)).toEqual([])
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

