"""Model asset management for the face engine.

We use the InsightFace *buffalo_l* model pack (Apache-2.0):
  * det_10g.onnx     - SCRFD-10GF face detector with 5-point landmarks
  * w600k_r50.onnx   - ArcFace ResNet-50 trained on WebFace600K (512-d embeddings)
  * genderage.onnx   - gender / age attribute head
  * 2d106det.onnx    - 106-point 2D landmark model (used for liveness / pose)

The models are downloaded on first use so the git repository stays small and
no compiled dependencies (the `insightface` package needs a C++ toolchain on
Windows) are required - we run the ONNX graphs directly with onnxruntime.
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import httpx

BUFFALO_L_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
REQUIRED = ["det_10g.onnx", "w600k_r50.onnx", "genderage.onnx", "2d106det.onnx"]


def models_dir() -> Path:
    root = os.environ.get("VERIFIED_MODELS_DIR")
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[2] / "models" / "buffalo_l"


def ensure_models(progress=None) -> Path:
    """Download and unpack buffalo_l if any required model is missing."""
    d = models_dir()
    d.mkdir(parents=True, exist_ok=True)
    if all((d / m).exists() for m in REQUIRED):
        return d
    if progress:
        progress("Downloading InsightFace buffalo_l model pack (~275 MB)...")
    with httpx.Client(follow_redirects=True, timeout=None) as client:
        with client.stream("GET", BUFFALO_L_URL) as r:
            r.raise_for_status()
            buf = io.BytesIO()
            total = int(r.headers.get("content-length", 0))
            done = 0
            for chunk in r.iter_bytes(1 << 20):
                buf.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(f"  {done / 1e6:6.1f} / {total / 1e6:6.1f} MB")
    with zipfile.ZipFile(buf) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if base in REQUIRED:
                with z.open(name) as src, open(d / base, "wb") as dst:
                    dst.write(src.read())
    missing = [m for m in REQUIRED if not (d / m).exists()]
    if missing:
        raise RuntimeError(f"Model download incomplete, missing: {missing}")
    return d


if __name__ == "__main__":
    ensure_models(print)
    print("models ready at", models_dir())
