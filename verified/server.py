"""FastAPI backend for the local UI.

Long-running pipeline phases run in worker threads; progress is streamed to
the browser as Server-Sent Events.  The webcam is captured in the browser and
posted here as JPEG frames, so the backend can run anywhere (incl. Docker).
"""
from __future__ import annotations

import asyncio
import io
import json
import queue
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT, Settings
from .face.engine import FaceEngine, eye_aspect_ratios
from .pipeline import Pipeline

WEB = ROOT / "web"
app = FastAPI(title="verified", version="1.0")
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

_settings = Settings()
_engine: FaceEngine | None = None
_engine_lock = threading.Lock()
_runs: dict[str, dict] = {}  # run_id -> {"events": deque, "queues": [Queue], "status": str}
_liveness: dict[str, dict] = {}  # session -> {"yaw": deque, "passed": bool}


def engine() -> FaceEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = FaceEngine()
    return _engine


def _run_state(run_id: str) -> dict:
    if run_id not in _runs:
        _runs[run_id] = {"events": deque(maxlen=5000), "queues": [], "status": "idle", "lock": threading.Lock()}
    return _runs[run_id]


def _emitter(run_id: str):
    st = _run_state(run_id)

    def emit(event: str, payload: dict):
        item = {"event": event, "data": payload, "t": time.time()}
        with st["lock"]:
            st["events"].append(item)
            for q in list(st["queues"]):
                q.put(item)

    return emit


def _pipeline(run_id: str) -> Pipeline:
    return Pipeline(Settings(), engine(), emit=_emitter(run_id))


# ------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
def status():
    s = Settings()
    out = {
        "chain": s.chain_name,
        "chain_id": s.chain_id,
        "explorer": s.explorer_url,
        "contract": s.contract_address,
        "eas": bool(s.enable_eas and s.eas_contract),
        "ots": s.enable_ots,
        "engines": {
            "google_lens": bool(s.serpapi_key),
            "yandex": bool(s.serpapi_key and s.enable_yandex),
            "google_reverse": bool(s.serpapi_key and s.enable_google_reverse),
            "google_vision": bool(s.google_vision_api_key or s.google_application_credentials),
            "bluesky": s.enable_bluesky,
            "name_expansion": s.enable_name_expansion and bool(s.serpapi_key),
        },
        "ipfs": bool(s.pinata_jwt),
        "wallet": None,
        "balance_eth": None,
        "rpc": None,
        "block": None,
    }
    try:
        from eth_account import Account

        from .chain.registry import connect

        w3 = connect(s)
        out["rpc"] = w3.provider.endpoint_uri
        out["block"] = w3.eth.block_number
        if s.private_key:
            acct = Account.from_key(s.private_key)
            out["wallet"] = acct.address
            out["balance_eth"] = float(w3.from_wei(w3.eth.get_balance(acct.address), "ether"))
        if s.contract_address:
            out["contract_deployed"] = len(w3.eth.get_code(w3.to_checksum_address(s.contract_address))) > 0
            try:
                from .chain.registry import Registry

                out["records"] = Registry(s, w3).count() if s.private_key else None
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        out["rpc_error"] = str(e)[:200]
    return out


# ----------------------------------------------------------------- preview
@app.post("/api/preview")
async def preview(frame: UploadFile = File(...), session: str = Form("default")):
    """Live face feedback for the webcam view + head-turn liveness challenge."""
    data = await frame.read()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "bad frame")
    eng = engine()
    faces = await asyncio.to_thread(eng.detect, img, 0, 320)
    if len(_liveness) > 200:  # bound memory: drop the oldest sessions
        for k in list(_liveness)[:100]:
            _liveness.pop(k, None)
    st = _liveness.setdefault(session, {"yaw": deque(maxlen=40), "passed": False, "left": False, "right": False, "frames": 0})
    out = {"faces": [], "liveness": {"passed": st["passed"], "left": st["left"], "right": st["right"]}, "size": [img.shape[1], img.shape[0]]}
    if faces:
        faces.sort(key=lambda f: -(f.width * f.height))
        f = faces[0]
        eng.quality(img, f)
        yaw = f.kps[2][0] - (f.kps[0][0] + f.kps[1][0]) / 2  # nose offset from eye midpoint
        yaw_ratio = float(yaw / (np.linalg.norm(f.kps[1] - f.kps[0]) + 1e-6))
        st["yaw"].append(yaw_ratio)
        st["frames"] += 1
        if yaw_ratio < -0.18:
            st["left"] = True
        if yaw_ratio > 0.18:
            st["right"] = True
        if st["left"] and st["right"] and not st["passed"]:
            st["passed"] = True
        ear = None
        try:
            lm = eng.landmarks(img, f)
            ear = [round(v, 3) for v in eye_aspect_ratios(lm)]
        except Exception:  # noqa: BLE001
            pass
        hints = []
        q = f.quality
        if q["size"] < 0.6:
            hints.append("move closer")
        if q["sharpness"] < 0.35:
            hints.append("hold still / more light")
        if q["brightness"] < 0.45:
            hints.append("adjust lighting")
        if abs(yaw_ratio) > 0.25 and st["passed"]:
            hints.append("face the camera")
        out["faces"] = [{"bbox": [round(float(v), 1) for v in f.bbox], "kps": f.kps.round(1).tolist(), "det_score": round(f.score, 3), "quality": q, "yaw_ratio": round(yaw_ratio, 3), "ear": ear, "hints": hints, "others": len(faces) - 1}]
        out["liveness"] = {"passed": st["passed"], "left": st["left"], "right": st["right"], "yaw": round(yaw_ratio, 3)}
    return out


@app.post("/api/preview/reset")
def preview_reset(session: str = Form("default")):
    _liveness.pop(session, None)
    return {"ok": True}


# ------------------------------------------------------------------- scan
@app.post("/api/scan")
async def scan(image: UploadFile = File(...), source: str = Form("upload"), auto_anchor: bool = Form(True)):
    data = await image.read()
    if not data:
        raise HTTPException(400, "empty image")
    run_id = Pipeline.new_run_id()
    st = _run_state(run_id)
    st["status"] = "running"

    def work():
        try:
            p = _pipeline(run_id)
            summary = p.scan(data, source=source, run_id=run_id)
            if summary["status"] == "matches" and auto_anchor:
                p.anchor(run_id)
            elif summary["status"] != "matches":
                p.emit("run.done", {"run_id": run_id, "status": summary["status"], "rejected_top": summary.get("search", {}).get("rejected", [])[:6] if summary.get("search") else []})
            st["status"] = "done"
        except Exception as e:  # noqa: BLE001
            st["status"] = "error"
            _emitter(run_id)("run.error", {"run_id": run_id, "message": f"{type(e).__name__}: {e}"})

    threading.Thread(target=work, daemon=True).start()
    return {"run_id": run_id}


@app.post("/api/anchor/{run_id}")
def anchor(run_id: str, match_index: int | None = Form(None)):
    st = _run_state(run_id)

    def work():
        try:
            st["status"] = "running"
            _pipeline(run_id).anchor(run_id, match_index)
            st["status"] = "done"
        except Exception as e:  # noqa: BLE001
            st["status"] = "error"
            _emitter(run_id)("run.error", {"run_id": run_id, "message": f"{type(e).__name__}: {e}"})

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


@app.post("/api/verify/{run_id}")
def verify(run_id: str, tamper: bool = Form(False), field: str = Form("match.similarity"), refetch: bool = Form(True)):
    s = Settings()
    run_dir = s.runs_dir / run_id
    if not (run_dir / "anchor.json").exists():
        raise HTTPException(404, "run not anchored")
    override = None
    tampered_info = None
    if tamper:
        from .chain.evidence import canonical_bytes

        bundle = json.loads((run_dir / "bundle.json").read_bytes())
        node = bundle
        parts = field.split(".")
        try:
            for k in parts[:-1]:
                node = node[k]
            old = node[parts[-1]]
        except (KeyError, TypeError):
            raise HTTPException(400, f"unknown bundle field {field!r}")
        new = (round(old + 0.0001, 4) if isinstance(old, float) else old + 1) if isinstance(old, (int, float)) and not isinstance(old, bool) else str(old) + "​x"
        node[parts[-1]] = new
        override = canonical_bytes(bundle)
        tampered_info = {"field": field, "old": old, "new": new}
    emit = _emitter(run_id)
    emit("verify.start", {"tamper": tampered_info, "refetch": refetch})
    rep = Pipeline(s, engine(), emit=emit).verify_run(run_id, refetch=refetch and not tamper, bundle_override=override)
    rep["tamper"] = tampered_info
    return rep


# ------------------------------------------------------------------ events
@app.get("/api/events/{run_id}")
async def events(run_id: str, since: float = 0.0):
    """SSE stream of pipeline events. `since` (unix time) skips the replayed history."""
    st = _run_state(run_id)
    q: queue.Queue = queue.Queue()
    with st["lock"]:
        history = [e for e in st["events"] if e["t"] > since]
        st["queues"].append(q)

    async def gen():
        try:
            for item in history:
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'], default=str)}\n\n"
            last_ping = time.time()
            while True:
                try:
                    item = q.get_nowait()
                    yield f"event: {item['event']}\ndata: {json.dumps(item['data'], default=str)}\n\n"
                    if item["event"] in ("run.done", "run.error"):
                        # keep the stream open a little for late consumers, then end
                        await asyncio.sleep(0.5)
                        break
                except queue.Empty:
                    await asyncio.sleep(0.15)
                    if time.time() - last_ping > 15:
                        yield ": ping\n\n"
                        last_ping = time.time()
        finally:
            with st["lock"]:
                if q in st["queues"]:
                    st["queues"].remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# -------------------------------------------------------------------- runs
@app.get("/api/runs")
def runs():
    s = Settings()
    out = []
    if s.runs_dir.exists():
        for d in sorted(s.runs_dir.iterdir(), reverse=True):
            f = d / "run.json"
            if f.exists():
                try:
                    j = json.loads(f.read_text())
                    m = None
                    if j.get("status") == "anchored":
                        m = j["search"]["matches"][j["selected_match"]]
                    out.append(
                        {
                            "run_id": j["run_id"],
                            "status": j["status"],
                            "source": j.get("source"),
                            "matches": len(j.get("search", {}).get("matches", [])) if j.get("search") else 0,
                            "platform": m["platform"] if m else None,
                            "link": m["canonical_link"] if m else None,
                            "similarity": m["similarity"] if m else None,
                            "tx": j.get("anchor", {}).get("explorer_tx") if j.get("anchor") else None,
                            "record_id": j.get("anchor", {}).get("record_id") if j.get("anchor") else None,
                            "verdict": j.get("verification", {}).get("verdict") if j.get("verification") else None,
                        }
                    )
                except Exception:  # noqa: BLE001
                    continue
    return out[:50]


@app.get("/api/run/{run_id}")
def run_detail(run_id: str):
    f = Settings().runs_dir / run_id / "run.json"
    if not f.exists():
        raise HTTPException(404)
    return JSONResponse(json.loads(f.read_text()))


@app.get("/api/run/{run_id}/file/{name}")
def run_file(run_id: str, name: str):
    d = Settings().runs_dir / run_id
    p = (d / name).resolve()
    if not str(p).startswith(str(d.resolve())) or not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p))


@app.get("/api/qr")
def qr(text: str):
    import qrcode

    img = qrcode.make(text, box_size=6, border=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")
