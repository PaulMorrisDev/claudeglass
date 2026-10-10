import type { Register } from 'claude-code'

import { register as agents } from './agents'
import { register as band } from './band'
import { register as coach } from './coach'
import { register as command } from './command'
import { register as compaction } from './compaction'
import { register as draft } from './draft'
import { register as pane } from './pane'
import { register as instructionsPage } from './page-instructions'
import { register as pins } from './pins'
import { register as readd } from './readd'
import { register as receipt } from './receipt'
import { register as spinner } from './spinner'
import { register as hint } from './hint'
import { register as tighten } from './tighten'
import { register as track } from './track'
import { register as urgent } from './urgent'

// ClaudeGlass: one file per feature, one line each here. The pane (pane.tsx) draws the two pages,
// Checkpoint and Startup; page-instructions.tsx registers the hooks that watch what loads at the start.

export const register: Register = (on, options) => {
  track(on, options)
  draft(on, options)
  coach(on, options)
  agents(on, options)
  spinner(on, options)
  hint(on, options)
  band(on, options)
  command(on, options)
  pane(on, options)
  compaction(on, options)
  pins(on, options)
  readd(on, options)
  receipt(on, options)
  instructionsPage(on, options)
  tighten(on, options)
  urgent(on, options)
}
