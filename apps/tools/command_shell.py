"""Compatibility entry: the tool moved to the `command_shell` package in
0.1.116. A definition copied before then still names this file; it runs the
package unchanged."""
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
runpy.run_module("command_shell", run_name="__main__", alter_sys=True)
