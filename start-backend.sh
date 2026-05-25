#!/bin/bash
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

# Ares — Start backend server
#
# HOST / PORT override the bind (default 0.0.0.0:8000). For HTTPS without a
# reverse proxy (Track D, D2.5), set ARES_TLS_CERT + ARES_TLS_KEY to PEM paths;
# uvicorn then serves TLS directly and the UI auto-uses wss:// for sockets.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/backend/.venv/bin/activate"
cd "$SCRIPT_DIR/backend"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

SSL_ARGS=()
if [ -n "${ARES_TLS_CERT:-}" ] && [ -n "${ARES_TLS_KEY:-}" ]; then
  SSL_ARGS=(--ssl-certfile "$ARES_TLS_CERT" --ssl-keyfile "$ARES_TLS_KEY")
  echo "Ares: HTTPS enabled (cert: $ARES_TLS_CERT)"
fi

exec uvicorn app.main:app --host "$HOST" --port "$PORT" "${SSL_ARGS[@]}" "$@"
