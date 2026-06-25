#!/usr/bin/env bash
set -euo pipefail

PROVISION_URL="${F5_TTS_PROVISION_URL:-https://raw.githubusercontent.com/Drynwhyl/f5-tts-pipeline-for-skyrim-net/master/scripts/provision_vast.sh}"

curl -fsSL "$PROVISION_URL" | bash
