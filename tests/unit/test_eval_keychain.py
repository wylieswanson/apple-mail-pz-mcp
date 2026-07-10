"""The eval's OpenRouter key must survive every rename of this project.

A Keychain service name identifies a *stored secret*, not the distribution.
The v0.11.0 rename changed `KEYCHAIN_SERVICE` from `apple-mail-fast-mcp-evals`
to `apple-mail-pz-mcp-evals` without adding the old name to the fallback chain,
so a key stored under the name the README told users to use became unreachable
and `make eval-tools` failed with "no API key". Same class of bug the IMAP
`SERVICE_NAME_PREFIX` fallback exists to prevent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

EVAL_DIR = Path(__file__).resolve().parents[2] / "evals" / "agent_tool_usability"
sys.path.insert(0, str(EVAL_DIR))
# run_eval imports `openai`, which is not a runtime dependency of the server.
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

import run_eval  # noqa: E402


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def _keychain_with(**stored: str) -> Any:
    """A fake Keychain holding keys under the given service names."""

    def lookup(service: str) -> str:
        return stored.get(service, "")

    return lookup


class TestKeychainFallbackChain:
    def test_current_service_is_preferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            run_eval,
            "_keychain_lookup",
            _keychain_with(
                **{"apple-mail-pz-mcp-evals": "new", "apple-mail-fast-mcp-evals": "old"}
            ),
        )
        assert run_eval.get_api_key() == "new"

    @pytest.mark.parametrize(
        "service",
        ["apple-mail-fast-mcp-evals", "apple-mail-mcp-evals"],
    )
    def test_every_pre_rename_name_still_resolves(
        self, monkeypatch: pytest.MonkeyPatch, service: str
    ) -> None:
        """A key stored under any name this project ever used must keep working."""
        monkeypatch.setattr(
            run_eval, "_keychain_lookup", _keychain_with(**{service: "stored"})
        )
        assert run_eval.get_api_key() == "stored"

    def test_env_var_beats_the_keychain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
        monkeypatch.setattr(
            run_eval, "_keychain_lookup", _keychain_with(**{"apple-mail-pz-mcp-evals": "kc"})
        )
        assert run_eval.get_api_key() == "from-env"

    def test_chain_lists_the_name_the_readme_documented(self) -> None:
        """Guards against a future rename dropping a name off the chain."""
        assert "apple-mail-fast-mcp-evals" in run_eval._LEGACY_KEYCHAIN_SERVICES
        assert run_eval.KEYCHAIN_SERVICE == "apple-mail-pz-mcp-evals"
