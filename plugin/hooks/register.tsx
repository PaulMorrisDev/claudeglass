import type { Register } from 'claude-code'

import { register as agentRow } from './agent-row'
import { register as agents } from './agents'
import { register as band } from './band'
import { register as budget } from './budget'
import { register as coach } from './coach'
import { register as command } from './command'
import { register as compaction } from './compaction'
import { register as draft } from './draft'
import { register as notes } from './notes'
import { register as now } from './page-now'
import { register as agentsPage } from './page-agents'
import { register as pane } from './pane'
import { register as compactionPage } from './page-compaction'
import { register as instructionsPage } from './page-instructions'
import { register as pins } from './pins'
import { register as readd } from './readd'
import { register as receipt } from './receipt'
import { register as rebuildLog } from './rebuild-log'
import { register as spinner } from './spinner'
import { register as hint } from './hint'
import { register as tighten } from './tighten'
import { register as track } from './track'
import { register as urgent } from './urgent'

// ClaudeGlass: one file per feature, one line each here. A new page (compaction
// review, agents) is a file that calls addPage() from its own register(); add it
// after `now` so the page bar lists it in order.

export const register: Register = (on, options) => {
  track(on, options)
  draft(on, options)
  coach(on, options)
  agents(on, options)
  notes(on, options)
  rebuildLog(on, options)
  spinner(on, options)
  hint(on, options)
  band(on, options)
  agentRow(on, options)
  command(on, options)
  pane(on, options)
  now(on, options)
  agentsPage(on, options)
  compaction(on, options)
  pins(on, options)
  readd(on, options)
  receipt(on, options)
  compactionPage(on, options)
  instructionsPage(on, options)
  tighten(on, options)
  budget(on, options)
  urgent(on, options)
}
