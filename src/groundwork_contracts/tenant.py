"""Customer tenant, consent, and conversation records.

Three independent access concepts live here and must not be conflated — each gates a different
resource, each is revocable separately, and a caller must never infer one from another:

``consent_state``
    Azure Lighthouse delegation (FR-006, rewritten 2026-08-24 — previously a multi-tenant
    enterprise application). This is the basis of the *bootstrap* authority System holds in a
    customer subscription — nothing more, per FR-006's bootstrap-only scope. Revocation halts
    in-flight work (FR-031c).

``ado_org_access_state``
    Whether the customer's admin has added System's Entra identity as a member of their Azure
    DevOps organisation (FR-038b). Independent of ``consent_state`` — Lighthouse delegation has no
    jurisdiction over Azure DevOps organisation membership, a separate Entra-governed control
    plane. Gates ``devops_project`` stage execution specifically.

``offshore_inference_consent``
    Consent to transient in-call audio inference leaving the Australian geography (FR-053d),
    required because every Voice Live model in ``australiaeast`` is Global-standard-only
    (research notes § V-003, PD-005). Revocation disables voice but does **not** halt a running
    deployment (FR-031d) — a deployment does not depend on voice, and halting it would penalise a
    customer for exercising a privacy choice.

All three default to absent/pending. None is ever inferred from another, and none is ever inferred
from conversation content.

``bootstrap_identity_*`` fields are not a consent concept — they record the *outcome* of FR-006a's
one-time-per-tenant direct-ARM write (the customer-tenant managed identity + federated credential),
made once ``consent_state`` is ``GRANTED``. See ``api/tenants.py`` for where they are populated.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

StrictModel = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

_GUID = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# RFC 5322-simplified email pattern — rejects values like "abc" that are not email addresses.
# Applied at the contract boundary so invalid addresses fail before they reach ACS Email.
_EMAIL = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"

# An HTTPS URL with a real host — admits both https://dev.azure.com/<org> and the legacy
# <org>.visualstudio.com form, while rejecting anything a stage could not build a request from.
_HTTPS_URL = r"^https://[a-zA-Z0-9][a-zA-Z0-9.\-]*\.[a-zA-Z]{2,}(/[^\s]*)?$"

# FR-053a: transcripts retained 12 months, then deleted. FR-052a applies the same period to
# audit records and reports. Enforced by container TTL and blob lifecycle, not by a cleanup job.
CONVERSATION_RETENTION = timedelta(days=365)

AUSTRALIAN_REGIONS = frozenset({"australiaeast", "australiasoutheast"})


class ConsentState(StrEnum):
    """Azure Lighthouse delegation state (FR-006, rewritten 2026-08-24).

    ``PENDING`` and ``REVOKED`` both deny access. There is no partially-consented state, because
    a partial grant would make the authority question ambiguous at exactly the moment it matters.
    """

    PENDING = "pending"
    GRANTED = "granted"
    REVOKED = "revoked"

    @property
    def permits_tenant_operations(self) -> bool:
        return self is ConsentState.GRANTED


class AdoOrgAccessState(StrEnum):
    """Azure DevOps organisation membership state for System's own identity (FR-038b).

    Independent of :class:`ConsentState` — Lighthouse delegation and Azure DevOps organisation
    membership are two different, separately-revocable control planes. Same PENDING/GRANTED/REVOKED
    shape as ``ConsentState`` deliberately, not by coincidence: both are operator-attested "has the
    customer granted access" gates with identical semantics, just over different resources.
    """

    PENDING = "pending"
    GRANTED = "granted"
    REVOKED = "revoked"

    @property
    def permits_devops_project_execution(self) -> bool:
        return self is AdoOrgAccessState.GRANTED


class ConversationChannel(StrEnum):
    TEAMS = "teams"
    VOICE = "voice"
    API = "api"


class DataClassification(StrEnum):
    """FR-053 classifies conversation content as Confidential.

    Modelled explicitly so the storage layer can assert the handling controls rather than relying
    on a convention that a future change might not notice.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    REGULATED = "regulated"


class SubscriptionEntitlement(BaseModel):
    """A subscription the tenant has authorised, and what may be done in it."""

    model_config = StrictModel

    subscription_id: Annotated[str, Field(pattern=_GUID)]
    display_name: Annotated[str, Field(min_length=1)]
    may_deploy: bool = False
    """False means plan-only. Read-only planning (User Story 1) needs no write entitlement, which
    is what lets Story 1 be exercised against real tenants safely."""
    bootstrap_identity_resource_id: Annotated[str, Field(min_length=1)] | None = None
    """ARM resource ID of this **subscription's** FR-006a bootstrap user-assigned managed
    identity, once created. Scoped per-subscription, not per-tenant — every other naming
    convention this codebase uses for bootstrap-adjacent resources
    (``deployment_resource_group_name``, ``managed_identity_name`` in ``stages/infrastructure.py``
    and ``stages/identity.py``) is already subscription-scoped, and Lighthouse delegation itself
    (FR-006) binds to one subscription at a time — a single tenant-wide identity would disagree
    with both. ``None`` until the bootstrap write succeeds for this subscription."""
    bootstrap_identity_client_id: Annotated[str, Field(pattern=_GUID)] | None = None
    bootstrap_identity_created_at: datetime | None = None

    @model_validator(mode="after")
    def _bootstrap_identity_fields_are_consistent(self) -> Self:
        # Both-or-neither: a resource ID with no client ID (or vice versa) is a half-written
        # bootstrap record, not a legitimate state (FR-006a).
        has_resource_id = self.bootstrap_identity_resource_id is not None
        has_client_id = self.bootstrap_identity_client_id is not None
        if has_resource_id != has_client_id:
            raise ValueError(
                "bootstrap_identity_resource_id and bootstrap_identity_client_id must both be "
                "set or both be absent (FR-006a) — a half-recorded bootstrap identity is not "
                "valid state"
            )
        if has_resource_id and self.bootstrap_identity_created_at is None:
            raise ValueError(
                "bootstrap_identity_resource_id is set but bootstrap_identity_created_at is "
                "missing; the audit trail requires knowing when the bootstrap write happened "
                "(FR-047)"
            )
        return self


class OffshoreInferenceConsent(BaseModel):
    """Recorded consent to transient offshore audio inference (FR-053d).

    ``artefact_uri`` is required: consent that leaves no durable artefact is not recorded consent,
    and FR-053d requires a durable one. ``disclosure_version`` matters because consent is only
    meaningful against the specific disclosure the customer was shown — if the disclosure text
    changes materially, prior consent no longer covers it.
    """

    model_config = StrictModel

    consenting_identity_object_id: Annotated[str, Field(pattern=_GUID)]
    consenting_identity_display_name: Annotated[str, Field(min_length=1)]
    consented_at: datetime
    artefact_uri: Annotated[str, Field(min_length=1)]
    disclosure_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]


class CustomerTenant(BaseModel):
    """A customer's Entra tenant and the authority granted within it."""

    model_config = StrictModel

    tenant_id: Annotated[str, Field(pattern=_GUID)]
    display_name: Annotated[str, Field(min_length=1)]
    consent_state: ConsentState = ConsentState.PENDING
    consent_granted_at: datetime | None = None
    consent_confirmed_by_object_id: Annotated[str, Field(pattern=_GUID)] | None = None
    """The Groundwork operator (FR-006's real, current verification mechanism: an authenticated
    ``CallerRole.OPERATOR`` identity attests that the customer's admin actually granted consent to
    the multi-tenant application, confirmed out-of-band — see
    ``api/tenants.py``'s onboarding-confirm route docstring for why this, and not an automated
    browser-redirect callback or a standing client-credential, is the real mechanism today).
    ``None`` for a tenant never onboarded through that route, or still ``PENDING``."""
    consent_confirmed_by_display_name: Annotated[str, Field(min_length=1)] | None = None
    consent_confirmation_note: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    """The operator's stated basis for the attestation above (e.g. "confirmed by customer admin
    Jane Doe via email 2026-08-06"). An attestation identity with no recorded basis is not a
    meaningfully auditable one — see ``api/tenants.py``'s ``ConfirmConsentRequest`` docstring."""
    ado_org_access_state: AdoOrgAccessState = AdoOrgAccessState.PENDING
    """FR-038b. Independent of ``consent_state`` — see module docstring."""
    ado_org_access_granted_at: datetime | None = None
    ado_org_access_confirmed_by_object_id: Annotated[str, Field(pattern=_GUID)] | None = None
    ado_org_access_confirmed_by_display_name: Annotated[str, Field(min_length=1)] | None = None
    ado_org_access_confirmation_note: Annotated[str, Field(min_length=1, max_length=500)] | None = (
        None
    )
    """Same attestation shape as ``consent_confirmed_by_*``/``consent_confirmation_note`` — see
    ``api/tenants.py``'s ``ConfirmAdoOrgAccessRequest`` docstring."""
    subscriptions: tuple[SubscriptionEntitlement, ...] = ()
    """FR-006a's bootstrap identity fields live on each :class:`SubscriptionEntitlement`, not
    here — bootstrap is scoped per-subscription, matching Lighthouse delegation's own scope and
    every subscription-keyed naming convention already in ``stages/infrastructure.py``/
    ``stages/identity.py``."""
    approved_regions: Annotated[frozenset[str], Field(min_length=1)]
    data_residency_regions: Annotated[frozenset[str], Field(min_length=1)]
    concurrency_cap: Annotated[int, Field(ge=1, le=50)] = 3
    readiness_contract_version: str | None = None
    voice_channel_enabled: bool = False
    offshore_inference_consent: OffshoreInferenceConsent | None = None
    notification_email: Annotated[str, Field(pattern=_EMAIL)] | None = None
    """Email address gathered from the customer during conversation and explicitly reconfirmed
    (FR-002 / FR-004b). Used for deployment-outcome notifications (FR-051) and for sending the
    multi-tenant application authorisation link. Absent until set by the conversation agent —
    notification is skipped when ``None``, not silently dropped to a default sender."""
    contact_display_name: Annotated[str, Field(min_length=1)] | None = None
    """Display name of the notification recipient, gathered alongside ``notification_email``."""
    devops_organization_url: Annotated[str, Field(pattern=_HTTPS_URL)] | None = None
    """The customer's own Azure DevOps organization URL (e.g. ``https://dev.azure.com/<org>``) —
    engagement data gathered from the customer (conversation or onboarding), never
    Groundwork-side configuration, because the ``devops_project`` and ``identity`` stages create
    the project *in the customer's organization* (FR-038). When absent, execution-time stages
    fall back to the worker-wide ``GROUNDWORK_DEVOPS_ORGANIZATION_URL`` setting (the documented
    single-engagement simplification); a tenant with neither is declined at execution time, not
    guessed for."""
    fabric_capacity_admin_upn: Annotated[str, Field(pattern=_EMAIL)] | None = None
    """A UPN-format Entra user in the *customer's own* tenant to name as Fabric capacity
    administrator — Microsoft requires at least one user (not service principal) on every
    capacity, and capacity is billable to the customer (see ``stages/fabric.py``). Gathered from
    the customer like ``notification_email``; falls back to the worker-wide
    ``GROUNDWORK_FABRIC_CAPACITY_ADMIN_UPN`` setting when absent. Same shape as an email address
    (``user@domain``), so it reuses ``_EMAIL``."""

    @model_validator(mode="after")
    def _granted_consent_has_timestamp(self) -> Self:
        if self.consent_state is ConsentState.GRANTED and self.consent_granted_at is None:
            raise ValueError(
                "consent_state is 'granted' but consent_granted_at is missing; the audit trail "
                "requires knowing when authority was granted (FR-047)"
            )
        return self

    @model_validator(mode="after")
    def _granted_ado_org_access_has_timestamp(self) -> Self:
        if (
            self.ado_org_access_state is AdoOrgAccessState.GRANTED
            and self.ado_org_access_granted_at is None
        ):
            raise ValueError(
                "ado_org_access_state is 'granted' but ado_org_access_granted_at is missing; the "
                "audit trail requires knowing when access was granted (FR-047)"
            )
        return self

    @model_validator(mode="after")
    def _residency_is_australian(self) -> Self:
        # FR-053b: persisted conversation content stays in Australian regions. A tenant configured
        # otherwise cannot be served compliantly, so reject at construction rather than discovering
        # it when the first transcript is written.
        outside = self.data_residency_regions - AUSTRALIAN_REGIONS
        if outside:
            raise ValueError(
                f"data_residency_regions contains non-Australian region(s) {sorted(outside)}; "
                f"FR-053b requires persisted conversation content to remain in Australian regions"
            )
        return self

    @model_validator(mode="after")
    def _voice_requires_consent(self) -> Self:
        # FR-053e: voice may only be enabled where offshore-inference consent is recorded.
        # Enforced in the type so no code path can enable voice without it.
        if self.voice_channel_enabled and self.offshore_inference_consent is None:
            raise ValueError(
                "voice_channel_enabled is True without offshore_inference_consent; FR-053d "
                "requires explicit recorded per-tenant consent before voice is enabled"
            )
        return self

    def entitlement_for(self, subscription_id: str) -> SubscriptionEntitlement | None:
        """Look up an entitlement, or None if the tenant has not authorised that subscription.

        Callers must treat None as "not entitled" (FR-008). There is deliberately no
        ``default=`` parameter that could be used to fabricate an entitlement.

        Case-insensitive: GUIDs are case-insensitive per RFC 4122, and Azure itself accepts a
        subscription id in any casing. Found live 2026-08-24 — a voice-extracted subscription id
        can render in different casing than what was recorded, and an exact string comparison
        rejected the genuinely-entitled subscription for no real reason.
        """
        target = subscription_id.lower()
        for entitlement in self.subscriptions:
            if entitlement.subscription_id.lower() == target:
                return entitlement
        return None

    def may_accept_voice_call(self) -> bool:
        """Whether an inbound call may be accepted for this tenant (FR-053e, SC-020a)."""
        return (
            self.consent_state.permits_tenant_operations
            and self.voice_channel_enabled
            and self.offshore_inference_consent is not None
        )


class ConversationTurn(BaseModel):
    """One turn of a conversation.

    Content is Confidential (FR-053). No audio is ever stored here — only the fact that it was
    discarded — because FR-053a requires raw audio to be discarded at transcription.
    """

    model_config = StrictModel

    sequence: Annotated[int, Field(ge=0)]
    speaker: Annotated[str, Field(min_length=1)]
    text: str
    occurred_at: datetime
    recognition_confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = None


class ConversationRecord(BaseModel):
    """Retained conversation content, TTL-enforced.

    ``audio_retained`` exists to be asserted False, not to permit True. FR-053a requires raw audio
    to be discarded once transcription completes, so a record claiming retained audio is a
    compliance defect that must alert.
    """

    model_config = StrictModel

    conversation_id: Annotated[str, Field(pattern=_GUID)]
    tenant_id: Annotated[str, Field(pattern=_GUID)]
    correlation_id: Annotated[str, Field(pattern=_GUID)]
    channel: ConversationChannel
    locale: Annotated[str, Field(pattern=r"^en-AU$")] = "en-AU"
    """FR-004a: Australian English only this release. Pattern-constrained rather than free text so
    a fallback locale cannot be introduced silently (FR-004b)."""
    classification: DataClassification = DataClassification.CONFIDENTIAL
    transcript: tuple[ConversationTurn, ...] = ()
    audio_retained: bool = False
    storage_region: Annotated[str, Field(min_length=1)]
    retention_expires_at: datetime
    injection_attempt_detected: bool = False
    created_at: datetime

    @model_validator(mode="after")
    def _audio_never_retained(self) -> Self:
        if self.audio_retained:
            raise ValueError(
                "audio_retained is True; FR-053a requires raw audio to be discarded once "
                "transcription completes. Retained audio is a compliance defect, not a state."
            )
        return self

    @model_validator(mode="after")
    def _storage_region_is_australian(self) -> Self:
        if self.storage_region not in AUSTRALIAN_REGIONS:
            raise ValueError(
                f"storage_region {self.storage_region!r} is not Australian; FR-053b requires "
                f"persisted conversation content to remain in Australian regions"
            )
        return self

    @model_validator(mode="after")
    def _classification_is_confidential(self) -> Self:
        # FR-053 fixes the classification. Allowing it to be lowered here would let a caller
        # downgrade handling controls for the most sensitive content in the system.
        if self.classification is not DataClassification.CONFIDENTIAL:
            raise ValueError(
                f"conversation content is classified Confidential by FR-053; "
                f"{self.classification.value!r} is not permitted"
            )
        return self

    @model_validator(mode="after")
    def _retention_within_policy(self) -> Self:
        limit = self.created_at + CONVERSATION_RETENTION
        # A later expiry than policy allows would silently extend retention of Confidential
        # content beyond what the customer was told (FR-053a).
        if self.retention_expires_at > limit:
            raise ValueError(
                f"retention_expires_at exceeds the {CONVERSATION_RETENTION.days}-day period "
                f"set by FR-053a"
            )
        return self

    @model_validator(mode="after")
    def _transcript_sequence_is_ordered(self) -> Self:
        sequences = [t.sequence for t in self.transcript]
        if sequences != sorted(sequences):
            raise ValueError("transcript turns are not in sequence order")
        if len(set(sequences)) != len(sequences):
            raise ValueError("transcript contains duplicate turn sequence numbers")
        return self

    def low_confidence_turns(self, threshold: float = 0.8) -> tuple[ConversationTurn, ...]:
        """Turns recognised below ``threshold``.

        FR-004b requires re-confirmation rather than proceeding on a low-confidence
        transcription of a material parameter.
        """
        return tuple(
            turn
            for turn in self.transcript
            if turn.recognition_confidence is not None and turn.recognition_confidence < threshold
        )
