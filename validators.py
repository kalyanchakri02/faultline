"""Deterministic oracles that check a generated parser against the RAW file. No LLM involved.
The agent can't talk its way past these: coverage and fidelity are computed from the raw text."""
import datetime as dt, re
from collections import Counter

XID_KW = re.compile(r"(?<![A-Za-z])Xid(?![A-Za-z])")

def _ints(line):
    return {int(t) for t in re.findall(r"(?<![\w.])\d+(?![\w.])", line)}
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
    # Format-tolerant Xid check. The only anchor is the keyword "Xid" (not "SXid" from nvswitch). We do NOT assume where the code
    # sits in the line: each parsed Xid event must match a distinct raw "Xid" line that contains its code (and pid) as a standalone number.
    raw_lines = [l for l in raw.splitlines() if XID_KW.search(l)]
    parsed = [e for e in events if e.get("kind") == "xid"]
    if len(parsed) != len(raw_lines):
        problems.append(f"Xid coverage mismatch: the raw file has {len(raw_lines)} lines containing the word 'Xid' (nvswitch 'SXid' lines excluded) "
                        f"but the parser produced {len(parsed)} events with kind='xid'. Raw examples: {[l.strip()[:200] for l in raw_lines[:2]]}")
    else:
        free = [_ints(l) for l in raw_lines]
        for e in parsed:
            code, pid = e.get("xid"), e.get("pid")
            hit = next((i for i, toks in enumerate(free) if toks is not None and isinstance(code, int) and code in toks and (pid is None or pid in toks)), None)
            if hit is None:
                problems.append(f"Xid event (xid={code}, pid={pid}) does not match any unclaimed raw Xid line: the code or pid is not a standalone number in the line. "
                                f"Raw examples: {[l.strip()[:200] for l in raw_lines[:2]]}")
                break
            free[hit] = None
    got_xids = Counter(e.get("xid") for e in parsed if e.get("xid") is not None)
    n_aer, n_thr = len(AER_RE.findall(raw)), len(THROTTLE_RE.findall(raw))
    g_aer, g_thr = sum(e.get("kind") == "aer" for e in events), sum(e.get("kind") == "throttle" for e in events)
    if n_aer != g_aer: problems.append(f"AER lines: raw has {n_aer}, parser emitted {g_aer} events with kind='aer'")
    if n_thr != g_thr: problems.append(f"throttle lines: raw has {n_thr}, parser emitted {g_thr} events with kind='throttle'")
    return {"ok": not problems, "problems": problems[:6], "summary": {"events": len(events), "xids": dict(got_xids), "aer": g_aer, "throttle": g_thr}}


def _raw_columns(raw: str):
    """Format-agnostic view of the raw telemetry: {column label: [floats per data row]}. JSON lines -> by key; delimited text -> by position."""
    import json as _json
    cols, header = {}, None
    rows = [l for l in raw.splitlines() if l.strip() and not l.lstrip().startswith("#") and not l.lower().startswith("timestamp")]
    heads = [l for l in raw.splitlines() if l.lstrip().startswith("#") or l.lower().startswith("timestamp")]
    if heads: header = re.split(r"[,\s]+", heads[0].lstrip("# ").strip())
    for i, l in enumerate(rows):
        s = l.strip()
        if s.startswith("{"):
            try: d = _json.loads(s)
            except Exception: continue
            items = [(k, v) for k, v in d.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        else:
            toks = re.split(r"[,\s]+", s)
            items = []
            for j, t in enumerate(toks):
                try: items.append((header[j] if header and len(header) == len(toks) else f"column {j}", float(t)))
                except ValueError: pass
        for k, v in items: cols.setdefault(k, {})[i] = v
    return {k: [c.get(i) for i in range(len(rows))] for k, c in cols.items()}

def _uncaptured_columns(rows, raw):
    """Raw columns that VARY over time but are not carried by any parsed field: the signature of a mis-named or dropped field."""
    out, parsed = [], {k: [float(r[k]) for r in rows] for k in TELEM_KEYS[1:] if rows and all(isinstance(r.get(k), (int, float)) for r in rows)}
    for label, s in _raw_columns(raw).items():
        if len(s) != len(rows) or any(x is None for x in s): continue
        if len(set(s)) < 2 or min(s) > 1e8: continue            # constant columns and epoch-like time columns carry no signal
        if not any(p == s for p in parsed.values()): out.append((label, s))
    return out

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
        for label, s in _uncaptured_columns(rows, raw)[:3]:
            problems.append(f"raw column {label!r} changes over time (values {sorted(set(s))[:5]}) but no parsed field carries it. A field is probably mis-named, mis-mapped or silently defaulted to 0.")
    if not problems:
        for k in CUMULATIVE:
            seq = [r[k] for r in rows]
            if any(b < a for a, b in zip(seq, seq[1:])): problems.append(f"counter '{k}' decreases over time; columns are probably mapped wrongly"); break
    return {"ok": not problems, "problems": problems[:6], "summary": {"rows": len(rows)}}
