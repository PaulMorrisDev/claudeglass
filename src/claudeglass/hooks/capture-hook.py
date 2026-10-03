#!/usr/bin/env python3
"""Claude Code hook: ClaudeGlass's metrics capture and live coaching.

A small launcher. The work is in ``capture_hook.py``, installed beside
it: Python keeps no bytecode for the script it is started on, only for a
module it imports, so a hook that held all its code here would compile
it again on every call. Claude Code runs ``python -I -S``, which leaves
this folder off the import path, so the launcher adds it.

Like the hook, it always exits 0 without printing anything on an error,
so a module that is missing or broken (an install cut short) can never
block or break a session. ``claudeglass capture connect`` fixes one.
"""

import os
import sys


def _launch() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import capture_hook
    except Exception:  # noqa: BLE001 - must never fail or block a session
        # Read what Claude Code sent, as the hook does, so it never writes
        # to a pipe nobody reads.
        try:
            sys.stdin.buffer.read()
        except Exception:  # noqa: BLE001
            pass
        return 0
    return capture_hook.main()


if __name__ == "__main__":
    sys.exit(_launch())
