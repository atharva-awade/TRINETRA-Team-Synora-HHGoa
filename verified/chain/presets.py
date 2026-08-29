"""Chain presets.

Hacker House Goa is explicitly multichain, and so is the anchoring layer: the
pipeline only needs an EVM RPC, a chain id, an explorer and (optionally) the
Ethereum Attestation Service addresses for that chain.  Pick one with
`CHAIN=<key>` in .env, or set RPC_URL / CHAIN_ID / EXPLORER_URL by hand.

EAS deployment addresses are the official ones from
https://github.com/ethereum-attestation-service/eas-contracts (README).
"""
from __future__ import annotations

PRESETS: dict[str, dict] = {
    "sepolia": {
        "chain_name": "Ethereum Sepolia",
        "chain_id": 11155111,
        "rpc_url": "https://ethereum-sepolia-rpc.publicnode.com",
        "rpc_fallbacks": "https://sepolia.drpc.org,https://rpc.sepolia.org,https://1rpc.io/sepolia",
        "explorer_url": "https://sepolia.etherscan.io",
        "eas_contract": "0xC2679fBD37d54388Ce493F1DB75320D236e1815e",
        "eas_schema_registry": "0x0a7E2Ff54e76B8E6659aedc9103FB21c038050D0",
        "eas_explorer": "https://sepolia.easscan.org",
        "faucet": "https://cloud.google.com/application/web3/faucet/ethereum/sepolia",
    },
    "base-sepolia": {
        "chain_name": "Base Sepolia",
        "chain_id": 84532,
        "rpc_url": "https://sepolia.base.org",
        "rpc_fallbacks": "https://base-sepolia-rpc.publicnode.com,https://base-sepolia.drpc.org",
        "explorer_url": "https://sepolia.basescan.org",
        "eas_contract": "0x4200000000000000000000000000000000000021",
        "eas_schema_registry": "0x4200000000000000000000000000000000000020",
        "eas_explorer": "https://base-sepolia.easscan.org",
        "faucet": "https://portal.cdp.coinbase.com/products/faucet",
    },
    "optimism-sepolia": {
        "chain_name": "Optimism Sepolia",
        "chain_id": 11155420,
        "rpc_url": "https://sepolia.optimism.io",
        "rpc_fallbacks": "https://optimism-sepolia-rpc.publicnode.com",
        "explorer_url": "https://sepolia-optimism.etherscan.io",
        "eas_contract": "0x4200000000000000000000000000000000000021",
        "eas_schema_registry": "0x4200000000000000000000000000000000000020",
        "eas_explorer": "https://optimism-sepolia.easscan.org",
        "faucet": "https://console.optimism.io/faucet",
    },
    "polygon-amoy": {
        "chain_name": "Polygon Amoy",
        "chain_id": 80002,
        "rpc_url": "https://rpc-amoy.polygon.technology",
        "rpc_fallbacks": "https://polygon-amoy-bor-rpc.publicnode.com",
        "explorer_url": "https://amoy.polygonscan.com",
        "eas_contract": "0xb101275a60d8bfb14529C421899aD7CA1Ae5B5Fc",
        "eas_schema_registry": "0x23c5701A1BDa89C61d181BD79E5203c730708AE7",
        "eas_explorer": "https://polygon-amoy.easscan.org",
        "faucet": "https://faucet.polygon.technology/",
    },
    "arbitrum-sepolia": {
        "chain_name": "Arbitrum Sepolia",
        "chain_id": 421614,
        "rpc_url": "https://sepolia-rollup.arbitrum.io/rpc",
        "rpc_fallbacks": "https://arbitrum-sepolia-rpc.publicnode.com",
        "explorer_url": "https://sepolia.arbiscan.io",
        "eas_contract": "0x4200000000000000000000000000000000000021",
        "eas_schema_registry": "0x4200000000000000000000000000000000000020",
        "eas_explorer": "https://arbitrum-sepolia.easscan.org",
        "faucet": "https://www.alchemy.com/faucets/arbitrum-sepolia",
    },
    "anvil": {
        "chain_name": "Anvil local",
        "chain_id": 31337,
        "rpc_url": "http://127.0.0.1:8545",
        "rpc_fallbacks": "",
        "explorer_url": "",
        "eas_contract": "",
        "eas_schema_registry": "",
        "eas_explorer": "",
        "faucet": "anvil funds its accounts automatically",
    },
    "tester": {
        "chain_name": "in-process test chain",
        "chain_id": 0,
        "rpc_url": "tester",
        "rpc_fallbacks": "",
        "explorer_url": "",
        "eas_contract": "",
        "eas_schema_registry": "",
        "eas_explorer": "",
        "faucet": "funded automatically",
    },
}

# EAS is not deployed on every chain; where it is missing we simply skip that step.
NO_EAS = {k for k, v in PRESETS.items() if not v["eas_contract"]}


def apply_preset(settings, key: str) -> str:
    """Overlay a preset onto a Settings instance. Explicit env values win."""
    import os

    key = (key or "").strip().lower()
    if key not in PRESETS:
        raise KeyError(f"unknown CHAIN preset {key!r}; known: {', '.join(sorted(PRESETS))}")
    preset = PRESETS[key]
    env_aliases = {
        "chain_name": "CHAIN_NAME", "chain_id": "CHAIN_ID", "rpc_url": "RPC_URL",
        "rpc_fallbacks": "RPC_FALLBACKS", "explorer_url": "EXPLORER_URL",
        "eas_contract": "EAS_CONTRACT", "eas_schema_registry": "EAS_SCHEMA_REGISTRY",
        "eas_explorer": "EAS_EXPLORER",
    }
    for field, value in preset.items():
        if field == "faucet":
            continue
        if os.environ.get(env_aliases[field]):  # an explicit env var always wins
            continue
        setattr(settings, field, value)
    if key in NO_EAS:
        settings.enable_eas = False
    return key
