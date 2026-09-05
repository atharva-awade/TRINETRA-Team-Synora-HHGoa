# SETUP — from zero to a recorded demo

Written for Windows 11; the macOS/Linux differences are noted inline. Total time
about 25 minutes, most of it a one-off 275 MB model download.

Verified on a clean install: Python 3.11, web3 7.16, numpy 2.4, onnxruntime 1.29 —
all 35 tests pass on that stack.

---

## 0. What you need (and what you don't)

| | what | required? | cost | where |
|---|---|---|---|---|
| 1 | **SerpApi key** | **yes** — this is the search half of the pipeline | free, 250 searches/month | https://serpapi.com |
| 2 | **Sepolia test ETH** | **yes** — this is the chain half | free from a faucet | see step 4 |
| 3 | Google Cloud Vision API key | optional | free tier 1 000 units/month | https://console.cloud.google.com |
| 4 | Pinata JWT | optional | free tier | https://app.pinata.cloud |
| 5 | Bluesky | no key at all | free | public API, already on |
| 6 | OpenTimestamps (Bitcoin) | no key at all | free | public calendars, already on |

So: **two things are mandatory — one API key and one faucet drip.** Everything else
either needs no key or degrades gracefully (the pipeline tells you what is off and
keeps going).

A run uses about **4 SerpApi searches**, so the free tier is ~60 runs a month.

---

## 1. Python

Install **Python 3.11 or 3.12** from https://www.python.org/downloads/ and tick
**“Add python.exe to PATH”** on the first screen.

> Avoid 3.13+ for now: some wheels lag. Do not use the Microsoft Store stub.

Check it:

```bat
python --version
```

If that prints nothing, close and reopen the terminal, or use `py -3.11 --version`.

---

## 2. Install the project

Unzip the project, open **Command Prompt** (or PowerShell) in that folder, and run:

```bat
run.bat
```

That single script creates a virtual environment, installs the dependencies,
copies `.env.example` to `.env` if needed, downloads the face models (~275 MB,
once), runs `doctor`, and starts the UI.

Prefer doing it by hand, or on macOS/Linux (`./run.sh` does the same):

```bat
python -m venv .venv
.venv\Scripts\activate                REM macOS/Linux:  source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env                REM macOS/Linux:  cp .env.example .env
python -m verified.cli models
```

Nothing here needs a C++ compiler: the face models run straight on ONNX Runtime.

---

## 3. The SerpApi key (5 minutes)

1. Sign up at **https://serpapi.com/users/sign_up** (Google sign-in works).
2. Open **https://serpapi.com/manage-api-key** and copy the *Private API Key*.
3. Open `.env` in Notepad and put it on the `SERPAPI_KEY=` line:

```ini
SERPAPI_KEY=your_key_here
```

That one key powers four engines: Google Lens, Google Reverse Image, Yandex
Images, and the name-based social sweep.

> `.env` is in `.gitignore`, so your keys never reach GitHub. Keep it that way, and
> never put a wallet key with real value in it.

---

## 4. Fund the demo wallet (2 minutes)

The project ships with a **throwaway testnet wallet** already in `.env`. Get its
address:

```bat
python -m verified.cli wallet show
```

Then send it free Sepolia ETH from **https://cloud.google.com/application/web3/faucet/ethereum/sepolia**
(paste the address, sign in with Google, 0.05 ETH per day). Any Sepolia faucet works.

**You need ≥ 0.02 ETH** because a first full run is four transactions: deploy the
registry, register the EAS schema (once ever), anchor the record, publish the
attestation. Later runs cost about 0.001.

Want your own wallet instead? `python -m verified.cli wallet new` prints a fresh
one and writes it to `.env` (it refuses to overwrite an existing key).

---

## 5. Optional keys (skip these if you're short on time)

**Google Cloud Vision** — adds a fifth search engine and the best name hints:

1. https://console.cloud.google.com → create/select a project.
2. **APIs & Services → Library →** search “Cloud Vision API” → **Enable**.
3. **APIs & Services → Credentials → Create credentials → API key** → copy it.
4. `.env`: `GOOGLE_VISION_API_KEY=your_key`

(An API key is enough — no service-account JSON needed.)

**Pinata** — actually pins the evidence bundle to IPFS instead of only computing
its CID locally:

1. https://app.pinata.cloud → **API Keys → New Key** → tick admin → create.
2. Copy the **JWT** (the long one, not the api key/secret).
3. `.env`: `PINATA_JWT=eyJ...`

---

## 6. Check everything before you rely on it

```bat
python -m verified.cli doctor
```

Every row should be OK (Vision/Pinata show WARN if you skipped them — that is fine).
Then prove the outside world actually answers:

```bat
python -m verified.cli doctor --probe
```

This makes one real call to each service — image host, SerpApi upload, Google Lens,
Yandex, Vision, Bluesky, Pinata, OpenTimestamps — and prints the raw error for
anything that fails. It costs about 2 searches and takes ~15 seconds. **Do this
before recording.**

---

## 7. Deploy the contract (once, ~20 seconds)

```bat
python -m verified.cli deploy
```

It prints the address and the Etherscan link, and writes `CONTRACT_ADDRESS` into
`.env` so every later run reuses it. (Skipping this is fine too — the first anchor
deploys automatically — but doing it now makes the recording shorter.)

---

## 8. First real run (do this before recording)

Start with a photo of a well-known public figure, and stop before the chain step:

```bat
python -m verified.cli run --image C:\path\to\photo.jpg --no-anchor
```

You should see engines reporting candidates, then green lines as faces are
biometrically verified, then a table of verified matches. If you get zero matches,
the person's photos are not indexed — try a more public subject.

Then the full pipeline:

```bat
python -m verified.cli run --image C:\path\to\photo.jpg
```

And the UI, which is what you want on camera:

```bat
python -m verified.cli serve
```

Open **http://127.0.0.1:8000** in Chrome or Edge. Allow the camera when asked
(the browser only allows the camera on `localhost` or HTTPS — `127.0.0.1` is fine).

---

## 9. Record it

Follow **DEMO.md** — a minute-by-minute script for a 4–6 minute unedited take:
doctor → live face scan with liveness → search with rejected look-alikes →
anchor → Etherscan/EAS → re-verification → tamper test.

Record with **Win + Alt + R** (Xbox Game Bar) or OBS. Upload to YouTube as
*unlisted* or Google Drive with link-sharing on, and check the link in a private
window before submitting.

---

## 10. Push to GitHub

```bat
git init                                   REM only if the folder is not a repo yet
git add -A
git commit -m "VERIFIED - face scan to social post to blockchain pipeline"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

`.env`, `runs/` and `models/` are gitignored — no keys, no evidence folders, no
275 MB of weights in the repo.

Then submit the repo link plus the recording link at
**https://forms.gle/oZbQGuwiNeHVcHWo8** (no resubmissions, so check both links first).

---

## Troubleshooting

**`python` not recognised** — PATH wasn't ticked during install. Reinstall and tick it, or use `py -3.11` everywhere.

**`doctor` says RPC unreachable** — a public Sepolia node is down or blocked; the app already tries four. Try again, or set `RPC_URL=` to an Alchemy/Infura Sepolia URL.

**`transaction simulation failed (insufficient funds?)`** — the faucet hasn't landed. Check the balance row in `doctor`.

**`No contract code at 0x… on chain 11155111`** — `CONTRACT_ADDRESS` in `.env` is from another chain. Blank it and run `deploy`.

**Camera does nothing** — another app holds it (Teams/Zoom), or Windows privacy settings block it: *Settings → Privacy & security → Camera → let desktop apps use the camera*. Use **upload photo** instead; the pipeline is identical.

**Capture button stays disabled** — that is the liveness gate: turn your head left, then right, until the dot goes green, and keep the quality bar above ~0.55.

**Search returns nothing** — check `doctor --probe` first. If Lens works but nothing verifies, the subject simply isn't indexed publicly; nothing gets anchored, which is the intended behaviour.

**Windows Defender/SmartScreen warning on run.bat** — right-click → Properties → Unblock, or run the commands from step 2 by hand.

**Still stuck** — every run writes `runs/<run_id>/` with the raw API responses in `raw/`. That folder plus the terminal output is everything needed to diagnose.
