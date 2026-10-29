"""Site recipes: named commands for a website, with the browser tool as the engine.

A recipe is one Python module that declares a site: its name, the origins its pages live on, and
its verbs. A verb is a template of ordinary script lines (`Recipe.steps`) or a Python function that
drives the seat through the page facade (`Recipe.verb`, see page.py). The declaration is the one
source of the verb's help, its argument checks, its health check and its reply.

`recipe <site> <verb> [arguments]` is a line of the command language, so a recipe runs inside a
script and shares its seat lock, stop epoch, activity stamp, transcript, artifacts and scrubbing.

A recipe is code that acts with the seat's signed-in identity. It loads from `recipes/` beside this
module and, for a seat that is a registered `tool-browser` node, from `recipes/` in the root that node was
registered with. The working directory and the environment decide nothing. No message can install or
change a recipe.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import shlex
import sys
import threading
from pathlib import Path

from . import commands
from . import plc
from . import secretstore

__all__ = ["Arg", "BrowserError", "NeedsPerson", "Opt", "Recipe", "RecipeError", "Result", "UsageError"]

# What a failed page call raises. A recipe may catch it to take another branch.
BrowserError = plc.BrowserError

BUNDLED_DIR = Path(__file__).resolve().parent / "recipes"
NODE_DIRNAME = "recipes"
# A seat loads recipes from its node's root only when the node of its name runs this definition.
NODE_DEFINITION = "tool-browser"
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
ARG_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
BUILTIN_VERBS = ("help", "check", "status")
# Verbs whose argument is code. A value never becomes code text, so a step line for one holds no placeholder.
CODE_VERBS = ("eval", "run-code")
KINDS = ("read", "write")
STATES = ("in", "out", "unknown")
# A recipe may call another recipe, and that one may not call a third.
MAX_NESTING = 1
# The longest rendering of data as the step's output; result.json holds all of it.
DATA_PREVIEW = 2000
# Page calls kept in a step's output (the rest are counted), and the width of each.
MAX_CALL_LINES = 60
CALL_LINE_WIDTH = 120


class RecipeError(ValueError):
    """A declaration is wrong. It is raised while the module loads, so the recipe is listed as broken."""


class UsageError(Exception):
    """The caller's arguments are wrong, or an env name the recipe needs is not set. The step fails
    with the verb's usage."""


class NeedsPerson(Exception):
    """Only a person can go on: the page is signed out, wants a code, or shows a consent screen.
    `reason` says what is wrong and `how` says what to do. The step fails with
    `needs a person: <reason>. <how>`."""

    def __init__(self, reason, how=""):
        self.reason = str(reason).strip().rstrip(".")
        self.how = str(how).strip()
        if self.how and not self.how.endswith((".", "!", "?")):
            self.how += "."
        super().__init__(f"{self.reason}. {self.how}" if self.how else self.reason)


class RecipeFailure(plc.BrowserError):
    """A `recipe` step that failed. `output` holds the page calls the verb made before it failed."""

    def __init__(self, message, output=""):
        super().__init__(message)
        self.output = output


class SensitiveFailure(plc.BrowserError):
    """A sensitive step failed, or its cleanup could not confirm that the page holds nothing of it. The text
    names the step and holds nothing the page or the driver said, which may be the value the step guards.
    When the page could not be confirmed clear, the text also says that Chrome was closed, or could not be."""

    def __init__(self, name, seat, confirmed=True, closed=None):
        text = f"the {name} step failed"
        if not confirmed and closed:
            text += ". The page could not be confirmed clear, so Chrome was closed."
        elif not confirmed:
            text += (
                ". The page could not be confirmed clear, and Chrome could not be closed. "
                f"Close it with `ar3 tool browser --seat {seat} close`."
            )
        super().__init__(text)


@dataclasses.dataclass
class Result:
    """What a code verb returns: `text` is the step's output, `data` (anything JSON can hold) is
    attached as result.json, and `files` are paths made by the page facade (screenshot, download,
    artifact), attached to the reply. A verb may also return a str (text) or a dict or list (data)."""

    text: object = None
    data: object = None
    files: tuple = ()

    def __post_init__(self):
        self.files = tuple(self.files or ())


class Arg:
    """A positional argument. `Arg("title", rest=True)` takes the rest of the words, joined by single
    spaces. Other keywords go to argparse's `add_argument`: type, default, nargs, choices, help, metavar."""

    def __init__(self, name, *, rest=False, **keywords):
        self.name = name
        self.rest = rest
        self.keywords = keywords


class Opt:
    """An option, `Opt("--limit", type=int, default=10, help="...")`. The flags and keywords go to
    argparse's `add_argument`, so a recipe can keep the exact flags of an existing command line."""

    def __init__(self, *flags, **keywords):
        self.flags = flags
        self.keywords = keywords


class _ParseError(Exception):
    pass


class _Formatter(argparse.HelpFormatter):
    def __init__(self, prog):
        # A usage line is one line, whatever the terminal is.
        super().__init__(prog, width=10_000)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _ParseError(message)


def _first_line(text, width=160):
    line = " ".join(str(text).split("\n", 1)[0].split())
    return line if len(line) <= width else line[: width - 3] + "..."


# --- the declaration -------------------------------------------------------------------------------

_BRACES = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}|[{}]")


def _template_names(text):
    names = []
    for match in _BRACES.finditer(text):
        if match.group(1):
            names.append(match.group(1))
        elif match.group(0) not in ("{{", "}}"):
            raise ValueError("a single brace; write {{ or }} for a literal one")
    return names


def _fill_template(text, values):
    return _BRACES.sub(lambda match: values[match.group(1)] if match.group(1) else match.group(0)[0], text)


def _one_value(name, value):
    """A step verb binds each value as one value. A newline, a carriage return, a NUL or `<<` could
    start a second line or open a block in the text a value is inserted into, so none is accepted."""
    if value is None:
        raise UsageError(f"{name} has no value")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, dict, set)):
        raise UsageError(f"{name} holds several values; a step verb binds one")
    text = str(value)
    if any(mark in text for mark in ("\n", "\r", "\0", "<<")):
        raise UsageError(f"{name} cannot hold a newline or <<")
    return text


class Verb:
    def __init__(self, site, name, *, kind, signin, help, args, example, func=None, steps=None, env=()):
        where = f"recipe {site}, verb {name}"
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise RecipeError(f"recipe {site}: a verb name is lower case letters, digits and -, such as list-all")
        if name in BUILTIN_VERBS:
            raise RecipeError(f"{where}: {name} is built in")
        if kind not in KINDS:
            raise RecipeError(f"{where}: kind is read or write")
        if not isinstance(signin, bool):
            raise RecipeError(f"{where}: signin is True or False")
        if not isinstance(help, str) or not help.strip():
            raise RecipeError(f"{where}: help says what the verb does")
        self.name, self.kind, self.signin, self.help = name, kind, signin, help.strip()
        self.example = example
        self.func = func
        self.parser = _Parser(
            prog=f"recipe {site} {name}", exit_on_error=False, add_help=False,
            allow_abbrev=False, formatter_class=_Formatter,
        )
        self.specs = list(args)
        self.dests = []
        self.rests = []
        for spec in self.specs:
            self._add(where, spec)
        if example is not None:
            try:
                self.parse(shlex.split(example))
            except (ValueError, UsageError) as exc:
                raise RecipeError(f"{where}: the example does not fit the arguments: {exc}") from None
        self.lines = []
        self.used = set()
        if steps is not None:
            self._compile(where, steps, set(self.dests) | set(env))

    def _add(self, where, spec):
        try:
            if isinstance(spec, Arg):
                if not isinstance(spec.name, str) or not ARG_NAME.fullmatch(spec.name):
                    raise RecipeError(f"{where}: an Arg name is lower case letters, digits and _, such as title")
                keywords = dict(spec.keywords)
                if spec.rest:
                    if {"type", "choices", "action"} & set(keywords):
                        raise RecipeError(f"{where}: a rest argument is text; it takes no type, choices or action")
                    keywords.setdefault("nargs", "+")
                    if keywords["nargs"] not in ("+", "*"):
                        raise RecipeError(f"{where}: a rest argument takes nargs + or *")
                action = self.parser.add_argument(spec.name, **keywords)
                if spec.rest:
                    self.rests.append(action.dest)
            elif isinstance(spec, Opt):
                if not spec.flags or not all(isinstance(flag, str) and flag.startswith("-") for flag in spec.flags):
                    raise RecipeError(f"{where}: an Opt is flags that start with -, such as --limit")
                action = self.parser.add_argument(*spec.flags, **spec.keywords)
            else:
                raise RecipeError(f"{where}: args holds Arg and Opt")
        except (argparse.ArgumentError, TypeError, ValueError) as exc:
            if isinstance(exc, RecipeError):
                raise
            raise RecipeError(f"{where}: {exc}") from None
        if action.dest in self.dests or not ARG_NAME.fullmatch(action.dest):
            raise RecipeError(f"{where}: {action.dest!r} is not a name of its own for an argument")
        self.dests.append(action.dest)

    def _compile(self, where, text, names):
        """Split the steps into lines of (verb, argument templates). A template is split into words
        before any value goes in, so a value is always one word whatever it holds."""
        if not isinstance(text, str):
            raise RecipeError(f"{where}: steps is text, one command to a line")
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            problem = f"{where}, steps line {number}"
            if "<<" in line:
                raise RecipeError(f"{problem}: a step cannot open a block")
            verb = line.split(None, 1)[0]
            if "{" in verb or "}" in verb:
                raise RecipeError(f"{problem}: the command word is fixed text")
            if verb in commands.RAW_ARGS:
                rest = line.split(None, 1)[1:]
                templates = rest
            else:
                try:
                    verb, *templates = shlex.split(line)
                except ValueError as exc:
                    raise RecipeError(f"{problem}: {exc}") from None
            for template in templates:
                try:
                    used = _template_names(template)
                except ValueError as exc:
                    raise RecipeError(f"{problem}: {exc}") from None
                if used and verb in CODE_VERBS:
                    raise RecipeError(
                        f"{problem}: {verb} takes code, so no value ({{{used[0]}}}) goes into it. A code verb "
                        "passes values to page.evaluate(js, *values) as data"
                    )
                unknown = [name for name in used if name not in names]
                if unknown:
                    raise RecipeError(
                        f"{problem}: {{{unknown[0]}}} is neither an argument nor a declared env name"
                    )
                self.used.update(used)
            self.lines.append((verb, templates))
        if not self.lines:
            raise RecipeError(f"{where}: steps has no command")

    def usage(self):
        return self.parser.format_usage().strip()

    def parse(self, words):
        """The parsed arguments of `words`, a Namespace. A rest argument is one string."""
        try:
            namespace = self.parser.parse_args(list(words))
        except (_ParseError, argparse.ArgumentError) as exc:
            raise UsageError(str(exc)) from None
        for dest in self.rests:
            value = getattr(namespace, dest)
            if isinstance(value, list):
                setattr(namespace, dest, " ".join(value))
        return namespace

    def bind(self, values):
        """The lines of a step verb as (command, [arguments]) with `values` in them."""
        text = {name: _one_value(name, values.get(name)) for name in self.used}
        return [(verb, [_fill_template(template, text) for template in templates]) for verb, templates in self.lines]

    def describe(self, site):
        """The verb's block in `recipe <site>`."""
        flags = [self.kind] + (["needs sign-in"] if self.signin else [])
        lines = [f"{self.name} ({', '.join(flags)})", *("  " + line for line in self.help.splitlines())]
        lines.append(f"  {self.usage()}")
        for spec in self.specs:
            label = spec.name if isinstance(spec, Arg) else "/".join(spec.flags)
            text = spec.keywords.get("help")
            if text:
                lines.append(f"    {label}: {text}")
        if self.example is not None:
            lines.append(f"  example: recipe {site} {self.name} {self.example}".rstrip())
        return lines


class Recipe:
    """One site. `origins` are the pages the recipe may open and fetch from, in the origin syntax of
    `browser secret set`: `https://app.example.com`, or `https://*.example.org` for any subdomain.
    `env` names the node settings it reads (`a8s vars <node> set env.NAME value`) and `secrets` the
    stored secrets it may type (`browser secret set NAME`)."""

    def __init__(self, name, title, origins, aliases=(), env=(), secrets=()):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise RecipeError("a recipe name is lower case letters, digits and -, such as givebacks")
        self.name = name
        if not isinstance(title, str) or not title.strip() or "\n" in title:
            raise RecipeError(f"recipe {name}: the title is one line")
        self.title = title.strip()
        self.aliases = self._names(aliases, "alias", lambda text: NAME.fullmatch(text))
        if name in self.aliases:
            raise RecipeError(f"recipe {name}: {name} is the name, not an alias")
        self.env = self._names(env, "env name", self._upper)
        self.secrets = self._names(secrets, "secret name", self._upper)
        if isinstance(origins, str):
            origins = [origins]
        if not origins:
            raise RecipeError(f"recipe {name}: declare at least one origin, such as https://app.example.com")
        try:
            self.origins = secretstore.check_origins(list(origins))
        except secretstore.SecretStoreError as exc:
            raise RecipeError(f"recipe {name}: origins: {exc}") from None
        self.origin_parts = [secretstore.origin_parts(origin) for origin in self.origins]
        self.verbs = {}
        self.probe = None

    def _names(self, names, what, valid):
        if isinstance(names, str):
            names = [names]
        kept = []
        for text in names:
            if not isinstance(text, str) or not valid(text) or text in kept:
                raise RecipeError(f"recipe {self.name}: {what} {text!r} is not a valid, distinct {what}")
            kept.append(text)
        return kept

    @staticmethod
    def _upper(text):
        try:
            secretstore.check_name(text)
        except secretstore.SecretStoreError:
            return False
        return True

    def status(self, function):
        """Declare the sign-in probe, `def status(page)`. It returns "in", "out" or "unknown" and has
        no side effect on the site: it opens a page and reads, and never signs in."""
        if self.probe is not None:
            raise RecipeError(f"recipe {self.name}: one status probe")
        self.probe = function
        return function

    def verb(self, name, *, kind="read", signin=False, help, args=(), example=None):
        """Declare a code verb: `@recipe.verb("list", help="...")` over `def list_(page, args)`. The
        function returns a Result, a str (text), or a dict or list (data)."""

        def declare(function):
            self._add(Verb(
                self.name, name, kind=kind, signin=signin, help=help, args=args,
                example=example, func=function,
            ))
            return function

        return declare

    def steps(self, name, *, kind="read", signin=False, help, args=(), example=None, steps):
        """Declare a step verb: script lines with `{argument}` and `{ENV_NAME}` placeholders. Each
        value goes in as one value. `{{` and `}}` write a brace."""
        self._add(Verb(
            self.name, name, kind=kind, signin=signin, help=help, args=args,
            example=example, steps=steps, env=self.env,
        ))

    def _add(self, verb):
        if verb.name in self.verbs:
            raise RecipeError(f"recipe {self.name}: verb {verb.name} is declared twice")
        self.verbs[verb.name] = verb

    def validate(self):
        """The checks that need the whole declaration; the loader runs them."""
        if any(verb.signin for verb in self.verbs.values()) and self.probe is None:
            raise RecipeError(
                f"recipe {self.name}: a verb needs sign-in, so the recipe needs a @recipe.status probe"
            )
        if not self.verbs:
            raise RecipeError(f"recipe {self.name}: declares no verb")

    def describe(self):
        lines = [f"{self.name}{' (' + ', '.join(self.aliases) + ')' if self.aliases else ''}: {self.title}"]
        lines.append("origins: " + ", ".join(self.origins))
        if self.env:
            lines.append("env: " + ", ".join(self.env))
        if self.secrets:
            lines.append("secrets: " + ", ".join(self.secrets))
        lines.append("sign-in probe: " + ("yes" if self.probe else "no"))
        for verb in self.verbs.values():
            lines.append("")
            lines.extend(verb.describe(self.name))
        lines.append("")
        lines.append(f"also: recipe {self.name} help [verb], recipe {self.name} status, recipe {self.name} check")
        return lines


# --- the loader ------------------------------------------------------------------------------------


class Entry:
    """A recipe file, loaded or broken. `source` is "bundled" or "node"; `replaces` is True when it
    stands in for a bundled recipe of the same name."""

    def __init__(self, name, path, source, recipe=None, error=None, replaces=False):
        self.name, self.path, self.source = name, path, source
        self.recipe, self.error, self.replaces = recipe, error, replaces


def _load_file(path, source):
    stem = path.stem
    module_name = f"ar3_browser_recipe_{hashlib.sha256(str(path).encode()).hexdigest()[:10]}_{stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        # A node's directory is the operator's: importing from it writes nothing into it.
        keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = keep
        found = getattr(module, "recipe", None)
        if not isinstance(found, Recipe):
            raise RecipeError("the module defines no `recipe` object (a Recipe)")
        if found.name != stem:
            raise RecipeError(f"the recipe is named {found.name!r} and the file is {path.name}")
        found.validate()
    except (Exception, SystemExit) as exc:
        sys.modules.pop(module_name, None)
        text = str(exc) if isinstance(exc, RecipeError) else f"{type(exc).__name__}: {exc}"
        return Entry(stem, path, source, error=_first_line(text))
    return Entry(stem, path, source, recipe=found)


class Library:
    """The recipes a run can name: the bundled ones, then those of each directory in `dirs`, which
    are the registered node's own. A recipe of the same name in a later directory replaces the one
    before it. Files load on first use, and a file that fails to load is a broken entry that stops
    nothing else."""

    def __init__(self, dirs=()):
        self.dirs = [Path(directory) for directory in dirs]
        self._entries = None

    def entries(self):
        if self._entries is None:
            self._entries = self._load()
        return list(self._entries.values())

    def _load(self):
        found = {}
        for source, directory in [("bundled", BUNDLED_DIR)] + [("node", d) for d in self.dirs]:
            try:
                names = sorted(
                    path for path in directory.iterdir()
                    if path.suffix == ".py" and not path.name.startswith(("_", ".")) and path.is_file()
                )
            except OSError:
                continue
            for path in names:
                entry = _load_file(path, source)
                before = found.get(entry.name)
                entry.replaces = before is not None and (before.source == "bundled" or before.replaces)
                found[entry.name] = entry
        return dict(sorted(found.items()))

    def find(self, site):
        """The entry whose name, or else whose alias, is `site`."""
        wanted = str(site).lower()
        entries = self.entries()
        for entry in entries:
            if entry.name == wanted:
                return entry
        # An alias two recipes claim goes to the node's own recipe, then to the last by name.
        claimants = sorted(
            (entry for entry in entries if entry.recipe and wanted in entry.recipe.aliases),
            key=lambda entry: (entry.source != "bundled", entry.name),
        )
        return claimants[-1] if claimants else None

    def listing(self):
        lines = []
        for entry in self.entries():
            marks = []
            if entry.source != "bundled":
                marks.append("[node]")
            if entry.replaces:
                marks.append("[replaces bundled]")
            if entry.error:
                marks.append(f"[broken: {entry.error}]")
            head = entry.name
            if entry.recipe:
                if entry.recipe.aliases:
                    head += f" ({', '.join(entry.recipe.aliases)})"
                head += f"  {entry.recipe.title}"
            lines.append("  ".join([head] + marks))
        return "\n".join(lines) if lines else "no recipes"


# --- the runner ------------------------------------------------------------------------------------


def _as_result(value):
    if value is None:
        return Result()
    if isinstance(value, Result):
        return value
    if isinstance(value, str):
        return Result(text=value)
    if isinstance(value, (dict, list)):
        return Result(data=value)
    raise TypeError(f"a verb returns a Result, a str, a dict or a list, not {type(value).__name__}")


def _data_preview(data, clean=lambda text: text):
    """The data as compact JSON, scrubbed by `clean` and then cut: a cut value is not found by the scrub."""
    text = clean(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    if len(text) <= DATA_PREVIEW:
        return text
    return f"{text[:DATA_PREVIEW]}... ({len(text)} characters; result.json holds all of it)"


def _write_artifact(path, text):
    """Write `path` through a file made beside it, so a reader never sees half a file."""
    aside = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with os.fdopen(os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(aside, path)


class _Call:
    """One `recipe` verb in a run, or a recipe another recipe called. The page facade reads `seat`,
    `run`, `allow_eval`, `epoch`, `library`, `lines` and `depth` from it and calls `nested`."""

    def __init__(self, seat, run, allow_eval, epoch, library, lines=None, depth=0):
        self.seat, self.run, self.allow_eval, self.epoch = seat, run, allow_eval, epoch
        self.library = library
        self.lines = [] if lines is None else lines
        self.depth = depth

    # -- what the facade uses

    def say(self, text):
        """Add a line for a page call to the step's output, scrubbed and cut to one short line, and
        return its place. A nested recipe's lines sit one step further in."""
        self.lines.append("  " * (self.depth + 1) + _first_line(self.run.clean(text), CALL_LINE_WIDTH))
        return len(self.lines) - 1

    def amend(self, index, text):
        self.lines[index] = "  " * (self.depth + 1) + _first_line(self.run.clean(text), CALL_LINE_WIDTH)

    def nested(self, site, verb, words):
        """Run another recipe's verb for the recipe that is running. Returns its Result."""
        if self.depth >= MAX_NESTING:
            raise plc.BrowserError("a recipe may call another recipe one level deep, not more")
        child = _Call(self.seat, self.run, self.allow_eval, self.epoch, self.library, self.lines, self.depth + 1)
        entry = self.library.find(site)
        if entry is None:
            raise RecipeFailure(f"unknown recipe: {site}")
        if entry.recipe is None:
            raise RecipeFailure(f"recipe {entry.name} is broken: {entry.error}")
        found = entry.recipe.verbs.get(verb)
        if found is None:
            raise RecipeFailure(f"recipe {entry.name} has no verb {verb}")
        return child.execute(entry.recipe, found, words)

    # -- the verb

    def dispatch(self, words):
        if not words or words == ["help"]:
            return self.library.listing()
        entry = self.library.find(words[0])
        if entry is None:
            known = ", ".join(entry.name for entry in self.library.entries())
            raise RecipeFailure(
                f"unknown recipe: {words[0]}. " + (f"Known recipes: {known}." if known else "No recipes are installed.")
            )
        if entry.recipe is None:
            raise RecipeFailure(f"recipe {entry.name} is broken: {entry.error}")
        rec, rest = entry.recipe, words[1:]
        if not rest or rest[0] == "help":
            return self.help(rec, entry, rest[1:])
        name, args = rest[0], rest[1:]
        if name == "status":
            return self.status(rec)
        if name == "check":
            return self.check(rec)
        verb = rec.verbs.get(name)
        if verb is None:
            raise RecipeFailure(
                f"recipe {rec.name} has no verb {name}. Verbs: {', '.join(rec.verbs)}, help, status, check"
            )
        return self.render(verb, self.execute(rec, verb, args))

    def help(self, rec, entry, rest):
        if rest:
            verb = rec.verbs.get(rest[0])
            if verb is None:
                raise RecipeFailure(f"recipe {rec.name} has no verb {rest[0]}. Verbs: {', '.join(rec.verbs)}")
            return "\n".join(verb.describe(rec.name))
        lines = rec.describe()
        if entry.source != "bundled":
            lines[0] += "  [node]"
        return "\n".join(lines)

    def _page(self, rec):
        from . import page

        return page.Page(self, rec)

    def _open(self):
        self.run.page_used = True
        if self.depth == 0:
            commands.session.ensure_running(self.seat, script_epoch=self.epoch)

    def failure(self, message):
        """A RecipeFailure for `message`. A stop that landed meanwhile is the reason, whatever
        the page said when its browser was closed under it."""
        if message != commands.session.STOP_MESSAGE and commands.session.stop_epoch(self.seat) != self.epoch:
            message = commands.session.STOP_MESSAGE
        return RecipeFailure(self.run.clean(message))

    @contextlib.contextmanager
    def failing(self, label, usage=None):
        """Turn what a recipe raises into the step's failure."""
        try:
            yield
        except RecipeFailure:
            raise
        except SensitiveFailure as exc:
            exc.__context__ = None  # it was raised while the error it replaces was in flight
            raise self.failure(f"{label}: {exc}") from None
        except UsageError as exc:
            raise self.failure(f"{exc}\n{usage}" if usage else str(exc)) from None
        except NeedsPerson as exc:
            raise self.failure(f"needs a person: {exc}") from None
        except plc.BrowserError as exc:
            raise self.failure(str(exc)) from None
        except (Exception, SystemExit) as exc:
            raise self.failure(f"{label}: {type(exc).__name__}: {exc}") from None

    def probe(self, rec, page):
        state = rec.probe(page)
        state = state.strip().lower() if isinstance(state, str) else ""
        return state if state in STATES else "unknown"

    def status(self, rec):
        if rec.probe is None:
            raise RecipeFailure(f"recipe {rec.name} declares no status probe")
        with self.failing(f"{rec.name} status"):
            self._open()
            page = self._page(rec)
            return self.probe(rec, page)

    def gate(self, rec, verb, page):
        """A verb that needs sign-in runs only on a page the probe says is signed in. Anything else
        stops it for a person: the recipe never signs in on a guess. `login` is how a person's
        sign-in is done, so it runs without the probe."""
        if not verb.signin or verb.name == "login":
            return
        state = self.probe(rec, page)
        self.say(f"status -> {state}")
        if state == "in":
            return
        how = f"Sign in by hand in the Chrome of seat {self.seat}"
        if "login" in rec.verbs:
            how = f"Run `recipe {rec.name} login`, or sign in by hand in the Chrome of seat {self.seat}"
        if state == "out":
            raise NeedsPerson(f"{rec.name} is signed out on seat {self.seat}", how)
        raise NeedsPerson(f"cannot tell whether {rec.name} is signed in on seat {self.seat}", how)

    def execute(self, rec, verb, words):
        """Parse, gate and run one verb. Returns its Result."""
        with self.failing(f"{rec.name} {verb.name}", verb.usage()):
            namespace = verb.parse(words)
            self._open()
            page = self._page(rec)
            self.gate(rec, verb, page)
            if verb.func is not None:
                return _as_result(verb.func(page, namespace))
            values = {dest: getattr(namespace, dest) for dest in verb.dests}
            values.update({name: page.env(name) for name in verb.used if name in rec.env})
            last = ""
            for command, args in verb.bind(values):
                last = page.command(command, *args)
            return Result(text=last)

    def check(self, rec):
        """The status probe, then each read verb that declares an example, each reported healthy or
        failing. One failing verb fails the step. `login` is never run: a check does not sign in."""
        report, failing = [], []
        if rec.probe is not None:
            try:
                report.append(f"status: {self.status(rec)}")
            except RecipeFailure as exc:
                self._stopped(exc)
                report.append(f"status: failing: {_first_line(exc)}")
                failing.append("status")
        for verb in rec.verbs.values():
            if verb.kind != "read" or verb.example is None or verb.name == "login":
                continue
            try:
                self.execute(rec, verb, shlex.split(verb.example))
                report.append(f"{verb.name}: healthy")
            except RecipeFailure as exc:
                self._stopped(exc)
                report.append(f"{verb.name}: failing: {_first_line(exc)}")
                failing.append(verb.name)
        if not report:
            return "nothing to check: no read verb declares an example"
        if failing:
            self.lines.extend(report)
            raise RecipeFailure("check failed: " + ", ".join(failing))
        return "\n".join(report + [f"check: healthy ({len(report)})"])

    def _stopped(self, failure):
        if str(failure) == commands.session.STOP_MESSAGE:
            raise failure

    def render(self, verb, result):
        """The step's text, with the verb's data and files attached to the run."""
        with self.failing("result"):
            text = str(result.text) if result.text is not None else ""
            if result.data is not None:
                try:
                    body = json.dumps(result.data, indent=2, ensure_ascii=False) + "\n"
                    if result.text is None:
                        text = _data_preview(result.data, self.run.clean)
                except (TypeError, ValueError) as exc:
                    raise plc.BrowserError(f"{verb.name} returned data that is not JSON: {exc}") from None
                path = commands.artifact(self.seat, "result.json")
                _write_artifact(path, self.run.clean(body))
                self.run.attach(path)
            for name in result.files:
                self.run.attach(self._attachable(name))
        return text

    def _attachable(self, name):
        """A file a verb returns must be one the facade made: inside the seat's artifacts."""
        artifacts = os.path.realpath(commands.session.artifacts_dir(self.seat))
        path = os.path.realpath(os.fspath(name))
        if not path.startswith(artifacts + os.sep) or not os.path.isfile(path):
            raise plc.BrowserError(
                "a verb attaches only files its page made (screenshot, download, artifact); "
                f"{_first_line(self.run.clean(str(name)))} is not one"
            )
        return os.fspath(name)

    def output(self, text=""):
        """The step's output: the page calls, then the text."""
        lines = list(self.lines)
        if len(lines) > MAX_CALL_LINES:
            lines = lines[:MAX_CALL_LINES] + [f"  ... {len(lines) - MAX_CALL_LINES} more lines"]
        if text:
            lines.append(text)
        return "\n".join(lines)


def invoke(seat, words, run, allow_eval, epoch, library=None):
    """The `recipe` verb: list the recipes, show one, or run one of its verbs. Returns the step's
    output, or raises RecipeFailure, which carries what the page calls did before the failure."""
    library = library if library is not None else Library()
    if epoch is None:
        epoch = commands.session.stop_epoch(seat)
    call = _Call(seat, run, allow_eval, epoch, library)
    try:
        text = call.dispatch(list(words))
    except RecipeFailure as exc:
        exc.output = call.output()
        raise
    return call.output(text)
