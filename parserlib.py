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
    """Index of validated parsers (many per kind, so old and new formats can coexist) plus a lessons memory."""
    def __init__(self, path="library"):
        self.path = path; os.makedirs(path, exist_ok=True)
        self.index_file = os.path.join(path, "index.json"); self.lessons_file = os.path.join(path, "lessons.json")

    def _load(self):
        try: raw = json.load(open(self.index_file))
        except Exception: return {}
        return {(e.get("name") or k): e for k, e in raw.items()} if any("|" in k for k in raw) else raw

    def _store(self, idx):
        tmp = self.index_file + ".tmp"; json.dump(idx, open(tmp, "w"), indent=1); os.replace(tmp, self.index_file)

    def entries(self): return self._load()

    def candidates(self, kind, fp):
        """Saved parsers of this kind. Exact-fingerprint matches first, then most-used. The fingerprint only ORDERS candidates;
        the validator against the raw file decides which one is used."""
        out = [{**e, "code": open(os.path.join(self.path, e["file"])).read()} for e in self._load().values() if e["kind"] == kind]
        return sorted(out, key=lambda e: (e["fingerprint"] != fp, -e.get("uses", 0)))

    def save(self, kind, fp, code, description, origin="learned", parent=None):
        with _LOCK:
            idx = self._load()
            ver = 1
            while f"{kind}_{hashlib.sha1(fp.encode()).hexdigest()[:8]}_v{ver}" in idx: ver += 1   # never overwrite an existing parser
            name = f"{kind}_{hashlib.sha1(fp.encode()).hexdigest()[:8]}_v{ver}"
            with open(os.path.join(self.path, name + ".py"), "w") as f: f.write(code)
            idx[name] = {"kind": kind, "fingerprint": fp, "file": name + ".py", "name": name, "version": ver, "origin": origin, "parent": parent,
                         "description": description[:200], "created": time.strftime("%Y-%m-%d %H:%M:%S"), "uses": 0}
            self._store(idx)
        return name

    def record_use(self, name):
        with _LOCK:
            idx = self._load()
            if name in idx: idx[name]["uses"] = idx[name].get("uses", 0) + 1; self._store(idx)

    def lessons(self, kind, n=5):
        try: ls = json.load(open(self.lessons_file))
        except Exception: ls = []
        return [l for l in ls if l["kind"] == kind][-n:]

    def add_lesson(self, kind, fp, text):
        with _LOCK:
            try: ls = json.load(open(self.lessons_file))
            except Exception: ls = []
            if any(l["text"] == text for l in ls): return False
            ls.append({"kind": kind, "fingerprint": fp, "text": text, "created": time.strftime("%Y-%m-%d %H:%M:%S")})
            tmp = self.lessons_file + ".tmp"; json.dump(ls, open(tmp, "w"), indent=1); os.replace(tmp, self.lessons_file)
        return True
