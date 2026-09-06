"""End-to-end orchestration:  face scan -> genuine search -> evidence bundle ->
on-chain anchor (+ EAS, IPFS, OpenTimestamps) -> independent re-verification.

The pipeline is two-phase so a UI can let the operator pick which verified
match to anchor:  `scan()` (phases 1-2) and `anchor()` (phases 3-7).
`run()` chains both with automatic best-match selection.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .chain import evidence as ev
from .chain import ipfs as ipfs_mod
from .chain import ots as ots_mod
from .chain.registry import ChainError, Registry
from .config import ROOT, Settings
from .face.engine import FaceEngine, cosine, crop_face, draw_faces, face_commitment
from .search.pipeline import run_search
from .search.types import canonical_url
from .search.verify import fetch_image

Emit = Callable[[str, dict], None]


def _b64(img: np.ndarray, max_side: int = 480, q: int = 85) -> str:
    if img is None or img.size == 0:
        return ""
    h, w = img.shape[:2]
    s = min(1.0, max_side / max(1, max(h, w)))
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode() if ok else ""


def ensure_salt(settings: Settings) -> bytes:
    """Per-installation HMAC salt for face commitments (persisted to .env)."""
    if settings.commitment_salt:
        return bytes.fromhex(settings.commitment_salt[2:] if settings.commitment_salt.startswith("0x") else settings.commitment_salt)
    salt = secrets.token_bytes(32)
    _persist_env("COMMITMENT_SALT", "0x" + salt.hex())
    settings.commitment_salt = "0x" + salt.hex()
    return salt


class Pipeline:
    def __init__(self, settings: Settings | None = None, engine: FaceEngine | None = None, emit: Emit | None = None):
        self.s = settings or Settings()
        self.engine = engine or FaceEngine()
        self.emit = emit or (lambda *_: None)
        self.s.runs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ utils
    def _run_dir(self, run_id: str) -> Path:
        d = self.s.runs_dir / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def new_run_id() -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    # ---------------------------------------------------------------- phase 1
    def scan(self, image_bytes: bytes, source: str = "upload", run_id: str | None = None, face_index: int = 0, target_url: str = "", name_hint: str = "") -> dict:
        run_id = run_id or self.new_run_id()
        run_dir = self._run_dir(run_id)
        t0 = time.time()
        self.emit("run.start", {"run_id": run_id, "source": source})

        # --- face -------------------------------------------------------------
        self.emit("stage", {"stage": "face", "status": "running"})
        img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("could not decode image")
        (run_dir / "query.jpg").write_bytes(image_bytes)
        faces = self.engine.analyze(img)
        if not faces:
            self.emit("stage", {"stage": "face", "status": "failed", "message": "No face detected"})
            summary = {"run_id": run_id, "status": "no_face", "faces": 0}
            (run_dir / "run.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
            return summary
        faces.sort(key=lambda f: (f.width * f.height, f.score), reverse=True)
        face_idx = min(len(faces) - 1, max(0, face_index))
        face = faces[face_idx]
        try:
            self.engine.landmarks(img, face)
        except Exception:  # noqa: BLE001
            pass
        salt = ensure_salt(self.s)
        commitment = face_commitment(face.embedding, salt)
        face_info = {
            **face.to_dict(),
            "faces_in_frame": len(faces),
            "selected_index": face_idx,
            "all_faces": [f.to_dict() for f in faces],
            "embedding_dim": int(face.embedding.shape[0]),
            "embedding_norm": round(float(np.linalg.norm(face.embedding)), 4),
            "commitment": commitment,
            "detector": "SCRFD-10GF (InsightFace buffalo_l)",
            "recognizer": "ArcFace ResNet-50 @ WebFace600K (512-d)",
            "source": source,
        }
        np.save(run_dir / "query_embedding.npy", face.embedding)
        (run_dir / "face.json").write_text(json.dumps(face_info, indent=1), encoding="utf-8")
        _write_jpeg(run_dir / "query_annotated.jpg", draw_faces(img, faces, selected_index=face_idx))
        initial_summary = {
            "run_id": run_id,
            "status": "searching",
            "source": source,
            "face": face_info,
            "stage": "search",
        }
        _atomic_write(run_dir / "run.json", json.dumps(initial_summary, indent=1, default=str).encode("utf-8"))
        self.emit(
            "face.detected",
            {
                **face_info,
                "face_crop_b64": _b64(crop_face(img, face, 0.4), 240),
                "annotated_b64": _b64(draw_faces(img, faces, selected_index=face_idx), 640),
                "image_size": [img.shape[1], img.shape[0]],
                "landmarks106": face.landmarks106.round(1).tolist() if face.landmarks106 is not None else None,
                "elapsed_s": round(time.time() - t0, 2),
            },
        )
        self.emit("stage", {"stage": "face", "status": "done"})

        # --- search -----------------------------------------------------------
        self.emit("stage", {"stage": "search", "status": "running"})
        result = run_search(self.engine, img, face, self.s, run_dir, self.emit, source=source, target_url=target_url, name_hint=name_hint)
        status = "matches" if result.matches else "no_match"
        self.emit("stage", {"stage": "search", "status": "done" if result.matches else "failed", "matches": len(result.matches)})
        summary = {
            "run_id": run_id,
            "status": status,
            "source": source,
            "face": face_info,
            "search": result.to_dict(),
            "elapsed_s": round(time.time() - t0, 2),
        }
        _atomic_write(run_dir / "run.json", json.dumps(summary, indent=1, default=str).encode("utf-8"))
        self.emit("scan.done", {"run_id": run_id, "status": status, "matches": [{k: v for k, v in m.items()} for m in summary["search"]["matches"]], "names": result.names})
        return summary

    # ---------------------------------------------------------------- phase 2
    @staticmethod
    def pick_best(matches: list[dict]) -> int | None:
        if not matches:
            return None

        def score(m):
            return m["similarity"] + (0.06 if m["is_post"] else 0) + (0.05 if m["is_social"] else 0) + (0.02 if "google_lens_exact" in m.get("engines", []) else 0)

        return max(range(len(matches)), key=lambda i: score(matches[i]))

    def anchor(self, run_id: str, match_index: int | None = None) -> dict:
        run_dir = self._run_dir(run_id)
        summary = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        if (run_dir / "anchor.json").exists():
            raise RuntimeError(f"run {run_id} is already anchored (see anchor.json) - refusing to write a duplicate record")
        matches = summary.get("search", {}).get("matches", [])
        if not matches:
            rejected = summary.get("search", {}).get("rejected", [])
            if match_index is not None and 0 <= match_index < len(rejected):
                matches = [rejected[match_index]]
                match_index = 0
            elif rejected and rejected[0].get("similarity", 0) >= 0.30:
                matches = [rejected[0]]
                match_index = 0
            else:
                raise RuntimeError("no verified matches to anchor")
        idx = self.pick_best(matches) if match_index is None else match_index
        if idx is None or not 0 <= idx < len(matches):
            raise RuntimeError(f"match index {match_index} out of range (0..{len(matches) - 1})")
        m = matches[idx]
        self.emit("anchor.selected", {"index": idx, "match": {k: v for k, v in m.items() if k != "face_crop_b64"}})

        # --- evidence bundle --------------------------------------------------
        self.emit("stage", {"stage": "evidence", "status": "running"})
        img_bytes, hashed_url = None, m["image_used"]
        for cand_url in dict.fromkeys([m.get("image") or "", m["image_used"], m.get("thumbnail") or ""]):
            if cand_url:
                img_bytes = fetch_image(cand_url)
                if img_bytes:
                    hashed_url = cand_url
                    break
        if img_bytes:
            content_hash = ev.sha256_hex(img_bytes)
            ext = ".png" if img_bytes[:8] == b"\x89PNG\r\n\x1a\n" else ".webp" if img_bytes[8:12] == b"WEBP" else ".jpg"
            (run_dir / f"match_image{ext}").write_bytes(img_bytes)
            content_hash_source = "refetched"
        else:
            content_hash = "0x" + m["image_sha256"]
            content_hash_source = "verification-time"
        face_json = summary["face"]
        log = lambda msg: self.emit("chain.log", {"message": msg})  # noqa: E731
        reg = Registry(self.s, log=log)
        bal = reg.balance_eth()
        self.emit("chain.wallet", {"address": reg.address, "balance_eth": round(bal, 6), "chain": self.s.chain_name, "rpc": reg.endpoint})
        need = 0.004 if self.s.contract_address else 0.02  # deploy costs an order of magnitude more
        if bal < need:
            raise ChainError(
                f"wallet {reg.address} holds {bal:.5f} ETH on {self.s.chain_name}; about {need} ETH is needed for this run "
                f"({'anchor' if self.s.contract_address else 'deploy + anchor'}"
                f"{' + EAS attestation' if self.s.enable_eas else ''}). Top it up from a faucet and retry."
            )
        deployed_now = reg.contract is None
        reg.ensure_deployed()
        if deployed_now:
            self.s.contract_address = reg.contract.address
            _persist_env("CONTRACT_ADDRESS", reg.contract.address)
            self.emit("chain.deployed", {"contract": reg.contract.address, "explorer": reg.explorer_address(reg.contract.address)})
        bundle = ev.build_bundle(
            query={
                "face_commitment": face_json["commitment"],
                "commitment_scheme": "HMAC-SHA256(salt, int8(ArcFace-512))",
                "detector": face_json["detector"],
                "recognizer": face_json["recognizer"],
                "quality": face_json["quality"],
                "faces_in_frame": face_json["faces_in_frame"],
                "source": summary["source"],
                "run_id": run_id,
            },
            match={
                "url": m["canonical_link"],
                "original_url": m["link"],
                "platform": m["platform"],
                "is_post": m["is_post"],
                "title": m["title"],
                "author": m["metadata"].get("author", ""),
                "text": (m["metadata"].get("text") or m["snippet"] or "")[:600],
                "posted_at": m["metadata"].get("published") or m["posted_at"] or "",
                "image_url": hashed_url,
                "image_urls": [u for u in dict.fromkeys([m.get("image") or "", m["image_used"], m.get("thumbnail") or ""]) if u],
                "image_sha256": content_hash,
                "image_sha256_source": content_hash_source,
                "similarity": round(m["similarity"], 4),
                "band": m["band"],
                "engines": m["engines"],
                "candidate_faces": m["candidate_faces"],
                "metadata_method": m["metadata"].get("method", ""),
            },
            search={
                "engines": summary["search"]["engines"],
                "candidates_total": summary["search"]["candidates_total"],
                "verified_matches": len(matches),
                "rejected": len(summary["search"]["rejected"]),
                "match_threshold": self.s.match_threshold,
                "inferred_names": summary["search"]["names"],
                "query_host": summary["search"].get("query_host"),
            },
            chain={"name": self.s.chain_name, "chain_id": self.s.chain_id, "registry": reg.contract.address},
        )
        digest = ev.digest_bundle(bundle)
        canonical = ev.canonical_bytes(bundle)
        (run_dir / "bundle.json").write_bytes(canonical)
        (run_dir / "bundle.pretty.json").write_text(json.dumps(json.loads(canonical.decode("utf-8")), indent=1, ensure_ascii=False), encoding="utf-8")
        self.emit("evidence.built", {**digest, "bundle": bundle})
        self.emit("stage", {"stage": "evidence", "status": "done"})

        # --- IPFS ---------------------------------------------------------------
        self.emit("stage", {"stage": "ipfs", "status": "running"})
        local_cid = ipfs_mod.cid_v1_raw(canonical)
        ipfs_info = {"cid_local": local_cid, "pinned": False}
        if self.s.pinata_jwt:
            try:
                cid = ipfs_mod.pin_bytes(canonical, f"verified-{run_id}.json", self.s.pinata_jwt, name=f"verified-{run_id}")
                ipfs_info.update({"cid": cid, "pinned": True, "gateway_url": f"{self.s.pinata_gateway.rstrip('/')}/{cid}", "provider": "Pinata"})
            except Exception as e:  # noqa: BLE001
                ipfs_info["error"] = str(e)
        evidence_cid = ipfs_info.get("cid") or local_cid
        ipfs_info["cid_on_chain"] = evidence_cid
        self.emit("ipfs.done", ipfs_info)
        self.emit("stage", {"stage": "ipfs", "status": "done" if ipfs_info["pinned"] else "skipped", "message": None if ipfs_info["pinned"] else "CID computed locally (no PINATA_JWT)"})

        # --- on-chain anchor ----------------------------------------------------
        self.emit("stage", {"stage": "chain", "status": "running"})
        receipt = reg.anchor(
            record_hash=digest["record_hash"],
            face_commitment=face_json["commitment"],
            content_hash=content_hash,
            merkle_root=digest["merkle_root"],
            uri=m["canonical_link"],
            platform=m["platform"],
            evidence_cid=evidence_cid,
            similarity=m["similarity"],
        )
        receipt["record_hash"] = digest["record_hash"]
        receipt["merkle_root"] = digest["merkle_root"]
        receipt["content_hash"] = content_hash
        receipt["face_commitment"] = face_json["commitment"]
        receipt["evidence_cid"] = evidence_cid
        (run_dir / "anchor.json").write_text(json.dumps(receipt, indent=1), encoding="utf-8")
        summary.update({"status": "anchored", "anchor": receipt, "ipfs": ipfs_info, "selected_match": idx, "bundle_digest": digest})
        _atomic_write(run_dir / "run.json", json.dumps(summary, indent=1, default=str).encode("utf-8"))
        self.emit("chain.anchored", receipt)
        self.emit("stage", {"stage": "chain", "status": "done"})

        # --- EAS attestation --------------------------------------------------
        eas_info: dict = {"enabled": bool(self.s.enable_eas and self.s.eas_contract)}
        if eas_info["enabled"]:
            self.emit("stage", {"stage": "eas", "status": "running"})
            try:
                from .chain.eas import EAS

                eas = EAS(reg, self.s.eas_contract, self.s.eas_schema_registry, self.s.eas_explorer)
                eas_info.update(
                    eas.attest(
                        record_hash=digest["record_hash"],
                        face_commitment=face_json["commitment"],
                        content_hash=content_hash,
                        merkle_root=digest["merkle_root"],
                        uri=m["canonical_link"],
                        platform=m["platform"],
                        similarity=m["similarity"],
                        evidence_cid=evidence_cid,
                        registry=reg.contract.address,
                        record_id=receipt["record_id"],
                        log=log,
                    )
                )
                self.emit("stage", {"stage": "eas", "status": "done"})
            except Exception as e:  # noqa: BLE001
                eas_info["error"] = str(e)
                self.emit("stage", {"stage": "eas", "status": "failed", "message": str(e)[:200]})
        else:
            self.emit("stage", {"stage": "eas", "status": "skipped"})
        (run_dir / "eas.json").write_text(json.dumps(eas_info, indent=1), encoding="utf-8")
        self.emit("eas.done", eas_info)

        # --- OpenTimestamps (Bitcoin) -------------------------------------------
        ots_info: dict = {"enabled": bool(self.s.enable_ots)}
        if self.s.enable_ots:
            self.emit("stage", {"stage": "ots", "status": "running"})
            try:
                (run_dir / "ots").mkdir(exist_ok=True)
                ots_info.update(ots_mod.stamp(canonical, run_dir / "ots" / "bundle.json.ots"))
                self.emit("stage", {"stage": "ots", "status": "done"})
            except Exception as e:  # noqa: BLE001
                ots_info["error"] = str(e)
                self.emit("stage", {"stage": "ots", "status": "failed", "message": str(e)[:200]})
        else:
            self.emit("stage", {"stage": "ots", "status": "skipped"})
        (run_dir / "ots.json").write_text(json.dumps(ots_info, indent=1), encoding="utf-8")
        self.emit("ots.done", ots_info)

        summary.update({"eas": eas_info, "ots": ots_info})
        _atomic_write(run_dir / "run.json", json.dumps(summary, indent=1, default=str).encode("utf-8"))

        # --- immediate independent re-verification --------------------------------
        self.emit("stage", {"stage": "verify", "status": "running"})
        try:
            report = self.verify_run(run_id, refetch=True, registry=reg)
            self.emit("stage", {"stage": "verify", "status": "done" if report["verdict"] == "VERIFIED" else "failed"})
        except Exception as e:  # noqa: BLE001
            report = {"verdict": "UNVERIFIED", "error": f"{type(e).__name__}: {e}", "checks": []}
            self.emit("stage", {"stage": "verify", "status": "failed", "message": str(e)[:200]})

        summary["verification"] = report
        _atomic_write(run_dir / "run.json", json.dumps(summary, indent=1, default=str).encode("utf-8"))
        self.emit("run.done", {"run_id": run_id, "status": "anchored", "anchor": receipt, "eas": eas_info, "ipfs": ipfs_info, "ots": ots_info, "verification": report})
        return summary

    def run(self, image_bytes: bytes, source: str = "upload", run_id: str | None = None, auto_anchor: bool = True, face_index: int = 0, target_url: str = "", name_hint: str = "") -> dict:
        summary = self.scan(image_bytes, source, run_id, face_index=face_index, target_url=target_url, name_hint=name_hint)
        if summary["status"] != "matches":
            self.emit("run.done", {"run_id": summary["run_id"], "status": summary["status"]})
            return summary
        if not auto_anchor:
            return summary
        return self.anchor(summary["run_id"])

    # ------------------------------------------------------------ verification
    def verify_run(self, run_id: str, refetch: bool = True, registry: Registry | None = None, bundle_override: bytes | None = None) -> dict:
        """Independently re-verify a run against the chain.

        Every check is recomputed from the stored files (or a supplied bundle),
        never from cached results, so tampering with any byte is detected.
        """
        run_dir = self._run_dir(run_id)
        if not (run_dir / "anchor.json").exists():
            raise RuntimeError(f"run {run_id} has not been anchored yet")
        anchor = json.loads((run_dir / "anchor.json").read_text(encoding="utf-8"))
        raw = bundle_override if bundle_override is not None else (run_dir / "bundle.json").read_bytes()
        checks: list[dict] = []

        def check(name, ok, detail="", expected=None, actual=None):
            c = {"name": name, "ok": bool(ok), "detail": detail}
            if expected is not None:
                c["expected"] = expected
            if actual is not None:
                c["actual"] = actual
            checks.append(c)
            self.emit("verify.check", c)
            return ok

        try:
            bundle = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            check("bundle.parse", False, f"bundle is not valid JSON: {e}")
            return {"verdict": "TAMPERED", "checks": checks}
        canonical = ev.canonical_bytes(bundle)
        stored = (run_dir / "bundle.json").read_bytes() if (run_dir / "bundle.json").exists() else b""
        if bundle_override is None:
            ok = canonical == raw
            check("bundle.canonical", ok, "stored bundle bytes are in canonical form" if ok else "stored bundle bytes differ from canonical serialisation")
        else:
            ok = raw == stored
            check("bundle.canonical", ok, "supplied bundle is byte-identical to the stored one" if ok else f"supplied bundle differs from the stored bundle by {abs(len(raw) - len(stored))} byte(s) — recomputing every hash from the supplied bytes", expected=f"{len(stored)} bytes stored", actual=f"{len(raw)} bytes supplied")
        rh = ev.record_hash(bundle)
        mr = ev.bundle_merkle_root(bundle)

        reg = registry or Registry(self.s, log=lambda m: self.emit("chain.log", {"message": m}))
        onchain = reg.verify_hash(rh)
        check("chain.record_hash", onchain["exists"], "recomputed keccak256(bundle) found on-chain" if onchain["exists"] else "recomputed hash NOT found on-chain - bundle differs from what was anchored", expected=anchor["record_hash"], actual=rh)
        rec = reg.get_record(anchor["record_id"])
        def _d(ok, what):
            return f"{what} matches on-chain record #{anchor['record_id']}" if ok else f"{what} DIFFERS from on-chain record #{anchor['record_id']}"

        ok = rec["recordHash"] == rh
        check("chain.record_id", ok, _d(ok, "recordHash"), expected=rec["recordHash"], actual=rh)
        ok = rec["merkleRoot"] == mr
        check("chain.merkle_root", ok, _d(ok, "Merkle root over all bundle fields"), expected=rec["merkleRoot"], actual=mr)
        ok = rec["faceCommitment"] == bundle["query"]["face_commitment"]
        check("chain.face_commitment", ok, _d(ok, "face commitment"), expected=rec["faceCommitment"], actual=bundle["query"]["face_commitment"])
        ok = rec["uri"] == bundle["match"]["url"]
        check("chain.uri", ok, _d(ok, "post URL"), expected=rec["uri"], actual=bundle["match"]["url"])
        bps = int(round(bundle["match"]["similarity"] * 10000))
        ok = rec["similarityBps"] == bps
        check("chain.similarity", ok, _d(ok, "similarity (bps)"), expected=rec["similarityBps"], actual=bps)
        ok = rec["contentHash"] == bundle["match"]["image_sha256"]
        check("chain.content_hash", ok, _d(ok, "post image sha256"), expected=rec["contentHash"], actual=bundle["match"]["image_sha256"])

        # selective disclosure: prove a single field on-chain
        try:
            proof = ev.proof_for(bundle, "match.url")
            ok = reg.verify_leaf(anchor["record_id"], proof["leaf"], proof["proof"])
            check("chain.merkle_proof(match.url)", ok, f"on-chain Merkle proof for match.url ({len(proof['proof'])} siblings) {'accepted' if ok else 'REJECTED'} by the contract")
        except Exception as e:  # noqa: BLE001
            check("chain.merkle_proof(match.url)", False, str(e))

        # live content drift
        drift = None
        if refetch:
            live = fetch_image(bundle["match"]["image_url"])
            if live is None:
                check("live.image_fetch", False, "matched image could not be fetched right now (deleted / blocked?) - cannot compare")
            else:
                live_hash = ev.sha256_hex(live)
                same = live_hash == rec["contentHash"]
                drift = not same
                if not same:
                    # some CDNs re-encode: compare faces instead of bytes
                    try:
                        emb = np.load(run_dir / "query_embedding.npy")
                        img = cv2.imdecode(np.frombuffer(live, np.uint8), cv2.IMREAD_COLOR)
                        faces = self.engine.detect(img) if img is not None else []
                        best = max((cosine(emb, self.engine.embed(img, f)) for f in faces), default=-1)
                        check("live.image_hash", False, f"live bytes differ from anchored hash (CDN re-encode or edit); face re-match similarity {best:.3f}", expected=rec["contentHash"], actual=live_hash)
                        check("live.face_rematch", best >= self.s.match_threshold, f"live image still contains the same face (sim {best:.3f})")
                    except Exception as e:  # noqa: BLE001
                        check("live.image_hash", False, f"live bytes differ; face re-check failed: {e}", expected=rec["contentHash"], actual=live_hash)
                else:
                    check("live.image_hash", True, "live post image bytes are identical to the anchored hash - no content drift", expected=rec["contentHash"], actual=live_hash)

        # IPFS copy
        try:
            ipfs_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8")).get("ipfs", {})
        except Exception:  # noqa: BLE001
            ipfs_meta = {}
        recomputed_cid = ipfs_mod.cid_v1_raw(canonical)
        recorded_cid = ipfs_meta.get("cid_local")
        check("ipfs.cid", bool(recorded_cid) and recomputed_cid == recorded_cid, f"recomputed CIDv1 matches the recorded one ({recomputed_cid})" if recorded_cid == recomputed_cid else "recomputed content id differs from the recorded CIDv1" if recorded_cid else "no CIDv1 recorded for this run", expected=recorded_cid, actual=recomputed_cid)
        if refetch and ipfs_meta.get("pinned") and ipfs_meta.get("cid"):
            try:
                remote = ipfs_mod.fetch_ipfs(ipfs_meta["cid"], [self.s.pinata_gateway, *ipfs_mod.PUBLIC_GATEWAYS])
                check("ipfs.pinned_copy", remote == canonical, "IPFS copy is byte-identical to the local bundle")
            except Exception as e:  # noqa: BLE001
                check("ipfs.pinned_copy", False, f"could not fetch pinned copy: {str(e)[:120]}")

        # EAS
        eas_path = run_dir / "eas.json"
        if eas_path.exists():
            eas_info = json.loads(eas_path.read_text(encoding="utf-8"))
            if eas_info.get("attestation_uid"):
                try:
                    from .chain.eas import EAS

                    att = EAS(reg, self.s.eas_contract, self.s.eas_schema_registry, self.s.eas_explorer).get(eas_info["attestation_uid"])
                    ok = att["data"]["recordHash"] == rh and att["revocationTime"] == 0
                    check("eas.attestation", ok, f"EAS attestation {eas_info['attestation_uid'][:14]}... " + ("carries the same recordHash and is not revoked" if ok else ("is REVOKED" if att["revocationTime"] else "carries a DIFFERENT recordHash")))
                except Exception as e:  # noqa: BLE001
                    check("eas.attestation", False, f"EAS lookup failed: {str(e)[:120]}")

        # OTS
        ots_file = run_dir / "ots" / "bundle.json.ots"
        if ots_file.exists():
            try:
                st = ots_mod.upgrade(ots_file) if refetch else ots_mod.status(ots_file)
                ok = st["file_digest"] == ev.sha256_hex(canonical)[2:]
                check("ots.digest", ok, f"OpenTimestamps proof digest matches bundle; Bitcoin status: {st['status']}" + (f" (block {st['bitcoin_block_heights']})" if st.get("bitcoin_block_heights") else " (calendar-pending; upgrades once mined)"))
            except Exception as e:  # noqa: BLE001
                check("ots.digest", False, f"OTS check failed: {str(e)[:120]}")

        core = [c for c in checks if c["name"].startswith(("bundle.", "chain."))]
        verdict = "VERIFIED" if all(c["ok"] for c in core) else "TAMPERED"
        report = {
            "verdict": verdict,
            "run_id": run_id,
            "record_id": anchor["record_id"],
            "record_hash": rh,
            "tx": anchor.get("explorer_tx"),
            "content_drift": drift,
            "checks": checks,
            "onchain_record": rec,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        name = "verification.json" if bundle_override is None else "verification.tampered.json"
        (run_dir / name).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        self.emit("verify.done", report)
        return report


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _write_jpeg(path: Path, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if ok:
        path.write_bytes(buf.tobytes())


def _persist_env(key: str, value: str) -> None:
    """Persist a discovered value (contract address, salt) back into .env so the
    next run reuses it. Set VERIFIED_NO_ENV_WRITE=1 to keep .env read-only."""
    if os.environ.get("VERIFIED_NO_ENV_WRITE") == "1":
        return
    env = ROOT / ".env"
    try:
        lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
        found = False
        for i, l in enumerate(lines):
            if l.split("=", 1)[0].strip() == key:
                lines[i] = f"{key}={value}"
                found = True
        if not found:
            lines.append(f"{key}={value}")
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
