#!/usr/bin/env bash
set -euo pipefail

# Fails the create step loudly when the restored environment cannot do its job,
# so breakage surfaces once at container creation instead of per developer.
UV_BIN="$HOME/.local/bin/uv"

echo "--- Tool versions ---"
"$UV_BIN" --version
python3 --version
az version > /dev/null && echo "Azure CLI: $(az version | grep -o '\"azure-cli\": \"[^\"]*\"')"
echo "azd: $(azd version 2>/dev/null | head -n1)"

echo "--- Lint (ruff) ---"
"$UV_BIN" run ruff check src tests

echo "--- Test suite (pytest) ---"
"$UV_BIN" run python -m pytest tests -q --no-header 2>&1 | tail -n 5

echo "Stack smoke test passed."
