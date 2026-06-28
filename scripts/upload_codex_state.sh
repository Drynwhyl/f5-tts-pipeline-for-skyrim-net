#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE:-/workspace}"
F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-$WORKSPACE_DIR/f5-tts}"
CURRENT_DIR="${CODEX_BACKUP_DIR:-$WORKSPACE_DIR/cloudsync/codex/current}"
PUBLISH_DIR="${CODEX_PUBLISH_DIR:-$WORKSPACE_DIR/cloudsync/codex/publish}"
CLOUD_DST="${CODEX_CLOUD_DST:-/F5-TTS-Vast/codex/v2/current/}"
DRY_RUN="${CODEX_CLOUD_COPY_DRY_RUN:-0}"
API_KEY="${VAST_API_KEY:-${VASTAI_API_KEY:-}}"
CONNECTION_ID="${VAST_CLOUD_CONNECTION_ID:-${F5_TTS_CLOUD_CONNECTION_ID:-}}"
INSTANCE_ID="${VAST_INSTANCE_ID:-${CONTAINER_ID:-${INSTANCE_ID:-}}}"

if [[ -z "$INSTANCE_ID" ]] && command -v vast-capabilities >/dev/null 2>&1; then
  INSTANCE_ID="$(vast-capabilities | jq -r '.instance.container_id // .instance.id // empty')"
fi

if [[ -z "$API_KEY" ]]; then
  echo "Set VAST_API_KEY to a Vast API key with copy permissions." >&2
  exit 1
fi
if [[ -z "$CONNECTION_ID" ]]; then
  echo "Set VAST_CLOUD_CONNECTION_ID to the Google Drive cloud connection id." >&2
  exit 1
fi
if [[ ! "$CONNECTION_ID" =~ ^[0-9]+$ ]]; then
  echo "VAST_CLOUD_CONNECTION_ID must be numeric, not the connection name." >&2
  echo "Current value: $CONNECTION_ID" >&2
  exit 1
fi
if [[ -z "$INSTANCE_ID" ]]; then
  echo "Could not determine this Vast instance id. Set VAST_INSTANCE_ID." >&2
  exit 1
fi

"$F5_TTS_BASE_DIR/scripts/backup_codex_state.sh"
archive="$CURRENT_DIR/codex-state.tar.zst"
sha="$CURRENT_DIR/codex-state.tar.zst.sha256"
if [[ ! -f "$archive" || ! -f "$sha" ]]; then
  echo "Codex state backup was not created correctly." >&2
  exit 1
fi

rm -rf "$PUBLISH_DIR"
mkdir -p "$PUBLISH_DIR"
cp -f "$archive" "$PUBLISH_DIR/codex-state.tar.zst"
cp -f "$sha" "$PUBLISH_DIR/codex-state.tar.zst.sha256"
cp -f "$archive" "$PUBLISH_DIR/codex-state-$(date -u +%Y%m%dT%H%M%SZ).tar.zst"
(cd "$PUBLISH_DIR" && sha256sum -c codex-state.tar.zst.sha256)

cmd=(vastai copy
  "C.$INSTANCE_ID:$PUBLISH_DIR/" \
  "drive.$CONNECTION_ID:$CLOUD_DST" \
  --api-key "$API_KEY")

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'Dry-run only. Vast API was not called.\n'
  printf 'Command that would run:\n'
  redacted=("${cmd[@]}")
  for i in "${!redacted[@]}"; do
    if [[ "${redacted[$i]}" == "--api-key" && -n "${redacted[$((i + 1))]:-}" ]]; then
      redacted[$((i + 1))]="<redacted>"
    fi
  done
  printf '  %q' "${redacted[@]}"
  printf '\n'
  exit 0
fi

if output="$("${cmd[@]}" 2>&1)"; then
  status=0
else
  status=$?
fi
printf '%s\n' "$output"
if (( status != 0 )) || grep -qiE 'failed with error|authorization error|traceback' <<<"$output"; then
  echo "Vast Codex state upload request failed." >&2
  exit 1
fi

echo "Codex state upload requested for instance $INSTANCE_ID -> $CLOUD_DST"
