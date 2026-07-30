# The Ark

The Ark is a suite for running a roster of AI agents on your own machine. **a8s**
routes messages between independent CLI agents, **r4t** governs the roster that
sends them, and **k7e** keeps what the roster learns. `ar3` is the front door: it
reports where the suite stands and probes the tools it runs on, and it never
runs another product's commands for you.

## Install

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh
```

That clones the suite into `~/.ar3` and adds one `source` line to your shell
rc; re-running it updates in place, and so does `git pull`. Nothing installs
into your projects. Without git on PATH the same line installs the latest
release from a tarball, and re-running it updates to the newest release. When
a re-run changes the tree and a8s has running nodes, it finishes with
`~/.ar3/a8s update` so handlers re-exec the new code.

Pin a release with `AR3_VERSION=vX.Y.Z` ahead of the same command. With access
to the development repo, `AR3_CHANNEL=beta` installs the development tree
instead of the latest release (git required); re-running pulls it forward.

Prefer to download, inspect, then run — the pipe-to-shell form cannot prove
the bytes you read are the bytes that execute if a server distinguishes the
two requests:

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh -o get-ar3.sh
less get-ar3.sh
sh get-ar3.sh
```

Prefer it manual? Clone anywhere and `source install.sh` from your shell rc —
the shims at the repo root — `ar3`, `a8s`, `tell`, `tells`, `r4t`, `k7e` — go
on `PATH`. Either way, add `--skills` to the source line to link the tool docs
under `docs/` into Claude Code and Cursor as agent skills.

For machine-wide or `run_as` agent-user installs, skip the shell rc and put the
shims on the shared PATH instead:

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh \
  | sudo AR3_SYSTEM=1 sh
```

That lands the suite in `/usr/local/lib/ar3` (override with `AR3_DIR`) and
symlinks the shims into `/usr/local/bin` (override with `AR3_BIN`). Re-running
updates in place. Agent users then resolve `tell` without reading an operator
home clone.

Headless and cron shells skip `.bashrc`, where the default install drops its
source line. Use the absolute shim (`~/.ar3/ar3`, `~/.ar3/a8s`, …), source
`install.sh` from `.profile`, or schedule the one-liner / local script:

```cron
0 3 * * * /bin/sh -c 'curl --proto "=https" --tlsv1.2 -fsSL https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh'
# offline-tolerant once installed:
# 0 3 * * * /bin/sh $HOME/.ar3/get.sh
```

The public [witw-llc/ar3](https://github.com/witw-llc/ar3) is the release
mirror — one commit per release of the whole suite.

## Where to start

- **[The Ark Raising](guide/README.md)** — a chapter-by-chapter build-along
  that raises a roster of agents from nothing.
- **[docs/ar3.md](docs/ar3.md)** — the front door.
- **[docs/a8s.md](docs/a8s.md)** — the message router.
- **[docs/r4t.md](docs/r4t.md)** — rosters, rigs, dispatch.
- **[docs/k7e.md](docs/k7e.md)** — the knowledge engine.

Every page is flat under [`docs/`](docs/) — one doc tree for the whole suite.

## Versioning

`VERSION` carries a single semver for the whole suite. Every merge to `main`
increments it — patch bump minimum, minor or major at the author's judgment. CI
fails any PR whose `VERSION` matches `main`, so `main`'s history doubles as the
release ledger.

## Licensing

The split is deliberate:

- **Code** — everything outside `guide/` — is **Apache-2.0**. See
  [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
- **The Ark Raising** — everything under `guide/` — is **CC BY-NC-ND 4.0**.
  See [`guide/LICENSE.md`](guide/LICENSE.md).

---

*Placeholder: the one-pager — what The Ark is and who it is for, in the product's
own voice — lands here after the wording session.*
