"""The schema-budget gate must actually gate.

A check that can only pass is worse than no check — it reports success while
measuring nothing. `check_readme_claims.sh` silently skipped its two real
assertions for a while; these tests exist so this one can't.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "schema_budget.py"
BASELINE = REPO_ROOT / "evals" / "schema_budget.json"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


class TestBaselineIsHonest:
    def test_baseline_exists_and_is_committed(self) -> None:
        assert BASELINE.exists(), "schema budget baseline must be committed"
        data = json.loads(BASELINE.read_text())
        assert data["read_only_bytes"] > 0
        assert data["full_bytes"] > data["read_only_bytes"]

    def test_baseline_tool_counts_match_the_server(self) -> None:
        data = json.loads(BASELINE.read_text())
        assert data["full_tools"] == 27
        assert data["read_only_tools"] == 13

    def test_check_passes_against_the_committed_baseline(self) -> None:
        result = _run("--check")
        assert result.returncode == 0, result.stdout + result.stderr


class TestGateCatchesRegressions:
    def test_over_baseline_fails(self, tmp_path: Path) -> None:
        """Shrink a copied baseline; --check must reject the real surface."""
        real = json.loads(BASELINE.read_text())
        shrunk = {**real, "full_bytes": 1, "read_only_bytes": 1}
        backup = BASELINE.read_text()
        BASELINE.write_text(json.dumps(shrunk))
        try:
            result = _run("--check")
        finally:
            BASELINE.write_text(backup)
        assert result.returncode == 1
        assert "Schema budget exceeded" in result.stdout

    def test_json_output_reports_both_modes(self) -> None:
        result = _run("--json")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert set(data) == {"read_only", "full"}
        assert data["full"]["tools"] > data["read_only"]["tools"]
        # entries are sorted most-expensive first, which is what makes the
        # report actionable
        entries = data["full"]["entries"]
        assert entries == sorted(entries, key=lambda e: -e["bytes"])

    @pytest.mark.parametrize("mode", ["read_only", "full"])
    def test_totals_equal_the_sum_of_entries(self, mode: str) -> None:
        data = json.loads(_run("--json").stdout)
        assert data[mode]["total_bytes"] == sum(
            e["bytes"] for e in data[mode]["entries"]
        )
