#!/usr/bin/env sh
# Groundwork preprovision gate (POSIX equivalent of preprovision.ps1).
#
# Fails fast if the target subscription or region cannot satisfy the platform, rather than
# discovering it partway through a provision: validate before promising.
#
# A check that cannot reach Azure is a FAILURE, never a pass (FR-015).

set -eu

failures=0

fail() {
    printf 'FAIL\n'
    printf '    reason:      %s\n' "$1"
    [ -n "${2:-}" ] && printf '    remediation: %s\n' "$2"
    failures=$((failures + 1))
}

printf 'Groundwork preprovision checks\n'

# Auto-resolve the operator principal so the AKS RBAC Cluster Admin grant in aks.bicep (and the Key
# Vault Secrets User grant in keyvault.bicep) apply to whoever is running this provision, without a
# manual `azd env set` step. Only resolves when unset — a deliberately-set AZURE_PRINCIPAL_ID (e.g.
# a service principal for unattended provisioning) is not overridden.
if [ -z "${AZURE_PRINCIPAL_ID:-}" ]; then
    printf '  Resolving AZURE_PRINCIPAL_ID from signed-in identity ... '
    if signed_in_id=$(az ad signed-in-user show --query id -o tsv --only-show-errors 2>/dev/null) && [ -n "$signed_in_id" ]; then
        azd env set AZURE_PRINCIPAL_ID "$signed_in_id" >/dev/null
        AZURE_PRINCIPAL_ID="$signed_in_id"
        export AZURE_PRINCIPAL_ID
        printf 'resolved (%s)\n' "$signed_in_id"
    else
        # A service-principal login has no signed-in user object. Not fatal — an empty
        # AZURE_PRINCIPAL_ID just means aks.bicep and keyvault.bicep skip the operator role
        # assignment (their `if (!empty(...))` guard).
        printf 'unavailable (no signed-in user; operator role assignment will be skipped)\n'
    fi
fi

printf '  AZURE_LOCATION is set ... '
if [ -n "${AZURE_LOCATION:-}" ]; then printf 'pass\n'; else
    fail 'AZURE_LOCATION is empty' 'azd env set AZURE_LOCATION eastus2'
fi

printf '  AZURE_SUBSCRIPTION_ID is set ... '
if [ -n "${AZURE_SUBSCRIPTION_ID:-}" ]; then printf 'pass\n'; else
    fail 'AZURE_SUBSCRIPTION_ID is empty' 'azd env set AZURE_SUBSCRIPTION_ID <id>'
fi

# Governance config has no IaC default on purpose (src/groundwork_shared/config/settings.py:
# "there is no default, because a default here could point at the wrong tenant"). Without this
# check, azd up provisions the full platform (15-20+ minutes) and only then discovers the gap,
# as a controlplane/orchestrator CrashLoopBackOff during azd deploy — checking here fails in
# seconds, before any Azure resource is created.
printf '  GROUNDWORK_APPROVAL_THRESHOLD_AUD is set ... '
if [ -n "${GROUNDWORK_APPROVAL_THRESHOLD_AUD:-}" ]; then printf 'pass\n'; else
    fail 'GROUNDWORK_APPROVAL_THRESHOLD_AUD is empty' 'azd env set GROUNDWORK_APPROVAL_THRESHOLD_AUD 1000'
fi

printf '  GROUNDWORK_APPROVER_ROLE is set ... '
if [ -n "${GROUNDWORK_APPROVER_ROLE:-}" ]; then printf 'pass\n'; else
    fail 'GROUNDWORK_APPROVER_ROLE is empty' 'azd env set GROUNDWORK_APPROVER_ROLE Groundwork.Approver'
fi

printf '  GROUNDWORK_TENANT_CONCURRENCY_CAP is set ... '
if [ -n "${GROUNDWORK_TENANT_CONCURRENCY_CAP:-}" ]; then printf 'pass\n'; else
    fail 'GROUNDWORK_TENANT_CONCURRENCY_CAP is empty' 'azd env set GROUNDWORK_TENANT_CONCURRENCY_CAP 3'
fi

printf '  GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS is set ... '
if [ -n "${GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS:-}" ]; then printf 'pass\n'; else
    fail 'GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS is empty' 'azd env set GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS 3'
fi

# Must match the @allowed list on the location parameter in infra/main.bicep. If you have a
# data-residency requirement, narrow both lists down to the region(s) you need.
printf '  Region is a supported region ... '
case "${AZURE_LOCATION:-}" in
    australiaeast|australiasoutheast|eastus|eastus2|westus3|swedencentral|francecentral|uksouth|canadacentral|japaneast|southeastasia|germanywestcentral) printf 'pass\n' ;;
    *) fail "region '${AZURE_LOCATION:-unset}' is not in the supported list" \
            'Pick a region from the @allowed list in infra/main.bicep.' ;;
esac

printf '  Azure CLI is authenticated ... '
if az account show --only-show-errors >/dev/null 2>&1; then printf 'pass\n'; else
    fail 'az account show failed' 'az login'
fi

# FR-041a: zone-redundant node pools require a region with three availability zones.
printf '  Region supports at least 3 availability zones ... '
if zones=$(az vm list-skus --location "${AZURE_LOCATION:-}" --resource-type virtualMachines \
        --query "[?name=='Standard_D4s_v5'].locationInfo[0].zones | [0]" -o tsv --only-show-errors 2>/dev/null); then
    count=$(printf '%s\n' "$zones" | tr -s '[:space:]' '\n' | grep -c '[0-9]' || true)
    if [ "$count" -ge 3 ]; then printf 'pass\n'; else
        fail "only $count zone(s) available" 'FR-041a requires three availability zones.'
    fi
else
    fail 'could not query VM SKU zone information' 'Check subscription access and region name.'
fi

for provider in \
    Microsoft.ContainerService \
    Microsoft.DocumentDB \
    Microsoft.KeyVault \
    Microsoft.OperationalInsights \
    Microsoft.Storage \
    Microsoft.Fabric \
    Microsoft.CognitiveServices \
    Microsoft.ManagedIdentity
do
    printf '  Provider registered: %s ... ' "$provider"
    if state=$(az provider show --namespace "$provider" --query registrationState -o tsv --only-show-errors 2>/dev/null); then
        if [ "$state" = 'Registered' ]; then printf 'pass\n'; else
            fail "state is $state" "az provider register --namespace $provider"
        fi
    else
        fail "could not query provider $provider" "az provider register --namespace $provider"
    fi
done

printf '\n'
if [ "$failures" -gt 0 ]; then
    printf 'Preprovision FAILED with %s blocking finding(s).\n' "$failures"
    exit 1
fi

printf 'All preprovision checks passed.\n'
exit 0
