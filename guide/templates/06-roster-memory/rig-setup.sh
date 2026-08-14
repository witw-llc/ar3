#!/usr/bin/env bash
set -euo pipefail

r4t rig add silo ollama-opencode --model qwen3.6
r4t rig add helper ollama-opencode --model qwen3.6
r4t rig set helper echo true
