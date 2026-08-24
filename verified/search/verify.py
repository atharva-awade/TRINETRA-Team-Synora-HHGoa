"""Biometric re-verification of search candidates.

Reverse-image engines return *visually similar* pages.  We turn those into
*identity-verified* matches: download each candidate image, detect faces,
embed them with ArcFace and compare against the query embedding.  Only
candidates whose best face clears the similarity threshold survive.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import cv2
import httpx
import numpy as np

from ..face.engine import FaceEngine, cosine, crop_face, similarity_band
from .types import Candidate, VerifiedMatch

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
MAX_BYTES = 8 * 1024 * 1024
_ALLOW_LOCAL = os.environ.get("ALLOW_LOCAL_FETCH", "") == "1"


def _is_private_host(url: str) -> bool:
    """SSRF guard: never let engine-supplied URLs point the fetcher at local/private networks."""
    try:
        host = urlparse(url).hostname or ""
        if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
            return True
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        return False  # a normal DNS name


def fetch_image(url: str, timeout: float = 10) -> bytes | None:
    if not url or not url.startswith("http"):
        return None
    if not _ALLOW_LOCAL and _is_private_host(url):
        return None
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA, "Accept": "image/*,*/*;q=0.8", "Referer": "https://www.google.com/"}) as c:
            with c.stream("GET", url) as r:
                if r.status_code != 200:
                    return None
                ctype = r.headers.get("content-type", "")
                if "text/html" in ctype:
                    return None
                buf = bytearray()
                for chunk in r.iter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > MAX_BYTES:
                        return None
                return bytes(buf)
    except Exception:  # noqa: BLE001
        return None


def decode_image(data: bytes) -> np.ndarray | None:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    if max(h, w) > 1600:  # keep inference fast
        s = 1600 / max(h, w)
        img = cv2.resize(img, (int(w * s), int(h * s)))
    if min(h, w) < 60:
        return None
    return img


def _b64_jpeg(img: np.ndarray, max_side: int = 160) -> str:
    h, w = img.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s < 1:
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode() if ok else ""


def verify_candidate(engine: FaceEngine, query_emb: np.ndarray, cand: Candidate) -> VerifiedMatch | None:
    """Download the candidate image(s) and compute the best face similarity."""
    tried = []
    for url in [u for u in (cand.image, cand.thumbnail) if u]:
        if url in tried:
            continue
        tried.append(url)
        data = fetch_image(url)
        if not data:
            continue
        img = decode_image(data)
        if img is None:
            continue
        faces = engine.detect(img)
        if not faces:
            # a thumbnail may have failed detection where the full image works
            continue
        best, best_face = -1.0, None
        for f in faces:
            engine.embed(img, f)
            s = cosine(query_emb, f.embedding)
            if s > best:
                best, best_face = s, f
        if best_face is None:
            continue
        return VerifiedMatch(
            candidate=cand,
            similarity=best,
            band=similarity_band(best),
            face_bbox=[float(v) for v in best_face.bbox],
            candidate_faces=len(faces),
            image_used=url,
            image_sha256=hashlib.sha256(data).hexdigest(),
            face_crop_b64=_b64_jpeg(crop_face(img, best_face)),
        )
    return None


def verify_all(engine: FaceEngine, query_emb: np.ndarray, candidates: list[Candidate], workers: int = 6, on_progress=None) -> list[VerifiedMatch]:
    """Verify candidates concurrently (network-bound); inference is serialised
    per ONNX session so we keep a modest pool."""
    results: list[VerifiedMatch] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(verify_candidate, engine, query_emb, c): c for c in candidates}
        for fut in as_completed(futs):
            done += 1
            try:
                m = fut.result()
            except Exception:  # noqa: BLE001
                m = None
            if m is not None:
                results.append(m)
            if on_progress:
                on_progress(done, len(candidates), m)
    results.sort(key=lambda m: m.similarity, reverse=True)
    return results


def dedupe(matches: list[VerifiedMatch]) -> list[VerifiedMatch]:
    """Merge matches pointing at the same canonical URL (keep the best score,
    remember every engine that surfaced it)."""
    by_key: dict[str, VerifiedMatch] = {}
    for m in matches:
        k = m.candidate.key
        if k in by_key:
            keep = by_key[k]
            if m.candidate.engine not in keep.engines:
                keep.engines.append(m.candidate.engine)
            if m.similarity > keep.similarity:
                m.engines = keep.engines
                by_key[k] = m
        else:
            m.engines = [m.candidate.engine]
            by_key[k] = m
    out = list(by_key.values())
    out.sort(key=lambda m: (m.similarity, m.candidate.is_post, m.candidate.is_social), reverse=True)
    return out
