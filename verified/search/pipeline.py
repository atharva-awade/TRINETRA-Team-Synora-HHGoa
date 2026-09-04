"""Search orchestration: query preparation -> multi-engine fan-out ->
biometric re-verification -> identity inference -> name expansion -> metadata."""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from ..config import Settings
from ..face.engine import Face, FaceEngine, crop_face
from . import host
from .engines import bluesky, gvision, serpapi_engines as serp
from .metadata import fetch_metadata
from .types import Candidate, VerifiedMatch
from .verify import dedupe, verify_all

Emit = Callable[[str, dict], None]

NAME_STOP = {
    "instagram", "facebook", "linkedin", "twitter", "youtube", "pinterest", "reddit", "threads", "tiktok", "wikipedia", "getty", "images", "stock", "video", "reel", "reels", "photo", "photos", "official", "profile", "page", "the", "and", "for", "with", "news", "shutterstock", "alamy", "pictures", "picture", "free", "download", "hd", "wallpaper", "wallpapers", "biography", "wiki", "age", "net", "worth", "height", "movies", "latest", "new", "best", "top", "tv", "india", "indian", "usa", "uk", "of", "in", "on", "at", "by", "from", "to", "a", "an", "is", "his", "her", "how", "who", "what", "why", "when", "where", "medium", "quora", "youtuber", "actor", "actress", "ceo", "founder", "team", "post", "posts", "story", "stories", "live", "watch", "pic", "pics", "png", "jpg", "vector", "clipart", "portrait", "headshot", "linkedin.com", "x", "com", "www", "http", "https", "google", "search", "lens", "image", "similar", "matching", "full", "partial", "visually",
}


def infer_names(matches: list[VerifiedMatch], hints: list[str]) -> list[str]:
    """Infer the most likely person name from web entities + verified titles."""
    scores: Counter = Counter()
    for i, h in enumerate(hints[:8]):
        h = h.strip()
        if 2 <= len(h.split()) <= 4 and re.match(r"^[A-Z][\w.'-]+( [A-Z][\w.'-]+){1,3}$", h) and h.split()[0].lower() not in NAME_STOP:
            scores[h] += 3.0 / (1 + i)
    for m in matches:
        if m.band == "reject":
            continue
        meta = m.metadata if isinstance(m.metadata, dict) else {}
        txt = f"{m.candidate.title} {m.candidate.snippet} {meta.get('author') or ''} {meta.get('title') or ''}"
        txt = re.sub(r"[|•·@#()\[\]\"“”:,]", " ", txt)
        for mt in re.finditer(r"\b([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){1,2})\b", txt):
            name = mt.group(1)
            words = [w.lower() for w in name.split()]
            if any(w in NAME_STOP for w in words):
                continue
            scores[name] += m.similarity
    return [n for n, _ in scores.most_common(3)]


def encode_jpeg_under(img: np.ndarray, max_bytes: int = 480_000, max_side: int = 1000) -> bytes:
    if img is None or img.size == 0:
        raise ValueError("empty image")
    h, w = img.shape[:2]
    s = min(1.0, max_side / max(1, max(h, w)))
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    for q in (92, 85, 78, 70, 60, 50):
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
        if ok and len(buf) <= max_bytes:
            return buf.tobytes()
    # still too big: downscale further
    return encode_jpeg_under(cv2.resize(img, (img.shape[1] // 2, img.shape[0] // 2)), max_bytes, max_side)


@dataclass
class SearchResult:
    matches: list[VerifiedMatch]
    rejected: list[VerifiedMatch]
    candidates_total: int
    engines: dict
    names: list[str]
    query_public_url: str | None
    query_host: str | None
    timings: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "matches": [m.to_dict() for m in self.matches],
            "rejected": [m.to_dict() for m in self.rejected],
            "candidates_total": self.candidates_total,
            "engines": self.engines,
            "names": self.names,
            "query_public_url": self.query_public_url,
            "query_host": self.query_host,
            "timings": self.timings,
            "errors": self.errors,
        }


def _prioritise(cands: list[Candidate], cap: int) -> list[Candidate]:
    """Dedupe by (canonical url, image) and rank; every field is untrusted, so
    a single malformed candidate must never abort the run."""
    seen: set[tuple[str, str]] = set()
    uniq: list[Candidate] = []
    for c in cands:
        try:
            img = c.image or c.thumbnail
            if not img or not c.link:
                continue
            k = (c.key, img)
            if k in seen:
                continue
            seen.add(k)
            uniq.append(c)
        except Exception:  # noqa: BLE001
            continue

    def rank(c: Candidate):
        try:
            return (bool(c.extra.get("exact")), c.is_post, c.is_social, -c.position)
        except Exception:  # noqa: BLE001
            return (False, False, False, 0)

    uniq.sort(key=rank, reverse=True)
    return uniq[:cap]


def run_search(
    engine: FaceEngine,
    img: np.ndarray,
    face: Face,
    settings: Settings,
    run_dir: Path,
    emit: Emit = lambda *_: None,
    source: str = "upload",
) -> SearchResult:
    t0 = time.time()
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    stats: dict = {}
    timings: dict = {}

    # ---- 1. query images -----------------------------------------------------
    crop = crop_face(img, face, margin=0.6)
    if crop is None or crop.size == 0:  # bbox fully outside the frame
        crop = img
    crop_bytes = encode_jpeg_under(crop)
    full_bytes = encode_jpeg_under(img, max_side=1400)
    (run_dir / "query_face.jpg").write_bytes(crop_bytes)
    emit("search.prepare", {"crop_bytes": len(crop_bytes), "full_bytes": len(full_bytes)})

    public_url = public_host = None
    need_url = settings.serpapi_key and (settings.enable_yandex or settings.enable_google_reverse) and settings.image_host != "none"
    if need_url:
        try:
            public_url, public_host = host.publish_temporary(crop_bytes, "query.jpg", settings.image_host, settings.pinata_jwt, settings.pinata_gateway)
            emit("search.hosted", {"url": public_url, "host": public_host, "ttl": "1h"})
        except Exception as e:  # noqa: BLE001
            errors.append(f"image host: {e}")
            emit("search.warning", {"message": f"Temporary image hosting failed ({e}); Yandex/Google-reverse skipped"})

    # ---- 2. engine fan-out ---------------------------------------------------
    candidates: list[Candidate] = []
    name_hints: list[str] = []
    jobs = []

    def _lens(kind: str, data: bytes):
        # Google Lens is driven by SerpApi's direct image upload. If the upload
        # endpoint is unavailable we fall back to the temporary public URL, so
        # the most important engine has two independent ways to be reached.
        image_id = None
        try:
            image_id = serp.upload_image(data, settings.serpapi_key)
        except Exception as e:  # noqa: BLE001
            if not public_url:
                raise
            emit("search.warning", {"message": f"SerpApi image upload failed ({str(e)[:90]}); using the hosted URL for Google Lens"})
        cands, raw = serp.google_lens(settings.serpapi_key, image_id=image_id, url=public_url, country=settings.search_country, hl=settings.search_lang)
        (raw_dir / f"google_lens_{kind}.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")
        kg = raw.get("knowledge_graph")
        if isinstance(kg, dict) and kg.get("title"):
            name_hints.append(kg["title"])
        elif isinstance(kg, list):
            for k in kg[:2]:
                if isinstance(k, dict) and k.get("title"):
                    name_hints.append(k["title"])
        return f"google_lens_{kind}", cands

    def _yandex():
        cands, raw = serp.yandex_reverse(settings.serpapi_key, public_url)
        (raw_dir / "yandex.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")
        return "yandex", cands

    def _greverse():
        cands, raw = serp.google_reverse_image(settings.serpapi_key, public_url, hl=settings.search_lang)
        (raw_dir / "google_reverse.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")
        return "google_reverse", cands

    def _vision():
        cands, raw, hints = gvision.web_detection(crop_bytes, settings.google_vision_api_key, settings.google_application_credentials)
        (raw_dir / "google_vision.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")
        name_hints.extend(hints)
        return "google_vision", cands

    if settings.serpapi_key:
        jobs.append(("google_lens_face", lambda: _lens("face", crop_bytes)))
        if source == "upload":
            jobs.append(("google_lens_full", lambda: _lens("full", full_bytes)))
        if public_url and settings.enable_yandex:
            jobs.append(("yandex", _yandex))
        if public_url and settings.enable_google_reverse:
            jobs.append(("google_reverse", _greverse))
    else:
        errors.append("SERPAPI_KEY not set - Google Lens / Yandex engines disabled")
    if settings.google_vision_api_key or settings.google_application_credentials:
        jobs.append(("google_vision", _vision))

    emit("search.fanout", {"engines": [j[0] for j in jobs]})
    t1 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        futs = {ex.submit(j[1]): j[0] for j in jobs}
        for fut, name in futs.items():
            try:
                eng_name, cands = fut.result()
                stats[eng_name] = {"candidates": len(cands), "social": sum(c.is_social for c in cands), "posts": sum(c.is_post for c in cands)}
                candidates.extend(cands)
                emit("search.engine", {"engine": eng_name, **stats[eng_name]})
            except Exception as e:  # noqa: BLE001
                stats[name] = {"error": str(e)}
                errors.append(f"{name}: {e}")
                emit("search.engine", {"engine": name, "error": str(e)})
    timings["engines_s"] = round(time.time() - t1, 2)

    # ---- 3. biometric re-verification ----------------------------------------
    shortlist = _prioritise(candidates, settings.max_candidates)
    emit("search.verify.start", {"candidates": len(candidates), "shortlist": len(shortlist)})
    t2 = time.time()
    verified_before = 0

    def _progress(done, total, m):
        payload = {"done": done + verified_before, "total": total + verified_before}
        if m is not None:
            payload.update({"similarity": round(m.similarity, 3), "band": m.band, "platform": m.platform, "link": m.candidate.link, "title": (m.candidate.title or "")[:90], "face_crop_b64": m.face_crop_b64, "engine": m.candidate.engine})
        emit("search.verify.progress", payload)

    verified = verify_all(engine, face.embedding, shortlist, on_progress=_progress, threshold=settings.match_threshold)
    timings["verify_s"] = round(time.time() - t2, 2)

    # ---- 4. identity inference + expansion -----------------------------------
    names = infer_names(verified, name_hints)
    emit("search.identity", {"names": names, "hints": name_hints[:6]})
    if names and settings.enable_name_expansion:
        expansion: list[Candidate] = []
        ex_jobs = []
        if settings.serpapi_key:
            ex_jobs.append(("google_name", lambda: serp.google_social_by_name(settings.serpapi_key, names[0], settings.search_country, settings.search_lang)))
        if settings.enable_bluesky:
            ex_jobs.append(("bluesky", lambda: bluesky.search(names[0])))
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {ex.submit(j[1]): j[0] for j in ex_jobs}
            for fut, name in futs.items():
                try:
                    cands, raw = fut.result()
                    (raw_dir / f"{name}.json").write_text(json.dumps(raw, indent=1, default=str), encoding="utf-8")
                    stats[name] = {"candidates": len(cands), "social": sum(c.is_social for c in cands), "posts": sum(c.is_post for c in cands)}
                    expansion.extend(cands)
                    emit("search.engine", {"engine": name, **stats[name], "query": names[0]})
                except Exception as e:  # noqa: BLE001
                    stats[name] = {"error": str(e)}
                    errors.append(f"{name}: {e}")
                    emit("search.engine", {"engine": name, "error": str(e)})
        known = {c.key for c in shortlist}
        # organic web results usually carry no thumbnail: pull og:image for social post/profile URLs
        bare = [c for c in expansion if c.is_social and not (c.image or c.thumbnail) and c.key not in known][:12]
        if bare:
            with ThreadPoolExecutor(max_workers=6) as ex:
                for c, meta in zip(bare, ex.map(lambda c: fetch_metadata(c.link, timeout=8), bare)):
                    if meta.get("image"):
                        c.image = meta["image"]
                        c.thumbnail = meta["image"]
                        c.extra["og"] = True
                        if meta.get("author"):
                            c.extra["author"] = meta["author"]
        expansion = [c for c in _prioritise(expansion, 40) if c.key not in known]
        if expansion:
            verified_before = len(shortlist)
            emit("search.verify.start", {"candidates": len(expansion), "shortlist": len(expansion), "phase": "expansion"})
            verified.extend(verify_all(engine, face.embedding, expansion, on_progress=_progress, threshold=settings.match_threshold))

    verified = dedupe(verified)
    matches = [m for m in verified if m.similarity >= settings.match_threshold]
    rejected = [m for m in verified if m.similarity < settings.match_threshold]

    # ---- 5. metadata for the top social matches ------------------------------
    top = [m for m in matches if m.candidate.is_social][:8] + [m for m in matches if not m.candidate.is_social][:3]
    t3 = time.time()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_metadata, m.candidate.link): m for m in top}
        for fut, m in futs.items():
            try:
                m.metadata = fut.result()
            except Exception as e:  # noqa: BLE001
                m.metadata = {"error": str(e)}
    timings["metadata_s"] = round(time.time() - t3, 2)
    if not names:
        names = infer_names(matches, name_hints)
    timings["total_s"] = round(time.time() - t0, 2)

    result = SearchResult(matches=matches, rejected=rejected, candidates_total=len(candidates), engines=stats, names=names, query_public_url=public_url, query_host=public_host, timings=timings, errors=errors)
    (run_dir / "search_result.json").write_text(json.dumps(result.to_dict(), indent=1, default=str), encoding="utf-8")
    emit("search.done", {"matches": len(matches), "rejected": len(rejected), "candidates": len(candidates), "names": names, "timings": timings, "top": [m.to_dict() | {"face_crop_b64": ""} for m in matches[:5]]})
    return result
