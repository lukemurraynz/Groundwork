#!/usr/bin/env sh
# Groundwork predown hook (POSIX equivalent of predown.ps1). See predown.ps1 for rationale.

set -u

client_id="${GROUNDWORK_ENTRA_APP_CLIENT_ID:-}"
if [ -z "$client_id" ]; then
    echo "Groundwork predown: no GROUNDWORK_ENTRA_APP_CLIENT_ID recorded, nothing to remove."
    exit 0
fi

echo "Groundwork predown: removing Entra application appId=${client_id}"

if ! az ad app show --id "$client_id" --query appId -o tsv --only-show-errors >/dev/null 2>&1; then
    echo "  application already gone, nothing to do"
    exit 0
fi

if ! az ad app delete --id "$client_id" --only-show-errors 2>/dev/null; then
    # Non-fatal: this must not block azd down from removing the resource group.
    echo "WARNING: failed to delete Entra application appId=${client_id}. Remove it manually: az ad app delete --id ${client_id}" >&2
    exit 0
fi

echo "  removed"
