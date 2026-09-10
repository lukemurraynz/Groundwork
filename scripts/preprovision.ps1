#!/usr/bin/env pwsh
# Groundwork preprovision gate.
#
# Fails fast if the target subscription or region cannot satisfy the platform, rather than
# discovering it partway through a provision: validate before promising.
#
# Every check reports a real result. A check that cannot reach Azure is a FAILURE, never a pass.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:failures = @()

function Test-Requirement {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Check,
        [string]$Remediation = ''
    )
    Write-Host -NoNewline "  $Name ... "
    try {
        $result = & $Check
        if ($result) {
            Write-Host 'pass'
        } else {
            Write-Host 'FAIL'
            $script:failures += [pscustomobject]@{ Name = $Name; Reason = 'check returned false'; Remediation = $Remediation }
        }
    } catch {
        # Unreachable is a failure, not a pass (FR-015).
        Write-Host 'FAIL (unreachable)'
        $script:failures += [pscustomobject]@{ Name = $Name; Reason = $_.Exception.Message; Remediation = $Remediation }
    }
}

Write-Host 'Groundwork preprovision checks'

# Auto-resolve the operator principal so the AKS RBAC Cluster Admin grant in aks.bicep (and the Key
# Vault Secrets User grant in keyvault.bicep) apply to whoever is running this provision, without a
# manual `azd env set` step. Only resolves when unset — an operator who has deliberately set
# AZURE_PRINCIPAL_ID to a different principal (e.g. a service principal for unattended provisioning)
# is not overridden.
if ([string]::IsNullOrWhiteSpace($env:AZURE_PRINCIPAL_ID)) {
    Write-Host -NoNewline '  Resolving AZURE_PRINCIPAL_ID from signed-in identity ... '
    $signedInId = az ad signed-in-user show --query id -o tsv --only-show-errors 2>$null
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($signedInId)) {
        azd env set AZURE_PRINCIPAL_ID $signedInId | Out-Null
        $env:AZURE_PRINCIPAL_ID = $signedInId
        Write-Host "resolved ($signedInId)"
    } else {
        # A service-principal login has no signed-in user object. Not fatal — an empty
        # AZURE_PRINCIPAL_ID just means aks.bicep and keyvault.bicep skip the operator role
        # assignment (their `if (!empty(...))` guard).
        Write-Host 'unavailable (no signed-in user; operator role assignment will be skipped)'
    }
}

$location = $env:AZURE_LOCATION
$subscription = $env:AZURE_SUBSCRIPTION_ID

Test-Requirement -Name 'AZURE_LOCATION is set' -Check {
    -not [string]::IsNullOrWhiteSpace($location)
} -Remediation 'Run: azd env set AZURE_LOCATION eastus2'

Test-Requirement -Name 'AZURE_SUBSCRIPTION_ID is set' -Check {
    -not [string]::IsNullOrWhiteSpace($subscription)
} -Remediation 'Run: azd env set AZURE_SUBSCRIPTION_ID <id>'

# Governance config has no IaC default on purpose (src/groundwork_shared/config/settings.py:
# "there is no default, because a default here could point at the wrong tenant"). Without this
# check, azd up provisions the full platform (15-20+ minutes) and only then discovers the gap,
# as a controlplane/orchestrator CrashLoopBackOff during azd deploy — checking here fails in
# seconds, before any Azure resource is created.
Test-Requirement -Name 'GROUNDWORK_APPROVAL_THRESHOLD_AUD is set' -Check {
    -not [string]::IsNullOrWhiteSpace($env:GROUNDWORK_APPROVAL_THRESHOLD_AUD)
} -Remediation 'Run: azd env set GROUNDWORK_APPROVAL_THRESHOLD_AUD 1000'

Test-Requirement -Name 'GROUNDWORK_APPROVER_ROLE is set' -Check {
    -not [string]::IsNullOrWhiteSpace($env:GROUNDWORK_APPROVER_ROLE)
} -Remediation 'Run: azd env set GROUNDWORK_APPROVER_ROLE Groundwork.Approver'

Test-Requirement -Name 'GROUNDWORK_TENANT_CONCURRENCY_CAP is set' -Check {
    -not [string]::IsNullOrWhiteSpace($env:GROUNDWORK_TENANT_CONCURRENCY_CAP)
} -Remediation 'Run: azd env set GROUNDWORK_TENANT_CONCURRENCY_CAP 3'

Test-Requirement -Name 'GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS is set' -Check {
    -not [string]::IsNullOrWhiteSpace($env:GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS)
} -Remediation 'Run: azd env set GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS 3'

# Must match the @allowed list on the location parameter in infra/main.bicep. If you have a
# data-residency requirement, narrow both lists down to the region(s) you need.
Test-Requirement -Name 'Region is a supported region' -Check {
    $location -in @('australiaeast', 'australiasoutheast', 'eastus', 'eastus2', 'westus3', 'swedencentral', 'francecentral', 'uksouth', 'canadacentral', 'japaneast', 'southeastasia', 'germanywestcentral')
} -Remediation 'Pick a region from the @allowed list in infra/main.bicep.'

Test-Requirement -Name 'Azure CLI is authenticated' -Check {
    $null = az account show --only-show-errors 2>$null
    $LASTEXITCODE -eq 0
} -Remediation 'Run: az login'

# Availability zones are required by FR-041a. A region without three zones cannot host a
# zone-redundant node pool, so this must block rather than warn.
Test-Requirement -Name 'Region supports at least 3 availability zones' -Check {
    $zones = az vm list-skus --location $location --resource-type virtualMachines `
        --query "[?name=='Standard_D4s_v5'].locationInfo[0].zones | [0]" -o tsv --only-show-errors 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'could not query VM SKU zone information' }
    ($zones -split '\s+' | Where-Object { $_ }).Count -ge 3
} -Remediation 'FR-041a requires zone-redundant node pools. Choose a region with three availability zones.'

$requiredProviders = @(
    'Microsoft.ContainerService',
    'Microsoft.DocumentDB',
    'Microsoft.KeyVault',
    'Microsoft.OperationalInsights',
    'Microsoft.Storage',
    'Microsoft.Fabric',
    'Microsoft.CognitiveServices',
    'Microsoft.ManagedIdentity'
)

foreach ($provider in $requiredProviders) {
    $p = $provider
    Test-Requirement -Name "Provider registered: $p" -Check {
        $state = az provider show --namespace $p --query registrationState -o tsv --only-show-errors 2>$null
        if ($LASTEXITCODE -ne 0) { throw "could not query provider $p" }
        $state -eq 'Registered'
    } -Remediation "Run: az provider register --namespace $p"
}

Write-Host ''
if ($script:failures.Count -gt 0) {
    Write-Host "Preprovision FAILED with $($script:failures.Count) blocking finding(s):" -ForegroundColor Red
    foreach ($f in $script:failures) {
        Write-Host ''
        Write-Host "  x $($f.Name)" -ForegroundColor Red
        Write-Host "    reason:      $($f.Reason)"
        if ($f.Remediation) { Write-Host "    remediation: $($f.Remediation)" }
    }
    Write-Host ''
    exit 1
}

Write-Host 'All preprovision checks passed.' -ForegroundColor Green
exit 0
