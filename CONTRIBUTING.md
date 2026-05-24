# Contributing to Ares

Thanks for your interest in Ares — an air-gappable RF propagation, geolocation, and
passive-observation platform. This guide covers how to set up a dev environment, the
checks CI runs, and a few house rules specific to this project. Ares is in **alpha**;
expect rough edges and moving parts.

## License & inbound contributions

Ares is dual-licensed under **MIT OR Apache-2.0** (see [`LICENSE-MIT`](LICENSE-MIT) and
[`LICENSE-APACHE`](LICENSE-APACHE)). Unless you explicitly state otherwise, any
contribution you intentionally submit for inclusion is **dual-licensed under those same
terms**, with no additional conditions. You confirm you have the right to contribute the
code under that license (don't paste in code you don't have the rights to).

## Scope & responsible use

Ares is built for **lawful, passive** RF observation, propagation planning, and
geolocation. Contributions must keep it that way:

- **Passive only.** No transmitters/jammers, no IMSI-catcher behaviour, no breaking
  encryption or privacy protections. Demod/decode paths operate on signals already
  in the clear (e.g. unencrypted control channels, published de-obfuscation of public
  broadcast formats like DroneID) — not on protecting users' private comms.
- **No detection-evasion or offensive tooling.** Defensive, research, and situational-
  awareness use cases only.

PRs that move Ares outside this scope will be declined regardless of code quality.

## Project layout

| Path | What it is |
|------|------------|
| `backend/` | FastAPI app + all DSP/DF/propagation (Python 3.12, numpy/scipy in-process) |
| `frontend/` | React + Vite SPA (Node 20) |
| `mobile/` | Expo / React Native client |
| `electron/` | Desktop shell |
| `atak-plugin/` | ATAK-CIV plugin (Kotlin / Gradle) |
| `scripts/` | Dev & install helpers |
| `docs/` | Deployment, remote-access, build-plan notes |

## Dev environment

**Backend** (Python 3.12):

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Frontend** (Node 20):

```bash
cd frontend
npm ci          # or: npm install
```

**Run it locally** (backend on `127.0.0.1:8000`, frontend served on `3000`):

```bash
./start-web.sh
# or, for live-reload dev:
cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000   # terminal 1
cd frontend && npm run dev                                        # terminal 2
```

Bind the backend to **loopback** by default — binding a non-loopback address flips auth on
and will 401 the dev UI unless you configure `ARES_AUTH`. See [`docs/REMOTE.md`](docs/REMOTE.md)
for intentional remote access.

## Before you open a PR — run the checks CI runs

CI (`.github/workflows/ci.yml`) runs three jobs. Reproduce them locally so your PR goes
green on the first push:

**1. License headers** — every source file needs the SPDX header:

```bash
python3 scripts/check_license_headers.py          # check
python3 scripts/check_license_headers.py --fix    # auto-insert any missing
```

New source files (`.py .js .jsx .ts .tsx .mjs .cjs .kt .sh .gradle .css`) must start with:

```
SPDX-License-Identifier: MIT OR Apache-2.0
Copyright (c) 2026 Ares
```

(after any shebang). The `--fix` flag handles placement for you.

**2. Backend** — compile, import, and the validation harness:

```bash
cd backend
python -m compileall -q app tests
python -c "from app.main import app; print(len(app.routes), 'routes OK')"
python -m tests.test_validation        # ITM / DF / TDOA / SGP4 / HF / RID / security …
```

The validation harness must stay **green** (`N passed, 0 failed`). If you change DSP/DF/
propagation maths, add or update a check in `backend/tests/test_validation.py` that pins
the expected result — ideally against a closed-form reference or measured anchor.

**3. Frontend** — unit tests + build (what CI gates on):

```bash
cd frontend
node --test tests/      # polar patterns, LoB maths, …
npm run build
npm run lint            # recommended locally; not a CI gate (yet)
```

## House rules (the non-obvious ones)

These reflect deliberate architecture decisions — please honour them:

- **DSP/DF/IQ stays local and real.** All signal processing runs in-process with
  numpy/scipy. No cloud DSP, no network round-trips for maths, and **no stubs that pretend
  to compute** — if something can't be done offline, it should say so, not fake a result.
- **Never fabricate live data.** Synthetic/demo data is allowed **only** as a clearly
  flagged offline fallback (e.g. `_synthetic` markers, `status: "no_capture"`). A live
  capture path that has no real data must report that honestly (empty / `null`), never
  inject a plausible-looking fake emitter, fix, or beacon into the map/CoT/results.
- **Keyed & external feeds never fake.** OSINT/keyed sources without credentials report
  `unavailable` with a signup link — they don't return placeholder data.
- **Keep GPL isolated.** GNU Radio / gr-gsm (GPL-3) are optional, guarded runtime imports
  confined to `backend/app/core/sdr/cellular/`. Don't add GPL/copyleft dependencies
  elsewhere, and don't bundle them — they're installed separately by the operator so the
  rest of Ares stays MIT/Apache-clean. See [`NOTICE`](NOTICE).
- **Match the surrounding code.** Mirror existing naming, comment density, and idioms in
  the file you're editing. Python uses type hints and `from __future__ import annotations`.

## Commits & pull requests

- **Conventional Commits** with a scope, matching existing history:
  `feat(sdr): …`, `fix(rid): …`, `perf(map): …`, `test(decode): …`, `ci: …`, `chore: …`.
- Keep commits **focused** — one logical change each; don't fold unrelated churn
  (formatting, settings files) into a feature commit.
- Open a PR against `master` from a topic branch (or your fork). In the description, say
  **what** changed and **why**, and note how you verified it (which checks you ran).
- Don't commit secrets, API keys, large binaries, or generated output.

## Reporting bugs & security issues

- **Bugs / features:** open a GitHub issue with steps to reproduce, your OS + SDR
  hardware, and relevant logs.
- **Security:** for anything sensitive (auth bypass, RCE, data exposure), please report it
  privately to the maintainer rather than opening a public issue, and allow time for a fix
  before disclosure.

Thanks for contributing! 🛰️
