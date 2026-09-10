"""T005 — the deterministic-execution boundary (ADR-0001), enforced mechanically.

"The model never touches execution" is easy to state and easy to erode. One import added under
deadline pressure, in a file nobody re-reads, and the orchestrator can call a model. Code review
catches that only if the reviewer happens to look at the import block.

So this asserts it by parsing the AST of every source file. It works with the current small tree and
keeps working as modules are added, which is the point — the guarantee should get stronger as the
codebase grows, not weaker.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

# Anything that can reach a model. The orchestrator must not import these.
MODEL_LIBRARIES = frozenset(
    {
        "openai",
        "anthropic",
        "agent_framework",
        "semantic_kernel",
        "azure.ai.inference",
        "azure.ai.projects",
        "azure.ai.agents",
        "azure.ai.voicelive",
        "langchain",
        "llama_index",
    }
)

# Anything that performs I/O. The contracts package must not import these — it has to stay
# testable with no cloud dependency, and unable to acquire one.
#
# Filesystem modules are included deliberately. The blueprint loader originally lived here and read
# a YAML manifest; it was moved to groundwork_shared.config.blueprints rather than relaxing this to
# "no I/O except one read". A guarantee with an exception is a matter of judgement; this one is a
# fact a test can check.
IO_LIBRARIES = frozenset(
    {
        "azure",
        "httpx",
        "requests",
        "aiohttp",
        "boto3",
        "socket",
        "urllib",
        "sqlite3",
        "psycopg",
        "fastapi",
        "uvicorn",
        "pathlib",
        "os",
        "shutil",
        "tempfile",
        "yaml",
        "subprocess",
    }
)


def _iter_python_files(package: str) -> list[Path]:
    root = SRC / package
    if not root.exists():
        return []
    return sorted(root.rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    """Top-level and dotted module names imported by a file.

    Uses the AST rather than a regex so a name inside a string or comment is not a false positive,
    and a multi-line or aliased import is not a false negative.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _violations(imported: set[str], banned: frozenset[str]) -> set[str]:
    """Banned imports, matching a module or any of its submodules.

    ``azure`` matches ``azure.cosmos``; ``azure_something_else`` is not a match.
    """
    hits: set[str] = set()
    for module in imported:
        for prefix in banned:
            if module == prefix or module.startswith(prefix + "."):
                hits.add(module)
    return hits


def test_contracts_package_has_no_io_dependency() -> None:
    """The deterministic-execution boundary must be testable with no cloud dependency."""
    files = _iter_python_files("groundwork_contracts")
    assert files, "expected groundwork_contracts to contain Python files"

    offenders: dict[str, set[str]] = {}
    for path in files:
        found = _violations(_imported_modules(path), IO_LIBRARIES)
        if found:
            offenders[str(path.relative_to(SRC))] = found

    assert not offenders, (
        f"groundwork_contracts must not import I/O libraries — it is the schema boundary and has "
        f"to stay testable without cloud access. Offenders: {offenders}"
    )


def test_contracts_package_has_no_model_dependency() -> None:
    files = _iter_python_files("groundwork_contracts")
    assert files

    offenders: dict[str, set[str]] = {}
    for path in files:
        found = _violations(_imported_modules(path), MODEL_LIBRARIES)
        if found:
            offenders[str(path.relative_to(SRC))] = found

    assert not offenders, f"groundwork_contracts must not import model libraries: {offenders}"


def test_orchestrator_never_imports_a_model_library() -> None:
    """The load-bearing assertion of the deterministic-execution boundary.

    The orchestrator executes only pre-authored, version-pinned automation (FR-026). If it can
    import a model client, it can be made to execute model output — and every downstream guarantee
    about determinism and auditability weakens at once.

    Passes trivially while the package is empty. That is fine: it is a ratchet, and it tightens
    automatically as stages are implemented.
    """
    files = _iter_python_files("groundwork_orchestrator")

    offenders: dict[str, set[str]] = {}
    for path in files:
        found = _violations(_imported_modules(path), MODEL_LIBRARIES)
        if found:
            offenders[str(path.relative_to(SRC))] = found

    assert not offenders, (
        f"groundwork_orchestrator must not import any model library (ADR-0001, "
        f"FR-026). Model calls belong in groundwork_controlplane. Offenders: {offenders}"
    )


def test_orchestrator_does_not_import_the_control_plane() -> None:
    """Dependency direction, so the boundary cannot be crossed transitively.

    Without this, the orchestrator could import a control-plane module that itself imports an agent,
    and reach a model indirectly while passing the direct-import check above.
    """
    files = _iter_python_files("groundwork_orchestrator")

    offenders: dict[str, set[str]] = {}
    for path in files:
        found = _violations(_imported_modules(path), frozenset({"groundwork_controlplane"}))
        if found:
            offenders[str(path.relative_to(SRC))] = found

    assert not offenders, (
        f"groundwork_orchestrator must not import groundwork_controlplane; a transitive path to a "
        f"model client would defeat the direct-import check. Offenders: {offenders}"
    )


def test_shared_package_does_not_import_the_control_plane() -> None:
    """groundwork_shared is the dependency sink both services import from.

    If shared ever imports controlplane, then orchestrator -> shared -> controlplane becomes a
    legal-looking transitive path that the direct-import check above cannot see — and the
    orchestrator image (which installs only the orchestrator extra) starts ImportError-ing on
    modules the local all-extras venv silently provides. Found live 2026-08-26 when the relocated
    readiness registry reached back into controlplane checks and crashed the production rollout.
    """
    files = _iter_python_files("groundwork_shared")

    offenders: dict[str, set[str]] = {}
    for path in files:
        found = _violations(_imported_modules(path), frozenset({"groundwork_controlplane"}))
        if found:
            offenders[str(path.relative_to(SRC))] = found

    assert not offenders, (
        f"groundwork_shared must not import groundwork_controlplane; shared is below both "
        f"services in the dependency graph. Offenders: {offenders}"
    )


def test_channels_do_not_orchestrate() -> None:
    """Channels translate transport. They must not import the orchestrator.

    This is what let the CL-009 voice block sit on one directory without touching anything else. If
    a channel could drive orchestration directly, deferring a channel would stop being cheap.
    """
    files = _iter_python_files("groundwork_channels")

    offenders: dict[str, set[str]] = {}
    for path in files:
        found = _violations(_imported_modules(path), frozenset({"groundwork_orchestrator"}))
        if found:
            offenders[str(path.relative_to(SRC))] = found

    assert not offenders, (
        f"channels must go through the control plane, not drive orchestration directly. "
        f"Offenders: {offenders}"
    )


@pytest.mark.parametrize(
    "package",
    [
        "groundwork_contracts",
        "groundwork_controlplane",
        "groundwork_orchestrator",
        "groundwork_channels",
        "groundwork_shared",
    ],
)
def test_package_is_typed(package: str) -> None:
    """Every package ships a py.typed marker so consumers get real type checking."""
    assert (SRC / package / "py.typed").exists(), f"{package} is missing a py.typed marker"
