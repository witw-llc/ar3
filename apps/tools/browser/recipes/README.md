# Writing a site recipe

A site recipe gives a website named commands, with the browser tool as the engine
behind them. One Python module describes one site: where its pages live, and each
verb a person or an agent may run. Help, argument checks, the health check and the
reply all come from that one description. How a recipe is called, listed and checked
is in the [browser README](../README.md#site-recipes). This page is how to write one.

A recipe is reviewed code that acts with the seat's signed-in identity. It lives in
this directory (a general driver for a public site, with no personal value in it) or in
the `recipes/` directory of a node (a private one). No message can install or change a
recipe. A model never runs inside one: a recipe is deterministic, and when a site
changes, the fix is a reviewed change to the recipe.

## Bundled recipes

| Recipe | Site | Verbs |
|---|---|---|
| `givebacks`, alias `gb` | GiveBacks, the newsletter CMS | `login`, `list`, `open`, `pull`, `screenshot` |

### givebacks

`recipe gb` reads an organization's newsletters from its GiveBacks site, with the seat's signed-in Chrome.
All five verbs read: none changes a newsletter or anything else the site holds.

| Verb | Arguments | What comes back |
|---|---|---|
| `login` | | `Login successful.`, or `Already signed in.` when the profile is signed in. |
| `list` | `[--limit 10]` | One line per draft (id, status, send date, subject) and the same rows as `result.json`. |
| `open` | `--id ID` | The seat's Chrome is on the editor of that newsletter. |
| `pull` | `--id ID [-o FILE]` | The design JSON, attached as `givebacks-design.json`. `-o` names the attachment by the last part of `FILE`, and nothing is written at `FILE`. |
| `screenshot` | `--id ID [--dir DIR]` | A full-page picture of the newsletter, attached as `newsletter-preview.png`. `--dir` is accepted and ignored. |

Every attachment's name starts with a timestamp, such as `20261024T201500-givebacks-design.json`.

`list`, `open`, `pull` and `screenshot` need a signed-in page. The status probe opens the site's message
list and reads where the site sends it: the list is `in`, a sign-in or passcode page is `out`, any other
page is `unknown`, and a verb stops for a person on `out` and `unknown`. `recipe gb check` runs the probe
and `list --limit 3`.

**Node settings.** Both are required by every verb, and a missing one fails the step and names it:

```bash
a8s vars <node> set env.GIVEBACKS_BASE https://yourgroup.givebacks.com
a8s vars <node> set env.GIVEBACKS_CAUSE_ID <the organization's id in the GiveBacks API>
```

`GIVEBACKS_BASE` is the address of the organization's site, with nothing after the host. It is the recipe's
site origin. The recipe's other origins are the same for everyone: `https://api.givebacks.com`, and Outlook
web (`https://outlook.office.com`, which sends the browser on to `https://outlook.cloud.microsoft`) for the
passcode.

**Sign-in.** The seat's signed-in profile is the credential, and `login` is only for when it is not. It
types the stored secrets into the form. A site that sends a one-time passcode gets it from the mail: the
recipe opens Outlook web in a second tab (Outlook web must already be signed in on the same seat), reads the
code from the first passcode message in the list, closes the tab, types the code into the six boxes without
showing it, ticks "trust this browser" and submits. If Outlook web is signed out on the seat, or the mail holds
no code, `login` stops with `needs a person` and says which. Store the two secrets on the node's machine, each for
the site's origin:

```bash
browser secret set GIVEBACKS_USER --origin https://yourgroup.givebacks.com --seat <node>
browser secret set GIVEBACKS_PASS --origin https://yourgroup.givebacks.com --seat <node>
```

The code is not a stored secret, and it is in no output: not the transcript, an error, the result or a file.
The recipe hides it the moment it reads it (`page.hide`) and types it with `page.fill_hidden`. All of it is one
sensitive step (`page.sensitive`), opened before the Outlook tab is opened and closed after the code page is left.
playwright-cli follows each click and key with a snapshot file of the page. During the sign-in those files hold
the mail, and the digits one to a box, where no scrub can find them, so the step deletes them when it ends.
While the step is open, nothing of the page is captured: a failure attaches no snapshot, and the error says only
`givebacks login: the passcode step failed`. The step ends with a cleanup that looks at the browser: no tab the
step opened is left (it closes each, at whatever address the mail ended), the sign-in tab is not on Outlook web,
and it is not the code page, or its six boxes read empty. It loads the code page again, or empties its boxes,
when digits are in them. If the cleanup cannot confirm that, the
tool closes the seat's Chrome before it reports the failure, and the error says so. Signing in works again at
the next script, because the profile keeps its state. Pixels are not scrubbed, so the step takes no screenshot and
starts no video, and it does not open while a video is recording: `login` stops with a message that says to stop
the video first.

**An alias for a shell.** `ar3 tool` reads a word that starts with `-` as its own option, so the alias needs
`--` before the recipe:

```bash
alias gb='ar3 tool browser --node <node> -- recipe gb'
gb list --limit 5
gb pull --id <id> -o design.json
```

## A first recipe

`shop.py`, for a made-up shop at `https://shop.example.com`:

```python
from browser.recipe import BrowserError, Opt, Recipe

recipe = Recipe(
    "shop", "The example shop",
    origins=["https://shop.example.com"], aliases=["sh"],
    secrets=["SHOP_USER", "SHOP_PASS"],
)


@recipe.status
def status(page):
    page.go("https://shop.example.com/account")
    return "in" if page.url().endswith("/account") else "out"


@recipe.verb("login", help="Sign in with the stored secrets SHOP_USER and SHOP_PASS.")
def login(page, args):
    page.go("https://shop.example.com/signin")
    page.fill_secret("#user", "SHOP_USER")
    page.fill_secret("#pass", "SHOP_PASS")
    page.click("button[type=submit]")
    page.wait_for_url("/account", 15)
    return "signed in"


@recipe.verb(
    "orders", kind="read", signin=True, help="List the newest orders as data.",
    args=[Opt("--limit", type=int, default=10, help="how many orders at most")],
    example="--limit 3",
)
def orders(page, args):
    reply = page.fetch("/api/orders")
    if not reply.ok:
        raise BrowserError(f"the shop answered {reply.status} for the orders")
    return {"orders": reply.json["orders"][: args.limit]}


recipe.steps(
    "open", kind="read", signin=True, help="Open an order and print its heading.",
    args=[Opt("--id", type=int, required=True, help="the order number")], example="--id 1",
    steps="go https://shop.example.com/orders/{id}\nwait-for h1\ntext h1",
)
```

Then `recipe shop` describes it, `recipe shop orders --limit 3` runs it, and `recipe shop
check` runs the status probe and then `orders --limit 3` and `open --id 1`.

## Declaring the site

`Recipe(name, title, origins, aliases=(), env=(), secrets=())`

| Field | Meaning |
|---|---|
| `name` | Lower case letters, digits and `-`. It must equal the file name, `shop.py`. |
| `title` | One line, shown in the listing. |
| `origins` | The origins the recipe may open and fetch from, in the syntax of `browser secret set --origin`: `https://shop.example.com`, or `https://*.example.org` for any subdomain (not the bare domain). At least one. The scheme, host and port are all part of an origin. |
| `aliases` | Other names for the site. A recipe name always beats an alias, and a node's alias beats a bundled one. |
| `env` | The node settings the recipe reads, set with `a8s vars <node> set env.NAME value`. Upper case. |
| `secrets` | The stored secrets the recipe types. Upper case. The operator stores each with `browser secret set NAME --origin ORIGIN --seat <node>`. |

The module must define `recipe`. A file that fails to import, defines no `recipe`, or
names the recipe differently from the file is listed as broken and nothing else is
affected. A file whose name starts with `_` is not loaded as a recipe. Keep a recipe's helper
code in its own file. A recipe imports from `browser.recipe` only: `Recipe`, `Arg`, `Opt`, `Result`,
`UsageError`, `NeedsPerson`, `BrowserError`.

## Verbs

Each verb is declared once:

| Keyword | Meaning |
|---|---|
| name | Lower case letters, digits and `-`. `help`, `check` and `status` are built in. |
| `kind` | `"read"` or `"write"`. The help marks each, and `check` runs only the `read` verbs. Default `"read"`. |
| `signin` | `True` when the verb needs a signed-in page. The runner runs the status probe first. Default `False`. |
| `help` | What the verb does. The first lines of the help. |
| `args` | A list of `Arg` and `Opt`. |
| `example` | The arguments of a call that works, such as `"--limit 3"`. `check` runs it. It must fit `args`, or the recipe does not load. |

A verb is one of two kinds.

**A step verb** is a template of command-language lines, for a flow with no branches:
`recipe.steps("open", ..., steps="go ...\nwait-for h1\ntext h1")`. Its result is the
output of the last line.

**A code verb** is a function that drives the page: `@recipe.verb("orders", ...)` over
`def orders(page, args)`. It covers branches, loops, polling, reading page text, the
vendor's own API through `fetch`, and tabs.

## Arguments

A verb's arguments are argparse's. `Arg` is a positional and `Opt` is an option, and both
pass argparse's own keywords on: `type`, `default`, `required`, `help`, `nargs`,
`choices`, `action`. A recipe can keep the exact flags of a command line it replaces:

```python
args=[Arg("id", type=int), Opt("-o", "--out", default="."), Opt("-v", "--verbose", action="store_true")]
```

`Arg("title", rest=True)` takes the rest of the words, joined by single spaces, so a caller
types `recipe shop read weekly newsletter` with no quotes. Abbreviated long options are
refused. A wrong argument fails the step with argparse's message and the verb's usage line,
and nothing touches the browser. Argument names are lower case letters, digits and `_`.

## Step verbs

Placeholders are `{name}`, where `name` is an argument or a declared `env` name. `{{` and
`}}` write a brace. Each value goes in as one value: it can not become a second word, a
second line or a block, and a value that holds a newline, a carriage return or `<<` is
refused. A verb of the command language that takes the rest of the line (`type`, `find`,
`assert-text`) gets the value as typed.

`eval` and `run-code` take code, and a value never becomes code text. So a step line for
either holds no placeholder, and a recipe that has one does not load. A recipe that needs a
value in page code is a code verb that passes it to `page.evaluate(js, *values)`. A step line
of `eval` or `run-code` with no placeholder keeps the `ALLOW_EVAL` rule and the refusal on a
seat that holds secrets.

Each line then runs as a command-language line does, with every check: the recipe's
origins, the stop epoch and the scrub. A step can call another recipe (`recipe other verb`),
one level deep.

## The page

A code verb gets a `Page`. It never calls playwright-cli or a subprocess itself. Every call
checks the stop epoch, adds a scrubbed line to the transcript, and checks the origins.

| Method | Does |
|---|---|
| `go(url)` | Opens `url`, which must be on the recipe's origins. |
| `click(target)`, `fill(target, text)`, `press(key)`, `select(target, value)`, `check(target)`, `uncheck(target)`, `hover(target)` | As the verbs of the same name. A target is a snapshot ref, a CSS selector or visible text. The text of `fill`, `select` and `press` is in the command line of `playwright-cli`, and a text that holds a stored secret, or a value the recipe hid, is refused with `playwright-cli was not run: an argument of its command line held a value that must not be in one`: nothing runs. A target the page gave a name from a value (an id made of a user name) is named by its path of tag names and positions. |
| `fill_secret(target, name)` | Types the stored secret `name`, which the recipe must declare. The secret's own origins still apply. |
| `hide(name, value)`, `fill_hidden(target, text)`, `no_page_files()` | For a value the recipe reads from a page and holds only in memory, such as a one-time passcode. `hide` makes it a scrubbed value for the rest of the script, like a stored secret: `[secret:NAME]` replaces it in the transcript, errors, return values, the reply and snapshots, and in the files playwright-cli has already written. It is the value whole that is found. A value shorter than 4 characters can not be scrubbed and `hide` refuses it. `fill_hidden` types a value with it in no command line and the transcript naming the field only. A failure reports the first line of the driver's error, because the rest is a call log that prints the text. The field must be in a page on the recipe's origins. `no_page_files` is a context manager: `with page.no_page_files(): ...`. playwright-cli writes a snapshot file of the page after each click, key, check, hover and navigation, and a value typed into several boxes is in it one piece to a line. The files that block writes are deleted when it ends, with an error or without. |
| `sensitive(name, cleanup)` | A context manager for the part of a verb that handles such a value: `with page.sensitive("passcode", cleanup): ...`. Open it before the first page that shows the value, and close it after the last. While it is open, nothing of the page is captured: a failure attaches no snapshot, and `snap`, `shot`, `console`, `video-start` and `screenshot` are refused. It does not open while a video is recording, because a recording keeps the pages of the step and nothing scrubs pixels. The page files its calls write, the cleanup's too, are deleted when it ends. When the block ends, with an error or without, the tool calls `cleanup()`, your own function. It makes the page hold nothing of the value (it closes the mail's tab, empties the boxes or leaves the page) and returns `True` only when it has looked and found that so. Any other answer, `None` too, and any error, mean the page is not confirmed clear, and the tool closes the seat's Chrome before it reports the failure. After a cleanup that did not confirm, capture stays off for the rest of the script, Chrome closed or not. Each call inside the step writes one line under the recipe's own, and that line shows the verb and its target and nothing that the page or the server answered (`click e30`, `fill e9`, `text #message`, `fetch GET https://...`, `tabs`), where outside the step it ends with `-> <what came back>`. A text given to the call after its target (`fill e9 <text>`) is left off. A `log(text)` line shows the text it is given, so give it none of the page's. The error that leaves a failed step is `<recipe> <verb>: the <name> step failed`, with a note when Chrome was closed, and holds nothing of the page or the driver, whose text may be the value. `NeedsPerson` and `UsageError` are the exception: you wrote their text, and they pass through when the cleanup confirms. |
| `wait(seconds)`, `wait_for(selector, seconds=10)`, `wait_for_url(substring, seconds=10)` | Waits. A stop ends `wait` within a moment. |
| `text(selector)`, `find(text)`, `url()`, `snapshot_text()` | Reads the page, as scrubbed text. |
| `screenshot(name, full_page=False)`, `download(target, seconds=30)` | Make a file, attach it to the reply and return its path. `full_page=True` takes the whole scrollable page and not the part in view. |
| `upload(target, path)`, `drop(target, *paths)` | Hand files to the page. |
| `back()`, `forward()`, `reload()` | Move through the history. |
| `evaluate(js, *values)`, `run_code(body, *values)` | The recipe's own code in the page. `js` is the source of a function, such as `(a, b) => a + b`. The values are its arguments, passed as JSON and never pasted into the code. `run_code` takes Playwright statements with `page` and `values` in scope. Both return JSON data. |
| `fetch(url, method="GET", json=None, body=None, headers=None, timeout=60)` | One `fetch` in the page, with its cookies. `url` may be relative to the page. Follows no redirect: one fails the call. Returns a response with `status`, `ok`, `url`, `headers`, `text` and `json`. |
| `http(url, method="GET", json=None, body=None, headers=None, timeout=30)` | Plain HTTP from this machine, for a public endpoint, with no cookies. |
| `tabs()`, `new_tab(url)`, `select_tab(index)`, `close_tab(index=None)` | Tabs. `tabs()` returns the addresses of the open tabs in order, and never their titles. A list the tool can not read is an error, so "I could not tell" is never taken for "there is none". |
| `local_storage_set(key, value)`, `resize(width, height)` | Page storage and window size. |
| `artifact(name, data_or_path)` | Saves text, bytes or a `pathlib.Path` as a file of the reply. |
| `log(text)` | A line in the transcript. |
| `env(name)` | A declared node setting. An undeclared or unset name fails the step with a usage message that names it. |
| `recipe(site, verb, *args)` | Runs another recipe's verb and returns its `Result`. One level deep. |
| `command(verb, *args)` | Any verb of the command language by name, as a script line would run it. |

No method returns a secret value. What the page gives back passes the scrubber first, so a
stored value shows as `[secret:NAME]`. A call that fails raises `BrowserError`, which a
verb may catch to take another branch.

## Origins

`go`, `fetch`, `http` and `new_tab` refuse a URL outside the recipe's origins before any
request is made. After a call that can navigate (`go`, `click`, `press`, `select`,
`check`, `uncheck`, `back`, `forward`, `reload`, `download`, `upload`, `drop`, tab changes) the
page's URL is checked, and a page outside the origins fails the verb.

A `fetch` follows no redirect. A redirect fails the call with a message, because the browser
would request the new address before the recipe's origins could be checked. Fetch the address the
redirect points to, or open the page with `go`. An `http` redirect is followed only to a URL on
the origins, and each hop is checked before it is followed.

A click or a `go` can not be stopped before the other site answers, so the check there comes
after: the page that a link or a server redirect took the browser to fails the verb. The in-page
code of `evaluate` and `run_code` is not checked: it is code you reviewed.

A page that is a local file (`file:`) fails the call whatever the origins say. The tool opens no local
file as a page: it sends the page to `about:blank` and removes the files the step wrote before the failure reaches
the recipe. A recipe that catches the error and goes on finds a blank page.

Put in `origins` every host the recipe uses, including a vendor's API host that `fetch` or
`http` calls, and keep the list as short as the site allows.

## Results and errors

A code verb returns `Result(text=None, data=None, files=())`, a `str` (text), or a `dict` or
`list` (data).

- The step's output is the text. With data and no text, the output is the data as compact JSON,
  cut at 2000 characters.
- The data is also attached whole as `result.json`.
- `files` are attached. They must be files the page made (`screenshot`, `download`,
  `artifact`), because those are inside the seat's artifacts directory.

A verb ends in one of these ways, and each says what to do next:

| Raised | The step fails with |
|---|---|
| `UsageError("...")` | The message and the verb's usage. Use it for a bad argument the parser could not see, such as an unknown title. A missing `env` name raises it too. |
| `NeedsPerson(reason, how)` | `needs a person: <reason>. <how>`. Use it for a code, a consent page, or anything only a person can do. |
| `BrowserError` or any page call that fails | The error and the failure snapshot. |
| Any other exception | `<recipe> <verb>: <ExceptionType>: <message>` and the failure snapshot. |
| Any exception inside `page.sensitive` | `<recipe> <verb>: the <name> step failed`, with a note when Chrome was closed, and no failure snapshot after a cleanup that did not confirm. `NeedsPerson` and `UsageError` keep their text when the cleanup confirms. |

Error text is scrubbed, and a stop that lands during a verb is reported as the stop.

## Sign-in

The seat's signed-in Chrome profile is the credential. Declare a status probe, and mark
the verbs that need a sign-in with `signin=True`:

```python
@recipe.status
def status(page):
    page.go("https://shop.example.com/account")
    return "in" if page.url().endswith("/account") else "out"
```

The probe returns `"in"`, `"out"` or `"unknown"` (anything else counts as unknown) and
changes nothing on the site. The runner calls it before each verb that needs sign-in. For
`out` or `unknown` the verb does not run: the step fails with `needs a person`, names the
seat, and says to sign in by hand, or to run the recipe's `login` verb when it has one.
The recipe never signs in on a guess, and `check` never calls `login`.

A verb named `login` runs without the probe. It types stored secrets with `fill_secret`.
A passcode a site sends by email belongs to another page. Do all of it inside
`with page.sensitive("passcode", cleanup):`, opened before the first page that shows the code, and closed
after the code page is left. Read the code there from `snapshot_text()` or `evaluate`, which return the text
to the recipe and print none of it. Do not read it with `text` or `find`: their output goes into the
transcript. Call `hide` at once and type it with `fill_hidden`. Close the tab you read it in before any step
that can fail, so that a snapshot taken after the step is of the site and not of the mail.

The cleanup is the part to write with care. It runs when the block ends, with an error or without, and its
answer is the only thing that keeps Chrome open: return `True` after you have looked and found that no tab
the step opened is left (`page.tabs()`) and that the code page, if it is still the page, shows all its boxes empty. Close a
tab that is left, load the page again, or empty the boxes, and then look again. Let a call that fails raise: a
cleanup that raises, or answers anything but `True`, makes the tool close the seat's Chrome. That is the safe
end, and the next script opens Chrome again. Do not catch an error to make the cleanup answer `True`.

## Calling another recipe

`page.recipe("other", "verb", "arg")` runs a verb of another recipe, with that recipe's origins
and its own sign-in probe, and returns its `Result`. A recipe may call another one level
deep, not more. A failure in the called verb comes back as a `BrowserError` with its message.

## A fixture test

A recipe ships with two kinds of test, in `apps/tools/tests/browser/`.

1. Unit tests on a fake browser. `fake_browser.py` stands in for playwright-cli and the session:
   a test says where a `goto` lands, what a selector reads, and what a `fetch` returns, then
   runs `recipe <site> <verb>` through `commands.run_script` and checks the output, the files
   and the failure text. Test every verb, a signed-out run, and a page that leaves the origins.
2. A live test on a fixture site. `site_fixture.py` serves pages from `fixtures/site/` on
   `127.0.0.1` and a port of its own (a sign-in form, a list, an item page, a JSON endpoint, a
   sign-out link and a link to a second origin). The live test, `test_recipe_live.py`, runs only
   with `AR3_TOOL_BROWSER_LIVE=1` and drives a real headless Chrome in a temp XDG directory. It
   stores the secrets with `browser secret set`, runs the recipe through `browser do` and
   `ar3 tool`, and checks that no stored value is in any reply, file or directory but
   `secrets.json`. The fixture recipe, `fixtures/node/recipes/fixture.py`, shows a recipe for a
   site whose address is a node setting: it takes its one origin from `FIXTURE_BASE`.

Use `example.com` and `127.0.0.1` in tests, docs and fixtures. A real name, account, address
or page in a recipe or a test is a leak, because the repository mirrors publicly.

## Before you ask for a review

- Every verb has `help`, and every read verb has an `example` that `check` can run.
- A verb that changes the site is `kind="write"`.
- `origins` holds only what the recipe needs. The status probe changes nothing.
- No personal value is in the file: names, accounts, addresses and titles come in as arguments
  or node settings.
- A code verb uses the `Page` and nothing else to reach the browser. It never pastes a value
  into code text: it passes values to `evaluate`, `run_code` and `fetch`.
- The tests above pass, and `recipe <site> check` is healthy on a signed-in node.
