"""Version/commit/build-date provenance (#version-introspection)."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from apple_mail_fast_mcp import __version__, version


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    version.build_info.cache_clear()


class TestBuildInfoFromBuildHook:
    def test_prefers_generated_build_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = SimpleNamespace(
            COMMIT="abc123def456",
            COMMIT_DATE="2026-07-09T12:00:00-07:00",
            BUILT_AT="2026-07-09T19:05:00+00:00",
            DIRTY=False,
        )
        monkeypatch.setitem(sys.modules, "apple_mail_fast_mcp._build_info", fake)
        # git must not even be consulted when the build hook baked it in
        monkeypatch.setattr(
            version, "_git", lambda *a: pytest.fail("git should not run")
        )
        info = version.build_info()
        assert info["source"] == "build"
        assert info["commit"] == "abc123def456"
        assert info["built_at"] == "2026-07-09T19:05:00+00:00"
        assert info["version"] == __version__


class TestBuildInfoFromGit:
    def test_falls_back_to_git_in_a_checkout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(version, "_from_build_hook", lambda: None)
        calls: dict[tuple[str, ...], str | None] = {
            ("rev-parse", "--short=12", "HEAD"): "0ef7dd33b850",
            ("log", "-1", "--format=%cI"): "2026-07-09T14:40:02-07:00",
            ("status", "--porcelain"): "",
        }
        monkeypatch.setattr(version, "_git", lambda *a: calls.get(a))
        info = version.build_info()
        assert info["source"] == "git"
        assert info["commit"] == "0ef7dd33b850"
        assert info["dirty"] is False

    def test_untracked_files_do_not_mean_dirty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uv leaves an untracked `.ok` in the checkout it builds from.

        Counting untracked files flagged every `uvx --from git+…` install as
        -dirty. `git status --porcelain --untracked-files=no` is the fix, and
        this asserts we pass that flag.
        """
        seen: list[tuple[str, ...]] = []

        def fake_git(*args: str) -> str | None:
            seen.append(args)
            if args[0] == "rev-parse":
                return "9aa07b537411"
            if args[0] == "status":
                # only tracked changes are reported when -uno is passed
                return "" if "--untracked-files=no" in args else "?? .ok"
            return "2026-07-09T15:57:51-07:00"

        monkeypatch.setattr(version, "_from_build_hook", lambda: None)
        monkeypatch.setattr(version, "_git", fake_git)
        assert version.build_info()["dirty"] is False
        assert any("--untracked-files=no" in a for a in seen), (
            "status must exclude untracked files"
        )

    def test_dirty_tree_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(version, "_from_build_hook", lambda: None)
        monkeypatch.setattr(
            version,
            "_git",
            lambda *a: "deadbeefcafe"
            if a[0] == "rev-parse"
            else (" M src/x.py" if a[0] == "status" else "2026-07-09T00:00:00Z"),
        )
        assert version.build_info()["dirty"] is True


class TestBuildInfoUnknown:
    def test_no_build_hook_and_no_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(version, "_from_build_hook", lambda: None)
        monkeypatch.setattr(version, "_git", lambda *a: None)
        info = version.build_info()
        assert info["source"] == "unknown"
        assert info["commit"] is None
        # the version itself always resolves
        assert info["version"] == __version__

    def test_never_raises_when_git_explodes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("git segfaulted")

        monkeypatch.setattr(version, "_from_build_hook", lambda: None)
        monkeypatch.setattr(subprocess, "run", boom)
        assert version.build_info()["version"] == __version__

    def test_git_timeout_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def timeout(*_a: Any, **_k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="git", timeout=2)

        monkeypatch.setattr(subprocess, "run", timeout)
        assert version._git("rev-parse", "HEAD") in (None,)


class TestVersionBanner:
    def test_banner_includes_version_and_commit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            version,
            "build_info",
            lambda: {
                "version": "0.10.2",
                "commit": "abc123",
                "commit_date": "2026-07-09T12:00:00-07:00",
                "built_at": None,
                "dirty": False,
                "source": "git",
            },
        )
        banner = version.version_banner()
        assert "0.10.2" in banner
        assert "abc123" in banner
        assert "2026-07-09T12:00:00-07:00" in banner
        assert "dirty" not in banner

    def test_banner_marks_dirty_tree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            version,
            "build_info",
            lambda: {
                "version": "0.10.2",
                "commit": "abc123",
                "commit_date": None,
                "built_at": None,
                "dirty": True,
                "source": "git",
            },
        )
        assert "abc123-dirty" in version.version_banner()

    def test_banner_admits_unknown_provenance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            version,
            "build_info",
            lambda: {
                "version": "0.10.2",
                "commit": None,
                "commit_date": None,
                "built_at": None,
                "dirty": None,
                "source": "unknown",
            },
        )
        banner = version.version_banner()
        assert "0.10.2" in banner
        assert "unknown" in banner
