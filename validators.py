"""Deterministic oracles that check a generated parser against the RAW file. No LLM involved.
The agent can't talk its way past these: coverage and fidelity are computed from the raw text."""
import datetime as dt, re
from collections import Counter

XID_RE = re.compile(r"NVRM: Xid \(PCI:[^)]*\):\s*(\d+)(?:,\s*pid=(\d+))?")
AER_RE = re.compile(r"AER:")
THROTTLE_RE = re.compile(r"clocks throttled")

LOG_KEYS = {"ts": str, "kind": str, "msg": str}
TELEM_KEYS = ["ts", "gpu", "temp_c", "power_w", "sm_clock_mhz", "ecc_sbe", "ecc_dbe", "remapped_rows", "remap_failures", "nvlink_crc", "pcie_replay"]
CUMULATIVE = ["ecc_sbe", "ecc_dbe", "remapped_rows", "remap_failures", "nvlink_crc", "pcie_replay"]

def _iso_ok(v):
    try: dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")); return True
    except Exception: return False

def validate_log(events, raw: str) -> dict:
    problems = []
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        return {"ok": False, "problems": ["parse() must return a list of dicts"]}
    for i, e in enumerate(events):
        for k, t in LOG_KEYS.items():
            if not isinstance(e.get(k), t): problems.append(f"event {i}: key '{k}' missing or not {t.__name__}"); break
        else:
            if not _iso_ok(e["ts"]): problems.append(f"event {i}: ts '{e['ts']}' is not ISO 8601 (YYYY-MM-DDTHH:MM:SS)")
        if e.get("kind") in ("aer", "throttle") and e.get("xid") is not None:
            problems.append(f"event {i}: kind='{e['kind']}' must have xid=None (got {e['xid']}); don't mistake ids like 'GPU-1234' for Xid codes")
        if e.get("kind") not in ("xid", "aer", "throttle"): problems.append(f"event {i}: kind must be one of xid/aer/throttle (got {e.get('kind')!r}); skip other lines")
        if len(problems) >= 5: break
    raw_xids = Counter(int(m.group(1)) for m in XID_RE.finditer(raw))
    got_xids = Counter(e.get("xid") for e in events if e.get("kind") == "xid" and e.get("xid") is not None)
    if raw_xids != got_xids:
        problems.append(f"Xid coverage mismatch: raw file has {dict(raw_xids)} but parser produced {dict(got_xids)}. "
                        f"Hint: the Xid code is the integer right after 'Xid (PCI:<addr>):'. 'SXid' (nvswitch) lines are NOT GPU Xids. Exact raw lines you must capture: {[l.strip()[:200] for l in raw.splitlines() if XID_RE.search(l)][:2]}")
    raw_pids = Counter(int(m.group(2)) for m in XID_RE.finditer(raw) if m.group(2))
    got_pids = Counter(e.get("pid") for e in events if e.get("kind") == "xid" and e.get("pid") is not None)
    if raw_pids != got_pids: problems.append(f"pid mismatch: raw {dict(raw_pids)} vs parsed {dict(got_pids)}")
    n_aer, n_thr = len(AER_RE.findall(raw)), len(THROTTLE_RE.findall(raw))
    g_aer, g_thr = sum(e.get("kind") == "aer" for e in events), sum(e.get("kind") == "throttle" for e in events)
    if n_aer != g_aer: problems.append(f"AER lines: raw has {n_aer}, parser emitted {g_aer} events with kind='aer'")
    if n_thr != g_thr: problems.append(f"throttle lines: raw has {n_thr}, parser emitted {g_thr} events with kind='throttle'")
    return {"ok": not problems, "problems": problems[:6], "summary": {"events": len(events), "xids": dict(got_xids), "aer": g_aer, "throttle": g_thr}}

def validate_telemetry(rows, raw: str) -> dict:
    problems = []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return {"ok": False, "problems": ["parse() must return a list of dicts"]}
    data_lines = [l for l in raw.splitlines() if l.strip() and not l.lstrip().startswith("#") and not l.lower().startswith("timestamp")]
    if len(rows) != len(data_lines):
        problems.append(f"row count mismatch: raw has {len(data_lines)} data lines (excluding header/# lines), parser returned {len(rows)}")
    tokens = {float(t) for t in re.findall(r"-?\d+(?:\.\d+)?", raw)}
    for i, r in enumerate(rows):
        miss = [k for k in TELEM_KEYS if k not in r]
        if miss: problems.append(f"row {i}: missing keys {miss}"); break
        bad = [k for k in TELEM_KEYS[1:] if not isinstance(r[k], (int, float)) or isinstance(r[k], bool)]
        if bad: problems.append(f"row {i}: non-numeric values for {bad}"); break
        if not _iso_ok(r["ts"]): problems.append(f"row {i}: ts '{r['ts']}' is not ISO 8601"); break
        invented = [k for k in TELEM_KEYS[1:] if float(r[k]) not in tokens]
        if invented: problems.append(f"row {i}: values for {invented} do not appear anywhere in the raw file (invented or mis-scaled)"); break
        if not (0 <= r["temp_c"] <= 130 and 0 <= r["sm_clock_mhz"] <= 3000): problems.append(f"row {i}: temp/clock out of plausible range"); break
    if not problems:
        for k in CUMULATIVE:
            seq = [r[k] for r in rows]
            if any(b < a for a, b in zip(seq, seq[1:])): problems.append(f"counter '{k}' decreases over time; columns are probably mapped wrongly"); break
    return {"ok": not problems, "problems": problems[:6], "summary": {"rows": len(rows)}}
