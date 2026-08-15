#!/usr/bin/env bash
# seed.sh — Bash wrapper for seed.py
# Usage: ./scripts/seed.sh [--scramble] [--host http://localhost:8000]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

echo "🌱 Seeding Cloud Resource Allocation Engine..."
python scripts/seed.py "$@"
