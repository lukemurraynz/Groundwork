# ADR-0006: FR-040 diagnostic settings on Fabric capacity accepted as impossible

**Date**: 2026-08-26
**Status**: Accepted (2026-08-26)

## Context

FR-040 requires the system to apply diagnostic settings to every resource it creates. The Fabric
capacity (`Microsoft.Fabric/capacities`) is created by the `fabric` stage via the customer's ADO
pipeline. Applying Azure Monitor diagnostic settings to it was a stated requirement.

Live ARM probing on 2026-08-26 proved this cannot be done via the standard diagnostic-settings
API. ARM rejects the request outright:

```
ResourceTypeNotSupported: The resource type 'microsoft.fabric/capacities'
does not support diagnostic settings
```

This is not a permission gap or a configuration error. The resource type simply does not support
the `Microsoft.Insights/diagnosticSettings` extension resource. `stages/monitoring.py`'s module
docstring records the exact error text and its source.

Microsoft Fabric exposes capacity telemetry through its own monitoring surface, not Azure Monitor
diagnostic settings.

## Decision

FR-040's diagnostics-on-capacity requirement cannot be satisfied via the Azure Monitor
diagnostic-settings API for `Microsoft.Fabric/capacities`. Release 1 ships without it.

Any future revision that wants capacity telemetry must design against Fabric's native monitoring
at spec level, before implementation begins. This is a spec-level design decision, not a code
change in `monitoring.py`.

The `monitoring` stage continues to verify that the infrastructure pipeline run converged. It
does not attempt to apply diagnostic settings to the Fabric capacity, and adding that attempt
would fail at runtime regardless.

## Consequences

**Positive**

- No phantom diagnostic-settings resource is deployed that ARM would silently reject or that
  would make `azd provision --preview` misleading.

**Negative / watch points**

- Capacity utilisation, throttling, and autoscale events are not visible in the platform's own
  Log Analytics workspace for R1. Operators wanting Fabric capacity telemetry must use the Fabric
  Admin portal or the Fabric Capacity Metrics app directly.
- A future blueprint revision that adds capacity monitoring must specify the exact Fabric-native
  mechanism (Capacity Metrics app, Fabric Admin API, Activator-based alerting) before any
  implementation work begins. Do not attempt to reuse the `Microsoft.Insights/diagnosticSettings`
  path: the ARM rejection is permanent for this resource type.

**Sources**: the project's research notes, FR-040 block (2026-08-26); `stages/monitoring.py` module docstring
(`ResourceTypeNotSupported` evidence)
