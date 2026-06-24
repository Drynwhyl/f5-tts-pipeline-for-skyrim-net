#!/usr/bin/env bash
set -euo pipefail

F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-/workspace/f5-tts}"
CLOUDSYNC_DIR="${F5_TTS_CLOUDSYNC_DIR:-/workspace/cloudsync}"
CURRENT_DIR="$CLOUDSYNC_DIR/current"
CLOUD_DST="${F5_TTS_CLOUD_DST:-/F5-TTS-Vast/current/}"
TRANSFER="${F5_TTS_CLOUD_UPLOAD_TRANSFER:-Instance To Cloud}"
DRY_RUN="${F5_TTS_CLOUD_COPY_DRY_RUN:-0}"
API_KEY="${VAST_API_KEY:-${VASTAI_API_KEY:-}}"
CONNECTION_ID="${VAST_CLOUD_CONNECTION_ID:-${F5_TTS_CLOUD_CONNECTION_ID:-}}"
INSTANCE_ID="${VAST_INSTANCE_ID:-${CONTAINER_ID:-${INSTANCE_ID:-}}}"

if [[ -z "$INSTANCE_ID" ]] && command -v vast-capabilities >/dev/null 2>&1; then
  INSTANCE_ID="$(vast-capabilities | jq -r '.instance.container_id // .instance.id // empty')"
fi

if [[ -z "$API_KEY" ]]; then
  echo "Set VAST_API_KEY to a Vast API key with cloud copy permissions." >&2
  exit 1
fi
if [[ -z "$CONNECTION_ID" ]]; then
  echo "Set VAST_CLOUD_CONNECTION_ID to the Google Drive cloud connection id." >&2
  exit 1
fi
if [[ -z "$INSTANCE_ID" ]]; then
  echo "Could not determine this Vast instance id. Set VAST_INSTANCE_ID." >&2
  exit 1
fi

"$F5_TTS_BASE_DIR/scripts/prepare_cloud_payload.sh"

cmd=(vastai cloud copy
  --api-key "$API_KEY" \
  --src "$CURRENT_DIR/" \
  --dst "$CLOUD_DST" \
  --instance "$INSTANCE_ID" \
  --connection "$CONNECTION_ID" \
  --transfer "$TRANSFER")

if [[ "$DRY_RUN" == "1" ]]; then
  cmd+=(--dry-run)
fi

"${cmd[@]}"

echo "Cloud upload requested for instance $INSTANCE_ID -> $CLOUD_DST"
