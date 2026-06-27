#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE:-/workspace}"
ARCHIVE="${1:-/workspace/cloudsync/codex/current/codex-state.tar.zst}"
ARCHIVE_NAME="$(basename "$ARCHIVE")"
SEARCH_DIR="$(dirname "$ARCHIVE")"
STAGING="$(mktemp -d)"

cleanup() {
  rm -rf "$STAGING"
}
trap cleanup EXIT

find_file() {
  local expected_path="$1"
  local filename="$2"
  local base_dir="$3"

  if [[ -f "$expected_path" ]]; then
    printf '%s\n' "$expected_path"
    return 0
  fi
  if [[ -f "$expected_path/$filename" ]]; then
    printf '%s\n' "$expected_path/$filename"
    return 0
  fi
  find "$base_dir" -maxdepth 5 -type f -name "$filename" -print -quit 2>/dev/null
}

archive_path="$(find_file "$ARCHIVE" "$ARCHIVE_NAME" "$SEARCH_DIR")"
if [[ -z "$archive_path" ]]; then
  echo "Missing Codex state archive: $ARCHIVE" >&2
  exit 1
fi

cp -f "$archive_path" "$STAGING/codex-state.tar.zst"

sha_path="$(find_file "${ARCHIVE}.sha256" "${ARCHIVE_NAME}.sha256" "$SEARCH_DIR")"
if [[ -z "$sha_path" ]]; then
  echo "Missing Codex state checksum: ${ARCHIVE}.sha256" >&2
  exit 1
fi
cp -f "$sha_path" "$STAGING/codex-state.tar.zst.sha256"
(cd "$STAGING" && sha256sum -c codex-state.tar.zst.sha256)

mkdir -p "$WORKSPACE_DIR"
tar --zstd -xf "$STAGING/codex-state.tar.zst" -C "$WORKSPACE_DIR"
echo "Restored Codex state into $WORKSPACE_DIR/.codex"
echo "If auth.json was not included in the backup, run Codex login again on this instance."
