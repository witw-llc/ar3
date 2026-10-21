#!/usr/bin/env python3
"""gemini_web: an a8s tool node that types each tell into Gemini Web.

Run as `python3 apps/tools/gemini_web <subcommand>`. The wake is `handle`; the
rest drive a seat by hand.
"""
import os
import sys

if __name__ == "__main__":
    # Run as a directory, Python puts this package's own folder first on the
    # path. Drop it, and put the tools folder (for `gemini_web` and `_runtime`)
    # and the foundation (`ar3`) in.
    here = os.path.dirname(os.path.abspath(__file__))
    tools = os.path.dirname(here)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(tools)), "lib"))
    sys.path.insert(0, tools)
    from gemini_web.cli import main

    sys.exit(main())
