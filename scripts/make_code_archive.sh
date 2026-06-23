#!/usr/bin/env bash
set -euo pipefail

F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-/workspace/f5-tts}"
OUT_DIR="${1:-/workspace/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$OUT_DIR/f5-tts-code-$STAMP.tar.zst"
SHA="$ARCHIVE.sha256"

mkdir -p "$OUT_DIR"
cd "$F5_TTS_BASE_DIR"

tar --zstd -cf "$ARCHIVE" \
  --exclude=.git \
  --exclude=__pycache__ \
  --exclude='*.pyc' \
  --exclude=F5TTS_v1_Base_v2 \
  --exclude=voices \
  --exclude=ruaccent-data \
  --exclude=logs \
  --exclude=backups \
  --exclude='*.tar.zst' \
  --exclude='*.tar.zst.sha256' \
  .
sha256sum "$ARCHIVE" > "$SHA"

echo "$ARCHIVE"
echo "$SHA"
