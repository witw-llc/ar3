import json, os, sys, tempfile
SPIKE = os.environ.get("K7E_CHASSIS_RUNS") or os.path.join(tempfile.gettempdir(), "k7e-chassis-runs")
for n in sys.argv[1:]:
    r = json.load(open(f"{SPIKE}/runs/{n}/result.json"))
    m = r['metrics']
    print("==", n)
    for d in ("7", "14", "30"):
        if d in m:
            print(d, {k: v for k, v in m[d].items() if k != "durable_n"})
    print("tel", m['telemetry'], "poison", m['poison_imperative_entries'], m['poison_entries_total'])
    dl = r['daily']
    print("visible/day", [d['visible'] for d in dl])
    print("files/day(every5)", [d['total_files'] for d in dl][::5], "pages", dl[-1]['pages'], dl[-1]['active_pages'],
          "folded", dl[-1]['folded'], "active_status", dl[-1]['active_status'])
    print("calls", r['calls'], "graph", r['graph'], "compiles", [c for c in r['compile_log'] if 'Compiled' in c[1]][:6])
    if r["options"].get("narration"):
        # The arm's compile model only ever narrates, so no page may fold a source.
        print("narration arm: sources folded by a page", dl[-1]["folded"], "(must be 0), pages", dl[-1]["pages"],
              "compile attempts", r["calls"]["compile"])
    inv = r.get("invariance")
    if inv:
        by_key = [c for c in inv["changed"] if c["before_has_key"] != c["after_has_key"]]
        print("invariance pairs", inv["pairs"], "top id changed", len(inv["changed"]), "key lost or gained", len(by_key))
        for c in by_key:
            print("  ", c)
