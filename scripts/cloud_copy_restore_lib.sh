#!/usr/bin/env bash

# Helpers for Vast Cloud Copy restores. Vast may add directory levels, preserve
# stale directory entries, or restore the stable archive name as a directory.
# Callers should always use a unique staging directory and identify archives by
# checksum instead of relying on their final path.

cc_find_named_file() {
  local search_dir="$1"
  local filename="$2"
  local candidate relative depth
  local best_candidate=""
  local best_depth=999999

  while IFS= read -r -d '' candidate; do
    relative="${candidate#"$search_dir"/}"
    depth="${relative//[^\/]/}"
    depth="${#depth}"
    if (( depth < best_depth )); then
      best_candidate="$candidate"
      best_depth="$depth"
    fi
  done < <(find "$search_dir" -maxdepth 8 -type f -name "$filename" -print0 2>/dev/null || true)

  [[ -n "$best_candidate" ]] || return 1
  printf '%s\n' "$best_candidate"
}

cc_wait_named_file() {
  local search_dir="$1"
  local filename="$2"
  local timeout_sec="$3"
  local poll_sec="${4:-5}"
  local stable_polls="${5:-2}"
  local deadline=$((SECONDS + timeout_sec))
  local stable_count=0
  local last_path=""
  local last_size=""
  local candidate size

  while (( SECONDS < deadline )); do
    if candidate="$(cc_find_named_file "$search_dir" "$filename")" && [[ -f "$candidate" ]]; then
      size="$(stat -c '%s' "$candidate" 2>/dev/null || true)"
      if [[ -n "$size" && "$size" != "0" && "$candidate" == "$last_path" && "$size" == "$last_size" ]]; then
        stable_count=$((stable_count + 1))
      else
        stable_count=0
      fi
      last_path="$candidate"
      last_size="$size"

      if (( stable_count >= stable_polls )); then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
    sleep "$poll_sec"
  done

  return 1
}

cc_find_checksum_match() {
  local search_dir="$1"
  local checksum_path="$2"
  local archive_pattern="$3"
  local expected_hash candidate actual_hash

  expected_hash="$(awk 'NR == 1 { print $1 }' "$checksum_path" 2>/dev/null || true)"
  [[ "$expected_hash" =~ ^[[:xdigit:]]{64}$ ]] || return 1

  while IFS= read -r -d '' candidate; do
    actual_hash="$(sha256sum "$candidate" 2>/dev/null | awk '{ print $1 }' || true)"
    if [[ "$actual_hash" == "$expected_hash" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done < <(find "$search_dir" -maxdepth 8 -type f -name "$archive_pattern" -print0 2>/dev/null || true)

  return 1
}

cc_wait_checksum_match() {
  local search_dir="$1"
  local checksum_path="$2"
  local archive_pattern="$3"
  local timeout_sec="$4"
  local poll_sec="${5:-10}"
  local deadline=$((SECONDS + timeout_sec))
  local candidate

  while (( SECONDS < deadline )); do
    if candidate="$(cc_find_checksum_match "$search_dir" "$checksum_path" "$archive_pattern")"; then
      printf '%s\n' "$candidate"
      return 0
    fi
    sleep "$poll_sec"
  done

  return 1
}

cc_request_cloud_copy() {
  local cloud_src="$1"
  local destination="$2"
  local instance_id="$3"
  local connection_id="$4"
  local api_key="$5"
  local output status

  mkdir -p "$destination"
  if output="$(vastai cloud copy \
      --src "${cloud_src%/}" \
      --dst "$destination" \
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
    return 1
  fi
}
