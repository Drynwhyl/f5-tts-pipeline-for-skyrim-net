#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE:-/workspace}"
CODEX_HOME="${CODEX_HOME:-$WORKSPACE_DIR/.codex}"
OUT_DIR="${CODEX_BACKUP_DIR:-$WORKSPACE_DIR/cloudsync/codex/current}"
INCLUDE_AUTH="${CODEX_BACKUP_INCLUDE_AUTH:-0}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$OUT_DIR/codex-state-$STAMP.tar.zst"
STABLE_ARCHIVE="$OUT_DIR/codex-state.tar.zst"
STABLE_SHA="$OUT_DIR/codex-state.tar.zst.sha256"
STAGING="$(mktemp -d)"

cleanup() {
  rm -rf "$STAGING"
}
trap cleanup EXIT

if [[ ! -d "$CODEX_HOME" ]]; then
  echo "Missing CODEX_HOME: $CODEX_HOME" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

excludes=(
  '.tmp'
  'app-server-control'
  'cache'
  'packages'
  'tmp'
)

if [[ "$INCLUDE_AUTH" != "1" ]]; then
  excludes+=('auth.json')
fi

if command -v rsync >/dev/null 2>&1; then
  rsync_args=(-a)
  for item in "${excludes[@]}"; do
    rsync_args+=(--exclude="$item")
  done
  mkdir -p "$STAGING/.codex"
  rsync "${rsync_args[@]}" "$CODEX_HOME/" "$STAGING/.codex/"
else
  cp -a "$CODEX_HOME" "$STAGING/.codex"
  for item in "${excludes[@]}"; do
    rm -rf "$STAGING/.codex/$item"
  done
fi

tar --zstd -cf "$ARCHIVE" -C "$STAGING" .codex
cp -f "$ARCHIVE" "$STABLE_ARCHIVE"
(
  cd "$OUT_DIR"
  sha256sum "$(basename "$STABLE_ARCHIVE")" > "$(basename "$STABLE_SHA")"
  sha256sum -c "$(basename "$STABLE_SHA")"
)

echo "$STABLE_ARCHIVE"
echo "$STABLE_SHA"
if [[ "$INCLUDE_AUTH" != "1" ]]; then
  echo "auth.json was excluded. Set CODEX_BACKUP_INCLUDE_AUTH=1 only if this storage is private and trusted."
fi
