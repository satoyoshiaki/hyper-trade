#!/usr/bin/env bash
# Start the Hyperliquid maker bot.
# Requires .env to be configured. Run from the project root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

if [ ! -f ".env" ]; then
    echo "ERROR: .env file not found. Copy .env.example and fill in your values."
    exit 1
fi

echo "Starting Hyperliquid maker bot..."
echo "Testnet mode: $(grep -i TESTNET .env | head -1 | cut -d= -f2)"
echo ""

python -m app.main
