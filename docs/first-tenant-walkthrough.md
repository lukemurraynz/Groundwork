# Getting your first tenant working

`azd up` deploys Groundwork. It doesn't create a customer engagement for you: that's a separate,
deliberate step, because the product's whole premise is that nothing writes into a tenant without
an explicit, attributable action. This walkthrough is that step, for your own tenant, so the voice
and chat surfaces have something to actually check against.

Nothing here is optional cosmetic setup. Skip any step and `VoiceEnablementGate` will refuse every
call with `voice not enabled for this tenant`, naming exactly which of the three conditions failed.

## What you need first

`postprovision` already granted the identity you deployed with (`AZURE_PRINCIPAL_ID`) all three
app roles (Operator, Approver, Requester) on the Entra application it created. You don't need to
assign yourself anything. You do need a bearer token for that application, and the Azure CLI's own
token cache can't give you one: `az account get-access-token --resource api://<client-id>` fails
with `AADSTS65001: consent_required`, because that command tries to reuse the **Azure CLI's own**
app registration, which nobody has consented to for this API's scope. Get a properly-scoped token
instead:

```bash
az login --scope "$(azd env get-value GROUNDWORK_ENTRA_APP_AUDIENCE)/.default"
```

This opens an interactive sign-in and, the first time, a consent prompt for Groundwork's control
plane. Accept it: you're the tenant admin in your own dev tenant, so this is a one-person
decision, not an org-wide approval. After that:

```bash
TOKEN=$(az account get-access-token --resource "$(azd env get-value GROUNDWORK_ENTRA_APP_AUDIENCE)" --query accessToken -o tsv)
HOST="$(azd env get-value GROUNDWORK_PUBLIC_URL)"
```

## The sequence

Four calls, in this order. The order isn't arbitrary: `CustomerTenant`'s own validator rejects
`voiceChannelEnabled: true` on a tenant with no recorded consent yet, so voice-channel enablement
has to come after consent, not before.

**Faster alternative**: if you're onboarding your own dev tenant, there's a single-call shortcut
that does all four steps at once. `POST /v1/tenants/onboarding/quick-onboard` creates the tenant,
confirms consent, records offshore-inference consent, and (optionally) enables voice, in one
request. It requires your token's `tid` to match the `tenantId` you're onboarding. See
[Quick-onboard your own tenant](#quick-onboard-your-own-tenant) below for the exact request.

**1. Create the tenant.** `tenantId` is your own Entra tenant ID (`az account show --query
tenantId -o tsv`): this is the tenant the voice/chat gate will actually check against when you
sign in.

```bash
curl -sS -X POST "$HOST/v1/tenants" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "tenantId": "<your-tenant-id>",
    "displayName": "My test engagement",
    "approvedRegions": ["australiaeast"],
    "dataResidencyRegions": ["australiaeast"]
  }'
```

**2. Confirm consent.** In a real engagement this attests that you actually checked the customer's
Entra admin center after they completed the admin-consent redirect (`GET
/v1/tenants/{tenantId}/onboarding/consent-url` gives you that URL). For your own tenant, you *are*
the admin: the note just needs to say something true.

Before confirming, run the read-only verification probe so your attestation is evidence-backed
rather than blind. It checks whether Groundwork's principal actually appears in the subscription's
Lighthouse delegation — no new secret, just the control plane's existing read-only credential:

```bash
curl -sS -X POST "$HOST/v1/tenants/<your-tenant-id>/onboarding/verify-consent" \
  -H "Authorization: Bearer $TOKEN"
```

If the response says `"consentCanBeConfirmed": true`, the customer's admin completed the
admin-consent flow and you can confirm with confidence. If `"delegationState": "pending"`, they
haven't finished it yet — re-run the probe after they approve.

```bash
curl -sS -X POST "$HOST/v1/tenants/<your-tenant-id>/onboarding/confirm" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"note": "own dev tenant, self-confirmed"}'
```

**3. Record offshore-inference consent.** This is the FR-053d disclosure: transient in-call audio
may leave your data-residency region for inference, and this call is what records that you agreed
to it. It writes an immutable artefact to blob storage *and* attaches the result to your tenant
record: those two used to be disconnected. The disclosure text is served verbatim by
`GET /v1/tenants/onboarding/offshore-inference-disclosure` and, on the voice channel, read aloud by
the agent before consent is recorded — the transcript then evidences what was shown. See `docs/adr/` if you're
wondering why this call does both.

```bash
curl -sS -X POST "$HOST/v1/tenants/offshore-inference-consent" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{}'
```

**4. Enable the voice channel.** The one step here that's a genuine product decision, not an
attestation of something that already happened: this is where an operator decides voice is part
of this engagement.

```bash
curl -sS -X POST "$HOST/v1/tenants/<your-tenant-id>/voice-channel" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled": true, "note": "voice included in this engagement"}'
```

## Check it worked

```bash
curl -sS "$HOST/v1/tenants/<your-tenant-id>" -H "Authorization: Bearer $TOKEN"
```

`consentState` should read `granted`, `voiceChannelEnabled` should read `true`, and
`offshoreInferenceConsent` should be a populated object, not `null`. Now open
`$HOST/static/voice.html`, sign in, and voice should connect instead of closing the socket with
`voice not enabled for this tenant`.

## Before you approve a deployment

Approval refuses (409, `notification-email-missing`) until the tenant has a recorded notification
recipient, so the deployment-outcome email can never be silently skipped. The conversational flow
captures this during planning; for REST/CLI onboarding where it never got captured, record it once:

```bash
curl -sS -X POST "$HOST/v1/tenants/<your-tenant-id>/notification-email" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "email": "customer@example.com",
    "displayName": "Jane Customer",
    "note": "reconfirmed by customer via Teams 2026-09-11"
  }'
```

Re-recording the address is idempotent re-confirmation, not a conflict.

If you get as far as actually approving a deployment plan and hit a `step-up-authentication-required`
403, that's a separate, later gate (`GROUNDWORK_REQUIRE_STEP_UP_APPROVAL`, on by default; see
[ADR-0011](adr/0011-voice-alone-authorises-irreversible-actions.md)), not this onboarding sequence.
It wants your token to show MFA or to have been issued in the last 10 minutes; sign in again or
turn it off for local testing with `azd env set GROUNDWORK_REQUIRE_STEP_UP_APPROVAL false`.

## Quick-onboard your own tenant

The four-call sequence above is the honest, step-by-step path for a real customer engagement where
each attestation is a separate decision. When you're onboarding your own dev tenant for testing and
you *are* the admin doing every step, the round-trips add nothing. One call replaces them:

```bash
curl -sS -X POST "$HOST/v1/tenants/onboarding/quick-onboard" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "tenantId": "<your-tenant-id>",
    "displayName": "My test engagement",
    "approvedRegions": ["australiaeast"],
    "dataResidencyRegions": ["australiaeast"],
    "consentNote": "own dev tenant, self-confirmed",
    "voiceEnabled": true,
    "voiceNote": "voice included in this engagement"
  }'
```

What it does in one call:

1. Creates the tenant record (`consent_state` starts `PENDING`, same as the multi-call path)
2. Confirms consent and flips it to `GRANTED`, recording your identity as the confirming operator
   (the same audit fields the `confirm` route sets)
3. Records offshore-inference consent against the current disclosure version
4. Enables voice if `voiceEnabled` is true

The response is the tenant's onboarding status, so you can see exactly what completed and what
remains. Your token's `tid` must match the `tenantId` you send: this endpoint exists for the
operator's own tenant, not for onboarding a customer tenant, which still goes through the
four-call sequence so each attestation stays a separate, reviewable step.

If you'd rather see the current state without changing anything, `GET
/v1/tenants/<your-tenant-id>/onboarding/status` returns each step's completion state and a
`nextAction` field telling you exactly which endpoint to call next.
