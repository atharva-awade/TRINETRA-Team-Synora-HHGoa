"""Offline end-to-end test: the real face models and the real Solidity contract
on an in-process chain, with only the SerpApi HTTP layer replaced by a local
fixture server whose "visual matches" point at the bundled sample photos.

Run:  python -m pytest tests -q          (needs no node, no keys, no network)
      ANVIL_RPC=http://127.0.0.1:8545 python -m pytest tests -q   (real node)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _chain():
    """Prefer an explicit node, then a local anvil binary, else eth-tester."""
    url = os.environ.get("ANVIL_RPC")
    if url:
        return url, "31337", None
    exe = shutil.which("anvil") or os.environ.get("ANVIL_BIN")
    if exe:
        proc = subprocess.Popen([exe, "--port", "8546", "--silent", "--chain-id", "31337"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        return "http://127.0.0.1:8546", "31337", proc
    return "tester", "0", None


def test_pipeline_end_to_end():
    from fixture_server import FixtureServer

    rpc, chain_id, proc = _chain()
    runs = Path(tempfile.mkdtemp(prefix="verified-runs-"))
    try:
        with FixtureServer(8766) as fx:
            os.environ.update(
                {
                    "SERPAPI_BASE": fx.base_url,
                    "SERPAPI_KEY": "fixture",
                    "RPC_URL": rpc,
                    "RPC_FALLBACKS": "",
                    "CHAIN_ID": chain_id,
                    "CHAIN_NAME": "local test chain",
                    "EXPLORER_URL": "",
                    "PRIVATE_KEY": ANVIL_KEY,
                    "CONTRACT_ADDRESS": "",
                    "COMMITMENT_SALT": "0x" + "ab" * 32,
                    "ENABLE_YANDEX": "false",
                    "ENABLE_GOOGLE_REVERSE": "false",
                    "ENABLE_BLUESKY": "false",
                    "ENABLE_NAME_EXPANSION": "false",
                    "ENABLE_EAS": "false",
                    "ENABLE_OTS": "false",
                    "IMAGE_HOST": "none",
                    "PINATA_JWT": "",
                    "RUNS_DIR": str(runs),
                    "ALLOW_LOCAL_FETCH": "1",
                    "VERIFIED_NO_ENV_WRITE": "1",
                }
            )
            # engines module reads SERPAPI_BASE at import time
            for m in list(sys.modules):
                if m.startswith("verified"):
                    del sys.modules[m]
            from verified.config import Settings
            from verified.pipeline import Pipeline

            s = Settings(_env_file=None)
            events = []
            p = Pipeline(s, emit=lambda e, d: events.append((e, d)))
            summary = p.run((ROOT / "samples" / "obama.jpg").read_bytes(), source="upload")
            assert summary["status"] == "anchored", summary["status"]
            matches = summary["search"]["matches"]
            assert matches, "expected at least one verified match"
            best = matches[summary["selected_match"]]
            assert "instagram.com" in best["link"] and best["similarity"] > 0.6, best
            # different people must be rejected by biometric verification
            rejected_links = {m["link"] for m in summary["search"]["rejected"]}
            assert "https://x.com/JoeBiden/status/1" in rejected_links
            assert "https://example.com/group" in rejected_links
            # two_people contains Obama too -> should be a verified match as well
            assert any("facebook.com" in m["link"] and m["similarity"] > 0.6 for m in matches)
            assert summary["verification"]["verdict"] == "VERIFIED"
            assert all(c["ok"] for c in summary["verification"]["checks"] if c["name"].startswith(("chain.", "bundle.")))

            # tamper: alter one byte of the bundle -> TAMPERED
            run_dir = runs / summary["run_id"]
            bundle = json.loads((run_dir / "bundle.json").read_bytes())
            bundle["match"]["similarity"] = round(bundle["match"]["similarity"] + 0.0001, 4)
            from verified.chain.evidence import canonical_bytes

            rep = p.verify_run(summary["run_id"], refetch=False, bundle_override=canonical_bytes(bundle))
            assert rep["verdict"] == "TAMPERED"
            names = {c["name"]: c["ok"] for c in rep["checks"]}
            assert names["chain.record_hash"] is False and names["chain.merkle_root"] is False
            # untouched fields still prove individually via on-chain Merkle proof
            assert names["chain.merkle_proof(match.url)"] is False  # root differs for tampered bundle
            good = p.verify_run(summary["run_id"], refetch=False)
            assert good["verdict"] == "VERIFIED"
            print("E2E OK:", best["link"], best["similarity"], summary["anchor"]["tx_hash"])
    finally:
        if proc:
            proc.terminate()
        shutil.rmtree(runs, ignore_errors=True)


if __name__ == "__main__":
    test_pipeline_end_to_end()
    print("all good")
