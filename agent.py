"""FaultLine agent v2: Nemotron on Nebius Token Factory. It *learns* parsers for unfamiliar log formats,
proves them against deterministic oracles, saves them to a library, then triages from the parsed evidence."""
from __future__ import annotations
import hashlib, json, os, re, time
from openai import OpenAI
from pydantic import ValidationError
import sandbox
from parserlib import Library, FILES, fingerprint, check
import review as rv
from schemas import Report, XIDS

BASE_URL = os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")
DEFAULT_MODEL = os.environ.get("FAULTLINE_MODEL", "nvidia/nemotron-3-super-120b-a12b")
MAX_STEPS = 22
MAX_PARSER_ATTEMPTS = 5
MAX_CANDIDATES = 6   # saved parsers tried per file before the agent writes a new one

SYSTEM = """You are FaultLine, an SRE agent that triages unhealthy NVIDIA GPU nodes. Input formats vary per node
(syslog, journald ISO, journald JSON, CSV / JSONL / dmon-table telemetry), so you first make sure each raw file has a
VALIDATED PARSER, then you triage from the parsed evidence.

PHASE 1 - parsers. For every file marked NEEDS PARSER, study the sample lines and call write_parser(kind, code, description).
Your code must define parse(text: str) -> list[dict] (stdlib only). A deterministic validator checks it against the raw file and
tells you exactly what is wrong; fix and resubmit. A passing parser is saved to a library and reused for future nodes, so make it
robust to variations (extra spaces, missing optional fields, different hosts/identifiers), not just this one file.
 kind='log': return one dict per line containing 'NVRM: Xid' (kind='xid'), 'AER:' (kind='aer') or 'clocks throttled' (kind='throttle'); skip all other lines.
   Keys: ts (ISO 'YYYY-MM-DDTHH:MM:SS'; if the year is missing use 2026; if the log only has seconds-since-boot like '[ 1234.567890]', use 2026-01-01T00:00:00 plus that many seconds), kind, xid (int or None), pid (int or None), pci (str or None), msg (the message text, without timestamp/host prefix).
   'SXid' lines are nvswitch events, NOT GPU Xids. For JSON logs the text lives in the MESSAGE field.
   Example of the classic NVIDIA Xid line: 'NVRM: Xid (PCI:0000:41:00): 79, pid=1432, name=python, GPU has fallen off the bus.' (code 79; pid may be '<unknown>' -> None). Formats change between driver versions and log collectors: rely on the sample lines you are shown, not on this example. The Xid code is an integer that is not part of the PCI address.
 kind='telemetry': return one dict per sample row (skip headers / '#' lines). Keys: ts (ISO), gpu (int), temp_c, power_w, sm_clock_mhz,
   ecc_sbe, ecc_dbe, remapped_rows, remap_failures, nvlink_crc, pcie_replay (all numbers, cumulative counters). Map vendor names (e.g. DCGM_FI_DEV_*) to these keys.

PHASE 2 - triage. When both parsers are ready, events.json and telemetry.json (canonical, parsed) appear in your run_python working directory,
next to the raw files. Use run_python to COMPUTE facts (counts per Xid, timelines, counter deltas, max temperature, clock drops). Do not eyeball.
Use lookup_xid for unfamiliar codes. Then call submit_report once, with evidence quoting numbers from your own run_python output.

MANDATORY telemetry review before you submit (even when the log has no Xids, because many faults leave no Xid): with run_python compute, from telemetry.json, the first->last delta of EVERY counter
(ecc_sbe, ecc_dbe, remapped_rows, remap_failures, nvlink_crc, pcie_replay), plus max temp_c, and min/max sm_clock_mhz. From events.json compute counts of each Xid code and of throttle events by reason (HW slowdown vs SW power cap).

Judgement rules: remap_failures > 0 or Xid 64 -> memory, rma, no matter what else you see. Xid 13/31 are usually application bugs; don't escalate hardware without supporting counters. Exhausted row remap / repeated DBE -> memory, rma.
Xid 79 -> pcie, drain_reboot. NVLink CRC growth / Xid 74 on its own -> interconnect, gpu_reset (RMA only if it persists after a reset, which you cannot know from one incident). Application-caused errors with flat hardware counters -> application, investigate_workload.
No Xids is NOT the same as healthy: check telemetry. Correctable ECC (SBE) rising with no DBE/Xid -> memory, monitor (watch the trend). Sustained high temperature with
'throttled by HW slowdown (thermal)' and falling SM clock -> thermal, inspect_cooling. 'throttled by SW power cap' lines are benign noise. Nothing abnormal anywhere -> classification none, action monitor, low severity.
Classification: application | memory | pcie | interconnect | thermal | none. Action: monitor | gpu_reset | drain_reboot | rma | investigate_workload | inspect_cooling."""

_ENUM_C = ["application", "memory", "pcie", "interconnect", "thermal", "none"]
_ENUM_A = ["monitor", "gpu_reset", "drain_reboot", "rma", "investigate_workload", "inspect_cooling"]
TOOLS = [
    {"type": "function", "function": {"name": "write_parser", "description": "Submit parser code for a raw file kind. It is executed in the sandbox and validated against the raw file; you get a pass/fail report. Passing parsers are saved to the library.",
        "parameters": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["log", "telemetry"]}, "code": {"type": "string", "description": "Python defining parse(text) -> list[dict]"},
                                                        "description": {"type": "string", "description": "One line: which format this parser handles"}}, "required": ["kind", "code", "description"]}}},
    {"type": "function", "function": {"name": "run_python", "description": "Run Python 3 (stdlib only) in a sandbox whose cwd has the raw files plus events.json / telemetry.json once parsed. Returns stdout/stderr/exit_code.",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "lookup_xid", "description": "Look up the meaning and typical remediation for an NVIDIA Xid code.",
        "parameters": {"type": "object", "properties": {"code": {"type": "integer"}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "submit_report", "description": "Submit the final triage report. Ends the investigation.",
        "parameters": {"type": "object", "properties": {
            "classification": {"type": "string", "enum": _ENUM_C}, "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "action": {"type": "string", "enum": _ENUM_A}, "root_cause": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"}}, "required": ["classification", "severity", "action", "root_cause", "evidence", "confidence"]}}},
]

def grounding_error(report, seen: str):
    """Evidence must cite numbers that actually appear in the agent's own sandbox output."""
    nums = [n for ev in report.evidence for n in re.findall(r"\d+(?:\.\d+)?", ev)]
    if not nums: return "Evidence contains no numbers. Cite concrete counts/values printed by your run_python output."
    missing = [n for n in nums if n not in seen]
    if len(missing) > len(nums) / 2: return f"Evidence numbers {missing[:5]} do not appear in any run_python output. Cite only what your code printed."
    return None

def _client():
    return OpenAI(base_url=BASE_URL, api_key=os.environ["NEBIUS_API_KEY"], timeout=120)

def _head(text, n=7):
    return "\n".join(l[:260] for l in text.splitlines()[:n])

def triage(incident: dict, model: str = DEFAULT_MODEL, lib: Library | None = None, use_library: bool = True):
    """Generator of event dicts: library_hit / library_stale / thought / parser_attempt / code / result / lookup / report / rejected / error / done."""
    lib = lib or Library()
    client, files = _client(), dict(incident["files"])
    parsed, source, fps, attempts = {}, {}, {}, {"log": 0, "telemetry": 0}
    seen, calls, report, tokens, t0, errors, analysed = "", 0, None, 0, time.time(), [], False
    stale, fails, lessons_added, reviews = {}, {"log": [], "telemetry": []}, 0, 0

    # ---- Phase 0: try the library before spending a single LLM token on parsing
    for kind, fname in FILES.items():
        fps[kind] = fingerprint(files[fname])
        for e in (lib.candidates(kind, fps[kind])[:MAX_CANDIDATES] if use_library else []):
            res = check(kind, e["code"], files[fname])          # the validator, not the fingerprint, decides
            if res["ok"]:
                parsed[kind], source[kind] = res["data"], f"library:{e['name']}"
                files["events.json" if kind == "log" else "telemetry.json"] = json.dumps(res["data"])
                lib.record_use(e["name"]); seen += json.dumps(res["summary"])
                yield {"type": "library_hit", "kind": kind, "name": e["name"], "fingerprint": fps[kind],
                       "exact_fingerprint": e["fingerprint"] == fps[kind], "summary": res["summary"]}
                break
            stale.setdefault(kind, {"name": e["name"], "code": e["code"], "problems": res["problems"]})   # best-ranked failing candidate = repair starting point
            yield {"type": "library_stale", "kind": kind, "name": e["name"], "problems": res["problems"]}

    need = [k for k in FILES if k not in parsed]
    def _need(k):
        s = f"- {k}: NEEDS PARSER. fingerprint={fps[k]}. First lines of {FILES[k]}:\n{_head(files[FILES[k]])}"
        ls = lib.lessons(k) if use_library else []
        if ls: s += "\n  Lessons from past parser-writing mistakes (apply them):\n" + "\n".join(f"  * {l['text']}" for l in ls)
        if k in stale:
            s += (f"\n  REPAIR HINT: the saved parser '{stale[k]['name']}' no longer validates on this file ({'; '.join(stale[k]['problems'])[:500]}). "
                  f"The format has probably drifted. Adapt it rather than starting over, and keep it working for the old format too:\n{stale[k]['code'][:2500]}")
        return s
    brief = incident["alert"] + "\n\nParser status:\n" + "\n".join(
        (f"- {k}: READY (reused from library, validated)" if k in parsed else _need(k)) for k in FILES)
    for k in stale:
        if k not in parsed: yield {"type": "repair_context", "kind": k, "name": stale[k]["name"], "problems": stale[k]["problems"]}
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": brief}]

    for step in range(1, MAX_STEPS + 1):
        try:
            resp = client.chat.completions.create(model=model, messages=msgs, tools=TOOLS, temperature=0, max_tokens=3500)
        except Exception as e:
            errors.append(str(e)[:200]); yield {"type": "error", "message": f"API error: {str(e)[:300]}"}; break
        tokens += getattr(resp.usage, "total_tokens", 0) or 0
        m = resp.choices[0].message
        if m.content: yield {"type": "thought", "step": step, "text": m.content.strip()}
        if not m.tool_calls:
            msgs.append({"role": "assistant", "content": m.content or ""})
            msgs.append({"role": "user", "content": "Use your tools. Finish by calling submit_report."}); continue
        msgs.append({"role": "assistant", "content": m.content or "", "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in m.tool_calls]})
        done = False
        for c in m.tool_calls:
            calls += 1
            try: args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError: args = None
            name = c.function.name
            if args is None:
                out = {"error": "Tool arguments were not valid JSON. Retry."}
            elif name == "write_parser":
                kind = args.get("kind")
                if kind not in FILES: out = {"error": "kind must be 'log' or 'telemetry'"}
                elif kind in parsed: out = {"status": "already ready", "note": "A validated parser already exists for this kind."}
                elif attempts[kind] >= MAX_PARSER_ATTEMPTS: out = {"status": "attempt limit reached", "note": "Analyse the raw file directly with run_python."}
                else:
                    attempts[kind] += 1
                    res = check(kind, args.get("code", ""), files[FILES[kind]])
                    yield {"type": "parser_attempt", "step": step, "kind": kind, "attempt": attempts[kind], "ok": res["ok"], "code": args.get("code", ""),
                           "problems": res["problems"], "summary": res.get("summary")}
                    rev = None
                    if res["ok"]:
                        rev = rv.review(client, model, kind, files[FILES[kind]], res["data"]); tokens += rev.get("tokens", 0); reviews += 1
                        yield {"type": "parser_review", "kind": kind, "verdict": rev["verdict"], "issues": rev["issues"]}
                        if rev["verdict"] == "fail":
                            res = {**res, "ok": False, "problems": ["Reviewer (independent model check of the output against the raw lines) found: " + " | ".join(rev["issues"])]}
                    if res["ok"]:
                        repaired = kind in stale
                        name_saved = lib.save(kind, fps[kind], args["code"], args.get("description", ""), "repaired" if repaired else "learned", stale[kind]["name"] if repaired else None)
                        parsed[kind], source[kind] = res["data"], f"{'repaired' if repaired else 'learned'}:{name_saved}"
                        files["events.json" if kind == "log" else "telemetry.json"] = json.dumps(res["data"])
                        seen += json.dumps(res["summary"])
                        yield {"type": "parser_saved", "kind": kind, "name": name_saved, "fingerprint": fps[kind], "repaired": repaired}
                        if fails[kind] and use_library:
                            text, tok = rv.reflect(client, model, kind, fails[kind], args["code"], repaired); tokens += tok
                            if text and lib.add_lesson(kind, fps[kind], text):
                                lessons_added += 1; yield {"type": "lesson_saved", "kind": kind, "text": text}
                        out = {"status": "VALIDATED and saved to library", "summary": res["summary"], "file_written": "events.json" if kind == "log" else "telemetry.json"}
                    else:
                        fails[kind].append("; ".join(res["problems"])[:400])
                        out = {"status": "REJECTED", "problems": res["problems"], "attempt": attempts[kind], "of": MAX_PARSER_ATTEMPTS}
            elif name == "run_python":
                yield {"type": "code", "step": step, "code": args.get("code", "")}
                out = sandbox.run_python(args.get("code", ""), files)
                seen += out["stdout"] + out["stderr"]
                still_pending = [k for k in FILES if k not in parsed and attempts[k] < MAX_PARSER_ATTEMPTS]
                if out["exit_code"] == 0 and not still_pending: analysed = True  # graceful degradation: if a parser could not be learned, raw-file analysis still counts
                yield {"type": "result", "step": step, **out}
            elif name == "lookup_xid":
                code = args.get("code"); e = XIDS.get(code)
                out = {"xid": code, "name": e[0], "guidance": e[1]} if e else {"xid": code, "error": "unknown Xid"}
                yield {"type": "lookup", "step": step, **out}
            elif name == "submit_report":
                pending = [k for k in FILES if k not in parsed and attempts[k] < MAX_PARSER_ATTEMPTS]
                try:
                    if pending: raise ValueError(f"Parsers not ready for: {pending}. Call write_parser first.")
                    if not analysed: raise ValueError("You have not run any analysis yet. Use run_python on telemetry.json / events.json to compute the counter deltas, max temp and Xid counts first.")
                    rep = Report(**args); g = grounding_error(rep, seen)
                    if g: raise ValueError(g)
                    report = rep; out = {"status": "accepted"}; done = True
                    yield {"type": "report", "step": step, "report": report.model_dump()}
                except (ValidationError, ValueError) as e:
                    out = {"status": "rejected", "errors": str(e)[:600]}
                    yield {"type": "rejected", "step": step, "errors": str(e)[:300]}
            else:
                out = {"error": f"unknown tool {name}"}
            msgs.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(out)})
        if done: break
    phash = hashlib.sha1(json.dumps(parsed, sort_keys=True).encode()).hexdigest()[:10] if len(parsed) == 2 else None
    yield {"type": "done", "seconds": round(time.time() - t0, 1), "tool_calls": calls, "tokens": tokens, "model": model,
           "parsers": source, "parser_attempts": dict(attempts), "reviews": reviews, "lessons_added": lessons_added, "repair_used": [k for k in stale if k in parsed and source[k].startswith("repaired")], "parsed_hash": phash, "errors": errors, "steps_used": step if "step" in dir() else 0, "report": report.model_dump() if report else None}

def run_to_end(incident, model=DEFAULT_MODEL, lib=None, use_library=True):
    last = None
    for ev in triage(incident, model, lib, use_library): last = ev
    return last
