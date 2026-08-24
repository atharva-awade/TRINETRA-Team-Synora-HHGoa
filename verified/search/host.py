"""Ephemeral public hosting for the *query face crop*.

Reverse-image engines other than Google Lens (which accepts direct uploads
through SerpApi's Image API) need a publicly reachable URL.  We publish only a
tight crop of the face, to a short-lived host (1 hour by default) and record
which host / URL was used so the run is auditable.
"""
from __future__ import annotations

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 verified-pipeline/1.0"


class HostError(RuntimeError):
    pass


def _litterbox(data: bytes, filename: str) -> str:
    r = httpx.post(
        "https://litterbox.catbox.moe/resources/internals/api.php",
        data={"reqtype": "fileupload", "time": "1h"},
        files={"fileToUpload": (filename, data, "image/jpeg")},
        headers={"User-Agent": UA},
        timeout=60,
    )
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("http"):
        raise HostError(f"litterbox: unexpected response {url[:120]}")
    return url


def _tmpfiles(data: bytes, filename: str) -> str:
    r = httpx.post(
        "https://tmpfiles.org/api/v1/upload",
        files={"file": (filename, data, "image/jpeg")},
        headers={"User-Agent": UA},
        timeout=60,
    )
    r.raise_for_status()
    url = r.json()["data"]["url"]
    # direct-download form: https://tmpfiles.org/dl/<id>/<name>
    return url.replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/", 1)


def _0x0(data: bytes, filename: str) -> str:
    r = httpx.post(
        "https://0x0.st",
        data={"expires": "1"},
        files={"file": (filename, data, "image/jpeg")},
        headers={"User-Agent": UA},
        timeout=60,
    )
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("http"):
        raise HostError(f"0x0.st: unexpected response {url[:120]}")
    return url


def _pinata(data: bytes, filename: str, jwt: str, gateway: str) -> str:
    from ..chain.ipfs import pin_bytes

    cid = pin_bytes(data, filename, jwt, content_type="image/jpeg")
    return f"{gateway.rstrip('/')}/{cid}"


def publish_temporary(data: bytes, filename: str = "query.jpg", preference: str = "auto", pinata_jwt: str = "", pinata_gateway: str = "") -> tuple[str, str]:
    """Return (public_url, host_name). Tries hosts in order of preference."""
    order = {
        "auto": ["litterbox", "tmpfiles", "0x0", "pinata"],
        "litterbox": ["litterbox", "tmpfiles", "0x0"],
        "tmpfiles": ["tmpfiles", "litterbox", "0x0"],
        "0x0": ["0x0", "litterbox", "tmpfiles"],
        "pinata": ["pinata", "litterbox", "tmpfiles"],
        "none": [],
    }[preference]
    errors = []
    for name in order:
        try:
            if name == "litterbox":
                return _litterbox(data, filename), name
            if name == "tmpfiles":
                return _tmpfiles(data, filename), name
            if name == "0x0":
                return _0x0(data, filename), name
            if name == "pinata" and pinata_jwt:
                return _pinata(data, filename, pinata_jwt, pinata_gateway), name
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {e}")
    raise HostError("no image host available: " + "; ".join(errors))
