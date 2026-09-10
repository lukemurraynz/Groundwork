"""Typed configuration, validated at startup.

Two rules shape this module:

**Fail fast with a named key.** A service that starts with missing configuration and fails on the
first request has converted a deployment error into a customer-visible incident. Every required
value is checked at startup and the error names the exact environment variable.

**Nothing production-connected by default.** There is no default endpoint, no default subscription,
no fallback connection string. A developer running locally gets an explicit error, never an
accidental connection to a customer tenant.

Governance values that must not be adjustable from a conversation — the approval threshold, the
concurrency cap, the residency allow-list — live here because configuration is exactly where
FR-020a and FR-045a require them to be.

``GROUNDWORK_REQUIRE_STEP_UP_APPROVAL`` defaults to enabled: approval routes require token evidence
of stronger or fresh authentication (``amr`` containing ``mfa`` or an ``iat`` within the last 10
minutes). Microsoft Entra Conditional Access still performs the real MFA challenge; this module
only makes the control plane fail closed when that evidence is missing. Set it to ``false`` per
environment if that's too strict for local development — see ADR-0011 for why the default is on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final

AUSTRALIAN_REGIONS: Final[frozenset[str]] = frozenset({"australiaeast", "australiasoutheast"})


class ConfigurationError(RuntimeError):
    """Required configuration is missing or invalid.

    Raised only at startup. Deliberately not caught anywhere in request handling — a service with
    invalid configuration should not be serving traffic.
    """


# A real incident, not a hypothetical (2026-08-06): `k8s/orchestrator/deployment.tmpl.yaml`
# substitutes `{{.Env.GROUNDWORK_FABRIC_CAPACITY_ADMIN_UPN}}` at `azd deploy` time. When that azd
# env key was never set at all (not even to an empty string — genuinely absent from the
# environment), Go's `text/template` renders the literal 10-character string `<no value>` rather
# than an empty string — a well-known Go template behaviour for a missing map key, not a bug in
# this codebase's own template. That string is non-empty, so `_require`'s emptiness check let it
# through, and the orchestrator started "successfully" with a required identity field silently
# holding this garbage sentinel instead of failing closed as every other missing-required-config
# path in this module does. Rejected explicitly here so *any* required setting rendered this way —
# not just the two that surfaced it — fails the same loud, named way as a genuinely empty value.
_GO_TEMPLATE_MISSING_KEY_SENTINEL = "<no value>"


def _require(name: str, env: dict[str, str]) -> str:
    value = env.get(name, "").strip()
    if not value or value == _GO_TEMPLATE_MISSING_KEY_SENTINEL:
        raise ConfigurationError(
            f"required configuration {name!r} is missing or empty. Set it via "
            f"`azd env set {name} <value>` or the pod environment. There is no default, "
            f"because a default here could point at the wrong tenant."
        )
    return value


def _optional(name: str, env: dict[str, str]) -> str | None:
    """Optional setting: ``None`` for absent, empty, *or* the Go-template sentinel.

    The sentinel check matters exactly as much here as it does in :func:`_require` — the
    2026-08-06 incident (see ``_GO_TEMPLATE_MISSING_KEY_SENTINEL``) only stayed caught because
    ``_require`` rejected it, and an optional field rendered ``<no value>`` by the k8s manifest
    would otherwise carry that literal string as its configured value.
    """
    value = env.get(name, "").strip()
    if not value or value == _GO_TEMPLATE_MISSING_KEY_SENTINEL:
        return None
    return value


def _require_int(name: str, env: dict[str, str], *, minimum: int, maximum: int) -> int:
    raw = _require(name, env)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"configuration {name!r} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"configuration {name!r} must be between {minimum} and {maximum}, got {value}"
        )
    return value


def _require_float(name: str, env: dict[str, str], *, minimum: float) -> float:
    raw = _require(name, env)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"configuration {name!r} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigurationError(f"configuration {name!r} must be at least {minimum}, got {value}")
    return value


def _int_with_default(name: str, env: dict[str, str], *, default: int, minimum: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    value_text = raw.strip()
    if not value_text or value_text == _GO_TEMPLATE_MISSING_KEY_SENTINEL:
        return default
    try:
        value = int(value_text)
    except ValueError as exc:
        raise ConfigurationError(
            f"configuration {name!r} must be an integer, got {value_text!r}"
        ) from exc
    if value < minimum:
        raise ConfigurationError(f"configuration {name!r} must be at least {minimum}, got {value}")
    return value


def _optional_bool(name: str, env: dict[str, str], *, default: bool = False) -> bool:
    value = env.get(name)
    if value is None:
        return default
    normalised = value.strip().lower()
    if not normalised or normalised == _GO_TEMPLATE_MISSING_KEY_SENTINEL:
        return default
    if normalised in {"1", "true", "yes"}:
        return True
    if normalised in {"0", "false", "no"}:
        return False
    raise ConfigurationError(
        f"configuration {name!r} must be a boolean (true/false/1/0/yes/no), got {value!r}"
    )


@dataclass(frozen=True, slots=True)
class GovernanceSettings:
    """Governance thresholds. Configuration only — never conversation-adjustable.

    FR-020a and FR-045a both require these to be configuration rather than something a caller can
    influence. Holding them in a frozen dataclass loaded once at startup means there is no setter
    for a request handler to reach.

    ``require_step_up_approval`` is deliberately only a token-evidence gate: Microsoft Entra
    Conditional Access still configures and enforces the actual MFA challenge, while this setting
    makes approval fail closed when the validated token lacks evidence of that stronger sign-in.
    Defaults on (secure by default, matching every other gate in this codebase); an operator can
    opt out per environment with ``GROUNDWORK_REQUIRE_STEP_UP_APPROVAL=false`` (see ADR-0011).
    """

    approval_threshold_aud: float
    approver_role: str
    default_tenant_concurrency_cap: int
    require_step_up_approval: bool = True

    def requires_second_approver(self, monthly_total_aud: float, environment: str) -> bool:
        if environment != "production":
            return False
        return monthly_total_aud >= self.approval_threshold_aud


@dataclass(frozen=True, slots=True)
class ResidencySettings:
    """Data residency constraints (FR-053b, PD-005)."""

    storage_region: str
    allowed_regions: frozenset[str] = field(default=AUSTRALIAN_REGIONS)

    def __post_init__(self) -> None:
        if self.storage_region not in self.allowed_regions:
            raise ConfigurationError(
                f"storage region {self.storage_region!r} is not in the allowed set "
                f"{sorted(self.allowed_regions)}. FR-053b requires persisted conversation "
                f"content to remain in Australian regions."
            )


@dataclass(frozen=True, slots=True)
class EntraSettings:
    """The Entra application inbound tokens are validated against (FR-005, FR-007).

    Populated by ``scripts/postprovision.ps1``/``.sh`` at provision time, not hand-configured — the
    application is created and its identifiers recorded automatically, so this is read-only
    consumption of that output.
    """

    tenant_id: str
    client_id: str
    audience: str
    # Optional: the redirect_uri registered on the app for the admin-consent endpoint
    # (api/tenants.py). Absent means onboarding consent-url generation isn't configured yet —
    # same "built but not broken" pattern as Settings.voice_live_endpoint.
    redirect_uri: str | None = None


@dataclass(frozen=True, slots=True)
class FoundrySettings:
    """The Microsoft Foundry project the planning agent (T045) runs against.

    PD-002/PD-003: Microsoft Foundry, not Copilot Studio; the native Microsoft Agent Framework
    against a Foundry-hosted agent. Populated from ``infra/modules/foundry.bicep`` outputs — the
    project and its model deployment are provisioned infrastructure, not hand-configured.
    """

    project_endpoint: str
    model_deployment: str


@dataclass(frozen=True, slots=True)
class ReadinessSettings:
    """Configuration T036-T042's readiness checks need beyond what a plan request supplies.

    ``orchestrator_principal_id`` is the object id of the identity that will execute a deployment
    (CL-004: one workload identity per component this release, not per customer tenant — see
    ``groundwork_shared.identity.credentials``'s module docstring) — populated from
    ``infra/main.bicep``'s ``GROUNDWORK_ORCHESTRATOR_PRINCIPAL_ID`` output, not hand-configured.

    ``devops_organization_url`` is genuinely optional: a newly onboarded tenant may not have
    connected an Azure DevOps organization yet, and ``devops.py``'s check already treats an absent
    value as UNREACHABLE rather than a configuration error — see that module's docstring.

    ``controlplane_principal_id`` is the object id of the control plane's own identity —
    populated from ``infra/main.bicep``'s ``GROUNDWORK_CONTROLPLANE_PRINCIPAL_ID`` output, the
    same convention as ``orchestrator_principal_id``. Needed because
    ``api/tenants.py``'s ``grant_customer_ado_org_access`` calls Azure DevOps *as* the control
    plane's own identity: found live 2026-09-07 that a brand-new Azure DevOps organization has
    never heard of that identity either, so the automated call 401s with a misleading
    "sign in at least once" error regardless of what the *target* (orchestrator) identity's own
    entitlement state is — a customer following only the orchestrator-entitlement instructions
    that error implied stays stuck forever. Optional, matching ``devops_organization_url``'s own
    pattern: absent until this identity has been introduced to at least one Azure DevOps
    organization, which is itself a real precondition, not a defect.
    """

    orchestrator_principal_id: str
    devops_organization_url: str | None = None
    controlplane_principal_id: str | None = None
    orchestrator_display_name: str | None = None
    """The orchestrator identity's ADO-facing display name (``id-gw-orch-<resourceToken>``),
    populated from ``infra/main.bicep``'s ``GROUNDWORK_ORCHESTRATOR_DISPLAY_NAME`` output. The
    Azure DevOps Project Collection Administrators people-picker (Organization Settings >
    Permissions) resolves a display name far more reliably than the bare object id in
    ``orchestrator_principal_id`` — found live 2026-09-07 building the onboarding instructions this
    name is used in. Optional, matching the other readiness fields' pattern: absent falls back to
    the object id, a worse but still-usable instruction, not a broken one."""


@dataclass(frozen=True, slots=True)
class Settings:
    """All configuration required to start a Groundwork service."""

    environment_name: str
    azure_location: str
    cosmos_endpoint: str
    storage_account_url: str
    key_vault_uri: str
    governance: GovernanceSettings
    residency: ResidencySettings
    entra: EntraSettings
    foundry: FoundrySettings
    readiness: ReadinessSettings
    applicationinsights_connection_string: str | None = None
    blueprints_path: str | None = None
    # Voice Live endpoint (optional): the WebSocket URL for Azure AI Voice Live in australiaeast.
    # When absent, voice endpoints return 503 — voice is built but not configured, not broken.
    voice_live_endpoint: str | None = None
    # ACS Email (optional): the Communication Services endpoint and sender address used to send
    # tenant onboarding invites and deployment notifications. When either is absent, email-sending
    # routes return 503 — built but not configured, not broken (same pattern as voice endpoint).
    acs_email_endpoint: str | None = None
    acs_email_sender_address: str | None = None

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> Settings:
        """Load and validate configuration.

        Args:
            env: Environment mapping. Defaults to ``os.environ``. Injectable so tests never
                mutate real process state.

        Raises:
            ConfigurationError: If any required value is missing or invalid. The message names
                the offending key.
        """
        source = dict(os.environ) if env is None else env

        location = _require("AZURE_LOCATION", source)

        governance = GovernanceSettings(
            approval_threshold_aud=_require_float(
                "GROUNDWORK_APPROVAL_THRESHOLD_AUD", source, minimum=0.0
            ),
            approver_role=_require("GROUNDWORK_APPROVER_ROLE", source),
            default_tenant_concurrency_cap=_require_int(
                "GROUNDWORK_TENANT_CONCURRENCY_CAP", source, minimum=1, maximum=50
            ),
            require_step_up_approval=_optional_bool(
                "GROUNDWORK_REQUIRE_STEP_UP_APPROVAL", source, default=True
            ),
        )

        residency = ResidencySettings(
            storage_region=source.get("GROUNDWORK_STORAGE_REGION", location).strip()
        )

        entra = EntraSettings(
            tenant_id=_require("AZURE_TENANT_ID", source),
            client_id=_require("GROUNDWORK_ENTRA_APP_CLIENT_ID", source),
            audience=_require("GROUNDWORK_ENTRA_APP_AUDIENCE", source),
            redirect_uri=source.get("GROUNDWORK_ENTRA_APP_REDIRECT_URI", "").strip() or None,
        )

        foundry = FoundrySettings(
            project_endpoint=_require("GROUNDWORK_FOUNDRY_PROJECT_ENDPOINT", source),
            model_deployment=_require("GROUNDWORK_FOUNDRY_MODEL_DEPLOYMENT", source),
        )

        readiness = ReadinessSettings(
            orchestrator_principal_id=_require("GROUNDWORK_ORCHESTRATOR_PRINCIPAL_ID", source),
            devops_organization_url=(_optional("GROUNDWORK_DEVOPS_ORGANIZATION_URL", source)),
            controlplane_principal_id=(_optional("GROUNDWORK_CONTROLPLANE_PRINCIPAL_ID", source)),
            orchestrator_display_name=(_optional("GROUNDWORK_ORCHESTRATOR_DISPLAY_NAME", source)),
        )

        return cls(
            environment_name=_require("AZURE_ENV_NAME", source),
            azure_location=location,
            cosmos_endpoint=_require("GROUNDWORK_COSMOS_ENDPOINT", source),
            storage_account_url=_require("GROUNDWORK_STORAGE_ACCOUNT_URL", source),
            key_vault_uri=_require("GROUNDWORK_KEY_VAULT_URI", source),
            governance=governance,
            residency=residency,
            entra=entra,
            foundry=foundry,
            readiness=readiness,
            # Optional: telemetry degrades to console logging if absent, which is a real
            # operational state rather than a silent fallback for a required dependency.
            applicationinsights_connection_string=(
                source.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip() or None
            ),
            # Optional: callers that need a blueprint catalogue (the control plane) fall back to a
            # repo-relative default that only resolves when running from a checkout, never inside a
            # container — a caller that needs it in a container must set this explicitly.
            blueprints_path=source.get("GROUNDWORK_BLUEPRINTS_PATH", "").strip() or None,
            voice_live_endpoint=(source.get("GROUNDWORK_VOICE_LIVE_ENDPOINT", "").strip() or None),
            acs_email_endpoint=(source.get("GROUNDWORK_ACS_EMAIL_ENDPOINT", "").strip() or None),
            acs_email_sender_address=(
                source.get("GROUNDWORK_ACS_EMAIL_SENDER_ADDRESS", "").strip() or None
            ),
        )


@dataclass(frozen=True, slots=True)
class OrchestratorSettings:
    """All configuration required to start the execution worker.

    Deliberately its own required-field set rather than reusing :class:`Settings` wholesale: the
    worker's own environment (``k8s/orchestrator/deployment.tmpl.yaml``) never carries
    ``GROUNDWORK_FOUNDRY_*``, ``GROUNDWORK_ENTRA_APP_*``, or
    ``GROUNDWORK_ORCHESTRATOR_PRINCIPAL_ID`` — those are control-plane-only concerns (the
    deterministic-execution boundary: no model client, no inbound token validation here) — so
    validating against the full
    :class:`Settings` shape would fail startup on keys this process is never given. Everything the
    worker's manifest *does* set is validated here, with the same fail-fast, named-key discipline
    as :class:`Settings`.
    """

    environment_name: str
    azure_location: str
    cosmos_endpoint: str
    storage_account_url: str
    key_vault_uri: str
    governance: GovernanceSettings
    residency: ResidencySettings
    allow_tenant_writes: bool
    max_concurrent_deployments: int
    # The orchestrator's own workload identity object id (``infra/main.bicep``'s
    # ``GROUNDWORK_ORCHESTRATOR_PRINCIPAL_ID`` output — the same value the control plane's
    # ``ReadinessSettings.orchestrator_principal_id`` reads, sourced from the same env var name for
    # consistency). Recorded as the ``actor`` on every audit record ``Sequencer`` writes
    # (``engine/sequencer.py``) — never the plan's or approval's identifier, which is a separate
    # concern (``AuthorityChain``). Required, no default: an orchestrator that cannot name its own
    # identity must not start executing against a customer tenant.
    orchestrator_principal_id: str
    # T081's fabric.py requires a UPN-format Entra user in the *customer's own* tenant to name as
    # Fabric capacity administrator — the ARM schema has no service-principal option. This is now
    # the FALLBACK, not the primary source: the per-tenant value lives on
    # ``CustomerTenant.fabric_capacity_admin_upn`` (gathered from the customer during
    # conversation), and the queue-consumption loop resolves tenant-record value first, this
    # worker-wide setting second — the single-engagement shape retained only so an environment
    # that already set it keeps working. Optional: a deployment whose tenant has neither value is
    # declined per-deployment at execution time (``queue_loop``'s AWAITING_TENANT_CONFIG) rather
    # than refusing worker startup — per-tenant data must be allowed to arrive per tenant.
    # Never guessed: Fabric capacity is billable to the customer, and blueprint.yaml's own
    # recovery path for this stage is halt-and-preserve precisely because an automatic teardown
    # would be financially material.
    fabric_capacity_admin_upn: str | None = None
    # Object id of the principal that VERIFIES Fabric workspaces post-provision (typically the
    # orchestrator MI). The fabric pipeline step grants it workspace Admin so read-only
    # validation can see the workspace (association-scoped lists). Optional.
    fabric_validator_object_id: str | None = None
    applicationinsights_connection_string: str | None = None
    # Genuinely optional — and now a FALLBACK, not the primary source: the per-tenant value lives
    # on ``CustomerTenant.devops_organization_url`` (the customer's own organization, gathered
    # from them), and the queue-consumption loop resolves tenant-record value first, this
    # worker-wide setting second. When both are absent for a deployment, the loop declines that
    # one deployment (AWAITING_TENANT_CONFIG) rather than refusing worker startup — an
    # unconfigured target is a per-engagement gap, not a worker-wide one.
    devops_organization_url: str | None = None
    # Optional, same reasoning as Settings.blueprints_path: the worker's own manifest
    # (docker/orchestrator.Dockerfile) sets GROUNDWORK_BLUEPRINTS_PATH to the baked-in
    # /app/blueprints copy; a local run outside a container falls back to the repo-relative default,
    # which only resolves from a checkout.
    blueprints_path: str | None = None
    acs_email_endpoint: str | None = None
    """ACS Email endpoint URL. When set alongside ``acs_email_sender_address``, the orchestrator
    constructs a ``_TenantNotifier`` and sends deployment-outcome emails to the address recorded
    on each ``CustomerTenant.notification_email``. When absent, notification is skipped rather
    than failing startup — matching the ``devops_organization_url`` optional pattern."""
    acs_email_sender_address: str | None = None
    """The 'from' address for ACS Email sends. Required when ``acs_email_endpoint`` is set;
    ignored otherwise."""
    drift_interval_seconds: int = 900

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> OrchestratorSettings:
        source = dict(os.environ) if env is None else env

        location = _require("AZURE_LOCATION", source)

        governance = GovernanceSettings(
            approval_threshold_aud=_require_float(
                "GROUNDWORK_APPROVAL_THRESHOLD_AUD", source, minimum=0.0
            ),
            approver_role=_require("GROUNDWORK_APPROVER_ROLE", source),
            default_tenant_concurrency_cap=_require_int(
                "GROUNDWORK_TENANT_CONCURRENCY_CAP", source, minimum=1, maximum=50
            ),
            require_step_up_approval=_optional_bool(
                "GROUNDWORK_REQUIRE_STEP_UP_APPROVAL", source, default=True
            ),
        )
        residency = ResidencySettings(
            storage_region=source.get("GROUNDWORK_STORAGE_REGION", location).strip()
        )

        # Local runs are read-only unless explicitly opted in. Without this, a developer running
        # the worker against their own credentials could write into a tenant by accident.
        allow_writes = source.get("GROUNDWORK_ALLOW_WRITES", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }

        return cls(
            environment_name=_require("AZURE_ENV_NAME", source),
            azure_location=location,
            cosmos_endpoint=_require("GROUNDWORK_COSMOS_ENDPOINT", source),
            storage_account_url=_require("GROUNDWORK_STORAGE_ACCOUNT_URL", source),
            key_vault_uri=_require("GROUNDWORK_KEY_VAULT_URI", source),
            governance=governance,
            residency=residency,
            allow_tenant_writes=allow_writes,
            max_concurrent_deployments=_require_int(
                "GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS", source, minimum=1, maximum=100
            ),
            drift_interval_seconds=_int_with_default(
                "GROUNDWORK_DRIFT_INTERVAL_SECONDS", source, default=900, minimum=0
            ),
            orchestrator_principal_id=_require("GROUNDWORK_ORCHESTRATOR_PRINCIPAL_ID", source),
            fabric_capacity_admin_upn=(_optional("GROUNDWORK_FABRIC_CAPACITY_ADMIN_UPN", source)),
            fabric_validator_object_id=_optional("GROUNDWORK_FABRIC_VALIDATOR_OBJECT_ID", source),
            applicationinsights_connection_string=(
                source.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip() or None
            ),
            devops_organization_url=(_optional("GROUNDWORK_DEVOPS_ORGANIZATION_URL", source)),
            blueprints_path=source.get("GROUNDWORK_BLUEPRINTS_PATH", "").strip() or None,
            acs_email_endpoint=source.get("GROUNDWORK_ACS_EMAIL_ENDPOINT", "").strip() or None,
            acs_email_sender_address=(
                source.get("GROUNDWORK_ACS_EMAIL_SENDER_ADDRESS", "").strip() or None
            ),
        )
