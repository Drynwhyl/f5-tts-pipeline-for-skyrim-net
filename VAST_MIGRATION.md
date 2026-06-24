# Vast.ai Disposable Workflow

This project is designed to run on disposable Vast.ai instances.

- Code lives in GitHub.
- Model, voices, and `config.json` live in a Google Drive folder connected to
  Vast Cloud Copy.
- The Vast instance is compute, not durable storage.
- Do not sync all of `/workspace`; rebuild venv/cache on each cold start.

## Persistent Payload

Cloud layout:

```text
/F5-TTS-Vast/current/f5-tts-data.tar.zst
/F5-TTS-Vast/current/f5-tts-data.tar.zst.sha256
/F5-TTS-Vast/backups/<timestamp>/f5-tts-data.tar.zst
/F5-TTS-Vast/backups/<timestamp>/f5-tts-data.tar.zst.sha256
```

The payload archive contains:

```text
F5TTS_v1_Base_v2/
voices/
config.json
```

It intentionally excludes:

```text
/workspace/f5-tts-env
/workspace/.cache
/workspace/.hf_home
/workspace/f5-tts-cache
```

## Vast Template

Use a separate Google Drive account/folder for Vast Cloud Copy, then create a
scoped Vast API key for the template.

Required API key permission groups:

```json
{
  "api": {
    "misc": {},
    "user_read": {},
    "instance_read": {},
    "instance_write": {}
  }
}
```

Template environment:

```bash
VAST_API_KEY=<scoped Vast API key>
VAST_CLOUD_CONNECTION_ID=<numeric Google Drive connection id from vastai show connections>
F5_TTS_REPO_URL=https://github.com/Drynwhyl/f5-tts-pipeline-for-skyrim-net.git
F5_TTS_CLOUD_SRC=/F5-TTS-Vast/current/
F5_TTS_CLOUD_DST=/F5-TTS-Vast/current/
F5_TTS_BASE_DIR=/workspace/f5-tts
F5_TTS_VENV=/workspace/f5-tts-env
```

Expose these ports when creating the template:

```text
22     SSH
1111   Vast portal
8080   Jupyter / terminal
6006   F5-TTS Gradio via Caddy
10100  F5-TTS API via Caddy
10200  F5-TTS Web via Caddy
```

Recommended onstart command:

```bash
bash -lc 'set -euo pipefail
cd /workspace
if [ ! -d f5-tts/.git ]; then
  git clone "${F5_TTS_REPO_URL:-https://github.com/Drynwhyl/f5-tts-pipeline-for-skyrim-net.git}" f5-tts
else
  git -C f5-tts pull --ff-only
fi
cd /workspace/f5-tts
./scripts/bootstrap_vast_from_cloudcopy.sh
'
```

The bootstrap script runs `vastai cloud copy` from inside the new instance,
waits for `/workspace/migration/f5-tts-data.tar.zst`, verifies the checksum,
builds the venv, applies runtime patches, and installs supervisor/Caddy services.

## First Cloud Upload

On a configured instance with the latest model/voices:

```bash
cd /workspace/f5-tts
./scripts/prepare_cloud_payload.sh
```

Then upload the stable payload using the helper:

```bash
export VAST_API_KEY=<scoped Vast API key>
export VAST_CLOUD_CONNECTION_ID=<numeric Google Drive connection id>
./scripts/upload_cloud_payload.sh
```

The helper prepares `/workspace/cloudsync/current/` and requests:

```bash
vastai cloud copy \
  --src /workspace/cloudsync/current/ \
  --dst /F5-TTS-Vast/current/ \
  --instance <this instance id> \
  --connection <connection id> \
  --transfer "Instance To Cloud"
```

For a first run, test the same command with `--dry-run` before uploading the
large archive:

```bash
F5_TTS_CLOUD_COPY_DRY_RUN=1 ./scripts/upload_cloud_payload.sh
```

## Fresh Instance Restore

If the template onstart is configured, no manual restore is needed. Rent the
instance and wait for bootstrap to complete.

Check progress:

```bash
cat /workspace/migration/bootstrap-status.md
tail -f /var/log/portal/f5-tts-api.log
supervisorctl status f5-tts-api f5-tts-web f5-tts-gradio caddy
```

Health checks:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:5000/
curl http://127.0.0.1:7860/
```

## Backup Before Destroy

Before destroying a working instance:

```bash
cd /workspace/f5-tts
git status --short --branch
git push origin master
export VAST_API_KEY=<scoped Vast API key>
export VAST_CLOUD_CONNECTION_ID=<numeric Google Drive connection id>
./scripts/upload_cloud_payload.sh
```

Destroy only after the Cloud Copy upload is complete and the code is pushed.

## Cancel / Retry

Cancel a stuck restore into an instance:

```bash
vastai cancel copy <INSTANCE_ID>:/workspace/migration/
```

For stuck uploads to cloud storage, cancel from the Vast Cloud Copy UI. Then
rerun the same bootstrap or upload command. If direction behavior looks
ambiguous, run `vastai cloud copy ... --dry-run` first.
