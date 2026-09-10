"""Blueprint manifest loading.

FR-013b requires a blueprint to be addable as a versioned declarative artefact without code change
to planning, validation, or orchestration. This module is what makes that true: it reads YAML and
returns a validated :class:`PlatformBlueprint`, so adding a blueprint is adding a file.

This lives in ``groundwork_shared``, not ``groundwork_contracts``, because it performs filesystem
I/O. The contracts package is asserted to have *zero* I/O so the deterministic-execution boundary
can be tested with no cloud dependency and cannot acquire one — relaxing that to "no I/O except
this one read"
would make the guarantee a matter of judgement rather than a fact a test can check.

``yaml.safe_load`` is used, never ``yaml.load``. A blueprint is a trusted repository artefact today,
but a loader that can construct arbitrary Python objects is the kind of thing that becomes an
injection point the moment someone makes blueprints customer-supplied.

**Digest verification (threat-model.md T-009).** Blueprints are pinned-version, mirrored artefacts
(never pulled live from ``br/public``) specifically so a compromised upstream package registry can't
substitute a malicious module — that mitigation was a build-time contract with no matching runtime
check: nothing verified the mirrored file on disk still matched what was actually reviewed. Each
``blueprint.yaml`` now needs a sibling ``blueprint.yaml.sha256`` (its lowercase hex SHA-256, plain
text, no newline handling required — ``compute_digest`` is exactly what a manifest author runs to
generate one) committed alongside it. Required, not optional: an absent sidecar fails closed
(``BlueprintLoadError``), the same discipline this codebase already applies everywhere else a check
having no way to run for real is treated as a gap, not a pass — an attacker able to modify the
manifest could otherwise simply delete the sidecar to skip verification entirely.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from groundwork_contracts.blueprint import PlatformBlueprint
from groundwork_contracts.errors import ContractViolation


class BlueprintLoadError(ContractViolation):
    """A blueprint manifest is missing, unparseable, or invalid.

    Raised at startup. A malformed blueprint must stop the service from starting rather than fail a
    customer's deployment partway through — the FR-013a catalogue is loaded once and trusted
    thereafter.
    """


def compute_digest(content: str) -> str:
    """The lowercase hex SHA-256 a ``blueprint.yaml.sha256`` sidecar must contain.

    Hashes the raw file bytes (UTF-8), not the parsed/normalised structure — the point is
    detecting *any* on-disk change to the reviewed artefact, including whitespace or comment
    edits a structural hash would ignore.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def to_snake_case(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert manifest camelCase keys to the model's snake_case field names.

    Manifests are authored in camelCase because that is the convention operators expect in Azure
    YAML. The models use snake_case because that is the Python convention. Mapping here, once, is
    better than aliasing every field or asking authors to write Python style in YAML.

    Public because every declarative manifest loader in this package needs it —
    ``groundwork_shared.config.readiness`` reuses it for ``assertions.yaml`` rather than
    duplicating the conversion.
    """

    def convert_key(key: str) -> str:
        out: list[str] = []
        for index, char in enumerate(key):
            if char.isupper() and index > 0:
                out.append("_")
            out.append(char.lower())
        return "".join(out)

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {convert_key(str(k)): walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    result = walk(payload)
    if not isinstance(result, dict):
        # Not an assert: asserts are stripped under `python -O`, and this is a real invariant
        # guarding a startup-time parse, not a debug aid.
        raise TypeError(f"expected a mapping after key conversion, got {type(result).__name__}")
    return result


def load_blueprint(manifest_path: Path) -> PlatformBlueprint:
    """Load and validate a single blueprint manifest.

    Args:
        manifest_path: Path to a ``blueprint.yaml``.

    Returns:
        The validated blueprint.

    Raises:
        BlueprintLoadError: If the file is missing, is not valid YAML, is not a mapping, fails
            contract validation, has no ``.sha256`` digest sidecar, or does not match it. The
            message names the manifest so a startup failure is diagnosable without a stack trace.
    """
    if not manifest_path.is_file():
        raise BlueprintLoadError(f"blueprint manifest not found: {manifest_path}")

    content = manifest_path.read_text(encoding="utf-8")

    digest_path = manifest_path.with_name(manifest_path.name + ".sha256")
    if not digest_path.is_file():
        raise BlueprintLoadError(
            f"blueprint manifest {manifest_path} has no {digest_path.name} digest sidecar "
            f"(threat-model.md T-009); generate one with "
            f"groundwork_shared.config.blueprints.compute_digest and commit it alongside the "
            f"manifest"
        )
    expected_digest = digest_path.read_text(encoding="utf-8").strip().lower()
    actual_digest = compute_digest(content)
    if actual_digest != expected_digest:
        raise BlueprintLoadError(
            f"blueprint manifest {manifest_path} does not match its {digest_path.name} digest "
            f"(expected {expected_digest}, got {actual_digest}); the mirrored file has changed "
            f"since it was last reviewed"
        )

    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise BlueprintLoadError(
            f"blueprint manifest {manifest_path} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise BlueprintLoadError(
            f"blueprint manifest {manifest_path} must be a mapping, got {type(raw).__name__}"
        )

    try:
        return PlatformBlueprint.model_validate(to_snake_case(raw))
    except ValueError as exc:
        raise BlueprintLoadError(
            f"blueprint manifest {manifest_path} failed contract validation: {exc}"
        ) from exc


def load_catalogue(blueprints_root: Path) -> dict[str, PlatformBlueprint]:
    """Load every blueprint under ``blueprints_root``.

    Returns:
        Blueprints keyed by ``blueprint_id``.

    Raises:
        BlueprintLoadError: If the directory is missing, contains no manifests, or contains two
            blueprints claiming the same id. An empty catalogue is an error rather than an empty
            dict: a running service with no blueprints can accept requests it can never fulfil.
    """
    if not blueprints_root.is_dir():
        raise BlueprintLoadError(f"blueprint directory not found: {blueprints_root}")

    catalogue: dict[str, PlatformBlueprint] = {}
    for manifest in sorted(blueprints_root.glob("*/blueprint.yaml")):
        blueprint = load_blueprint(manifest)
        if blueprint.blueprint_id in catalogue:
            raise BlueprintLoadError(
                f"duplicate blueprint id {blueprint.blueprint_id!r} at {manifest}; "
                f"plans reference blueprints by id, so duplicates make selection ambiguous"
            )
        catalogue[blueprint.blueprint_id] = blueprint

    if not catalogue:
        raise BlueprintLoadError(
            f"no blueprint manifests found under {blueprints_root}; the service cannot serve "
            f"plan requests it has no approved topology for"
        )

    return catalogue
