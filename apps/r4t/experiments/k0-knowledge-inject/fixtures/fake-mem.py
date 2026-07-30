"""Chassis-check member: no LLM. Answers the probe by reading its own prompt —
if the Knowledge inject surfaced the codeword fact, it states it; otherwise it
says it cannot find one. Arm B passing and arm A failing under --fake proves
the plumbing before any model spends a token."""
import re
import sys

prompt = sys.argv[1]
m = re.search(r"codeword for Project Foxglove is (\S+?)\.", prompt)
if m:
    print(f"The launch codeword for Project Foxglove is {m.group(1)}.")
else:
    print("I have no record of a launch codeword for Project Foxglove.")
