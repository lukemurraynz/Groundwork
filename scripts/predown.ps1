#!/usr/bin/env pwsh
# Groundwork predown hook — removes the Entra application postprovision.ps1 created.
#
# Symmetric with postprovision.ps1. Entra app registrations have no resource group, so `azd down`'s
# normal resource-group deletion never touches them — without this hook, every `azd down` would
# leave a tenant-level object behind permanently.
#
# Idempotent: if the recorded client ID is missing, or the application is already gone, this
# succeeds rather than failing the `azd down` run. A predown hook that can block teardown is worse
# than one that silently no-ops when there is nothing left to remove.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$clientId = $env:GROUNDWORK_ENTRA_APP_CLIENT_ID
if ([string]::IsNullOrWhiteSpace($clientId)) {
    Write-Host 'Groundwork predown: no GROUNDWORK_ENTRA_APP_CLIENT_ID recorded, nothing to remove.'
    exit 0
}

Write-Host "Groundwork predown: removing Entra application appId=$clientId"

$exists = az ad app show --id $clientId --query appId -o tsv --only-show-errors 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '  application already gone, nothing to do'
    exit 0
}

az ad app delete --id $clientId --only-show-errors 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    # Non-fatal: az ad app delete failing must not block azd down from removing the resource
    # group. Surfaced loudly so a leaked app registration is visible rather than silent.
    Write-Warning "Failed to delete Entra application appId=$clientId. Remove it manually: az ad app delete --id $clientId"
    exit 0
}

Write-Host '  removed'
