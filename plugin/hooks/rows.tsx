import type { Elements, RenderElement } from 'claude-code'

// The rows every page lists things in: a name on the left and its figure at the
// right edge, both text. A row is never a Button: what a page lets the person do
// sits below its list, and only the page's one main action has a hotkey.

/** How wide a row runs, in cells: the figure sits at its right edge, near enough to read with its name. */
export const ROW_CELLS = 56

type Table = Elements[keyof Elements]

type RowOptions = {
  /** Drawn dim: a row that matters less than the ones around it. */
  dim?: boolean
  /** Cells before the name, for a row that belongs to the one above it. */
  indent?: number
  /** A theme colour for the name, never the only sign of what the row means. */
  color?: string
}

/** One row of a list: `name` on the left, cut short when it does not fit, and `figure` at the right edge. */
export function figureRow(table: Table, key: string, name: string, figure: string, options: RowOptions = {}): RenderElement {
  const { Box, Text } = table
  const indent = options.indent ?? 0
  return (
    <Box key={key} flexDirection="row" justifyContent="space-between" gap={2} width={ROW_CELLS} paddingLeft={indent}>
      <Box flexShrink={1}>
        <Text dimColor={options.dim} color={options.color} wrap="truncate">
          {name}
        </Text>
      </Box>
      <Text dimColor={options.dim}>{figure}</Text>
    </Box>
  )
}

/** A list's heading: its title in bold and, when it has one, the list's total at the right edge. */
export function heading(table: Table, key: string, title: string, figure: string = ''): RenderElement {
  const { Box, Text } = table
  return (
    <Box key={key} flexDirection="row" justifyContent="space-between" gap={2} width={ROW_CELLS}>
      <Text bold>{title}</Text>
      {figure !== '' ? <Text bold>{figure}</Text> : null}
    </Box>
  )
}
