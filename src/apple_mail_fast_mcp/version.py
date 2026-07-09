"""Version, commit, and build-date introspection.

Answers "what exactly am I running?" for three audiences: the CLI (``--version``),
the MCP host (``serverInfo.version`` at initialize), and an agent asking the
question mid-conversation (the ``server`` block of ``diagnose_mail_access``).

Provenance is resolved in descending order of trust:

1. ``_build_info.py`` — written by the Hatch build hook at wheel-build time.
   This is the only source that survives ``uv tool install`` / ``pip install``,
   because the installed package has no ``.git`` alongside it.
2. ``git`` — for a source checkout (``uv sync``, editable installs), where the
   working tree is the truth and may be dirty.
3. Neither — an sdist built outside a repo. Commit is then genuinely unknown,
   and we say so rather than guessing.

Never raises: a version banner must not be able to take down the server.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from apple_mail_fast_mcp import __version__

_GIT_TIMEOUT_SECONDS = 2


def _repo_root() -> Path:
    # src/apple_mail_fast_mcp/version.py -> src/apple_mail_fast_mcp -> src -> repo
    return Path(__file__).resolve().parents[2]


def _git(*args: str) -> str | None:
    """Run a git command in the repo root, or return None if that isn't possible."""
    root = _repo_root()
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _from_build_hook() -> dict[str, Any] | None:
    try:
        from apple_mail_fast_mcp import _build_info  # type: ignore[attr-defined]
    except ImportError:
        return None
    return {
        "commit": getattr(_build_info, "COMMIT", None),
        "commit_date": getattr(_build_info, "COMMIT_DATE", None),
        "built_at": getattr(_build_info, "BUILT_AT", None),
        "dirty": getattr(_build_info, "DIRTY", None),
        "source": "build",
    }


def _from_git() -> dict[str, Any] | None:
    commit = _git("rev-parse", "--short=12", "HEAD")
    if commit is None:
        return None
    return {
        "commit": commit,
        "commit_date": _git("log", "-1", "--format=%cI"),
        "built_at": None,
        # Tracked modifications only, matching `git describe --dirty`: an
        # untracked file doesn't stop HEAD from describing the code, and build
        # tools leave untracked markers behind (see hatch_build.py).
        "dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        "source": "git",
    }


@lru_cache(maxsize=1)
def build_info() -> dict[str, Any]:
    """Return version/commit/date provenance. Cached; never raises."""
    info: dict[str, Any] = {
        "version": __version__,
        "commit": None,
        "commit_date": None,
        "built_at": None,
        "dirty": None,
        "source": "unknown",
    }
    try:
        resolved = _from_build_hook() or _from_git()
    except Exception:  # pragma: no cover - defensive; provenance is never critical
        resolved = None
    if resolved:
        info.update(resolved)
    return info


def version_banner() -> str:
    """One-line human-readable version string for ``--version``."""
    info = build_info()
    parts = [f"apple-mail-pz-mcp {info['version']}"]
    if info["commit"]:
        commit = info["commit"]
        if info["dirty"]:
            commit += "-dirty"
        parts.append(f"commit {commit}")
    if info["commit_date"]:
        parts.append(f"committed {info['commit_date']}")
    if info["built_at"]:
        parts.append(f"built {info['built_at']}")
    if info["source"] == "unknown":
        parts.append("commit unknown (installed from sdist)")
    return " | ".join(parts)
