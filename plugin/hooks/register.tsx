import type { Register } from 'claude-code'

import { register as band } from './band'
import { register as command } from './command'
import { register as notes } from './notes'
import { register as now } from './page-now'
import { register as pane } from './pane'
import { register as rebuildLog } from './rebuild-log'
import { register as spinner } from './spinner'
import { register as hint } from './hint'
import { register as track } from './track'

// ClaudeGlass: one file per feature, one line each here. A new page (compaction
// review, agents) is a file that calls addPage() from its own register(); add it
// after `now` so the page bar lists it in order.

export const register: Register = (on, options) => {
  track(on, options)
  notes(on, options)
  rebuildLog(on, options)
  spinner(on, options)
  hint(on, options)
  band(on, options)
  command(on, options)
  pane(on, options)
  now(on, options)
}
