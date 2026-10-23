import json, sys, threading
from concurrent.futures import ThreadPoolExecutor
from decompnet.app.engine import RomNetwork
from decompnet.app.cli import home_dir
from decompnet import scoring, toolchains

net = RomNetwork(home_dir() / ".decomp" / "decomp.db", home_dir())
proj = net.project_row("slus-01252")
main = json.loads(proj["config"])["compiler"]["toolchain"]
ids = net._survey_compilers()
installed = [t for t in ids if toolchains.installed(t) is not None]
variants = [v for t in installed for v in (t, f"{t}@2.21", f"{t}@2.34", f"{t}/O1")]
rows = net.store.all("SELECT * FROM functions WHERE project_id=? AND status='open' AND best_source IS NOT NULL "
                     "AND vram BETWEEN ? AND ? ORDER BY vram", proj["id"], 0x8016A000, 0x80172000)
units = net._unit_contexts(proj["id"])
print("variants:", variants, file=sys.stderr)

def one(fn):
    header = net._context(proj["id"], net._effective_unit(fn, units))["header"]
    target = net._target_obj(fn, proj)
    out = {}
    for v in variants:
        try:
            res = net._compile(proj, header, fn["best_source"], toolchain=v)
            if not res.ok:
                out[v] = None; continue
            cand, verdict, _ = net._evaluate(fn, proj, res.obj)
            out[v] = None if cand is None else (0 if verdict else scoring.diff(target, cand).score)
        except Exception as e:
            out[v] = f"err:{type(e).__name__}"
    return fn["name"], fn["n_insns"], json.loads(fn["meta"] or "{}").get("toolchain") or main, out

with ThreadPoolExecutor(max_workers=6) as pool:
    results = list(pool.map(one, rows))
json.dump({"variants": variants, "results": results}, open(sys.argv[1], "w"), indent=1)
print("done", len(results), file=sys.stderr)
