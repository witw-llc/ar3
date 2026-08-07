#!/usr/bin/env python3
"""Deterministic gardening checks for the project's private wiki.

    tools/wiki-gardener.py /path/to/wiki [--json]

The wiki charter (`Charter-Wiki` on the wiki) borrows ArchWiki's conventions:
a category and a state on the first line of every page, stale content flagged
rather than edited away, `_Sidebar.md` listing index pages only, and no page
left unreachable. This tool checks the mechanical half of that charter — the
part a script settles without reading for meaning. Judgement calls (is the
reason a good reason, is the category the right one) stay with the gardener.

Six defect classes:

    uncategorized     no charter category declared in the page header
    stateless         no charter state declared in the page header
    unexplained-flag  a banner without a reason, a date, or both
    orphan            unreachable from `_Sidebar.md` within two link hops
    sidebar-leaf      a `_Sidebar.md` entry that is not an index page
    dead-link         an internal link to a page that does not exist

Renamed pages are deliberately not checked. A rename leaves no trace in the
working tree — the old name is simply absent, indistinguishable from a page
that never existed — so the check would need git history, and history is where
the enforcement already lives: renames are forbidden after adoption, and the
wiki's log shows every one.

The tool is stdlib-only, reads nothing but the given directory, and touches no
network. Exit 0 when clean, 1 when defects are found.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from collections import deque
from pathlib import Path

CATEGORIES = (
    "Engine",
    "Experiments",
    "Journal",
    "Owner-Memo",
    "Plans",
    "Playbook",
    "Reference",
    "Spoken-Report",
    "Story",
    "Memory",
    "Decisions",
)

STATES = ("WIP", "Not Validated", "Validated", "Deprecated", "Archived")

FLAGS = ("Out of date", "Accuracy", "Expansion", "Merge", "Move", "Archive")

# The navigation surface: `Home` and `_Sidebar` declare neither category nor state.
EXEMPT_FROM_DECLARATIONS = ("Home",)

# Indexes that belong to no single category. The charter names both, so the
# sidebar carries them alongside the eleven category indexes.
CROSS_CUTTING_INDEXES = ("Attention", "Charters")

SIDEBAR = "_Sidebar.md"
MAX_HOPS = 2

DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
FENCE = re.compile(r"^\s*(```|~~~)", re.MULTILINE)
CODE_SPAN = re.compile(r"`[^`]*`")
WIKI_LINK = re.compile(r"\[\[([^\]]+)\]\]")
MD_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)")
MARKED = re.compile(r"(?:\*\*|__|`)\s*([^*_`]+?)\s*(?:\*\*|__|`)")
EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
# The charter's declaration line is `Category: X` · `State: Y`, so a field runs
# from its key to the first separator; everything past that is the next field.
FIELD_END = re.compile(r"[`·•|,;]|\s[—–-]\s|\*\*|__")


class Defect:
    def __init__(self, page: str, kind: str, detail: str) -> None:
        self.page = page
        self.kind = kind
        self.detail = detail

    def key(self) -> tuple[str, str, str]:
        return (self.page, self.kind, self.detail)

    def line(self) -> str:
        return f"{self.page}: {self.kind} — {self.detail}"

    def as_dict(self) -> dict[str, str]:
        return {"page": self.page, "defect": self.kind, "detail": self.detail}


def strip_code(text: str) -> str:
    """Drop fenced blocks and inline spans so example markup is not read as links."""
    out, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append(CODE_SPAN.sub(" ", line))
    return "\n".join(out)


def header_region(text: str) -> str:
    """The banner-and-category zone: everything above the first `##` heading."""
    lines = []
    for line in text.splitlines():
        if line.startswith("## "):
            break
        lines.append(line)
    return "\n".join(lines)


def index_names() -> set[str]:
    """Index pages: `Home`, each category's listing page, and the two
    cross-cutting indexes.

    A category index is named for its category, singular or pluralized —
    Engine → Engines, Owner-Memo → Owner-Memos, Story → Stories — so most of
    the set is derivable from the charter's eleven names rather than listed.
    """
    names = {"home"} | {name.lower() for name in CROSS_CUTTING_INDEXES}
    for category in CATEGORIES:
        lower = category.lower()
        names.update({lower, lower + "s", lower + "es"})
        if lower.endswith("y"):
            names.add(lower[:-1] + "ies")
    return names


def link_targets(text: str) -> list[str]:
    body = strip_code(text)
    targets = []
    for raw in WIKI_LINK.findall(body):
        targets.append(raw.split("|")[-1].strip())
    for raw in MD_LINK.findall(body):
        targets.append(raw.strip())
    return targets


def resolve(target: str, files: dict[str, str]) -> str | None:
    """Map a link target onto a wiki page name, or None when nothing matches.

    `files` maps a lowercased lookup key to the page name. GitHub renders
    spaces as hyphens and appends `.md` to page files; attachments (the spoken
    reports' `.m4a`) resolve by their own full name.
    """
    if not target or target.startswith("#") or EXTERNAL.match(target):
        return None
    candidate = urllib.parse.unquote(target).split("#")[0].split("?")[0].strip()
    if candidate.startswith("./"):
        candidate = candidate[2:]
    # The wiki is flat. A target carrying a path is a link into a tree that
    # does not exist once GitHub renders the page.
    if not candidate or "/" in candidate:
        return None
    for form in (candidate, candidate.replace(" ", "-")):
        for key in (form, form + ".md"):
            hit = files.get(key.lower())
            if hit is not None:
                return hit
    return None


def is_internal(target: str) -> bool:
    return bool(target) and not target.startswith("#") and not EXTERNAL.match(target)


def header_field(text: str, key: str) -> str | None:
    """The value of a `Key: value` declaration in the page header, if present.

    Markup around the key is ignored, so the charter's backticked form, a bold
    form and a bare one all read the same.
    """
    pattern = re.compile(
        rf"(?:^\s*>?\s*|[`·•|,;]\s*)[`*_]*\s*{key}\s*[`*_]*\s*:\s*(?P<value>.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(header_region(text))
    if match is None:
        return None
    return FIELD_END.split(match.group("value"))[0].strip(" .*_`[]")


def check_declaration(page: str, text: str, key: str, allowed: tuple[str, ...], kind: str,
                      absent: str) -> list[Defect]:
    if page in EXEMPT_FROM_DECLARATIONS:
        return []
    declared = header_field(text, key)
    if declared is None:
        return [Defect(page, kind, absent)]
    if declared.lower() not in {value.lower() for value in allowed}:
        return [Defect(page, kind, f'declares "{declared}", which the charter does not define')]
    return []


def banner_blocks(text: str) -> list[tuple[str, str]]:
    """Flag banners in the header region, as (flag name, full banner text).

    A banner is one of the six flag names set in bold or backticks near the top
    of the page; the rest of the line — and the rest of the blockquote, when the
    banner is quoted — is its reason and date.
    """
    lines = header_region(text).splitlines()
    known = {flag.lower(): flag for flag in FLAGS}
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index]
        flag = next(
            (known[m.strip().lower()] for m in MARKED.findall(line) if m.strip().lower() in known),
            None,
        )
        if flag is None:
            index += 1
            continue
        block = [line]
        index += 1
        # A blockquote banner carries its reason on the following quoted lines.
        quoted = line.lstrip().startswith(">")
        while quoted and index < len(lines) and lines[index].lstrip().startswith(">"):
            block.append(lines[index])
            index += 1
        blocks.append((flag, "\n".join(block)))
    return blocks


def check_flags(page: str, text: str) -> list[Defect]:
    defects = []
    for flag, block in banner_blocks(text):
        has_date = bool(DATE.search(block))
        remainder = DATE.sub(" ", block)
        remainder = re.sub(re.escape(flag), " ", remainder, flags=re.IGNORECASE)
        remainder = re.sub(r"[>*_#\[\]()|`\-–—:.,]", " ", remainder)
        has_reason = len(remainder.split()) >= 3
        if has_date and has_reason:
            continue
        if not has_date and not has_reason:
            missing = "no reason and no date"
        elif not has_date:
            missing = "no date"
        else:
            missing = "no reason"
        defects.append(Defect(page, "unexplained-flag", f'the "{flag}" banner carries {missing}'))
    return defects


def check_dead_links(page: str, text: str, files: dict[str, str]) -> list[Defect]:
    defects, seen = [], set()
    for target in link_targets(text):
        if not is_internal(target) or target in seen:
            continue
        seen.add(target)
        if resolve(target, files) is None:
            defects.append(Defect(page, "dead-link", f"links to {target}, which does not exist"))
    return defects


def check_sidebar_leaves(sidebar_text: str, files: dict[str, str]) -> list[Defect]:
    indexes = index_names()
    defects, seen = [], set()
    for target in link_targets(sidebar_text):
        page = resolve(target, files)
        if page is None or page in seen:
            continue
        seen.add(page)
        if page.lower() not in indexes:
            defects.append(
                Defect("_Sidebar", "sidebar-leaf", f"links to {page}, which is not an index page")
            )
    return defects


def check_orphans(pages: dict[str, str], sidebar_text: str, files: dict[str, str]) -> list[Defect]:
    """Reachability from the sidebar: a sidebar entry is one hop, what it links
    to is two. A record reached through its index page is therefore reachable."""
    reachable: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    for target in link_targets(sidebar_text):
        page = resolve(target, files)
        if page is not None and page not in reachable:
            reachable.add(page)
            queue.append((page, 1))
    while queue:
        page, hops = queue.popleft()
        if hops >= MAX_HOPS or page not in pages:
            continue
        for target in link_targets(pages[page]):
            nxt = resolve(target, files)
            if nxt is not None and nxt not in reachable:
                reachable.add(nxt)
                queue.append((nxt, hops + 1))
    return [
        Defect(page, "orphan", f"unreachable from _Sidebar within {MAX_HOPS} link hops")
        for page in pages
        if page not in reachable and not page.startswith("_")
    ]


def garden(wiki: Path) -> tuple[list[Defect], int]:
    files: dict[str, str] = {}
    pages: dict[str, str] = {}
    for path in sorted(wiki.iterdir()):
        if not path.is_file():
            continue
        files[path.name.lower()] = path.stem
        if path.suffix.lower() == ".md":
            pages[path.stem] = path.read_text(encoding="utf-8", errors="replace")

    defects: list[Defect] = []
    for page, text in pages.items():
        if not page.startswith("_"):
            defects += check_declaration(
                page, text, "category", CATEGORIES, "uncategorized",
                "no category declared above the first section",
            )
            defects += check_declaration(
                page, text, "state", STATES, "stateless",
                "no state declared above the first section",
            )
            defects += check_flags(page, text)
        defects += check_dead_links(page, text, files)

    sidebar = wiki / SIDEBAR
    if sidebar.is_file():
        sidebar_text = pages[sidebar.stem]
        defects += check_sidebar_leaves(sidebar_text, files)
        defects += check_orphans(pages, sidebar_text, files)
    else:
        defects.append(
            Defect("_Sidebar", "orphan", "no _Sidebar.md: reachability cannot be checked")
        )

    defects.sort(key=Defect.key)
    return defects, len(pages)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a wiki checkout against the gardening charter."
    )
    parser.add_argument("wiki", type=Path, help="path to a wiki checkout")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    if not args.wiki.is_dir():
        parser.error(f"not a directory: {args.wiki}")

    defects, page_count = garden(args.wiki)

    if args.json:
        print(
            json.dumps(
                {
                    "wiki": str(args.wiki),
                    "pages": page_count,
                    "defects": [d.as_dict() for d in defects],
                },
                indent=2,
            )
        )
    else:
        for defect in defects:
            print(defect.line())
        if defects:
            flagged = len({d.page for d in defects})
            print(f"\n{len(defects)} defects on {flagged} of {page_count} pages")
        else:
            print(f"{page_count} pages, no defects")

    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main())
