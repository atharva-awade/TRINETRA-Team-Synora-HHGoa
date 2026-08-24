"""Evidence bundle: canonical serialisation, hashing and Merkle proofs.

The bundle is the single source of truth that gets anchored.  Its canonical
form is JSON with sorted keys and no insignificant whitespace, so anyone can
re-derive the exact same bytes (and therefore hashes) from the stored file.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from eth_utils import keccak

BUNDLE_VERSION = "1.0"


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def record_hash(bundle: dict) -> str:
    return "0x" + keccak(canonical_bytes(bundle)).hex()


def sha256_hex(data: bytes) -> str:
    return "0x" + hashlib.sha256(data).hexdigest()


def flatten(obj: Any, prefix: str = "") -> dict[str, str]:
    """Flatten nested JSON into dotted-key -> canonical-string leaves."""
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k in sorted(obj):
            out.update(flatten(obj[k], f"{prefix}{k}."))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}{i}."))
    else:
        out[prefix[:-1]] = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return out


def leaf_hash(key: str, value: str) -> bytes:
    return keccak(f"{key}={value}".encode("utf-8"))


def _pair(a: bytes, b: bytes) -> bytes:
    return keccak(a + b) if a < b else keccak(b + a)


def merkle_root(leaves: list[bytes]) -> bytes:
    if not leaves:
        return b"\x00" * 32
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_proof(leaves: list[bytes], index: int) -> list[bytes]:
    proof: list[bytes] = []
    level = list(leaves)
    idx = index
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        sibling = idx ^ 1
        proof.append(level[sibling])
        level = [_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        idx //= 2
    return proof


def verify_proof(leaf: bytes, proof: list[bytes], root: bytes) -> bool:
    h = leaf
    for p in proof:
        h = _pair(h, p)
    return h == root


def bundle_leaves(bundle: dict) -> tuple[list[str], list[bytes]]:
    flat = flatten(bundle)
    keys = list(flat)
    return keys, [leaf_hash(k, flat[k]) for k in keys]


def bundle_merkle_root(bundle: dict) -> str:
    _, leaves = bundle_leaves(bundle)
    return "0x" + merkle_root(leaves).hex()


def proof_for(bundle: dict, key: str) -> dict:
    keys, leaves = bundle_leaves(bundle)
    i = keys.index(key)
    flat = flatten(bundle)
    return {
        "key": key,
        "value": flat[key],
        "leaf": "0x" + leaves[i].hex(),
        "proof": ["0x" + p.hex() for p in merkle_proof(leaves, i)],
        "root": "0x" + merkle_root(leaves).hex(),
    }


def build_bundle(*, query: dict, match: dict, search: dict, chain: dict, created_at: str | None = None) -> dict:
    return {
        "version": BUNDLE_VERSION,
        "pipeline": "verified-face-chain",
        "created_at": created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "query": query,
        "match": match,
        "search": search,
        "chain": chain,
    }


def digest_bundle(bundle: dict) -> dict:
    """All hashes an anchor needs, derived from the bundle alone."""
    return {
        "record_hash": record_hash(bundle),
        "merkle_root": bundle_merkle_root(bundle),
        "canonical_sha256": sha256_hex(canonical_bytes(bundle)),
        "canonical_size": len(canonical_bytes(bundle)),
        "leaf_count": len(flatten(bundle)),
    }
