#!/bin/bash
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

# Ares — Start backend server
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/backend/.venv/bin/activate"
cd "$SCRIPT_DIR/backend"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 "$@"
