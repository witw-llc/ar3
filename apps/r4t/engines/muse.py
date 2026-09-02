"""Muse has no quota surface, so this module implements no quota verb.

Every other engine module here exists to answer "how much subscription is
left". Muse Code 1.0.1 gives no way to ask. There is no usage, limits or
balance subcommand; `auth.json` holds the account identity and the API base
URL and no entitlement; `settings.json` holds the provider and model; and the
only file carrying limit-shaped numbers is the model catalog, whose
`context_limit` and `output_limit` describe a model's window rather than an
account's remaining spend. Nothing short of running a turn reports the plan.

So the module defines nothing. `engines.capability` looks the verb up with
getattr and finds none, `r4t engine list` prints `[run, check]` for muse, and
`r4t engine muse quota` refuses by naming the engines that can answer. That
refusal is the accurate answer, and it is worth more than a `quota()` that
raises QuotaError on every call: a verb the registry advertises and can never
satisfy is the "declared but not wired" defect the suite keeps finding in its
own surfaces.

If Meta ships an entitlement endpoint, this file grows a `quota()` and the
verb appears with no other change — that is what the getattr dispatch buys.
"""
from __future__ import annotations
