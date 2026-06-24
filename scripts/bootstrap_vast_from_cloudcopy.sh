#!/usr/bin/env bash
set -euo pipefail

F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-/workspace/f5-tts}"
MIGRATION_DIR="${F5_TTS_MIGRATION_DIR:-/workspace/migration}"
CLOUD_SRC="${F5_TTS_CLOUD_SRC:-/F5-TTS-Vast/current/}"
ARCHIVE="$MIGRATION_DIR/f5-tts-data.tar.zst"
SHA="$MIGRATION_DIR/f5-tts-data.tar.zst.sha256"
TRANSFER="${F5_TTS_CLOUD_RESTORE_TRANSFER:-Cloud To Instance}"
TIMEOUT_SEC="${F5_TTS_CLOUD_COPY_TIMEOUT_SEC:-3600}"
POLL_SEC="${F5_TTS_CLOUD_COPY_POLL_SEC:-10}"
API_KEY="${VAST_API_KEY:-${VASTAI_API_KEY:-}}"
CONNECTION_ID="${VAST_CLOUD_CONNECTION_ID:-${F5_TTS_CLOUD_CONNECTION_ID:-}}"
INSTANCE_ID="${VAST_INSTANCE_ID:-${CONTAINER_ID:-${INSTANCE_ID:-}}}"
STATUS_FILE="$MIGRATION_DIR/bootstrap-status.md"

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

write_status() {
  mkdir -p "$MIGRATION_DIR"
  printf '# Bootstrap status\n\nLast update: %s\n\n%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" > "$STATUS_FILE"
}

normalize_payload() {
  if [[ ! -f "$ARCHIVE" && -f "$MIGRATION_DIR/current/f5-tts-data.tar.zst" ]]; then
    cp -f "$MIGRATION_DIR/current/f5-tts-data.tar.zst" "$ARCHIVE"
  fi
  if [[ ! -f "$SHA" && -f "$MIGRATION_DIR/current/f5-tts-data.tar.zst.sha256" ]]; then
    cp -f "$MIGRATION_DIR/current/f5-tts-data.tar.zst.sha256" "$SHA"
  fi
}

payload_ok() {
  normalize_payload
  [[ -f "$ARCHIVE" && -f "$SHA" ]] && (cd "$MIGRATION_DIR" && sha256sum -c "$(basename "$SHA")")
}

if [[ -z "$INSTANCE_ID" ]] && command -v vast-capabilities >/dev/null 2>&1; then
  INSTANCE_ID="$(vast-capabilities | jq -r '.instance.container_id // .instance.id // empty')"
fi

mkdir -p "$MIGRATION_DIR"

if [[ ! -d "$F5_TTS_BASE_DIR" ]]; then
  echo "Missing repo directory: $F5_TTS_BASE_DIR" >&2
  exit 1
fi

if [[ "${F5_TTS_SKIP_CLOUD_COPY:-0}" != "1" ]]; then
  if payload_ok; then
    log "Existing data archive checksum is OK; skipping Cloud Copy."
  else
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

    write_status "Requesting Cloud Copy from \`$CLOUD_SRC\` to \`$MIGRATION_DIR/\`."
    log "Requesting Cloud Copy restore for instance $INSTANCE_ID."
    vastai cloud copy \
      --api-key "$API_KEY" \
      --src "$CLOUD_SRC" \
      --dst "$MIGRATION_DIR/" \
      --instance "$INSTANCE_ID" \
      --connection "$CONNECTION_ID" \
      --transfer "$TRANSFER"
  fi
fi

deadline=$((SECONDS + TIMEOUT_SEC))
until payload_ok; do
  if (( SECONDS >= deadline )); then
    write_status "Timed out waiting for Cloud Copy payload."
    echo "Timed out waiting for $ARCHIVE and matching checksum." >&2
    exit 1
  fi
  log "Waiting for Cloud Copy payload..."
  sleep "$POLL_SEC"
done

write_status "Payload restored and checksum verified. Running setup."
log "Payload checksum OK. Running Vast setup."

export F5_TTS_DATA_ARCHIVE="$ARCHIVE"
export F5_TTS_INSTALL_SERVICES=1
bash "$F5_TTS_BASE_DIR/scripts/setup_vast.sh"

log "Waiting for API health."
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health >/tmp/f5-tts-health.json; then
    break
  fi
  sleep 5
done

curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:5000/ >/dev/null
curl -fsS http://127.0.0.1:7860/ >/dev/null
supervisorctl status f5-tts-api f5-tts-web f5-tts-gradio caddy

write_status "Bootstrap completed successfully."
log "Bootstrap completed successfully."
