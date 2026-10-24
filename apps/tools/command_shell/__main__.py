#!/usr/bin/env python3
"""tool-command-shell: an a8s tool node that runs a shell command per message.

Run as `python3 apps/tools/command_shell <wake arguments>`.
"""
import os
import sys

if __name__ == "__main__":
    # Run as a directory, Python puts this package's own folder first on the
    # path. Drop it, and put the tools folder (for `command_shell` and
    # `_runtime`) and the foundation (`ar3`) in.
    here = os.path.dirname(os.path.abspath(__file__))
    tools = os.path.dirname(here)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(tools)), "lib"))
    sys.path.insert(0, tools)
    from command_shell.shell import main

    sys.exit(main(sys.argv[1:]))
