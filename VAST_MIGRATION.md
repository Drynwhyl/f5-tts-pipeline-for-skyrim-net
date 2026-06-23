# Vast.ai Migration

## What Lives In `/workspace`

- Repo: `/workspace/f5-tts`
- Venv: `/workspace/f5-tts-env`
- Model: `/workspace/f5-tts/F5TTS_v1_Base_v2`
- Voices: `/workspace/f5-tts/voices`
- Config: `/workspace/f5-tts/config.json`
- RUAccent data: `/workspace/f5-tts/ruaccent-data`
- Runtime cache/logs: `/workspace/f5-tts-cache`, `/workspace/logs`

## First Boot

```bash
cd /workspace
git clone <private-repo-url> f5-tts
cd /workspace/f5-tts

# Optional, if a data archive was uploaded.
export F5_TTS_DATA_ARCHIVE=/workspace/backups/f5-tts-data.tar.zst

./scripts/setup_vast.sh
./scripts/start_services.sh
```

If no data archive is supplied, `setup_vast.sh` downloads
`Misha24-10/F5-TTS_RUSSIAN` files for `F5TTS_v1_Base_v2`. Voices still need to
be restored separately if they are not in the archive.

## Vast Template Defaults

Expose ports `8000`, `5000`, and `7860`.

Useful environment:

```bash
F5_TTS_BASE_DIR=/workspace/f5-tts
F5_TTS_VENV=/workspace/f5-tts-env
HF_HOME=/workspace/.cache/huggingface
XDG_CACHE_HOME=/workspace/.cache
PIP_CACHE_DIR=/workspace/.cache/pip
OPEN_BUTTON_PORT=5000
```

## Backup Before Destroy

```bash
cd /workspace/f5-tts
./scripts/make_migration_archive.sh /workspace/backups
```

Copy both the `.tar.zst` and `.sha256` files off the instance before destroying
it. Vast container storage is not a durable backup by itself.

## Checks

```bash
source /workspace/f5-tts-env/bin/activate
python /workspace/f5-tts/scripts/preflight.py
curl http://localhost:8000/health
curl http://localhost:5000/
```
