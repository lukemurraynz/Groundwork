// Log Analytics, Application Insights, and operator alerting.
//
// Alerting lives here rather than in a later phase on purpose. Voice was promoted above reporting,
// so without this the platform would take inbound customer calls with no alerting on failure rate,
// queue depth, or dependency health — a Well-Architected review gap. This is tasks T086a and T086b.
//
// API versions: pinned to versions the Bicep CLI can type-check.
//
// The subscription offers newer stable versions (workspaces 2026-03-01), but Bicep 0.43.8 has no
// type definitions for them and emits BCP081 — meaning no compile-time property validation. Trading
// three months of API recency for the ability to catch a mistyped property before deployment is the
// right trade here: validate before promise, and an unvalidated template promises
// more than it can prove. Revisit when the Bicep type index catches up.

@description('Azure region.')
param location string

@description('Deterministic suffix.')
param resourceToken string

@description('Tags applied to every resource.')
param tags object

@description('Log retention in days. 365 to match the FR-052a audit and report retention period.')
@minValue(30)
@maxValue(730)
param retentionInDays int = 365

@description('Principal ID of the control-plane workload identity, granted ingestion access.')
param controlPlanePrincipalId string

@description('Principal ID of the orchestrator workload identity, granted ingestion access.')
param orchestratorPrincipalId string

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-groundwork-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    // Public ingestion is disabled at the workspace. Telemetry may contain tenant identifiers,
    // which FR-049 treats as sensitive.
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-groundwork-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    // Local auth disabled: telemetry is written with workload identity, not an instrumentation
    // key. The secretless-identity rule prohibits long-lived credentials.
    DisableLocalAuth: true
    IngestionMode: 'LogAnalytics'
  }
}

// Monitoring Metrics Publisher — the ingestion-only role Azure Monitor's own error message names
// when a token-credential exporter is rejected: "your Application Insights resource may be
// configured incorrectly... has the correct `Monitoring Metrics Publisher` role assigned." Verified
// live 2026-07-31 with `az role definition list --name "Monitoring Metrics Publisher"`, after
// DisableLocalAuth above (correct, already set) turned out to be only half the requirement — the
// resource refusing local auth does not by itself grant anyone AAD-based ingestion access; that is
// a separate RBAC grant this module never made. Found via a live 403 on every telemetry export
// after wiring `groundwork_shared.telemetry.otel.configure_telemetry`'s workload-identity
// credential through for the first time; deliberately not AcrPush-style write access to the
// resource's configuration, only to its ingestion endpoint.
var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'

resource controlPlaneIngestion 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, controlPlanePrincipalId, monitoringMetricsPublisherRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      monitoringMetricsPublisherRoleId
    )
    principalId: controlPlanePrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource orchestratorIngestion 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, orchestratorPrincipalId, monitoringMetricsPublisherRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      monitoringMetricsPublisherRoleId
    )
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-groundwork-${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'gwops'
    enabled: true
    // Receivers are intentionally empty in the template. Adding an email or webhook here would
    // put an operator's address in source control. Configure receivers post-deployment.
    emailReceivers: []
  }
}

// FR-054: alert on deployment failure rate, stage duration breach, queue depth, and dependency
// unavailability. Thresholds are starting points to be tuned against real telemetry — the runbook
// (T112) owns that. A scheduled query rule is used because these are custom application metrics,
// not platform metrics.
resource deploymentFailureAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-gw-deployment-failures-${resourceToken}'
  location: location
  tags: tags
  properties: {
    displayName: 'Groundwork deployment failure rate'
    description: 'Fires when deployment failures exceed the tolerated rate (FR-054).'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    scopes: [logAnalytics.id]
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where Properties.event == "deployment_failed" | summarize Failures = count()'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 2
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

resource queueDepthAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-gw-queue-depth-${resourceToken}'
  location: location
  tags: tags
  properties: {
    displayName: 'Groundwork deployment queue depth'
    description: 'Fires when queued deployments accumulate, indicating the per-tenant cap or worker capacity needs attention (FR-045a, FR-054).'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT5M'
    // 30 minutes rather than 15. The original intent was "alert only when queue depth is
    // sustained", expressed as two failing evaluation periods — but ARM rejects more than one
    // period for a query that does not project TimeGenerated. Widening the window preserves the
    // intent (a transient spike still will not fire) within the platform's constraint.
    windowSize: 'PT30M'
    scopes: [logAnalytics.id]
    criteria: {
      allOf: [
        {
          query: 'AppMetrics | where Name == "groundwork_queue_depth" | summarize Depth = max(Max)'
          timeAggregation: 'Maximum'
          // Required whenever timeAggregation is not 'Count'. ARM rejects the rule with
          // "Metric Measure Column was not specified" otherwise — and Bicep cannot catch it,
          // because the constraint is conditional on another property's value.
          metricMeasureColumn: 'Depth'
          operator: 'GreaterThan'
          threshold: 20
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// FR-054: stage duration breach. Queries the custom histogram
// `groundwork_shared.telemetry.metrics.record_stage_duration` emits from
// `Sequencer._run_one_stage` — every real blueprint stage attempt, tagged by stage_name. Found and
// fixed 2026-08-02 alongside this alert: `alert-gw-deployment-failures` and `alert-gw-queue-depth`
// above already existed but queried a metric and event nothing in this codebase emitted (see
// `metrics.py`'s own module docstring) — this alert and its emitter were built together so it does
// not repeat that gap. No fixed per-stage threshold exists yet (stage durations vary widely, from
// `networking`'s read-only check to `infrastructure`'s full deployment-stack apply); 600 seconds
// (10 minutes) is a conservative starting point for *any* single stage attempt, to be tuned per
// stage against real telemetry by the runbook (T112), not a considered per-stage SLO.
resource stageDurationBreachAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-gw-stage-duration-breach-${resourceToken}'
  location: location
  tags: tags
  properties: {
    displayName: 'Groundwork stage duration breach'
    description: 'Fires when any single blueprint stage attempt runs longer than expected (FR-054).'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    scopes: [logAnalytics.id]
    criteria: {
      allOf: [
        {
          query: 'AppMetrics | where Name == "groundwork_stage_duration_seconds" | summarize LongestSeconds = max(Max)'
          timeAggregation: 'Maximum'
          metricMeasureColumn: 'LongestSeconds'
          operator: 'GreaterThan'
          threshold: 600
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// FR-054: dependency unavailability. Unlike the other three alerts, this needs no custom emitter —
// `configure_azure_monitor` (otel.py) auto-instruments every outgoing httpx/Azure SDK call as an
// AppDependencies entry with a real Success flag, for free. A failed dependency call does not
// always mean a stage failed (T072's own retry may absorb it), so this is deliberately a coarser,
// earlier-warning signal than `alert-gw-deployment-failures` — sustained dependency failure sends
// an operator looking before enough of it compounds into an actual halted deployment.
resource dependencyUnavailabilityAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-gw-dependency-unavailability-${resourceToken}'
  location: location
  tags: tags
  properties: {
    displayName: 'Groundwork dependency unavailability'
    description: 'Fires when outbound dependency calls (Azure APIs, Retail Prices, Azure DevOps) fail at a sustained rate (FR-054).'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    scopes: [logAnalytics.id]
    criteria: {
      allOf: [
        {
          query: 'AppDependencies | summarize FailurePerMille = 1000 * countif(Success == false) / count()'
          timeAggregation: 'Average'
          metricMeasureColumn: 'FailurePerMille'
          operator: 'GreaterThan'
          threshold: 100
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// SC-014 / T086b: availability SLO for the request and planning surface only. In-flight deployment
// progression is explicitly excluded — it is held to durable resumability (FR-030), not uptime.
resource availabilityAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-gw-availability-slo-${resourceToken}'
  location: location
  tags: tags
  properties: {
    displayName: 'Groundwork request surface availability SLO (99.9%)'
    description: 'Availability of authentication, plan generation, approval, and status queries. Excludes in-flight deployment progression per SC-014.'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT30M'
    scopes: [logAnalytics.id]
    criteria: {
      allOf: [
        {
          // Expressed in per-mille because Bicep has no floating-point literals: 999 per 1000
          // is 99.9%. Scaling in the query keeps the threshold exact rather than rounding the
          // SLO to 99% or 100%, either of which would be a different commitment.
          query: 'AppRequests | where Url !contains "/deployments/" or Name startswith "GET" | summarize SuccessPerMille = 1000 * countif(Success == true) / count()'
          timeAggregation: 'Average'
          metricMeasureColumn: 'SuccessPerMille'
          operator: 'LessThan'
          threshold: 999
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// FR-054 sibling: queue-loop heartbeat. The orchestrator emits one `queue_poll_cycle` event per
// 5s poll; this alert fires on its ABSENCE, which is the only reliable signal that the loop (and
// therefore execution + orphan recovery) has stopped - a hung cycle produces no error of its own.
resource queuePollHeartbeatAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-gw-queue-poll-heartbeat-${resourceToken}'
  location: location
  tags: tags
  properties: {
    displayName: 'Groundwork queue-poll heartbeat missing'
    description: 'Fires when the orchestrator queue-consumption loop stops emitting its per-cycle heartbeat (FR-054). Silence means hung or dead worker.'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT10M'
    scopes: [logAnalytics.id]
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where Properties.event == "queue_poll_cycle" | summarize Cycles = count()'
          timeAggregation: 'Count'
          operator: 'LessThan'
          threshold: 1
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// FR-054 sibling: drift detection paging. Fires the moment any tenant's platform readiness
// regresses into a blocking state between plan-time and reality.
resource driftDetectedAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-gw-platform-drift-${resourceToken}'
  location: location
  tags: tags
  properties: {
    displayName: 'Groundwork platform drift detected'
    description: 'Fires when drift-watch classifies an onboarded tenant subscription as drifted - a readiness check regressed into blocking after deployment (FR-054).'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    scopes: [logAnalytics.id]
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where Properties.event == "platform_drift_detected" | summarize Hits = count()'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// FR-054 sibling: drift-loop liveness. The loop emits one `drift_evaluation_cycle_completed`
// per cycle per pod even with zero eligible targets, so silence means the loop itself is dead.
resource driftLoopAbsenceAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-gw-drift-loop-heartbeat-${resourceToken}'
  location: location
  tags: tags
  properties: {
    displayName: 'Groundwork drift-watch loop missing'
    description: 'Fires when no drift evaluation cycle completes for an hour - the loop is hung or dead on every pod.'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT15M'
    windowSize: 'PT1H'
    scopes: [logAnalytics.id]
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where Properties.event == "drift_evaluation_cycle_completed" | summarize Cycles = count()'
          timeAggregation: 'Count'
          operator: 'LessThan'
          threshold: 1
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

output logAnalyticsWorkspaceId string = logAnalytics.id
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output actionGroupId string = actionGroup.id
