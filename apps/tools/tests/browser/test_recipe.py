"""Site recipes: the declaration, the binding of arguments, the loader, the listing and the runner.

The runner tests drive a recipe through `commands.run_script`, as a `recipe` line in a script does, on
a fake browser (fake_browser.py). The fixture recipe and site get their own end-to-end run in
test_recipe_live.py.
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from browser import commands
from browser import plc
from browser import recipe
from browser import secretstore
from browser import session
from browser.recipe import Arg, NeedsPerson, Opt, Recipe, RecipeError, Result, UsageError

from fake_browser import FakeBrowser, run_script, static_library, write_recipes

VALUE = "hunter2-value"
ORIGIN = "https://app.example.com"


def shop(**kwargs):
    kwargs.setdefault("origins", [ORIGIN])
    return Recipe("shop", "A shop", **kwargs)


@pytest.fixture
def browser(monkeypatch):
    fake = FakeBrowser(monkeypatch)
    fake.url = f"{ORIGIN}/"
    return fake


def run(rec, script, **kwargs):
    return run_script(script, static_library(rec), **kwargs)


def output(result, index=0):
    return result.steps[index].get("output", "")


# --- the declaration -------------------------------------------------------------------------------


def test_a_recipe_declares_its_site_and_normalizes_its_origins():
    rec = Recipe(
        "shop", " The shop ", origins=["HTTPS://App.Example.com:443", "https://*.example.org", ORIGIN],
        aliases=["sh"], env=["SHOP_BASE"], secrets=["SHOP_USER", "SHOP_PASS"],
    )
    assert (rec.name, rec.title, rec.aliases, rec.env, rec.secrets) == (
        "shop", "The shop", ["sh"], ["SHOP_BASE"], ["SHOP_USER", "SHOP_PASS"],
    )
    assert rec.origins == [ORIGIN, "https://*.example.org"]
    assert rec.origin_parts[0] == {"scheme": "https", "host": "app.example.com", "port": "", "wildcard": False}
    assert rec.origin_parts[1]["wildcard"] is True


def test_a_recipe_takes_a_single_origin_as_text():
    assert Recipe("shop", "A shop", origins=ORIGIN).origins == [ORIGIN]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"name": "Shop"}, "lower case"),
        ({"name": "1shop"}, "lower case"),
        ({"name": "a" * 33}, "lower case"),
        ({"name": "sho p"}, "lower case"),
        ({"title": ""}, "one line"),
        ({"title": "two\nlines"}, "one line"),
        ({"origins": []}, "at least one origin"),
        ({"origins": ["http://127.0.0.1:*"]}, "port is a number"),
        ({"origins": ["https://*.com"]}, "at least two labels"),
        ({"origins": ["https://app.example.com/path"]}, "no path"),
        ({"origins": ["ftp://app.example.com"]}, "http:// or https://"),
        ({"aliases": ["shop"]}, "name, not an alias"),
        ({"aliases": ["sh", "sh"]}, "distinct alias"),
        ({"aliases": ["Sh"]}, "valid, distinct alias"),
        ({"env": ["lower"]}, "valid, distinct env name"),
        ({"env": ["A", "A"]}, "valid, distinct env name"),
        ({"secrets": ["bad-name"]}, "valid, distinct secret name"),
    ],
)
def test_a_bad_site_declaration_is_refused(kwargs, message):
    base = {"name": "shop", "title": "A shop", "origins": [ORIGIN]}
    with pytest.raises(RecipeError, match=message):
        Recipe(**{**base, **kwargs})


def test_origins_use_the_syntax_of_a_secret_origin():
    """One grammar in the tool: what `browser secret set --origin` refuses, a recipe refuses."""
    for origin in ("https://*.example.org", "http://127.0.0.1:3000", "http://[::1]:3000", "http://*.localhost:3000"):
        assert Recipe("shop", "A shop", origins=[origin]).origins == [secretstore.parse_origin(origin)]


@pytest.mark.parametrize(
    "build, message",
    [
        (lambda r: r.steps("help", help="x", steps="url"), "built in"),
        (lambda r: r.steps("check", help="x", steps="url"), "built in"),
        (lambda r: r.steps("status", help="x", steps="url"), "built in"),
        (lambda r: r.steps("Bad", help="x", steps="url"), "a verb name"),
        (lambda r: r.steps("a b", help="x", steps="url"), "a verb name"),
        (lambda r: r.steps("go", kind="delete", help="x", steps="url"), "read or write"),
        (lambda r: r.steps("go", signin="yes", help="x", steps="url"), "True or False"),
        (lambda r: r.steps("go", help="  ", steps="url"), "says what the verb does"),
        (lambda r: r.steps("go", help="x", steps=""), "no command"),
        (lambda r: r.steps("go", help="x", steps="# only a comment\n\n"), "no command"),
        (lambda r: r.steps("go", help="x", steps="url {nope}"), "neither an argument nor a declared env name"),
        (lambda r: r.steps("go", help="x", steps="url {"), "single brace"),
        (lambda r: r.steps("go", help="x", steps="url }"), "single brace"),
        (lambda r: r.steps("go", help="x", steps="{verb} here"), "command word"),
        (lambda r: r.steps("go", help="x", steps="eval x <<END"), "cannot open a block"),
        (lambda r: r.steps("go", help="x", steps="go 'unterminated"), "quotation"),
        (lambda r: r.steps("go", help="x", steps=7), "steps is text"),
        (lambda r: r.steps("go", help="x", steps="url", args=[Opt("--n", type=int)], example="--n x"), "does not fit"),
        (lambda r: r.steps("go", help="x", steps="url", args=[Opt("--n", required=True)], example=""), "does not fit"),
        (lambda r: r.steps("go", help="x", steps="url", example="--nope"), "does not fit"),
        (lambda r: r.steps("go", help="x", steps="url", example="'open"), "does not fit"),
    ],
)
def test_a_bad_verb_declaration_is_refused_and_names_the_recipe(build, message):
    rec = shop(env=["SHOP_BASE"])
    with pytest.raises(RecipeError, match=message) as refused:
        build(rec)
    assert "recipe shop" in str(refused.value)


@pytest.mark.parametrize(
    "specs, message",
    [
        ([Opt("limit")], "flags that start with -"),
        ([Opt()], "flags that start with -"),
        ([Opt("--n", nope=1)], "unexpected keyword"),
        ([Opt("--n", type=int, action="store_true")], "unexpected keyword|type"),
        ([Opt("--n"), Opt("--n")], "conflicting option string"),
        ([Opt("--n"), Opt("-n", dest="n")], "name of its own"),
        ([Arg("Title")], "lower case letters"),
        ([Arg("-x")], "lower case letters"),
        ([Arg("title", required=True)], "required"),
        ([Arg("title", rest=True, type=int)], "rest argument is text"),
        ([Arg("title", rest=True, choices=["a"])], "rest argument is text"),
        ([Arg("title", rest=True, nargs=2)], "nargs"),
        ([Arg("title"), Arg("title")], "name of its own"),
        (["--limit"], "Arg and Opt"),
        ([Opt("--HTML")], "name of its own"),
    ],
)
def test_a_bad_argument_declaration_is_refused(specs, message):
    with pytest.raises(RecipeError, match=message):
        shop().steps("go", help="x", steps="url", args=specs)


def test_a_verb_is_declared_once_and_a_recipe_needs_a_verb_and_one_probe():
    rec = shop()
    rec.steps("go", help="x", steps="url")
    with pytest.raises(RecipeError, match="declared twice"):
        rec.steps("go", help="x", steps="url")

    @rec.status
    def status(page):
        return "in"

    with pytest.raises(RecipeError, match="one status probe"):
        rec.status(status)
    with pytest.raises(RecipeError, match="declares no verb"):
        shop().validate()


def test_a_verb_that_needs_sign_in_needs_a_probe():
    rec = shop()
    rec.steps("go", signin=True, help="x", steps="url")
    with pytest.raises(RecipeError, match="needs a @recipe.status probe"):
        rec.validate()

    @rec.status
    def status(page):
        return "in"

    rec.validate()


def test_the_decorators_hand_back_the_function():
    rec = shop()

    @rec.verb("hello", help="Say hello.")
    def hello(page, args):
        return "hi"

    @rec.status
    def status(page):
        return "in"

    assert hello(None, None) == "hi" and status(None) == "in"
    assert list(rec.verbs) == ["hello"]


def test_defaults_are_a_read_verb_that_needs_no_sign_in():
    rec = shop()
    rec.steps("go", help="x", steps="url")
    verb = rec.verbs["go"]
    assert (verb.kind, verb.signin, verb.example) == ("read", False, None)


# --- the binding of arguments ----------------------------------------------------------------------


def parse(args, words):
    rec = shop()
    rec.steps("probe", help="x", steps="url", args=args)
    return rec.verbs["probe"].parse(words)


def test_options_and_positionals_bind_by_argparse_rules():
    args = [
        Arg("name"), Arg("count", type=int, nargs="?", default=3),
        Opt("--limit", "-l", type=int, default=10), Opt("--mode", choices=["a", "b"], default="a"),
        Opt("--dry-run", action="store_true"), Opt("--tag", action="append", default=[]),
    ]
    found = parse(args, ["x"])
    assert (found.name, found.count, found.limit, found.mode, found.dry_run, found.tag) == ("x", 3, 10, "a", False, [])
    found = parse(args, ["x", "7", "-l", "4", "--mode", "b", "--dry-run", "--tag", "p", "--tag", "q"])
    assert (found.count, found.limit, found.mode, found.dry_run, found.tag) == (7, 4, "b", True, ["p", "q"])
    assert parse([Opt("--limit", type=int)], ["--limit=5"]).limit == 5


def test_a_rest_positional_takes_the_rest_of_the_words_joined_by_single_spaces():
    args = [Arg("title", rest=True), Opt("--limit", type=int, default=1)]
    assert parse(args, ["weekly", "newsletter", "for", "May"]).title == "weekly newsletter for May"
    assert parse(args, ["--limit", "2", "weekly", "news"]).title == "weekly news"
    assert parse(args, ["one"]).title == "one"


def test_a_rest_positional_may_take_none_and_keeps_words_that_look_like_options_after_a_double_dash():
    args = [Arg("title", rest=True, nargs="*")]
    assert parse(args, []).title == ""
    assert parse([Arg("title", rest=True)], ["--", "-5", "off", "--now"]).title == "-5 off --now"


def test_a_rest_positional_is_needed_unless_it_takes_none():
    with pytest.raises(UsageError, match="required: title"):
        parse([Arg("title", rest=True)], [])


@pytest.mark.parametrize(
    "args, words, message",
    [
        ([Opt("--limit", type=int)], ["--limit", "x"], "invalid int value"),
        ([Opt("--limit", type=int)], ["--limit"], "expected one argument"),
        ([Opt("--id", required=True)], [], "required: --id"),
        ([Arg("name")], [], "required: name"),
        ([], ["extra"], "unrecognized arguments: extra"),
        ([Opt("--limit")], ["--nope"], "unrecognized arguments: --nope"),
        ([Opt("--limit")], ["--lim", "3"], "unrecognized arguments"),
        ([Opt("--mode", choices=["a", "b"])], ["--mode", "c"], "invalid choice"),
        ([Arg("name")], ["a", "b"], "unrecognized arguments: b"),
    ],
)
def test_an_argparse_error_is_a_usage_error_that_prints_nothing_and_exits_nothing(args, words, message, capsys):
    with pytest.raises(UsageError, match=message):
        parse(args, words)
    assert capsys.readouterr() == ("", "")


def test_a_help_flag_is_not_special():
    with pytest.raises(UsageError, match="unrecognized arguments: -h"):
        parse([], ["-h"])


def test_a_usage_line_stays_on_one_line_whatever_the_terminal(monkeypatch):
    monkeypatch.setenv("COLUMNS", "20")
    rec = shop()
    rec.steps(
        "probe", help="x", steps="url",
        args=[Opt("--alpha-option"), Opt("--beta-option"), Opt("--gamma-option"), Arg("positional_name")],
    )
    usage = rec.verbs["probe"].usage()
    assert "\n" not in usage
    assert usage.startswith("usage: recipe shop probe ")


def test_the_flags_of_an_existing_command_line_keep_their_spelling():
    """A recipe can mirror a CLI it replaces: short flags, long flags, defaults, store_true."""
    rec = shop()
    rec.steps(
        "pull", help="x", steps="url",
        args=[Arg("id", type=int), Opt("-o", "--out", default="."), Opt("-v", "--verbose", action="store_true")],
        example="7 -v -o dir",
    )
    found = rec.verbs["pull"].parse(["7", "-v", "-o", "dir"])
    assert (found.id, found.out, found.verbose) == (7, "dir", True)
    assert rec.verbs["pull"].usage() == "usage: recipe shop pull [-o OUT] [-v] id"


# --- step verbs --------------------------------------------------------------------------------------


def steps_of(text, args=(), env=("SHOP_BASE",)):
    rec = shop(env=list(env))
    rec.steps("probe", help="x", steps=text, args=list(args))
    return rec.verbs["probe"]


def test_a_step_value_binds_as_one_word_whatever_it_holds():
    verb = steps_of("fill #q {query}\ngo {SHOP_BASE}/item/{id}", [Opt("--query"), Opt("--id")])
    awkward = "a b 'c' \"d\" $HOME `x` ; | & * ? \\ $(id) #not-a-comment"
    assert verb.bind({"query": awkward, "id": "7", "SHOP_BASE": "https://app.example.com"}) == [
        ("fill", ["#q", awkward]),
        ("go", ["https://app.example.com/item/7"]),
    ]


def test_quotes_in_a_template_group_words_and_the_value_still_goes_in_whole():
    verb = steps_of('fill "#q" "{query} now" bare-{id}', [Opt("--query"), Opt("--id")])
    assert verb.bind({"query": "a b", "id": "7"}) == [("fill", ["#q", "a b now", "bare-7"])]


def test_a_rest_of_line_verb_gets_the_value_verbatim():
    verb = steps_of("type {text}\nassert-text   {text} here\nvideo-chapter chapter '{text}'", [Opt("--text")])
    text = "don't 'quote' me, \"ok\""
    assert verb.bind({"text": text}) == [
        ("type", [text]),
        ("assert-text", [f"{text} here"]),
        ("video-chapter", [f"chapter '{text}'"]),
    ]


def test_a_rest_of_line_verb_with_no_argument_gets_none():
    assert steps_of("dialog-accept").bind({}) == [("dialog-accept", [])]


@pytest.mark.parametrize("bad", ["two\nlines", "carriage\rreturn", "nul\0byte", "block <<END", "<<", "a<<b"])
def test_a_value_with_a_newline_a_carriage_return_a_nul_or_a_block_marker_is_refused(bad):
    verb = steps_of("fill #q {query}\ntype {query}", [Opt("--query")])
    with pytest.raises(UsageError, match="query cannot hold a newline or <<"):
        verb.bind({"query": bad})


def test_a_value_may_hold_a_single_less_than_sign_and_other_marks():
    verb = steps_of("type {text}", [Opt("--text")])
    assert verb.bind({"text": "a < b > c <b>x</b>"}) == [("type", ["a < b > c <b>x</b>"])]


def test_a_value_with_no_value_or_several_values_is_refused():
    verb = steps_of("type {text}", [Opt("--text")])
    with pytest.raises(UsageError, match="text has no value"):
        verb.bind({"text": None})
    with pytest.raises(UsageError, match="text holds several values"):
        verb.bind({"text": ["a", "b"]})


def test_numbers_and_flags_bind_as_text():
    verb = steps_of("go {SHOP_BASE}/{n}/{flag}", [Opt("--n", type=int), Opt("--flag", action="store_true")])
    assert verb.bind({"n": 7, "flag": True, "SHOP_BASE": "https://app.example.com"}) == [
        ("go", ["https://app.example.com/7/true"]),
    ]


def test_doubled_braces_write_braces():
    verb = steps_of("type ({{a: {x}}})", [Opt("--x")])
    assert verb.bind({"x": "1"}) == [("type", ["({a: 1})"])]


@pytest.mark.parametrize("line", [
    "eval document.title = '{text}'",
    "eval {text}",
    "run-code await page.fill('#q', '{text}');",
    "run-code return {SHOP_BASE};",
    "eval ({{a: {text}}})",
])
def test_a_placeholder_never_goes_into_code(line):
    rec = shop(env=["SHOP_BASE"])
    with pytest.raises(RecipeError, match=r"steps line 2: (eval|run-code) takes code, so no value \(\{\w+\}\) goes into it") as refused:
        rec.steps("go", help="x", args=[Opt("--text")], steps="url\n" + line)
    assert "page.evaluate(js, *values)" in str(refused.value)
    assert "recipe shop, verb go" in str(refused.value)


def test_code_in_a_step_line_may_hold_braces_and_no_value():
    verb = steps_of("eval ({{a: 1}})\nrun-code return page.url();")
    assert verb.bind({}) == [("eval", ["({a: 1})"]), ("run-code", ["return page.url();"])]


def test_the_other_rest_of_line_verbs_still_take_a_value():
    verb = steps_of("find {t}\nassert-text {t}\ndialog-accept {t}\nvideo-chapter {t}", [Opt("--t")])
    assert verb.bind({"t": "x y"}) == [
        ("find", ["x y"]), ("assert-text", ["x y"]), ("dialog-accept", ["x y"]), ("video-chapter", ["x y"]),
    ]


def test_a_recipe_file_with_a_value_in_code_is_broken_and_says_why(bundled):
    write_recipes(bundled, alpha=source("alpha", extra="recipe.steps('bad', help='h', args=[], steps='eval {nope}')\n"))
    (entry,) = recipe.Library().entries()
    assert entry.recipe is None
    assert "eval takes code, so no value ({nope}) goes into it" in entry.error


def test_comments_and_blank_lines_in_steps_are_not_commands():
    verb = steps_of("# a comment\n\n  url  \n\n# another\nback")
    assert verb.bind({}) == [("url", []), ("back", [])]


def test_an_env_value_is_bound_like_an_argument():
    verb = steps_of("go {SHOP_BASE}/x")
    with pytest.raises(UsageError, match="cannot hold a newline"):
        verb.bind({"SHOP_BASE": "https://app.example.com\nclick x"})


def test_a_step_verb_runs_its_lines_through_the_command_path_with_values_bound(browser, monkeypatch):
    monkeypatch.setenv("SHOP_BASE", ORIGIN)
    rec = shop(env=["SHOP_BASE"])
    rec.steps(
        "open", help="Open an item.", args=[Opt("--id", type=int, required=True)], example="--id 4",
        steps="go {SHOP_BASE}/item/{id}\ntext h1",
    )
    browser.texts["h1"] = "Item four"
    result = run(rec, "recipe shop open --id 4")
    assert result.ok, result.error
    assert browser.url == f"{ORIGIN}/item/4"
    assert output(result).splitlines()[-1] == "Item four"
    assert ("goto", f"{ORIGIN}/item/4") in browser.calls


def test_a_step_line_is_checked_against_the_origins_like_any_page_call(browser, monkeypatch):
    monkeypatch.setenv("SHOP_BASE", "https://other.example.net")
    rec = shop(env=["SHOP_BASE"])
    rec.steps("open", help="Open.", steps="go {SHOP_BASE}/x")
    result = run(rec, "recipe shop open")
    assert not result.ok
    assert "go: refusing 'https://other.example.net/x'" in result.error
    assert browser.verbs() == []


def test_a_step_value_never_becomes_a_second_line_or_a_second_verb(browser):
    rec = shop()
    rec.steps("note", help="Type a note.", args=[Arg("text", rest=True)], steps="type {text}")
    result = run(rec, 'recipe shop note "x\\ngo https://evil.example"')
    assert result.ok
    assert browser.url == f"{ORIGIN}/"
    assert [call for call in browser.calls if call[0] == "type"] == [("type", "x\\ngo https://evil.example")]


# --- the loader --------------------------------------------------------------------------------------


def source(name, title="Title", aliases=(), extra=""):
    return (
        "from browser.recipe import Recipe\n"
        f"recipe = Recipe({name!r}, {title!r}, origins=['https://{name}.example.com'], aliases={list(aliases)!r})\n"
        f"recipe.steps('go', help='Go.', steps='go https://{name}.example.com/')\n{extra}"
    )


@pytest.fixture
def bundled(tmp_path, monkeypatch):
    directory = tmp_path / "bundled"
    directory.mkdir()
    monkeypatch.setattr(recipe, "BUNDLED_DIR", directory)
    return directory


def test_the_loader_reads_the_bundled_directory(bundled):
    write_recipes(bundled, alpha=source("alpha"), beta=source("beta"))
    library = recipe.Library()
    assert [(e.name, e.source, e.error) for e in library.entries()] == [
        ("alpha", "bundled", None), ("beta", "bundled", None),
    ]
    assert library.find("alpha").recipe.name == "alpha"


def test_the_loader_skips_names_that_start_with_an_underscore_or_a_dot_and_other_files(bundled):
    write_recipes(bundled, _helper=source("helper"), alpha=source("alpha"))
    (bundled / ".hidden.py").write_text(source("hidden"))
    (bundled / "notes.txt").write_text("not a recipe")
    (bundled / "sub").mkdir()
    (bundled / "sub" / "inner.py").write_text(source("inner"))
    (bundled / "dir.py").mkdir()
    assert [e.name for e in recipe.Library().entries()] == ["alpha"]


def test_the_real_bundled_directory_holds_the_guide_to_writing_a_recipe():
    assert (recipe.BUNDLED_DIR / "README.md").is_file()


def test_a_node_directory_adds_recipes_and_marks_them(bundled, tmp_path):
    write_recipes(bundled, alpha=source("alpha"))
    node = write_recipes(tmp_path / "node", beta=source("beta"))
    library = recipe.Library(dirs=[node])
    assert [(e.name, e.source, e.replaces) for e in library.entries()] == [
        ("alpha", "bundled", False), ("beta", "node", False),
    ]


def test_a_node_recipe_replaces_a_bundled_one_of_the_same_name_and_the_listing_says_so(bundled, tmp_path):
    write_recipes(bundled, alpha=source("alpha", title="Bundled alpha"))
    node = write_recipes(tmp_path / "node", alpha=source("alpha", title="Node alpha"))
    library = recipe.Library(dirs=[node])
    (entry,) = library.entries()
    assert (entry.source, entry.replaces, entry.recipe.title) == ("node", True, "Node alpha")
    assert library.listing() == "alpha  Node alpha  [node]  [replaces bundled]"


def test_a_broken_node_recipe_still_replaces_the_bundled_one_and_says_it_is_broken(bundled, tmp_path):
    write_recipes(bundled, alpha=source("alpha"))
    node = write_recipes(tmp_path / "node", alpha="raise RuntimeError('boom')")
    (entry,) = recipe.Library(dirs=[node]).entries()
    assert entry.recipe is None
    assert (entry.source, entry.replaces) == ("node", True)
    assert recipe.Library(dirs=[node]).listing() == "alpha  [node]  [replaces bundled]  [broken: RuntimeError: boom]"


def test_a_later_directory_replaces_an_earlier_one_but_only_a_bundled_one_is_named_replaced(bundled, tmp_path):
    first = write_recipes(tmp_path / "first", beta=source("beta", title="First"))
    second = write_recipes(tmp_path / "second", beta=source("beta", title="Second"))
    (entry,) = recipe.Library(dirs=[first, second]).entries()
    assert entry.recipe.title == "Second"
    assert entry.replaces is False


def test_a_missing_node_directory_adds_nothing(bundled, tmp_path):
    write_recipes(bundled, alpha=source("alpha"))
    assert [e.name for e in recipe.Library(dirs=[tmp_path / "nowhere"]).entries()] == ["alpha"]


@pytest.mark.parametrize(
    "body, message",
    [
        ("this is not python (", "SyntaxError"),
        ("raise RuntimeError('boom')", "RuntimeError: boom"),
        ("import not_a_module_anywhere", "ModuleNotFoundError"),
        ("raise SystemExit(3)", "SystemExit"),
        ("x = 1", "defines no `recipe` object"),
        ("recipe = 'text'", "defines no `recipe` object"),
        (source("other"), "named 'other' and the file is broken.py"),
        ("from browser.recipe import Recipe\nrecipe = Recipe('broken', 't', origins=['https://a.example.com'])\n", "declares no verb"),
        (source("broken", extra="recipe.steps('x', signin=True, help='h', steps='url')\n"), "needs a @recipe.status probe"),
        ("from browser.recipe import Recipe\nrecipe = Recipe('broken', 't', origins=[])\n", "at least one origin"),
    ],
)
def test_a_module_that_cannot_load_is_listed_as_broken_and_stops_nothing_else(bundled, body, message):
    write_recipes(bundled, alpha=source("alpha"), broken=body, omega=source("omega"))
    library = recipe.Library()
    entries = {entry.name: entry for entry in library.entries()}
    assert sorted(entries) == ["alpha", "broken", "omega"]
    assert entries["alpha"].recipe and entries["omega"].recipe
    assert entries["broken"].recipe is None
    assert message in entries["broken"].error
    assert "\n" not in entries["broken"].error
    assert f"broken  [broken: " in library.listing()


def test_a_broken_recipe_is_not_run_and_the_step_says_why(bundled, browser):
    write_recipes(bundled, broken="raise RuntimeError('boom')", alpha=source("alpha"))
    result = run_script("recipe broken go\nurl", recipe.Library())
    assert not result.ok
    assert result.error == "recipe broken is broken: RuntimeError: boom"
    assert browser.opened == 0


def test_a_broken_recipe_does_not_stop_a_good_one_in_the_same_script(bundled, browser):
    write_recipes(bundled, broken="raise RuntimeError('boom')", alpha=source("alpha"))
    result = run_script("recipe alpha go", recipe.Library())
    assert result.ok, result.error
    assert ("goto", "https://alpha.example.com/") in browser.calls


def test_a_name_that_differs_from_the_file_is_broken_even_when_it_would_shadow(bundled):
    write_recipes(bundled, alpha=source("alpha"), beta=source("alpha"))
    entries = {entry.name: entry for entry in recipe.Library().entries()}
    assert entries["alpha"].recipe is not None
    assert "named 'alpha' and the file is beta.py" in entries["beta"].error


def test_loading_a_recipe_writes_nothing_into_its_directory(bundled, tmp_path):
    node = write_recipes(tmp_path / "node", beta=source("beta"))
    write_recipes(bundled, alpha=source("alpha"))
    recipe.Library(dirs=[node]).entries()
    assert sorted(os.listdir(node)) == ["beta.py"]
    assert sorted(os.listdir(bundled)) == ["alpha.py"]


def test_a_recipe_file_is_loaded_under_a_private_module_name(bundled):
    import sys

    write_recipes(bundled, alpha=source("alpha"))
    recipe.Library().entries()
    names = [name for name in sys.modules if name.startswith("ar3_browser_recipe_")]
    assert any(name.endswith("_alpha") for name in names)
    assert "alpha" not in sys.modules


def test_a_site_resolves_by_name_or_alias_in_any_case(bundled):
    write_recipes(bundled, alpha=source("alpha", aliases=["a1", "al"]))
    library = recipe.Library()
    for site in ("alpha", "ALPHA", "a1", "Al"):
        assert library.find(site).name == "alpha"
    assert library.find("beta") is None


def test_a_name_beats_an_alias_and_a_node_alias_beats_a_bundled_one(bundled, tmp_path):
    write_recipes(bundled, alpha=source("alpha"), beta=source("beta", aliases=["alpha", "shared"]),
                  gamma=source("gamma", aliases=["shared"]))
    node = write_recipes(tmp_path / "node", delta=source("delta", aliases=["shared"]))
    library = recipe.Library(dirs=[node])
    assert library.find("alpha").name == "alpha"
    assert library.find("shared").name == "delta"


def test_the_library_loads_once(bundled, tmp_path):
    write_recipes(bundled, alpha=source("alpha"))
    library = recipe.Library()
    first = library.entries()[0]
    (bundled / "beta.py").write_text(source("beta"))
    assert library.entries()[0] is first
    assert [e.name for e in library.entries()] == ["alpha"]


# --- the listing and the help ----------------------------------------------------------------------


def test_the_listing_is_one_line_per_recipe_with_aliases_title_and_markers(bundled, tmp_path):
    write_recipes(bundled, alpha=source("alpha", title="The alpha site", aliases=["a1", "al"]), beta=source("beta"))
    node = write_recipes(tmp_path / "node", beta=source("beta", title="Node beta"), gamma="1/0")
    assert recipe.Library(dirs=[node]).listing().splitlines() == [
        "alpha (a1, al)  The alpha site",
        "beta  Node beta  [node]  [replaces bundled]",
        "gamma  [node]  [broken: ZeroDivisionError: division by zero]",
    ]


def test_no_recipes_is_said_in_words(bundled):
    assert recipe.Library().listing() == "no recipes"


def test_the_recipe_verb_lists_with_no_word_or_help_and_opens_no_browser(bundled, browser):
    write_recipes(bundled, alpha=source("alpha"))
    for line in ("recipe", "recipe help"):
        result = run_script(line, recipe.Library())
        assert result.ok
        assert output(result) == "alpha  Title"
    assert browser.opened == 0 and browser.calls == []


def full_recipe():
    rec = Recipe(
        "shop", "The shop", origins=[ORIGIN, "https://*.example.org"], aliases=["sh"],
        env=["SHOP_BASE"], secrets=["SHOP_USER", "SHOP_PASS"],
    )

    @rec.status
    def status(page):
        return "in"

    rec.steps("login", help="Sign in.\nSecond line.", steps="url")
    rec.steps(
        "list", signin=True, help="List the items.",
        args=[Opt("--limit", type=int, default=10, help="how many"), Arg("section", nargs="?", help="which part")],
        example="--limit 3", steps="url",
    )
    rec.steps("rename", kind="write", signin=True, help="Rename one.", args=[Arg("title", rest=True)],
              example="a new name", steps="url")
    return rec


def test_the_help_of_a_site_shows_title_origins_env_secrets_and_each_verb(browser):
    result = run(full_recipe(), "recipe shop")
    assert result.ok
    assert output(result).splitlines() == [
        "shop (sh): The shop  [node]",
        f"origins: {ORIGIN}, https://*.example.org",
        "env: SHOP_BASE",
        "secrets: SHOP_USER, SHOP_PASS",
        "sign-in probe: yes",
        "",
        "login (read)",
        "  Sign in.",
        "  Second line.",
        "  usage: recipe shop login",
        "",
        "list (read, needs sign-in)",
        "  List the items.",
        "  usage: recipe shop list [--limit LIMIT] [section]",
        "    --limit: how many",
        "    section: which part",
        "  example: recipe shop list --limit 3",
        "",
        "rename (write, needs sign-in)",
        "  Rename one.",
        "  usage: recipe shop rename title [title ...]",
        "  example: recipe shop rename a new name",
        "",
        "also: recipe shop help [verb], recipe shop status, recipe shop check",
    ]
    assert browser.opened == 0 and browser.calls == []


def test_help_is_the_same_as_naming_the_site_and_help_of_a_verb_shows_that_verb(browser):
    rec = full_recipe()
    assert output(run(rec, "recipe shop help")) == output(run(rec, "recipe shop"))
    assert output(run(rec, "recipe sh help list")).splitlines() == [
        "list (read, needs sign-in)",
        "  List the items.",
        "  usage: recipe shop list [--limit LIMIT] [section]",
        "    --limit: how many",
        "    section: which part",
        "  example: recipe shop list --limit 3",
    ]
    result = run(rec, "recipe shop help nope")
    assert result.error == "recipe shop has no verb nope. Verbs: login, list, rename"


def test_an_unknown_recipe_or_verb_fails_and_says_what_exists(bundled, browser):
    write_recipes(bundled, alpha=source("alpha"), beta=source("beta"))
    library = recipe.Library()
    assert run_script("recipe nope", library).error == "unknown recipe: nope. Known recipes: alpha, beta."
    assert run_script("recipe alpha nope", library).error == (
        "recipe alpha has no verb nope. Verbs: go, help, status, check"
    )
    assert browser.opened == 0


def test_with_no_recipes_an_unknown_recipe_says_none_are_installed(bundled):
    result = run_script("recipe nope", recipe.Library())
    assert result.error == "unknown recipe: nope. No recipes are installed."


# --- the runner: results -----------------------------------------------------------------------------


def verb_returning(value, **kwargs):
    rec = shop()
    rec.verb("get", help="Get.", **kwargs)(lambda page, args: value)
    return rec


def test_a_str_is_the_text_of_the_step(browser):
    result = run(verb_returning("hello\nthere"), "recipe shop get")
    assert result.ok
    assert output(result) == "hello\nthere"
    assert result.files == []


def test_data_alone_is_a_compact_json_step_output_and_an_attached_result_json(browser):
    data = {"items": [{"id": 1, "title": "café"}], "n": 2}
    result = run(verb_returning(data), "recipe shop get")
    assert result.ok
    assert output(result) == '{"items":[{"id":1,"title":"café"}],"n":2}'
    (path,) = result.files
    assert path.endswith("-result.json")
    assert json.loads(open(path, encoding="utf-8").read()) == data
    assert open(path, encoding="utf-8").read() == json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def test_a_list_is_data_too(browser):
    result = run(verb_returning([1, "two", None]), "recipe shop get")
    assert output(result) == '[1,"two",null]'
    assert json.loads(open(result.files[0]).read()) == [1, "two", None]


def test_data_over_the_cap_is_cut_in_the_output_and_whole_in_the_file(browser):
    data = {"rows": ["x" * 50] * 100}
    result = run(verb_returning(data), "recipe shop get")
    text = output(result)
    assert len(text) < recipe.DATA_PREVIEW + 200
    assert text.startswith('{"rows":["xxxx')
    assert text.endswith(f"characters; result.json holds all of it)")
    assert json.loads(open(result.files[0]).read()) == data


def test_text_with_data_is_the_output_and_the_data_is_attached(browser):
    result = run(verb_returning(Result(text="three items", data={"n": 3})), "recipe shop get")
    assert output(result) == "three items"
    assert json.loads(open(result.files[0]).read()) == {"n": 3}


def test_a_verb_that_returns_nothing_has_an_empty_output_and_no_file(browser):
    for value in (None, Result()):
        result = run(verb_returning(value), "recipe shop get")
        assert result.ok and output(result) == "" and result.files == []


@pytest.mark.parametrize("value", [3, 2.5, True, ("a", "b"), object()])
def test_a_verb_that_returns_something_else_fails_and_says_what_it_may_return(browser, value):
    result = run(verb_returning(value), "recipe shop get")
    assert not result.ok
    assert "shop get: TypeError: a verb returns a Result, a str, a dict or a list, not " in result.error


def test_data_that_json_cannot_hold_fails_the_step(browser):
    result = run(verb_returning({"when": object()}), "recipe shop get")
    assert not result.ok
    assert result.error.startswith("get returned data that is not JSON: ")
    assert not [path for path in result.files if path.endswith("result.json")]


def test_files_a_verb_returns_are_attached_once(browser):
    rec = shop()

    @rec.verb("make", help="Make a file.")
    def make(page, args):
        path = page.artifact("note.txt", "hello")
        return Result(text="made", files=[path, path])

    result = run(rec, "recipe shop make")
    assert result.ok, result.error
    (path,) = result.files
    assert path.endswith("-note.txt") and open(path).read() == "hello"


def test_a_verb_attaches_only_files_its_page_made(browser, tmp_path):
    stranger = tmp_path / "elsewhere.txt"
    stranger.write_text("private")
    secrets_path = secretstore.path("seat")
    secretstore.set_secret("seat", "PASSWORD", VALUE, [ORIGIN])
    for target in (stranger, secrets_path, "/etc/passwd", tmp_path / "missing"):
        rec = shop()
        rec.verb("take", help="Take.")(lambda page, args, target=target: Result(files=[str(target)]))
        result = run(rec, "recipe shop take")
        assert not result.ok
        assert "a verb attaches only files its page made" in result.error
        assert result.files == [] or all("failure" in path for path in result.files)
        assert VALUE not in result.error


def test_a_file_name_that_walks_out_of_artifacts_is_not_attachable(browser):
    rec = shop()

    @rec.verb("walk", help="Walk.")
    def walk(page, args):
        return Result(files=[os.path.join(session.artifacts_dir("seat"), "..", "secrets.json")])

    secretstore.set_secret("seat", "PASSWORD", VALUE, [ORIGIN])
    result = run(rec, "recipe shop walk")
    assert not result.ok and "attaches only files its page made" in result.error


# --- the runner: errors --------------------------------------------------------------------------------


def failing_with(error):
    rec = shop()

    @rec.verb("get", help="Get.", args=[Opt("--n", type=int, default=1)], example="--n 2")
    def get(page, args):
        raise error

    return rec


def test_a_usage_error_fails_the_step_with_the_usage_of_the_verb(browser):
    result = run(failing_with(UsageError("no item has 'x' in its title")), "recipe shop get")
    assert result.error == "no item has 'x' in its title\nusage: recipe shop get [--n N]"


def test_bad_arguments_fail_the_step_with_the_usage_and_run_no_page_call(browser):
    result = run(failing_with(RuntimeError("never reached")), "recipe shop get --n x")
    assert result.error == "argument --n: invalid int value: 'x'\nusage: recipe shop get [--n N]"
    assert browser.opened == 0 and browser.calls == []
    result = run(failing_with(RuntimeError("never reached")), "recipe shop get --nope")
    assert result.error == "unrecognized arguments: --nope\nusage: recipe shop get [--n N]"


def test_needs_a_person_fails_the_step_with_the_reason_and_what_to_do(browser):
    result = run(failing_with(NeedsPerson("the site wants a code", "Type the code by hand")), "recipe shop get")
    assert result.error == "needs a person: the site wants a code. Type the code by hand."


def test_needs_a_person_without_a_how_ends_after_the_reason():
    assert str(NeedsPerson("a consent page.")) == "a consent page"
    assert str(NeedsPerson("a", "do b.")) == "a. do b."
    error = NeedsPerson("why", "how")
    assert (error.reason, error.how) == ("why", "how.")


def test_any_other_exception_fails_the_step_naming_the_verb_and_the_exception(browser):
    result = run(failing_with(KeyError("items")), "recipe shop get")
    assert result.error == "shop get: KeyError: 'items'"
    result = run(failing_with(SystemExit(2)), "recipe shop get")
    assert result.error == "shop get: SystemExit: 2"


def test_a_failed_page_call_fails_the_step_with_its_message_and_the_failure_snapshot(browser):
    result = run(failing_with(plc.BrowserError("click: nothing visible matching 'Buy'")), "recipe shop get")
    assert result.error == "click: nothing visible matching 'Buy'"
    assert [path for path in result.files if path.endswith("failure.txt")]


def test_every_error_text_is_scrubbed(browser):
    secretstore.set_secret("seat", "PASSWORD", VALUE, [ORIGIN])
    for error in (UsageError(f"bad {VALUE}"), NeedsPerson(f"saw {VALUE}", f"clear {VALUE}"),
                  RuntimeError(f"boom {VALUE}"), plc.BrowserError(f"call log {VALUE}")):
        result = run(failing_with(error), "recipe shop get")
        assert not result.ok
        assert VALUE not in result.error and "[secret:PASSWORD]" in result.error
        assert VALUE not in result.transcript()
        assert all(VALUE not in json.dumps(step) for step in result.steps)


def test_a_failure_keeps_the_page_calls_made_before_it_in_the_step(browser):
    rec = shop()

    @rec.verb("get", help="Get.")
    def get(page, args):
        page.go(f"{ORIGIN}/a")
        page.log("halfway")
        raise plc.BrowserError("then it broke")

    result = run(rec, "recipe shop get")
    step = result.steps[0]
    assert step["ok"] is False and step["error"] == "then it broke"
    assert step["output"].splitlines() == [f"  go {ORIGIN}/a -> {ORIGIN}/a", "  log: halfway"]
    assert result.transcript().splitlines()[:4] == [
        "! recipe shop get",
        f"        go {ORIGIN}/a -> {ORIGIN}/a",
        "        log: halfway",
        "      then it broke",
    ]


def test_a_run_stops_at_a_failing_recipe_step_and_runs_nothing_after_it(browser):
    result = run(failing_with(RuntimeError("x")), "url\nrecipe shop get\nurl")
    assert [step["command"] for step in result.steps] == ["url", "recipe shop get"]


def test_the_page_calls_and_the_text_make_the_step_output_in_that_order(browser):
    rec = shop()

    @rec.verb("get", help="Get.")
    def get(page, args):
        page.go(f"{ORIGIN}/a")
        return "the text"

    result = run(rec, "recipe shop get")
    assert output(result).splitlines() == [f"  go {ORIGIN}/a -> {ORIGIN}/a", "the text"]
    assert result.transcript().splitlines() == [
        "  recipe shop get",
        f"        go {ORIGIN}/a -> {ORIGIN}/a",
        "      the text",
    ]


def test_a_long_run_of_page_calls_is_counted_not_all_kept(browser):
    rec = shop()

    @rec.verb("get", help="Get.")
    def get(page, args):
        for number in range(recipe.MAX_CALL_LINES + 15):
            page.log(f"call {number}")
        return "the text"

    lines = output(run(rec, "recipe shop get")).splitlines()
    assert len(lines) == recipe.MAX_CALL_LINES + 2
    assert lines[-2:] == ["  ... 15 more lines", "the text"]
    assert lines[0] == "  log: call 0"


# --- the runner: sign-in --------------------------------------------------------------------------------


def signin_recipe(state, login=True):
    rec = shop()
    calls = []

    @rec.status
    def status(page):
        calls.append("status")
        return state() if callable(state) else state

    @rec.verb("read", signin=True, help="Read.")
    def read(page, args):
        calls.append("read")
        return "page text"

    @rec.verb("public", help="Public.")
    def public(page, args):
        calls.append("public")
        return "open to all"

    if login:
        @rec.verb("login", signin=True, help="Sign in.")
        def login_verb(page, args):
            calls.append("login")
            return "signed in"

    return rec, calls


def test_a_verb_that_needs_sign_in_runs_when_the_probe_says_in(browser):
    rec, calls = signin_recipe("in")
    result = run(rec, "recipe shop read")
    assert result.ok and calls == ["status", "read"]
    assert output(result).splitlines() == ["  status -> in", "page text"]


def test_out_stops_the_verb_for_a_person_and_names_the_seat_and_the_login_verb(browser):
    rec, calls = signin_recipe("out")
    result = run(rec, "recipe shop read")
    assert not result.ok
    assert result.error == (
        "needs a person: shop is signed out on seat seat. "
        "Run `recipe shop login`, or sign in by hand in the Chrome of seat seat."
    )
    assert calls == ["status"]


def test_unknown_stops_the_verb_for_a_person_too(browser):
    rec, calls = signin_recipe("unknown")
    result = run(rec, "recipe shop read")
    assert result.error == (
        "needs a person: cannot tell whether shop is signed in on seat seat. "
        "Run `recipe shop login`, or sign in by hand in the Chrome of seat seat."
    )
    assert calls == ["status"]


@pytest.mark.parametrize("state", [None, "", "maybe", 7, "IN ", ["in"]])
def test_anything_but_in_out_or_unknown_is_unknown(browser, state):
    rec, calls = signin_recipe(state)
    expected = "in" if state == "IN " else "unknown"
    result = run(rec, "recipe shop read")
    assert result.ok is (expected == "in")
    if expected == "unknown":
        assert "cannot tell whether shop is signed in" in result.error


def test_without_a_login_verb_the_message_says_to_sign_in_by_hand(browser):
    rec, _ = signin_recipe("out", login=False)
    result = run(rec, "recipe shop read")
    assert result.error == (
        "needs a person: shop is signed out on seat seat. Sign in by hand in the Chrome of seat seat."
    )


def test_the_seat_named_is_the_seat_of_the_run(browser):
    rec, _ = signin_recipe("out")
    result = run(rec, "recipe shop read", seat="garmin")
    assert "on seat garmin" in result.error and "Chrome of seat garmin" in result.error


def test_a_recipe_never_signs_in_again_on_out_or_unknown(browser):
    for state in ("out", "unknown"):
        rec, calls = signin_recipe(state)
        run(rec, "recipe shop read")
        assert "login" not in calls


def test_a_verb_that_needs_no_sign_in_runs_without_the_probe(browser):
    rec, calls = signin_recipe("out")
    result = run(rec, "recipe shop public")
    assert result.ok and calls == ["public"]


def test_login_runs_without_the_probe_even_when_it_is_declared_to_need_sign_in(browser):
    rec, calls = signin_recipe("out")
    result = run(rec, "recipe shop login")
    assert result.ok and calls == ["login"]


def test_the_probe_runs_before_every_verb_that_needs_sign_in(browser):
    states = iter(["in", "out"])
    rec, calls = signin_recipe(lambda: next(states))
    assert run(rec, "recipe shop read").ok
    assert not run(rec, "recipe shop read").ok
    assert calls == ["status", "read", "status"]


def test_a_probe_that_fails_fails_the_verb_with_its_error(browser):
    def broken():
        raise plc.BrowserError("go: net::ERR_CONNECTION_REFUSED")

    rec, calls = signin_recipe(broken)
    result = run(rec, "recipe shop read")
    assert result.error == "go: net::ERR_CONNECTION_REFUSED"
    assert calls == ["status"]


def test_the_status_verb_prints_the_probe_answer_and_fails_nothing(browser):
    for state in ("in", "out", "unknown"):
        rec, _ = signin_recipe(state)
        result = run(rec, "recipe shop status")
        assert result.ok and output(result) == state


def test_status_without_a_probe_is_a_failure(browser):
    rec = shop()
    rec.steps("go", help="x", steps="url")
    assert run(rec, "recipe shop status").error == "recipe shop declares no status probe"


def test_the_probe_opens_the_browser_and_listing_does_not(browser):
    rec, _ = signin_recipe("in")
    run(rec, "recipe shop")
    assert browser.opened == 0
    run(rec, "recipe shop status")
    assert browser.opened == 1


# --- the runner: check -----------------------------------------------------------------------------------


def check_recipe(read_fails=(), state="in"):
    rec = shop()
    ran = []

    @rec.status
    def status(page):
        ran.append("status")
        return state

    def declare(name, kind="read", example="", signin=True):
        @rec.verb(name, kind=kind, signin=signin, help=name, args=[Opt("--n", type=int, default=0)],
                  example=example or None)
        def verb(page, args):
            ran.append((name, args.n))
            if name in read_fails:
                raise plc.BrowserError(f"{name}: it broke\nsecond line")
            return "fine"

    declare("alpha", example="--n 1")
    declare("beta", example="--n 2")
    declare("noexample")
    declare("writer", kind="write", example="--n 3")
    declare("open", example="--n 4", signin=False)
    return rec, ran


def test_check_runs_the_probe_then_each_read_verb_with_an_example_and_reports_each(browser):
    rec, ran = check_recipe()
    result = run(rec, "recipe shop check")
    assert result.ok, result.error
    assert ran == ["status", "status", ("alpha", 1), "status", ("beta", 2), ("open", 4)]
    assert output(result).splitlines()[-5:] == [
        "status: in", "alpha: healthy", "beta: healthy", "open: healthy", "check: healthy (4)",
    ]


def test_check_fails_the_step_when_one_verb_fails_and_reports_the_others(browser):
    rec, ran = check_recipe(read_fails=("beta",))
    result = run(rec, "recipe shop check")
    assert not result.ok
    assert result.error == "check failed: beta"
    assert result.steps[0]["output"].splitlines()[-4:] == [
        "status: in", "alpha: healthy", "beta: failing: beta: it broke", "open: healthy",
    ]
    assert ("open", 4) in ran


def test_check_with_the_probe_out_reports_each_verb_that_needs_sign_in_as_failing(browser):
    rec, ran = check_recipe(state="out")
    result = run(rec, "recipe shop check")
    assert result.error == "check failed: alpha, beta"
    lines = result.steps[0]["output"].splitlines()
    assert lines[-4] == "status: out"
    assert lines[-3].startswith("alpha: failing: needs a person: shop is signed out on seat seat.")
    assert lines[-1] == "open: healthy"
    assert ("alpha", 1) not in ran


def test_a_probe_that_fails_is_a_failing_status_in_check(browser):
    rec = shop()

    @rec.status
    def status(page):
        raise plc.BrowserError("go: refused")

    rec.steps("open", help="x", example="", steps="url")
    result = run(rec, "recipe shop check")
    assert result.error == "check failed: status"
    assert "status: failing: go: refused" in result.steps[0]["output"]


def test_check_with_no_example_has_nothing_to_check_and_says_so(browser):
    rec = shop()
    rec.steps("go", help="x", steps="url")
    result = run(rec, "recipe shop check")
    assert result.ok and output(result) == "nothing to check: no read verb declares an example"


def test_check_never_runs_login_even_when_it_declares_an_example(browser):
    rec = shop()
    ran = []

    @rec.verb("login", help="Sign in.", example="")
    def login(page, args):
        ran.append("login")

    rec.steps("go", help="x", example="", steps="url")
    result = run(rec, "recipe shop check")
    assert result.ok, result.error
    assert ran == []
    assert output(result).splitlines()[-2:] == ["go: healthy", "check: healthy (1)"]


def test_check_attaches_no_result_json(browser):
    rec = shop()

    @rec.verb("get", help="x", example="")
    def get(page, args):
        return {"a": 1}

    result = run(rec, "recipe shop check")
    assert result.ok, result.error
    assert result.files == []


def test_a_stop_during_check_ends_it_at_once(browser):
    rec = shop()
    seen = []

    @rec.verb("one", help="x", example="")
    def one(page, args):
        seen.append("one")
        session.bump_stop_epoch("seat")
        page.url()

    @rec.verb("two", help="x", example="")
    def two(page, args):
        seen.append("two")

    result = run(rec, "recipe shop check")
    assert result.error == session.STOP_MESSAGE
    assert seen == ["one"]


# --- the runner: nesting ---------------------------------------------------------------------------------


def two_recipes():
    outer = Recipe("outer", "Outer", origins=[ORIGIN])
    inner = Recipe("inner", "Inner", origins=["https://inner.example.net"])
    third = Recipe("third", "Third", origins=["https://third.example.net"])

    @inner.verb("fetch", help="Inner.", args=[Arg("word", rest=True, nargs="*")])
    def inner_fetch(page, args):
        page.log(f"inner got {args.word!r}")
        return Result(text=f"inner:{args.word}", data={"w": args.word})

    @inner.verb("deeper", help="Calls a third.")
    def deeper(page, args):
        page.recipe("third", "ping")

    @third.verb("ping", help="Ping.")
    def ping(page, args):
        return "pong"

    @outer.verb("get", help="Calls the inner recipe.")
    def get(page, args):
        reply = page.recipe("inner", "fetch", "a", "b")
        page.log(f"outer saw {reply.text} {reply.data}")
        return reply.text.upper()

    @outer.verb("deep", help="Calls two levels.")
    def deep(page, args):
        page.recipe("inner", "deeper")

    @outer.verb("wrong", help="Calls a verb that is not there.")
    def wrong(page, args):
        page.recipe("inner", "nope")

    @outer.verb("catch", help="Takes another branch when the inner recipe fails.")
    def catch(page, args):
        try:
            page.recipe("inner", "deeper")
        except recipe.BrowserError as exc:
            return f"caught: {exc}"

    return static_library(outer, inner, third)


def test_a_recipe_calls_another_one_level_deep(browser):
    result = run_script("recipe outer get", two_recipes())
    assert result.ok, result.error
    assert output(result).splitlines() == [
        "  recipe inner fetch a b",
        "    log: inner got 'a b'",
        "  log: outer saw inner:a b {'w': 'a b'}",
        "INNER:A B",
    ]


def test_a_recipe_may_not_call_a_third_level(browser):
    result = run_script("recipe outer deep", two_recipes())
    assert result.error == "a recipe may call another recipe one level deep, not more"
    assert result.steps[0]["output"].splitlines() == ["  recipe inner deeper", "    recipe third ping"]


def test_a_nested_call_to_nothing_fails_the_step(browser):
    result = run_script("recipe outer wrong", two_recipes())
    assert result.error == "recipe inner has no verb nope"


def test_the_outer_recipe_can_catch_a_failed_nested_call(browser):
    result = run_script("recipe outer catch", two_recipes())
    assert result.ok
    assert output(result).splitlines()[-1] == "caught: a recipe may call another recipe one level deep, not more"


def test_a_nested_verb_is_gated_by_its_own_recipe_and_its_failure_keeps_its_message(browser):
    outer = Recipe("outer", "Outer", origins=[ORIGIN])
    inner = Recipe("inner", "Inner", origins=["https://inner.example.net"])
    inner.status(lambda page: "out")
    inner.verb("fetch", signin=True, help="x")(lambda page, args: "never")
    outer.verb("get", help="x")(lambda page, args: page.recipe("inner", "fetch"))
    result = run_script("recipe outer get", static_library(outer, inner))
    assert result.error == (
        "needs a person: inner is signed out on seat seat. Sign in by hand in the Chrome of seat seat."
    )


def test_nesting_through_step_lines_is_counted_too(browser):
    outer = Recipe("outer", "Outer", origins=[ORIGIN])
    inner = Recipe("inner", "Inner", origins=["https://inner.example.net"])
    third = Recipe("third", "Third", origins=["https://third.example.net"])
    third.verb("ping", help="x")(lambda page, args: "pong")
    inner.steps("relay", help="x", steps="recipe third ping")
    outer.steps("via", help="x", steps="recipe inner relay")
    result = run_script("recipe outer via", static_library(outer, inner, third))
    assert result.error == "a recipe may call another recipe one level deep, not more"


def test_a_step_line_can_call_a_recipe_one_level_deep(browser):
    outer = Recipe("outer", "Outer", origins=[ORIGIN])
    inner = Recipe("inner", "Inner", origins=["https://inner.example.net"])
    inner.verb("ping", help="x")(lambda page, args: "pong")
    outer.steps("via", help="x", steps="recipe inner ping")
    result = run_script("recipe outer via", static_library(outer, inner))
    assert result.ok, result.error
    assert output(result).splitlines()[-1] == "pong"


# --- the runner: a stop ------------------------------------------------------------------------------------


def test_a_stop_between_two_page_calls_ends_the_verb_with_the_stop_text_and_no_snapshot(browser):
    rec = shop()

    @rec.verb("get", help="Get.")
    def get(page, args):
        page.url()
        session.bump_stop_epoch("seat")
        page.url()
        raise AssertionError("the second call did not stop the verb")

    result = run(rec, "recipe shop get\nurl")
    assert result.error == session.STOP_MESSAGE
    assert [step["command"] for step in result.steps] == ["recipe shop get"]
    assert result.files == []


def test_a_stop_that_closed_the_browser_under_a_call_is_still_the_reason(browser):
    rec = shop()

    @rec.verb("get", help="Get.")
    def get(page, args):
        session.bump_stop_epoch("seat")
        raise plc.BrowserError("Target page, context or browser has been closed")

    result = run(rec, "recipe shop get")
    assert result.error == session.STOP_MESSAGE and result.files == []


def test_a_stop_that_surfaces_as_another_exception_is_still_the_reason(browser):
    rec = shop()

    @rec.verb("get", help="Get.")
    def get(page, args):
        session.bump_stop_epoch("seat")
        raise KeyError("lost")

    assert run(rec, "recipe shop get").error == session.STOP_MESSAGE


def test_a_stop_in_the_middle_of_a_wait_ends_it_within_a_moment(browser):
    rec = shop()

    @rec.verb("get", help="Get.")
    def get(page, args):
        page.wait(30)
        return "waited"

    threading.Timer(0.4, session.bump_stop_epoch, args=("seat",)).start()
    started = time.monotonic()
    result = run(rec, "recipe shop get")
    assert result.error == session.STOP_MESSAGE
    assert time.monotonic() - started < 5


def test_a_verb_started_after_the_stop_runs_to_the_end(browser):
    session.bump_stop_epoch("seat")
    rec = verb_returning("fine")
    assert run(rec, "recipe shop get").ok


def test_a_script_that_read_the_epoch_before_a_stop_does_not_start_a_recipe(browser):
    epoch = session.stop_epoch("seat")
    session.bump_stop_epoch("seat")
    result = commands.run_script("seat", "recipe shop get", epoch=epoch, recipes=static_library(verb_returning("x")))
    assert result.error == session.STOP_MESSAGE
    assert browser.calls == []


# --- the guide ---------------------------------------------------------------------------------------------------


GUIDE = Path(__file__).resolve().parents[2] / "browser" / "recipes" / "README.md"


def guide_example():
    text = GUIDE.read_text(encoding="utf-8")
    return text.split("```python\n", 1)[1].split("```", 1)[0]


def test_the_recipe_in_the_guide_loads_and_does_what_the_guide_says(bundled, browser):
    write_recipes(bundled, shop=guide_example())
    library = recipe.Library()
    (entry,) = library.entries()
    assert entry.error is None, entry.error
    rec = entry.recipe
    assert (rec.name, rec.aliases, rec.secrets, list(rec.verbs)) == (
        "shop", ["sh"], ["SHOP_USER", "SHOP_PASS"], ["login", "orders", "open"],
    )
    assert [verb.example for verb in rec.verbs.values()] == [None, "--limit 3", "--id 1"]
    assert [verb.name for verb in rec.verbs.values() if verb.signin] == ["orders", "open"]
    # check runs the probe, then the read verbs with an example: orders and open.
    browser.url = "https://shop.example.com/account"
    browser.fetches["https://shop.example.com/api/orders"] = {"status": 200, "text": json.dumps({"orders": [1, 2, 3, 4]})}
    browser.texts["h1"] = "Order 1"
    result = run_script("recipe shop check", library)
    assert result.ok, result.error
    assert output(result).splitlines()[-4:] == ["status: in", "orders: healthy", "open: healthy", "check: healthy (3)"]
    done = run_script("recipe sh orders --limit 2", library)
    assert output(done).splitlines()[-1] == '{"orders":[1,2]}'


def test_a_symlinked_recipes_directory_and_a_symlinked_recipe_file_both_load(bundled, tmp_path):
    elsewhere = write_recipes(tmp_path / "private-repo" / "recipes", beta=source("beta"))
    node = tmp_path / "node"
    node.mkdir()
    os.symlink(elsewhere, node / "recipes")
    assert [e.name for e in recipe.Library(dirs=[node / "recipes"]).entries()] == ["beta"]
    directory = tmp_path / "linked"
    directory.mkdir()
    write_recipes(tmp_path / "real", gamma=source("gamma"))
    os.symlink(tmp_path / "real" / "gamma.py", directory / "gamma.py")
    assert [e.name for e in recipe.Library(dirs=[directory]).entries()] == ["gamma"]


def test_the_preview_of_data_is_scrubbed_before_it_is_cut_so_a_value_at_the_cut_does_not_show_its_start():
    value = "482916"
    data = {"rows": ["x" * 1987 + value + "y" * 50]}  # the JSON text puts the value at 1997..2003, over the cut at 2000
    scrubber = secretstore.Scrubber({})
    scrubber.hide("PASSCODE", value)
    preview = recipe._data_preview(data, scrubber.scrub)
    assert "482" not in preview and "[se" in preview
    cut_first = recipe._data_preview(data)
    assert "482" in cut_first, "the control: cut before the scrub, the start of the value shows"
    assert preview.startswith('{"rows":["xxxx')
