# Site recipes

A recipe provides named commands for one website. One Python module declares
the site, its permitted origins, and its verbs. The browser tool uses that
declaration for help, argument checks, health checks, and results.
See the [browser reference](../README.md#site-recipes) to run recipes.

A recipe is reviewed code. It acts as the account signed in to the browser
profile. Messages can run it but cannot install or change it. Recipes do not
run a model. A site change can require a reviewed code change.

## Bundled recipes

| Recipe | Site | Verbs |
|---|---|---|
| `givebacks`, alias `gb` | GiveBacks newsletter service | `login`, `list`, `open`, `pull`, `screenshot` |

### givebacks

Use `recipe gb` to read an organization's newsletters. The verbs do not change
newsletter content. `login` can change the browser's sign-in state.

| Verb | Arguments | Result |
|---|---|---|
| `login` | None | `Login successful.` or `Already signed in.` |
| `list` | `[--limit 10]` | Draft IDs, status, send dates, and subjects. Also attaches `result.json`. |
| `open` | `--id ID` | Opens the newsletter editor. |
| `pull` | `--id ID [-o FILE]` | Attaches design JSON. `-o` selects the attachment basename; it does not write to `FILE`. Default basename: `givebacks-design.json`. |
| `screenshot` | `--id ID [--dir DIR]` | Attaches a full-page image as `newsletter-preview.png`. Accepts but ignores `--dir`. |

Attachment names start with a timestamp. `list`, `open`, `pull`, and
`screenshot` require sign-in. The status probe opens the message list. It
returns `in` for that list, `out` for a sign-in or passcode page, and `unknown`
for another page. `out` and `unknown` stop the verb for manual action.
`recipe gb check` runs this probe and `list --limit 3`.

Set both required node settings. Replace the example values:

```bash
a8s vars browser-seat set env.GIVEBACKS_BASE https://yourgroup.givebacks.com
a8s vars browser-seat set env.GIVEBACKS_CAUSE_ID YOUR_CAUSE_ID
```

`GIVEBACKS_BASE` contains the scheme and host without a path. Other permitted
origins are `https://api.givebacks.com`, `https://outlook.office.com`, and
`https://outlook.cloud.microsoft`.

Store the sign-in values on the node's machine. Each command uses a hidden
prompt:

```bash
ar3 tool browser --node browser-seat secret set GIVEBACKS_USER --origin https://yourgroup.givebacks.com
ar3 tool browser --node browser-seat secret set GIVEBACKS_PASS --origin https://yourgroup.givebacks.com
```

If the site requests a passcode, the recipe reads it through Outlook web in
a separate tab. Sign in to Outlook in the same browser profile first. The
recipe reads the first passcode message, closes that tab, enters the code,
selects the trusted-browser option, and submits. A missing mail sign-in or
passcode returns `needs a person`.

The passcode uses a sensitive step. The recipe hides it in memory and never
returns it. Capture is disabled while that step is active. Driver snapshots
created during the step are deleted. A failure returns
`givebacks login: the passcode step failed` without page contents.

Cleanup closes tabs opened by the step. It confirms that the sign-in tab is
not an Outlook page and that any remaining passcode boxes are empty. If it
cannot confirm this, the tool closes Chrome before it reports the failure.
A video must be stopped before this sign-in step can run.

Run the recipe:

```bash
ar3 tool browser --node browser-seat -- recipe gb login
ar3 tool browser --node browser-seat -- recipe gb list --limit 5
```

For repeated use in a POSIX shell, define an alias. Keep the `--` separator:

```bash
alias gb='ar3 tool browser --node browser-seat -- recipe gb'
gb list --limit 5
gb pull --id NEWSLETTER_ID -o design.json
```

## Write a recipe

Put general recipes in the bundled recipe directory. Put private recipes in
`recipes/` under a registered browser node's root. Only the operator should
write there. A recipe has the permissions of the browser's OS user.

This example is `shop.py` for `https://shop.example.com`:

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

`recipe shop` shows help. `recipe shop orders --limit 3` lists orders.
`recipe shop check` runs the status probe and the declared read examples.

## Site declaration

`Recipe(name, title, origins, aliases=(), env=(), secrets=())`

| Field | Requirements |
|---|---|
| `name` | Lowercase letters, digits, and `-`. Must match the filename without `.py`. |
| `title` | One line for the recipe list. |
| `origins` | At least one allowed origin. Uses the browser secret-origin syntax. A wildcard subdomain excludes the bare domain. |
| `aliases` | Alternative names. Recipe names take precedence over aliases. Private aliases take precedence over bundled aliases. |
| `env` | Uppercase setting names. Set with `a8s vars NODE set env.NAME VALUE`. |
| `secrets` | Uppercase secret names. Store with `ar3 tool browser --node NODE secret set NAME --origin ORIGIN`. |

The module must define `recipe`. A failed import, missing declaration, or
mismatched name marks the recipe as broken. Other recipes remain available.
The loader ignores filenames that start with `_`. Keep helper code separate.
Import the recipe API from `browser.recipe`: `Recipe`, `Arg`, `Opt`, `Result`,
`UsageError`, `NeedsPerson`, and `BrowserError`.

## Verbs

| Declaration field | Function |
|---|---|
| Name | Lowercase letters, digits, and `-`. `help`, `check`, and `status` are built in. |
| `kind` | `"read"` by default, or `"write"`. Health checks run only read verbs. |
| `signin` | Set `True` to require a successful sign-in probe. Default: `False`. |
| `help` | Short description of the action. |
| `args` | List of `Arg` and `Opt` declarations. |
| `example` | Valid example arguments for the health check. Invalid examples prevent loading. |

A step verb uses a command-language template. Its result is the last step's
output. A code verb uses a function decorated with `@recipe.verb`. Use a code
verb for branches, loops, API calls, or tab operations.

## Arguments and templates

`Arg` declares a positional argument. `Opt` declares an option. Both accept
argparse keywords such as `type`, `default`, `required`, `help`, `nargs`,
`choices`, and `action`:

```python
args=[Arg("id", type=int), Opt("-o", "--out", default="."), Opt("-v", "--verbose", action="store_true")]
```

`Arg("title", rest=True)` joins the remaining words with spaces. Argument
names contain lowercase letters, digits, and `_`. Abbreviated long options
are refused. Invalid arguments return usage before browser work.

A step template can use `{name}` for an argument or declared setting.
Use `{{` and `}}` for literal braces. Each substituted value remains one
argument. Values that contain a newline, carriage return, or `<<` are refused.
Commands such as `type` and `find` keep the remaining line as text.

Do not put placeholders in `eval` or `run-code` steps. Pass values separately
to `page.evaluate(js, *values)` in a code verb. Literal `eval` and `run-code`
steps still require `ALLOW_EVAL` and a seat without secrets.

Each step retains origin checks, stop checks, and redaction. A step can call
another recipe, with at most one nested recipe call.

## Page API

A code verb receives a `Page`. Use this API to access the browser. Do not call
Playwright CLI or subprocesses directly. Each page call checks the stop token,
redacts its transcript, and applies the relevant origin checks.

| Method | Function |
|---|---|
| `go(url)` | Opens a URL on the declared origins. |
| `click(target)`, `dblclick(target)`, `fill(target, text)`, `press(key)`, `select(target, value)`, `check(target)`, `uncheck(target)`, `hover(target)` | Runs the corresponding browser command. Targets can be snapshot references, CSS selectors, or visible text. |
| `fill_secret(target, name)` | Types a declared stored secret. The secret's own origin restrictions also apply. |
| `hide(name, value)` | Adds a protected value for this script. Redacts it as `[secret:NAME]`. Requires 4 to 4096 characters. |
| `fill_hidden(target, text)` | Types a protected value without a command-line value or transcript value. Requires a field on a declared origin. |
| `no_page_files()` | Deletes driver page files created inside its context, including on failure. |
| `sensitive(name, cleanup)` | Disables capture while sensitive data is handled. Runs cleanup on exit. See Sensitive steps. |
| `wait(seconds)`, `wait_for(selector, seconds=10)`, `wait_for_url(substring, seconds=10)` | Waits for time or a page condition. A stop interrupts the wait. |
| `text(selector)`, `find(text)`, `url()`, `snapshot_text(target=None, depth=None, boxes=False)` | Reads redacted page text or its URL. |
| `screenshot(name, full_page=False)`, `download(target, seconds=30)` | Creates an attached file and returns its path. |
| `upload(target, *paths)`, `drop(target, *paths)` | Supplies files to the page. Browser-state paths are refused. |
| `back()`, `forward()`, `reload()` | Changes the page through browser history or reload. |
| `evaluate(js, *values)` | Calls a JavaScript function with separate JSON arguments. Returns JSON data. |
| `run_code(body, *values)` | Runs reviewed Playwright code with `page` and `values` in scope. |
| `fetch(url, method="GET", json=None, body=None, headers=None, timeout=60)` | Uses browser cookies for one request. Refuses redirects. Returns `status`, `ok`, `url`, `headers`, `text`, and `json`. |
| `http(url, method="GET", json=None, body=None, headers=None, timeout=30)` | Makes a request from this machine without browser cookies. Checks redirect origins before following them. |
| `tabs()`, `new_tab(url)`, `select_tab(index)`, `close_tab(index=None)` | Reads or changes tabs. `tabs()` returns URLs in order, without titles. An unreadable list is an error. |
| `local_storage_set(key, value)`, `resize(width, height)` | Sets page storage or window size. |
| `artifact(name, data_or_path)` | Attaches text, bytes, or a `pathlib.Path`. Applies the artifact safety checks. |
| `log(text)` | Adds a transcript line. Do not supply sensitive page text. |
| `env(name)` | Reads a declared setting. Missing or undeclared settings return a usage error. |
| `recipe(site, verb, *args)` | Calls another recipe with its own origins and sign-in probe. Returns its `Result`. One nested call is allowed. |
| `command(verb, *args)` | Runs a browser command with its normal checks. |

No method returns a stored secret. Returned page text passes through redaction.
A failed page call raises `BrowserError`. A recipe can catch it for a valid
alternative path. Plain fill, select, and press arguments cannot contain
protected values. Selectors derived from protected page text use element
positions instead of that text.

## Origins

`go`, `fetch`, `http`, and `new_tab` check the destination before a request.
After an action that can navigate, the tool also checks the resulting URL.
A click or server redirect can reach a different site before this check.
The check then fails the verb.

Browser `fetch` refuses redirects. Open the destination explicitly or use
`go` when navigation is intended. `http` checks each redirect before following
it. Reviewed code in `evaluate` and `run_code` is not subject to these checks.

A `file:` page fails regardless of the declared origins. The tool replaces it
with `about:blank` and deletes files from the failed step. A recipe that catches
this error receives a blank page.

Declare every required host, including API hosts. Keep the allowed list narrow.

## Results and errors

Return `Result(text=None, data=None, files=())`, a string, a dictionary, or a
list. Text becomes the step output. Data without text prints as compact JSON,
limited to 2000 characters. Complete data is attached as `result.json`.
Attached files must come from page methods such as `screenshot`, `download`,
or `artifact`.

| Error | Result |
|---|---|
| `UsageError(message)` | Shows the message and usage. Use for invalid values that argument parsing cannot detect. |
| `NeedsPerson(reason, how)` | Shows `needs a person: <reason>. <how>`. Use for manual codes, consent, or sign-in. |
| `BrowserError` | Shows the error and a permitted failure snapshot. |
| Other exception | Shows the recipe, verb, exception type, and redacted message. Attaches a permitted failure snapshot. |
| Sensitive-step exception | Shows a fixed step failure without page contents. See Sensitive steps. |

A stop during a verb is reported as a stop. All error output passes through
redaction.

## Sign-in

Declare a status probe and mark dependent verbs with `signin=True`:

```python
@recipe.status
def status(page):
    page.go("https://shop.example.com/account")
    return "in" if page.url().endswith("/account") else "out"
```

The probe must not change the site. It returns `"in"`, `"out"`, or `"unknown"`.
Other results count as unknown. A required probe runs before each verb.
Only `in` permits that verb to run. Other results request manual sign-in or
the recipe's explicit `login` verb. A `login` verb runs without the probe.
Health checks never call `login`.

## Sensitive steps

Use `with page.sensitive("passcode", cleanup):` for a one-time code or another
value read from a page. Open the context before the first sensitive page.
Close it only after the last sensitive page has been cleared or closed.

Read the value with `snapshot_text()` or `evaluate`. These return text to the
recipe without printing it. Do not use `text` or `find` for this purpose.
Call `hide` immediately. Type the value with `fill_hidden`.

During the context, capture commands are refused: `snap`, `shot`, `console`,
`video-start`, and `screenshot`. An active video prevents entry to the context.
The tool deletes driver page files created by the context and its cleanup.
This includes split passcode fields that ordinary value redaction cannot find.

Inside the context, transcript lines show only the operation and target.
They omit page results and input text. `log` still prints its supplied text;
do not pass sensitive text to it.

The tool always calls `cleanup()`, on success or failure. Cleanup must inspect
the browser and return `True` only after it confirms all sensitive data is
cleared. Close extra tabs. Clear passcode fields or leave the page. Inspect
the result again. Let a failed check raise an error; do not convert it to
`True`.

Any other return value, including `None`, or any cleanup exception closes
Chrome. Capture stays disabled for the rest of that script. Failure output
contains only `<recipe> <verb>: the <name> step failed`, plus any close note.
`NeedsPerson` and `UsageError` retain their author-written messages only when
cleanup confirms success. They must contain no sensitive page text.

## Tests and review

Put recipe tests in `apps/tools/tests/browser/`.

1. Use `fake_browser.py` for unit tests. Test every verb, signed-out behavior,
   invalid arguments, returned files, and navigation outside allowed origins.
2. Use `site_fixture.py` and `fixtures/site/` for a local live test. The fixture
   serves sign-in, list, detail, API, sign-out, and cross-origin pages.
3. Enable the live test with `AR3_TOOL_BROWSER_LIVE=1`. Use a temporary XDG
   directory and headless Chrome. Check that replies and artifacts contain
   no stored value. Only `secrets.json` may contain the fixture's secret.

The fixture recipe in `fixtures/node/recipes/fixture.py` reads its origin from
`FIXTURE_BASE`. Use `example.com` and `127.0.0.1` for examples and tests.
Do not include personal accounts, addresses, or private site content.

Before review, confirm these conditions:

- Each verb has help. Each read verb has an example that `check` can run.
- Every verb that changes site content uses `kind="write"`.
- Origins include only required hosts. The status probe changes no site data.
- Personal values come from arguments or node settings.
- Browser access uses `Page`. Values are passed as arguments, never inserted
  into executable source text.
- Unit tests pass. A live fixture test passes when available.
- The recipe health check passes on the intended signed-in profile.
