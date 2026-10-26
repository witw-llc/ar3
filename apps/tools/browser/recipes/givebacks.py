"""GiveBacks, the newsletter CMS, as a site recipe: sign in, list the drafts, open one in the editor,
pull its design and take a picture of it.

The verb names and flags are short and fixed, because people type them by habit: `login`,
`list [--limit N]`, `open --id ID`, `pull --id ID [-o FILE]` and `screenshot --id ID [--dir DIR]`. A
file comes back as an attachment and not as a path on this machine, so `-o` names the attachment and
`--dir` is accepted and ignored.

Two node settings say which organization this is: GIVEBACKS_BASE, the address of its GiveBacks site,
and GIVEBACKS_CAUSE_ID, its id in the API. The site's address is the recipe's one site origin, so it
is read when the recipe loads. The API host and Outlook web are the same for everyone. Outlook web is
the mailbox the sign-in's one-time passcode arrives in.

The passcode is not a stored secret. The recipe reads it from the mail, hides it from every output at
once (`page.hide`) and types it without showing it (`page.fill_hidden`). All of it happens in one sensitive
step (`page.sensitive`), opened before the mail is opened and closed after the code page is left. While the
step is open, nothing of the page is captured: a failure attaches no snapshot, and the error says only that
the step failed. The page files playwright-cli writes meanwhile are deleted when it ends, because they hold
the mail and the digits. The step ends with a cleanup that looks at the browser and says whether it holds any
of the code. When it cannot say so, the tool closes Chrome.
"""
import json
import os
import re
from urllib.parse import quote, urlsplit

from browser.recipe import BrowserError, NeedsPerson, Opt, Recipe, Result, UsageError

API_ORIGIN = "https://api.givebacks.com"
MESSAGES_API = API_ORIGIN + "/services/communication/messages"
OUTLOOK_START = "https://outlook.office.com/mail/"
OUTLOOK_HOSTS = ("outlook.office.com", "outlook.cloud.microsoft")
MICROSOFT_SIGN_IN_HOSTS = ("login.microsoftonline.com", "login.live.com")
# A page whose address holds one of these is not a signed-in page.
SIGN_IN_PATHS = ("/sign-in", "/sign_in", "/one-time-passcode")
# What the driver says when a "leave this page?" prompt holds a navigation, or stands in front of it.
PROMPT_SIGNS = ("timed out", "does not handle the modal state")
SCREENSHOT_NAME = "newsletter-preview.png"
DESIGN_NAME = "givebacks-design.json"
PASSCODE_LENGTH = 6

_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
_REF = re.compile(r"\[ref=(\w+)\]")
_SIX_DIGITS = re.compile(r"\b(\d{6})\b")
# The snapshot of a field prints its value after the ref: `textbox "PinInput" [ref=e11]: 4`.
_BOX_VALUE = re.compile(r"\]:\s*\S")
# More tabs than a sign-in opens. The cleanup closes at most this many before it gives up.
MAX_TABS = 8


def _site_origin(value):
    """scheme://host[:port] when `value` is a site address with nothing after the host, else ""."""
    try:
        parts = urlsplit(str(value).strip())
        port = parts.port
    except ValueError:
        return ""
    host = parts.hostname or ""
    if (
        parts.scheme not in ("http", "https") or len(host) > 253 or not _HOST.fullmatch(host)
        or parts.username is not None or parts.password is not None
        or parts.query or parts.fragment or parts.path.strip("/")
    ):
        return ""
    default = {"http": 80, "https": 443}[parts.scheme]
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


recipe = Recipe(
    "givebacks", "GiveBacks newsletters",
    origins=[origin for origin in (
        _site_origin(os.environ.get("GIVEBACKS_BASE", "")), API_ORIGIN,
        *(f"https://{host}" for host in OUTLOOK_HOSTS),
    ) if origin],
    aliases=["gb"], env=["GIVEBACKS_BASE", "GIVEBACKS_CAUSE_ID"], secrets=["GIVEBACKS_USER", "GIVEBACKS_PASS"],
)


# --- settings, pages and snapshots ------------------------------------------------------------------


def _settings(page):
    """(site address, cause id), or a usage failure that names each setting that is missing."""
    found, missing = {}, []
    for name in ("GIVEBACKS_BASE", "GIVEBACKS_CAUSE_ID"):
        try:
            found[name] = page.env(name)
        except UsageError:
            missing.append(name)
    if missing:
        raise UsageError(
            f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set. "
            "Set each on the node with `a8s vars <node> set env.NAME <value>`"
        )
    base = _site_origin(found["GIVEBACKS_BASE"])
    if not base:
        raise UsageError("GIVEBACKS_BASE is the address of the GiveBacks site, such as https://yourgroup.givebacks.com")
    return base, found["GIVEBACKS_CAUSE_ID"]


def _message_url(base, message_id, suffix=""):
    return f"{base}/messages/{quote(message_id, safe='')}{suffix}"


def _visit(page, url):
    """Go to `url`, leaving the page as a person would: the page's own "leave this page?" prompt is
    answered yes. Clearing `onbeforeunload` covers the common prompt. A navigation that times out, or
    that meets a prompt already standing, may be held by one that only a listener can raise, so that
    is answered and the navigation tried once more."""
    try:
        page.evaluate("() => { window.onbeforeunload = null; return true; }")
    except BrowserError:
        pass  # a page that cannot run it is left as it is; the navigation says what is wrong
    try:
        return page.go(url)
    except BrowserError as exc:
        if not any(sign in str(exc) for sign in PROMPT_SIGNS):
            raise
    try:
        page.command("dialog-accept")
    except BrowserError:
        pass  # no prompt was up
    return page.go(url)


def _ref(line):
    found = _REF.search(line)
    return found.group(1) if found else None


def _find_ref(snapshot, test):
    """The ref on the first line of `snapshot` that `test` accepts."""
    for line in snapshot.split("\n"):
        if test(line):
            ref = _ref(line)
            if ref:
                return ref
    return None


def _state(url):
    """in, out or unknown, from the address `/messages` ended on."""
    if any(marker in url for marker in SIGN_IN_PATHS):
        return "out"
    return "in" if urlsplit(url).path.startswith("/messages") else "unknown"


def _land_on_messages(page, base):
    """Open the message list. The site sends a signed-out page on to its sign-in a moment after it
    loads, so the address is read after a pause. Returns it."""
    _visit(page, f"{base}/messages")
    page.wait(3)
    return page.url()


# --- sign-in state ----------------------------------------------------------------------------------


@recipe.status
def status(page):
    """in, out or unknown, from where the site sends the message list."""
    base, _cause = _settings(page)
    return _state(_land_on_messages(page, base))


# --- login ------------------------------------------------------------------------------------------


@recipe.verb(
    "login",
    help=(
        "Sign in to GiveBacks.\n"
        "The seat's signed-in profile comes first. Otherwise this types the stored secrets GIVEBACKS_USER\n"
        "and GIVEBACKS_PASS, reads the one-time passcode from Outlook web in a second tab (Outlook web must\n"
        "be signed in on this seat), types it without showing it, and ticks \"trust this browser\"."
    ),
)
def login(page, args):
    base, _cause = _settings(page)
    url = _land_on_messages(page, base)
    state = _state(url)
    if state == "in":
        return "Already signed in."
    if state == "unknown":
        raise NeedsPerson(
            "cannot tell whether givebacks is signed in",
            "Look at the page in this seat's Chrome, and sign in by hand if it asks",
        )
    if "/one-time-passcode" in url:
        # A code page left open from before: a fresh sign-in is what sends a fresh code.
        page.log("A passcode page is already open. Signing out to get a fresh code.")
        sign_out = _find_ref(page.snapshot_text(), lambda line: "Sign Out" in line and "button" in line.lower())
        if sign_out:
            page.click(sign_out)
            page.wait(3)
        url = page.url()
    if "/sign-in" in url or "/sign_in" in url:
        _fill_login(page)
        page.wait(4)
        url = page.url()
    if "/one-time-passcode" in url:
        url = _sign_in_with_passcode(page, base)
    if _state(url) != "out":
        page.command("save")
        return "Login successful."
    raise BrowserError("Login failed - still not on the messages page.")


def _fill_login(page):
    """Type the stored user name and password into the sign-in form and submit it."""
    page.log("Logging in to GiveBacks...")
    email = password = submit = None
    for line in page.snapshot_text().split("\n"):
        lower = line.lower()
        if email is None and "email" in lower and "textbox" in lower:
            email = _ref(line)
        if password is None and "password" in lower and "textbox" in lower:
            password = _ref(line)
        # The "Sign In" button, not "Sign in with Apple" or "Sign in with Google".
        if (
            submit is None and "button" in lower and "sign in" in lower
            and "apple" not in lower and "google" not in lower
        ):
            submit = _ref(line)
    if not email or not password:
        raise BrowserError("Cannot find login form fields.")
    page.fill_secret(email, "GIVEBACKS_USER")
    page.fill_secret(password, "GIVEBACKS_PASS")
    if submit:
        page.click(submit)
    else:
        page.click(password)
        page.press("Enter")


# --- the one-time passcode --------------------------------------------------------------------------


def _find_code(snapshot):
    """The first standalone six-digit number of the snapshot that is not on a phone or fax line."""
    lines = snapshot.split("\n")
    for line in lines:
        found = _SIX_DIGITS.search(line)
        if found and "phone" not in line.lower() and "fax" not in line.lower():
            return found.group(1)
    for line in lines:
        if "generic" in line and "ref=" in line:
            found = _SIX_DIGITS.search(line)
            if found:
                return found.group(1)
    return None


def _on_site(url, base):
    return url == base or url.startswith(base + "/")


def _outlook_signed_out(page):
    """Raise NeedsPerson when the tab the seat is on is Microsoft's sign-in page."""
    if (urlsplit(page.url()).hostname or "") in MICROSOFT_SIGN_IN_HOSTS:
        raise NeedsPerson(
            "Outlook web is signed out in this seat, and GiveBacks wants a code from it",
            "Sign in to Outlook web by hand in this seat's Chrome, then log in again",
        )


def _passcode_from_outlook(page):
    """Open Outlook web in a second tab, open the message that holds the code and read the code from
    it. The code is hidden the moment it is known. Returns it, or None when the mail has none."""
    opened = None
    try:
        page.new_tab(OUTLOOK_START)
    except BrowserError as exc:
        opened = exc  # the tab may only be passing through Microsoft's sign-in: it is looked at again below
    page.wait(5)
    _outlook_signed_out(page)
    if (urlsplit(page.url()).hostname or "") not in OUTLOOK_HOSTS:
        raise opened or BrowserError("Outlook web did not open.")
    snapshot = page.snapshot_text()
    message = _find_ref(snapshot, lambda line: (
        "option" in line.lower()
        and any(word in line.lower() for word in ("passcode", "givebacks", "verification", "code"))
    ))
    if message:
        page.click(message)
        page.wait(2)
        snapshot = page.snapshot_text()
    code = _find_code(snapshot)
    if code:
        page.hide("PASSCODE", code)
    return code


def _back_to_the_site(page, base):
    """Close the Outlook tab and return to the sign-in tab."""
    if not _on_site(page.url(), base):
        page.close_tab()
        page.wait(1)
    page.select_tab(0)
    page.wait(1)


def _read_passcode(page, base):
    try:
        code = _passcode_from_outlook(page)
    except Exception:
        try:
            _back_to_the_site(page, base)
        except BrowserError:
            pass  # the failure being raised is the one to report
        raise
    _back_to_the_site(page, base)
    return code


def _pin_boxes(snapshot):
    """The refs of the code boxes of a snapshot of the code page, in order."""
    return [
        ref for ref in (_ref(line) for line in snapshot.split("\n") if "PinInput" in line and "textbox" in line)
        if ref
    ]


def _sign_in_with_passcode(page, base):
    """Read the code from Outlook web, type it into the six boxes, tick "trust this browser" and submit.
    Returns the address the site ended on.

    The page of the code is looked at first, while there is nothing to hide, so a page without six boxes
    fails with a message that says so. Everything from the first page that shows the code to the moment
    the code page is left is one sensitive step: it is opened before the Outlook tab, it deletes the page
    files of its calls, it reports a failure in one line that holds nothing of the pages, and it ends with
    `_passcode_is_gone`, which looks at the browser. Failing to find it clear closes Chrome."""
    found = len(_pin_boxes(page.snapshot_text()))
    if found < PASSCODE_LENGTH:
        raise BrowserError(f"Found {found} pin inputs, need {PASSCODE_LENGTH}.")
    tabs_before = len(page.tabs())
    with page.sensitive("passcode", cleanup=lambda: _passcode_is_gone(page, base, tabs_before)):
        page.log("A one-time passcode is required. Reading it from Outlook web.")
        page.wait(5)
        code = _read_passcode(page, base)
        if not code:
            raise NeedsPerson(
                "GiveBacks sent a one-time passcode and none could be read from Outlook web",
                "Read the mail by hand and sign in by hand in this seat's Chrome",
            )
        snapshot = page.snapshot_text()
        boxes = _pin_boxes(snapshot)
        if len(boxes) < PASSCODE_LENGTH:
            raise BrowserError(f"Found {len(boxes)} pin inputs, need {PASSCODE_LENGTH}.")
        for box, digit in zip(boxes, code[:PASSCODE_LENGTH]):
            page.fill_hidden(box, digit)
            page.wait(0.2)
        trust = _find_ref(snapshot, lambda line: (
            "trust" in line.lower() and ("checkbox" in line.lower() or "check" in line.lower())
        ))
        if trust:
            page.check(trust)
            page.wait(0.5)
        submit = _find_ref(snapshot, lambda line: (
            "Submit" in line and "button" in line.lower() and "disabled" not in line.lower()
        ))
        if submit:
            page.click(submit)
        else:
            # The form submits itself once the last box is full, or on Enter.
            page.click(boxes[-1])
            page.press("Enter")
        page.wait(3)
        page.wait(3)  # the site takes a moment more to send the browser on
        return page.url()


def _boxes_are_empty(page):
    """True when the page shows all its code boxes and none of them holds anything. A page that shows
    fewer than six boxes is not known to be empty: the boxes may be missing from the snapshot, not from the
    page."""
    boxes = [line for line in page.snapshot_text().split("\n") if "PinInput" in line and "textbox" in line]
    return len(boxes) >= PASSCODE_LENGTH and not any(_BOX_VALUE.search(line) for line in boxes)


def _passcode_is_gone(page, base, tabs_before):
    """The cleanup of the passcode step. True only when it has looked and found that the browser holds
    nothing of the code. `tabs_before` is how many tabs the seat had when the step began. Every tab past
    them was opened by the step, and the mail is in one of them, at whatever address it ended on, so each is
    closed. The first tab is the sign-in tab: it is not on Outlook web, and it is either not the code page
    or shows its boxes empty. A code page with digits in it is loaded again, and when that fails the boxes
    are emptied one by one. Every call here can fail, and a failure is not a guess: it ends the cleanup in
    an error, which the tool takes for "not confirmed" and answers by closing Chrome."""
    for _ in range(MAX_TABS):
        urls = page.tabs()
        if len(urls) <= tabs_before:
            break
        page.close_tab(len(urls) - 1)
    else:
        return False
    if len(urls) < tabs_before or (urlsplit(urls[0]).hostname or "") in OUTLOOK_HOSTS + MICROSOFT_SIGN_IN_HOSTS:
        return False
    if len(urls) > 1:
        page.select_tab(0)
    if "/one-time-passcode" not in page.url() or _boxes_are_empty(page):
        return True
    try:
        _visit(page, f"{base}/messages")
    except BrowserError:
        for box in _pin_boxes(page.snapshot_text()):
            page.fill_hidden(box, "")
    return "/one-time-passcode" not in page.url() or _boxes_are_empty(page)


# --- the read verbs ---------------------------------------------------------------------------------


def _filled(value):
    """False for what a script counts as false: null, false, 0 and "". An empty list is true."""
    return not (value is None or value is False or value == 0 or value == "")


@recipe.verb(
    "list", kind="read", signin=True,
    help="List recent newsletter drafts: id, status, send date and subject. The rows also come back as data.",
    args=[Opt("--limit", type=int, default=10, help="How many to list (default 10); raise it to reach older editions")],
    example="--limit 3",
)
def list_drafts(page, args):
    _base, cause = _settings(page)
    limit = max(1, int(args.limit or 10))
    reply = page.fetch(f"{MESSAGES_API}?cause_id={quote(cause, safe='')}&per_page={limit}", timeout=15)
    payload = found = reply.json
    if isinstance(payload, dict):
        # The first of these the API filled in, as a script reads `d.messages || d.data || d`.
        found = next((payload[key] for key in ("messages", "data") if _filled(payload.get(key))), payload)
    if not isinstance(found, list) or not all(isinstance(message, dict) for message in found[:limit]):
        raise BrowserError(f"Could not parse messages list. The API answered {reply.status}: {reply.text[:500]}")
    rows = []
    for message in found[:limit]:
        sent = message.get("sent_at")
        rows.append({
            "id": message.get("id", message.get("uuid", "?")),
            "status": message.get("status", "?"),
            "sent": str(sent)[:10] if sent else "",
            "subject": message.get("subject", message.get("name", "?")),
        })
    table = "\n".join(f"  {row['id']}  [{row['status']}]  {row['sent']}  {row['subject']}" for row in rows)
    return Result(text=table or "No messages.", data={"messages": rows})


@recipe.verb(
    "open", kind="read", signin=True,
    help="Open a newsletter in the editor, in this seat's Chrome.",
    args=[Opt("--id", required=True, help="Message/draft UUID")],
)
def open_editor(page, args):
    base, _cause = _settings(page)
    url = _message_url(base, args.id, "/design")
    _visit(page, url)
    return f"Opening editor: {url}"


@recipe.verb(
    "pull", kind="read", signin=True,
    help="Pull a newsletter's design JSON. It comes back as an attached file.",
    args=[
        Opt("--id", required=True, help="Message/draft UUID"),
        Opt(
            "-o", "--output",
            help=f"Name of the attached file (default {DESIGN_NAME}); only the last part of a path is used",
        ),
    ],
)
def pull(page, args):
    _base, cause = _settings(page)
    name = _attachment_name(args.output, DESIGN_NAME)
    reply = page.fetch(f"{MESSAGES_API}/{quote(args.id, safe='')}?cause_id={quote(cause, safe='')}", timeout=15)
    design = _design_of(reply.json)
    if design is None:
        raise BrowserError(f"Could not parse design from response. The API answered {reply.status}: {reply.text[:500]}")
    page.artifact(name, json.dumps(design, indent=2))
    body = design.get("body")
    rows = body.get("rows") if isinstance(body, dict) else None
    count = len(rows) if isinstance(rows, list) else 0
    return f"Pulling design for {args.id}...\nGot design: {count} rows\nAttached {name}"


def _design_of(payload):
    """The design a message holds, or None. The API keeps it as JSON text in `message.template`."""
    message = payload.get("message") if isinstance(payload, dict) else None
    template = message.get("template") if isinstance(message, dict) else None
    if not isinstance(template, str) or "counters" not in template:
        return None
    try:
        design = json.loads(template)
    except ValueError:
        return None
    return design if isinstance(design, dict) else None


def _attachment_name(output, default):
    """The name of the attached file: the last part of -o, which may be a path on someone's machine."""
    if not output:
        return default
    name = re.split(r"[\\/]", output)[-1].strip()
    if not name or name.startswith(".") or "\0" in name:
        raise UsageError("-o names the attached file, such as design.json")
    return name


@recipe.verb(
    "screenshot", kind="read", signin=True,
    help="Take a full-page screenshot of a newsletter. It comes back as an attached image.",
    args=[
        Opt("--id", required=True, help="Message/draft UUID"),
        Opt("--dir", default=".", help="Accepted and ignored: the screenshot is attached, not saved to a directory"),
    ],
)
def screenshot(page, args):
    base, _cause = _settings(page)
    _visit(page, _message_url(base, args.id))
    page.wait(4)
    try:
        page.screenshot(SCREENSHOT_NAME, full_page=True)
    except BrowserError:
        # The editor's own preview, when the message page would not give a picture.
        _visit(page, _message_url(base, args.id, "/design"))
        page.wait(5)
        try:
            page.screenshot(SCREENSHOT_NAME, full_page=True)
        except BrowserError as exc:
            raise BrowserError(f"Screenshot failed: {exc}") from None
    return "Screenshot saved."
