"""Run model-written Python in an isolated subprocess with CPU/mem/time/file limits."""
import os, resource, subprocess, sys, tempfile

MAX_OUT = 4000

def _limits():
    # Each limit is best-effort: macOS rejects RLIMIT_AS and a low RLIMIT_NPROC breaks things, so skip those there.
    for name, val in (("RLIMIT_CPU", (10, 10)), ("RLIMIT_AS", (1 << 30, 1 << 30)),
                      ("RLIMIT_FSIZE", (1 << 22, 1 << 22)), ("RLIMIT_NPROC", (64, 64))):
        if sys.platform == "darwin" and name in ("RLIMIT_AS", "RLIMIT_NPROC"): continue
        try: resource.setrlimit(getattr(resource, name), val)
        except (ValueError, OSError, AttributeError): pass
    try: os.setsid()
    except OSError: pass

def run_python(code: str, files: dict, timeout: int = 20, max_out: int = MAX_OUT) -> dict:
    with tempfile.TemporaryDirectory(prefix="faultline_") as d:
        for n, c in files.items():
            with open(os.path.join(d, n), "w") as f: f.write(c)
        try:
            p = subprocess.run([sys.executable, "-I", "-c", code], cwd=d, capture_output=True, text=True,
                               timeout=timeout, preexec_fn=_limits, env={"PATH": "/usr/bin:/bin", "HOME": d})
            out, err, rc = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired:
            out, err, rc = "", f"TimeoutExpired after {timeout}s", -1
    cut = lambda s: s if len(s) <= max_out else s[:max_out] + f"\n...[truncated {len(s)-max_out} chars]"
    return {"stdout": cut(out), "stderr": cut(err), "exit_code": rc}
