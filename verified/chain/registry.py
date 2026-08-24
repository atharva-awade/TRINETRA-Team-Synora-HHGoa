"""web3.py client for the VerifiedRegistry contract (deploy / anchor / verify)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account
from web3 import Web3
from web3.exceptions import Web3Exception

from ..config import Settings

ARTIFACT = Path(__file__).with_name("artifacts") / "VerifiedRegistry.json"


class ChainError(RuntimeError):
    pass


def load_artifact() -> dict:
    return json.loads(ARTIFACT.read_text())


def connect(settings: Settings, log=lambda *_: None) -> Web3:
    """Connect to the first responsive RPC endpoint on the configured chain."""
    errors = []
    for url in settings.rpc_urls:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
            cid = w3.eth.chain_id
            if settings.chain_id and cid != settings.chain_id:
                errors.append(f"{url}: chain id {cid} != {settings.chain_id}")
                continue
            log(f"RPC connected: {url} (chain {cid}, block {w3.eth.block_number})")
            return w3
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {type(e).__name__}: {str(e)[:80]}")
    raise ChainError("No RPC endpoint reachable:\n  " + "\n  ".join(errors))


@dataclass
class TxResult:
    tx_hash: str
    block_number: int
    gas_used: int
    status: int
    contract_address: str | None = None
    logs: list | None = None


class Registry:
    def __init__(self, settings: Settings, w3: Web3 | None = None, log=lambda *_: None):
        self.s = settings
        self.log = log
        self.w3 = w3 or connect(settings, log)
        self.art = load_artifact()
        self.account = Account.from_key(settings.private_key) if settings.private_key else None
        self.contract = None
        if settings.contract_address:
            self.contract = self.w3.eth.contract(address=Web3.to_checksum_address(settings.contract_address), abi=self.art["abi"])

    # ------------------------------------------------------------- utilities
    @property
    def address(self) -> str | None:
        return self.account.address if self.account else None

    def balance_eth(self) -> float:
        if not self.account:
            return 0.0
        return float(self.w3.from_wei(self.w3.eth.get_balance(self.account.address), "ether"))

    def explorer_tx(self, tx_hash: str) -> str:
        return f"{self.s.explorer_url.rstrip('/')}/tx/{tx_hash}" if self.s.explorer_url else tx_hash

    def explorer_address(self, addr: str) -> str:
        return f"{self.s.explorer_url.rstrip('/')}/address/{addr}" if self.s.explorer_url else addr

    def _send(self, fn_tx, value: int = 0, gas_margin: float = 1.25) -> TxResult:
        if not self.account:
            raise ChainError("PRIVATE_KEY not set (run: python -m verified.cli wallet new) - read-only mode")
        base = {
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address, "pending"),
            "chainId": self.w3.eth.chain_id,
            "value": value,
        }
        # EIP-1559 fees when available, legacy otherwise
        try:
            latest = self.w3.eth.get_block("latest")
            base_fee = latest.get("baseFeePerGas")
        except Exception:  # noqa: BLE001
            base_fee = None
        if base_fee is not None:
            try:
                tip = self.w3.eth.max_priority_fee
            except Exception:  # noqa: BLE001
                tip = self.w3.to_wei(1.5, "gwei")
            tip = max(tip, self.w3.to_wei(1, "gwei"))
            base.update({"maxPriorityFeePerGas": tip, "maxFeePerGas": base_fee * 2 + tip})
        else:
            base["gasPrice"] = self.w3.eth.gas_price
        tx = fn_tx.build_transaction(base)
        try:
            est = self.w3.eth.estimate_gas(tx)
        except Web3Exception as e:
            raise ChainError(f"gas estimation failed (does the wallet have Sepolia ETH? / revert?): {e}") from e
        tx["gas"] = int(est * gas_margin)
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        self.log(f"tx sent {tx_hash.hex()} - waiting for inclusion...")
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=240, poll_latency=2)
        if receipt["status"] != 1:
            raise ChainError(f"transaction reverted: {tx_hash.hex()}")
        h = tx_hash.hex()
        h = h if h.startswith("0x") else "0x" + h
        return TxResult(tx_hash=h, block_number=receipt["blockNumber"], gas_used=receipt["gasUsed"], status=receipt["status"], contract_address=receipt.get("contractAddress"), logs=receipt.get("logs"))

    # ---------------------------------------------------------------- deploy
    def deploy(self) -> TxResult:
        c = self.w3.eth.contract(abi=self.art["abi"], bytecode=self.art["bytecode"])
        res = self._send(c.constructor())
        self.contract = self.w3.eth.contract(address=res.contract_address, abi=self.art["abi"])
        self.log(f"VerifiedRegistry deployed at {res.contract_address}")
        return res

    def ensure_deployed(self) -> str:
        if self.contract is None:
            self.log("No CONTRACT_ADDRESS configured - deploying VerifiedRegistry...")
            self.deploy()
        else:
            code = self.w3.eth.get_code(self.contract.address)
            if len(code) == 0:
                raise ChainError(f"No contract code at {self.contract.address} on chain {self.w3.eth.chain_id}. Redeploy with: python -m verified.cli deploy")
        return self.contract.address

    # ---------------------------------------------------------------- anchor
    def anchor(self, *, record_hash: str, face_commitment: str, content_hash: str, merkle_root: str, uri: str, platform: str, evidence_cid: str, similarity: float) -> dict:
        self.ensure_deployed()
        bps = int(round(max(0.0, min(1.0, similarity)) * 10000))
        fn = self.contract.functions.anchor(
            Web3.to_bytes(hexstr=record_hash),
            Web3.to_bytes(hexstr=face_commitment),
            Web3.to_bytes(hexstr=content_hash),
            Web3.to_bytes(hexstr=merkle_root),
            uri,
            platform,
            evidence_cid,
            bps,
        )
        res = self._send(fn)
        events = self.contract.events.Anchored().process_receipt(_receipt_like(res))
        rec_id = int(events[0]["args"]["id"]) if events else None
        block = self.w3.eth.get_block(res.block_number)
        return {
            "tx_hash": res.tx_hash,
            "block_number": res.block_number,
            "block_timestamp": int(block["timestamp"]),
            "gas_used": res.gas_used,
            "record_id": rec_id,
            "contract": self.contract.address,
            "chain_id": self.w3.eth.chain_id,
            "chain": self.s.chain_name,
            "submitter": self.account.address,
            "explorer_tx": self.explorer_tx(res.tx_hash),
            "explorer_contract": self.explorer_address(self.contract.address),
        }

    # ---------------------------------------------------------------- verify
    def get_record(self, record_id: int) -> dict:
        r = self.contract.functions.get(record_id).call()
        keys = ["recordHash", "faceCommitment", "contentHash", "merkleRoot", "uri", "platform", "evidenceCID", "similarityBps", "timestamp", "submitter"]
        d = dict(zip(keys, r))
        for k in ("recordHash", "faceCommitment", "contentHash", "merkleRoot"):
            d[k] = "0x" + bytes(d[k]).hex()
        return d

    def verify_hash(self, record_hash: str) -> dict:
        exists, rid, ts, submitter = self.contract.functions.verify(Web3.to_bytes(hexstr=record_hash)).call()
        return {"exists": bool(exists), "record_id": int(rid), "timestamp": int(ts), "submitter": submitter}

    def verify_leaf(self, record_id: int, leaf: str, proof: list[str]) -> bool:
        return bool(self.contract.functions.verifyLeaf(record_id, Web3.to_bytes(hexstr=leaf), [Web3.to_bytes(hexstr=p) for p in proof]).call())

    def records_by_commitment(self, commitment: str) -> list[int]:
        return [int(i) for i in self.contract.functions.recordsByCommitment(Web3.to_bytes(hexstr=commitment)).call()]

    def count(self) -> int:
        return int(self.contract.functions.count().call())


class _AttrDict(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:
            raise AttributeError(k) from e


def _receipt_like(res: TxResult) -> dict:
    return _AttrDict({"logs": res.logs or [], "blockNumber": res.block_number, "transactionHash": res.tx_hash, "status": res.status})


def new_wallet() -> tuple[str, str]:
    acct = Account.create()
    return acct.address, acct.key.hex() if acct.key.hex().startswith("0x") else "0x" + acct.key.hex()
