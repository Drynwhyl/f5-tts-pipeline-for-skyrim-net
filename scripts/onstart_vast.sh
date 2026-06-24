#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE:-/workspace}"
F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-$WORKSPACE_DIR/f5-tts}"
F5_TTS_REPO_URL="${F5_TTS_REPO_URL:-https://github.com/Drynwhyl/f5-tts-pipeline-for-skyrim-net.git}"
GITHUB_TOKEN_VALUE="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

clone_url="$F5_TTS_REPO_URL"
if [[ -n "$GITHUB_TOKEN_VALUE" && "$clone_url" =~ ^https://github.com/(.*)$ ]]; then
  clone_url="https://x-access-token:${GITHUB_TOKEN_VALUE}@github.com/${BASH_REMATCH[1]}"
fi

mkdir -p "$WORKSPACE_DIR"
cd "$WORKSPACE_DIR"

if [[ ! -d "$F5_TTS_BASE_DIR/.git" ]]; then
  git clone "$clone_url" "$F5_TTS_BASE_DIR"
else
  git -C "$F5_TTS_BASE_DIR" pull --ff-only
fi

git -C "$F5_TTS_BASE_DIR" remote set-url origin "$F5_TTS_REPO_URL"

cd "$F5_TTS_BASE_DIR"
bash scripts/setup_github_auth.sh

if [[ "${INSTALL_CODEX:-1}" == "1" ]]; then
  bash scripts/install_codex_workspace.sh
fi

bash scripts/bootstrap_vast_from_cloudcopy.sh
