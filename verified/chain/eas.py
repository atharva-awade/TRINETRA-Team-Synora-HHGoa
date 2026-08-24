"""Ethereum Attestation Service (EAS) integration.

Besides our own registry we publish each match as a standard EAS attestation
so any EAS-aware tool (easscan.org, wallets, other dapps) can consume it.
Sepolia deployment addresses come from the EAS docs.
"""
from __future__ import annotations

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import Web3

from .registry import Registry, _receipt_like

SCHEMA = (
    "bytes32 recordHash,bytes32 faceCommitment,bytes32 contentHash,bytes32 merkleRoot,"
    "string uri,string platform,uint16 similarityBps,string evidenceCID,address registry,uint256 recordId"
)
SCHEMA_TYPES = ["bytes32", "bytes32", "bytes32", "bytes32", "string", "string", "uint16", "string", "address", "uint256"]

REGISTRY_ABI = [
    {"type": "function", "name": "register", "stateMutability": "nonpayable", "inputs": [{"name": "schema", "type": "string"}, {"name": "resolver", "type": "address"}, {"name": "revocable", "type": "bool"}], "outputs": [{"name": "", "type": "bytes32"}]},
    {"type": "function", "name": "getSchema", "stateMutability": "view", "inputs": [{"name": "uid", "type": "bytes32"}], "outputs": [{"name": "", "type": "tuple", "components": [{"name": "uid", "type": "bytes32"}, {"name": "resolver", "type": "address"}, {"name": "revocable", "type": "bool"}, {"name": "schema", "type": "string"}]}]},
]

EAS_ABI = [
    {
        "type": "function",
        "name": "attest",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "request",
                "type": "tuple",
                "components": [
                    {"name": "schema", "type": "bytes32"},
                    {
                        "name": "data",
                        "type": "tuple",
                        "components": [
                            {"name": "recipient", "type": "address"},
                            {"name": "expirationTime", "type": "uint64"},
                            {"name": "revocable", "type": "bool"},
                            {"name": "refUID", "type": "bytes32"},
                            {"name": "data", "type": "bytes"},
                            {"name": "value", "type": "uint256"},
                        ],
                    },
                ],
            }
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "getAttestation",
        "stateMutability": "view",
        "inputs": [{"name": "uid", "type": "bytes32"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "uid", "type": "bytes32"},
                    {"name": "schema", "type": "bytes32"},
                    {"name": "time", "type": "uint64"},
                    {"name": "expirationTime", "type": "uint64"},
                    {"name": "revocationTime", "type": "uint64"},
                    {"name": "refUID", "type": "bytes32"},
                    {"name": "recipient", "type": "address"},
                    {"name": "attester", "type": "address"},
                    {"name": "revocable", "type": "bool"},
                    {"name": "data", "type": "bytes"},
                ],
            }
        ],
    },
    {"type": "event", "name": "Attested", "anonymous": False, "inputs": [{"indexed": True, "name": "recipient", "type": "address"}, {"indexed": True, "name": "attester", "type": "address"}, {"indexed": False, "name": "uid", "type": "bytes32"}, {"indexed": True, "name": "schemaUID", "type": "bytes32"}]},
]

ZERO = "0x" + "00" * 32


def schema_uid(schema: str = SCHEMA, resolver: str = "0x" + "00" * 20, revocable: bool = True) -> str:
    """EAS derives schema UIDs deterministically: keccak256(abi.encodePacked(schema, resolver, revocable))."""
    packed = schema.encode() + bytes.fromhex(resolver[2:].rjust(40, "0")) + (b"\x01" if revocable else b"\x00")
    return "0x" + keccak(packed).hex()


class EAS:
    def __init__(self, reg: Registry, eas_address: str, schema_registry: str, explorer: str):
        self.reg = reg
        self.w3 = reg.w3
        self.eas = self.w3.eth.contract(address=Web3.to_checksum_address(eas_address), abi=EAS_ABI)
        self.schema_registry = self.w3.eth.contract(address=Web3.to_checksum_address(schema_registry), abi=REGISTRY_ABI)
        self.explorer = explorer.rstrip("/")

    def ensure_schema(self, log=lambda *_: None) -> str:
        uid = schema_uid()
        rec = self.schema_registry.functions.getSchema(Web3.to_bytes(hexstr=uid)).call()
        if rec[3]:  # schema string non-empty => registered
            return uid
        log("Registering EAS schema (one-time)...")
        self.reg._send(self.schema_registry.functions.register(SCHEMA, "0x" + "00" * 20, True))
        return uid

    def attest(self, *, record_hash: str, face_commitment: str, content_hash: str, merkle_root: str, uri: str, platform: str, similarity: float, evidence_cid: str, registry: str, record_id: int, log=lambda *_: None) -> dict:
        uid = self.ensure_schema(log)
        bps = int(round(max(0.0, min(1.0, similarity)) * 10000))
        data = abi_encode(
            SCHEMA_TYPES,
            [
                Web3.to_bytes(hexstr=record_hash),
                Web3.to_bytes(hexstr=face_commitment),
                Web3.to_bytes(hexstr=content_hash),
                Web3.to_bytes(hexstr=merkle_root),
                uri,
                platform,
                bps,
                evidence_cid,
                Web3.to_checksum_address(registry),
                int(record_id),
            ],
        )
        request = (Web3.to_bytes(hexstr=uid), (self.reg.account.address, 0, True, Web3.to_bytes(hexstr=ZERO), data, 0))
        res = self.reg._send(self.eas.functions.attest(request))
        events = self.eas.events.Attested().process_receipt(_receipt_like(res))
        att_uid = "0x" + bytes(events[0]["args"]["uid"]).hex() if events else None
        return {
            "schema_uid": uid,
            "attestation_uid": att_uid,
            "tx_hash": res.tx_hash,
            "block_number": res.block_number,
            "eas_contract": self.eas.address,
            "explorer_attestation": f"{self.explorer}/attestation/view/{att_uid}" if att_uid else None,
            "explorer_schema": f"{self.explorer}/schema/view/{uid}",
        }

    def get(self, attestation_uid: str) -> dict:
        a = self.eas.functions.getAttestation(Web3.to_bytes(hexstr=attestation_uid)).call()
        from eth_abi import decode as abi_decode

        vals = abi_decode(SCHEMA_TYPES, bytes(a[9]))
        names = ["recordHash", "faceCommitment", "contentHash", "merkleRoot", "uri", "platform", "similarityBps", "evidenceCID", "registry", "recordId"]
        decoded = {n: ("0x" + v.hex() if isinstance(v, (bytes, bytearray)) else v) for n, v in zip(names, vals)}
        return {"uid": "0x" + bytes(a[0]).hex(), "schema": "0x" + bytes(a[1]).hex(), "time": int(a[2]), "revocationTime": int(a[4]), "attester": a[7], "recipient": a[6], "data": decoded}
