# tool-timer

Schedule delayed callback messages and send them back to the caller when due.
Send a delay and a message body to set a callback. The tool stores the timer in
its working directory, waits in a background worker, and delivers the message
back to the sender when the delay expires.

Use `ar3 tool timer` from a terminal. Use `tell` from another node.
See [Tool nodes](../../../docs/a8s-tools.md).

## Needs

Install AR3. No extra packages or credentials are required.

## Register

```bash
a8s add callmeback ~/timers/callmeback tool-timer --ALLOW=operator,assistant
a8s start callmeback
```

`ALLOW` is required. If `ALLOW` is missing, a8s stops the wake before the tool
runs and drops the message. Use `a8s logs callmeback` to read the error. A
sender outside `ALLOW` gets no reply. The node logs the refusal.

## Usage

Send `<delay> <message>` to schedule a callback, `list` to inspect your pending
timers, or `cancel <id|all>` to cancel them.

From another node:

```bash
tell callmeback 600 Check the deployment status
tell callmeback 10m Review the test run
tell --attach summary.txt callmeback 2h Follow up on this report
tell callmeback list
tell callmeback cancel all
```

On the node's machine:

```bash
ar3 tool timer --node callmeback list
ar3 tool timer --node callmeback cancel 01JABCDEF0123456789ABCDEFG
ar3 tool timer --node callmeback cancel all
ar3 tool timer --node callmeback stop
ar3 tool timer --node callmeback -- "600 Check the deployment status"
```

### Commands

| Message | Result |
|---|---|
| `<delay> <message>` | Schedules `<message>` (and any attached files) to return to the sender after `<delay>`. Delays accept seconds (`600`) or `s`, `m`, `h`, and `d` suffixes (`30s`, `10m`, `1.5h`, `1d`). |
| `list` | Replies immediately with the sender's pending timers ordered by due time. |
| `cancel <id>` | Cancels one pending timer owned by the sender and replies immediately. |
| `cancel all` | Cancels all pending timers owned by the sender and replies immediately. |
| `help` | Replies immediately with this usage text. Also accepts `/help`, `--help`, and `/?` on a single line. |

### Reply

Scheduling a timer on a routed node sends no immediate reply. A sleeping or
resident caller stays asleep until the timer fires. When the delay expires, the
tool sends `<message>` (and any staged attachments) to the original sender.

Invalid delays, missing callback text, `list`, `cancel`, and `help` reply
immediately.

### Options

Set an option at registration with `a8s add ... --KEY=VALUE`. Change a
registered option with `a8s vars <node> set KEY VALUE`. Pass `--var KEY=VALUE`
before `--` for one local run.

| Variable | Default | Function |
|---|---|---|
| `ALLOW` | Required | Lists permitted sender names separated by commas or spaces. Names are not case-sensitive. |
| `MAX_DELAY` | `604800` | Sets the maximum allowed delay in seconds (default 7 days). |
| `IDLE_TIMEOUT` | Unset | Optional idle check interval in seconds. When set, a8s runs `idle` to resume a stopped worker if `timers.json` still holds pending entries. |

```bash
a8s vars callmeback set MAX_DELAY 86400
a8s vars callmeback set IDLE_TIMEOUT 60
```

## State

All state lives in the node's working directory and moves with that directory:

- `timers.json` stores pending callbacks.
- `.timers/` holds lock files, worker metadata, and staged copies of attached files until delivery or cancellation.

## Platforms

The tool runs on POSIX and Windows. On POSIX, the delivery worker runs in its
own session. On Windows, the worker starts outside the wake job object when the
host permits breakaway. Stopping the node runs `stop`, which ends the worker
and leaves `timers.json` intact on disk.

## Try it by hand

Run a callback in a scratch directory without registration:

```bash
ar3 tool timer -- "1 Hello from timer"
ar3 tool timer -- help
ar3 tool timer --help
```

When no outbox exists in the working directory, `ar3 tool timer` waits for the
delay inline and prints the callback.

## Errors

| Condition | Action |
|---|---|
| Missing `ALLOW` | Run `a8s vars callmeback set ALLOW operator,assistant`. |
| Delay exceeds `MAX_DELAY` | Use a shorter delay or raise `MAX_DELAY` with `a8s vars`. |
| Timer ID not found | Run `tell callmeback list` to check pending timer IDs. |

## Trust

Each sender can list or cancel only its own timers through `tell`. Local CLI
commands (`ar3 tool timer --node ... list` and `cancel`) inspect and manage all
timers in the node's directory. Give access only to senders allowed to store
temporary callback payloads and attachments on the node's disk.
