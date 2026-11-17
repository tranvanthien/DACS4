#!/usr/bin/env bash
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

if [ -f "venv/bin/python" ]; then
    venv/bin/python scripts/backup_db.py "$@"
else
    python3 scripts/backup_db.py "$@"
fi
