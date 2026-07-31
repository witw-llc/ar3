#!/usr/bin/env bash
# Seed Wren's private knowledge store, and give it the chapter 5 bridge so it
# can dream. Run from anywhere; adjust the node name if your roster is not silo.
set -euo pipefail

WREN_STORE="${WREN_STORE:-$HOME/.config/r4t/rosters/silo/agents/wren/k7e}"

K7E_HOME="$WREN_STORE" k7e store "Ship window for the silo roster" \
  --tags ops,deploy \
  --content "Ship on Tuesday mornings. Friday ships are forbidden — nobody reads the logs over the weekend, and a bad ROSTER.md takes the whole node down until someone notices on Monday."

K7E_HOME="$WREN_STORE" k7e store "Who signs off a roster change" \
  --tags ops,roster \
  --content "The owner signs off every ROSTER.md edit before it ships. Moss drafts, Wren commits, the owner approves."

K7E_HOME="$WREN_STORE" k7e config llm_command "$HOME/ark/bin/ask"
