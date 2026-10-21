"""Resolve a click or fill target from a CSS selector or plain visible text.

An agent driving a page it cannot see has to name things the way a person
would — "Sign in", not "#login-form > button:nth-of-type(2)". One in-page pass
classifies the argument, and when it is text rather than a selector it returns
the matching elements, each with the simplest selector that uniquely identifies
it, so an ambiguous instruction comes back as a list to choose from instead of
a guess.

A ref printed by `snap` (`e5`, or `f1e5` inside a frame) is tried before any
of that: it names one element playwright-cli itself already knows how to find
— the accessibility snapshot's own `aria-ref` lookup — so it is resolved that
way and handed back as the target verbatim, never run through text or CSS
matching.

Text matching runs two tiers. The page-side pass compares literal text first
(case-insensitive), because that is the common case and needs no Python round
trip. When that finds nothing, the second tier compares after normalising
quote and apostrophe confusables — a chat page renders a curly apostrophe, an
agent types a straight one, and the two should mean the same target.
"""
import re
import unicodedata
from difflib import get_close_matches

from . import plc
from .plc import BrowserError, evaluate_json, js_string

# playwright-cli's own shape for a ref it printed: an optional frame index
# (`f<n>`) followed by an element index (`e<n>`) — see `targetLocators` in
# playwright-core, which matches a target against this exact pattern before
# treating it as a selector.
REF_RE = re.compile(r"^(f\d+)?e\d+$")

_QUOTE_TRANSLATION = str.maketrans({
    "\N{LEFT SINGLE QUOTATION MARK}": "'",
    "\N{RIGHT SINGLE QUOTATION MARK}": "'",
    "\N{SINGLE LOW-9 QUOTATION MARK}": "'",
    "\N{PRIME}": "'",
    "\N{LEFT DOUBLE QUOTATION MARK}": '"',
    "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
    "\N{DOUBLE LOW-9 QUOTATION MARK}": '"',
    "\N{DOUBLE PRIME}": '"',
})


def _normalize(text):
    """Fold quote confusables, apply NFKC, and collapse whitespace."""
    text = unicodedata.normalize("NFKC", (text or "").translate(_QUOTE_TRANSLATION))
    return re.sub(r"\s+", " ", text).strip().lower()

HELPERS_JS = r"""
const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
const QUOTE_FOLD = { '\u2018': "'", '\u2019': "'", '\u201A': "'", '\u2032': "'", '\u201C': '"', '\u201D': '"', '\u201E': '"', '\u2033': '"' };
const foldQuotes = (s) => (s || '').replace(/[\u2018\u2019\u201A\u2032\u201C\u201D\u201E\u2033]/g, (ch) => QUOTE_FOLD[ch]);
const normalize = (s) => foldQuotes(s).normalize('NFKC').replace(/\s+/g, ' ').trim().toLowerCase();
const isVisible = (el) => {
  const r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return false;
  const st = window.getComputedStyle(el);
  return st.visibility !== 'hidden' && st.display !== 'none';
};
const isEnabled = (el) => !el.disabled && el.getAttribute('aria-disabled') !== 'true';
const uniq = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
const cssPath = (el) => {
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1 && node.tagName !== 'HTML') {
    if (node.id && uniq('#' + CSS.escape(node.id))) { parts.unshift('#' + CSS.escape(node.id)); break; }
    let part = node.tagName.toLowerCase();
    const parent = node.parentElement;
    if (parent) {
      const kin = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
      if (kin.length > 1) part += ':nth-of-type(' + (kin.indexOf(node) + 1) + ')';
    }
    parts.unshift(part);
    node = node.parentElement;
  }
  return parts.join(' > ');
};
const bestSelector = (el) => {
  if (el.id && uniq('#' + CSS.escape(el.id))) return '#' + CSS.escape(el.id);
  const testid = el.getAttribute('data-testid');
  if (testid) { const s = '[data-testid=' + JSON.stringify(testid) + ']'; if (uniq(s)) return s; }
  if (el.classList.length) {
    const s = el.tagName.toLowerCase() + '.' + Array.from(el.classList).map((c) => CSS.escape(c)).join('.');
    if (uniq(s)) return s;
  }
  return cssPath(el);
};
const dropAncestors = (list) => list.filter((el) => !list.some((o) => o !== el && el.contains(o)));
"""

CLICK_JS = r"""
let selectorValid = true, selectorCount = 0, selectorVisibleCount = 0, selectorTarget = '';
try {
  const matches = Array.from(document.querySelectorAll(arg));
  selectorCount = matches.length;
  const actionable = matches.filter((el) => isVisible(el) && isEnabled(el));
  selectorVisibleCount = actionable.length;
  selectorTarget = actionable.length ? bestSelector(actionable[0]) : '';
} catch (e) { selectorValid = false; }
const target = norm(arg).toLowerCase();
const normTarget = normalize(arg);
const SEL = 'button, a, [role="button"], [role="link"], [role="tab"], [role="menuitem"], input[type="button"], input[type="submit"], label, [onclick]';
const describe = (el) => ({ tag: el.tagName.toLowerCase(), text: norm(el.textContent), selector: bestSelector(el) });
const tier = (vis, predicate) => dropAncestors(vis.filter(predicate));
const gather = (pool) => {
  const vis = pool.filter(isVisible);
  return {
    exact: tier(vis, (el) => norm(el.textContent).toLowerCase() === target),
    sub: tier(vis, (el) => norm(el.textContent).toLowerCase().includes(target)),
    all: vis,
  };
};
const semantic = gather(Array.from(document.querySelectorAll(SEL)));
let m = semantic;
let pool = semantic.all;
if (m.exact.length === 0 && m.sub.length === 0) {
  const pointer = Array.from(document.querySelectorAll('body *')).filter((el) => window.getComputedStyle(el).cursor === 'pointer');
  const byCursor = gather(pointer);
  m = byCursor;
  pool = Array.from(new Set([...semantic.all, ...byCursor.all]));
}
// The pool stays whole here — a descendant that inherits cursor:pointer, or
// carries only part of an ancestor's text (an icon span, a wrapped word),
// must not delete a still-matching ancestor before the tier is known.
// dropAncestors runs per tier, inside `tier()`, once matches are known — an
// ancestor is suppressed only when a descendant ALSO matches that tier.
const normExact = tier(pool, (el) => normalize(el.textContent) === normTarget);
const normSub = tier(pool, (el) => normalize(el.textContent).includes(normTarget));
return {
  selectorValid, selectorCount, selectorVisibleCount, selectorTarget,
  exact: m.exact.map(describe), substring: m.sub.map(describe),
  normExact: normExact.map(describe), normSub: normSub.map(describe),
  candidates: pool.map(describe),
};
"""

FILL_JS = r"""
const FILLABLE = 'input:not([type="button"]):not([type="submit"]):not([type="checkbox"]):not([type="radio"]):not([type="hidden"]), textarea, select, [contenteditable=""], [contenteditable="true"]';
let selectorValid = true, selectorCount = 0, selectorVisibleCount = 0, selectorTarget = '';
try {
  const matches = Array.from(document.querySelectorAll(arg)).filter((el) => el.matches(FILLABLE));
  selectorCount = matches.length;
  const actionable = matches.filter((el) => isVisible(el) && isEnabled(el));
  selectorVisibleCount = actionable.length;
  selectorTarget = actionable.length ? bestSelector(actionable[0]) : '';
} catch (e) { selectorValid = false; }
const target = norm(arg).toLowerCase();
const labelsFor = (el) => {
  const labels = [];
  const add = (value) => { const label = norm(value); if (label && !labels.includes(label)) labels.push(label); };
  add(el.getAttribute('aria-label'));
  const ref = el.getAttribute('aria-labelledby');
  if (ref) { const l = document.getElementById(ref); if (l) add(l.textContent); }
  if (el.id) { const l = document.querySelector('label[for=' + JSON.stringify(el.id) + ']'); if (l) add(l.textContent); }
  const wrap = el.closest('label');
  if (wrap) add(wrap.textContent);
  add(el.getAttribute('placeholder'));
  return labels;
};
const withLabel = Array.from(document.querySelectorAll(FILLABLE)).filter(isVisible)
  .map((el) => ({ el, labels: labelsFor(el) })).filter((x) => x.labels.length);
const mk = (x) => ({ tag: x.el.tagName.toLowerCase(), text: x.label, selector: bestSelector(x.el) });
const exact = withLabel.map((x) => ({ ...x, label: x.labels.find((label) => label.toLowerCase() === target) })).filter((x) => x.label);
const sub = withLabel.map((x) => ({ ...x, label: x.labels.find((label) => label.toLowerCase().includes(target)) })).filter((x) => x.label);
const candidates = [];
withLabel.forEach((x) => {
  const selector = bestSelector(x.el);
  const tag = x.el.tagName.toLowerCase();
  x.labels.forEach((label) => candidates.push({ tag, text: label, selector }));
});
return { selectorValid, selectorCount, selectorVisibleCount, selectorTarget, exact: exact.map(mk), substring: sub.map(mk), candidates };
"""


def _classify(seat, argument, body_js):
    expression = f"((arg) => {{{HELPERS_JS}{body_js}}})('{js_string(argument)}')"
    return evaluate_json(seat, expression)


def _dedupe_by_selector(candidates):
    """One entry per control.

    A control can carry several labels (fill) or sit in more than one
    candidate pool (click's semantic/pointer union), and several of its texts
    can match the same query — that is one match, not several, so ambiguity
    is judged on distinct selectors, not on how many texts matched.
    """
    seen = {}
    for candidate in candidates:
        seen.setdefault(candidate["selector"], candidate)
    return list(seen.values())


def _normalized_matches(argument, candidates):
    """Exact-after-normalisation first, substring-after-normalisation second."""
    target = _normalize(argument)
    exact = _dedupe_by_selector([c for c in candidates if _normalize(c["text"]) == target])
    if exact:
        return exact
    return _dedupe_by_selector([c for c in candidates if target in _normalize(c["text"])])


def _closest_hint(argument, candidates):
    texts = [c["text"] for c in candidates if c.get("text")]
    match = get_close_matches(argument, texts, n=1, cutoff=0.4)
    return f"; closest: {match[0]!r}" if match else ""


def _normalized_tier(argument, verdict):
    """The normalised-match candidates, however this verb computed them.

    click resolves its own normalised tiers in-page (normExact/normSub),
    tier by tier, so an ancestor is dropped only when a descendant matches
    that same tier — a plain dropAncestors over the raw pool would delete a
    matching ancestor for holding only part of the text (an icon, a wrapped
    word). fill has no such ancestor problem — a label is not a DOM ancestor
    of its control — so it still normalises here, over the flat pool.
    """
    if "normExact" in verdict:
        return verdict["normExact"] or verdict["normSub"]
    return _normalized_matches(argument, verdict.get("candidates", []))


def _pick(argument, verdict, noun):
    """One match acts; zero or many is an error naming what to do next."""
    candidates = verdict["exact"] or verdict["substring"]
    if not candidates:
        candidates = _normalized_tier(argument, verdict)
    if len(candidates) == 1:
        return candidates[0]["selector"]
    if not candidates:
        hint = _closest_hint(argument, verdict.get("candidates", []))
        raise BrowserError(
            f"{noun}: nothing visible matching {argument!r} — try `snap`, or pass a CSS selector{hint}"
        )
    lines = [f"{noun}: {len(candidates)} elements match {argument!r} — pick one by selector:"]
    for candidate in candidates:
        text = candidate["text"][:60]
        lines.append(f'  {candidate["tag"]}  "{text}"  {candidate["selector"]}')
    raise BrowserError("\n".join(lines))


def _resolve_ref(seat, ref):
    """A ref from the last `snap` of this session, resolved playwright-cli's
    own way: an `aria-ref` lookup against its last accessibility snapshot
    (see `targetLocators` in playwright-core), not a page-side search.

    A no-op `eval` against the ref exercises exactly that lookup without
    clicking or filling anything — playwright-cli resolves the target the
    same way for every verb, so confirming it here means every target-taking
    verb gets the same clear failure for a ref that fell off the page.
    """
    try:
        plc.run(seat, "eval", "() => true", ref, timeout=10)
    except BrowserError as exc:
        if "not found in the current page snapshot" in str(exc):
            raise BrowserError(
                f"ref {ref} is not on the page any more — snap again"
            ) from exc
        raise
    return ref


def _target(seat, argument, body_js, noun):
    if REF_RE.match(argument):
        return _resolve_ref(seat, argument)
    verdict = _classify(seat, argument, body_js)
    if verdict["selectorValid"] and verdict["selectorCount"] >= 1:
        if verdict["selectorVisibleCount"] < 1:
            raise BrowserError(f"{noun}: selector {argument!r} matched nothing visible and enabled")
        return verdict["selectorTarget"] or argument
    return _pick(argument, verdict, noun)


def click_target(seat, argument):
    return _target(seat, argument, CLICK_JS, "click")


def fill_target(seat, argument):
    return _target(seat, argument, FILL_JS, "fill")
