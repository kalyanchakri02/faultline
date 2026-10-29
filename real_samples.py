"""Format-robustness check on VERBATIM Xid lines from public sources (Netdata guide, Crusoe support docs),
wrapped in raw `dmesg` framing ([ seconds-since-boot ] timestamps) that the agent has never seen.
Accuracy is NOT scored: public pages give example lines, not labelled incidents. We only check that the agent
learns a validated parser for real-world formatting quirks (pid='<unknown>', 4-digit PCI domains, uptime timestamps)."""
import json, random, shutil
import agent, loggen
from parserlib import Library

REAL = {
  "netdata_xid79": "NVRM: Xid (PCI:0000:41:00): 79, pid=1432, name=python, GPU has fallen off the bus.",          # netdata.cloud xid guide
  "crusoe_xid79_unknown_pid": "NVRM: Xid (PCI:0000:14:00): 79, pid='<unknown>', name=<unknown>, GPU has fallen off the bus.",  # support.crusoecloud.com
  "crusoe_xid95_uncontained": "NVRM: Xid (PCI:0002:00:04): 95, pid='<unknown>', name=<unknown>, Uncontained: FBHUB. RST: Yes, D-RST: No",  # support.crusoecloud.com
}
NOISE = ["nvidia-nvswitch0: SXid warning rate-limited (benign)", "NVRM: GPU at PCI:0000:41:00: GPU-1234 clocks throttled by SW power cap",
         "nvidia-uvm: Loaded the UVM driver, major device number 510.", "pcieport 0000:40:01.1: AER: Corrected error received: 0000:40:01.1"]

def build(line, seed=1):
    r = random.Random(seed); t = 100.0; out = []
    for i in range(40):
        t += r.uniform(5, 200)
        if i == 24: out.append(f"[{t:12.6f}] {line}")
        else: out.append(f"[{t:12.6f}] {r.choice(NOISE)}")
    base = loggen.generate("healthy_node", 1, 0)
    return {"scenario": "real", "files": {"dmesg.log": "\n".join(out) + "\n", "dcgm_telemetry.txt": base["files"]["dcgm_telemetry.txt"]},
            "alert": "Alert: GPU health check failed. Files: dmesg.log (raw dmesg with seconds-since-boot), dcgm_telemetry.txt (5-min DCGM samples). Formats are not guaranteed."}

if __name__ == "__main__":
    shutil.rmtree("library_real", ignore_errors=True); lib = Library("library_real"); res = {}
    for name, line in REAL.items():
        d = agent.run_to_end(build(line), lib=lib)
        rep = d["report"] or {}
        res[name] = dict(parsers=d["parsers"], attempts=d["parser_attempts"], seconds=d["seconds"], tokens=d["tokens"],
                         classification=rep.get("classification"), action=rep.get("action"), root_cause=rep.get("root_cause"))
        print(name, json.dumps(res[name], indent=1))
    json.dump(res, open("real_samples_results.json", "w"), indent=1)
