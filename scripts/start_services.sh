#!/usr/bin/env bash
set -euo pipefail

export F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-/workspace/f5-tts}"
export F5_TTS_VENV="${F5_TTS_VENV:-/workspace/f5-tts-env}"
export F5_TTS_MODEL_DIR="${F5_TTS_MODEL_DIR:-$F5_TTS_BASE_DIR/F5TTS_v1_Base_v2}"
export F5_TTS_VOICES_DIR="${F5_TTS_VOICES_DIR:-$F5_TTS_BASE_DIR/voices}"
export F5_TTS_CONFIG_PATH="${F5_TTS_CONFIG_PATH:-$F5_TTS_BASE_DIR/config.json}"
export F5_TTS_CACHE_DIR="${F5_TTS_CACHE_DIR:-/workspace/f5-tts-cache}"
export F5_TTS_API_URL="${F5_TTS_API_URL:-http://localhost:8000}"
export F5_TTS_RUACCENT_DIR="${F5_TTS_RUACCENT_DIR:-$F5_TTS_BASE_DIR/ruaccent-data}"
export HF_HOME="${HF_HOME:-/workspace/.cache/huggingface}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/workspace/.cache}"

LOG_DIR="${F5_TTS_LOG_DIR:-/workspace/logs}"
mkdir -p "$LOG_DIR"

source "$F5_TTS_VENV/bin/activate"
cd "$F5_TTS_BASE_DIR"

start_one() {
  local name="$1"
  shift
  if pgrep -f "$*" >/dev/null 2>&1; then
    echo "$name already running"
    return
  fi
  nohup "$@" >>"$LOG_DIR/$name.log" 2>&1 &
  echo "$name pid=$!"
}

start_one f5-tts-api python "$F5_TTS_BASE_DIR/api_server.py"
start_one f5-tts-web python "$F5_TTS_BASE_DIR/web_ui.py"
start_one f5-tts-gradio python "$F5_TTS_BASE_DIR/run_gradio.py" --host 0.0.0.0 --port 7860

echo "logs: $LOG_DIR"
