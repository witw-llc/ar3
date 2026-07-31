Chapter 5's LLM bridge on the free path (ollama's local HTTP API, qwen3.6).
k7e writes a prompt to stdin and reads the answer from stdout; this is the
whole contract. Copy `ask` somewhere on disk, then:

    chmod +x ~/ark/bin/ask
    k7e config llm_command "$HOME/ark/bin/ask"

`ASK_MODEL=qwen3:1.7b ...` picks a different local model. On the subscription
path the bridge is the Cursor CLI with no prompt argument:

    k7e config llm_command 'agent --model auto -p --trust --force --approve-mcps'

`NOTES.md` is the scratch file the chapter distills.
Used by [guide/05-nothing-learned-twice.md](../../05-nothing-learned-twice.md).
