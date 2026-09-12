// Network Security Perimeter for Groundwork's own backend data-plane resources
// (docs/waf-assessment.md §2.11). NSP needs no VNet: a resource association plus a
// subscription-scoped access rule works regardless of AKS's own networking mode, which is why
// this was chosen over classic private endpoints. Private endpoints would require giving AKS a
// bring-your-own VNet, and vnetSubnetID is a creation-time-only AKS setting (confirmed against
// the `az aks` CLI reference, 2026-09-12) — adding one later means recreating the cluster, which
// would break every already-provisioned environment (including groundwork-dev).
//
// Scope, deliberately: Key Vault, Storage, Cosmos, and Foundry — NOT Container Registry (no NSP
// support at all as of 2026-09-12; see registry.bicep's IP-allowlist instead), NOT the AKS
// cluster's own public ingress (voice.html and the REST/WebSocket API must stay publicly reachable
// for voice/chat to work, and the same is true for any future Teams channel — it always transits
// the public Bot Framework connector, regardless of how private the backend is), and NOT Speech.
//
// Speech was in the original design but removed after a live deployment failure (2026-09-12):
// `Microsoft.CognitiveServices/accounts` NSP support is per-`kind`, not per-resource-type as the
// onboarded-services documentation implies. Foundry's account has `kind: AIServices` (NSP-
// supported, GA); the Speech account has `kind: SpeechServices`, which ARM rejects outright —
// "Account ... Kind SpeechServices doesn't support Network Security Perimeter." Confirmed via
// `az cognitiveservices account show --query kind` against both live accounts. Re-adding Speech
// here later needs that kind-level check re-verified, not just "it's a Cognitive Services account."
//
// Associations ship in `Learning` mode, not `Enforced`. Learning observes and logs what would be
// allowed or denied without blocking anything, so enabling this cannot break the documented local
// dev workflow (`uv run uvicorn ... --reload` using the developer's own DefaultAzureCredential) or
// any other existing access path. Flip individual associations to `Enforced` by hand once an
// operator has reviewed the NSP diagnostic logs for a representative period (see the follow-up
// step in docs/release-checklist.md) — a second parameter for a value that's meant to change
// exactly once isn't worth adding here.
//
// Cosmos DB's NSP support is public preview, not GA, as of 2026-09-12 (Key Vault, Storage, and
// Foundry's AIServices kind are all GA). Included here anyway, deliberately: Cosmos holds tenant
// and approval records, the highest-value target of the four.

@description('Azure region.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('Deterministic suffix.')
param resourceToken string

@description('Resource ID of the Key Vault to associate.')
param keyVaultResourceId string

@description('Resource ID of the Storage account to associate.')
param storageAccountResourceId string

@description('Resource ID of the Cosmos DB account to associate.')
param cosmosAccountResourceId string

@description('Resource ID of the Foundry (Cognitive Services, kind AIServices) account to associate.')
param foundryAccountResourceId string

resource perimeter 'Microsoft.Network/networkSecurityPerimeters@2025-07-01' = {
  name: 'nsp-groundwork-${resourceToken}'
  location: location
  tags: tags
}

resource profile 'Microsoft.Network/networkSecurityPerimeters/profiles@2025-07-01' = {
  parent: perimeter
  name: 'default'
}

// Inbound, subscription-based: covers every workload in this platform subscription (AKS pods
// included) regardless of which VNet - or none - they run in. No IP address to guess or keep
// current, no VNet dependency.
resource platformSubscriptionRule 'Microsoft.Network/networkSecurityPerimeters/profiles/accessRules@2025-07-01' = {
  parent: profile
  name: 'allow-platform-subscription'
  properties: {
    direction: 'Inbound'
    subscriptions: [
      {
        id: subscription().id
      }
    ]
  }
}

resource keyVaultAssociation 'Microsoft.Network/networkSecurityPerimeters/resourceAssociations@2025-07-01' = {
  parent: perimeter
  name: 'assoc-keyvault-${resourceToken}'
  properties: {
    accessMode: 'Learning'
    privateLinkResource: {
      id: keyVaultResourceId
    }
    profile: {
      id: profile.id
    }
  }
}

resource storageAssociation 'Microsoft.Network/networkSecurityPerimeters/resourceAssociations@2025-07-01' = {
  parent: perimeter
  name: 'assoc-storage-${resourceToken}'
  properties: {
    accessMode: 'Learning'
    privateLinkResource: {
      id: storageAccountResourceId
    }
    profile: {
      id: profile.id
    }
  }
}

// Public preview, not GA — see module header. Deliberate inclusion, not an oversight.
resource cosmosAssociation 'Microsoft.Network/networkSecurityPerimeters/resourceAssociations@2025-07-01' = {
  parent: perimeter
  name: 'assoc-cosmos-${resourceToken}'
  properties: {
    accessMode: 'Learning'
    privateLinkResource: {
      id: cosmosAccountResourceId
    }
    profile: {
      id: profile.id
    }
  }
}

resource foundryAssociation 'Microsoft.Network/networkSecurityPerimeters/resourceAssociations@2025-07-01' = {
  parent: perimeter
  name: 'assoc-foundry-${resourceToken}'
  properties: {
    accessMode: 'Learning'
    privateLinkResource: {
      id: foundryAccountResourceId
    }
    profile: {
      id: profile.id
    }
  }
}

output perimeterResourceId string = perimeter.id
output perimeterName string = perimeter.name
