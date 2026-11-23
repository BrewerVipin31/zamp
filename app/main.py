"""FastAPI app: upload/sample -> background pipeline run -> live SSE stage events -> stored history."""
import asyncio
import hashlib
import json
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from . import extract_llm, store
from .pipeline import STAGES, Pipeline

app = FastAPI(title="AP Autopilot")
STATIC = Path(__file__).parent / "static"
LIVE: dict[int, dict] = {}  # run_id -> {"events": [...], "done": bool}
MAX_BYTES = 10 * 1024 * 1024
store.init()


def _start(data: bytes, filename: str) -> int:
    sha = hashlib.sha256(data).hexdigest()
    run_id = store.create_run(filename, sha)
    (store.UPLOADS / f"{run_id}.bin").write_bytes(data)
    LIVE[run_id] = {"events": [], "done": False}

    def emit(ev):
        LIVE[run_id]["events"].append(ev)

    def work():
        try:
            res = Pipeline(run_id, data, filename, emit).run()
            store.finish_run(run_id, res)
            emit({"type": "done", "result": res})
        except Exception as e:  # never leave the UI hanging
            store.fail_run(run_id, f"{type(e).__name__}: {e}")
            emit({"type": "error", "message": str(e)})
        finally:
            LIVE[run_id]["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return run_id


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/static/{name}")
def static(name: str):
    p = (STATIC / name).resolve()
    if p.parent != STATIC.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/config")
def config():
    p = extract_llm.provider()
    return {"extractor": f"{p}:{extract_llm.model_name(p)}" if p else "rules",
            "stages": [{"key": k, "name": n, "desc": d} for k, n, d in STAGES],
            "company": store.COMPANY, "rules": store.RULES}


@app.get("/api/samples")
def samples():
    return json.loads((store.SAMPLES / "manifest.json").read_text())


@app.get("/samples/{name}")
def sample_file(name: str):
    p = (store.SAMPLES / name).resolve()
    if p.parent != store.SAMPLES.resolve() or p.suffix != ".pdf" or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


@app.post("/api/runs")
async def create_run(file: UploadFile | None = File(None), sample: str | None = Form(None)):
    if sample:
        p = (store.SAMPLES / sample).resolve()
        if p.parent != store.SAMPLES.resolve() or not p.exists():
            raise HTTPException(404, "unknown sample")
        data, name = p.read_bytes(), sample
    elif file is not None:
        data, name = await file.read(), file.filename or "upload"
        if not data:
            raise HTTPException(400, "empty file")
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "file larger than 10 MB")
    else:
        raise HTTPException(400, "send a file or a sample name")
    return {"run_id": _start(data, name)}


@app.get("/api/runs/{run_id}/events")
async def events(run_id: int):
    if run_id not in LIVE:
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404)

        async def replay():
            res = run["result"]
            if res:
                for st in res["stages"]:
                    yield f"data: {json.dumps({'type': 'stage_done', 'stage': st['key'], 'payload': st})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'result': res})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'error', 'message': run.get('primary_reason') or 'run did not finish'})}\n\n"
        return StreamingResponse(replay(), media_type="text/event-stream")

    async def stream():
        i = 0
        while True:
            evs = LIVE[run_id]["events"]
            while i < len(evs):
                yield f"data: {json.dumps(evs[i], default=str)}\n\n"
                i += 1
            if LIVE[run_id]["done"] and i >= len(LIVE[run_id]["events"]):
                break
            await asyncio.sleep(0.08)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def runs():
    return store.list_runs()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int):
    r = store.get_run(run_id)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: int):
    p = store.UPLOADS / f"{run_id}.bin"
    if not p.exists():
        raise HTTPException(404)
    data = p.read_bytes()
    mt = "application/pdf" if data[:4] == b"%PDF" else "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    return FileResponse(p, media_type=mt)


@app.get("/api/reference")
def reference():
    pos = []
    for po in store.POS:
        used = store.po_consumption(po["po_number"])
        value = sum(l["quantity"] * l["unit_price"] for l in po["lines"])
        billed = sum(u["amount"] for u in used.values())
        vend = next(v for v in store.VENDORS if v["vendor_id"] == po["vendor_id"])
        pos.append({**po, "vendor_name": vend["name"], "value": value, "billed": billed, "remaining": value - billed,
                    "lines": [{**l, "billed_qty": used.get(l["line"], {}).get("quantity", 0)} for l in po["lines"]]})
    return {"company": store.COMPANY, "vendors": store.VENDORS, "pos": pos, "rules": store.RULES, "ledger": store.ledger()}


@app.post("/api/reset")
def reset():
    if any(not v["done"] for v in LIVE.values()):
        raise HTTPException(409, "a run is in progress")
    LIVE.clear()
    store.reset()
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True}
