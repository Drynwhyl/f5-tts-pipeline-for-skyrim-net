#!/usr/bin/env bash
set -euo pipefail

GITHUB_TOKEN_VALUE="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
GIT_USER_NAME="${GIT_USER_NAME:-Drynwhyl}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-}"
F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-/workspace/f5-tts}"
F5_TTS_REPO_URL="${F5_TTS_REPO_URL:-https://github.com/Drynwhyl/f5-tts-pipeline-for-skyrim-net.git}"

git config --global user.name "$GIT_USER_NAME"
if [[ -n "$GIT_USER_EMAIL" ]]; then
  git config --global user.email "$GIT_USER_EMAIL"
else
  git config --global user.email "${GIT_USER_NAME}@users.noreply.github.com"
fi

if [[ -n "$GITHUB_TOKEN_VALUE" ]]; then
  git config --global credential.helper store
  umask 077
  printf 'https://x-access-token:%s@github.com\n' "$GITHUB_TOKEN_VALUE" > ~/.git-credentials
  echo "Configured GitHub credential helper for github.com."
else
  echo "GITHUB_TOKEN/GH_TOKEN is not set; git push will require external auth."
fi

if [[ -d "$F5_TTS_BASE_DIR/.git" ]]; then
  git -C "$F5_TTS_BASE_DIR" remote set-url origin "$F5_TTS_REPO_URL"
fi
