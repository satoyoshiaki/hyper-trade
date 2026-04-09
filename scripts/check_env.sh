#!/usr/bin/env bash
# Validate environment and dependencies before starting the bot.

set -euo pipefail

echo "=== Hyperliquid Bot Environment Check ==="
echo ""

# Python version
PYTHON=$(python3 --version 2>&1)
echo "[OK] Python: $PYTHON"

# Check .env
if [ ! -f ".env" ]; then
    echo "[FAIL] .env not found. Copy .env.example to .env."
    exit 1
fi
echo "[OK] .env exists"

# Check required env vars
check_var() {
    if grep -q "^${1}=your_" .env 2>/dev/null || ! grep -q "^${1}=" .env 2>/dev/null; then
        echo "[WARN] ${1} looks like placeholder or missing"
    else
        echo "[OK] ${1} set"
    fi
}

check_var PRIVATE_KEY
check_var WALLET_ADDRESS

# Check TESTNET
TESTNET=$(grep "^TESTNET=" .env | cut -d= -f2)
if [ "$TESTNET" = "true" ]; then
    echo "[OK] TESTNET=true (safe mode)"
else
    echo "[WARN] TESTNET is not 'true' — MAINNET mode! Confirm this is intentional."
fi

# Check packages
python3 -c "import hyperliquid" 2>/dev/null && echo "[OK] hyperliquid-python-sdk installed" || echo "[FAIL] hyperliquid-python-sdk not installed — run: pip install hyperliquid-python-sdk"
python3 -c "import fastapi" 2>/dev/null && echo "[OK] fastapi installed" || echo "[FAIL] fastapi not installed"
python3 -c "import uvicorn" 2>/dev/null && echo "[OK] uvicorn installed" || echo "[FAIL] uvicorn not installed"
python3 -c "import pydantic_settings" 2>/dev/null && echo "[OK] pydantic-settings installed" || echo "[FAIL] pydantic-settings not installed"

echo ""
echo "=== Check complete ==="
