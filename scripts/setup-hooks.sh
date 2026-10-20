#!/usr/bin/env bash
set -eo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"
CHECK_SCRIPT="$REPO_ROOT/scripts/hooks/check-file-tokens.sh"

mkdir -p "$HOOKS_DIR"
chmod +x "$CHECK_SCRIPT"

ln -sf "../../scripts/hooks/check-file-tokens.sh" "$HOOKS_DIR/pre-commit"
chmod +x "$HOOKS_DIR/pre-commit"

echo "Pre-commit hook installed successfully at $HOOKS_DIR/pre-commit -> $CHECK_SCRIPT"
