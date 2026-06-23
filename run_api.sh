#!/bin/bash
set -euo pipefail

F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-/workspace/f5-tts}"
F5_TTS_VENV="${F5_TTS_VENV:-/workspace/f5-tts-env}"

source "$F5_TTS_VENV/bin/activate"
exec python3 "$F5_TTS_BASE_DIR/api_server.py"
