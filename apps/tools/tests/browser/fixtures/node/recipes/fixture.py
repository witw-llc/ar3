"""The recipe for the test site in fixtures/site. The recipe tests load it as a node's own recipe.

The site's address is a node setting, FIXTURE_BASE, because the test server takes a free port. The
recipe takes its one origin from it.
"""
import os
from urllib.parse import urlsplit

from browser.recipe import Arg, BrowserError, Opt, Recipe, UsageError

BASE = os.environ.get("FIXTURE_BASE", "http://127.0.0.1:9")

recipe = Recipe(
    "fixture", "The fixture site of the recipe tests", origins=[BASE], aliases=["fx"],
    env=["FIXTURE_BASE", "FIXTURE_ENTRY_USER", "FIXTURE_ENTRY_PASS"], secrets=["FIXTURE_USER", "FIXTURE_PASS"],
)


@recipe.status
def status(page):
    page.go(f"{BASE}/list")
    return "in" if urlsplit(page.url()).path == "/list" else "out"


@recipe.verb("login", help="Sign in with the stored secrets FIXTURE_USER and FIXTURE_PASS.")
def login(page, args):
    page.go(f"{BASE}/signin")
    page.fill_secret("#user", "FIXTURE_USER")
    page.fill_secret("#pass", "FIXTURE_PASS")
    page.click("button[type=submit]")
    page.wait_for_url("/list", 10)
    return "signed in"


@recipe.verb(
    "list", kind="read", signin=True, help="List the items as data.",
    args=[Opt("--limit", type=int, default=10, help="how many items at most")], example="--limit 3",
)
def list_items(page, args):
    reply = page.fetch("/api/items")
    if not reply.ok:
        raise BrowserError(f"the items endpoint answered {reply.status}")
    return {"items": reply.json["items"][: args.limit]}


@recipe.verb(
    "read", kind="read", signin=True, help="Print the body of the item whose title holds the words.",
    args=[Arg("title", rest=True, help="words of the title, with no quotes")], example="second item",
)
def read(page, args):
    items = page.fetch(f"{BASE}/api/items").json["items"]
    wanted = args.title.lower()
    found = [item for item in items if wanted in item["title"].lower()]
    if not found:
        raise UsageError(f"no item has {args.title!r} in its title")
    page.go(f"{BASE}/item/{found[0]['id']}")
    return page.text("article")


recipe.steps(
    "open", kind="read", signin=True, help="Open an item by its id and print its title.",
    args=[Opt("--id", type=int, required=True, help="the item id")], example="--id 1",
    steps="go {FIXTURE_BASE}/item/{id}\nwait-for h1\ntext h1",
)

recipe.steps(
    "logout", kind="write", help="Sign out of the site.",
    steps="go {FIXTURE_BASE}/list\nclick Sign out\nwait-for-url /signin",
)


@recipe.verb(
    "enter", kind="write",
    help="Sign in with two values the recipe holds in memory, as it would a one-time passcode.",
    args=[
        Opt("--into", default="#user", help="the field the first value goes in"),
        Opt("--page", default="/signin", help="the page it is on"),
    ],
)
def enter(page, args):
    user, password = page.env("FIXTURE_ENTRY_USER"), page.env("FIXTURE_ENTRY_PASS")
    page.hide("ENTRY_USER", user)
    page.hide("ENTRY_PASS", password)
    page.go(f"{BASE}{args.page}")
    page.fill_hidden(args.into, user)
    page.fill_hidden("#pass", password)
    page.click("button[type=submit]")
    page.wait_for_url("/list", 10)
    return "signed in"


@recipe.verb(
    "boxes", kind="write",
    help="Type one character into each sign-in field, then click and hover, with or without keeping page files.",
    args=[Opt("--keep", action="store_true", help="keep the page files playwright-cli writes")],
)
def boxes(page, args):
    page.go(f"{BASE}/signin")

    def type_and_act():
        page.fill_hidden("#user", "Q")
        page.fill_hidden("#pass", "Z")
        page.click("#user")
        page.hover("#go")

    if args.keep:
        type_and_act()
    else:
        with page.no_page_files():
            type_and_act()
    return "typed"


@recipe.verb(
    "guarded", kind="write",
    help="Type a value that no scrub knows into the sign-in page, then end the way --how says.",
    args=[Opt("--how", choices=["fine", "fail", "stuck", "exposed"], default="fine")],
)
def guarded(page, args):
    # The value of the node's setting, written backwards: no scrub knows it, as none knows one digit to a box.
    typed = page.env("FIXTURE_ENTRY_USER")[::-1]

    def empty():
        page.go(f"{BASE}/signin")
        return page.evaluate("() => document.querySelector('#user').value === ''") is True

    def work():
        page.go(f"{BASE}/signin")
        page.fill_hidden("#user", typed)
        page.click("#user")
        if args.how != "fine":
            page.run_code("throw new Error(values[0])", typed)
        return "typed and cleared"

    if args.how == "exposed":
        return work()  # no sensitive step: the control
    with page.sensitive("passcode", (lambda: False) if args.how == "stuck" else empty):
        return work()


@recipe.verb("shots", kind="read", help="Screenshot the tall page twice: the part in view, then the whole page.")
def shots(page, args):
    page.go(f"{BASE}/tall")
    page.screenshot("view")
    page.screenshot("whole", full_page=True)
    return "two screenshots"


@recipe.verb(
    "offsite", kind="read", signin=True,
    help="Try to leave the recipe's origins by one route. Every route must fail.",
    args=[Opt("--how", choices=["go", "fetch", "http", "tab", "click"], default="go")],
)
def offsite(page, args):
    page.go(f"{BASE}/list")
    target = page.evaluate("() => document.querySelector('#elsewhere').href")
    if args.how == "go":
        page.go(target)
    elif args.how == "fetch":
        page.fetch(target)
    elif args.how == "http":
        page.http(target)
    elif args.how == "tab":
        page.new_tab(target)
    else:
        page.click("#elsewhere")
    return "left the origins"


@recipe.verb(
    "bounce", kind="read", signin=True,
    help="Fetch an address that redirects. A fetch follows no redirect, so this must fail.",
    args=[Opt("--to", choices=["inside", "outside"], default="outside", help="where the redirect points")],
)
def bounce(page, args):
    page.go(f"{BASE}/list")
    page.fetch(f"{BASE}/redirect-{'in' if args.to == 'inside' else 'out'}")
    return "followed a redirect"


@recipe.verb(
    "slow", kind="read", help="Wait for a while, on the sign-in page. A stop ends the wait.",
    args=[Opt("--seconds", type=float, default=60.0, help="how long to wait")],
)
def slow(page, args):
    page.go(f"{BASE}/signin")
    page.wait(args.seconds)
    return "waited"
