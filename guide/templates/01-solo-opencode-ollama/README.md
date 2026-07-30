Chapter 1's agent on the free path (OpenCode via ollama, qwen3.6).
Copy both files into your agent directory (e.g. `~/ark/solo/`), then:

    a8s add solo ~/ark/solo ~/ark/solo/solo.json
    a8s start solo

The persona is the `prompt=` string in reply.sh — the chapter's customize
step swaps that one line.
Used by [guide/01-hello-agent.md](../../01-hello-agent.md).
