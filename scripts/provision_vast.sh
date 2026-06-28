#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE:-/workspace}"
F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-$WORKSPACE_DIR/f5-tts}"
F5_TTS_REPO_URL="${F5_TTS_REPO_URL:-https://github.com/Drynwhyl/f5-tts-pipeline-for-skyrim-net.git}"
LOG_FILE="${F5_TTS_BOOTSTRAP_LOG:-$WORKSPACE_DIR/bootstrap.log}"
MIGRATION_DIR="${F5_TTS_MIGRATION_DIR:-$WORKSPACE_DIR/migration}"
STATUS_FILE="$MIGRATION_DIR/bootstrap-status.md"
LOCK_FILE="$WORKSPACE_DIR/.f5-tts-bootstrap.lock"
DONE_FILE="$WORKSPACE_DIR/.f5-tts-bootstrap-complete"
GITHUB_TOKEN_VALUE="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

mkdir -p "$WORKSPACE_DIR" "$MIGRATION_DIR"
touch "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

write_status() {
  mkdir -p "$MIGRATION_DIR"
  printf '# Bootstrap status\n\nLast update: %s\n\n%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" > "$STATUS_FILE"
}

source_environment() {
  set -a
  [[ -f /etc/environment ]] && . /etc/environment
  [[ -f "$WORKSPACE_DIR/.env" ]] && . "$WORKSPACE_DIR/.env"
  set +a
}

clone_or_update_repo() {
  local clone_url="$F5_TTS_REPO_URL"

  if [[ -n "$GITHUB_TOKEN_VALUE" && "$clone_url" =~ ^https://github.com/(.*)$ ]]; then
    clone_url="https://x-access-token:${GITHUB_TOKEN_VALUE}@github.com/${BASH_REMATCH[1]}"
  fi

  git config --global --add safe.directory "$F5_TTS_BASE_DIR" >/dev/null 2>&1 || true
  mkdir -p "$(dirname "$F5_TTS_BASE_DIR")"

  if [[ ! -d "$F5_TTS_BASE_DIR/.git" ]]; then
    log "Cloning repo into $F5_TTS_BASE_DIR."
    git clone "$clone_url" "$F5_TTS_BASE_DIR"
  else
    log "Updating repo in $F5_TTS_BASE_DIR."
    git -C "$F5_TTS_BASE_DIR" fetch origin master
    git -C "$F5_TTS_BASE_DIR" checkout master
    git -C "$F5_TTS_BASE_DIR" pull --ff-only origin master
  fi

  git -C "$F5_TTS_BASE_DIR" remote set-url origin "$F5_TTS_REPO_URL"
}

determine_instance_id() {
  local instance_id="${VAST_INSTANCE_ID:-${CONTAINER_ID:-${INSTANCE_ID:-}}}"
  if [[ -z "$instance_id" ]] && command -v vast-capabilities >/dev/null 2>&1; then
    instance_id="$(vast-capabilities | jq -r '.instance.container_id // .instance.id // empty')"
  fi
  printf '%s\n' "$instance_id"
}

find_copy_result() {
  local search_dir="$1"
  local expected_path="$2"
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
  find "$search_dir" -maxdepth 4 -type f -name "$basename_expected" -print -quit 2>/dev/null
}

wait_for_stable_file() {
  local search_dir="$1"
  local expected_path="$2"
  local timeout_sec="${3:-300}"
  local poll_sec="${4:-5}"
  local stable_polls="${5:-2}"
  local deadline=$((SECONDS + timeout_sec))
  local stable_count=0
  local last_size=""
  local found_path=""
  local size=""

  while (( SECONDS < deadline )); do
    found_path="$(find_copy_result "$search_dir" "$expected_path")"
    if [[ -n "$found_path" && -f "$found_path" ]]; then
      size="$(stat -c '%s' "$found_path")"
      if [[ "$size" != "0" && "$size" == "$last_size" ]]; then
        stable_count=$((stable_count + 1))
      else
        stable_count=0
      fi
      last_size="$size"

      if (( stable_count >= stable_polls )); then
        printf '%s\n' "$found_path"
        return 0
      fi
      log "Waiting for stable copy of $(basename "$expected_path") (${size} bytes)..."
    else
      log "Waiting for copy result: $(basename "$expected_path")..."
    fi
    sleep "$poll_sec"
  done

  return 1
}

find_file_matching_checksum() {
  local search_dir="$1"
  local sha_path="$2"
  local expected_hash candidate

  expected_hash="$(awk 'NR == 1 { print $1 }' "$sha_path")"
  [[ "$expected_hash" =~ ^[[:xdigit:]]{64}$ ]] || return 1

  while IFS= read -r -d '' candidate; do
    if [[ "$(sha256sum "$candidate" | awk '{ print $1 }')" == "$expected_hash" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done < <(find "$search_dir" -maxdepth 4 -type f -name 'codex-state*.tar.zst' -print0 2>/dev/null)

  return 1
}

restore_codex_from_cloud() {
  local api_key="${VAST_API_KEY:-${VASTAI_API_KEY:-}}"
  local connection_id="${VAST_CLOUD_CONNECTION_ID:-${F5_TTS_CLOUD_CONNECTION_ID:-}}"
  local instance_id="$1"
  local cloud_src="${CODEX_CLOUD_SRC:-/F5-TTS-Vast/codex/current/}"
  local current_dir="${CODEX_BACKUP_DIR:-$WORKSPACE_DIR/cloudsync/codex/current}"
  local incoming_dir="$current_dir/incoming"
  local timeout_sec="${CODEX_RESTORE_TIMEOUT_SEC:-300}"
  local archive_path sha_path

  if [[ "${RESTORE_CODEX:-1}" != "1" ]]; then
    log "Skipping Codex state restore (RESTORE_CODEX=${RESTORE_CODEX:-})."
    return 0
  fi
  if [[ -z "$api_key" || -z "$connection_id" || -z "$instance_id" ]]; then
    log "Skipping Codex state restore; Vast API key, connection id, or instance id is missing."
    return 0
  fi
  if [[ ! "$connection_id" =~ ^[0-9]+$ ]]; then
    log "Skipping Codex state restore; VAST_CLOUD_CONNECTION_ID is not numeric."
    return 0
  fi

  rm -rf "$incoming_dir"
  mkdir -p "$incoming_dir"

  log "Requesting Codex state restore from $cloud_src."
  if output="$(vastai cloud copy \
      --src "${cloud_src%/}" \
      --dst "$incoming_dir" \
      --instance "$instance_id" \
      --connection "$connection_id" \
      --transfer "Cloud To Instance" \
      --api-key "$api_key" 2>&1)"; then
    status=0
  else
    status=$?
  fi
  printf '%s\n' "$output"
  if (( status != 0 )) || grep -qiE 'failed with error|authorization error|traceback' <<<"$output"; then
    log "Codex state Cloud Copy request failed; continuing without restored Codex state."
    return 0
  fi

  sha_path="$(wait_for_stable_file "$incoming_dir" "$incoming_dir/codex-state.tar.zst.sha256" "$timeout_sec" 5 2)" || {
    log "Codex state checksum was not restored; refusing to extract an unverified archive."
    return 0
  }
  archive_path="$(wait_for_stable_file "$incoming_dir" "$incoming_dir/codex-state.tar.zst" 30 5 2)" || true
  if [[ -z "$archive_path" ]]; then
    archive_path="$(find_file_matching_checksum "$incoming_dir" "$sha_path")" || {
      log "No Codex archive matched the restored checksum; refusing to extract it."
      return 0
    }
    log "Stable Codex archive name was absent; using checksum-matched $(basename "$archive_path")."
  fi

  mkdir -p "$current_dir"
  cp -f "$sha_path" "$current_dir/codex-state.tar.zst.sha256"
  cp -f "$archive_path" "$current_dir/codex-state.tar.zst"

  if ! (cd "$current_dir" && sha256sum -c codex-state.tar.zst.sha256); then
    log "Codex state checksum verification failed; refusing to extract the archive."
    return 0
  fi

  bash "$F5_TTS_BASE_DIR/scripts/restore_codex_state.sh" "$current_dir/codex-state.tar.zst" || {
    log "Codex state restore failed; continuing without restored Codex state."
    return 0
  }
}

main() {
  source_environment

  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "Another F5-TTS bootstrap is already running; exiting."
    write_status "Another F5-TTS bootstrap is already running."
    exit 0
  fi

  if [[ -f "$DONE_FILE" && "${F5_TTS_FORCE_BOOTSTRAP:-0}" != "1" ]]; then
    log "Bootstrap already completed; set F5_TTS_FORCE_BOOTSTRAP=1 to rerun."
    write_status "Bootstrap already completed."
    exit 0
  fi

  write_status "Starting Vast provisioning bootstrap."
  log "Starting Vast provisioning bootstrap."

  clone_or_update_repo
  cd "$F5_TTS_BASE_DIR"

  bash scripts/setup_github_auth.sh

  if [[ "${INSTALL_CODEX:-1}" == "1" ]]; then
    bash scripts/install_codex_workspace.sh
    restore_codex_from_cloud "$(determine_instance_id)"
  fi

  bash scripts/bootstrap_vast_from_cloudcopy.sh

  touch "$DONE_FILE"
  write_status "Provisioning bootstrap completed successfully."
  log "Provisioning bootstrap completed successfully."
}

main "$@"
