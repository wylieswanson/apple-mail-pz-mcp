"""Hatch build hook: freeze git provenance into the wheel.

An installed package has no ``.git`` beside it, so ``uv tool install`` /
``pip install`` would otherwise lose the commit forever. Capture it at build
time into ``_build_info.py``, which ``version.py`` prefers over a live git
lookup.

The file is generated into the source tree (it is gitignored) and removed
afterwards, so a build never leaves the checkout dirty. If git isn't available
— an sdist built off-repo — the hook writes nothing and provenance degrades to
"unknown" rather than to a wrong answer.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_TARGET = Path("src/apple_mail_fast_mcp/_build_info.py")


def _git(root: Path, *args: str) -> str | None:
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        commit = _git(root, "rev-parse", "--short=12", "HEAD")
        if not commit:
            return
        commit_date = _git(root, "log", "-1", "--format=%cI")
        # Tracked modifications only. Build tools litter their checkouts with
        # untracked marker files — uv drops a `.ok` into the git checkout it
        # builds from — and counting those flags every uvx install as "dirty".
        # `git describe --dirty` ignores untracked files for the same reason:
        # an untracked file does not make HEAD stop describing the code.
        dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=no"))
        built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        target = root / _TARGET
        target.write_text(
            '"""Generated at build time by hatch_build.py. Do not edit or commit."""\n\n'
            f"COMMIT = {commit!r}\n"
            f"COMMIT_DATE = {commit_date!r}\n"
            f"BUILT_AT = {built_at!r}\n"
            f"DIRTY = {dirty!r}\n"
        )
        # The file is gitignored, and hatchling excludes VCS-ignored paths from
        # the wheel by default — without force_include the provenance is written
        # and then silently dropped from the artifact.
        build_data.setdefault("force_include", {})[str(target)] = (
            "apple_mail_fast_mcp/_build_info.py"
        )

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact_path: str
    ) -> None:
        target = Path(self.root) / _TARGET
        if target.exists():
            target.unlink()
