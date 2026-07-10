"""The build hook freezes git provenance into the wheel. Both of its bugs lived
in `finalize()`.

1. It deleted `_build_info.py` unconditionally — including the copy that
   `build-mcpb.sh` stages into the .mcpb tree, which has no `.git` and so never
   goes through `initialize()`. Bundles shipped reporting "commit unknown".
2. It counted untracked files as a dirty tree, so every `uvx --from git+…`
   install reported `-dirty` from a pristine clone (uv leaves a `.ok` marker in
   the checkout it builds from).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import hatch_build  # noqa: E402


class _Hook(hatch_build.CustomBuildHook):
    """Construct the hook without hatchling's plugin machinery."""

    def __init__(self, root: str) -> None:  # noqa: D107  (no super().__init__)
        self.__dict__["_root"] = root

    @property
    def root(self) -> str:  # type: ignore[override]
        return self.__dict__["_root"]


@pytest.fixture
def hook(tmp_path: Path) -> _Hook:
    (tmp_path / "src" / "apple_mail_fast_mcp").mkdir(parents=True)
    return _Hook(str(tmp_path))


TARGET = Path("src/apple_mail_fast_mcp/_build_info.py")


class TestFinalizeOnlyRemovesWhatItWrote:
    def test_a_staged_build_info_survives(
        self, hook: _Hook, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The .mcpb path: no git, so initialize() bails — finalize must not
        delete the provenance build-mcpb.sh froze in."""
        staged = tmp_path / TARGET
        staged.write_text("COMMIT = 'staged'\n")
        monkeypatch.setattr(hatch_build, "_git", lambda *a: None)  # no .git

        build_data: dict[str, Any] = {}
        hook.initialize("standard", build_data)
        hook.finalize("standard", build_data, "artifact.whl")

        assert staged.exists(), "finalize() deleted a file it did not create"
        assert staged.read_text() == "COMMIT = 'staged'\n"

    def test_its_own_file_is_cleaned_up(
        self, hook: _Hook, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The repo path: initialize() wrote it, so the checkout stays clean."""
        monkeypatch.setattr(
            hatch_build,
            "_git",
            lambda root, *a: "" if a[0] == "status" else "abc123def456",
        )
        build_data: dict[str, Any] = {}
        hook.initialize("standard", build_data)
        assert (tmp_path / TARGET).exists()

        hook.finalize("standard", build_data, "artifact.whl")
        assert not (tmp_path / TARGET).exists()

    def test_finalize_without_initialize_is_safe(
        self, hook: _Hook, tmp_path: Path
    ) -> None:
        staged = tmp_path / TARGET
        staged.write_text("COMMIT = 'staged'\n")
        hook.finalize("standard", {}, "artifact.whl")
        assert staged.exists()


class TestInitializeShipsTheProvenance:
    def test_force_include_or_the_wheel_drops_it(
        self, hook: _Hook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_build_info.py is gitignored, and hatchling excludes VCS-ignored
        paths — without force_include it is written and then silently dropped."""
        monkeypatch.setattr(
            hatch_build,
            "_git",
            lambda root, *a: "" if a[0] == "status" else "abc123def456",
        )
        build_data: dict[str, Any] = {}
        hook.initialize("standard", build_data)
        assert list(build_data["force_include"].values()) == [
            "apple_mail_fast_mcp/_build_info.py"
        ]

    def test_no_git_writes_nothing(
        self, hook: _Hook, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hatch_build, "_git", lambda *a: None)
        build_data: dict[str, Any] = {}
        hook.initialize("standard", build_data)
        assert not (tmp_path / TARGET).exists()
        assert "force_include" not in build_data


class TestDirtyIgnoresUntrackedFiles:
    def test_status_is_asked_to_exclude_untracked(
        self, hook: _Hook, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uv drops an untracked `.ok` into the checkout it builds from."""
        seen: list[tuple[str, ...]] = []

        def fake_git(root: Any, *args: str) -> str | None:
            seen.append(args)
            if args[0] == "rev-parse":
                return "abc123def456"
            if args[0] == "status":
                return "" if "--untracked-files=no" in args else "?? .ok"
            return "2026-07-09T00:00:00Z"

        monkeypatch.setattr(hatch_build, "_git", fake_git)
        hook.initialize("standard", {})

        status_calls = [a for a in seen if a[0] == "status"]
        assert status_calls, "dirty state was never computed"
        assert all("--untracked-files=no" in a for a in status_calls)

    def test_dirty_written_as_false_for_a_clean_tree(
        self, hook: _Hook, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hatch_build,
            "_git",
            lambda root, *a: "" if a[0] == "status" else "abc123def456",
        )
        hook.initialize("standard", {})
        assert "DIRTY = False" in (tmp_path / TARGET).read_text()


class TestGitHelperNeverRaises:
    def test_missing_repo_returns_none(self, tmp_path: Path) -> None:
        assert hatch_build._git(tmp_path, "rev-parse", "HEAD") is None

    def test_subprocess_failure_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".git").mkdir()

        def boom(*_a: Any, **_k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="git", timeout=5)

        monkeypatch.setattr(subprocess, "run", boom)
        assert hatch_build._git(tmp_path, "rev-parse", "HEAD") is None
