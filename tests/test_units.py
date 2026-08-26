"""Unit tests for the parts that must never regress: canonical evidence,
Merkle proofs, biometric separation, the SSRF guard and the HTTP surface."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from verified.chain import evidence as ev  # noqa: E402
from verified.chain.ipfs import cid_v1_raw  # noqa: E402
from verified.search import verify as sv  # noqa: E402
from verified.search.types import Candidate, canonical_url, is_post_url, platform_of  # noqa: E402


# ----------------------------------------------------------------- evidence
def test_canonical_is_stable_and_safe():
    a = {"b": 1, "a": {"y": [1, 2], "x": "é"}}
    b = {"a": {"x": "é", "y": [1, 2]}, "b": 1}
    assert ev.canonical_bytes(a) == ev.canonical_bytes(b)
    assert ev.record_hash(a) == ev.record_hash(b)
    # NaN / inf / lone surrogates must not blow up or leak into the hash input
    weird = {"n": float("nan"), "i": float("inf"), "s": "x\ud800y"}
    out = json.loads(ev.canonical_bytes(weird))
    assert out["n"] is None and out["i"] is None and "\ud800" not in out["s"]


def test_dotted_keys_cannot_collide():
    assert ev.bundle_merkle_root({"a.b": 1}) != ev.bundle_merkle_root({"a": {"b": 1}})


def test_merkle_proofs_hold_for_every_leaf():
    for n in range(1, 20):
        bundle = {f"k{i}": {"v": i, "s": f"value {i}"} for i in range(n)}
        keys, leaves = ev.bundle_leaves(bundle)
        root = ev.merkle_root(leaves)
        for i, k in enumerate(keys):
            p = ev.proof_for(bundle, k)
            assert p["root"] == "0x" + root.hex()
            assert ev.verify_proof(leaves[i], [bytes.fromhex(x[2:]) for x in p["proof"]], root)


def test_one_changed_byte_changes_every_commitment():
    bundle = ev.build_bundle(query={"c": "0x1"}, match={"url": "https://x.com/a/status/1", "similarity": 0.61}, search={}, chain={})
    d1 = ev.digest_bundle(bundle)
    bundle["match"]["similarity"] = 0.6101
    d2 = ev.digest_bundle(bundle)
    assert d1["record_hash"] != d2["record_hash"]
    assert d1["merkle_root"] != d2["merkle_root"]


def test_cid_matches_known_vector():
    # `echo -n hello | ipfs add --cid-version 1 --raw-leaves`
    assert cid_v1_raw(b"hello") == "bafkreibm6jg3ux5qumhcn2b3flc3tyu6dmlb4xa7u5bf44yegnrjhc4yeq"


# ------------------------------------------------------------------- search
@pytest.mark.parametrize(
    "url,platform,post",
    [
        ("https://www.instagram.com/p/ABC123/", "Instagram", True),
        ("https://instagram.com/someone", "Instagram", False),
        ("https://twitter.com/a/status/1?s=20", "X (Twitter)", True),
        ("https://www.linkedin.com/posts/x-1", "LinkedIn", True),
        ("https://bsky.app/profile/a.bsky.social/post/3k", "Bluesky", True),
        ("https://example.com/a", None, False),
    ],
)
def test_platform_and_post_detection(url, platform, post):
    assert platform_of(url) == platform
    assert is_post_url(url, platform) is post


def test_canonical_url_strips_tracking_and_mirrors():
    assert canonical_url("http://mobile.twitter.com/a/status/1?utm_source=x&s=20") == "https://x.com/a/status/1"
    assert canonical_url("https://WWW.Instagram.com/p/ABC/") == "https://instagram.com/p/ABC"


def test_candidate_survives_hostile_payloads():
    c = Candidate(engine="e", title=None, link=12345, source={"a": 1}, thumbnail=["x"], image=None, position="not-a-number")
    assert c.title == "" and c.link == "12345" and c.source == "" and c.thumbnail == "" and c.position == 0
    assert c.platform is None and c.is_post is False  # must not raise


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x", "http://localhost/x", "http://[::1]/x", "http://2130706433/",
        "http://[::ffff:127.0.0.1]/x", "http://10.0.0.5/x", "http://192.168.1.1/x",
        "http://169.254.169.254/latest/meta-data/", "http://172.16.4.4/", "file:///etc/passwd",
        "http://foo.internal/x", "http://box.local/x", "http://0.0.0.0/",
    ],
)
def test_ssrf_guard_blocks_private_targets(url):
    assert sv._is_private_host(url) is True


def test_ssrf_guard_allows_public_hosts():
    assert sv._is_private_host("https://pbs.twimg.com/media/x.jpg") is False


# --------------------------------------------------------------------- face
def test_same_person_separates_from_others():
    import cv2

    from verified.face.engine import FaceEngine, cosine

    eng = FaceEngine()
    emb = {}
    for name in ("obama.jpg", "obama2.jpg", "biden.jpg"):
        img = cv2.imread(str(ROOT / "samples" / name))
        f = eng.primary_face(img)
        assert f is not None, name
        emb[name] = f.embedding
    same = cosine(emb["obama.jpg"], emb["obama2.jpg"])
    diff = cosine(emb["obama.jpg"], emb["biden.jpg"])
    assert same > 0.6, same
    assert diff < 0.3, diff
    assert same - diff > 0.4


def test_face_commitment_is_deterministic_and_salted():
    import numpy as np

    from verified.face.engine import face_commitment

    e = np.random.RandomState(0).randn(512).astype("float32")
    e /= np.linalg.norm(e)
    assert face_commitment(e, b"salt-a") == face_commitment(e, b"salt-a")
    assert face_commitment(e, b"salt-a") != face_commitment(e, b"salt-b")
    assert len(face_commitment(e, b"salt-a")) == 66


# --------------------------------------------------------------------- http
def test_http_surface_rejects_traversal_and_cross_origin(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("RPC_URL", "tester")
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "11" * 32)
    for m in [m for m in list(sys.modules) if m.startswith("verified")]:
        del sys.modules[m]
    from verified.server import app

    with TestClient(app) as client:
        for bad in ["..", "xxx", "20260907-084512-ZZZZZZ", "C:\\Users"]:
            assert client.get(f"/api/run/{bad}").status_code in (400, 404)
        assert client.get("/api/run/20260101-000000-abcdef/file/.env").status_code == 404
        assert client.get("/api/run/20260101-000000-abcdef/file/..%2F.env").status_code == 404
        # CSRF: a cross-origin POST must be refused
        r = client.post("/api/verify/20260101-000000-abcdef", headers={"Origin": "https://evil.example"}, data={})
        assert r.status_code == 403
        assert client.get("/").status_code == 200
