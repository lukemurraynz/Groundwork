"""Every manifest dependency has at least one real import somewhere in the tree (taxonomy 26).

Catches a specific class of drift: a dependency declared in `pyproject.toml`, described in a
module docstring as load-bearing, but never actually imported anywhere. ADR-0013/ADR-0014 record
a real instance of this (`agent-framework-core`/`agent-framework-foundry`), now resolved — both
packages are genuinely used again, so `KNOWN_UNUSED_DEPENDENCIES` is empty.

If this test starts failing because a *new* dependency has zero imports, that is almost certainly
the same bug recurring: either dead weight to remove, or a real usage site that got refactored
away without removing the import — go check, don't just add the name to the exception list. Only
add to the exception list when there's an ADR recording why the dependency is deliberately unused
right now.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

REPO_ROOT = Path(__file__).resolve().parents[2]

# Empty — see the module docstring above. Kept as a named, typed constant (rather than removed
# outright) so the next genuinely-unused dependency has an obvious place to be recorded, with an
# ADR reference, instead of a bare literal creeping into the checks below.
KNOWN_UNUSED_DEPENDENCIES: frozenset[str] = frozenset()

# Near-universal dev/lint/test/server tooling invoked as a CLI or discovered as a plugin, never
# imported by name in application source. Matched by exact name or dash-prefix (so
# "pytest-asyncio"/"pytest-cov" match "pytest" without listing every plugin).
DEV_TOOL_NAMES = ("pytest", "ruff", "mypy", "uvicorn", "types-")

# Famous PyPI-distribution-name -> import-name mismatches the truncation heuristic below can't
# guess. Not exhaustive — just the ones this project actually declares.
KNOWN_IMPORT_ALIASES: dict[str, list[str]] = {
    "pyyaml": ["yaml"],
    "pyjwt": ["jwt"],
}


def _parse_dependencies() -> list[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    specs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        specs.extend(group)
    names = []
    for spec in specs:
        name = re.split(r"[<>=!~;\[\s]", spec, maxsplit=1)[0].strip()
        if name:
            names.append(name)
    return names


def _candidate_import_names(dist_name: str) -> list[str]:
    alias = KNOWN_IMPORT_ALIASES.get(dist_name.lower())
    if alias:
        return alias
    segments = dist_name.replace("_", "-").split("-")
    return ["_".join(segments[:n]) for n in range(len(segments), 0, -1)]


def _is_dev_tool(dist_name: str) -> bool:
    lower = dist_name.lower()
    return any(lower == tool or lower.startswith(tool) for tool in DEV_TOOL_NAMES)


def _scanned_source() -> str:
    texts = []
    for directory in ("src", "tests"):
        for path in (REPO_ROOT / directory).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(texts)


def test_every_dependency_has_a_real_import_or_a_recorded_reason() -> None:
    haystack = _scanned_source()
    unexpectedly_unused = []

    for dist_name in _parse_dependencies():
        if _is_dev_tool(dist_name) or dist_name in KNOWN_UNUSED_DEPENDENCIES:
            continue
        candidates = _candidate_import_names(dist_name)
        pattern = re.compile(
            r"^\s*(?:import|from)\s+(?:" + "|".join(re.escape(c) for c in candidates) + r")\b",
            re.M,
        )
        if not pattern.search(haystack):
            unexpectedly_unused.append(f"{dist_name} (tried: {', '.join(candidates)})")

    assert not unexpectedly_unused, (
        "declared dependencies with no import anywhere in src/ or tests/, and no entry in "
        "KNOWN_UNUSED_DEPENDENCIES: "
        f"{unexpectedly_unused}. Either remove the dependency, find the missing import, or "
        "record why it's deliberately unused (with an ADR) and add it to the exception list."
    )


def test_known_unused_dependencies_are_still_actually_unused() -> None:
    """Guards the exception list itself: if agent-framework-core/foundry ever gets a real import
    (the native client was restored per ADR-0013), this test fails and tells you to shrink the
    exception list — an exception nobody needs anymore is a stale allowlist, not a safety net."""
    haystack = _scanned_source()
    still_unused = []

    for dist_name in sorted(KNOWN_UNUSED_DEPENDENCIES):
        candidates = _candidate_import_names(dist_name)
        pattern = re.compile(
            r"^\s*(?:import|from)\s+(?:" + "|".join(re.escape(c) for c in candidates) + r")\b",
            re.M,
        )
        still_unused.append((dist_name, not pattern.search(haystack)))

    now_used = [name for name, unused in still_unused if not unused]
    assert not now_used, (
        f"{now_used} now has a real import but is still listed in KNOWN_UNUSED_DEPENDENCIES — "
        "remove it from the exception list (and update ADR-0013 if this means the native "
        "Agent Framework client was restored)."
    )
