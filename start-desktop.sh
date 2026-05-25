#!/bin/bash
# Ares — Start the Tauri (Rust) desktop app. The binary spawns the backend itself.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$SCRIPT_DIR/src-tauri/target/release/ares-desktop"
if [ -x "$BIN" ]; then
    exec "$BIN" "$@"
fi
# Fallback when the prebuilt binary is missing: build+run from source.
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
cd "$SCRIPT_DIR/src-tauri"
exec cargo run --release "$@"
