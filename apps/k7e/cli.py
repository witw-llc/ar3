"""k7e CLI — commands table, dispatcher, and main argparse entry."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import config
from engine import (
    init,
    get,
    list_nodes,
    recall,
    store_entry,
    rebuild_mocs,
    reindex,
    search,
    stats,
    store_asset,
    append_entry,
    compile_tag,
    process_pending_embeddings,
    supersede,
)
from distill import distill, consolidate
from hygiene import run_audit


COMMANDS: list[tuple[str, str, str]] = [
    ("search",      "<query> [--limit N] [--json] [--ids]", "Hybrid search (BM25 + semantic + metadata)."),
    ("get",         "<id>",                            "Read a full knowledge entry."),
    ("supersede",   "<old_id> <new_id>",               "Mark an entry as superseded by a newer one."),
    ("store",       "<title> [--tags] [--aliases]",    "Create a new entry (content from stdin or --content)."),
    ("append",        "<id> --section <name>",           "Append to an existing entry's section."),
    ("asset",       "<file>",                          "Store binary (content-addressed, deduped). Prints path."),
    ("compile",     "<tag> [--dry-run]",               "Synthesize entries for a tag into a reference page."),
    ("recall",      "<text> [--limit N]",            "Recall relevant knowledge for a topic or conversation (RAG)."),
    ("distill", "<file|dir> [--dry-run]",          "Extract knowledge from raw experience files."),
    ("consolidate", "[--dry-run]",                 "Find and merge duplicate nodes."),
    ("reindex",     "[--embeddings]",                  "Rebuild search index from files."),
    ("embed-pending", "",                              "Process queued embeddings."),
    ("rebuild-mocs", "",                               "Rebuild all Maps of Content from entry tags."),
    ("stats",       "[--json]",                        "Show knowledge store statistics."),
    ("check",       "[--fix]",                         "Audit structural integrity."),
    ("list",        "[--status] [--tag] [--ids]",      "List entries with optional filters."),
    ("status",      "",                                "Show system capabilities and recommendations."),
    ("config",      "<key> [value]",                   "Get or set configuration (llm, embeddings, etc)."),
]

_LLM_REQUIRED = (
    "k7e {cmd} requires an LLM command. Set llm_command (or a purpose-specific "
    "override) — stdin in, stdout out — then retry (see `k7e status`)."
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="k7e",
        description="Knowledge accumulation engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_format_commands(),
    )
    sub = parser.add_subparsers(dest="command")

    # search
    p = sub.add_parser("search", help="Hybrid search")
    p.add_argument("query", help="Search query")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p.add_argument("--ids", action="store_true", help="Output IDs only, one per line")
    p.add_argument("--rerank", action="store_true", help="Rerank results with the LLM")
    p.add_argument("--include-superseded", action="store_true", help="Include superseded entries")

    # get
    p = sub.add_parser("get", help="Read entry")
    p.add_argument("id", help="Entry ID (e.g., KG-00001)")

    # supersede
    p = sub.add_parser("supersede", help="Mark an entry as superseded by a newer one")
    p.add_argument("old_id", help="Entry being retired")
    p.add_argument("new_id", help="Entry that replaces it")

    # store
    p = sub.add_parser("store", help="Create new entry")
    p.add_argument("title", help="Entry title")
    p.add_argument("--tags", default="", help="Comma-separated tags")
    p.add_argument("--aliases", default="", help="Comma-separated aliases")
    p.add_argument("--content", default=None, help="Content (or pipe via stdin)")

    # append
    p = sub.add_parser("append", help="Append to entry")
    p.add_argument("id", help="Entry ID")
    p.add_argument("--section", default="Edge Cases", help="Section to append to")
    p.add_argument("--content", default=None, help="Content (or pipe via stdin)")

    # asset
    p = sub.add_parser("asset", help="Store binary file")
    p.add_argument("file", help="Path to file")

    # recall
    p = sub.add_parser("recall", help="Recall relevant knowledge (RAG)")
    p.add_argument("text", nargs="?", default=None, help="Topic, question, or conversation (reads stdin if omitted)")
    p.add_argument("--limit", type=int, default=8)

    # distill
    p = sub.add_parser("distill", help="Extract knowledge from files")
    p.add_argument("paths", nargs="+", help="Files or directories")
    p.add_argument("--dry-run", action="store_true")

    # consolidate
    p = sub.add_parser("consolidate", help="Find and merge duplicate nodes")
    p.add_argument("--dry-run", action="store_true")

    # compile
    p = sub.add_parser("compile", help="Synthesize entries for a tag into a reference page")
    p.add_argument("tag", help="Tag to compile")
    p.add_argument("--dry-run", action="store_true")

    # reindex
    p = sub.add_parser("reindex", help="Rebuild index")
    p.add_argument("--embeddings", action="store_true")

    # embed-pending
    sub.add_parser("embed-pending", help="Process queued embeddings")

    # rebuild-mocs
    sub.add_parser("rebuild-mocs", help="Rebuild MOCs from tags")

    # stats
    p = sub.add_parser("stats", help="Statistics")
    p.add_argument("--json", action="store_true")

    # check
    p = sub.add_parser("check", help="Audit integrity")
    p.add_argument("--fix", action="store_true")

    # list
    p = sub.add_parser("list", help="List entries")
    p.add_argument("--status", default=None)
    p.add_argument("--tag", default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--ids", action="store_true", help="Output IDs only, one per line")

    # status
    sub.add_parser("status", help="System capabilities and recommendations")

    # config
    p = sub.add_parser("config", help="Get or set configuration")
    p.add_argument("key", help="Config key (llm_command, summarize_command, embeddings, ...)")
    p.add_argument("value", nargs="?", default=None, help="Value to set (omit to read)")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    init()

    if args.command == "search":
        results = search(
            args.query,
            limit=args.limit,
            include_superseded=args.include_superseded,
            rerank=True if args.rerank else None,
        )
        if args.ids:
            for r in results:
                print(r['id'])
        elif args.json:
            print(json.dumps(results, indent=2))
        elif not results:
            print("No results.")
        else:
            for r in results:
                print(f"  {r['id']}  {r['title']}  (score: {r['score']})")

    elif args.command == "get":
        try:
            print(get(args.id, track_usage=True))
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 1

    elif args.command == "supersede":
        try:
            get(args.new_id)
        except FileNotFoundError:
            print(f"Node {args.new_id} not found", file=sys.stderr)
            return 1
        if supersede(args.old_id, args.new_id):
            print(f"{args.old_id} superseded by {args.new_id}")
        else:
            print(f"Node {args.old_id} not found", file=sys.stderr)
            return 1

    elif args.command == "store":
        content = args.content or sys.stdin.read()
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
        aliases = [a.strip() for a in args.aliases.split(",") if a.strip()] if args.aliases else []
        entry_id = store_entry(args.title, content, tags=tags, aliases=aliases)
        print(f"Stored {entry_id}: {args.title}")

    elif args.command == "append":
        content = args.content or sys.stdin.read()
        append_entry(args.id, args.section, content)
        print(f"Appended to {args.id} [{args.section}]")

    elif args.command == "asset":
        try:
            rel_path = store_asset(args.file)
            print(rel_path)
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 1

    elif args.command == "recall":
        text = args.text
        if text is None:
            if sys.stdin.isatty():
                print("Usage: k7e recall <text>  or  echo '...' | k7e recall", file=sys.stderr)
                return 1
            text = sys.stdin.read()
        if not config.llm_configured("summarize"):
            print(_LLM_REQUIRED.format(cmd="recall"), file=sys.stderr)
            return 1
        answer, sources = recall(text, limit=args.limit)
        if not sources:
            print("No relevant knowledge found.")
        elif answer:
            print(answer)
            ids = ", ".join(e["id"] for e in sources)
            print(f"\n---\nSources: {ids}")
        else:
            print("LLM synthesis failed.", file=sys.stderr)
            return 1

    elif args.command == "distill":
        if not config.llm_configured("distill"):
            print(_LLM_REQUIRED.format(cmd="distill"), file=sys.stderr)
            return 1
        results = distill(args.paths, dry_run=args.dry_run)
        for r in results:
            action = r["action"]
            title = r["title"]
            entry_id = r.get("id", "")
            print(f"  [{action}] {entry_id} {title}")
        if not results:
            print("No new knowledge extracted.")

    elif args.command == "consolidate":
        results = consolidate(dry_run=args.dry_run)
        total_superseded = 0
        for r in results:
            if r["action"] == "would_consolidate":
                print(f"  [keep] {r['keeper']}  {r['title']}  (merge {len(r['duplicates'])} dupes)")
            else:
                print(f"  [done] {r['keeper']}  {r['title']}  (superseded {r['count']})")
                total_superseded += r["count"]
        if not results:
            print("No duplicates found.")
        elif not args.dry_run:
            print(f"\nConsolidated: {total_superseded} nodes superseded across {len(results)} groups.")

    elif args.command == "compile":
        if not config.llm_configured("compile"):
            print(_LLM_REQUIRED.format(cmd="compile"), file=sys.stderr)
            return 1
        node_id = compile_tag(args.tag, dry_run=args.dry_run)
        if node_id:
            print(f"Compiled {node_id}: {args.tag} — Compiled Reference")

    elif args.command == "reindex":
        reindex(embeddings=args.embeddings)
        print("Reindex complete.")

    elif args.command == "embed-pending":
        count = process_pending_embeddings()
        print(f"Processed {count} pending embedding(s).")

    elif args.command == "rebuild-mocs":
        rebuild_mocs()
        print("MOCs rebuilt.")

    elif args.command == "stats":
        s = stats()
        if args.json:
            print(json.dumps(s, indent=2))
        else:
            print(f"Entries: {s['total_nodes']}  MOCs: {s['total_mocs']}  Assets: {s['total_assets']}")
            print(f"Avg confidence: {s['avg_confidence']}")
            if s["top_tags"]:
                print("Top tags: " + ", ".join(f"{t}({c})" for t, c in s["top_tags"]))

    elif args.command == "check":
        issues = run_audit(fix=args.fix)
        if issues:
            for i in issues:
                print(f"  {i}")
            print(f"\n{len(issues)} issue(s).")
        else:
            print("Clean.")

    elif args.command == "list":
        nodes = list_nodes(status=args.status, tag=args.tag)
        if args.ids:
            for n in nodes:
                print(n['id'])
        elif args.json:
            print(json.dumps(nodes, indent=2))
        else:
            for n in nodes:
                print(f"  {n['id']}  {n['title']}  [{n['status']}]  conf:{n['confidence']}")

    elif args.command == "status":
        print(config.status())

    elif args.command == "config":
        if args.value is None:
            val = config.get(args.key)
            if val is not None:
                print(val)
            elif args.key in {v[0] for v in config.LLM_PURPOSES.values()}:
                purpose = next(
                    p for p, (k, _) in config.LLM_PURPOSES.items() if k == args.key
                )
                cmd, source = config.command_source(purpose)
                if cmd and source == config.LLM_FALLBACK_KEY:
                    print(f"{cmd} (via llm_command)")
                else:
                    print(f"{args.key}: not set")
            else:
                print(f"{args.key}: not set")
        else:
            cfg = config.load_config()
            cfg[args.key] = args.value
            config.save_config(cfg)
            print(f"{args.key} = {args.value}")

    return 0


def _format_commands():
    lines = ["\nCommands:"]
    for name, usage, desc in COMMANDS:
        lines.append(f"  {name:14} {usage:30} {desc}")
    return "\n".join(lines)
