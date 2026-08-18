#!/usr/bin/env bash
# Seed Wren's private knowledge store with the two notes chapter 6 writes by
# hand. Run from anywhere; adjust the node name if your roster is not silo.
# Nothing else to configure: a member's store distills through the member's
# own rig, which r4t supplies on every idle pass.
set -euo pipefail

WREN_STORE="${WREN_STORE:-$HOME/.config/r4t/rosters/silo/agents/wren/k7e}"

K7E_HOME="$WREN_STORE" k7e store "Ship window for the silo roster" \
  --tags ops,deploy \
  --content "Ship on Tuesday mornings. Friday ships are forbidden — nobody reads the logs over the weekend, and a bad r4t.md takes the whole node down until someone notices on Monday."

K7E_HOME="$WREN_STORE" k7e store "Who signs off a roster change" \
  --tags ops,roster \
  --content "The owner signs off every r4t.md edit before it ships. Moss drafts, Wren commits, the owner approves."
