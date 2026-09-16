"""Devin has no headless quota surface, so this module implements no quota verb.

Every other engine module here exists to answer "how much subscription is
left" without spending a turn. Devin CLI 3000.10.27 gives no way to ask: the
usage views (`/usage`, `/session-stats`) are slash commands inside a session,
`devin auth status` reports only the login, and `devin cloud` manages
resources rather than reporting balance. Nothing short of running a turn
reports the plan.

So the module defines nothing. `engines.capability` looks the verb up with
getattr and finds none, `r4t engine list` prints `[run, check]` for devin, and
`r4t engine devin quota` refuses by naming the engines that can answer. That
refusal is the accurate answer, and it is worth more than a `quota()` that
raises QuotaError on every call: a verb the registry advertises and can never
satisfy is the "declared but not wired" defect the suite keeps finding in its
own surfaces.

If Cognition ships a headless usage verb, this file grows a `quota()` and the
verb appears with no other change — that is what the getattr dispatch buys.
"""
from __future__ import annotations
