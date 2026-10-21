#!/usr/bin/env python3
"""vault: an a8s tool node that stores files for agents, sealed end to end.

Run as `python3 apps/tools/vault <subcommand>`. The wake is `handle`; the rest
are the operator's admin commands and the agent's client.
"""
import os
import sys

if __name__ == "__main__":
    # Run as a directory, Python puts this package's own folder first on the
    # path, where its `vault.py` would shadow the package. Drop it, and put the
    # tools folder (for `vault` and `_runtime`) and the foundation (`ar3`) in.
    here = os.path.dirname(os.path.abspath(__file__))
    tools = os.path.dirname(here)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(tools)), "lib"))
    sys.path.insert(0, tools)
    from vault.cli import main

    sys.exit(main())
