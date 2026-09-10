#!/usr/bin/env sh
# Groundwork postprovision hook (POSIX equivalent of postprovision.ps1).
#
# See postprovision.ps1 for the full rationale: this creates the multi-tenant Entra application
# (FR-006) idempotently, applies its app roles, ensures a service principal exists, and records the
# result as azd environment values. It requests no API permissions and runs no admin-consent flow —
# that remains a deliberate, separate decision.

set -eu

env_name="${AZURE_ENV_NAME:-}"
if [ -z "$env_name" ]; then
    echo "AZURE_ENV_NAME is not set; cannot derive a deterministic app display name." >&2
    exit 1
fi

display_name="Groundwork-${env_name}"
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
role_manifest_path="${script_dir}/../infra/app-roles.json"

echo "Groundwork postprovision: Entra application '${display_name}'"

existing=$(az ad app list --filter "displayName eq '${display_name}'" --query '[0]' -o json --only-show-errors 2>/dev/null || true)

if [ -n "$existing" ] && [ "$existing" != "null" ] && [ "$existing" != "[]" ]; then
    app_id=$(printf '%s' "$existing" | python3 -c "import sys,json; print(json.load(sys.stdin)['appId'])")
    echo "  found existing application appId=${app_id}, reusing"
else
    echo "  no existing application found, creating"
    created=$(az ad app create \
        --display-name "$display_name" \
        --sign-in-audience "AzureADMultipleOrgs" \
        --only-show-errors -o json)
    app_id=$(printf '%s' "$created" | python3 -c "import sys,json; print(json.load(sys.stdin)['appId'])")
    echo "  created appId=${app_id}"

    # api://<appId> requires no domain ownership or verification — the right choice while no
    # groundwork.* domain is owned (see the project's naming research (not included in this release)).
    audience="api://${app_id}"
    if ! az ad app update --id "$app_id" --identifier-uris "$audience" --only-show-errors 2>/dev/null; then
        echo "WARNING: failed to set identifier URI on the application; continuing without it." >&2
    fi
fi

echo "  applying app role definitions"
# @<path> file-reference form, verified directly against this tenant. Passing the JSON as an
# inline string argument is the less reliable path (see postprovision.ps1 for why the PowerShell
# version needed this same fix).
if ! az ad app update --id "$app_id" --app-roles "@${role_manifest_path}" --only-show-errors 2>/dev/null; then
    echo "WARNING: failed to apply app roles; the application exists but may be missing roles." >&2
fi

sp_id=$(az ad sp show --id "$app_id" --query id -o tsv --only-show-errors 2>/dev/null || true)
if [ -n "$sp_id" ]; then
    echo "  service principal already exists"
else
    echo "  creating service principal"
    sp_id=$(az ad sp create --id "$app_id" --query id -o tsv --only-show-errors 2>/dev/null || true)
    if [ -z "$sp_id" ]; then
        echo "WARNING: failed to create the service principal for this application." >&2
    fi
fi

# Grant the operator every app role — ported from postprovision.ps1, which carries the full
# rationale: TokenValidator defaults require_role=True, so a signed-in caller with zero app-role
# assignments fails EVERY authenticated route with a bare AuthenticationError no matter how
# correct their token otherwise is, and a fresh app registration assigns nobody any role by
# default. This block was previously ps1-only — a gap this fixes, not a new decision — so a
# Linux/Mac operator running azd up got a broken environment (every authenticated call failing)
# until they found and ran the equivalent Graph calls by hand. Scoped to AZURE_PRINCIPAL_ID (the
# same identity preprovision.sh already resolves for the AKS/Key Vault operator grants), all
# three roles (Operator/Approver/Requester): this is the operator's own environment, not a
# customer's, so the usual production least-privilege-per-role split doesn't apply the same way.
if [ -n "$sp_id" ] && [ -n "${AZURE_PRINCIPAL_ID:-}" ]; then
    echo "  granting operator (${AZURE_PRINCIPAL_ID}) all app roles"
    for role_id in 2e3f4a5b-6c7d-4e8f-9a0b-1c2d3e4f5a6b 1d2e3f4a-5b6c-4d7e-8f9a-0b1c2d3e4f5a 8f6a9c1e-2b3d-4e5f-9a1b-6c7d8e9f0a1b; do
        existing_count=$(az rest --method GET \
            --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${sp_id}/appRoleAssignedTo" \
            --query "value[?principalId=='${AZURE_PRINCIPAL_ID}' && appRoleId=='${role_id}'] | length(@)" \
            -o tsv --only-show-errors 2>/dev/null || true)
        if [ -z "$existing_count" ] || [ "$existing_count" = "0" ]; then
            role_body_path=$(mktemp)
            python3 -c "
import json
print(json.dumps({'principalId': '${AZURE_PRINCIPAL_ID}', 'resourceId': '${sp_id}', 'appRoleId': '${role_id}'}))
" > "$role_body_path"
            az rest --method POST \
                --uri "https://graph.microsoft.com/v1.0/users/${AZURE_PRINCIPAL_ID}/appRoleAssignments" \
                --headers 'Content-Type=application/json' --body "@${role_body_path}" \
                --only-show-errors >/dev/null 2>&1 || true
            rm -f "$role_body_path"
        fi
    done
else
    echo "WARNING: AZURE_PRINCIPAL_ID not resolved or no service principal; skipping operator app-role grant. Authenticated routes will fail for the operator with AuthenticationError until a role is assigned manually." >&2
fi

azd env set GROUNDWORK_ENTRA_APP_CLIENT_ID "$app_id"
azd env set GROUNDWORK_ENTRA_APP_AUDIENCE "api://${app_id}"
azd env set GROUNDWORK_ENTRA_APP_DISPLAY_NAME "$display_name"

echo "Groundwork postprovision: complete (appId=${app_id})"
