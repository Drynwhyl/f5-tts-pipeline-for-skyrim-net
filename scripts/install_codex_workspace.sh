#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE:-/workspace}"
CODEX_HOME="${CODEX_HOME:-$WORKSPACE_DIR/.codex}"
BIN_DIR="$WORKSPACE_DIR/bin"
ENV_FILE="$WORKSPACE_DIR/.env"
INSTALL_METHOD="${CODEX_INSTALL_METHOD:-official}"

mkdir -p "$CODEX_HOME" "$BIN_DIR"

ensure_env_line() {
  local line="$1"
  touch "$ENV_FILE"
  grep -Fqx "$line" "$ENV_FILE" || printf '%s\n' "$line" >> "$ENV_FILE"
}

ensure_shell_line_last() {
  local file="$1"
  local line="$2"
  local tmp

  touch "$file"
  tmp="$(mktemp)"
  grep -Fvx "$line" "$file" > "$tmp" || true
  printf '%s\n' "$line" >> "$tmp"
  cat "$tmp" > "$file"
  rm -f "$tmp"
}

if [[ ! -x "$CODEX_HOME/packages/standalone/current/bin/codex" && "$INSTALL_METHOD" == "official" ]]; then
  echo "Installing Codex CLI into CODEX_HOME=$CODEX_HOME using official installer."
  curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_HOME="$CODEX_HOME" sh
fi

if [[ ! -x "$CODEX_HOME/packages/standalone/current/bin/codex" ]]; then
  if [[ -s /opt/nvm/nvm.sh ]]; then
    . /opt/nvm/nvm.sh
  fi
  if ! command -v npm >/dev/null 2>&1; then
    echo "Codex official install did not create a standalone binary, and npm is unavailable." >&2
    exit 1
  fi
  echo "Installing Codex CLI into $WORKSPACE_DIR/codex-npm using npm fallback."
  npm install -g @openai/codex --prefix "$WORKSPACE_DIR/codex-npm"
  ln -sfn "$WORKSPACE_DIR/codex-npm/bin/codex" "$BIN_DIR/codex"
else
  ln -sfn "$CODEX_HOME/packages/standalone/current/bin/codex" "$BIN_DIR/codex"
fi

ensure_env_line 'export PATH="/workspace/bin:$PATH"'
ensure_env_line 'export CODEX_HOME="/workspace/.codex"'
ensure_shell_line_last "$HOME/.profile" 'export PATH="/workspace/bin:$PATH"'
ensure_shell_line_last "$HOME/.profile" 'export CODEX_HOME="/workspace/.codex"'
ensure_shell_line_last "$HOME/.bashrc" 'export PATH="/workspace/bin:$PATH"'
ensure_shell_line_last "$HOME/.bashrc" 'export CODEX_HOME="/workspace/.codex"'

"$BIN_DIR/codex" --version
echo "Codex CLI is available at $BIN_DIR/codex"
