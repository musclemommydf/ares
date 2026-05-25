#!/usr/bin/env bash
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares
#
# Launch the Ares Tauri desktop shell (Track D / D3). The shell spawns the
# backend, waits for /api/v1/health, then opens the UI at 127.0.0.1:8000.
#
# Requires the Rust toolchain (rustup). `cargo run` needs no Tauri CLI; the CLI
# is only needed for icons (`cargo tauri icon ../frontend/public/icon.png`, once)
# and for building installers (`cargo tauri build`). Pass --release-free dev runs
# by exporting ARES_TAURI_DEV=1.
set -euo pipefail
cd "$(dirname "$0")/src-tauri"
if [ "${ARES_TAURI_DEV:-}" = "1" ]; then
  exec cargo run "$@"          # faster compile, unoptimized
fi
exec cargo run --release "$@"   # optimized desktop binary
