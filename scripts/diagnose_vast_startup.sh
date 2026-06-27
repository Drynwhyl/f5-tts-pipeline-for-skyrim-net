#!/usr/bin/env bash
set -uo pipefail

section() {
  printf '\n== %s ==\n' "$1"
}

path_state() {
  local path="$1"
  if [[ -e "$path" ]]; then
    printf 'present  %s\n' "$path"
  else
    printf 'missing  %s\n' "$path"
  fi
}

env_state() {
  local name="$1"
  local value="${!name:-}"
  if [[ -n "$value" ]]; then
    printf 'set      %s (length=%s)\n' "$name" "${#value}"
  else
    printf 'unset    %s\n' "$name"
  fi
}

section "Process and launch mode"
if [[ -r /proc/1/cmdline ]]; then
  tr '\0' ' ' </proc/1/cmdline
  printf '\n'
fi
if [[ -f /.launch ]]; then
  printf '/.launch: '
  tr '\n' ' ' </.launch
  printf '\n'
else
  printf '/.launch is absent\n'
fi

section "Expected base-image paths"
for path in \
  /opt/instance-tools/bin/entrypoint.sh \
  /workspace \
  /etc/supervisor \
  /etc/portal.yaml \
  /.provisioning_complete \
  /.provisioning_failed \
  /workspace/bootstrap.log \
  /workspace/migration/bootstrap-status.md \
  /workspace/.f5-tts-bootstrap-complete; do
  path_state "$path"
done

section "Non-secret path settings"
printf 'WORKSPACE=%q\n' "${WORKSPACE:-<unset>}"
printf 'DATA_DIRECTORY=%q\n' "${DATA_DIRECTORY:-<unset>}"
printf 'JUPYTER_DIR=%q\n' "${JUPYTER_DIR:-<unset>}"
printf 'F5_TTS_BASE_DIR=%q\n' "${F5_TTS_BASE_DIR:-<unset>}"
printf 'CODEX_HOME=%q\n' "${CODEX_HOME:-<unset>}"

section "Required secret settings (values suppressed)"
for name in VAST_API_KEY VAST_CLOUD_CONNECTION_ID GITHUB_TOKEN; do
  env_state "$name"
done
env_state PROVISIONING_SCRIPT

section "SSH keys"
for key_file in /root/.ssh/authorized_keys /home/user/.ssh/authorized_keys; do
  if [[ -s "$key_file" ]]; then
    printf '%s: %s non-empty line(s)\n' \
      "$key_file" "$(grep -cve '^[[:space:]]*$' "$key_file" 2>/dev/null || true)"
    if command -v ssh-keygen >/dev/null 2>&1; then
      ssh-keygen -lf "$key_file" 2>/dev/null || true
    fi
  else
    printf '%s: missing or empty\n' "$key_file"
  fi
done

section "Services"
if command -v supervisorctl >/dev/null 2>&1; then
  supervisorctl status 2>&1 || true
else
  printf 'supervisorctl is unavailable\n'
fi

section "Result"
result=0
if [[ ! -d /workspace ]]; then
  printf 'FAIL: /workspace is absent; the Vast base-image entrypoint did not complete.\n'
  result=1
elif [[ ! -s /root/.ssh/authorized_keys ]]; then
  printf 'FAIL: root authorized_keys is absent or empty; SSH key injection/propagation did not complete.\n'
  result=1
elif [[ ! -f /workspace/bootstrap.log ]]; then
  printf 'FAIL: F5-TTS provisioning did not start. Check PROVISIONING_SCRIPT and provisioning logs.\n'
  result=1
else
  printf 'Base startup reached the F5-TTS provisioning stage. Inspect bootstrap-status.md and service status above.\n'
fi

exit "$result"
