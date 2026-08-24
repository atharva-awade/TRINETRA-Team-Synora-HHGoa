"""Google Cloud Vision - Web Detection engine.

Accepts raw image bytes (no public URL needed) and returns pages that embed
the same / a partially matching image, visually similar images, and *web
entities* - which is the most reliable machine-readable hint of the person's
name for the name-expansion step.
"""
from __future__ import annotations

import base64
import json
import os

import httpx

from ..types import Candidate

ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"


def _auth(api_key: str, sa_path: str) -> tuple[dict, dict]:
    """Return (params, headers) for either API-key or service-account auth."""
    if api_key:
        return {"key": api_key}, {}
    if sa_path and os.path.exists(sa_path):
        try:
            from google.oauth2 import service_account  # type: ignore
            import google.auth.transport.requests  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pip install google-auth to use a service account JSON") from e
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        return {}, {"Authorization": f"Bearer {creds.token}"}
    raise RuntimeError("Google Vision: set GOOGLE_VISION_API_KEY or GOOGLE_APPLICATION_CREDENTIALS")


def web_detection(image_bytes: bytes, api_key: str = "", sa_path: str = "", max_results: int = 50) -> tuple[list[Candidate], dict, list[str]]:
    params, headers = _auth(api_key, sa_path)
    body = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode()},
                "features": [{"type": "WEB_DETECTION", "maxResults": max_results}],
                "imageContext": {"webDetectionParams": {"includeGeoResults": False}},
            }
        ]
    }
    r = httpx.post(ENDPOINT, params=params, headers=headers, content=json.dumps(body), timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Google Vision HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    resp = (data.get("responses") or [{}])[0]
    if "error" in resp:
        raise RuntimeError(f"Google Vision: {resp['error']}")
    wd = resp.get("webDetection", {})
    out: list[Candidate] = []
    for i, p in enumerate(wd.get("pagesWithMatchingImages", []) or []):
        imgs = [m.get("url") for m in (p.get("fullMatchingImages") or []) + (p.get("partialMatchingImages") or []) if m.get("url")]
        out.append(
            Candidate(
                engine="gvision_pages",
                title=p.get("pageTitle", "") or "",
                link=p.get("url", ""),
                source="",
                thumbnail=imgs[0] if imgs else "",
                image=imgs[0] if imgs else "",
                position=i + 1,
                extra={"full": bool(p.get("fullMatchingImages")), "all_images": imgs[:5]},
            )
        )
    for i, m in enumerate(wd.get("fullMatchingImages", []) or []):
        if m.get("url"):
            out.append(Candidate(engine="gvision_full", title="Full matching image", link=m["url"], image=m["url"], thumbnail=m["url"], position=i + 1))
    for i, m in enumerate(wd.get("partialMatchingImages", []) or []):
        if m.get("url"):
            out.append(Candidate(engine="gvision_partial", title="Partial matching image", link=m["url"], image=m["url"], thumbnail=m["url"], position=i + 1))
    for i, m in enumerate(wd.get("visuallySimilarImages", []) or []):
        if m.get("url"):
            out.append(Candidate(engine="gvision_similar", title="Visually similar image", link=m["url"], image=m["url"], thumbnail=m["url"], position=i + 1))
    entities = [e.get("description", "") for e in sorted(wd.get("webEntities", []) or [], key=lambda e: -float(e.get("score", 0))) if e.get("description")]
    labels = [l.get("label", "") for l in wd.get("bestGuessLabels", []) or [] if l.get("label")]
    return out, data, labels + entities
