import json, os, shutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import loggen, agent
from parserlib import Library

LIBDIR = os.environ.get("FAULTLINE_LIB", "library")
app = FastAPI(title="FaultLine")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index(): return FileResponse("static/index.html")

@app.get("/api/scenarios")
def scenarios():
    return {"scenarios": [{"id": k, "title": v["title"]} for k, v in loggen.SCENARIOS.items()],
            "formats": [{"id": i, "log": l, "telemetry": t} for i, (l, t) in enumerate(loggen.FORMATS)]}

@app.get("/api/incident/{name}")
def incident(name: str, seed: int = 7, fmt: int = 0):
    if name not in loggen.SCENARIOS: raise HTTPException(404)
    d = loggen.generate(name, seed, fmt)
    return {"alert": d["alert"], "fmt": d["fmt"], "log_head": d["files"]["dmesg.log"].splitlines()[:5], "telemetry_head": d["files"]["dcgm_telemetry.txt"].splitlines()[:3]}

@app.get("/api/library")
def library(): return list(Library(LIBDIR).entries().values())

@app.post("/api/library/reset")
def reset():
    shutil.rmtree(LIBDIR, ignore_errors=True); Library(LIBDIR); return {"ok": True}

@app.get("/api/triage/{name}")
def triage(name: str, seed: int = 7, fmt: int = 0):
    if name not in loggen.SCENARIOS: raise HTTPException(404)
    inc = loggen.generate(name, seed, fmt)
    def stream():
        for ev in agent.triage(inc, lib=Library(LIBDIR)):
            yield f"data: {json.dumps(ev)}\n\n"
        yield f"data: {json.dumps({'type': 'truth', 'truth': inc['truth']})}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
