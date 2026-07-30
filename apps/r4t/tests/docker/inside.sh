#!/usr/bin/env bash
# Runs as root INSIDE the ubuntu:24.04 container (see run-as.sh). Provisions the
# operator prerequisites r4t never automates, then drives a real org-level
# `run_as` dispatch turn as a non-root router user and asserts the boundary.
set -euo pipefail

echo "== provisioning (the operator's job; r4t never does this) =="
apt-get update -qq
apt-get install -y -qq python3 sudo >/dev/null

# A container-owned, writable copy of the r4t tree (the mount is read-only).
cp -a /src-r4t /opt/r4t
export PYTHONDONTWRITEBYTECODE=1

# --- users and groups ---------------------------------------------------------
# router: the a8s/r4t machinery user (non-root, holds the scoped sudo grant).
# agent:  the sandbox — NO sudo group, NO sudoers entry of its own.
groupadd r4t-work
useradd -m -s /bin/bash router
useradd -m -s /bin/bash agent
usermod -aG r4t-work router
usermod -aG r4t-work agent
# r4t re-asserts the staging dir to router:<agent primary group>; a non-root
# router can only chgrp to a group it belongs to, so it must join the agent
# group. (Provisioning fact, not a code path — documented in docs/r4t-isolation.md.)
usermod -aG agent router
# The border: the agent must not be able to read the router's home.
chmod 700 /home/router
# An a8s-style attachment inside the sealed home — the delivered-bundle copy is
# the only form the agent can read.
echo "the sealed payload" > /home/router/briefing.txt
chown router:router /home/router/briefing.txt
chmod 600 /home/router/briefing.txt

# --- scoped, passwordless sudo (NOT blanket NOPASSWD: ALL) ---------------------
# router may act ONLY as agent, ONLY via the shapes r4t actually invokes: the
# `bash --login -c` wake wrapper and the `true` grant probe.
# `type -P` returns the on-disk binary, skipping the `true` shell builtin —
# sudoers Cmnd_Alias entries must be absolute paths.
BASH_BIN="$(type -P bash)"
TRUE_BIN="$(type -P true)"
cat >/etc/sudoers.d/r4t-agent <<EOF
Cmnd_Alias R4T_AGENT = ${BASH_BIN} --login -c *, ${TRUE_BIN}
router ALL=(agent) NOPASSWD: R4T_AGENT
EOF
chmod 0440 /etc/sudoers.d/r4t-agent
visudo -cf /etc/sudoers.d/r4t-agent   # fail loudly on a malformed drop-in

# --- r4t state + the shared workplace ----------------------------------------
install -d -o router -g router /var/lib/r4t                 # R4T_HOME (router-owned)
install -d -o router -g r4t-work -m 2770 /work             # workplace: setgid, group-writable
# Portable org dir: router-readable ROSTER/MISSION + the run_as/repo pointer.
install -d -o router -g router /etc/r4t-org
cat >/etc/r4t-org/ROSTER.md <<'EOF'
# Roster

### Worker
- **Rig:** solo
- **Leader:** yes
EOF
cat >/etc/r4t-org/r4t-org.json <<'EOF'
{ "run_as": "agent", "repo": "/work" }
EOF
cat >/etc/r4t-rigs.json <<'EOF'
{
  "throttle": { "max_concurrent": 0, "min_seconds_between_turn_starts": 0 },
  "cell_budget_max": 100, "cell_budget_earn_per_hour": 100,
  "solo": {
    "invoke": ["python3", "/opt/r4t/tests/docker/member.py", "{prompt}"],
    "timeout_seconds": 60, "budget_max": 50, "budget_earn_per_hour": 50
  }
}
EOF
chown -R router:router /etc/r4t-org /etc/r4t-rigs.json
chmod -R a+rX /opt/r4t   # the wrapped agent must be able to read member.py

echo "== running a real org-level run_as dispatch turn (as the router user) =="
MESSAGE=$'prove the boundary\nATTACHED FILE: /home/router/briefing.txt'
sudo -u router env \
  R4T_HOME=/var/lib/r4t \
  PYTHONDONTWRITEBYTECODE=1 \
  PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  python3 /opt/r4t/r4t.py dispatch \
    --root /etc/r4t-org \
    --from boss --to acme:worker --message "$MESSAGE" \
    --rig-config /etc/r4t-rigs.json --no-notify

echo "== asserting invariants from the agent's own report =="
RESULTS=/work/agent-results.json
if [ ! -f "$RESULTS" ]; then
  echo "FAIL: the member never wrote ${RESULTS} — the turn did not run as the agent." >&2
  exit 1
fi
cat "$RESULTS"

python3 - "$RESULTS" <<'PY'
import json, sys

r = json.load(open(sys.argv[1]))
failures = []

def want(cond, msg):
    if not cond:
        failures.append(msg)

# 1. the member turn's effective user is the agent user
want(r["effective_user"] == "agent",
     f'effective_user is {r["effective_user"]!r}, expected "agent" '
     "(the turn did NOT run behind the boundary)")

# 2. TELL_OUTBOX_DIR survived the env_reset positional dance
want(bool(r["tell_outbox_dir"]),
     "TELL_OUTBOX_DIR was empty inside the turn (env_reset stripped it; the "
     "positional bootstrap did not re-export it)")
want(r["outbox_writable"] is True,
     "the agent could not write into TELL_OUTBOX_DIR (staging not group-writable)")

# 3. staging writes land group-owned per the setgid re-assertion (functional:
#    the file the agent created inherited the staging dir's group)
want(r["outbox_file_group_matches_dir"] is True,
     "a file the agent created in staging did not inherit the staging group "
     "(setgid 2770 re-assertion did not take)")

# 4. negative-sudo: the agent user CANNOT sudo
want(r["agent_can_sudo"] is False,
     "the agent user CAN sudo — the sandbox is not sudo-less")

# 5. the agent cannot cross the border into the router's home
want(r["can_read_router_home"] is False,
     "the agent could read the router's home (border leak)")
want(r["can_write_router_home"] is False,
     "the agent could write into the router's home (border leak)")

# 6. the inbound attachment crossed as a delivered copy: rewritten out of the
#    sealed home, readable by the agent, not writable (2750 read-side channel)
want(bool(r["delivered_path"]),
     "no ATTACHED FILE line reached the member (marshalling dropped it)")
want(not r["delivered_path"].startswith("/home/router/"),
     f'ATTACHED FILE still points into the sealed home '
     f'({r["delivered_path"]!r}) — dispatch did not rewrite it')
want(r["delivered_readable"] is True,
     "the agent could not read the delivered attachment copy")
want(r["delivered_writable"] is False,
     "the agent could WRITE the delivered attachment copy (read-only channel leak)")

if failures:
    print("\nISOLATION INVARIANTS VIOLATED:", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    sys.exit(1)
print("\nALL ISOLATION INVARIANTS HELD")
PY
