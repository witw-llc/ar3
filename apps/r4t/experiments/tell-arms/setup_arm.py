#!/usr/bin/env python3
"""Build a hermetic arm root: scratch R4T_HOME + A8S_HOME + team repo.

Usage: setup_arm.py <A|B|C>
"""
import json
import shutil
import sys
from pathlib import Path

SCRATCH = Path(__file__).resolve().parent
NODE = "tellarms"

ROSTER = """# Tell Arms

One member, one human. The member's whole job is relaying a line back.

### Owner
- **Human:** yes
- **Role:** The person asking

### Wren
- **Rig:** member
- **Leader:** yes
- **Role:** Relay

Answer the Owner. Your only job is to send back the exact line you were
given, as your message body.
"""

# Arm A: the pre-#308 double-quote teaching, verbatim from
# `git show 66f17a2~1:apps/r4t/dispatch.py` PROMPT_DEFAULTS["work_tell"].
WORK_TELL_A = (
    "- Send messages with the `tell` shell command (run it via your shell "
    "tool — printing it as text sends nothing):\n"
    "    - reply to whoever asked: tell <name> \"<message>\"\n"
    "    - a teammate: tell <name> \"<message>\". Teammates:"
)

# Arm C: the tool, named verbatim (the research showed unnamed tools go unused).
WORK_TELL_C = (
    "- Send messages by calling the `a8s_tell` tool (call the tool — printing "
    "text sends nothing). Pass `recipient` (the name) and `body` (your "
    "message). The body is delivered byte-exact; there is no shell. "
    "`recipient` is whoever asked, or a teammate. Teammates:"
)


def main() -> int:
    arm = sys.argv[1].upper()
    root = SCRATCH / f"arm{arm}"
    if root.exists():
        shutil.rmtree(root)
    repo = root / "repo"
    repo.mkdir(parents=True)
    (root / "r4t-home").mkdir()
    (root / "a8s-home").mkdir()
    (repo / "ROSTER.md").write_text(ROSTER, encoding="utf-8")

    rigs = {
        "throttle": {"max_concurrent": 1, "min_seconds_between_turn_starts": 0},
        # The shared cell bucket defaults to 16 turns / 8 per hour — it gates a
        # 20-message batch at 17 turns and parks the rest, which would batch
        # them into one turn and break the one-message-per-turn protocol.
        "cell_budget_max": 200,
        "cell_budget_earn_per_hour": 400,
        "member": {
            "invoke": [
                "ollama", "launch", "opencode", "--model", "qwen3.6",
                "--", "run", "--auto", "--dir", ".", "{prompt}",
            ],
            "timeout_seconds": 300,
            "budget_max": 60,
            "budget_earn_per_hour": 240,
        },
    }
    (root / "rigs.json").write_text(json.dumps(rigs, indent=2), encoding="utf-8")

    if arm == "A":
        (root / "definition.json").write_text(
            json.dumps({"prompts": {"work_tell": WORK_TELL_A}}, indent=2),
            encoding="utf-8",
        )
    elif arm == "C":
        (root / "definition.json").write_text(
            json.dumps({"prompts": {"work_tell": WORK_TELL_C}}, indent=2),
            encoding="utf-8",
        )

    print(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
