#!/usr/bin/env pwsh
# Associate Groundwork's Microsoft AI Cloud Partner Program ID with the identities that operate
# customer environments, so Azure Consumed Revenue attributes to us (Partner Admin Link / PEC).
#
# [VERIFIED] 2026-08-26 against learn.microsoft.com:
#  - partner-center/membership/link-partner-id-for-azure-performance-pal-dpor — Az.ManagementPartner
#    `New-AzManagementPartner -PartnerId <id>` associates the signed-in principal; the link is at
#    the user/service-principal account level.
#  - azure/lighthouse/how-to/partner-earned-credit — for ARM-deployed Lighthouse (our model,
#    ADR-0002), the association must be made in the SERVICE PROVIDER tenant against the user or
#    service principal that has the PEC-eligible RBAC role on onboarded subscriptions.
#
# Usage: set GROUNDWORK_PARTNER_ID (the Associated PartnerID from the partner profile) and run:
#   ./scripts/link-partner-id.ps1                       # links the signed-in operator user
#   ./scripts/link-partner-id.ps1 -PrincipalId <oid>   # links a specific identity (e.g. the
#                                                      # control-plane managed identity object id)
# Idempotent: re-running updates/re-confirms the association.

param(
    [string]$PrincipalId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$partnerId = $env:GROUNDWORK_PARTNER_ID
if ([string]::IsNullOrWhiteSpace($partnerId)) {
    Write-Error 'GROUNDWORK_PARTNER_ID is not set. Export it first, e.g.: $env:GROUNDWORK_PARTNER_ID = "1234567"'
    exit 1
}

if (-not (Get-Module -ListAvailable Az.ManagementPartner)) {
    Write-Host 'Installing Az.ManagementPartner module (once per machine)'
    Install-Module Az.ManagementPartner -Scope CurrentUser -Force -AllowClobber
}
Import-Module Az.ManagementPartner

if ($PrincipalId) {
    Write-Warning @'
Associating a specific principal programmatically requires running this cmdlet AS that principal
(PAL binds to the authenticated account). For a managed identity, use an Interactive or
service-principal login whose identity IS the target, or perform the association once from the
Azure portal blade while delegated into the relevant context:
  https://portal.azure.com/#blade/Microsoft_Azure_Billing/managementpartnerblade
'@
}

Write-Host "Linking partner ID $partnerId to the current signed-in account..."
$existing = Get-AzManagementPartner -ErrorAction SilentlyContinue
if ($existing -and $existing.PartnerId -eq $partnerId) {
    Write-Host '  already linked to this partner ID - nothing to do.'
} elseif ($existing) {
    Write-Host "  currently linked to $($existing.PartnerId); updating"
    Update-AzManagementPartner -PartnerId $partnerId | Out-Null
    Write-Host '  updated.'
} else {
    New-AzManagementPartner -PartnerId $partnerId | Out-Null
    Write-Host '  linked.'
}

Write-Host 'Verify anytime with: Get-AzManagementPartner'
