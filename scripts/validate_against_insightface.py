#!/usr/bin/env python3
"""Prove that our dependency-free face engine matches reference InsightFace.

`verified` re-implements the InsightFace *buffalo_l* inference path (SCRFD
detector + ArcFace-R50 recogniser) directly on onnxruntime, so it installs with
no C++ toolchain. This script checks that the re-implementation is not merely
similar but numerically identical to the reference package on the same models.

    pip install insightface            # reference implementation, dev-only
    python scripts/validate_against_insightface.py

It reports, per sample image: face count, worst bounding-box delta, worst
landmark delta and the worst embedding cosine against the reference. Anything
below 0.9999 fails.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import cv2
import numpy as np

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from verified.face.engine import ARCFACE_DST, FaceEngine, cosine, umeyama_similarity  # noqa: E402


def iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    i = max(0, x2 - x1) * max(0, y2 - y1)
    return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i + 1e-9)


def main() -> int:
    try:
        from insightface.app import FaceAnalysis
        from insightface.utils import face_align
    except ImportError:
        print("This validation needs the reference package:  pip install insightface")
        return 2

    # point insightface at the very same model files we ship
    home = Path.home() / ".insightface" / "models"
    home.mkdir(parents=True, exist_ok=True)
    link = home / "buffalo_l"
    if not link.exists():
        try:
            link.symlink_to(ROOT / "models" / "buffalo_l", target_is_directory=True)
        except OSError:
            print(f"copy or link {ROOT / 'models' / 'buffalo_l'} to {link} first")
            return 2

    ref = FaceAnalysis(name="buffalo_l", allowed_modules=["detection", "recognition"], providers=["CPUExecutionProvider"])
    ref.prepare(ctx_id=-1, det_size=(640, 640))
    mine = FaceEngine()

    images = sorted(p for p in (ROOT / "samples").iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    worst_cos, worst_bbox, worst_kps, worst_m, total = 1.0, 0.0, 0.0, 0.0, 0
    print(f"{'image':24s} {'faces':>5s} {'bbox Δpx':>9s} {'kps Δpx':>8s} {'align Δ':>9s} {'min cosine':>11s}")
    for path in images:
        img = cv2.imread(str(path))
        if img is None:
            continue
        rfaces = ref.get(img)
        mfaces = mine.analyze(img, with_attrs=False)
        if not rfaces:
            print(f"{path.name:24s} {'0':>5s}  (no face in reference either: {len(mfaces)} here)")
            continue
        cos_l, bbox_l, kps_l, m_l = [], [], [], []
        for r in rfaces:
            m = max(mfaces, key=lambda m: iou(r.bbox, m.bbox)) if mfaces else None
            if m is None or iou(r.bbox, m.bbox) < 0.5:
                cos_l.append(0.0)
                continue
            total += 1
            cos_l.append(cosine(r.normed_embedding, m.embedding))
            bbox_l.append(float(np.abs(np.asarray(r.bbox) - np.asarray(m.bbox)).max()))
            kps_l.append(float(np.abs(np.asarray(r.kps) - np.asarray(m.kps)).max()))
            m_l.append(float(np.abs(face_align.estimate_norm(r.kps, 112) - umeyama_similarity(m.kps.astype(float), ARCFACE_DST.astype(float))).max()))
        worst_cos = min(worst_cos, min(cos_l, default=1.0))
        worst_bbox = max(worst_bbox, max(bbox_l, default=0.0))
        worst_kps = max(worst_kps, max(kps_l, default=0.0))
        worst_m = max(worst_m, max(m_l, default=0.0))
        print(f"{path.name:24s} {len(rfaces):5d} {max(bbox_l, default=0):9.4f} {max(kps_l, default=0):8.4f} {max(m_l, default=0):9.2e} {min(cos_l, default=1):11.6f}")

    print(f"\n{total} faces compared")
    print(f"  worst bounding-box delta      {worst_bbox:.4f} px")
    print(f"  worst landmark delta          {worst_kps:.4f} px")
    print(f"  worst alignment-matrix delta  {worst_m:.2e}")
    print(f"  worst embedding cosine        {worst_cos:.6f}")
    ok = worst_cos > 0.9999 and worst_bbox < 0.01 and worst_kps < 0.01
    print("\n" + ("PASS - numerically identical to reference InsightFace buffalo_l" if ok else "FAIL - the implementations diverge"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
