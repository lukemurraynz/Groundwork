# Groundwork: Product Specification

*AI-Driven Autonomous Data Platform Provisioning*

**Version**: 1.4
**Status**: Draft
**Target Platform**: Microsoft Azure, Microsoft Fabric, Azure DevOps/GitHub, Microsoft Foundry

> This document is the authoritative product input for this repository. Design decisions made
> against it are recorded in `docs/adr/`, not in this file. Amend this
> file only when the product intent itself changes, and bump the version when you do.

## Platform decisions

Decisions that amend the originally supplied v1.0 specification. Recorded here so downstream specs
inherit them and the deviation from the original text stays visible.

Records below are grouped by topic rather than by number; use this index to navigate.

| ID | Decision | Date |
| --- | --- | --- |
| [PD-001](#pd-001-microsoft-foundry-replaces-copilot-studio-2026-07-30) | Microsoft Foundry replaces Copilot Studio | 2026-07-30 |
| [PD-002](#pd-002-aks-hosting-and-production-from-day-one-posture-2026-07-30) | AKS hosting; production from day one | 2026-07-30 |
| [PD-003](#pd-003-product-name-groundwork-2026-07-30) | Product name: Groundwork | 2026-07-30 |
| [PD-004](#pd-004-groundwork-is-delivered-by-azure-developer-cli-not-cicd-2026-07-30) | Delivered by Azure Developer CLI, not CI/CD | 2026-07-30 |
| [PD-005](#pd-005-split-residency-with-per-tenant-consent-for-voice-2026-07-30) | Split residency with per-tenant consent for voice | 2026-07-30 |

### PD-001: Microsoft Foundry replaces Copilot Studio (2026-07-30)

**Decision**: the conversational and agentic layer is hosted on Microsoft Foundry. The voice channel
is delivered with Foundry primitives: Azure AI Voice Live for real-time speech-to-speech, and Azure
Communication Services Call Automation for the PSTN/telephony leg. Microsoft Copilot Studio is out of
scope and MUST NOT be introduced without product-owner approval and a funded licence.

**Rationale**: no budget exists for Copilot Studio IVR licensing. Foundry-hosted agents built with
Microsoft Agent Framework cover the same conversational responsibilities using capacity already in
scope for this product.

**Consequence**: every reference to "Copilot Studio" in the v1.0 text below is superseded by
"Microsoft Foundry". The channel list and architecture diagram in this document have been updated
accordingly. This decision is a hard gate: a future change proposing Copilot Studio must reopen
this PD explicitly rather than reintroducing it quietly.

**Open item**: the exact Voice Live and ACS Call Automation service configuration, regional
availability, and licensing/consumption model are version-sensitive and MUST be verified against
live Microsoft Learn documentation during the planning phase, then recorded with a `[VERIFIED]` block.
Treat this as `[VERIFY]` until then.

### PD-002: AKS hosting and production-from-day-one posture (2026-07-30)

**Decision**: the provisioning service runs on Azure Kubernetes Service: stateless request handling,
zone-redundant node pools, AKS workload identity, and deployment state held in durable external
storage. Every release targets production standards from first ship; there is no pilot, MVP, or
proof-of-concept phase.

**Rationale**: the product provisions production infrastructure in customers' tenants from day one, so
a pilot-grade implementation carries the same risk with fewer controls. AKS provides the horizontally
scalable, zone-redundant orchestration the non-functional requirements already demand, and aligns with
the delivery team's Azure Kubernetes, open-source, Well-Architected, and reliability expertise.

**Consequence**: the roadmap phases below describe deferred *capabilities*, not deferred quality.
Reliability, security, observability, disaster recovery, and cost governance ship with each capability.
Design and review are assessed against the Azure Well-Architected Framework pillars, and AKS hosting
is treated as a fixed constraint (see PD-002 above), not an option to revisit casually.

### PD-005: Split residency with per-tenant consent for voice (2026-07-30)

**Decision**: persisted conversation content stays in Australian regions. Transient in-call audio inference
may be processed outside Australian regions, as a bounded exception, and only for tenants who have given
explicit recorded consent. Tenants who decline keep full capability over chat and the REST API.

**Rationale**: Phase 0 verification established that every Azure AI Voice Live model available in
`australiaeast` is offered only as a **Global standard** deployment; `Regional` and `Data zone standard`
are not available there for any Voice Live model. Global standard does not guarantee that generative
inference stays inside the Australian geography, which conflicted with the residency commitment made in
PD-001's channel choice and in the conversation-data decision. Since raw audio is already discarded at
transcription, the offshore exposure is limited to in-flight audio and is never a stored asset.

**Why consent is per tenant rather than per release**: the alternative was to remove the voice channel
entirely to protect residency, which would have withdrawn the capability from the majority of customers who
would have accepted the disclosure. Making consent a tenant attribute puts the compliance decision where the
obligation actually sits (with the customer) and costs nothing in capability, because no capability is
voice-only.

**Consequence**: the exception is enforced in code, not by infrastructure. A future change that persists an
intermediate inference artefact would breach residency without tripping any Azure control, so the residency
test must assert on stored artefacts and their regions rather than on configuration.

**Not settled by this decision**: whether the voice channel earns its build cost in Release 1 remains an open
product question. Voice cannot complete an irreversible action on its own; confirmation must come through a
channel yielding a durable artefact, so it gathers requirements and hands off. That trade-off is worth
deciding on product grounds before voice implementation begins.

### PD-004: Groundwork is delivered by Azure Developer CLI, not CI/CD (2026-07-30)

**Decision**: Groundwork's own platform (AKS, Cosmos DB, Foundry project and agents, observability,
immutable storage) is provisioned and deployed with the Azure Developer CLI (`azd`). There is no CI/CD
pipeline in the release path for this release.

**Scope boundary: this does not change the product.** The Azure DevOps projects, repositories, and
pipelines in the DevOps section and in feature FR-038 are a **customer-facing capability** that Groundwork
creates *inside customer tenants*. They are unaffected. `azd` is how we ship Groundwork; Azure DevOps
pipelines are part of what Groundwork builds for a customer. Conflating the two would either put a
customer-facing pipeline in our release path or impose an `azd` dependency on a customer tenant.

**Rationale**: product-owner decision. `azd` gives one deterministic, previewable command set
(`azd provision --preview`, `azd provision`, `azd deploy`) covering infrastructure, services, and
Foundry-hosted agents together, and keeps agent definitions versioned with the release that expects them.

**Consequence and accepted risk**: a local CLI deploy has no automated quality gate and no independent
record of who shipped which commit, which is in tension with the Well-Architected review posture (PD-002)
and the audit-by-construction rule. Six manual
compensating controls are therefore mandatory and are documented in
`docs/release-checklist.md`: tag the deployed commit, run and record the full
test suite before deploying, always preview first and retain the output, never `kubectl apply`, deploy only
from a clean tree, and record deployer and tag in the audit log. This risk is accepted for this release and
should be revisited before Groundwork carries production customer deployments. Adopting a pipeline later is
additive, not a migration: the same `azd` commands run unchanged inside one.

### PD-003: Product name: Groundwork (2026-07-30)

**Decision**: the product is named **Groundwork**. The internal codename is **Ironbark**. `AutoMateDP`
is retired as a product name and survives only as the current repository name.

**Rationale**: Groundwork describes what the product does: it lays the foundation a customer's data
teams then build on, and claims preparation and solidity rather than intelligence or autonomy, so there
is nothing in the name to disprove. It survives the Phase 3 shift to lifecycle management, works as a CLI
verb and chart name, and reads as a name a real naming process would produce. Selection method, candidate
pool, screening, and weighted scoring: the project's naming research (not included in this release).

**Status 2026-07-30: confirmed, with accepted residual risk.** Screening after the initial decision found a
material collision: **GroundWork Open Source, Inc.** has traded since 2004 as *GroundWork Monitor
Enterprise*, an IT and cloud infrastructure monitoring platform with an Azure connector, sold through the MSP
and channel-partner motion: identical word, same software classes, same buyer channel. All tested
`groundwork.*` domains are registered. **Ironbark** is withdrawn (product-owner preference, plus an existing
registered Australian software mark). Evidence: the project's naming research (not included in this release) § Screening results.

The product owner reviewed this and elected to keep **Groundwork**. The risk is accepted, not resolved.

**Consequences that remain in force**:

- No `groundwork.*` domain is available, so the deployment-plan schema `$id` stays a URN
  (`urn:groundwork:schemas:deployment-plan:1.0.0`). Adopt an HTTPS `$id` only once a domain is actually owned.
  This is a domain-ownership fact, independent of the naming decision.
- A trademark opinion should precede any contractual, marketplace, or trademark-application use. Nothing here
  substitutes for one.
- The qualified form **Groundwork Platform** is recommended for formal and market-facing contexts, with plain
  *Groundwork* in speech and internal use.

Screening also found an existing Fortra product called **AutoMate**, so reviving `AutoMateDP` is not an
option either.

## Executive summary

The AI-Driven Autonomous Data Platform Provisioning solution enables organisations to provision an
enterprise-ready data platform within their own Microsoft Azure tenant using natural language
through voice or chat.

Customers interact with an AI agent via a phone call, Microsoft Teams, web portal, or other
supported channels. The AI gathers deployment requirements, authenticates the customer, validates
the target environment, generates a deployment plan, estimates costs, and orchestrates a fully
automated deployment using approved Infrastructure-as-Code (IaC) artefacts and release pipelines.

The solution reduces deployment time from days or weeks to less than one hour while maintaining
enterprise governance, security, repeatability, and auditability.

## Vision

To create an autonomous deployment platform that allows customers to provision, configure, upgrade,
and manage enterprise data platforms through natural conversation, while ensuring every deployment
adheres to organisational standards, governance policies, and security requirements.

## Problem statement

Deploying enterprise data platforms currently involves manual customer onboarding, infrastructure
engineers, DevOps engineers, platform engineers, multiple approval processes, repetitive deployment
activities, human configuration errors, long deployment lead times, and inconsistent
implementations.

This results in high operational costs, slow customer onboarding, configuration drift, reduced
scalability, and increased support effort.

## Product goals

The product shall:

- Enable customers to request deployments using voice or natural language.
- Eliminate manual provisioning activities.
- Standardise deployments using approved templates.
- Reduce deployment time to under 60 minutes.
- Maintain enterprise governance.
- Provide complete deployment traceability.
- Support multi-tenant deployments.
- Enable future lifecycle management using AI.

## Target users

**Primary**: enterprise customers, platform administrators, cloud engineers, managed service
providers, professional services teams.

**Secondary**: support engineers, customer success teams, operations teams, sales engineers.

## Supported channels

The deployment experience shall be available through Foundry-hosted voice (Azure AI Voice Live over
Azure Communication Services telephony, see PD-001), Microsoft Teams, a web portal, a REST API, a
CLI, and future mobile applications.

Voice is one interface; all channels use the same orchestration backend.

## Product architecture

```text
Customer
      │
Voice / Chat / Teams / Portal
      │
      ▼
Conversational AI
(Microsoft Foundry — Agent Framework
 agents + Voice Live / ACS telephony)
      │
      ▼
Planning Agent
      │
      ▼
Validation Agent
      │
      ▼
Deployment Orchestrator
      │
      ▼
Azure APIs
Microsoft Graph
Azure DevOps / GitHub
Microsoft Fabric
Azure Resource Manager
Terraform
Bicep
PowerShell
```

## Core components

### Conversational AI

Responsibilities: understand customer intent, guide deployment conversations, gather deployment
requirements, authenticate customers, present deployment plans, provide deployment status updates.

Supported interaction modes: voice, chat, Teams, portal.

### Planning Agent

Responsible for transforming conversational requests into structured deployment plans.

Example request:

> Deploy our standard production Fabric platform into Australia East using our enterprise landing
> zone.

Produces: deployment configuration, required infrastructure, dependencies, estimated duration,
estimated monthly cost, deployment risk assessment.

### Validation Agent

Validates customer identity, Azure tenant accessibility, Azure subscription, landing zone
readiness, required permissions, service quotas, naming conventions, Azure Policy compliance,
existing deployments, and version compatibility.

### Security Agent

Validates RBAC requirements, Managed Identity availability, Key Vault configuration, network
security, Private Endpoint requirements, customer governance standards, and least-privilege
deployment permissions.

### Cost Agent

Calculates monthly Azure costs, Fabric capacity requirements, storage estimates, networking costs,
monitoring costs, and cost optimisation recommendations.

### Deployment Orchestrator

Responsible for executing deployment workflows, managing long-running operations, tracking
deployment state, retry logic, rollback, notifications, and health monitoring.

The orchestrator executes deterministic automation and does not rely on AI decision-making during
deployment execution.

## Authentication model

- **Enterprise App Registration** — customer grants consent to a multi-tenant application.
  Preferred deployment model.
- **Federated Identity** — supports workload identity federation for secretless authentication.
- **Managed Identity** — supported for Azure-hosted deployment components.
- **Interactive Sign-in** — supported for customer-initiated deployments.

## Deployment workflow

**Phase 1**: customer requests deployment, conversation begins, customer authenticates.

**Phase 2**: Planning Agent generates deployment plan, Validation Agent validates environment,
cost estimate generated → customer confirms.

**Phase 3**: deployment request submitted, deployment ID generated, workflow queued.

**Phase 4**: deployment executes. Typical stages:

- Create Azure DevOps or GitHub project.
- Import repositories.
- Deploy infrastructure.
- Configure networking.
- Configure identities.
- Deploy data platform.
- Configure monitoring.
- Execute validation tests.

**Phase 5**: deployment report generated, customer notified, deployment archived.

## Supported deployments

**Infrastructure**: Resource Groups, VNets, Private Endpoints, Key Vault, Log Analytics,
Application Insights, Managed Identities.

**Data services**: Microsoft Fabric, Azure Data Factory, Azure Synapse Analytics (where
applicable), Azure Databricks, Azure SQL, Azure Storage, Event Hubs, Service Bus, Azure Functions,
Logic Apps, Microsoft Purview.

**DevOps**: Azure DevOps Projects, GitHub Repositories, Pipelines, Service Connections, Variable
Groups, Deployment Environments.

## MCP tool catalogue

The MCP server exposes reusable tools including:

| Domain | Tools |
| --- | --- |
| Customer | `ValidateCustomer()`, `AuthenticateCustomer()` |
| Azure | `ListSubscriptions()`, `ValidateTenant()`, `ValidateLandingZone()`, `ListRegions()` |
| Deployment | `GenerateDeploymentPlan()`, `EstimateCosts()`, `CreateDeployment()` |
| Azure DevOps / GitHub | `CreateProject()`, `ImportRepository()`, `ConfigurePipelines()` |
| Infrastructure | `DeployInfrastructure()`, `ConfigureNetworking()`, `ConfigureSecurity()` |
| Platform | `DeployFabric()`, `DeployADF()`, `DeployDatabricks()`, `DeployPurview()` |
| Operations | `RunHealthChecks()`, `GenerateDeploymentReport()`, `RollbackDeployment()`, `GetDeploymentStatus()` |

## AI responsibilities

AI **is** responsible for: understanding customer intent, asking clarifying questions, building
deployment plans, recommending architectures, explaining deployment status, producing documentation.

AI **is not** responsible for: executing deployments directly, making security decisions without
validation, bypassing approval workflows, modifying production infrastructure without explicit
confirmation.

## Governance

Every deployment shall include approval workflow support, audit logging, change history, version
tracking, policy validation, cost tracking, resource tagging, and compliance validation.

## Observability

The platform shall provide a real-time deployment dashboard, deployment timeline, AI conversation
history (where compliant with organisational policy), OpenTelemetry traces, Azure Monitor
integration, Log Analytics, Application Insights, and alerting.

## Notifications

Customers receive updates through voice callback (optional), email, Microsoft Teams, SMS, the web
portal, and the REST API.

## Non-functional requirements

**Availability**: 99.9% service availability.

**Security**: Microsoft Entra ID authentication; RBAC enforcement; encryption in transit and at
rest; secretless authentication where possible; Azure Key Vault integration.

**Performance**: deployment request validation under 30 seconds; deployment plan generation under
60 seconds; standard deployment completion under 60 minutes (subject to Azure provisioning times).

**Scalability**: support concurrent deployments across multiple customer tenants; stateless
front-end services; horizontally scalable orchestration.

**Reliability**: automatic retries; idempotent deployment operations; rollback support; resume
after transient failures.

## Roadmap

Phases sequence **capabilities**, not quality tiers. Per PD-002, every phase ships to production
standards; nothing below is a pilot or a proof of concept.

### Phase 1: Release 1 (Production)

- Voice and chat deployment requests.
- Microsoft Entra ID authentication.
- Azure DevOps integration.
- Infrastructure deployment.
- Microsoft Fabric deployment.
- Deployment reporting.

### Phase 2: Enterprise Expansion

- GitHub support.
- Advanced approval workflows.
- Cost optimisation recommendations.
- Multi-region deployments.
- Customer web portal.
- Teams application.

### Phase 3: Autonomous Platform Operations

- Platform upgrades.
- AI-assisted scaling.
- Drift detection.
- Self-healing automation.
- Compliance remediation.
- Continuous optimisation recommendations.

## Success metrics

**Operational**: 90% reduction in manual deployment effort; 80% reduction in deployment time; 95%
deployment success rate on first execution; zero manual infrastructure configuration for standard
deployments.

**Business**: faster customer onboarding; lower operational costs; increased deployment
consistency; improved customer satisfaction; reduced engineering overhead.

## Future vision

The long-term vision extends beyond deployment. The platform becomes an autonomous AI platform
engineering service capable of managing the full lifecycle of enterprise data platforms. Customers
interact using natural language to provision new environments, apply upgrades, enable additional
services, remediate compliance issues, optimise costs, and monitor operational health. AI agents
provide planning, validation, and operational intelligence, while deterministic orchestration
ensures secure, repeatable, and governed execution across every customer tenant.
