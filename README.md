# The Ark

The Ark is a suite for running crews of AI agents on your own machine. **a8s**
routes messages between independent CLI agents, **r4t** governs the teams that
send them, and **k7e** keeps what those teams learn. `ar3` is the front door: it
reports where the suite stands and probes the tools it runs on, and it never
runs another product's commands for you.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh
```

That clones the suite into `~/.ar3` and adds one `source` line to your shell
rc; re-running it updates in place, and so does `git pull`. Nothing installs
into your projects.

Prefer it manual? Clone anywhere and `source install.sh` from your shell rc —
the shims at the repo root — `ar3`, `a8s`, `tell`, `tells`, `r4t`, `k7e` — go
on `PATH`. Either way, add `--skills` to the source line to link the tool docs
under `docs/` into Claude Code and Cursor as agent skills.

Development happens in a private repository. The public
[witw-llc/ar3](https://github.com/witw-llc/ar3) carries one commit per release
of the whole suite — clone it, pull it, and read it freely; issues and pull
requests live with the development repo.

## Where to start

- **[The Ark Raising](guide/README.md)** — a chapter-by-chapter build-along
  that raises a crew of agents from nothing.
- **[apps/a8s/README.md](apps/a8s/README.md)** — the message router.
- **[apps/r4t/README.md](apps/r4t/README.md)** — rosters, rigs, dispatch.
- **[apps/k7e/README.md](apps/k7e/README.md)** — the knowledge engine.

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
