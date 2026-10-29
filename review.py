"""Model-based review and reflection. These calls use the LLM's reasoning where fixed rules can't generalise:
- review(): a fresh, independent model call inspects a parser's output against sample raw lines (wrong code? mixed-up columns? bad timestamp?)
- reflect(): after the agent recovers from failed attempts, it writes a short general lesson that future runs receive."""
import json, re
from validators import XID_KW

def _ask(client, model, system, user, max_tokens=1200):
    r = client.chat.completions.create(model=model, temperature=0, max_tokens=max_tokens,
                                       messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return (r.choices[0].message.content or ""), (getattr(r.usage, "total_tokens", 0) or 0)

def _json(text):
    m = re.search(r"\{.*\}", text, re.S)
    try: return json.loads(m.group(0)) if m else None
    except Exception: return None

def _sample(kind, raw, data):
    lines = [l for l in raw.splitlines() if l.strip()]
    if kind == "log":
        pick = [l for l in lines if XID_KW.search(l)][:6] + [l for l in lines if "AER:" in l][:2] + [l for l in lines if "clocks throttled" in l][:2]
        rest = [l for l in lines if l not in pick]; pick += rest[:: max(1, len(rest) // 3)][:3]
        evs = [e for e in data if e.get("kind") == "xid"][:6] + [e for e in data if e.get("kind") == "aer"][:2] + [e for e in data if e.get("kind") == "throttle"][:2]
        return pick, evs
    head = [l for l in lines if l.lstrip().startswith("#") or l.lower().startswith("timestamp")][:3]
    body = [l for l in lines if l not in head]
    return head + body[:3] + body[-1:], data[:3] + data[-1:]

REVIEW_SYS = "You are a strict reviewer of log parsers. You only flag concrete errors you can point to in the lines shown. Reply with JSON only."

def review(client, model, kind, raw, data):
    raw_s, parsed_s = _sample(kind, raw, data)
    if kind == "log":
        rules = ("Each event must have ts (ISO 8601; conversions such as seconds-since-boot to a date are fine), kind (xid|aer|throttle), xid (the NVIDIA Xid error code, int, only for kind=xid else null), "
                 "pid (int or null when absent/'<unknown>'), pci, msg. Check: the Xid code is the real code and not part of a PCI address/pid/timestamp; pid correct; kind correct; ts consistent with the raw line; non-Xid events have xid null.")
    else:
        rules = ("Each row must have ts (ISO 8601), gpu, temp_c, power_w, sm_clock_mhz, ecc_sbe, ecc_dbe, remapped_rows, remap_failures, nvlink_crc, pcie_replay. Check the column mapping against the raw "
                 "header/field names (temperature vs power vs clock; each counter in the right key), units, and ts conversion.")
    user = (f"{rules}\n\nRAW LINES (sample):\n" + "\n".join(l[:300] for l in raw_s) + f"\n\nPARSED OUTPUT for the whole file (sample):\n{json.dumps(parsed_s, default=str)[:5000]}"
            '\n\nReply: {"verdict": "pass" or "fail", "issues": ["short concrete issue", ...]}')
    try:
        txt, tok = _ask(client, model, REVIEW_SYS, user)
    except Exception as e:
        return {"verdict": "pass", "issues": [], "tokens": 0, "note": f"reviewer unavailable: {str(e)[:80]}"}
    j = _json(txt) or {}
    verdict = "fail" if str(j.get("verdict", "pass")).lower() == "fail" and j.get("issues") else "pass"
    return {"verdict": verdict, "issues": [str(i)[:300] for i in (j.get("issues") or [])][:4], "tokens": tok}

def reflect(client, model, kind, fails, final_code, repaired):
    user = (f"An agent was writing a parser for a {kind} file ({'repairing an older parser whose format had drifted' if repaired else 'from scratch'}). "
            "Its failed attempts were rejected for these reasons:\n" + "\n".join(f"- {f[:300]}" for f in fails[-4:]) +
            f"\n\nThe attempt that finally passed began:\n{final_code[:700]}\n\n"
            "Write ONE or TWO sentences: a general lesson that would help future parser writers avoid these mistakes. "
            "Do not mention specific hostnames, numbers or exact log text. Reply with the lesson only.")
    try:
        txt, tok = _ask(client, model, "You write concise, general engineering lessons.", user, max_tokens=800)
    except Exception:
        return None, 0
    txt = txt.strip().strip('"')
    if not txt or txt[-1] not in '.!)': return None, tok     # discard truncated lessons
    return txt[:400], tok
