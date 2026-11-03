#!/usr/bin/env bash
# Start the Gambles dashboard at http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")"
exec uv run python -m app.main "$@"
