#!/usr/bin/env bash
set -euo pipefail

export F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-/workspace/f5-tts}"
export F5_TTS_VENV="${F5_TTS_VENV:-/workspace/f5-tts-env}"
export F5_TTS_MODEL_DIR="${F5_TTS_MODEL_DIR:-$F5_TTS_BASE_DIR/F5TTS_v1_Base_v2}"
export F5_TTS_VOICES_DIR="${F5_TTS_VOICES_DIR:-$F5_TTS_BASE_DIR/voices}"
export F5_TTS_CONFIG_PATH="${F5_TTS_CONFIG_PATH:-$F5_TTS_BASE_DIR/config.json}"
export F5_TTS_CACHE_DIR="${F5_TTS_CACHE_DIR:-/workspace/f5-tts-cache}"
export F5_TTS_RUACCENT_DIR="${F5_TTS_RUACCENT_DIR:-$F5_TTS_BASE_DIR/ruaccent-data}"
export HF_HOME="${HF_HOME:-/workspace/.cache/huggingface}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/workspace/.cache}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-/workspace/.cache/pip}"
F5_TTS_MODEL_REPO="${F5_TTS_MODEL_REPO:-Misha24-10/F5-TTS_RUSSIAN}"

if [[ ! -d "$F5_TTS_BASE_DIR" ]]; then
  echo "Expected repo at $F5_TTS_BASE_DIR" >&2
  exit 1
fi

mkdir -p "$F5_TTS_MODEL_DIR" "$F5_TTS_VOICES_DIR" "$F5_TTS_CACHE_DIR" "$F5_TTS_RUACCENT_DIR" "$HF_HOME" "$PIP_CACHE_DIR" /workspace/logs /workspace/backups

if command -v apt-get >/dev/null 2>&1 && [[ "$(id -u)" == "0" ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends ffmpeg git python3 python3-venv python3-pip libsndfile1 ca-certificates curl zstd
elif ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is missing and apt-get cannot run as root in this container." >&2
  exit 1
fi

if [[ ! -x "$F5_TTS_VENV/bin/python" ]]; then
  python3 -m venv "$F5_TTS_VENV"
fi

source "$F5_TTS_VENV/bin/activate"
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r "$F5_TTS_BASE_DIR/requirements-vast.txt" -c "$F5_TTS_BASE_DIR/constraints-vast.txt"
python "$F5_TTS_BASE_DIR/scripts/apply_runtime_patches.py"

if [[ -n "${F5_TTS_DATA_ARCHIVE:-}" ]]; then
  tar --zstd -xf "$F5_TTS_DATA_ARCHIVE" -C "$F5_TTS_BASE_DIR"
fi

if [[ ! -f "$F5_TTS_MODEL_DIR/model_last_inference.safetensors" || ! -f "$F5_TTS_MODEL_DIR/vocab.txt" ]]; then
  hf download "$F5_TTS_MODEL_REPO" \
    F5TTS_v1_Base_v2/model_last_inference.safetensors \
    F5TTS_v1_Base_v2/vocab.txt \
    --local-dir "$F5_TTS_BASE_DIR"
fi

python "$F5_TTS_BASE_DIR/scripts/preflight.py" --skip-gpu-load
