#!/usr/bin/env bash
set -euo pipefail

# Thin orchestrator: runs every script under post-start.d/ in sorted order.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/run-scripts.sh"

run_script_directory "$SCRIPT_DIR/post-start.d"
