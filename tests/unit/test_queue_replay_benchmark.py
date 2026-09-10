from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.benchmark
def test_queue_replay_harness_runs_small_fixture() -> None:
    script = Path("scripts/benchmarks/queue_replay.py")
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(script),
            "--tenants",
            "2",
            "--deployments-per-tenant",
            "2",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "total_deployments: 4" in completed.stdout
    assert "executed: 4" in completed.stdout
