# Screen-recording script (≈ 4–6 minutes, unedited)

Goal of the recording: show **face scan → real social post found → on-chain anchor → independent re-verification (incl. a tamper test)** in one continuous take.

## Before you hit record

1. `.env` has `SERPAPI_KEY`, `PRIVATE_KEY` (funded with **≥ 0.02 Sepolia ETH** — deploy + EAS schema + anchor + attest is four transactions), `CONTRACT_ADDRESS` (run `python -m verified.cli deploy` once — you can also show the deploy in the recording), optionally `GOOGLE_VISION_API_KEY` and `PINATA_JWT`.
2. Run `python -m verified.cli doctor` — everything green.
3. Pick the subject:
   * **Primary:** a teammate with public photos (public LinkedIn profile picture, public Instagram/X posts). Do one dry run *before* recording (it costs ~4 SerpApi searches) to confirm verified matches come back. If the teammate's footprint is too small, fall back to:
   * **Fallback:** a photo of a well-known public figure (upload mode). Verified Instagram/X/Facebook matches are essentially guaranteed.
4. Open a second browser tab on `https://sepolia.etherscan.io` (and `https://sepolia.easscan.org`) ready to paste hashes.
5. Terminal in the repo with the venv active, font size large.

## Take

**0:00 — Terminal.** `python -m verified.cli doctor` → show models, SerpApi account (searches left), RPC block, wallet balance, contract bytes. One sentence: "Real engines, real chain, no mocks."

**0:30 — UI.** `python -m verified.cli serve`, open `http://127.0.0.1:8000`. Point at the header pills: chain + block, wallet balance, engines enabled.

**0:50 — Face scan.** *Start camera*. Show the detection corners, landmarks, quality bars. Turn head left, then right — liveness dot turns green, capture button unlocks. Say: "A photo held to the camera won't pass this." Click **Capture & search**.

**1:20 — Search.** Narrate what streams in: engine chips (Google Lens face crop, Yandex, Vision, …), candidate faces appearing — greens are biometrically the same person, greys are look-alikes rejected by ArcFace. Point at a rejected one: "Reverse image search says *similar*; our model says *not you*. Only verified faces go further." Inferred identity chip appears; name-expansion sweep adds Instagram/LinkedIn/X results, also face-verified.

**2:30 — Match.** Verified matches list: platform badge, post badge, cosine gauge. Click the link to open the actual post in a new tab for two seconds — it's real.

**2:50 — Anchor.** Watch the rail: Evidence (bundle + Merkle root) → IPFS (CID) → Anchor (tx sent … mined) → EAS → Bitcoin/OTS → Re-verify. Receipt card fills: record #, tx, contract, recordHash, merkleRoot, faceCommitment, contentHash, CID. Scan/click the QR or link → **Etherscan** tab: show the transaction, the `Anchored` event log, the contract. Open the **EAS** link: show the attestation with decoded fields.

**3:50 — Independent re-verification.** Scroll to the checklist: every check PASS, verdict **VERIFIED**. Read two of them aloud: "recomputed keccak256(bundle) found on-chain", "on-chain Merkle proof for match.url accepted by the contract", "live post image bytes identical — no content drift".

**4:20 — Tamper test.** Click **Tamper test**. One field (similarity) is changed by 0.0001 in the local bundle → record_hash, merkle_root, similarity, merkle_proof turn **FAIL**, verdict **TAMPERED**, with on-chain vs local hash shown. Click **Re-verify** → back to VERIFIED.

**4:40 — Reload-proof (nice touch).** Refresh the page: the ledger at the bottom lists every anchor;
click a run id to reopen it (the URL keeps `#run=<id>`), and Re-verify / Tamper test work on it again —
useful if you navigate away to Etherscan mid-demo.

**4:50 — CLI proof (optional but strong).** In the terminal:
```
python -m verified.cli verify --run <run_id>
python -m verified.cli tamper --run <run_id> --field match.url
```
Show the exit code / verdict. Show the `runs/<run_id>/` folder: `bundle.json`, `anchor.json`,
`raw/google_lens_face.json` (the genuine API response), `ots/bundle.json.ots`, `verification.json`.
If you have 20 more seconds, `python -m pytest tests -q` runs 31 tests (real models, real contract on
an in-process chain, no keys needed) — good evidence that the pipeline is not held together with tape.

**5:20 — Close.** One line on privacy: "Nothing biometric is on-chain — only an HMAC commitment; the evidence is content-addressed and verifiable by anyone with the bundle."

## If something goes wrong live

* *No verified matches:* switch to upload mode with the fallback photo. Nothing was anchored — that is the correct behaviour, say so.
* *Tx pending long:* Sepolia can take 30 s; the rail shows "chain: running" with the tx hash in the log. Keep narrating the evidence bundle.
* *An engine chip shows ✕:* the others carry on; mention graceful degradation.
* *Camera blocked:* use **Upload photo** — the pipeline is identical.
