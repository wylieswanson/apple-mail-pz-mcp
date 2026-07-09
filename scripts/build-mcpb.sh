#!/usr/bin/env bash
# Build the Claude Desktop .mcpb bundle (#332).
#
# Produces dist/apple-mail-pz-mcp-<version>.mcpb — a uv-type MCP bundle
# (manifest_version 0.4). Claude Desktop manages Python + uv and resolves
# dependencies from the bundled pyproject.toml/uv.lock at install time, so we
# never bundle compiled wheels (the spec warns those aren't portable).
#
# Requires Node (for `npx @anthropic-ai/mcpb`). Run: ./scripts/build-mcpb.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MANIFEST="mcpb/manifest.json"
VERSION="$(grep '"version"' "$MANIFEST" | head -1 | sed -E 's/.*"version": *"([^"]+)".*/\1/')"
BUILD_DIR="$(mktemp -d)"
OUT_DIR="$ROOT/dist"
OUT="$OUT_DIR/apple-mail-pz-mcp-${VERSION}.mcpb"

cleanup() { rm -rf "$BUILD_DIR"; }
trap cleanup EXIT

echo "Building apple-mail-pz-mcp.mcpb v${VERSION}"

# Stage only what `uv run` needs at the bundle root to resolve + launch the
# server: the manifest, the project metadata/lock, the README referenced by
# pyproject, the license, the package source, and every build-backend input
# pyproject names. `hatch_build.py` is one of those: pyproject declares it as a
# custom wheel hook, so omitting it makes the host's `uv run` fail with
# "Build script does not exist: hatch_build.py" — a bundle that installs and
# then cannot start. The smoke test below is what catches that class of bug.
cp "$MANIFEST" "$BUILD_DIR/manifest.json"
cp pyproject.toml uv.lock README.md LICENSE hatch_build.py "$BUILD_DIR/"
mkdir -p "$BUILD_DIR/src"
cp -R src/apple_mail_fast_mcp "$BUILD_DIR/src/"

# Drop compiled bytecode. A developer's working tree has __pycache__ from the
# last test run (CI's fresh checkout does not), and copying it doubles the
# bundle with .pyc files for whatever interpreters happened to run locally.
find "$BUILD_DIR/src" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$BUILD_DIR" -name '*.pyc' -delete

# Freeze provenance into the bundle. The staged tree has no .git, so the Hatch
# build hook can't resolve a commit there and `--version` would report
# "unknown". This script *does* run in the repo, so write _build_info.py now —
# the same file the hook would have produced. Tracked modifications only, to
# match `git describe --dirty` (see hatch_build.py).
if [ -d .git ]; then
    COMMIT="$(git rev-parse --short=12 HEAD)"
    COMMIT_DATE="$(git log -1 --format=%cI)"
    BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then DIRTY=True; else DIRTY=False; fi
    cat > "$BUILD_DIR/src/apple_mail_fast_mcp/_build_info.py" <<EOF
"""Generated at bundle-build time by scripts/build-mcpb.sh. Do not edit."""

COMMIT = '${COMMIT}'
COMMIT_DATE = '${COMMIT_DATE}'
BUILT_AT = '${BUILT_AT}'
DIRTY = ${DIRTY}
EOF
    echo "Froze provenance: ${COMMIT} (dirty=${DIRTY})"
fi

# Smoke test: run the bundle exactly the way the host will
# (`uv run --directory <bundle> apple-mail-pz-mcp`), so a bundle that cannot
# build or launch fails the build instead of shipping. `--version` exercises
# the full build + import + argparse path and exits immediately.
#
# Run it against a COPY: `uv run --directory` materializes a .venv (~200MB)
# inside the directory it is given, and packing that would balloon the bundle
# from ~300KB to ~70MB.
SMOKE_DIR="$(mktemp -d)"
smoke_cleanup() { rm -rf "$SMOKE_DIR"; }
trap 'cleanup; smoke_cleanup' EXIT
cp -R "$BUILD_DIR"/. "$SMOKE_DIR"/

echo "Smoke-testing the staged bundle (as the host launches it)..."
if ! SMOKE_OUT="$(uv run --directory "$SMOKE_DIR" apple-mail-pz-mcp --version 2>&1)"; then
    echo "$SMOKE_OUT" >&2
    echo ""
    echo "ERROR: the staged bundle does not launch. Do not ship it." >&2
    echo "       Reproduce with: uv run --directory <bundle> apple-mail-pz-mcp --version" >&2
    exit 1
fi
echo "  $SMOKE_OUT"

# The bundle must carry its provenance. If this says "unknown", the frozen
# _build_info.py did not survive the build and .mcpb users cannot tell which
# commit they are running.
if [ -d .git ] && echo "$SMOKE_OUT" | grep -q "unknown"; then
    echo "ERROR: bundle reports an unknown commit; _build_info.py did not survive." >&2
    exit 1
fi
smoke_cleanup

# Belt and braces: the staging dir must still be pristine. A build artifact in
# here silently multiplies the shipped bundle's size.
for junk in .venv __pycache__ dist; do
    if [ -e "$BUILD_DIR/$junk" ]; then
        echo "ERROR: build artifact '$junk' leaked into the staging dir." >&2
        exit 1
    fi
done

# Validate against the MCPB schema, then pack the directory into a .mcpb (zip).
npx --yes @anthropic-ai/mcpb@latest validate "$BUILD_DIR/manifest.json"
mkdir -p "$OUT_DIR"
npx --yes @anthropic-ai/mcpb@latest pack "$BUILD_DIR" "$OUT"

echo ""
npx --yes @anthropic-ai/mcpb@latest info "$OUT" || true
echo ""
echo "Built: $OUT"
