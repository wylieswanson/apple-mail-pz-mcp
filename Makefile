.PHONY: help install dev test test-unit test-integration test-e2e test-verbose lint format typecheck complexity audit check-all coverage clean eval-descriptions eval-tools eval-tasks schema-budget check-bundle

help:
	@echo "Available targets:"
	@echo "  make install          - Install dependencies"
	@echo "  make dev              - Install with dev dependencies"
	@echo "  make test             - Run unit tests"
	@echo "  make test-unit        - Run unit tests only"
	@echo "  make test-integration - Run integration tests (requires Mail.app)"
	@echo "  make test-e2e         - Run end-to-end tests"
	@echo "  make test-verbose     - Run tests with verbose output"
	@echo "  make lint             - Run ruff linter"
	@echo "  make format           - Run ruff formatter"
	@echo "  make typecheck        - Run mypy type checker"
	@echo "  make complexity       - Check cyclomatic complexity"
	@echo "  make schema-budget    - Report the tools/list token cost per request"
	@echo "  make check-bundle     - Build + smoke-test the .mcpb bundle"
	@echo "  make eval-tasks       - Multi-turn cost eval: round-trips per completed task"
	@echo "  make audit            - Run all audit scripts"
	@echo "  make check-all        - Run all checks"
	@echo "  make coverage         - Run tests with coverage report"
	@echo "  make clean            - Remove cache and build artifacts"

install:
	uv sync

dev:
	uv sync --dev

test:
	uv run pytest tests/ -m "not integration and not e2e and not benchmark" -q

test-unit:
	uv run pytest tests/unit/ -q

test-integration:
	MAIL_TEST_MODE=true uv run pytest tests/integration/ --run-integration -v

test-e2e:
	MAIL_TEST_MODE=true uv run pytest tests/e2e/ -v

benchmark:
	MAIL_TEST_MODE=true uv run pytest tests/benchmarks/ --run-benchmark -v -s

benchmark-baseline:
	@echo "Re-capturing baselines into tests/benchmarks/baseline.json..."
	MAIL_TEST_MODE=true uv run pytest tests/benchmarks/ --run-benchmark --capture-baseline -v -s

test-verbose:
	uv run pytest tests/ -m "not integration and not e2e and not benchmark" -v --tb=long

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff format src/ tests/

typecheck:
	uv run mypy src/

complexity:
	@./scripts/check_complexity.sh

audit:
	@./scripts/check_dependencies.sh
	@./scripts/check_applescript_safety.sh
	@./scripts/check_readme_claims.sh

coverage:
	uv run pytest tests/ -m "not integration and not e2e and not benchmark" --cov=apple_mail_fast_mcp --cov-report=term-missing -q

schema-budget:
	@uv run python scripts/schema_budget.py

# Build the .mcpb and smoke-test it by launching it as the host does. Not part
# of check-all (needs node + network); CI runs it on every PR.
check-bundle:
	@./scripts/build-mcpb.sh

# Keep this list identical to .github/workflows/test.yml's unit-tests job.
# Three divergent definitions of "validated" (here, CI, and the release skill)
# are how check_readme_claims.sh sat un-run for months while silently skipping
# its own assertions.
check-all: lint typecheck test test-e2e complexity
	@./scripts/check_version_sync.sh
	@./scripts/check_client_server_parity.sh
	@./scripts/check_docs.sh
	@./scripts/check_readme_claims.sh
	@./scripts/check_applescript_safety.sh
	@uv run python scripts/schema_budget.py --check
	@echo ""
	@echo "All checks passed."

clean:
	rm -rf __pycache__ .pytest_cache .coverage htmlcov/ .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Regenerate the blind-eval tool descriptions from the live FastMCP server
# (keeps evals/agent_tool_usability/tool_descriptions.md in sync — #219).
eval-descriptions:
	uv run python evals/agent_tool_usability/generate_descriptions.py

# Run the blind agent tool-usability eval against the open-weight OpenRouter
# models (needs an OPENROUTER_API_KEY env var or the apple-mail-pz-mcp-evals /
# openrouter Keychain entry; costs money). The Claude column is produced
# separately via a Claude Code subagent. See evals/agent_tool_usability/. (#219)
#
# Models use each family's latest non-dated slug where one exists
# (mistralai/mistral-large, deepseek/deepseek-chat), so the eval tracks the
# current model instead of pinning a dated id that later 404s (#358). The exact
# version served is recorded per-result as `resolved_model`, and run_eval
# pre-checks availability (fails loud before spending credits). qwen-2.5-72b /
# llama-3.3-70b have no non-dated alias, so their line slug is used as-is.
eval-tools:
	uv run --with openai python evals/agent_tool_usability/run_eval.py \
		--model mistralai/mistral-large qwen/qwen-2.5-72b-instruct \
		meta-llama/llama-3.3-70b-instruct deepseek/deepseek-chat \
		--runs 5

# Multi-turn cost eval: drives a real model through the real MCP server and
# reports round-trips per completed task. MODEL=<openrouter-slug> to override.
MODEL ?= anthropic/claude-sonnet-5
eval-tasks:
	uv run --with openai python evals/agent_tool_usability/task_eval.py \
		--model $(MODEL) --json evals/agent_tool_usability/results/task_cost.json
