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

# shellcheck source=scripts/cloud_copy_restore_lib.sh
source "$F5_TTS_BASE_DIR/scripts/cloud_copy_restore_lib.sh"

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

write_status() {
  mkdir -p "$MIGRATION_DIR"
  printf '# Bootstrap status\n\nLast update: %s\n\n%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" > "$STATUS_FILE"
}

promote_copy_result() {
  local found_path="$1"
  local final_path="$2"
  local tmp_path

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

    run_id="run-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    run_dir="$INCOMING_DIR/$run_id"
    checksum_retry_dir="$INCOMING_DIR/${run_id}-checksum-retry"
    archive_retry_dir="$INCOMING_DIR/${run_id}-archive-retry"
    checksum_timeout_sec="${F5_TTS_CHECKSUM_TIMEOUT_SEC:-90}"
    mkdir -p "$run_dir"

    write_status "Requesting Vast Cloud Copy from \`$CLOUD_SRC\` to unique staging \`$run_dir/\`."
    log "Requesting Vast directory Cloud Copy restore for instance $INSTANCE_ID."
    if ! cc_request_cloud_copy "$CLOUD_SRC" "$run_dir" "$INSTANCE_ID" "$CONNECTION_ID" "$API_KEY"; then
      write_status "Vast directory Cloud Copy request failed."
      echo "Vast directory Cloud Copy request failed." >&2
      exit 1
    fi

    write_status "Waiting for restored payload files to become stable."
    sha_path="$(cc_wait_named_file "$run_dir" "f5-tts-data.tar.zst.sha256" "$checksum_timeout_sec" "$POLL_SEC" "$STABLE_POLLS")" || true
    if [[ -z "$sha_path" ]]; then
      log "Payload checksum was absent from the directory restore; requesting it separately."
      cc_request_cloud_copy \
        "${CLOUD_SRC%/}/f5-tts-data.tar.zst.sha256" \
        "$checksum_retry_dir" "$INSTANCE_ID" "$CONNECTION_ID" "$API_KEY" || {
          write_status "Separate checksum Cloud Copy request failed."
          echo "Separate checksum Cloud Copy request failed." >&2
          exit 1
        }
      sha_path="$(cc_wait_named_file "$checksum_retry_dir" "f5-tts-data.tar.zst.sha256" 120 "$POLL_SEC" 2)" || {
        write_status "Timed out waiting for separately restored checksum file."
        echo "Timed out waiting for separately restored checksum file." >&2
        exit 1
      }
    fi

    archive_path="$(cc_wait_checksum_match "$run_dir" "$sha_path" "f5-tts-data*.tar.zst" "$TIMEOUT_SEC" "$POLL_SEC")" || true
    if [[ -z "$archive_path" ]]; then
      log "No payload archive matched after the first restore; retrying the cloud directory once."
      cc_request_cloud_copy "$CLOUD_SRC" "$archive_retry_dir" "$INSTANCE_ID" "$CONNECTION_ID" "$API_KEY" || {
        write_status "Payload archive retry request failed."
        echo "Payload archive retry request failed." >&2
        exit 1
      }
      archive_path="$(cc_wait_checksum_match "$archive_retry_dir" "$sha_path" "f5-tts-data*.tar.zst" "$TIMEOUT_SEC" "$POLL_SEC")" || {
        write_status "No restored payload archive matched its checksum."
        echo "No restored payload archive matched its checksum." >&2
        exit 1
      }
    fi

    promote_copy_result "$sha_path" "$SHA"
    promote_copy_result "$archive_path" "$ARCHIVE"
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
supervisorctl status f5-tts-api f5-tts-web caddy
supervisorctl status f5-tts-gradio || true

write_status "Bootstrap completed successfully."
log "Bootstrap completed successfully."
