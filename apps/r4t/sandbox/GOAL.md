# Goal: terminal battleship

Create **battleship.py** in this directory (the repo root — your cwd).

Requirements:
- 5×5 grid, 3 single-cell ships at fixed positions
- Read guesses from stdin: one `row col` per line (integers 0–4)
- Print `HIT row col` or `MISS row col` for each guess
- When all 3 ships are hit: print a win message and **exit 0**
- If stdin ends before all ships are hit: print a lose message and **exit 1**

Who does what:
- **Dev** writes battleship.py here, then tells Tester
- **Tester** runs `python3 battleship.py` with test input, tells Lead whether it passed or failed
- **Lead** delegates to Dev, then Tester, then answers the human after a pass
