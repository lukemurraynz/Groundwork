#!/usr/bin/env bash
set -euo pipefail

# The repo is mounted as root-owned from the container's perspective on some hosts;
# without safe.directory every git command fails with "detected dubious ownership".
workspace_folder="${containerWorkspaceFolder:-${PWD}}"
git config --global --add safe.directory "$workspace_folder"
echo "Git safe.directory configured: $workspace_folder"
