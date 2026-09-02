# ar3 doctrine

Every app in ar3 follows one doctrine. A user who has never opened a
new ar3 app can navigate it because it feels like the ones already learned. The
doctrine governs **how** apps do things, never **how much** they need: r4t taking
a TUI dependency and k7e taking none are both compliant.

`lib/ar3/` is the foundation package that makes the rules true. It sits under
`lib/` rather than at the repo root, because the root already holds the `ar3`
shim and a directory cannot share a name with a file beside it. Every app
reaches it the way it already reaches `ar3ver`, which lives there too — each
entry point appends `<repo>/lib` to `sys.path`, then imports `ar3.<module>`,
with import sites degrading gracefully when one app is relocated away from the
repo (the isolation container copies `apps/r4t` alone). The shared modules are
`ar3.ulid`, `ar3.home` (config-home resolution), `ar3.fsio` (`atomic_write_text`),
`ar3.proc` (`spawn` / `terminate_group`), `ar3.envseam` (the reserved-env
contract), and `ar3.vendor` (the vendoring hook). Beyond stdlib there are exactly
two tiers: **tier 1** is `ar3/_vendor/`, unmodified PyPI releases pinned with
verified sha256 in `ar3/_vendor/vendor.txt`; **tier 2** is the foundation's deps
mechanism, which fetches on demand.

## 1. Dependencies

- The core of every app is **pure stdlib**.
- **Hot paths stay stdlib permanently.** `tell` is the named case and takes no
  dependency, ever.
- Anything beyond stdlib arrives **only through the foundation's two tiers**:
  vendored code in `ar3/_vendor/`, or fetched through the foundation's deps
  mechanism.
- **No app-local vendor directory. No app-local pip logic.**
- An unavailable optional dependency **degrades with a warning at the point of
  use**. It never fails a command that did not need it.

## 2. Filesystem

- **One config-home resolution**, honoring `XDG_CONFIG_HOME`, used by every app:
  `ar3.home`.
- **One `atomic_write` helper**, `ar3.fsio`, with `fsync` and mode `0600`
  available as flags.
- **One state-directory shape** across apps.
- No app re-implements another app's path resolution. An app that needs another
  app's path **imports that app's resolver**.

## 3. CLI feel

- **One verb grammar.** The same action carries the same verb in every app.
- **`--json` is uniform** wherever machine-readable output exists: same flag,
  same position, same meaning.
- **One help voice** — one short sentence, no internals. Mechanics live in the
  app's docs page or its docstring.
- **One error style**: `<app>: <message>`, to stderr.
- **Shared exit-code meanings**, declared in the foundation: `0` success, `1`
  failure, `124` timeout, matching `timeout(1)`. Any further code is declared in
  the foundation before it is used, never invented locally.
- **`--version` is answered by `ar3ver` everywhere.**

The test to apply to any proposed CLI change: a user who learns one ar3 app has
learned the grammar of all of them, including the ones that do not exist yet.

## 4. Processes

- **One spawn/kill escalation policy**, `ar3.proc`, used everywhere: `SIGTERM`,
  a grace period, then `SIGKILL`, applied to the **process group** rather than
  the process.
- **One timeout convention**, with `124` as its exit code.
- No app writes its own kill sequence.

## 5. Integration

- Apps compose **only through foundation-defined seams** — the contracts modules
  the foundation owns.
- **Composition is opt-in.** r4t with a8s for the network seat, k7e with r4t for
  memory. Every app still runs alone.
- **No app hardcodes another app's internals** outside the foundation. Reserved
  environment names, sibling paths, and shared file layouts are declared once, in
  the foundation, and imported.
- The foundation is **a library, not a bus**. It is called; it does not run,
  route, or own lifecycles.

## 6. Docs and release

- **Diátaxis page homes**: `guide/` tutorial, `docs/<app>.md` how-to,
  `docs/<app>-*.md` reference, the private wiki for explanation.
- **Frontmatter is the skill gate.** A `docs/*.md` page beginning `---` installs
  as a skill; deep pages do not grow frontmatter.
- **Quoted YAML scalars** in skill frontmatter, always.
- **One suite `VERSION`**, bumped on every merge to `main`.
- **A merge to `main` is the release.** There is no second switch.
- **Changelog discipline**: user-visible changes land under `Unreleased` in the
  same PR.
- **Pre-v1: no migration code.** The schema changes and the user re-derives.

## The apps

- [a8s](a8s.md) — the message router. [Development notes](a8s-development.md).
- [r4t](r4t.md) — the roster. [Development notes](r4t-development.md).
- [k7e](k7e.md) — the knowledge engine. [Architecture](k7e-architecture.md).
- [ar3](ar3.md) — the front door.
