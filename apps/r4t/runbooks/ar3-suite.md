---
name: "ar3-suite"
extends: "triforce"
workdir: "."
comms: "open"
egress: true
---

# AR3 suite

The Triforce roster under release discipline. Take this one when the repo is a
product other people install: it keeps Triforce's three members and replaces
the charter with the rules a shipped version has to obey.

Write your own `## Roster` in the file that extends this one and it replaces
Triforce's whole roster — a section replaces, it never blends.

## Charter

How this team works, whatever it is working on.

**Amendment.** A rule here changes by a recorded decision, never by drift, a
member's in-turn judgment, or a sentence in a pull request body. A member who
thinks a rule is wrong says so to its lead and keeps following it until the
record exists.

**One branch per batch.** All work lands on a single branch named for the
target version. Never open half a dozen parallel pull requests: they cannot be
integration-tested together, they put the merge onto the reviewer, and the
result depends on the order they land in. One batch PR is a frozen picture of
the future; a scatter is a guess.

**Version every merge.** The repo carries one semver, and every merge to the
default branch bumps it — patch minimum. State the build date and the version
of the platform the software runs on (the Python, Node or Go version) wherever
the software reports its own version, so a user can tell a broken build from a
good one at a glance. Merge and version bump are the same event, so the
default branch's history is the release ledger.

**The merge ladder has four rungs and each is a different person.** A member
branches, commits, and opens a sub-pull-request against the version branch.
The lead engineer reviews it and the adversarial round runs. The roster lead
merges the sub-PR into the version branch. The owner merges the version branch
to the default branch, and that merge is the release. Nobody else touches the
default branch, for any reason.

**A finding is not a fact until something tried to kill it.** The reviewer
writes each concern as a separate, falsifiable claim: what is wrong, where it
is, and what it costs. Each claim goes to a member who did not write the code
and who is prompted to refute it, not to weigh it. A killed claim is dead and
does not come back. A surviving claim is fixed or answered before the merge,
and the record of it goes in the pull request body.

**Write no lineage into the code.** A comment carries an issue number, a pull
request number or a person's name only when it names work still to be done.
"Here until the migration lands" and "added in PR 1234" are workflow, not
artifacts — git already answers that, and answers it better. The reviewer
catches this at the last gate.

**Report by exception.** The standing question up the chain is *is anything
significant happening in your area*, and silence is a complete answer. Anyone
may pull a status on demand at any level; only the push is curated. Never
spend the owner's attention to save the machine's.

**Reproduce before fixing.** "Could not reproduce, here is what I tried" is a
valid outcome and a good one.

**Hygiene is part of every role.** Run the test suite before you hand anything
over. Add the changelog line in the same change a user would notice. Leave the
working tree clean.
