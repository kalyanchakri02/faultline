"""Persistent library of agent-written parsers, keyed by a cheap format fingerprint."""
import hashlib, json, os, re, threading, time
import sandbox, validators

_LOCK = threading.Lock()
FILES = {"log": "dmesg.log", "telemetry": "dcgm_telemetry.txt"}

def _shape(s: str) -> str:
    s = re.sub(r"[A-Za-z]+", "a", s); s = re.sub(r"\d+", "9", s); return re.sub(r"\s+", " ", s)

def fingerprint(text: str) -> str:
    line = next((l for l in text.splitlines() if l.strip()), "")
    if line.lstrip().startswith("{"):
        try: return "json:" + ",".join(sorted(json.loads(line).keys()))[:200]
        except Exception: pass
    return "text:" + _shape(line[:14])

def run_parser(code: str, text: str) -> dict:
    """Execute parser code in the sandbox against raw text. Returns {ok, data|error}."""
    harness = ("import json, runpy\nns = runpy.run_path('_parser.py')\n"
               "out = ns['parse'](open('_input.txt', encoding='utf-8', errors='replace').read())\n"
               "print(json.dumps(out, default=str))")
    r = sandbox.run_python(harness, {"_parser.py": code, "_input.txt": text}, timeout=20, max_out=2_000_000)
    if r["exit_code"] != 0: return {"ok": False, "error": (r["stderr"] or "non-zero exit")[-1500:]}
    try: return {"ok": True, "data": json.loads(r["stdout"])}
    except Exception as e: return {"ok": False, "error": f"parse() output was not JSON-serialisable: {e}"}

def check(kind: str, code: str, text: str) -> dict:
    r = run_parser(code, text)
    if not r["ok"]: return {"ok": False, "problems": [r["error"]], "data": None}
    v = (validators.validate_log if kind == "log" else validators.validate_telemetry)(r["data"], text)
    return {**v, "data": r["data"]}

class Library:
    def __init__(self, path="library"):
        self.path = path; os.makedirs(path, exist_ok=True)
        self.index_file = os.path.join(path, "index.json")

    def _load(self):
        try: return json.load(open(self.index_file))
        except Exception: return {}

    def entries(self): return self._load()

    def lookup(self, kind, fp):
        e = self._load().get(f"{kind}|{fp}")
        if not e: return None
        return {**e, "code": open(os.path.join(self.path, e["file"])).read()}

    def save(self, kind, fp, code, description):
        with _LOCK:
            idx = self._load(); key = f"{kind}|{fp}"
            ver = idx.get(key, {}).get("version", 0) + 1
            name = f"{kind}_{hashlib.sha1(fp.encode()).hexdigest()[:8]}_v{ver}"
            with open(os.path.join(self.path, name + ".py"), "w") as f: f.write(code)
            idx[key] = {"kind": kind, "fingerprint": fp, "file": name + ".py", "name": name, "version": ver,
                        "description": description[:200], "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "uses": idx.get(key, {}).get("uses", 0)}
            tmp = self.index_file + ".tmp"; json.dump(idx, open(tmp, "w"), indent=1); os.replace(tmp, self.index_file)
        return name

    def record_use(self, kind, fp):
        with _LOCK:
            idx = self._load(); e = idx.get(f"{kind}|{fp}")
            if e:
                e["uses"] += 1
                tmp = self.index_file + ".tmp"; json.dump(idx, open(tmp, "w"), indent=1); os.replace(tmp, self.index_file)
