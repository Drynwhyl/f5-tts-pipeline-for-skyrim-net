#!/usr/bin/env bash
set -euo pipefail

F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-/workspace/f5-tts}"
F5_TTS_VENV="${F5_TTS_VENV:-/workspace/f5-tts-env}"
F5_TTS_MODEL_DIR="${F5_TTS_MODEL_DIR:-$F5_TTS_BASE_DIR/F5TTS_v1_Base_v2}"
F5_TTS_VOICES_DIR="${F5_TTS_VOICES_DIR:-$F5_TTS_BASE_DIR/voices}"
F5_TTS_CONFIG_PATH="${F5_TTS_CONFIG_PATH:-$F5_TTS_BASE_DIR/config.json}"
F5_TTS_CACHE_DIR="${F5_TTS_CACHE_DIR:-/workspace/f5-tts-cache}"
F5_TTS_RUACCENT_DIR="${F5_TTS_RUACCENT_DIR:-$F5_TTS_BASE_DIR/ruaccent-data}"
HF_HOME="${HF_HOME:-/workspace/.hf_home}"
XDG_CACHE_HOME="${XDG_CACHE_HOME:-/workspace/.cache}"
PIP_CACHE_DIR="${PIP_CACHE_DIR:-/workspace/.cache/pip}"

API_INTERNAL_PORT="${F5_TTS_API_INTERNAL_PORT:-8000}"
WEB_INTERNAL_PORT="${F5_TTS_WEB_INTERNAL_PORT:-5000}"
GRADIO_INTERNAL_PORT="${F5_TTS_GRADIO_INTERNAL_PORT:-7860}"
API_EXTERNAL_PORT="${F5_TTS_API_EXTERNAL_PORT:-10100}"
WEB_EXTERNAL_PORT="${F5_TTS_WEB_EXTERNAL_PORT:-10200}"
GRADIO_EXTERNAL_PORT="${F5_TTS_GRADIO_EXTERNAL_PORT:-6006}"
REPLACE_TENSORBOARD="${F5_TTS_REPLACE_TENSORBOARD:-1}"

if [[ "$(id -u)" != "0" ]]; then
  echo "This script must run as root because it writes supervisor and portal config." >&2
  exit 1
fi
if [[ ! -x "$F5_TTS_VENV/bin/python" ]]; then
  echo "Missing venv: $F5_TTS_VENV. Run scripts/setup_vast.sh first." >&2
  exit 1
fi
if ! command -v supervisorctl >/dev/null 2>&1; then
  echo "supervisorctl not found; this script is for Vast/base-image containers." >&2
  exit 1
fi

install -d /opt/supervisor-scripts /etc/supervisor/conf.d

write_wrapper() {
  local path="$1"
  local label="$2"
  local command="$3"

  cat >"$path" <<EOF
#!/bin/bash
utils=/opt/supervisor-scripts/utils
. "\${utils}/logging.sh"
. "\${utils}/cleanup_generic.sh"
. "\${utils}/pty.sh"
. "\${utils}/environment.sh"
. "\${utils}/exit_serverless.sh"
. "\${utils}/exit_portal.sh" "$label"

export F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR}"
export F5_TTS_VENV="${F5_TTS_VENV}"
export F5_TTS_MODEL_DIR="${F5_TTS_MODEL_DIR}"
export F5_TTS_VOICES_DIR="${F5_TTS_VOICES_DIR}"
export F5_TTS_CONFIG_PATH="${F5_TTS_CONFIG_PATH}"
export F5_TTS_CACHE_DIR="${F5_TTS_CACHE_DIR}"
export F5_TTS_RUACCENT_DIR="${F5_TTS_RUACCENT_DIR}"
export HF_HOME="${HF_HOME}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR}"
export F5_TTS_API_URL="http://127.0.0.1:${API_INTERNAL_PORT}"

source "${F5_TTS_VENV}/bin/activate"
cd "${F5_TTS_BASE_DIR}"
pty $command 2>&1
EOF
  chmod +x "$path"
}

write_conf() {
  local path="$1"
  local name="$2"
  local command="$3"

  cat >"$path" <<EOF
[program:$name]
environment=PROC_NAME="%(program_name)s"
command=$command
autostart=true
autorestart=unexpected
stdout_logfile=/dev/stdout
redirect_stderr=true
stdout_logfile_maxbytes=0
EOF
}

write_wrapper "/opt/supervisor-scripts/f5-tts-api.sh" "F5-TTS API" \
  "python ${F5_TTS_BASE_DIR}/api_server.py"
write_wrapper "/opt/supervisor-scripts/f5-tts-web.sh" "F5-TTS Web" \
  "python ${F5_TTS_BASE_DIR}/web_ui.py"
write_wrapper "/opt/supervisor-scripts/f5-tts-gradio.sh" "F5-TTS Gradio" \
  "python ${F5_TTS_BASE_DIR}/run_gradio.py --host 127.0.0.1 --port ${GRADIO_INTERNAL_PORT}"

write_conf "/etc/supervisor/conf.d/f5-tts-api.conf" "f5-tts-api" "/opt/supervisor-scripts/f5-tts-api.sh"
write_conf "/etc/supervisor/conf.d/f5-tts-web.conf" "f5-tts-web" "/opt/supervisor-scripts/f5-tts-web.sh"
write_conf "/etc/supervisor/conf.d/f5-tts-gradio.conf" "f5-tts-gradio" "/opt/supervisor-scripts/f5-tts-gradio.sh"

if [[ -f /etc/portal.yaml ]]; then
  python3 - "$API_EXTERNAL_PORT" "$WEB_EXTERNAL_PORT" "$GRADIO_EXTERNAL_PORT" \
    "$API_INTERNAL_PORT" "$WEB_INTERNAL_PORT" "$GRADIO_INTERNAL_PORT" "$REPLACE_TENSORBOARD" <<'PY'
import sys
import yaml

api_ext, web_ext, gradio_ext, api_int, web_int, gradio_int, replace_tensorboard = sys.argv[1:]
with open("/etc/portal.yaml") as f:
    data = yaml.safe_load(f) or {}

apps = data.setdefault("applications", {})
if replace_tensorboard == "1":
    apps.pop("Tensorboard", None)

apps["F5-TTS API"] = {
    "hostname": "localhost",
    "external_port": int(api_ext),
    "internal_port": int(api_int),
    "open_path": "/",
    "name": "F5-TTS API",
}
apps["F5-TTS Web"] = {
    "hostname": "localhost",
    "external_port": int(web_ext),
    "internal_port": int(web_int),
    "open_path": "/",
    "name": "F5-TTS Web",
}
apps["F5-TTS Gradio"] = {
    "hostname": "localhost",
    "external_port": int(gradio_ext),
    "internal_port": int(gradio_int),
    "open_path": "/",
    "name": "F5-TTS Gradio",
}

with open("/etc/portal.yaml", "w") as f:
    yaml.safe_dump(data, f, sort_keys=False)
PY
fi

supervisorctl reread
supervisorctl update
supervisorctl restart f5-tts-api f5-tts-web f5-tts-gradio
if supervisorctl status tensorboard >/dev/null 2>&1 && [[ "$REPLACE_TENSORBOARD" == "1" ]]; then
  supervisorctl stop tensorboard || true
fi
if supervisorctl status caddy >/dev/null 2>&1; then
  supervisorctl restart caddy
fi

supervisorctl status f5-tts-api f5-tts-web f5-tts-gradio caddy || true
