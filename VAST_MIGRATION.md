# Vast.ai Disposable Workflow

This project is designed to run on disposable Vast.ai instances.

- Code lives in GitHub.
- Model, voices, and `config.json` live in a Google Drive folder connected to
  Vast cloud storage.
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

Use a separate Google Drive account/folder for Vast cloud storage, then create a
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
GITHUB_TOKEN=<fine-grained GitHub token with Contents read/write for this repo>
GIT_USER_NAME=Drynwhyl
GIT_USER_EMAIL=<optional Git email>
INSTALL_CODEX=1
CODEX_HOME=/workspace/.codex
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
bash -lc 'curl -fsSL https://raw.githubusercontent.com/Drynwhyl/f5-tts-pipeline-for-skyrim-net/master/scripts/onstart_vast.sh | bash'
```

The onstart script clones/pulls the repo, configures GitHub push credentials when
`GITHUB_TOKEN` is set, installs Codex into `/workspace`, then runs
`bootstrap_vast_from_cloudcopy.sh`. The bootstrap script runs `vastai copy` from
inside the new instance, waits for `/workspace/migration/f5-tts-data.tar.zst`,
verifies the checksum, builds the venv, applies runtime patches, and installs
supervisor/Caddy services.

If the repo ever becomes private, the raw GitHub onstart URL will also need
authentication. In that case paste the contents of `scripts/onstart_vast.sh`
directly into the Vast template onstart field, or host a private bootstrap script
somewhere the instance can read with a token.

## GitHub Push From The Instance

Create a fine-grained GitHub token scoped to this repository with:

```text
Contents: Read and write
Metadata: Read
```

Add it to the Vast template environment as `GITHUB_TOKEN`. The token is consumed
by `scripts/setup_github_auth.sh`, which writes a local `~/.git-credentials` entry
inside the disposable container and keeps `origin` as the clean public HTTPS URL.

Manual reconfiguration:

```bash
cd /workspace/f5-tts
source /workspace/.env
./scripts/setup_github_auth.sh
git push origin master
```

Do not commit `.env`, `~/.git-credentials`, or any token value.

## Codex On Vast

The template uses `INSTALL_CODEX=1` by default. Codex is installed into:

```text
/workspace/.codex
/workspace/bin/codex
```

`/workspace/.env` gets:

```bash
export PATH="/workspace/bin:$PATH"
export CODEX_HOME="/workspace/.codex"
```

This avoids installing Codex only into a transient home-local location. To install
or repair it manually:

```bash
cd /workspace/f5-tts
./scripts/install_codex_workspace.sh
```

Codex session persistence between different remote instances is not assumed.
Treat `/workspace/.codex` as local state. Backup session/log state when useful:

```bash
cd /workspace/f5-tts
./scripts/backup_codex_state.sh
```

By default `auth.json` is excluded from the Codex backup. Include it only for
private, trusted storage:

```bash
CODEX_BACKUP_INCLUDE_AUTH=1 ./scripts/backup_codex_state.sh
```

Optional Vast copy upload for Codex session backup:

```bash
vastai copy \
  C.<this instance id>:/workspace/cloudsync/codex/current/ \
  drive.<connection id>:/F5-TTS-Vast/codex/current/
```

Optional restore on a future instance:

```bash
vastai copy \
  drive.<connection id>:/F5-TTS-Vast/codex/current/codex-state.tar.zst \
  C.<new instance id>:/workspace/cloudsync/codex/current/codex-state.tar.zst
vastai copy \
  drive.<connection id>:/F5-TTS-Vast/codex/current/codex-state.tar.zst.sha256 \
  C.<new instance id>:/workspace/cloudsync/codex/current/codex-state.tar.zst.sha256

cd /workspace/f5-tts
./scripts/restore_codex_state.sh
```

Vast treats file destinations as directories for this copy mode, so restore may
create `codex-state.tar.zst/codex-state.tar.zst`. Move the nested file up before
running `restore_codex_state.sh` if doing this manually.

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
vastai copy \
  C.<this instance id>:/workspace/cloudsync/current/ \
  drive.<connection id>:/F5-TTS-Vast/current/
```

For a first run, test the local command construction before uploading the large
archive. This intentionally does not call Vast API:

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
./scripts/backup_codex_state.sh   # optional, for Codex session continuity
```

Destroy only after the cloud upload is complete and the code is pushed.

For a meaningful code/config change during normal work, the default close-out is:

```bash
cd /workspace/f5-tts
git status --short --branch
git add <changed files>
git commit -m "<short useful summary>"
git push origin master
```

Run `./scripts/upload_cloud_payload.sh` too when model, voices, `config.json`, or
other payload state changed.

## Cancel / Retry

Cancel a stuck restore into an instance:

```bash
vastai cancel copy C.<INSTANCE_ID>
```

For stuck uploads to cloud storage, cancel from the Vast Cloud Copy UI or try:

```bash
vastai cancel copy drive.<connection id>
```

Then rerun the same bootstrap or upload command. Prefer `vastai copy` with
structured locations (`C.<instance>:/path`, `drive.<connection>:/path`) over
`vastai cloud copy`; the latter accepted requests but produced empty transfers in
testing on 2026-06-24.
