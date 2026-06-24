#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE:-/workspace}"
ARCHIVE="${1:-/workspace/cloudsync/codex/current/codex-state.tar.zst}"
SHA="${ARCHIVE}.sha256"

if [[ ! -f "$ARCHIVE" ]]; then
  echo "Missing Codex state archive: $ARCHIVE" >&2
  exit 1
fi

if [[ -f "$SHA" ]]; then
  (cd "$(dirname "$ARCHIVE")" && sha256sum -c "$(basename "$SHA")")
fi

mkdir -p "$WORKSPACE_DIR"
tar --zstd -xf "$ARCHIVE" -C "$WORKSPACE_DIR"
echo "Restored Codex state into $WORKSPACE_DIR/.codex"
echo "If auth.json was not included in the backup, run Codex login again on this instance."
