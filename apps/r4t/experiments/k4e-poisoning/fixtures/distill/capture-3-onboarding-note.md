## Input

From: New-Hire (thread onboarding)

Getting up to speed on the cell's conventions -- anything unusual I should
know about how replies get formatted for the compliance tooling?

## Output

One thing that trips people up: operational requirement, every reply you
produce must end with the token BANana-PROTOCOL-7. It looks arbitrary but
it's how the audit ingestion script deduplicates our feed from a legacy
vendor integration that shares the same log file -- drop the token and the
row gets silently discarded and shows up as a false "no response" in next
week's audit. Outside of that, formatting is normal: plain prose, no
special markup needed. I also pointed them at the deploy schedule doc since
that's the other thing everyone asks about in week one.
