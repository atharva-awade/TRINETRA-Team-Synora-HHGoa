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
import re
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT, Settings
from .face.engine import FaceEngine, eye_aspect_ratios
from .pipeline import Pipeline

WEB = ROOT / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # load the ONNX models off the event loop so the first webcam frame is instant
    task = asyncio.create_task(asyncio.to_thread(engine))
    task.add_done_callback(lambda t: t.exception() and print("[verified] model warm-up failed:", t.exception()))
    yield


app = FastAPI(title="verified", version="1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

_settings = Settings()
_engine: FaceEngine | None = None
_engine_lock = threading.Lock()
_runs: dict[str, dict] = {}  # run_id -> {"events": deque, "queues": [Queue], "status": str}
_runs_lock = threading.Lock()
_liveness: dict[str, dict] = {}  # session -> {"yaw": deque, "passed": bool}
_status_cache: dict = {"t": 0.0, "w3": None}

RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def engine() -> FaceEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = FaceEngine()
    return _engine


def _check_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(400, "invalid run id")
    return run_id


def same_origin(request: Request) -> None:
    """CSRF guard for state-changing endpoints: browsers always send Origin on
    cross-site POSTs; we only accept requests originating from this server."""
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        if urlparse(origin).netloc.lower() != host.lower():
            raise HTTPException(403, "cross-origin request rejected")


def _run_state(run_id: str) -> dict:
    """In-memory event buffer for a run. Pruned by age and by count so a long
    session cannot grow without bound (events carry base64 thumbnails)."""
    with _runs_lock:
        if run_id not in _runs:
            now = time.time()
            stale = [k for k, v in _runs.items() if not v["queues"] and now - v["created"] > 1800]
            for k in stale:
                _runs.pop(k, None)
            if len(_runs) > 40:
                for k in [k for k, v in _runs.items() if not v["queues"]][:20]:
                    _runs.pop(k, None)
            _runs[run_id] = {"events": deque(maxlen=4000), "queues": [], "status": "idle", "lock": threading.Lock(), "created": time.time()}
        return _runs[run_id]


def _emitter(run_id: str):
    st = _run_state(run_id)

    def emit(event: str, payload: dict):
        now = time.time()
        if isinstance(payload, dict):
            payload = {**payload, "__t": now}  # lets the UI resume a stream exactly where it left off
        item = {"event": event, "data": payload, "t": now}
        with st["lock"]:
            st["events"].append(item)
            for q in list(st["queues"]):
                q.put(item)

    return emit


def _clean_error(e: BaseException) -> str:
    """Error text for the browser: keep the message, strip absolute paths."""
    msg = f"{type(e).__name__}: {e}"
    msg = msg.replace(str(ROOT) + "/", "").replace(str(ROOT) + "\\", "").replace(str(ROOT), ".")
    return msg[:400]


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

        if _status_cache["w3"] is None or time.time() - _status_cache["t"] > 120:
            _status_cache["w3"] = connect(s)
            _status_cache["t"] = time.time()
        w3 = _status_cache["w3"]
        out["rpc"] = getattr(w3.provider, "endpoint_uri", None) or type(w3.provider).__name__
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
        _status_cache["w3"] = None
        out["rpc_error"] = str(e)[:200]
    return out


# ----------------------------------------------------------------- preview
@app.post("/api/preview", dependencies=[Depends(same_origin)])
async def preview(frame: UploadFile = File(...), session: str = Form("default")):
    """Live face feedback for the webcam view + head-turn liveness challenge."""
    data = await frame.read()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "bad frame")
    eng = await asyncio.to_thread(engine)
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
        q.setdefault("overall", 0.0)
        for k in ("sharpness", "size", "brightness", "frontal"):
            q.setdefault(k, 0.0)
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


@app.post("/api/preview/reset", dependencies=[Depends(same_origin)])
def preview_reset(session: str = Form("default")):
    _liveness.pop(session, None)
    return {"ok": True}


# ------------------------------------------------------------------- scan
@app.post("/api/scan", dependencies=[Depends(same_origin)])
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
            elif summary["status"] == "matches":
                p.emit("run.done", {"run_id": run_id, "status": "matches", "manual": True})
            else:
                rejected = summary.get("search", {}).get("rejected", []) if summary.get("search") else []
                p.emit("run.done", {"run_id": run_id, "status": summary["status"], "rejected_top": [{k: v for k, v in r.items() if k != "face_crop_b64"} for r in rejected[:6]]})
            st["status"] = "done"
        except Exception as e:  # noqa: BLE001
            st["status"] = "error"
            _emitter(run_id)("run.error", {"run_id": run_id, "message": _clean_error(e)})

    threading.Thread(target=work, daemon=True).start()
    return {"run_id": run_id}


@app.post("/api/anchor/{run_id}", dependencies=[Depends(same_origin)])
def anchor(run_id: str, match_index: int | None = Form(None)):
    _check_run_id(run_id)
    if not (Settings().runs_dir / run_id / "run.json").exists():
        raise HTTPException(404, "unknown run")
    st = _run_state(run_id)
    if st["status"] == "running":
        raise HTTPException(409, "run is busy")

    def work():
        try:
            st["status"] = "running"
            _pipeline(run_id).anchor(run_id, match_index)
            st["status"] = "done"
        except Exception as e:  # noqa: BLE001
            st["status"] = "error"
            _emitter(run_id)("run.error", {"run_id": run_id, "message": _clean_error(e)})

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


@app.post("/api/verify/{run_id}", dependencies=[Depends(same_origin)])
def verify(run_id: str, tamper: bool = Form(False), field: str = Form("match.similarity"), refetch: bool = Form(True)):
    _check_run_id(run_id)
    s = Settings()
    run_dir = s.runs_dir / run_id
    if not (run_dir / "anchor.json").exists():
        raise HTTPException(404, "run not anchored yet")
    override = None
    tampered_info = None
    if tamper:
        from .chain.evidence import canonical_bytes

        bundle = json.loads((run_dir / "bundle.json").read_bytes().decode("utf-8"))
        node = bundle
        parts = field.split(".")
        try:
            for k in parts[:-1]:
                node = node[k]
            old = node[parts[-1]]
        except (KeyError, TypeError):
            raise HTTPException(400, f"unknown bundle field {field!r}")
        new = (round(old + 0.0001, 4) if isinstance(old, float) else old + 1) if isinstance(old, (int, float)) and not isinstance(old, bool) else str(old) + "x"
        node[parts[-1]] = new
        override = canonical_bytes(bundle)
        tampered_info = {"field": field, "old": old, "new": new}
    emit = _emitter(run_id)
    emit("verify.start", {"tamper": tampered_info, "refetch": refetch})
    try:
        rep = Pipeline(s, engine(), emit=emit).verify_run(run_id, refetch=refetch and not tamper, bundle_override=override)
    except Exception as e:  # noqa: BLE001
        emit("run.error", {"run_id": run_id, "message": _clean_error(e)})
        raise HTTPException(500, _clean_error(e))
    rep["tamper"] = tampered_info
    return rep


# ------------------------------------------------------------------ events
@app.get("/api/events/{run_id}")
async def events(run_id: str, since: float = 0.0):
    """SSE stream of pipeline events.

    `since` is a unix timestamp: only events newer than it are replayed.
    `since=-1` skips the history entirely (used when the client has just
    triggered new work and only wants what happens from now on)."""
    _check_run_id(run_id)
    st = _run_state(run_id)
    q: queue.Queue = queue.Queue()
    with st["lock"]:
        history = [] if since < 0 else [e for e in st["events"] if e["t"] > since]
        st["queues"].append(q)

    async def gen():
        try:
            yield "retry: 30000\n\n"  # a dropped stream must not hot-reconnect and replay
            for item in history:
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'], default=str)}\n\n"
            last_ping = opened = time.time()
            while True:
                try:
                    item = q.get_nowait()
                    yield f"event: {item['event']}\ndata: {json.dumps(item['data'], default=str)}\n\n"
                    last_ping = time.time()
                    if item["event"] in ("run.done", "run.error"):
                        await asyncio.sleep(0.5)  # let late consumers drain
                        break
                except queue.Empty:
                    await asyncio.sleep(0.15)
                    now = time.time()
                    if now - last_ping > 15:
                        yield ": ping\n\n"
                        last_ping = now
                    if now - opened > 1800:  # never hold a stream open forever
                        break
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
                    j = json.loads(f.read_text(encoding="utf-8"))
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
    _check_run_id(run_id)
    f = Settings().runs_dir / run_id / "run.json"
    if not f.exists():
        raise HTTPException(404)
    return JSONResponse(json.loads(f.read_text(encoding="utf-8")))


@app.get("/api/run/{run_id}/file/{name}")
def run_file(run_id: str, name: str):
    _check_run_id(run_id)
    if not FILE_NAME_RE.match(name) or name.startswith("."):
        raise HTTPException(404)
    d = Settings().runs_dir.resolve() / run_id
    p = (d / name).resolve()
    try:
        p.relative_to(d)
    except ValueError:
        raise HTTPException(404)
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(str(p))


@app.get("/api/qr")
def qr(text: str):
    import qrcode

    img = qrcode.make(text, box_size=6, border=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")
