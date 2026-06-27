#!/usr/bin/env bash
set -euo pipefail

F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-/workspace/f5-tts}"
MIGRATION_DIR="${F5_TTS_MIGRATION_DIR:-/workspace/migration}"
INCOMING_DIR="${F5_TTS_MIGRATION_INCOMING_DIR:-$MIGRATION_DIR/incoming}"
CLOUD_SRC="${F5_TTS_CLOUD_SRC:-/F5-TTS-Vast/current/}"
ARCHIVE="$MIGRATION_DIR/f5-tts-data.tar.zst"
SHA="$MIGRATION_DIR/f5-tts-data.tar.zst.sha256"
TIMEOUT_SEC="${F5_TTS_CLOUD_COPY_TIMEOUT_SEC:-3600}"
POLL_SEC="${F5_TTS_CLOUD_COPY_POLL_SEC:-10}"
STABLE_POLLS="${F5_TTS_CLOUD_COPY_STABLE_POLLS:-3}"
API_KEY="${VAST_API_KEY:-${VASTAI_API_KEY:-}}"
CONNECTION_ID="${VAST_CLOUD_CONNECTION_ID:-${F5_TTS_CLOUD_CONNECTION_ID:-}}"
INSTANCE_ID="${VAST_INSTANCE_ID:-${CONTAINER_ID:-${INSTANCE_ID:-}}}"
STATUS_FILE="$MIGRATION_DIR/bootstrap-status.md"

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

write_status() {
  mkdir -p "$MIGRATION_DIR"
  printf '# Bootstrap status\n\nLast update: %s\n\n%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" > "$STATUS_FILE"
}

find_copy_result() {
  local expected_path="$1"
  local basename_expected
  basename_expected="$(basename "$expected_path")"

  if [[ -f "$expected_path" ]]; then
    printf '%s\n' "$expected_path"
    return 0
  fi
  if [[ -f "$expected_path/$basename_expected" ]]; then
    printf '%s\n' "$expected_path/$basename_expected"
    return 0
  fi
  find "$INCOMING_DIR" -maxdepth 4 -type f -name "$basename_expected" -print -quit 2>/dev/null
}

wait_for_stable_copy() {
  local expected_path="$1"
  local deadline=$((SECONDS + TIMEOUT_SEC))
  local stable_count=0
  local last_size=""
  local found_path=""
  local size=""

  while (( SECONDS < deadline )); do
    found_path="$(find_copy_result "$expected_path")"
    if [[ -n "$found_path" && -f "$found_path" ]]; then
      size="$(stat -c '%s' "$found_path")"
      if [[ "$size" != "0" && "$size" == "$last_size" ]]; then
        stable_count=$((stable_count + 1))
      else
        stable_count=0
      fi
      last_size="$size"

      if (( stable_count >= STABLE_POLLS )); then
        printf '%s\n' "$found_path"
        return 0
      fi
      log "Waiting for stable copy of $(basename "$expected_path") (${size} bytes)..."
    else
      log "Waiting for copy result: $(basename "$expected_path")..."
    fi
    sleep "$POLL_SEC"
  done

  return 1
}

promote_copy_result() {
  local expected_path="$1"
  local final_path="$2"
  local found_path tmp_path

  found_path="$(wait_for_stable_copy "$expected_path")" || return 1
  tmp_path="$MIGRATION_DIR/.$(basename "$final_path").tmp.$$"
  cp -f "$found_path" "$tmp_path"
  rm -rf "$final_path"
  mv "$tmp_path" "$final_path"
}

payload_ok() {
  [[ -f "$ARCHIVE" && -f "$SHA" ]] && (cd "$MIGRATION_DIR" && sha256sum -c "$(basename "$SHA")")
}

if [[ -z "$INSTANCE_ID" ]] && command -v vast-capabilities >/dev/null 2>&1; then
  INSTANCE_ID="$(vast-capabilities | jq -r '.instance.container_id // .instance.id // empty')"
fi

mkdir -p "$MIGRATION_DIR" "$INCOMING_DIR"

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
    if [[ ! "$CONNECTION_ID" =~ ^[0-9]+$ ]]; then
      echo "VAST_CLOUD_CONNECTION_ID must be the numeric id from 'vastai show connections', not the connection name." >&2
      echo "Current value: $CONNECTION_ID" >&2
      exit 1
    fi
    if [[ -z "$INSTANCE_ID" ]]; then
      echo "Could not determine this Vast instance id. Set VAST_INSTANCE_ID." >&2
      exit 1
    fi

    rm -rf "$INCOMING_DIR"
    mkdir -p "$INCOMING_DIR"

    write_status "Requesting Vast Cloud Copy from \`$CLOUD_SRC\` to \`$INCOMING_DIR/\`."
    log "Requesting Vast directory Cloud Copy restore for instance $INSTANCE_ID."
    if output="$(vastai cloud copy \
        --src "${CLOUD_SRC%/}" \
        --dst "$INCOMING_DIR" \
        --instance "$INSTANCE_ID" \
        --connection "$CONNECTION_ID" \
        --transfer "Cloud To Instance" \
        --api-key "$API_KEY" 2>&1)"; then
      status=0
    else
      status=$?
    fi
    printf '%s\n' "$output"
    if (( status != 0 )) || grep -qiE 'failed with error|authorization error|traceback' <<<"$output"; then
      write_status "Vast directory Cloud Copy request failed."
      echo "Vast directory Cloud Copy request failed." >&2
      exit 1
    fi

    write_status "Waiting for restored payload files to become stable."
    promote_copy_result "$INCOMING_DIR/f5-tts-data.tar.zst.sha256" "$SHA" || {
      write_status "Timed out waiting for checksum file."
      echo "Timed out waiting for restored checksum file." >&2
      exit 1
    }
    promote_copy_result "$INCOMING_DIR/f5-tts-data.tar.zst" "$ARCHIVE" || {
      write_status "Timed out waiting for data archive."
      echo "Timed out waiting for restored data archive." >&2
      exit 1
    }
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
