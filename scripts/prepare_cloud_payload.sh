#!/usr/bin/env bash
set -euo pipefail

F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-/workspace/f5-tts}"
CLOUDSYNC_DIR="${F5_TTS_CLOUDSYNC_DIR:-/workspace/cloudsync}"
CURRENT_DIR="$CLOUDSYNC_DIR/current"
BACKUPS_DIR="$CLOUDSYNC_DIR/backups"

if [[ ! -d "$F5_TTS_BASE_DIR" ]]; then
  echo "Missing repo directory: $F5_TTS_BASE_DIR" >&2
  exit 1
fi

mkdir -p "$CURRENT_DIR" "$BACKUPS_DIR"

mapfile -t outputs < <("$F5_TTS_BASE_DIR/scripts/make_migration_archive.sh" "$BACKUPS_DIR")
archive="${outputs[0]:-}"
sha="${outputs[1]:-}"

if [[ ! -f "$archive" || ! -f "$sha" ]]; then
  echo "Migration archive was not created correctly." >&2
  exit 1
fi

cp -f "$archive" "$CURRENT_DIR/f5-tts-data.tar.zst"
(
  cd "$CURRENT_DIR"
  sha256sum f5-tts-data.tar.zst > f5-tts-data.tar.zst.sha256
  sha256sum -c f5-tts-data.tar.zst.sha256
)

cat >"$CLOUDSYNC_DIR/README.txt" <<EOF
This directory is prepared for Vast Cloud Copy.

Upload current payload:
  src: $CURRENT_DIR/
  dst: /F5-TTS-Vast/current/
  transfer: Instance To Cloud

The timestamped backup remains at:
  $archive
  $sha
EOF

echo "$CURRENT_DIR/f5-tts-data.tar.zst"
echo "$CURRENT_DIR/f5-tts-data.tar.zst.sha256"
echo "$archive"
echo "$sha"
