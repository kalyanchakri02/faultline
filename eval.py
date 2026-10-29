"""Cold-start evaluation. Fresh parser library -> the agent must LEARN each unseen log/telemetry format, then REUSE it.
usage: python3 eval.py [seeds=2] [model]
Reports: triage accuracy, learn-vs-reuse cost, parser reuse rate, and determinism of parsed evidence."""
import json, shutil, statistics as st, sys
from concurrent.futures import ThreadPoolExecutor
import loggen, agent
from parserlib import Library

SEEDS = int(sys.argv[1]) if len(sys.argv) > 1 else 2
MODEL = sys.argv[2] if len(sys.argv) > 2 else agent.DEFAULT_MODEL
LIBDIR = "library_eval"
shutil.rmtree(LIBDIR, ignore_errors=True)
lib = Library(LIBDIR)

def one(job):
    name, seed, fmt = job
    inc = loggen.generate(name, seed, fmt)
    d = agent.run_to_end(inc, MODEL, lib)
    rep, truth = d.get("report"), inc["truth"]
    learned = [k for k, v in d["parsers"].items() if v.startswith("learned")]
    return dict(scenario=name, seed=seed, fmt=fmt, ok_class=bool(rep) and rep["classification"] == truth["classification"],
                ok_action=bool(rep) and rep["action"] == truth["action"], seconds=d["seconds"], tool_calls=d["tool_calls"], tokens=d["tokens"],
                learned=learned, parser_attempts=d["parser_attempts"], parsed_hash=d["parsed_hash"],
                errors=d.get("errors"), steps_used=d.get("steps_used"), got=(rep or {}).get("classification"), got_action=(rep or {}).get("action"), want=truth["classification"], want_action=truth["action"])

names = list(loggen.SCENARIOS)
# 1) learning pass: one incident per format (3 in parallel) on an empty library
learn_jobs = [(names[i], 1, i) for i in range(3)]
with ThreadPoolExecutor(3) as ex: learn = list(ex.map(one, learn_jobs))
# 2) reuse pass: every scenario x every format x seeds, library now populated
jobs = [(n, s, f) for s in range(1, SEEDS + 1) for n in names for f in range(3) if (n, s, f) not in learn_jobs]
with ThreadPoolExecutor(4) as ex: reuse = list(ex.map(one, jobs))
# 3) determinism: re-run a few identical incidents, compare parsed evidence + decisions
det_jobs = [(n, 1, f) for n, f in [("ecc_remap_exhausted", 0), ("nvlink_flaky", 1), ("thermal_throttle", 2), ("healthy_node", 1)]]
with ThreadPoolExecutor(4) as ex: det_a, det_b = list(ex.map(one, det_jobs)), list(ex.map(one, det_jobs))
same_ev = sum(a["parsed_hash"] == b["parsed_hash"] for a, b in zip(det_a, det_b))
same_dec = sum((a["got"], a["got_action"]) == (b["got"], b["got_action"]) for a, b in zip(det_a, det_b))

allr = learn + reuse
m = lambda xs, k: st.mean(x[k] for x in xs) if xs else float("nan")
print(f"\nmodel={MODEL}  total triages={len(allr)}  library: {len(lib.entries())} parsers learned")
print(f"\n{'scenario':22} {'class':>7} {'action':>7}")
for n in names:
    r = [x for x in allr if x["scenario"] == n]
    print(f"{n:22} {sum(x['ok_class'] for x in r)}/{len(r):<5} {sum(x['ok_action'] for x in r)}/{len(r):<5}")
print(f"OVERALL  class {sum(x['ok_class'] for x in allr)}/{len(allr)}   action {sum(x['ok_action'] for x in allr)}/{len(allr)}")
print(f"\nLEARNING triages (unseen format, parsers written + validated): n={len(learn)}  avg {m(learn,'seconds'):.1f}s  {m(learn,'tool_calls'):.1f} calls  {m(learn,'tokens'):.0f} tokens")
print(f"REUSE triages    (parsers from library):                n={len(reuse)}  avg {m(reuse,'seconds'):.1f}s  {m(reuse,'tool_calls'):.1f} calls  {m(reuse,'tokens'):.0f} tokens")
re_hits = sum(1 for x in reuse if not x["learned"])
print(f"Parser reuse rate in reuse pass: {re_hits}/{len(reuse)}  (others had to re-learn: {[(x['scenario'], x['fmt'], x['learned']) for x in reuse if x['learned']]})")
print(f"Determinism (same incident run twice): identical parsed evidence {same_ev}/{len(det_jobs)}, identical decision {same_dec}/{len(det_jobs)}")
miss = [(x['scenario'], x['fmt'], f"{x['got']}/{x['got_action']}", f"want {x['want']}/{x['want_action']}", x['errors'] or f"steps={x['steps_used']}") for x in allr if not (x['ok_class'] and x['ok_action'])]
print("Misses:", miss or "none")
json.dump(dict(learn=learn, reuse=reuse, det_a=det_a, det_b=det_b), open("eval_results.json", "w"), indent=1)
