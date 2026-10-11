"""`build-mcpb.sh` must freeze provenance wherever it is run from a checkout.

It used to ask `[ -d .git ]`. In a linked worktree `.git` is a *file* pointing
at the real git dir, so both the freeze and the "unknown commit" guard were
skipped: `make check-bundle` from a worktree built a bundle reporting
`commit unknown` and called it a success. CI and the main checkout have a real
`.git` directory, so nothing noticed.

The script is exercised as a script, against a throwaway repo, with `uv` and
`npx` stubbed on PATH — no node, no network, no real build.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Runs bash + git as local subprocesses. Opts out of the #408 real-I/O guard.
pytestmark = pytest.mark.allow_real_io

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build-mcpb.sh"

# Stands in for `uv run --directory <bundle> apple-mail-pz-mcp --version`:
# reports the commit frozen into the staged tree, as the real banner would.
# FAKE_UV_LOSE_BUILD_INFO simulates the frozen file not surviving the build.
# FAKE_UV_NOISE adds the kind of stderr chatter the real uv prints around it.
_FAKE_UV = """#!/bin/sh
if [ -n "${FAKE_UV_NOISE:-}" ]; then
    echo "warning: unknown field in pyproject.toml, ignoring" >&2
fi
info="$3/src/apple_mail_fast_mcp/_build_info.py"
if [ -f "$info" ] && [ -z "${FAKE_UV_LOSE_BUILD_INFO:-}" ]; then
    commit="$(sed -n "s/^COMMIT = '\\(.*\\)'$/\\1/p" "$info")"
    echo "apple-mail-pz-mcp 0.0.0 | commit $commit"
else
    echo "apple-mail-pz-mcp 0.0.0 | commit unknown (installed from sdist)"
fi
"""

# `npx --yes @anthropic-ai/mcpb@latest <validate|pack|info> ...`
_FAKE_NPX = """#!/bin/sh
if [ "$3" = "pack" ]; then : > "$5"; fi
"""


def _env(bin_dir: Path, **extra: str) -> dict[str, str]:
    # Drop inherited GIT_* (set when the suite runs under a git hook) so git
    # resolves the throwaway repo from cwd, and ignore the developer's config.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return {**env, **extra}


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _populate(root: Path) -> None:
    """The minimum tree the script stages from."""
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "build-mcpb.sh")
    (root / "mcpb").mkdir()
    (root / "mcpb" / "manifest.json").write_text('{\n  "version": "0.0.0"\n}\n')
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "hatch_build.py"):
        (root / name).write_text("")
    pkg = root / "src" / "apple_mail_fast_mcp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (root / ".gitignore").write_text("dist/\n")


@pytest.fixture(scope="module")
def bin_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("bin")
    for name, body in (("uv", _FAKE_UV), ("npx", _FAKE_NPX)):
        (d / name).write_text(body)
        (d / name).chmod(0o755)
    return d


@pytest.fixture(scope="module")
def checkouts(tmp_path_factory: pytest.TempPathFactory, bin_dir: Path) -> dict[str, Path]:
    """One repo seen three ways: its main checkout, a linked worktree, and an
    exported copy with no git at all (an unpacked sdist)."""
    base = tmp_path_factory.mktemp("repo")
    env = _env(bin_dir)

    main = base / "main"
    _populate(main)
    _git(main, env, "init", "-q")
    _git(main, env, "add", "-A")
    _git(main, env, "commit", "-q", "-m", "init")

    worktree = base / "worktree"
    _git(main, env, "worktree", "add", "-q", str(worktree))
    assert (worktree / ".git").is_file(), "a linked worktree's .git is a file"

    exported = base / "exported"
    _populate(exported)

    return {"main": main, "worktree": worktree, "exported": exported}


def _build(root: Path, bin_dir: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "build-mcpb.sh")],
        cwd=root,
        env=_env(bin_dir, **extra_env),
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("kind", ["main", "worktree"])
class TestInAGitCheckout:
    def test_bundle_reports_the_real_commit(
        self, checkouts: dict[str, Path], bin_dir: Path, kind: str
    ) -> None:
        root = checkouts[kind]
        head = _git(root, _env(bin_dir), "rev-parse", "--short=12", "HEAD")

        result = _build(root, bin_dir)

        assert result.returncode == 0, result.stderr
        assert f"commit {head}" in result.stdout
        assert "unknown" not in result.stdout

    def test_unknown_commit_fails_the_build(
        self, checkouts: dict[str, Path], bin_dir: Path, kind: str
    ) -> None:
        result = _build(checkouts[kind], bin_dir, FAKE_UV_LOSE_BUILD_INFO="1")

        assert result.returncode == 1
        assert "unknown commit" in result.stderr

    def test_an_unrelated_unknown_does_not_fail_the_build(
        self, checkouts: dict[str, Path], bin_dir: Path, kind: str
    ) -> None:
        """The smoke output carries uv's stderr too. Only the banner's own
        `commit unknown` means the provenance was lost (#7)."""
        result = _build(checkouts[kind], bin_dir, FAKE_UV_NOISE="1")

        assert result.returncode == 0, result.stderr
        assert "unknown commit" not in result.stderr


class TestOutsideAGitCheckout:
    def test_unknown_is_tolerated_without_git(
        self, checkouts: dict[str, Path], bin_dir: Path
    ) -> None:
        """No repo means no commit to freeze; "unknown" is the honest answer."""
        result = _build(checkouts["exported"], bin_dir)

        assert result.returncode == 0, result.stderr
        assert "commit unknown" in result.stdout
