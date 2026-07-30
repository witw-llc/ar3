#!/usr/bin/env bash
sender="$1"
message="$2"

prompt="You are solo, an AI agent on this machine. Answer in one or two
sentences, no preamble.

$sender asks: $message"

answer="$(ollama launch opencode --model qwen3.6 -- run --auto --dir . "$prompt" 2>/dev/null)"
tell "$sender" "$answer"
