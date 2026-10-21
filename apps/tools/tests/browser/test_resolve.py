import pytest

from browser import plc
from browser import resolve


def _page(monkeypatch, verdict):
    monkeypatch.setattr(resolve, "evaluate_json", lambda seat, expression: verdict)


def _verdict(**overrides):
    base = {
        "selectorValid": False,
        "selectorCount": 0,
        "selectorVisibleCount": 0,
        "selectorTarget": "",
        "exact": [],
        "substring": [],
        "candidates": [],
    }
    base.update(overrides)
    return base


def test_a_css_selector_that_matches_is_used_as_given(monkeypatch):
    _page(monkeypatch, _verdict(
        selectorValid=True, selectorCount=1, selectorVisibleCount=1, selectorTarget="#login",
    ))
    assert resolve.click_target("seat", "#login") == "#login"


def test_a_selector_matching_only_hidden_elements_is_an_error(monkeypatch):
    _page(monkeypatch, _verdict(selectorValid=True, selectorCount=2, selectorVisibleCount=0))
    with pytest.raises(plc.BrowserError, match="nothing visible"):
        resolve.click_target("seat", ".hidden")


def test_visible_text_resolves_to_a_selector(monkeypatch):
    _page(monkeypatch, _verdict(
        exact=[{"tag": "button", "text": "Sign in", "selector": "#signin"}],
    ))
    assert resolve.click_target("seat", "Sign in") == "#signin"


def test_substring_is_the_fallback_when_nothing_matches_exactly(monkeypatch):
    _page(monkeypatch, _verdict(
        substring=[{"tag": "button", "text": "Sign in now", "selector": "#signin"}],
    ))
    assert resolve.click_target("seat", "Sign in") == "#signin"


def test_no_match_asks_for_a_snapshot_rather_than_guessing(monkeypatch):
    _page(monkeypatch, _verdict())
    with pytest.raises(plc.BrowserError, match="snap"):
        resolve.click_target("seat", "Sign in")


def test_several_matches_come_back_as_selectors_to_choose_from(monkeypatch):
    _page(monkeypatch, _verdict(exact=[
        {"tag": "button", "text": "Next", "selector": "#a"},
        {"tag": "a", "text": "Next", "selector": "#b"},
    ]))
    with pytest.raises(plc.BrowserError) as raised:
        resolve.click_target("seat", "Next")
    message = str(raised.value)
    assert "2 elements match" in message
    assert "#a" in message and "#b" in message


def test_fill_resolves_by_label(monkeypatch):
    _page(monkeypatch, _verdict(
        exact=[{"tag": "input", "text": "Password", "selector": "#pw"}],
    ))
    assert resolve.fill_target("seat", "Password") == "#pw"


# click resolves its normalised tiers (normExact/normSub) in-page now \u2014 see
# CLICK_JS \u2014 so a verdict standing in for a real page carries those fields,
# not the flat `candidates` list. `candidates` remains, unconsumed by
# matching, only as the near-miss hint's pool.


def test_straight_apostrophe_typed_matches_a_curly_one_rendered(monkeypatch):
    _page(monkeypatch, _verdict(
        normExact=[{"tag": "a", "text": "Mrs. Example\u2019s class", "selector": "#c1"}],
        normSub=[],
    ))
    assert resolve.click_target("seat", "Mrs. Example's class") == "#c1"


def test_curly_apostrophe_typed_matches_a_straight_one_rendered(monkeypatch):
    _page(monkeypatch, _verdict(
        normExact=[{"tag": "a", "text": "Mrs. Example's class", "selector": "#c1"}],
        normSub=[],
    ))
    assert resolve.click_target("seat", "Mrs. Example\u2019s class") == "#c1"


def test_curly_double_quote_typed_matches_a_straight_one_rendered(monkeypatch):
    _page(monkeypatch, _verdict(
        normExact=[{"tag": "span", "text": 'the "big" one', "selector": "#c2"}],
        normSub=[],
    ))
    assert resolve.click_target("seat", "the \u201cbig\u201d one") == "#c2"


def test_straight_double_quote_typed_matches_a_curly_one_rendered(monkeypatch):
    _page(monkeypatch, _verdict(
        normExact=[{"tag": "span", "text": "the \u201cbig\u201d one", "selector": "#c2"}],
        normSub=[],
    ))
    assert resolve.click_target("seat", 'the "big" one') == "#c2"


def test_no_match_names_the_closest_visible_candidate(monkeypatch):
    _page(monkeypatch, _verdict(
        normExact=[],
        normSub=[],
        candidates=[
            {"tag": "a", "text": "Mrs. Example's class", "selector": "#c1"},
        ],
    ))
    with pytest.raises(plc.BrowserError) as raised:
        resolve.click_target("seat", "Mrs. Ecksample's class")
    message = str(raised.value)
    assert "nothing visible matching" in message
    assert "closest:" in message
    assert "Mrs. Example's class" in message


def test_exact_match_wins_over_a_normalised_one_when_both_are_visible(monkeypatch):
    _page(monkeypatch, _verdict(
        exact=[{"tag": "a", "text": "Mrs. Example's class", "selector": "#straight"}],
        normExact=[
            {"tag": "a", "text": "Mrs. Example's class", "selector": "#straight"},
            {"tag": "a", "text": "Mrs. Example\u2019s class", "selector": "#curly"},
        ],
    ))
    assert resolve.click_target("seat", "Mrs. Example's class") == "#straight"


def test_button_in_the_semantic_pool_survives_a_pointer_fallback_straight_typed(monkeypatch):
    _page(monkeypatch, _verdict(
        normExact=[{"tag": "button", "text": "Owner\u2019s page", "selector": "#target"}],
        normSub=[],
    ))
    assert resolve.click_target("seat", "Owner's page") == "#target"


def test_button_in_the_semantic_pool_survives_a_pointer_fallback_curly_typed(monkeypatch):
    _page(monkeypatch, _verdict(
        normExact=[{"tag": "button", "text": "Owner's page", "selector": "#target"}],
        normSub=[],
    ))
    assert resolve.click_target("seat", "Owner\u2019s page") == "#target"


# R1, second review pass: dropAncestors on the whole pool (not per tier) let
# a pointer-inheriting *child* wipe out a still-matching *ancestor* \u2014 the
# child's isVisible/cursor status put it in the same pool as its parent, and
# a blanket dropAncestors treated that as "redundant," regardless of whether
# the child's own text matched anything. click now computes normExact/normSub
# per tier, dropping an ancestor only when a descendant ALSO matches that
# tier (see the `tier()` helper in CLICK_JS).
#
# `candidates` below models the pre-fix pool (the ancestor already deleted,
# because its pointer-cursor child shared its pool) \u2014 the pre-fix `_pick`
# read only `candidates`, so resolving against it is genuinely red. `normExact`
# /`normSub` model the fixed, tier-aware output.


def test_ancestor_survives_a_matching_pointer_child_with_partial_text_straight_typed(monkeypatch):
    # <button id="target" style="cursor:pointer">Owner\u2019s <span>page</span></button>
    # click "Owner's page" (straight): literal tier misses (button's own text
    # is curly), the span inherits cursor:pointer and lands in the same pool,
    # and a blanket dropAncestors removed the button for it \u2014 leaving only
    # the span's partial text ("page") to normalise against.
    _page(monkeypatch, _verdict(
        candidates=[{"tag": "span", "text": "page", "selector": "#target > span"}],
        normExact=[{"tag": "button", "text": "Owner\u2019s page", "selector": "#target"}],
        normSub=[],
    ))
    assert resolve.click_target("seat", "Owner's page") == "#target"


def test_ancestor_survives_a_matching_pointer_child_with_partial_text_curly_typed(monkeypatch):
    # Same markup, curly-typed query: the button's own text matches literally
    # (tier 1), so this direction never touches the pointer pool or
    # normalisation \u2014 it already worked, and must keep working.
    _page(monkeypatch, _verdict(
        exact=[{"tag": "button", "text": "Owner\u2019s page", "selector": "#target"}],
    ))
    assert resolve.click_target("seat", "Owner\u2019s page") == "#target"


def test_ancestor_survives_a_matching_pointer_icon_child_straight_typed(monkeypatch):
    # <button id="target" style="cursor:pointer">Owner\u2019s page
    #   <span aria-hidden="true">\u2197</span></button>
    # Straight-typed query: literal tier misses (button's full text carries
    # the trailing icon and a curly apostrophe). The aria-hidden icon span
    # inherits cursor:pointer and shares the button's pool; a blanket
    # dropAncestors removed the button for it, leaving only the icon glyph
    # to normalise against.
    _page(monkeypatch, _verdict(
        candidates=[{"tag": "span", "text": "\u2197", "selector": "#target > span"}],
        normExact=[],
        normSub=[{"tag": "button", "text": "Owner\u2019s page \u2197", "selector": "#target"}],
    ))
    assert resolve.click_target("seat", "Owner's page") == "#target"


def test_ancestor_survives_a_matching_pointer_icon_child_curly_typed(monkeypatch):
    # Same markup, curly-typed query: "Owner\u2019s page" is a literal
    # substring of the button's full text ("Owner\u2019s page \u2197"), so
    # tier 1 already resolves it \u2014 this direction already worked.
    _page(monkeypatch, _verdict(
        substring=[{"tag": "button", "text": "Owner\u2019s page \u2197", "selector": "#target"}],
    ))
    assert resolve.click_target("seat", "Owner\u2019s page") == "#target"


def test_a_fully_matching_descendant_still_wins_over_its_matching_ancestor(monkeypatch):
    # <button id="target"><span id="inner">Owner\u2019s page</span></button>
    # Here the descendant carries the *whole* text, so both ancestor and
    # descendant match the same normalised tier \u2014 this is the case
    # dropAncestors exists for, and per-tier matching must still resolve it
    # to the innermost element, not read it as two elements matching.
    _page(monkeypatch, _verdict(
        normExact=[{"tag": "span", "text": "Owner\u2019s page", "selector": "#target > span"}],
        normSub=[],
    ))
    assert resolve.click_target("seat", "Owner's page") == "#target > span"


# R2 (PR #9 review): a fillable control can carry several labels (aria-label,
# label[for], a wrapping <label>, a placeholder). The old code kept only the
# first one in `candidates`, so a query that only matched a later label found
# nothing once literal matching failed. All labels must reach normalisation,
# and matching by more than one of a control's labels must still count as one
# control, not an ambiguous pick.


def test_fill_matches_by_a_later_label_once_normalised_straight_typed(monkeypatch):
    _page(monkeypatch, _verdict(
        candidates=[
            {"tag": "input", "text": "Account holder", "selector": "#target"},
            {"tag": "input", "text": "Owner\u2019s name", "selector": "#target"},
        ],
    ))
    assert resolve.fill_target("seat", "Owner's name") == "#target"


def test_fill_matches_by_a_later_label_once_normalised_curly_typed(monkeypatch):
    _page(monkeypatch, _verdict(
        candidates=[
            {"tag": "input", "text": "Account holder", "selector": "#target"},
            {"tag": "input", "text": "Owner's name", "selector": "#target"},
        ],
    ))
    assert resolve.fill_target("seat", "Owner\u2019s name") == "#target"


def test_two_labels_matching_the_same_control_is_one_match_not_two(monkeypatch):
    # Both "Owner's name" and "Owner's account" contain "owner", so a
    # substring query against either normalised label resolves the same
    # control \u2014 that must collapse to a single candidate, not read as
    # "2 elements match" for what is in fact one input.
    _page(monkeypatch, _verdict(
        candidates=[
            {"tag": "input", "text": "Owner's name", "selector": "#target"},
            {"tag": "input", "text": "Owner's account", "selector": "#target"},
        ],
    ))
    assert resolve.fill_target("seat", "owner") == "#target"


def test_normalised_exact_beats_normalised_substring(monkeypatch):
    _page(monkeypatch, _verdict(
        normExact=[{"tag": "a", "text": "Owner\u2019s page", "selector": "#exact"}],
        normSub=[
            {"tag": "a", "text": "Owner\u2019s page", "selector": "#exact"},
            {"tag": "a", "text": "Owner\u2019s page and more", "selector": "#sub"},
        ],
    ))
    assert resolve.click_target("seat", "Owner's page") == "#exact"


def test_css_selector_resolution_is_untouched_by_normalisation(monkeypatch):
    _page(monkeypatch, _verdict(
        selectorValid=True, selectorCount=1, selectorVisibleCount=1, selectorTarget="#login",
        candidates=[{"tag": "button", "text": "Owner\u2019s page", "selector": "#other"}],
    ))
    assert resolve.click_target("seat", "#login") == "#login"


# #10: a ref printed by `snap` (`e5`, or `f1e5` inside a frame) must be
# accepted as a target, resolved playwright-cli's own way (an `aria-ref`
# lookup, not our page-side text/CSS classifier) before anything else is
# tried.


def _fail_classify(monkeypatch):
    """If ref handling falls through to text/CSS classification, fail loudly
    rather than let a coincidental match hide the bug."""
    def _boom(seat, expression):
        raise AssertionError("a ref must not reach the text/CSS classifier")
    monkeypatch.setattr(resolve, "evaluate_json", _boom)


def test_a_ref_from_the_last_snap_resolves(monkeypatch):
    _fail_classify(monkeypatch)
    calls = []
    monkeypatch.setattr(
        resolve.plc, "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "### Result\ntrue\n",
    )
    assert resolve.click_target("seat", "e5") == "e5"
    args, kwargs = calls[0]
    assert args == ("seat", "eval", "() => true", "e5")
    assert kwargs.get("timeout") == 10


def test_a_ref_with_a_frame_prefix_resolves(monkeypatch):
    _fail_classify(monkeypatch)
    monkeypatch.setattr(resolve.plc, "run", lambda *a, **k: "### Result\ntrue\n")
    assert resolve.click_target("seat", "f133e172") == "f133e172"
    assert resolve.fill_target("seat", "f1e5") == "f1e5"


def test_a_stale_ref_names_the_fix(monkeypatch):
    _fail_classify(monkeypatch)

    def _stale(*args, **kwargs):
        raise plc.BrowserError(
            "Error: Ref f133e172 not found in the current page snapshot. "
            "Try capturing new snapshot."
        )

    monkeypatch.setattr(resolve.plc, "run", _stale)
    with pytest.raises(plc.BrowserError) as raised:
        resolve.click_target("seat", "f133e172")
    message = str(raised.value)
    assert message == "ref f133e172 is not on the page any more \u2014 snap again"
    assert "nothing visible matching" not in message


def test_a_playwright_error_unrelated_to_a_stale_ref_is_not_reworded(monkeypatch):
    _fail_classify(monkeypatch)

    def _boom(*args, **kwargs):
        raise plc.BrowserError("playwright-cli timed out after 10s")

    monkeypatch.setattr(resolve.plc, "run", _boom)
    with pytest.raises(plc.BrowserError, match="timed out"):
        resolve.click_target("seat", "e5")


def test_text_that_merely_looks_like_a_word_is_not_mistaken_for_a_ref(monkeypatch):
    """Only playwright-cli's own ref shape short-circuits to `_resolve_ref` \u2014
    ordinary text and CSS selectors are untouched, including one that starts
    with the letter e or f."""
    _page(monkeypatch, _verdict(
        exact=[{"tag": "button", "text": "export", "selector": "#export"}],
    ))
    assert resolve.click_target("seat", "export") == "#export"
