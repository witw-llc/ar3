# scratch notes — the silo roster, thursday

tell only sends when the member actually RUNS it. Wren printed
`tell you "Inkwell"` as text and it vanished — cleaned off as terminal
chrome, anything under about 80 characters goes.

budgets: rig_budget_max lives on the rig and is machine-global. Set helper
to 1 and Moss went RESTING with the message queued, ready in ~59 min.
Nothing was dropped — gates govern when a member runs, never whether a
message survives.

r4t idle drains the queue by hand instead of waiting out the refill.

2>/dev/null on a harness invoke matters: the progress UI paints on stderr,
the finished answer is alone on stdout.

flush = dump turn, then retire the conversation, then archive the history
log. STATUS.md is what a refounded member reads on the way back up.

two members driving the same CLI in one Workdir share one conversation.
give every member its own Workdir.
