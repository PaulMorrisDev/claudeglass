import type { Elements, RenderElement } from 'claude-code'

// The rows every page lists things in: a name on the left and its figure at the
// right edge, both text. A row is never a Button: what a page lets the person do
// sits below its list, and only the page's one main action has a hotkey.
//
// A long name is cut with an ellipsis and never pushes its figure: the name's Box
// may shrink to nothing (`flexShrink` 1, `minWidth` 0) and the figure's Box never
// shrinks (`flexShrink` 0), so the figure keeps its place on one line.

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
  /** A mark before the name that keys the row to a colour or a shade elsewhere on the page (a bar's segment); `color` may be left out. */
  mark?: { glyph: string; color?: string }
}

/** One row of a list: `name` on the left, cut short when it does not fit, and `figure` at the right edge. */
export function figureRow(table: Table, key: string, name: string, figure: string, options: RowOptions = {}): RenderElement {
  const { Box, Text } = table
  const indent = options.indent ?? 0
  const mark = options.mark
  return (
    <Box key={key} flexDirection="row" justifyContent="space-between" gap={2} width={ROW_CELLS} paddingLeft={indent}>
      <Box flexShrink={1} minWidth={0}>
        <Text dimColor={options.dim} color={options.color} wrap="truncate">
          {mark === undefined || mark.color === undefined ? null : <Text color={mark.color}>{`${mark.glyph} `}</Text>}
          {mark !== undefined && mark.color === undefined ? `${mark.glyph} ${name}` : name}
        </Text>
      </Box>
      <Box flexShrink={0}>
        <Text dimColor={options.dim}>{figure}</Text>
      </Box>
    </Box>
  )
}

/** A list's heading: its title in bold and, when it has one, the list's total at the right edge. */
export function heading(table: Table, key: string, title: string, figure: string = ''): RenderElement {
  const { Box, Text } = table
  return (
    <Box key={key} flexDirection="row" justifyContent="space-between" gap={2} width={ROW_CELLS}>
      <Box flexShrink={1} minWidth={0}>
        <Text bold wrap="truncate">
          {title}
        </Text>
      </Box>
      {figure !== '' ? (
        <Box flexShrink={0}>
          <Text bold>{figure}</Text>
        </Box>
      ) : null}
    </Box>
  )
}
