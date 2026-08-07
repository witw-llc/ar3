# Chapter 1 — Hello, Agent

**Teaches A — [a8s](../docs/a8s.md), the message router.**

## 1. Capability

At the end of this chapter you will have `solo`: an agent registered on a8s,
running on your own machine, that you message with `tell` and that answers
you. Not an echo — a real model behind a real harness, with tools, reading
its own files when you ask it to. You will have seen the whole loop (outbox,
router, inbox, wake, reply), taken solo's handler away and watched a message
wait instead of die, and changed solo's character by editing one string.

## 2. Time

About 20 minutes of hands-on time. On the free path start the model download
first — it runs in the background while you read, and it is the only long
wait in the chapter.

## 3. Starting state

- No leftover `TELL_OUTBOX_DIR` from an earlier a8s seat on this machine —
  `tell` checks it before anything else, so a stale value silently sends
  your mail to that other seat's outbox instead of the one you build below:

**Run**

```bash
unset TELL_OUTBOX_DIR
```

- The Ark installed and on your PATH:

**Run**

```bash
curl -fsSL https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh
```

  That clones the suite into `~/.ar3` and adds one `source` line to your
  shell rc. Open a new shell (or re-source the rc) so `PATH` picks it up.

- Python 3 installed (`python3 --version` answers).
- One harness. **Pick your path** — the free path is the default, and every
  pasted output below was captured on it:
  - **Free path** — [OpenCode](https://opencode.ai/) plus `ollama`. Start the
    download now:

**Run**

```bash
ollama pull qwen3.6
```

  - **Subscription path** — the Cursor agent CLI (`agent`), installed and
    logged in.

Now type the suite's front door command:

**Run**

```bash
ar3
```

You should see:

```
A R K
8 4 7
S T E

The Ark — a8s routes the messages, r4t governs the roster,
k7e keeps what they learn. ar3 reads; each product owns its own verbs.

a8s — agent message router  (/home/you/.config/a8s)
  ✓ cli       a8s -> /home/you/.ar3/a8s
  ✗ registry  no registry at /home/you/.config/a8s/a8s.json   (try: a8s discover <dir>)

r4t — the roster  (/home/you/.config/r4t)
  ✓ cli      r4t -> /home/you/.ar3/r4t
  ✗ rigs     no rig config at /home/you/.config/r4t/rigs.json   (try: r4t init)
  ✗ rosters  none under /home/you/.config/r4t/rosters   (try: r4t init)

k7e — knowledge engine  (/home/you/.config/k7e)
  ✓ cli    k7e -> /home/you/.ar3/k7e
  ✗ store  no store at /home/you/.config/k7e   (try: k7e store <title>)

next: ar3 doctor — probe the harnesses and tools the suite runs on
```

Three CLIs found, nothing configured — the correct fresh-machine state.
`ar3` never changes anything; it reads and tells you which command owns the
next move. Take its suggestion:

**Run**

```bash
ar3 doctor
```

You should see:

```
ar3 doctor — probes only; nothing here is installed, started, or changed

Harnesses
  ✓ claude    2.1.220 (Claude Code)  (/home/you/.local/bin/claude)
  ✓ agent     2026.07.23-e383d2b  (/home/you/.local/bin/agent)
  ✓ codex     codex-cli 0.144.6  (/home/you/.local/bin/codex)
  ✓ copilot   GitHub Copilot CLI 1.0.75.  (/home/you/.local/bin/copilot)
  ✓ opencode  1.18.3  (/home/you/.local/bin/opencode)
  ✓ agy       1.1.8  (/home/you/.local/bin/agy)
  ✓ ollama    ollama version is 0.32.5  (/home/you/.local/bin/ollama)

Services
  ✓ ollama serve  3 model(s): qwen3.6:latest, qwen3:1.7b, qwen3:0.6b
  ✓ docker        daemon 29.6.2

Tooling
  ✓ git  git version 2.50.1 (Apple Git-155)

✓ core prerequisites satisfied  (10/10 probes green)
```

Your panel will show ✗ for harnesses you haven't installed — that is fine.
This chapter needs one path's worth: `opencode` plus `ollama` on the free
path, or `agent` on the subscription path. Everything else can stay red.

## 4. The change

Two directories: one agent that thinks, and one seat for you to speak from.

An a8s agent is a directory plus a **definition** — a small JSON file naming
the command that wakes it. When a message arrives, a8s substitutes `$SENDER`
and `$MESSAGE` into that command and runs it, with the agent's own directory
as the working directory. a8s never looks inside; whether the command is a
shell script, a model harness, or both is entirely your business.

**Run**

```bash
mkdir -p ~/ark/solo ~/ark/me
```

solo's wake command is three moves: seed a prompt, hand it to the harness,
send the answer back with `tell`.

**Create** `~/ark/solo/reply.sh` (free path)

```bash
#!/usr/bin/env bash
sender="$1"
message="$2"

prompt="You are solo, an AI agent on this machine. Answer in one or two
sentences, no preamble.

$sender asks: $message"

answer="$(ollama launch opencode --model qwen3.6 -- run --auto --dir . "$prompt" 2>/dev/null)"
tell "$sender" "$answer"
```

**Subscription path** — one line differs. Use this `answer=` line instead:

```bash
answer="$(agent --model auto -p --trust --force --approve-mcps "$prompt" 2>/dev/null)"
```

The `2>/dev/null` is doing real work: a harness paints its progress UI on
stderr and puts the finished answer alone on stdout, so throwing stderr away
leaves you holding exactly the reply and nothing else. `--auto` (free path)
and `--trust --force` (subscription path) are the headless permission flags —
without them the harness waits forever for a prompt nobody is there to
answer.

The `prompt=` block is the **seeded prompt**: solo's character first, then
who is asking and what they asked. It is the only place solo's personality
lives, and it is a shell string you own outright.

**Create** `~/ark/solo/solo.json`

```json
{
  "description": "solo — a local agent: a harness CLI behind a8s",
  "invoke": ["bash", "reply.sh", "$SENDER", "$MESSAGE"],
  "max_wake_seconds": 600
}
```

`max_wake_seconds` is the one guard rail worth setting today: harnesses
occasionally hang instead of exiting, and this kills a wake that runs long
rather than letting it hold the agent forever.

`~/ark/me` is your **filedrop seat**: a directory a8s delivers your mail
into, with no CLI to wake — you read it with `tells`. Register both, then
look at the roster:

**Run**

```bash
a8s add me ~/ark/me filedrop
a8s add solo ~/ark/solo ~/ark/solo/solo.json
a8s ls
```

You should see:

```
added me -> /home/you/ark/me
definition: /home/you/.ar3/apps/a8s/definitions/filedrop.json  (explicit)
wake_path: recorded this shell's PATH for every node's wakes
added solo -> /home/you/ark/solo
definition: /home/you/ark/solo/solo.json  (explicit)
NAME   STATUS    DEFINITION   ROOT
me     stopped   filedrop     /home/you/ark/me
solo   stopped   solo         /home/you/ark/solo
```

That third line appears once, on the first `a8s add` on a machine. A woken
agent gets the environment of whatever shell started its handler, which is
right when you start from a terminal and wrong when cron or ssh does it — so
a8s writes down the PATH of the shell you are typing in now and gives every
wake that instead. `a8s config` shows it as `wake_path`.

Registered but stopped: nothing routes until a **handler** process is
attached. Start one for each:

**Run**

```bash
a8s start solo
a8s start me
```

You should see:

```
started solo as PID 8396
started me as PID 8401
```

## 5. Run it

Speak from your seat. `tell` figures out who you are from the directory you
stand in, and `tells` watches your inbox for the window you give it — Ctrl+C
as soon as the answer lands. Give this first wait a long window: solo's
first wake pays for the model's cold start on top of the harness's own, and
that can run past two minutes before a single word comes back.

**Run**

```bash
cd ~/ark/me
tell solo "Read solo.json in your own directory and tell me in one sentence what it does."
tells --timeout 300
```

## 6. Expected receipt

You should see:

```
tell -> solo: Read solo.json in your own directory and tell me in one sentence what it does.
solo: solo.json describes a local agent — an OpenCode instance driven by ollama — that invokes reply.sh with the provided sender and message inputs.
```

The first turn takes a minute or two while the model loads; later ones take
seconds. That round trip crossed the full machinery: your envelope was
written to `~/ark/me/.outbox/`, the router stamped you as the sender and
moved it to solo's inbox, solo's handler ran `reply.sh` with your text, and
the reply rode the same road back into `~/ark/me/.inbox/`.

And solo did something an echo cannot: it read a file. Nothing in the prompt
carried the contents of `solo.json` — solo has tools, and it used them to go
look.

## 7. Break it

Take the handler away and message solo anyway:

**Run**

```bash
a8s stop solo
tell solo "Are you still there?"
tells --timeout 20
```

You should see:

```
solo: sent SIGTERM to PID 8396
waiting up to 600s for stop…
solo: stopped
tell -> solo: Are you still there?
tells: no message within 20s
```

`stop` waits for the handler to let go — up to the `max_wake_seconds` you
set, because a wake in flight finishes before the handler detaches. Then
`tell` accepted your message and nothing answered. Nothing crashed and
nothing warned you, which is exactly the state worth learning to read.

## 8. Diagnose

Two reads settle any "where did my message go?" question. The registry says
who is attached:

**Run**

```bash
a8s ls
```

You should see:

```
NAME   STATUS               DEFINITION   ROOT
me     running (pid 8401)   filedrop     /home/you/ark/me
solo   stopped              solo         /home/you/ark/solo
```

And the per-agent log says what actually moved:

**Run**

```bash
a8s logs solo --tail 2
```

You should see:

```
2026-07-29T21:40:09.213208Z [a8s] solo: detached
2026-07-29T21:40:09.771701Z received from me: Are you still there?
```

Read those two lines together. The handler **detached**, and after that the
message was **received** — routed out of your outbox into solo's inbox on
disk — with no wake line following it, because no process was there to wake.
It is not lost and it is not held in anyone's memory; it is a file waiting in
a directory. That is the whole durability story, and it is why the fix is one
command.

## 9. Fix

Attach a handler again:

**Run**

```bash
a8s start solo
tells --timeout 300
```

You should see:

```
started solo as PID 11786
solo: Yes, I'm here. What can I help you with?
```

The waiting message woke solo the moment a handler claimed it. You did not
resend anything.

## 10. Check

Ask the front door where the suite stands now:

**Run**

```bash
ar3
```

You should see (a8s section):

```
a8s — agent message router  (/home/you/.config/a8s)
  ✓ cli       a8s -> /home/you/.ar3/a8s
  ✓ registry  2 agent(s), 0 alias(es), 0 namespace(s)
  ✓ router    attached: me, solo
```

The registry has your two agents and both handlers are attached. The r4t
section still shows ✗ — that is chapter 2 — and k7e waits until chapter 5.

## 11. Customize

Solo's character is one string in one file. Change it:

**Replace** `~/ark/solo/reply.sh` — the whole `prompt=` assignment:

```bash
prompt="You are solo, the keeper of a lighthouse on this machine. Answer in
one or two sentences, no preamble, and always mention the weather.

$sender asks: $message"
```

No restart needed — the definition runs the script fresh on every wake:

**Run**

```bash
cd ~/ark/me
tell solo "What are you watching over today?"
tells --timeout 300
```

You should see:

```
tell -> solo: What are you watching over today?
solo: I'm watching over the northern channel, where the jagged teeth of reef wait for careless ships. The fog rolls thick tonight, so I've kept the light burning low and steady.
```

That is the whole tuning surface for a bare agent: whatever you put in the
prompt is who it is.

## 12. Commit point

Your agent is two files; keep them under version control like anything else
you built:

**Run**

```bash
cd ~/ark/solo
git init -q
git add reply.sh solo.json
git commit -q -m "solo: a local agent behind a8s"
```

Copy-paste versions of solo's two files live in
[templates/01-solo-opencode-ollama/](templates/01-solo-opencode-ollama/) and
[templates/01-solo-cursor/](templates/01-solo-cursor/), carrying the plain
persona — the lighthouse is one line away.

## Beyond this machine

Two pointers for later — nothing in this chapter needs them:

- Messaging an agent on another machine: [a8s remotes](../docs/a8s.md#remotes-issue-63).
- Reaching your agents by text message: [a8s-android](https://github.com/neilobremski/a8s-android).

## What you own

An agent with a mind, an address, and a durable mailbox — yours, on your
hardware, for as long as you keep the handler running.

Keep `solo` and `me` running. You will notice what is missing the first time
you use it in anger: solo forgets you between messages, nothing bounds what a
runaway wake costs you, and a second agent would need a second script. That
is chapter 2.
