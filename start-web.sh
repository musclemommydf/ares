#!/bin/bash
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

# Ares — Open web browser UI
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/backend/.venv/bin/activate"
cd "$SCRIPT_DIR/backend"
uvicorn app.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!
sleep 2
if command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:3000
elif command -v open &>/dev/null; then
    open http://localhost:3000
fi
cd "$SCRIPT_DIR/frontend" && npx vite preview --port 3000
kill $BACKEND_PID 2>/dev/null || true
