"""Command-line interface.

    python -m verified.cli doctor                  # check keys, RPC, wallet, models
    python -m verified.cli wallet new              # create a throwaway testnet wallet
    python -m verified.cli deploy                  # deploy VerifiedRegistry
    python -m verified.cli run --image face.jpg    # full pipeline (scan -> search -> anchor -> verify)
    python -m verified.cli verify --run <run_id>   # independent re-verification
    python -m verified.cli tamper --run <run_id>   # flip one byte, show verification failing
    python -m verified.cli anchor --run <run_id> --match 2   # anchor a chosen match
    python -m verified.cli serve                   # local UI at http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.markup import escape as esc
from rich.panel import Panel
from rich.table import Table

from .config import ROOT, Settings

if sys.platform == "win32":  # make rich's unicode glyphs safe on legacy code pages
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

app = typer.Typer(add_completion=False, help="verified - face scan -> genuine social search -> blockchain anchor")
wallet_app = typer.Typer(help="wallet helpers")
app.add_typer(wallet_app, name="wallet")
console = Console()

STAGE_LABELS = {"face": "Face scan", "search": "Genuine search", "evidence": "Evidence bundle", "ipfs": "IPFS", "chain": "On-chain anchor", "eas": "EAS attestation", "ots": "Bitcoin (OTS)", "verify": "Re-verification"}


def _emit_console(event: str, payload: dict) -> None:
    if event == "stage":
        st = payload["status"]
        colour = {"running": "yellow", "done": "green", "failed": "red", "skipped": "dim"}.get(st, "white")
        msg = f" - {esc(str(payload.get('message')))}" if payload.get("message") else ""
        console.print(f"[{colour}]• {STAGE_LABELS.get(payload['stage'], payload['stage'])}: {st}{msg}[/{colour}]")
    elif event == "face.detected":
        q = payload["quality"]
        console.print(f"  face {payload['bbox']} det={payload['det_score']} quality={q['overall']} age~{payload['age']} {payload['gender']} faces_in_frame={payload['faces_in_frame']}")
        console.print(f"  commitment {payload['commitment']}")
    elif event == "search.hosted":
        console.print(f"  query crop hosted (ttl {payload['ttl']}) at {esc(payload['url'])} via {payload['host']}")
    elif event == "search.fanout":
        console.print(f"  engines: {', '.join(payload['engines'])}")
    elif event == "search.engine":
        if "error" in payload:
            console.print(f"  [red]{payload['engine']}: {esc(payload['error'][:160])}[/red]")
        else:
            console.print(f"  {payload['engine']}: {payload['candidates']} candidates ({payload['social']} social, {payload['posts']} posts)" + (f"  q={esc(repr(payload['query']))}" if payload.get("query") else ""))
    elif event == "search.verify.start":
        console.print(f"  biometric re-verification of {payload['shortlist']} candidates{' (expansion)' if payload.get('phase') else ''}...")
    elif event == "search.verify.progress" and payload.get("band") in ("strong", "match"):
        console.print(f"    [green]✓ {payload['similarity']:.3f} {payload['band']:6s} {payload['platform'] or 'Web':12s} {esc(payload['link'][:80])}[/green]")
    elif event == "search.identity":
        console.print(f"  inferred identity: {esc(str(payload['names'] or '-'))}")
    elif event == "search.done":
        console.print(f"  [bold]{payload['matches']} verified matches[/bold] ({payload['rejected']} rejected, {payload['candidates']} candidates) in {payload['timings'].get('total_s')}s")
    elif event == "anchor.selected":
        m = payload["match"]
        console.print(Panel(f"[bold]{esc(m['platform'])}[/bold]  sim={m['similarity']:.3f} ({m['band']})\n{esc(m['title'][:100])}\n{esc(m['canonical_link'])}", title="selected match", box=box.ROUNDED))
    elif event == "evidence.built":
        console.print(f"  recordHash {payload['record_hash']}\n  merkleRoot {payload['merkle_root']}  ({payload['leaf_count']} leaves, {payload['canonical_size']} bytes)")
    elif event == "ipfs.done":
        console.print(f"  CID {payload['cid_on_chain']}" + (f"  pinned → {esc(payload['gateway_url'])}" if payload.get("pinned") else "  (local CIDv1, not pinned)") + (f"  [red]{esc(payload['error'][:100])}[/red]" if payload.get("error") else ""))
    elif event == "chain.wallet":
        console.print(f"  wallet {payload['address']}  balance {payload['balance_eth']} ETH  ({payload['chain']})")
    elif event == "chain.log":
        console.print(f"  [dim]{esc(str(payload['message']))}[/dim]")
    elif event == "chain.deployed":
        console.print(f"  [bold green]deployed VerifiedRegistry at {payload['contract']}[/bold green]  {esc(payload['explorer'])}")
    elif event == "chain.anchored":
        console.print(Panel(f"tx      {payload['tx_hash']}\nblock   {payload['block_number']}   record #{payload['record_id']}   gas {payload['gas_used']}\n{esc(payload['explorer_tx'])}", title=f"anchored on {esc(payload['chain'])}", box=box.ROUNDED, style="green"))
    elif event == "eas.done" and payload.get("attestation_uid"):
        console.print(f"  EAS attestation {payload['attestation_uid']}\n  {esc(str(payload['explorer_attestation']))}")
    elif event == "ots.done" and payload.get("calendars"):
        console.print(f"  OpenTimestamps: submitted to {len(payload['calendars'])} calendars → {payload['ots_file']} (Bitcoin confirmation pending)")
    elif event == "verify.check":
        console.print(f"    {'[green]PASS[/green]' if payload['ok'] else '[red]FAIL[/red]'} {esc(payload['name']):32s} {esc(payload['detail'][:110])}")
    elif event == "verify.done":
        colour = "green" if payload["verdict"] == "VERIFIED" else "red"
        console.print(f"[bold {colour}]VERDICT: {payload['verdict']}[/bold {colour}]")


def _settings() -> Settings:
    return Settings()


@app.command()
def doctor():
    """Check models, keys, RPC connectivity, wallet balance and contract."""
    s = _settings()
    t = Table(title="verified doctor", box=box.SIMPLE_HEAVY)
    t.add_column("check")
    t.add_column("status")
    t.add_column("detail")

    def row(name, ok, detail, warn=False):
        t.add_row(name, "[green]OK[/green]" if ok else ("[yellow]WARN[/yellow]" if warn else "[red]FAIL[/red]"), esc(str(detail)))

    from .chain.presets import PRESETS

    if s.chain:
        row("chain preset", s.chain in PRESETS, f"CHAIN={s.chain} -> {s.chain_name} (id {s.chain_id})" if s.chain in PRESETS else f"unknown preset {s.chain!r}; known: {', '.join(sorted(PRESETS))}")

    from .face.models import REQUIRED, models_dir

    md = models_dir()
    missing = [m for m in REQUIRED if not (md / m).exists()]
    row("models", not missing, f"{md}" if not missing else f"missing {missing} - run: python -m verified.cli models")
    row("SERPAPI_KEY", bool(s.serpapi_key), "set" if s.serpapi_key else "missing - Google Lens / Yandex disabled (serpapi.com free tier)")
    row("GOOGLE_VISION_API_KEY", bool(s.google_vision_api_key or s.google_application_credentials), "set" if (s.google_vision_api_key or s.google_application_credentials) else "not set (optional engine)", warn=True)
    row("PINATA_JWT", bool(s.pinata_jwt), "set" if s.pinata_jwt else "not set - CID computed locally, not pinned", warn=True)
    if s.serpapi_key:
        try:
            import httpx

            r = httpx.get("https://serpapi.com/account.json", params={"api_key": s.serpapi_key}, timeout=20)
            j = r.json()
            ok = r.status_code == 200 and "error" not in j
            row("serpapi account", ok, f"plan={j.get('plan_name')} searches_left={j.get('total_searches_left')}" if ok else str(j)[:120])
        except Exception as e:  # noqa: BLE001
            row("serpapi account", False, f"{type(e).__name__}: {e}")
    try:
        from .chain.registry import connect

        w3 = connect(s)
        row("RPC", True, f"{getattr(w3.provider, 'endpoint_uri', None) or type(w3.provider).__name__} chain={w3.eth.chain_id} block={w3.eth.block_number}")
        if s.private_key:
            from eth_account import Account

            acct = Account.from_key(s.private_key)
            bal = float(w3.from_wei(w3.eth.get_balance(acct.address), "ether"))
            need = 0.004 if s.contract_address else 0.02
            faucet = PRESETS.get(s.chain or "sepolia", {}).get("faucet", "")
            row("wallet", bal >= need, f"{acct.address}  {bal:.5f} ETH" + ("" if bal >= need else f"  <- needs ~{need} ETH for this run: {faucet}"))
        else:
            row("wallet", False, "PRIVATE_KEY missing - run: python -m verified.cli wallet new")
        if s.contract_address:
            code = w3.eth.get_code(w3.to_checksum_address(s.contract_address))
            row("contract", len(code) > 0, f"{s.contract_address} ({len(code)} bytes)" if len(code) else f"no code at {s.contract_address} on this chain - run: python -m verified.cli deploy")
        else:
            row("contract", False, "CONTRACT_ADDRESS empty - will auto-deploy on first anchor (or run: deploy)", warn=True)
    except Exception as e:  # noqa: BLE001
        row("RPC", False, str(e)[:200])
    console.print(t)


@app.command()
def models():
    """Download the InsightFace buffalo_l ONNX models."""
    from .face.models import ensure_models

    d = ensure_models(lambda m: console.print(f"[dim]{m}[/dim]"))
    console.print(f"[green]models ready:[/green] {d}")


@wallet_app.command("new")
def wallet_new(write_env: bool = typer.Option(True, help="append PRIVATE_KEY to .env if not set")):
    """Generate a throwaway testnet wallet."""
    from .chain.registry import new_wallet

    addr, key = new_wallet()
    console.print(f"address     {addr}\nprivate key {key}")
    from .chain.presets import PRESETS as _P

    faucet = _P.get(_settings().chain or "sepolia", {}).get("faucet", "")
    if write_env:
        if _settings().private_key:
            console.print("[yellow].env already has a PRIVATE_KEY - not overwriting (pass --no-write-env to silence)[/yellow]")
        else:
            from .pipeline import _persist_env

            _persist_env("PRIVATE_KEY", key)
            console.print(f"[green]PRIVATE_KEY written to {ROOT / '.env'}[/green]")
    console.print(f"Fund it (needs ~0.02 ETH for deploy + anchor + attestation): {faucet}")


@wallet_app.command("show")
def wallet_show():
    s = _settings()
    from eth_account import Account

    if not s.private_key:
        console.print("[red]PRIVATE_KEY not set - run: python -m verified.cli wallet new[/red]")
        raise typer.Exit(1)
    acct = Account.from_key(s.private_key)
    console.print(acct.address)


@app.command()
def deploy():
    """Deploy VerifiedRegistry and persist CONTRACT_ADDRESS in .env."""
    from .chain.registry import Registry
    from .pipeline import _persist_env

    s = _settings()
    reg = Registry(s, log=lambda m: console.print(f"[dim]{m}[/dim]"))
    console.print(f"deployer {reg.address} balance {reg.balance_eth():.5f} ETH")
    res = reg.deploy()
    _persist_env("CONTRACT_ADDRESS", res.contract_address)
    console.print(f"[bold green]VerifiedRegistry deployed:[/bold green] {res.contract_address}\n{reg.explorer_address(res.contract_address)}\ntx {reg.explorer_tx(res.tx_hash)}")


@app.command()
def run(
    image: Path = typer.Option(..., exists=True, help="input face photo"),
    source: str = typer.Option("upload", help="upload|webcam (affects which query variants are searched)"),
    no_anchor: bool = typer.Option(False, help="stop after search (no blockchain write)"),
    match: int = typer.Option(None, help="anchor this match index instead of the automatic best pick"),
):
    """Full pipeline on an image file."""
    from .pipeline import Pipeline

    p = Pipeline(_settings(), emit=_emit_console)
    t0 = time.time()
    summary = p.scan(image.read_bytes(), source=source)
    if summary["status"] != "matches":
        console.print(f"[red]status: {summary['status']}[/red] - nothing to anchor. Run dir: runs/{summary['run_id']}")
        raise typer.Exit(2)
    _print_matches(summary["search"]["matches"])
    if no_anchor:
        console.print(f"run saved: runs/{summary['run_id']}  (anchor later with: python -m verified.cli anchor --run {summary['run_id']} --match N)")
        return
    summary = p.anchor(summary["run_id"], match)
    console.print(f"[bold]done in {time.time() - t0:.1f}s → runs/{summary['run_id']}[/bold]")


@app.command()
def anchor(run: str = typer.Option(..., help="run id"), match: int = typer.Option(None, help="match index")):
    """Anchor a previously scanned run."""
    from .pipeline import Pipeline

    Pipeline(_settings(), emit=_emit_console).anchor(run, match)


@app.command()
def verify(run: str = typer.Option(..., help="run id (folder under runs/)"), bundle: Path = typer.Option(None, help="verify this bundle file instead of the stored one"), no_refetch: bool = typer.Option(False)):
    """Independently re-verify a run against the on-chain record."""
    from .pipeline import Pipeline

    p = Pipeline(_settings(), emit=_emit_console)
    rep = p.verify_run(run, refetch=not no_refetch, bundle_override=bundle.read_bytes() if bundle else None)
    raise typer.Exit(0 if rep["verdict"] == "VERIFIED" else 1)


@app.command()
def tamper(run: str = typer.Option(..., help="run id"), field: str = typer.Option("match.similarity", help="dotted bundle field to alter")):
    """Demo: alter one field of the evidence bundle and show verification failing."""
    from .pipeline import Pipeline

    s = _settings()
    run_dir = s.runs_dir / run
    bundle = json.loads((run_dir / "bundle.json").read_bytes().decode("utf-8"))
    node = bundle
    parts = field.split(".")
    try:
        for k in parts[:-1]:
            node = node[k]
        old = node[parts[-1]]
    except (KeyError, TypeError):
        console.print(f"[red]unknown bundle field {esc(field)}[/red]")
        raise typer.Exit(2)
    node[parts[-1]] = (old + 0.0001) if isinstance(old, (int, float)) and not isinstance(old, bool) else (str(old) + "x")
    from .chain.evidence import canonical_bytes

    tampered = canonical_bytes(bundle)
    (run_dir / "bundle.tampered.json").write_bytes(tampered)
    console.print(f"[yellow]altered {esc(field)}: {esc(repr(old))} → {esc(repr(node[parts[-1]]))}  (saved bundle.tampered.json)[/yellow]")
    p = Pipeline(s, emit=_emit_console)
    rep = p.verify_run(run, refetch=False, bundle_override=tampered)
    raise typer.Exit(0 if rep["verdict"] == "TAMPERED" else 1)


@app.command("verify-bundle")
def verify_bundle(
    bundle: Path = typer.Option(..., exists=True, help="evidence bundle JSON"),
    contract: str = typer.Option(..., help="VerifiedRegistry address"),
    record: int = typer.Option(..., help="on-chain record id"),
    chain: str = typer.Option("sepolia", help="chain preset"),
    rpc: str = typer.Option("", help="override the preset RPC URL"),
    field: str = typer.Option("match.url", help="field to prove on-chain"),
):
    """Verify a bundle against the chain with nothing else - no runs/ folder, no wallet.

    This is the third-party path: hand someone the bundle plus the contract
    address and record id and they can check it themselves."""
    import subprocess

    script = ROOT / "verify_standalone.py"
    cmd = [sys.executable, str(script), "--bundle", str(bundle), "--contract", contract, "--record", str(record), "--chain", chain, "--field", field]
    if rpc:
        cmd += ["--rpc", rpc]
    raise typer.Exit(subprocess.call(cmd))


@app.command()
def serve(host: str = typer.Option(None), port: int = typer.Option(None), reload: bool = False):
    """Start the local web UI."""
    import uvicorn

    s = _settings()
    uvicorn.run("verified.server:app", host=host or s.host, port=port or s.port, reload=reload, log_level="info")


def _print_matches(matches: list[dict]) -> None:
    t = Table(title="verified matches", box=box.SIMPLE_HEAVY)
    t.add_column("#")
    t.add_column("sim")
    t.add_column("band")
    t.add_column("platform")
    t.add_column("post?")
    t.add_column("engines")
    t.add_column("link")
    for i, m in enumerate(matches[:15]):
        t.add_row(str(i), f"{m['similarity']:.3f}", m["band"], esc(m["platform"]), "✓" if m["is_post"] else "", ",".join(e.replace("google_", "g_") for e in m["engines"]), esc(m["link"][:70]))
    console.print(t)


if __name__ == "__main__":
    app()
