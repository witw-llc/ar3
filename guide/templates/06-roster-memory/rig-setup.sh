#!/usr/bin/env bash
set -euo pipefail

r4t rig add silo opencode-ollama --model qwen3.6
r4t rig add helper opencode-ollama --model qwen3.6
r4t rig set helper echo true
