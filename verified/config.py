"""Runtime configuration (read from environment / .env)."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # --- search engines -----------------------------------------------------
    serpapi_key: str = Field(default="", alias="SERPAPI_KEY")
    google_vision_api_key: str = Field(default="", alias="GOOGLE_VISION_API_KEY")
    google_application_credentials: str = Field(default="", alias="GOOGLE_APPLICATION_CREDENTIALS")
    enable_yandex: bool = Field(default=True, alias="ENABLE_YANDEX")
    enable_google_reverse: bool = Field(default=True, alias="ENABLE_GOOGLE_REVERSE")
    enable_bluesky: bool = Field(default=True, alias="ENABLE_BLUESKY")
    enable_name_expansion: bool = Field(default=True, alias="ENABLE_NAME_EXPANSION")
    image_host: str = Field(default="auto", alias="IMAGE_HOST")  # auto|litterbox|tmpfiles|0x0|pinata|none
    search_country: str = Field(default="in", alias="SEARCH_COUNTRY")
    search_lang: str = Field(default="en", alias="SEARCH_LANG")
    max_candidates: int = Field(default=60, alias="MAX_CANDIDATES")
    match_threshold: float = Field(default=0.40, alias="MATCH_THRESHOLD")

    # --- storage ------------------------------------------------------------
    pinata_jwt: str = Field(default="", alias="PINATA_JWT")
    pinata_gateway: str = Field(default="https://gateway.pinata.cloud/ipfs", alias="PINATA_GATEWAY")
    runs_dir: Path = Field(default=ROOT / "runs", alias="RUNS_DIR")

    # --- blockchain ---------------------------------------------------------
    rpc_url: str = Field(default="https://ethereum-sepolia-rpc.publicnode.com", alias="RPC_URL")
    rpc_fallbacks: str = Field(
        default="https://sepolia.drpc.org,https://rpc.sepolia.org,https://1rpc.io/sepolia",
        alias="RPC_FALLBACKS",
    )
    chain_id: int = Field(default=11155111, alias="CHAIN_ID")
    chain_name: str = Field(default="Ethereum Sepolia", alias="CHAIN_NAME")
    explorer_url: str = Field(default="https://sepolia.etherscan.io", alias="EXPLORER_URL")
    private_key: str = Field(default="", alias="PRIVATE_KEY")
    contract_address: str = Field(default="", alias="CONTRACT_ADDRESS")
    commitment_salt: str = Field(default="", alias="COMMITMENT_SALT")  # hex; generated per install if empty
    enable_eas: bool = Field(default=True, alias="ENABLE_EAS")
    eas_contract: str = Field(default="0xC2679fBD37d54388Ce493F1DB75320D236e1815e", alias="EAS_CONTRACT")
    eas_schema_registry: str = Field(default="0x0a7E2Ff54e76B8E6659aedc9103FB21c038050D0", alias="EAS_SCHEMA_REGISTRY")
    eas_schema_uid: str = Field(default="", alias="EAS_SCHEMA_UID")
    eas_explorer: str = Field(default="https://sepolia.easscan.org", alias="EAS_EXPLORER")
    enable_ots: bool = Field(default=True, alias="ENABLE_OTS")

    # --- server -------------------------------------------------------------
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    @property
    def rpc_urls(self) -> list[str]:
        urls = [self.rpc_url] + [u.strip() for u in self.rpc_fallbacks.split(",") if u.strip()]
        seen, out = set(), []
        for u in urls:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out


settings = Settings()
