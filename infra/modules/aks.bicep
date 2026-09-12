// AKS cluster hosting the Groundwork platform (PD-002).
//
// Three requirements drive this file:
//
//   FR-041a  zone-redundant node pools in a single Australian region
//   FR-044   workload identity, with no Kubernetes-stored client secrets
//   FR-041   control plane and execution workers independently scalable, so one tenant's
//            deployment load cannot starve another tenant's planning requests
//
// The last one is why there are two user node pools rather than one. A shared pool would let a
// burst of deployment work consume the capacity the planning surface needs to meet SC-003, which
// is the exact starvation FR-045c prohibits.
//
// API version pinned to 2024-09-01: the newest version Bicep 0.43.8 can type-check. The
// subscription offers 2026-05-01, but it emits BCP081 (no type definitions), which would mean no
// compile-time validation of the agent pool and security profile properties below — exactly the
// properties most costly to get wrong.

@description('Azure region.')
param location string

@description('Deterministic suffix.')
param resourceToken string

@description('Tags applied to every resource.')
param tags object

@description('Kubernetes version, pinned so an upgrade is a reviewed change rather than drift.')
param kubernetesVersion string

@description('Availability zones (FR-041a).')
param availabilityZones array

@description('Log Analytics workspace for container insights.')
param logAnalyticsWorkspaceId string

@description('Resource ID of the AKS control-plane identity.')
param clusterIdentityId string

@description('Resource ID of the control-plane workload identity.')
param controlPlaneIdentityId string

@description('Resource ID of the orchestrator workload identity.')
param orchestratorIdentityId string

@description('Object ID of the operator who needs kubectl access to deploy and administer the cluster. Empty means no standing human access.')
param operatorPrincipalId string = ''

@description('Name of the container registry nodes pull images from.')
param containerRegistryName string

@description('Resource ID of a pre-created Standard static public IP to use for cluster outbound traffic instead of an AKS-managed one. Empty means AKS picks its own (address unknowable at template-author time). Only needed when the registry firewall (registry.bicep) must allowlist a fixed, known IP for kubelet image pulls.')
param outboundPublicIpId string = ''

// docs/waf-assessment.md §5.5: cost floor from three always-on node pools. Spot pricing is
// deliberately scoped to control-plane only, never executor or system — see this param's own
// description for why, and k8s/controlplane/deployment.tmpl.yaml for the matching toleration +
// soft on-demand preference that keeps the fixed replica count off spot in steady state.
@description('Adds a fourth, optional node pool for the control-plane tier at Spot pricing, scaling from 0 - burst/cost-optimisation capacity only, never a replacement for the on-demand controlplane pool. Off by default. Deliberately not offered for the executor pool: a stage timeout is classified transient and consumes the blueprint\'s retry budget (2-3 attempts) the same as any other transient failure, and Spot\'s ~30s eviction notice would raise how often that budget gets spent on evictions rather than genuine failures - a reliability regression for real customer deployments, not just a cost tradeoff. Not offered for the system pool either: cluster-critical add-ons need the same reliability floor AKS itself recommends against Spot for.')
param enableSpotControlPlanePool bool = false

@description('Minimum node count for the optional Spot control-plane pool. 0 is valid for a User-mode pool and is the point of offering this at all - true scale-to-zero when no burst capacity is needed.')
param spotControlPlanePoolMinCount int = 0

@description('Maximum node count for the optional Spot control-plane pool.')
param spotControlPlanePoolMaxCount int = 3

// Node sizing is parameterised because it is quota- and cost-bound, not design-bound. The *shape*
// — three separate pools — is design-bound and identical in every profile, because FR-045c requires
// that executor load cannot starve the planning surface. Collapsing the pools would save more money
// and break that guarantee; shrinking them does not.
//
// Quota check before changing these: each node consumes its vmSize's vCPU count, and the sum across
// all pools at maximum must fit the subscription's regional quota. Verified 2026-07-30: this
// subscription has 65 DSv5 vCPUs in australiaeast.
//
// A cluster that provisions and then cannot scale is a worse failure than one that refuses to
// provision, because it surfaces under load rather than at deploy time.
@description('VM size for all node pools. Note this is immutable per pool — changing it replaces the node pools.')
param nodeVmSize string = 'Standard_D4s_v5'

@description('Minimum system node pool size.')
@minValue(1)
param systemPoolMinCount int = 3

@description('Maximum system node pool size.')
@minValue(1)
param systemPoolMaxCount int = 3

@description('Minimum control-plane node pool size.')
@minValue(1)
param controlPlanePoolMinCount int = 2

@description('Maximum control-plane node pool size.')
@minValue(1)
param controlPlanePoolMaxCount int = 6

@description('Minimum executor node pool size.')
@minValue(1)
param executorPoolMinCount int = 2

@description('Maximum executor node pool size.')
@minValue(1)
param executorPoolMaxCount int = 6

resource cluster 'Microsoft.ContainerService/managedClusters@2024-10-01' = {
  name: 'aks-groundwork-${resourceToken}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${clusterIdentityId}': {}
    }
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: 'gw-${resourceToken}'
    enableRBAC: true

    securityProfile: {
      // FR-044: workload identity is the whole point. OIDC issuer is its prerequisite.
      workloadIdentity: {
        enabled: true
      }
      imageCleaner: {
        enabled: true
        intervalHours: 24
      }
    }

    oidcIssuerProfile: {
      enabled: true
    }

    // AKS-managed application routing add-on: gives the control plane a real external HTTPS
    // entrypoint (managed NGINX, ingress class webapprouting.kubernetes.azure.com) without
    // standing up and patching a self-managed ingress controller. Per
    // .apm/skills/aks-cluster-architecture's own guidance this is the "simple supported ingress
    // now" choice (GA, security-patched through November 2026) — Application Gateway for
    // Containers or App Routing's Gateway API mode is the named long-term target if/when WAF,
    // private frontend, or Gateway API route types are needed; recorded here rather than in an
    // ADR because this environment has neither yet.
    ingressProfile: {
      webAppRouting: {
        enabled: true
      }
    }

    aadProfile: {
      managed: true
      enableAzureRBAC: true
    }

    // Local accounts disabled: a kubeconfig with embedded credentials is exactly the long-lived
    // secret the secretless-identity rule prohibits.
    disableLocalAccounts: true

    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        count: systemPoolMinCount
        vmSize: nodeVmSize
        osType: 'Linux'
        osSKU: 'AzureLinux'
        availabilityZones: availabilityZones
        enableAutoScaling: true
        minCount: systemPoolMinCount
        maxCount: systemPoolMaxCount
        maxPods: 50
        // System pool is tainted so application workloads cannot land on it and compete with
        // cluster-critical components.
        nodeTaints: ['CriticalAddonsOnly=true:NoSchedule']
        upgradeSettings: {
          maxSurge: '33%'
        }
      }
      {
        name: 'controlplane'
        mode: 'User'
        count: controlPlanePoolMinCount
        vmSize: nodeVmSize
        osType: 'Linux'
        osSKU: 'AzureLinux'
        availabilityZones: availabilityZones
        enableAutoScaling: true
        minCount: controlPlanePoolMinCount
        maxCount: controlPlanePoolMaxCount
        maxPods: 50
        nodeLabels: {
          'groundwork.io/tier': 'controlplane'
        }
        upgradeSettings: {
          // Surge one node at a time. FR-043 requires rolling upgrades not to fail or duplicate an
          // in-flight deployment; a smaller surge keeps disruption bounded and predictable.
          maxSurge: '1'
        }
      }
      {
        name: 'executor'
        mode: 'User'
        count: executorPoolMinCount
        vmSize: nodeVmSize
        osType: 'Linux'
        osSKU: 'AzureLinux'
        availabilityZones: availabilityZones
        enableAutoScaling: true
        minCount: executorPoolMinCount
        maxCount: executorPoolMaxCount
        maxPods: 30
        nodeLabels: {
          'groundwork.io/tier': 'executor'
        }
        upgradeSettings: {
          maxSurge: '1'
        }
      }
    ]

    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkPolicy: 'cilium'
      networkDataplane: 'cilium'
      loadBalancerSku: 'standard'
      outboundType: 'loadBalancer'
      serviceCidr: '10.100.0.0/16'
      dnsServiceIP: '10.100.0.10'
      loadBalancerProfile: !empty(outboundPublicIpId)
        ? {
            outboundIPs: {
              publicIPs: [
                {
                  id: outboundPublicIpId
                }
              ]
            }
          }
        : null
    }

    autoUpgradeProfile: {
      // Patch-level auto-upgrade only. Minor upgrades stay a deliberate, reviewed change.
      upgradeChannel: 'patch'
      nodeOSUpgradeChannel: 'NodeImage'
    }

    addonProfiles: {
      omsagent: {
        enabled: true
        config: {
          logAnalyticsWorkspaceResourceID: logAnalyticsWorkspaceId
        }
      }
      azureKeyvaultSecretsProvider: {
        enabled: true
        config: {
          enableSecretRotation: 'true'
          rotationPollInterval: '2m'
        }
      }
    }

    // A pod disruption budget alone will not save an in-flight deployment; FR-043 is satisfied by
    // durable checkpoints (FR-035) plus graceful drain. This keeps drains orderly.
    autoScalerProfile: {
      'skip-nodes-with-local-storage': 'false'
      'skip-nodes-with-system-pods': 'true'
      'max-graceful-termination-sec': '600'
    }
  }
}

// A genuinely new pool, not part of `cluster.properties.agentPoolProfiles` above — verified live
// 2026-09-12: ARM rejects adding a pool to that array on an *existing* cluster outright
// ("Adding agent pools to an existing cluster is not allowed through managed cluster operations
// ... use per agent pool operations"), even though the same array works fine for pools present at
// cluster creation. The dedicated `agentPools` child resource type is the sanctioned way to add
// one afterward, and works identically whether the cluster is new or already running.
resource spotControlPlanePool 'Microsoft.ContainerService/managedClusters/agentPools@2024-10-01' = if (enableSpotControlPlanePool) {
  parent: cluster
  name: 'spotcp'
  properties: {
    mode: 'User'
    count: spotControlPlanePoolMinCount
    vmSize: nodeVmSize
    osType: 'Linux'
    osSKU: 'AzureLinux'
    availabilityZones: availabilityZones
    enableAutoScaling: true
    minCount: spotControlPlanePoolMinCount
    maxCount: spotControlPlanePoolMaxCount
    maxPods: 50
    scaleSetPriority: 'Spot'
    // Delete, not Deallocate: a reclaimed node's own local state (none held here - the
    // control plane is stateless) is not worth paying to keep. Deallocate would keep billing
    // for the underlying disk with no benefit here.
    scaleSetEvictionPolicy: 'Delete'
    // -1 means "pay up to the on-demand price" - eviction only happens on genuine Azure
    // capacity reclaim, never because a price cap was undercut.
    spotMaxPrice: -1
    nodeLabels: {
      'groundwork.io/tier': 'controlplane'
    }
    // The taint every Spot pool needs by convention: nothing schedules here unless it explicitly
    // tolerates it (k8s/controlplane/deployment.tmpl.yaml). Prevents anything *else* in the
    // cluster from silently landing on interruptible capacity.
    nodeTaints: ['kubernetes.azure.com/scalesetpriority=spot:NoSchedule']
    // No upgradeSettings/maxSurge here - verified live 2026-09-12, ARM rejects it outright
    // ("Spot pools can't set max surge"): surge exists to guarantee replacement capacity during a
    // node-image upgrade, which Spot's whole premise (reclaimable, not guaranteed) can't promise.
  }
}

// Federated credentials bind Kubernetes service accounts to Azure identities, so no secret is ever
// stored in the cluster (FR-044).
resource controlPlaneFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  name: '${last(split(controlPlaneIdentityId, '/'))}/gw-controlplane'
  properties: {
    issuer: cluster.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:groundwork:controlplane'
    audiences: ['api://AzureADTokenExchange']
  }
}

resource orchestratorFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  name: '${last(split(orchestratorIdentityId, '/'))}/gw-orchestrator'
  properties: {
    issuer: cluster.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:groundwork:orchestrator'
    audiences: ['api://AzureADTokenExchange']
  }
}

// Azure Kubernetes Service RBAC Cluster Admin. Verified 2026-07-30 against the live subscription
// with `az role definition list --name "Azure Kubernetes Service RBAC Cluster Admin"`.
//
// This is what makes `azd deploy`'s kubectl step (and any manual kubectl) work for the operator
// without a kubeconfig admin credential ever existing — disableLocalAccounts above means there is
// no such credential to leak. Azure RBAC is the only path in, and this is that path, granted
// automatically rather than left as a manual step discovered only when `azd deploy` fails.
//
// Scoped to this cluster alone, not the subscription: an operator who can deploy Groundwork does
// not thereby gain cluster-admin on every AKS cluster in the tenant.
var aksRbacClusterAdminRoleId = 'b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b'

resource operatorClusterAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorPrincipalId)) {
  scope: cluster
  name: guid(cluster.id, operatorPrincipalId, aksRbacClusterAdminRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      aksRbacClusterAdminRoleId
    )
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

// AcrPull for the *kubelet* identity, not the cluster's control-plane identity above. AKS
// auto-generates a separate identity (in the managed MC_* resource group) specifically for nodes
// pulling images, and it doesn't exist until the managedClusters resource does — which is why this
// grant lives here rather than in registry.bicep. Verified live 2026-07-31 with
// `az aks show --query identityProfile.kubeletidentity` after granting AcrPull to the wrong
// (control-plane) identity produced a real 401 Unauthorized on image pull.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

resource kubeletAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  // Named from cluster.id, not the kubelet identity's objectId: that objectId is a runtime property
  // of the cluster resource, and roleAssignments' `name` must be computable at deployment start
  // (BCP120). cluster.id is deterministic from the resource declaration, so it still ties this
  // assignment uniquely to this cluster+registry pair even though `properties.principalId` below
  // resolves at deployment time.
  name: guid(registry.id, cluster.id, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      acrPullRoleId
    )
    principalId: cluster.properties.identityProfile.kubeletidentity.objectId
    principalType: 'ServicePrincipal'
  }
}

output clusterName string = cluster.name
output oidcIssuerUrl string = cluster.properties.oidcIssuerProfile.issuerURL
output clusterFqdn string = cluster.properties.fqdn
