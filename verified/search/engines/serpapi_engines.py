"""SerpApi-backed engines: Google Lens, Google Reverse Image, Yandex Images,
and a name-expansion Google web search restricted to social networks.

Every raw response is persisted by the caller for auditability.
"""
from __future__ import annotations

import os

import httpx

from ..types import Candidate

# Base URL is overridable so the integration test-suite can point the engines at a
# local fixture server (see tests/); production always talks to serpapi.com.
SERPAPI_BASE = os.environ.get("SERPAPI_BASE", "https://serpapi.com").rstrip("/")
SERPAPI = f"{SERPAPI_BASE}/search.json"
SERPAPI_IMAGE = f"{SERPAPI_BASE}/image"


class SerpApiError(RuntimeError):
    pass


def _get(params: dict, api_key: str, timeout: float = 90) -> dict:
    params = {**params, "api_key": api_key, "output": "json"}
    r = httpx.get(SERPAPI, params=params, timeout=timeout)
    if r.status_code != 200:
        try:
            msg = r.json().get("error", r.text[:300])
        except Exception:  # noqa: BLE001
            msg = r.text[:300]
        raise SerpApiError(f"SerpApi HTTP {r.status_code}: {msg}")
    data = r.json()
    if "error" in data and not any(k in data for k in ("visual_matches", "images_results", "image_results", "organic_results", "exact_matches")):
        if "hasn't returned any results" in str(data["error"]).lower() or "no results" in str(data["error"]).lower():
            return data  # legitimate empty result set
        raise SerpApiError(f"SerpApi: {data['error']}")
    return data


def upload_image(image_bytes: bytes, api_key: str) -> str:
    """Upload the query image (<=500 KB) and get a short-lived image_id."""
    r = httpx.post(SERPAPI_IMAGE, data={"api_key": api_key}, files={"image": ("query.jpg", image_bytes, "image/jpeg")}, timeout=60)
    if r.status_code != 200:
        raise SerpApiError(f"SerpApi image upload HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if "image_id" not in data:
        raise SerpApiError(f"SerpApi image upload failed: {data}")
    return data["image_id"]


# ----------------------------------------------------------------- Google Lens
def google_lens(api_key: str, *, image_id: str | None = None, url: str | None = None, search_type: str = "all", country: str = "in", hl: str = "en") -> tuple[list[Candidate], dict]:
    params = {"engine": "google_lens", "type": search_type, "hl": hl, "country": country, "no_cache": "true"}
    if image_id:
        params["image_id"] = image_id
    elif url:
        params["url"] = url
    else:
        raise ValueError("google_lens needs image_id or url")
    data = _get(params, api_key)
    out: list[Candidate] = []
    for section in ("exact_matches", "visual_matches"):
        for i, m in enumerate(data.get(section, []) or []):
            link = m.get("link") or ""
            if not link:
                continue
            out.append(
                Candidate(
                    engine="google_lens" if section == "visual_matches" else "google_lens_exact",
                    title=m.get("title", "") or "",
                    link=link,
                    source=m.get("source", "") or "",
                    thumbnail=m.get("thumbnail", "") or "",
                    image=m.get("image", "") or "",
                    position=int(m.get("position", i + 1)),
                    extra={"exact": bool(m.get("exact_matches")) or section == "exact_matches", "image_width": m.get("image_width"), "image_height": m.get("image_height")},
                )
            )
    for i, m in enumerate(data.get("organic_results", []) or []):
        link = m.get("link") or ""
        if link:
            out.append(Candidate(engine="google_lens_organic", title=m.get("title", ""), link=link, source=m.get("source", "") or m.get("displayed_link", ""), thumbnail=m.get("thumbnail", "") or "", snippet=m.get("snippet", ""), position=i + 1))
    return out, data


# -------------------------------------------------------- Google Reverse Image
def google_reverse_image(api_key: str, image_url: str, hl: str = "en") -> tuple[list[Candidate], dict]:
    data = _get({"engine": "google_reverse_image", "image_url": image_url, "hl": hl, "no_cache": "true"}, api_key)
    out: list[Candidate] = []
    for i, m in enumerate(data.get("image_results", []) or []):
        link = m.get("link") or ""
        if link:
            out.append(Candidate(engine="google_reverse", title=m.get("title", ""), link=link, source=m.get("displayed_link", "") or m.get("source", ""), thumbnail=m.get("thumbnail", "") or "", snippet=m.get("snippet", ""), position=int(m.get("position", i + 1))))
    for i, m in enumerate(data.get("inline_images", []) or []):
        link = m.get("link") or m.get("source") or ""
        if link:
            out.append(Candidate(engine="google_reverse_inline", title=m.get("title", "") or m.get("source_name", ""), link=link, source=m.get("source", ""), thumbnail=m.get("thumbnail", "") or "", image=m.get("original", "") or "", position=i + 1))
    return out, data


# ---------------------------------------------------------------------- Yandex
def yandex_reverse(api_key: str, image_url: str) -> tuple[list[Candidate], dict]:
    data = _get({"engine": "yandex_images", "url": image_url, "no_cache": "true"}, api_key)
    out: list[Candidate] = []
    for i, m in enumerate(data.get("images_results", []) or []):
        link = m.get("link") or ""
        if not link:
            continue
        out.append(
            Candidate(
                engine="yandex",
                title=m.get("title", "") or "",
                link=link,
                source=m.get("source", "") or "",
                thumbnail=m.get("thumbnail", "") or "",
                image=m.get("original", "") or "",
                snippet=m.get("snippet", "") or "",
                posted_at=m.get("posted_at", "") or "",
                position=int(m.get("position", i + 1)),
            )
        )
    return out, data


# ------------------------------------------------------- name expansion search
SOCIAL_SITES = ["instagram.com", "x.com", "twitter.com", "linkedin.com", "facebook.com", "threads.net", "tiktok.com", "youtube.com"]


def google_social_by_name(api_key: str, name: str, country: str = "in", hl: str = "en", num: int = 20) -> tuple[list[Candidate], dict]:
    q = f'"{name}" (' + " OR ".join(f"site:{s}" for s in SOCIAL_SITES) + ")"
    data = _get({"engine": "google", "q": q, "num": num, "hl": hl, "gl": country, "no_cache": "true"}, api_key)
    out: list[Candidate] = []
    for i, m in enumerate(data.get("organic_results", []) or []):
        link = m.get("link") or ""
        if link:
            out.append(Candidate(engine="google_name", title=m.get("title", ""), link=link, source=m.get("displayed_link", "") or m.get("source", ""), thumbnail=m.get("thumbnail", "") or "", snippet=m.get("snippet", ""), posted_at=m.get("date", "") or "", position=int(m.get("position", i + 1)), extra={"query": q}))
    for i, m in enumerate(data.get("inline_images", []) or []):
        link = m.get("link") or m.get("source") or ""
        if link:
            out.append(Candidate(engine="google_name_images", title=m.get("title", "") or "", link=link, source=m.get("source", ""), thumbnail=m.get("thumbnail", "") or "", image=m.get("original", "") or "", position=i + 1, extra={"query": q}))
    return out, data
