#!/usr/bin/env bash
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares
#
# Build the optional Rust acceleration extension (Track D, D4) into the backend
# venv. Ares runs fine without it — app.core.native falls back to pure Python —
# so this is opt-in, for after a hot path has been promoted into backend/native.
#
#   ./scripts/build-native.sh
#
# Requires the Rust toolchain (rustup). Installs maturin into the venv if needed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

"$PY" -m pip install --quiet 'maturin>=1.5,<2'
cd "$ROOT/backend/native"
exec "$PY" -m maturin develop --release
