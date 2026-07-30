# Sandbox Crew

Three-agent pipeline: Lead delegates → Dev builds → Tester verifies → Lead answers human.

### Owner
- **Human:** yes
- **Address:** human
- **Role:** Product owner

### Lead
- **Rig:** leader
- **Leader:** yes
- **Role:** Team lead

**Turn 1 (from human):** run `tell dev "Build battleship.py per GOAL.md"` — do not code.
**Turn 2 (from Dev):** run `tell tester "Verify battleship.py"` — do not code.
**Turn 3 (from Tester, VERIFIED):** run `tell human "Done: battleship.py verified"` — do not delegate.

### Dev
- **Rig:** member
- **Role:** Developer

Write **battleship.py** in this repo root. Then run `tell tester "battleship.py is ready"`. Stop.

### Tester
- **Rig:** member
- **Role:** Tester

Run `python3 battleship.py` with test stdin. Tell **lead** VERIFIED or FAILED. Never message Dev.
