"""IPFS pinning via Pinata (optional) + local CIDv1 computation.

Even without a Pinata key we compute the CIDv1 (raw-leaves, sha2-256, dag-pb
single block) of the canonical bundle bytes so the identifier stored on-chain
is content-derived and can be pinned later by anyone holding the bundle.
"""
from __future__ import annotations

import hashlib
import json

import httpx

PINATA_V3 = "https://uploads.pinata.cloud/v3/files"
PINATA_LEGACY = "https://api.pinata.cloud/pinning/pinFileToIPFS"

_B32 = "abcdefghijklmnopqrstuvwxyz234567"


def _b32_lower(data: bytes) -> str:
    bits = 0
    value = 0
    out = []
    for b in data:
        value = (value << 8) | b
        bits += 8
        while bits >= 5:
            out.append(_B32[(value >> (bits - 5)) & 31])
            bits -= 5
    if bits:
        out.append(_B32[(value << (5 - bits)) & 31])
    return "".join(out)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def cid_v1_raw(data: bytes) -> str:
    """CIDv1, codec raw (0x55), multihash sha2-256 - what `ipfs add --cid-version 1 --raw-leaves`
    yields for a block-sized file (<= 256 KiB)."""
    mh = b"\x12\x20" + hashlib.sha256(data).digest()
    return "b" + _b32_lower(b"\x01" + _varint(0x55) + mh)


def pin_bytes(data: bytes, filename: str, jwt: str, content_type: str = "application/json", name: str | None = None) -> str:
    """Pin bytes to Pinata; returns the CID reported by Pinata."""
    headers = {"Authorization": f"Bearer {jwt}"}
    try:
        r = httpx.post(PINATA_V3, headers=headers, data={"network": "public", "name": name or filename}, files={"file": (filename, data, content_type)}, timeout=90)
        if r.status_code in (200, 201):
            body = r.json()
            cid = (body.get("data") or {}).get("cid") or body.get("cid")
            if cid:
                return cid
        err_v3 = f"v3 HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:  # noqa: BLE001
        err_v3 = f"v3 {type(e).__name__}: {e}"
    r = httpx.post(PINATA_LEGACY, headers=headers, data={"pinataMetadata": json.dumps({"name": name or filename})}, files={"file": (filename, data, content_type)}, timeout=90)
    if r.status_code == 200 and r.json().get("IpfsHash"):
        return r.json()["IpfsHash"]
    raise RuntimeError(f"Pinata pin failed ({err_v3}; legacy HTTP {r.status_code}: {r.text[:200]})")


def fetch_ipfs(cid: str, gateways: list[str] | None = None, timeout: float = 30) -> bytes:
    gateways = gateways or ["https://gateway.pinata.cloud/ipfs", "https://ipfs.io/ipfs", "https://cloudflare-ipfs.com/ipfs", "https://dweb.link/ipfs"]
    errors = []
    for g in gateways:
        try:
            r = httpx.get(f"{g.rstrip('/')}/{cid}", timeout=timeout, follow_redirects=True)
            if r.status_code == 200:
                return r.content
            errors.append(f"{g}: HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{g}: {type(e).__name__}")
    raise RuntimeError("IPFS fetch failed: " + "; ".join(errors))
