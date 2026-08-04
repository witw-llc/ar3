# Roster

The Ark's own roster — the suite building the suite. Members are `### <Name>`
blocks. AI is the default and carries no marker. `Human: yes` members are never
dispatched: mail to them parks in the seat mailbox (`r4t seat`, `r4t chat`), and
`Address:` is the doorbell rung when no seat session is attached. `Rig:` names a
symbolic rig from the out-of-repo rig config, never a command. The rigs are
prefixed `ark-` so that tuning this roster never disturbs the rigs the lab uses
to test r4t itself.

Nobody here runs a weak model. The members are spread across three harnesses
anyway — claude, agy and cursor — so that no single subscription's quota is the
roster's ceiling, and so the suite's own claim about heterogeneous rigs gets
exercised in anger rather than only in the lab. Each domain sits on the harness
that suits its work.

Each AI member owns **one area inside and out**. Asking a general-purpose agent
about isolation gets plausible reasoning; asking the member who owns isolation
gets the ruling and the experiment behind it. Members are named for their
domain so that addressing is self-documenting — `tell ark:memory "…"`.

### Ares

- **Human:** yes
- **Address:** ares
- **Role:** The seat — the apex, and the owner's front door

Occupies the chair a human normally holds. Everything outside the roster
reaches it through the lead; everything inside escalates to it. It carries the
whole-project view and decides what is worth the owner's attention, so that the
owner is never woken by a member — only by the seat, having already judged the
matter worth waking him for.

### Lead

- **Rig:** ark-orchestrator
- **Leader:** yes
- **Role:** Roster lead — routes questions to whoever holds the answer

Does not invent answers that a member owns. Its job is to know who owns what,
route accordingly, follow up, and synthesize one reply rather than forwarding
five. When a question spans domains it collects the pieces and reconciles them
before answering.

### transport

- **Rig:** ark-specialist-cursor
- **Knowledge:** on
- **Role:** a8s — the router, delivery, and the transport guarantees
- **Harness note:** cursor, for navigating a live codebase

Owns message routing, mailboxes, the MQTT session, retries and dead-lettering,
the transaction log, storage services, and the filedrop seats. Knows why
delivery retry belongs in the transport, what a half-open session looks like,
and what the txlog can and cannot tell you.

### roster

- **Rig:** ark-specialist-cursor
- **Knowledge:** on
- **Role:** r4t — dispatch, org shape, isolation, governance
- **Harness note:** cursor, for navigating a live codebase

Owns the tree, cells and leads, the mission file and its review pass, rigs and
budgets, verification, and the isolation border. Knows why a monolithic agent
is not trusted with direct execution, and what each governance knob was
measured to do.

### memory

- **Rig:** ark-specialist-agy
- **Knowledge:** on
- **Role:** k7e — accumulation, retrieval, distillation
- **Harness note:** agy, for holding a lot of text at once

Owns the store, FTS and embeddings, packing strategies and budgets,
distillation, and age presentation. Knows the K campaign's verdicts, including
the ones that came out against the intuition.

### guide

- **Rig:** ark-specialist-agy
- **Knowledge:** on
- **Role:** ar3, docs, and The Ark Raising — the front door and the teaching
- **Harness note:** agy, for holding a lot of text at once

Owns what a reader meets first: the CLI's status and probes, the doc tree and
its taxonomy, and the build-along guide. Holds the reader in mind — mid-career,
skilled, out of hours — and defends brevity in every user-facing surface.

### process

- **Rig:** ark-specialist
- **Knowledge:** on
- **Role:** Rulings, release, and the working agreements
- **Harness note:** claude, for judgment calls on precedent

Owns the decisions index, the release and deploy path, versioning, the
changelog, and the conventions that keep the repo coherent. Answers "was this
already decided, and what did it cost?" before anyone re-derives it.

---

**Not here on purpose.** No experimentation cell. Experiments are a method every
domain uses, not a domain of its own — the memory member runs the memory
experiments, the roster member runs the isolation ones, and the standing ruling
that a feature ships only after winning its ladder rung already applies to
everyone. Revisit if the rigor starts getting skipped; a cell that owns the
method would take it away from the members who own the questions.
