#!/usr/bin/env python3
"""VERIFIED - standalone third-party verifier.

Anyone can check an anchored record without this project's run folder, without
a wallet and without trusting us. All it needs is:

  * the evidence bundle JSON (from IPFS, from the repo, or handed over), and
  * the chain + registry address + record id (all printed in the receipt).

    python verify_standalone.py --bundle bundle.json \
        --chain sepolia --contract 0xREGISTRY --record 3

It recomputes keccak256(canonical(bundle)), the Merkle root over every field
and (with --field) an on-chain Merkle proof, then compares everything with the
record stored on-chain. Only `web3` and `eth-utils` are required.

Exit code 0 = VERIFIED, 1 = TAMPERED / mismatch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verified.chain import evidence as ev  # noqa: E402
from verified.chain.presets import PRESETS  # noqa: E402

ABI = json.loads((Path(__file__).parent / "verified/chain/artifacts/VerifiedRegistry.json").read_text(encoding="utf-8"))["abi"]

GREEN, RED, DIM, OFF = "\033[92m", "\033[91m", "\033[2m", "\033[0m"


def main() -> int:
    ap = argparse.ArgumentParser(description="Independently verify a VERIFIED record against the chain.")
    ap.add_argument("--bundle", required=True, type=Path, help="evidence bundle JSON (canonical or pretty)")
    ap.add_argument("--contract", required=True, help="VerifiedRegistry address")
    ap.add_argument("--record", required=True, type=int, help="on-chain record id")
    ap.add_argument("--chain", default="sepolia", choices=sorted(PRESETS), help="chain preset")
    ap.add_argument("--rpc", default="", help="override the preset RPC URL")
    ap.add_argument("--field", default="match.url", help="bundle field to prove on-chain via verifyLeaf")
    a = ap.parse_args()

    from web3 import Web3

    preset = PRESETS[a.chain]
    rpc = a.rpc or preset["rpc_url"]
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    print(f"{DIM}chain   {preset['chain_name']} (id {w3.eth.chain_id}) via {rpc}{OFF}")

    raw = a.bundle.read_bytes()
    bundle = json.loads(raw.decode("utf-8"))
    canonical = ev.canonical_bytes(bundle)
    record_hash = ev.record_hash(bundle)
    merkle_root = ev.bundle_merkle_root(bundle)
    print(f"{DIM}bundle  {len(canonical)} canonical bytes · sha256 {hashlib.sha256(canonical).hexdigest()[:16]}…{OFF}")

    c = w3.eth.contract(address=Web3.to_checksum_address(a.contract), abi=ABI)
    keys = ["recordHash", "faceCommitment", "contentHash", "merkleRoot", "uri", "platform", "evidenceCID", "similarityBps", "timestamp", "submitter"]
    rec = dict(zip(keys, c.functions.get(a.record).call()))
    for k in ("recordHash", "faceCommitment", "contentHash", "merkleRoot"):
        rec[k] = "0x" + bytes(rec[k]).hex()

    checks: list[tuple[str, bool, str]] = []
    exists, rid, ts, submitter = c.functions.verify(Web3.to_bytes(hexstr=record_hash)).call()
    checks.append(("keccak256(bundle) is anchored on this chain", bool(exists), f"record #{rid}" if exists else "not found - the bundle differs from anything anchored here"))
    checks.append(("recordHash matches record #%d" % a.record, rec["recordHash"] == record_hash, f"{rec['recordHash'][:18]}… vs {record_hash[:18]}…"))
    checks.append(("Merkle root over all fields matches", rec["merkleRoot"] == merkle_root, f"{rec['merkleRoot'][:18]}… vs {merkle_root[:18]}…"))
    checks.append(("post URL matches", rec["uri"] == bundle["match"]["url"], rec["uri"]))
    checks.append(("face commitment matches", rec["faceCommitment"] == bundle["query"]["face_commitment"], rec["faceCommitment"][:18] + "…"))
    checks.append(("post image sha256 matches", rec["contentHash"] == bundle["match"]["image_sha256"], rec["contentHash"][:18] + "…"))
    bps = int(round(float(bundle["match"]["similarity"]) * 10000))
    checks.append(("similarity matches", rec["similarityBps"] == bps, f"{rec['similarityBps']} bps on-chain"))

    try:
        proof = ev.proof_for(bundle, a.field)  # the leaf is recomputed locally, never taken on trust
        ok = bool(c.functions.verifyLeaf(a.record, Web3.to_bytes(hexstr=proof["leaf"]), [Web3.to_bytes(hexstr=p) for p in proof["proof"]]).call())
        checks.append((f"on-chain Merkle proof for {a.field}", ok, f"{len(proof['proof'])} siblings · value {proof['value'][:60]}"))
    except Exception as e:  # noqa: BLE001
        checks.append((f"on-chain Merkle proof for {a.field}", False, str(e)[:90]))

    print()
    for name, ok, detail in checks:
        print(f"  {GREEN + 'PASS' + OFF if ok else RED + 'FAIL' + OFF}  {name:44s} {DIM}{detail}{OFF}")
    verdict = all(ok for _, ok, _ in checks)
    print()
    print(f"  anchored by {rec['submitter']} at unix {rec['timestamp']}")
    if preset["explorer_url"]:
        print(f"  {preset['explorer_url']}/address/{a.contract}")
    print(f"\n  VERDICT: {(GREEN + 'VERIFIED') if verdict else (RED + 'TAMPERED')}{OFF}\n")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
