"""OpenTimestamps: free, keyless anchoring of the record hash into Bitcoin.

Calendar servers aggregate submitted digests into a Merkle tree and commit the
root in a Bitcoin transaction (usually within a few hours).  We store the
.ots proof next to the bundle; `upgrade()` later fetches the Bitcoin block
attestation so the proof becomes independently verifiable against the
Bitcoin blockchain - a second, independent chain anchoring the same record.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

CALENDARS = [
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
    "https://ots.btc.catallaxy.com",
]


def stamp(data: bytes, out_path: Path, calendars: list[str] | None = None, timeout: float = 20) -> dict:
    from opentimestamps.calendar import RemoteCalendar
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

    digest = hashlib.sha256(data).digest()
    ts = Timestamp(digest)
    used, errors = [], []
    for url in calendars or CALENDARS:
        try:
            cal_ts = RemoteCalendar(url, user_agent="verified-face-chain/1.0").submit(digest, timeout=timeout)
            ts.merge(cal_ts)
            used.append(url)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {type(e).__name__}: {str(e)[:80]}")
        if len(used) >= 2:
            break
    if not used:
        raise RuntimeError("no OpenTimestamps calendar reachable: " + "; ".join(errors))
    dtf = DetachedTimestampFile(OpSHA256(), ts)
    ctx = BytesSerializationContext()
    dtf.serialize(ctx)
    out_path.write_bytes(ctx.getbytes())
    return {"ots_file": str(out_path), "sha256": digest.hex(), "calendars": used, "status": "pending", "errors": errors}


def status(ots_path: Path) -> dict:
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
    from opentimestamps.core.serialize import BytesDeserializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile

    dtf = DetachedTimestampFile.deserialize(BytesDeserializationContext(ots_path.read_bytes()))
    pending, bitcoin = [], []
    for _msg, att in dtf.timestamp.all_attestations():
        if isinstance(att, PendingAttestation):
            pending.append(att.uri)
        elif isinstance(att, BitcoinBlockHeaderAttestation):
            bitcoin.append(att.height)
    return {"file_digest": dtf.file_digest().hex(), "pending_calendars": pending, "bitcoin_block_heights": bitcoin, "status": "confirmed" if bitcoin else "pending"}


def upgrade(ots_path: Path, timeout: float = 20) -> dict:
    """Ask the calendars whether the Bitcoin attestation is available yet and
    merge it into the proof file."""
    from opentimestamps.calendar import RemoteCalendar
    from opentimestamps.core.notary import PendingAttestation
    from opentimestamps.core.serialize import BytesDeserializationContext, BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile

    dtf = DetachedTimestampFile.deserialize(BytesDeserializationContext(ots_path.read_bytes()))
    upgraded = False
    for msg, att in list(dtf.timestamp.all_attestations()):
        if isinstance(att, PendingAttestation):
            try:
                new_ts = RemoteCalendar(att.uri, user_agent="verified-face-chain/1.0").get_timestamp(msg, timeout=timeout)
                # find the sub-timestamp holding this commitment and merge
                for sub in _walk(dtf.timestamp):
                    if sub.msg == msg:
                        sub.merge(new_ts)
                        upgraded = True
            except Exception:  # noqa: BLE001
                continue
    if upgraded:
        ctx = BytesSerializationContext()
        dtf.serialize(ctx)
        ots_path.write_bytes(ctx.getbytes())
    return {"upgraded": upgraded, **status(ots_path)}


def _walk(ts):
    yield ts
    for _op, child in ts.ops.items():
        yield from _walk(child)
