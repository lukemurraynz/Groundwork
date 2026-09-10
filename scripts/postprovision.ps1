#!/usr/bin/env pwsh
# Groundwork postprovision hook — Entra app registration lifecycle.
#
# Creates the multi-tenant Entra ID application that customers consent to (FR-006) and that
# api/auth.py validates inbound bearer tokens against (FR-005, FR-007). Paired with predown.ps1,
# which removes it, so `azd up` / `azd down` fully own its lifecycle rather than leaving a
# tenant-level object behind after the resource group is gone.
#
# Deliberately narrow scope. This script creates the bare application, its app roles, and its
# service principal — nothing else. It does NOT request or grant any Microsoft Graph or ARM API
# permission, and it does NOT run an admin-consent flow. Deciding what delegated or application
# permissions Groundwork needs inside a customer's tenant is an architecture decision that has not
# been made yet (it depends on the still-open CL-002 landing zone contract), and granting broad
# permissions automatically in a script would be exactly the kind of standing configuration change
# that needs a human decision, not a default.
#
# Why a hook and not the Microsoft Graph Bicep extension: verified 2026-07-30 against
# learn.microsoft.com/azure/azure-resource-manager/bicep/deployment-stacks-known-issues —
# "Microsoft Graph provider isn't supported" for deployment stacks. Bicep's own non-stack deployment
# also has no `azd down` cleanup path for resources outside a resource group (Entra objects have no
# resource group at all). Hooks are the documented, symmetric mechanism: postprovision creates,
# predown deletes, both idempotent by construction.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$envName = $env:AZURE_ENV_NAME
if ([string]::IsNullOrWhiteSpace($envName)) {
    Write-Error 'AZURE_ENV_NAME is not set; cannot derive a deterministic app display name.'
    exit 1
}

# Deterministic per-environment name, so dev and production environments never collide and a
# re-run always finds the same object rather than creating a duplicate.
$displayName = "Groundwork-$envName"
$roleManifestPath = Join-Path $PSScriptRoot '..' 'infra' 'app-roles.json' -Resolve

Write-Host "Groundwork postprovision: Entra application '$displayName'"

# Idempotency check. `az ad app list --filter` queries Microsoft Graph directly, so this is real
# state, not a local cache that could drift from what actually exists.
$existing = az ad app list --filter "displayName eq '$displayName'" --query '[0]' -o json --only-show-errors 2>$null
$app = $null
if ($LASTEXITCODE -eq 0 -and $existing -and $existing -ne 'null') {
    $app = $existing | ConvertFrom-Json
    Write-Host "  found existing application appId=$($app.appId), reusing"
} else {
    Write-Host '  no existing application found, creating'
    $created = az ad app create `
        --display-name $displayName `
        --sign-in-audience 'AzureADMultipleOrgs' `
        --only-show-errors -o json 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to create Entra application: $created"
        exit 1
    }
    $app = $created | ConvertFrom-Json
    Write-Host "  created appId=$($app.appId)"

    # api://<appId> is the Azure-provided App ID URI form that requires no domain ownership or
    # verification — the right choice while no groundwork.* domain is owned (see
    # the project's naming research (not included in this release)).
    $audience = "api://$($app.appId)"
    az ad app update --id $app.appId --identifier-uris $audience --only-show-errors 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Failed to set identifier URI on the application; continuing without it."
    }
}

# App roles are applied every run, not only on creation, so an update to infra/app-roles.json is
# picked up on the next `azd provision` without needing to delete and recreate the application.
#
# Uses the `@<path>` file-reference form, not the JSON content passed as a string argument.
# PowerShell's native-command argument marshalling mangles a large inline JSON string (quoting is
# lost passing through to the underlying az CLI process); `@path` is az's own designed mechanism
# for exactly this and was verified directly against this tenant before being written here.
Write-Host '  applying app role definitions'
az ad app update --id $app.appId --app-roles "@$roleManifestPath" --only-show-errors 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Failed to apply app roles; the application exists but may be missing roles.'
}

# The `access_as_user` delegated scope every frontend (voice.html's MSAL config) requests —
# found missing live 2026-08-23 (AADSTS65005 "asked for scope ... that doesn't exist"): a fresh
# `az ad app create` gives an application zero exposed API scopes by default; app roles alone
# (above) do not create one. Applied every run, same idempotent full-replace pattern as app roles
# above, with a fixed scope `id` (not freshly generated each run) so re-applying never invalidates
# a user's already-granted consent for this scope.
Write-Host '  applying OAuth2 permission scope (access_as_user)'
$apiScopeBodyPath = Join-Path ([System.IO.Path]::GetTempPath()) "groundwork-api-scope-$envName.json"
@{
    api = @{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes = @(
            @{
                id                      = '207e6d4e-6cea-444b-817e-e42cdbb9245b'
                adminConsentDescription = "Allows the app to call Groundwork's control-plane API as the signed-in user."
                adminConsentDisplayName = 'Access Groundwork as you'
                userConsentDescription  = "Allows the app to access Groundwork's control-plane API on your behalf."
                userConsentDisplayName  = 'Access Groundwork as you'
                value                   = 'access_as_user'
                type                    = 'User'
                isEnabled               = $true
            }
        )
    }
} | ConvertTo-Json -Depth 5 -Compress | Set-Content -Path $apiScopeBodyPath -NoNewline
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$($app.id)" `
    --headers 'Content-Type=application/json' --body "@$apiScopeBodyPath" --only-show-errors 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Failed to apply the access_as_user OAuth2 permission scope; sign-in will fail at Entra with AADSTS65005 until this is fixed.'
}
Remove-Item -Path $apiScopeBodyPath -ErrorAction SilentlyContinue

# The service principal is what makes the app assignable and consentable in a tenant. Creating it
# is idempotent: `az ad sp create` fails harmlessly if one already exists for this appId, which we
# detect and treat as success rather than a warning.
$spId = az ad sp show --id $app.appId --query id -o tsv --only-show-errors 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '  creating service principal'
    $spId = az ad sp create --id $app.appId --query id -o tsv --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'Failed to create the service principal for this application.'
        $spId = $null
    }
} else {
    Write-Host '  service principal already exists'
}

# Grant the operator every app role — found live 2026-08-24: TokenValidator's TokenPolicy
# defaults `require_role=True` (api/main.py never overrides it), so a signed-in caller with zero
# app-role assignments fails EVERY authenticated route with a bare AuthenticationError, no matter
# how correct their token otherwise is. A fresh app registration assigns nobody any role by
# default; without this, the operator who just stood the environment up cannot use their own
# deployment at all until someone manually grants a role via Graph — exactly the kind of gap this
# script exists to close. Scoped to the resolved AZURE_PRINCIPAL_ID (same identity
# preprovision.ps1 already resolves for the AKS/Key Vault operator grants), all three roles
# (Operator/Approver/Requester): this is the operator's own environment, not a customer's, so the
# usual production least-privilege-per-role split doesn't apply the same way here.
if ($spId -and -not [string]::IsNullOrWhiteSpace($env:AZURE_PRINCIPAL_ID)) {
    Write-Host "  granting operator ($($env:AZURE_PRINCIPAL_ID)) all app roles"
    foreach ($roleId in @('2e3f4a5b-6c7d-4e8f-9a0b-1c2d3e4f5a6b', '1d2e3f4a-5b6c-4d7e-8f9a-0b1c2d3e4f5a', '8f6a9c1e-2b3d-4e5f-9a1b-6c7d8e9f0a1b')) {
        $existingAssignment = az rest --method GET --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$spId/appRoleAssignedTo" `
            --query "value[?principalId=='$($env:AZURE_PRINCIPAL_ID)' && appRoleId=='$roleId'] | length(@)" -o tsv --only-show-errors 2>$null
        if ($existingAssignment -eq '0' -or [string]::IsNullOrWhiteSpace($existingAssignment)) {
            $roleBodyPath = Join-Path ([System.IO.Path]::GetTempPath()) "groundwork-role-assign-$envName-$roleId.json"
            @{ principalId = $env:AZURE_PRINCIPAL_ID; resourceId = $spId; appRoleId = $roleId } | ConvertTo-Json -Compress | Set-Content -Path $roleBodyPath -NoNewline
            az rest --method POST --uri "https://graph.microsoft.com/v1.0/users/$($env:AZURE_PRINCIPAL_ID)/appRoleAssignments" `
                --headers 'Content-Type=application/json' --body "@$roleBodyPath" --only-show-errors 2>&1 | Out-Null
            Remove-Item -Path $roleBodyPath -ErrorAction SilentlyContinue
        }
    }
} else {
    Write-Warning 'AZURE_PRINCIPAL_ID not resolved or no service principal; skipping operator app-role grant. Authenticated routes will fail for the operator with AuthenticationError until a role is assigned manually.'
}

# The admin-consent endpoint (learn.microsoft.com/entra/identity-platform/v2-admin-consent,
# [VERIFIED] 2026-08-06) requires `redirect_uri` and rejects any value not already registered on
# the app. This is deliberately a real, already-hosted, generic Microsoft landing page — not a
# callback Groundwork hosts and processes. api/tenants.py's onboarding-confirm route docstring
# explains why: that same doc explicitly warns "never use the tenant ID value of the `tenant`
# parameter to authenticate or authorize users" (it can be forged by anyone who just navigates to
# the URL), so this redirect is informational only for the admin, never a verification signal —
# a Groundwork operator's own attestation is what actually flips consent_state to GRANTED.
$redirectUri = 'https://portal.azure.com'
Write-Host "  registering admin-consent redirect URI ($redirectUri)"
az ad app update --id $app.appId --web-redirect-uris $redirectUri --only-show-errors 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Failed to register the redirect URI; onboarding consent-url generation may work but the admin-consent flow itself will fail at Entra with a redirect_uri mismatch until this is fixed.'
}
azd env set GROUNDWORK_ENTRA_APP_REDIRECT_URI $redirectUri

# Recorded so downstream services and this same hook, on a later run, need not re-query Graph to
# know what was created. Consumed by k8s manifests as GROUNDWORK_ENTRA_APP_CLIENT_ID /
# GROUNDWORK_ENTRA_APP_AUDIENCE, and by TokenPolicy.expected_audience at runtime.
azd env set GROUNDWORK_ENTRA_APP_CLIENT_ID $app.appId
azd env set GROUNDWORK_ENTRA_APP_AUDIENCE "api://$($app.appId)"
azd env set GROUNDWORK_ENTRA_APP_DISPLAY_NAME $displayName

# ---------------------------------------------------------------------------
# External ingress (AKS application routing add-on, enabled unconditionally in aks.bicep) +
# real TLS via cert-manager/Let's Encrypt HTTP-01 — codified here rather than left as a manual
# `kubectl apply` step, same discipline as the Entra app above. No groundwork.* domain is owned
# (CLAUDE.md), so this uses the add-on's own load-balancer IP with a nip.io hostname instead of
# App Routing's DNS-zone-based managed-certificate path, which needs a delegated zone we don't
# have.
# ---------------------------------------------------------------------------

$clusterName = $env:AZURE_AKS_CLUSTER_NAME
$resourceGroup = $env:AZURE_RESOURCE_GROUP
if ([string]::IsNullOrWhiteSpace($clusterName) -or [string]::IsNullOrWhiteSpace($resourceGroup)) {
    Write-Warning 'AZURE_AKS_CLUSTER_NAME/AZURE_RESOURCE_GROUP not set; skipping ingress setup.'
} else {
    Write-Host 'Groundwork postprovision: external ingress'
    az aks get-credentials --resource-group $resourceGroup --name $clusterName --overwrite-existing --only-show-errors 2>&1 | Out-Null

    Write-Host '  waiting for the application-routing ingress controller external IP'
    $ingressIp = $null
    for ($i = 0; $i -lt 30; $i++) {
        $ingressIp = kubectl get svc -n app-routing-system nginx -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
        if (-not [string]::IsNullOrWhiteSpace($ingressIp)) { break }
        Start-Sleep -Seconds 10
    }

    if ([string]::IsNullOrWhiteSpace($ingressIp)) {
        Write-Warning 'Ingress controller external IP did not appear within 5 minutes; skipping ingress/TLS setup this run. Re-run `azd provision` once the AKS add-on has settled.'
    } else {
        $ingressHost = "$($ingressIp.Replace('.', '-')).nip.io"
        Write-Host "  ingress IP $ingressIp -> host $ingressHost"

        # externalTrafficPolicy: Cluster, not the add-on's own Local default — found live
        # (2026-08-23) breaking cert-manager's HTTP-01 self-check with a bare connection timeout:
        # Local only forwards LB traffic to a node that actually hosts a backend pod, which
        # breaks hairpin routing (a pod calling back out to the cluster's own external IP, exactly
        # what cert-manager's self-check and any future in-cluster health probe against the public
        # hostname both do). The NginxIngressController CRD (approuting.kubernetes.azure.com/v1alpha1)
        # does not expose externalTrafficPolicy, so this patches the add-on-managed Service
        # directly; re-applied every provision in case an add-on reconcile ever reverts it. Trade-off
        # accepted: Cluster loses real client-IP preservation at the ingress (SNAT'd through a
        # second hop) — acceptable here since nothing downstream does IP-based rate limiting yet.
        Write-Host '  setting nginx Service externalTrafficPolicy=Cluster (hairpin fix for in-cluster self-checks)'
        kubectl patch svc nginx -n app-routing-system -p '{"spec":{"externalTrafficPolicy":"Cluster"}}' 2>&1 | Out-Null

        Write-Host '  installing cert-manager (idempotent)'
        helm repo add jetstack https://charts.jetstack.io --force-update 2>&1 | Out-Null
        helm repo update jetstack 2>&1 | Out-Null
        helm upgrade --install cert-manager jetstack/cert-manager `
            --namespace cert-manager --create-namespace --set crds.enabled=true --wait --timeout 5m 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'cert-manager install/upgrade failed; TLS certificate issuance will not work until this is fixed.'
        }

        $contactEmail = $env:GROUNDWORK_LETSENCRYPT_CONTACT_EMAIL
        if ([string]::IsNullOrWhiteSpace($contactEmail)) {
            $contactEmail = az account show --query user.name -o tsv --only-show-errors 2>$null
        }
        Write-Host "  applying letsencrypt-prod ClusterIssuer (contact: $contactEmail)"
        $issuerTemplatePath = Join-Path $PSScriptRoot '..' 'infra' 'cluster-issuer.tmpl.yaml' -Resolve
        $issuerYaml = (Get-Content $issuerTemplatePath -Raw).Replace('__CONTACT_EMAIL__', $contactEmail)
        $issuerYaml | kubectl apply -f - 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Failed to apply the letsencrypt-prod ClusterIssuer.'
        }

        # SPA platform, not Web: msal-browser's authorization-code+PKCE flow is rejected with
        # AADSTS9002326 ("cross-origin token redemption is permitted only for the 'Single-Page
        # Application' client-type") if registered under the Web platform, which is why the
        # admin-consent redirect above uses --web-redirect-uris but this one does not. az CLI has
        # no dedicated SPA-redirect-URI flag, so this goes through Graph directly.
        # /static/voice.html itself: the frontend uses MSAL's full-page loginRedirect flow, not a
        # popup, so the app is the redirect target — handleRedirectPromise() in its own initAuth()
        # picks the result up on return. (A dedicated blank redirect page was tried first, for a
        # popup flow; abandoned after loginPopup produced four distinct cross-window-messaging
        # failures in a row across two browsers, most conclusively a popup that completed a real
        # auth exchange but never signalled back to the opener at all.)
        $spaRedirectUri = "https://$ingressHost/static/voice.html"
        Write-Host "  registering SPA redirect URI ($spaRedirectUri)"
        # `--body @<path>`, not an inline JSON string: PowerShell's native-command argument
        # marshalling mangles inline JSON (the same reason the app-roles update above uses
        # `@$roleManifestPath` rather than a string argument) — an inline `--body $spaBody` here
        # exited 0 with no visible error yet silently never patched the application.
        $spaBodyPath = Join-Path ([System.IO.Path]::GetTempPath()) "groundwork-spa-redirect-$envName.json"
        @{ spa = @{ redirectUris = @($spaRedirectUri) } } | ConvertTo-Json -Compress | Set-Content -Path $spaBodyPath -NoNewline
        az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$($app.id)" `
            --headers 'Content-Type=application/json' --body "@$spaBodyPath" --only-show-errors 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Failed to register the SPA redirect URI; sign-in from the external URL will fail at Entra with a redirect_uri mismatch until this is fixed.'
        }
        Remove-Item -Path $spaBodyPath -ErrorAction SilentlyContinue

        azd env set GROUNDWORK_INGRESS_HOST $ingressHost
        azd env set GROUNDWORK_PUBLIC_URL "https://$ingressHost"
        Write-Host "  external URL: https://$ingressHost/static/voice.html"
    }
}

Write-Host "Groundwork postprovision: complete (appId=$($app.appId))"
