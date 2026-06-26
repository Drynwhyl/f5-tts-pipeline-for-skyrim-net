# F5-TTS Russian Voice VM

## Vast.ai migration note

This project now runs inside a Vast.ai container. Also read the parent
`/workspace/AGENTS.md` first; it is the Vast instance guide and documents
container limits, storage persistence, supervisor, Caddy, and port mapping.

Current Vast paths and services:

- Repo: `/workspace/f5-tts`
- Python venv: `/workspace/f5-tts-env`
- Model: `/workspace/f5-tts/F5TTS_v1_Base_v2`
- Voices: `/workspace/f5-tts/voices`
- API supervisor service: `f5-tts-api`, internal `http://127.0.0.1:8000`
- Web supervisor service: `f5-tts-web`, internal `http://127.0.0.1:5000`
- Gradio supervisor service: `f5-tts-gradio`, internal `http://127.0.0.1:7860`

The older `~/...`, `systemd`, and NVIDIA L4 notes below describe the original GCP
VM setup. On Vast, use `supervisorctl` and the `/workspace/...` paths above.

Disposable Vast workflow:

- Use Vast `copy` with structured cloud endpoints, not hand-managed
  `rclone.conf`, for model/voice payloads.
- On new instances, the Vast template should set `PROVISIONING_SCRIPT` to
  `scripts/provision_vast.sh`, which clones the repo, configures GitHub auth,
  installs Codex into `/workspace`, restores Codex state, and runs
  `scripts/bootstrap_vast_from_cloudcopy.sh`.
- Before destroying an instance, run `scripts/upload_cloud_payload.sh` after
  pushing code, then run `scripts/upload_codex_state.sh`.
- After meaningful project work, commit and push to GitHub. If model, voices, or
  `config.json` changed, also run `scripts/upload_cloud_payload.sh`.
- Codex state lives under `/workspace/.codex`; use `scripts/upload_codex_state.sh`
  when session continuity matters. Do not assume the Codex app will sync remote
  instance sessions automatically.
- Do not preserve `/workspace/f5-tts-env`, `.cache`, `.hf_home`, or
  `f5-tts-cache`; these are rebuildable.

### Cloud copy verification notes

The Google Drive upload was tested end to end on 2026-06-26:

- `scripts/upload_cloud_payload.sh` successfully uploaded
  `/F5-TTS-Vast/current/f5-tts-data.tar.zst` and its SHA-256 file.
- `scripts/upload_codex_state.sh` successfully uploaded the current Codex
  archive and SHA-256 file under `/F5-TTS-Vast/codex/current/`.
- Wait for `vastai show instance` to report `Cloud Copy Operation Complete`;
  the initial `vastai copy` response only confirms that the transfer was
  queued.
- The local `gdrive:` rclone remote may exist with an empty OAuth token and
  fail with `empty token found`. Do not rely on local rclone for verification.
- A direct `vastai copy drive.<id>:<large-file> C.<instance>:<path>` restore may
  report that it was initiated without actually starting, and can leave a
  stale zero-byte directory entry. It worked for small checksum files but was
  unreliable for the large archive.
- For an end-to-end verification, use `vastai cloud copy` with
  `--transfer "Cloud To Instance"` to restore the cloud directory into a
  temporary path, then run `sha256sum -c` there. Both the 1.2 GiB F5-TTS
  archive and the Codex archive passed this round-trip checksum test.
- Avoid capturing or sharing raw output from `vastai show connections
  --api-key ...`; this CLI version may echo a request URL containing the API
  key.

## Environment

- Python venv: `~/f5-tts-env` — activate before running any python/pip commands
- System packages: ffmpeg, nvidia-driver-550-server (CUDA 13.0 driver)
- GPU: NVIDIA L4 (23GB VRAM)

## Model

- HF model: `Misha24-10/F5-TTS_RUSSIAN` variant `F5TTS_v1_Base_v2`
- Weights: `~/models/f5-tts/F5TTS_v1_Base_v2/model_last_inference.safetensors`
- Vocab: `~/models/f5-tts/F5TTS_v1_Base_v2/vocab.txt`
- License: CC-BY-NC 4.0

## Services (systemd, auto-start on boot)

| Service | Endpoint | What |
|---------|----------|------|
| `f5-tts-api` | `http://localhost:8000` | OpenAI-compatible TTS API (FastAPI) |
| `f5-tts-web` | `http://localhost:5000` | Character Manager Web UI |
| `f5-tts-gradio` | `http://localhost:7860` | Gradio web UI |

```bash
sudo systemctl status|restart|stop|start f5-tts-api
sudo systemctl status|restart|stop|start f5-tts-web
sudo systemctl status|restart|stop|start f5-tts-gradio
journalctl -u f5-tts-api -f   # live logs
journalctl -u f5-tts-web -f   # web UI logs
journalctl -u f5-tts-gradio -f
```

## API server

- Code: `~/models/f5-tts/api_server.py`
- Wraps F5-TTS inference with both OpenAI `/v1/audio/speech` and native XTTS (Coqui) endpoints
- Returns 3 values from `infer_process()`: `(wave_numpy, sample_rate, spectrogram)`
- Reference voices stored in `~/models/f5-tts/voices/<voice_name>/`
- **RUAccent** loaded at startup for automatic stress marking on both ref_text and gen_text
- **Request logging middleware** logs all inbound requests (method, path, headers, body, query, response) — live logs via `journalctl -u f5-tts-api -f`
- **`speed` is `Optional[float]` in `TTSRequest`** (default None). When not provided (SkyrimNet), dynamic speed auto-adjusts. When explicitly set (TTS Preview slider), it takes effect directly.

## Gradio UI

- Wrapper: `~/models/f5-tts/run_gradio.py`
- Patches `cached_path` to load the Russian model instead of default SWivid/F5-TTS
- `cached_path` is imported from the `cached_path` package (not utils_infer)
- Gradio app loads model at import time (module-level), must patch before import

## Stress marks

- `+` before stressed vowel works in both ref_text and gen_text (valid token in vocab)
- ref_text and gen_text are concatenated before tokenization
- RUAccent installed in venv: `from ruaccent import RUAccent; acc = RUAccent(); acc.load(use_dictionary=True)`
- RUAccent model: `models--Den4ikAI--ruaccent` in HuggingFace cache
- **RUAccent auto-applied** in `_tts_infer()` to both gen_text and ref_text (after Whisper transcription)
- **RUAccent auto-applied** in `_save_single_voice()` to ref_text before saving
- **RUAccent `custom_dict`** loaded from `config.json[custom_accent_dict]` at startup via `acc.load(custom_dict=...)`. Updated on `/v1/reload` via `accentor.custom_dict` + `accentor.accents.update()`. Entries here bypass the NN — word must match exactly (case-insensitive lookup).
- **Patched** `~/f5-tts-env/lib/python3.10/site-packages/ruaccent/accent_model.py:35` — onnx models expect `token_type_ids` input but RUAccent doesn't pass it. Added `inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])` before `session.run()`. This fix will be lost on `pip install --upgrade ruaccent`.

## Chunking

- `chunk_text()` in `utils_infer.py:73` splits gen_text into batches for inference
- Split regex: `(?<=[;:.!?])\s+|(?<=[；：。！？])` — only on sentence-ending punctuation (`.` `!` `?` `;` `:`), **not** on commas, em-dashes, or hyphens
- Comma stays in text (token id 12, affects prosody/pausing) but never triggers a batch split
- If a single sentence exceeds max_chars, `chunk_text()` further splits it at commas to keep chunk size reasonable
- Dynamic max_chars per batch (line 404): `int(len(ref_text_bytes) / ref_audio_duration * (22 - ref_audio_duration) * speed)`, capped at `7.0 * ref_bytes_per_sec` to limit each chunk's gen audio to ~7s

## Semantic Chunking (API path)

- Local implementation: `~/models/f5-tts/semantic_infer.py`. This wraps the F5 inference path without modifying Gradio or the upstream package.
- Enabled by `config.json[semantic_chunking.enabled]`. When enabled, `_tts_infer()` calls `infer_process_semantic()`; when disabled, it falls back to upstream `infer_process()`.
- Preview endpoint: `POST /v1/audio/chunks/preview`. The Web UI `/tts-test` calls it before generation and shows the exact chunk plan.
- The chunker estimates Russian speech units instead of raw character length. Stress marks (`+`) are ignored for unit counting, punctuation contributes pause budget, and chunks prefer semantic/prosodic boundaries: sentence ends first, then commas/dashes/colons/semicolons when the generated audio budget would otherwise get too long.
- Boundary selection uses **duration budget**, while generated frame count uses a separate frame target:
  - `estimated_sec = speech_units / ref_units_per_sec + punctuation_pause_sec`
  - `frame_sec = max(2.0, estimated_sec * frame_margin)`
  - `budget_sec = max(2.0, estimated_sec * duration_margin)`
  - `extra_frames = int(frame_sec * target_sample_rate / hop_length)`
- `duration_margin` is only a safety margin for choosing chunk boundaries. Do not use it directly for generated frames; F5-TTS treats frame duration as the real target and fills excess frames with pauses or stretched vowels.
- `frame_margin` is the real generation-duration margin. Current default is `1.10`, chosen as a compromise between avoiding word dropout and avoiding slow, stretched output.
- `target_total_sec` and `hard_total_sec` protect the F5 30s prompt+generation window. `max_gen_budget_sec` prevents one generated chunk from becoming too long and burbling even if the total window technically fits.
- `min_chunk_sec` prevents decorative fragments from being split unnaturally, e.g. vocatives and tails such as `Утгерд, пойдем за мной.` or `Я уже устал ждать, блин!`.
- `comma_softening` keeps short vocative/decorative comma phrases together when possible, so commas still affect prosody but do not always force separate breaths.
- `weak_start_merge_enabled` can merge chunks that start with weak conjunctions such as `хотя`, `что`, `если`, `когда` back into the previous chunk when it fits the hard budget. This avoids unnatural standalone subordinate clauses.
- `ref_guard_enabled` appends runtime-only silence after the processed reference audio. The guard is appended **after** `atempo`, so higher preview speeds do not compress it.
- Guard duration is speed-aware: `effective_ref_guard_silence_ms = min(ref_guard_max_silence_ms, ref_guard_silence_ms + max(0, speed - 1.0) * ref_guard_speed_scale_ms)`. Preview reports the effective guard.
- Runtime ref tail quarantine is **disabled by default**. Do not rely on runtime trimming to fix custom voices: it can create text/audio mismatch when one written sentence spans multiple audio ranges. Keep `ref_tail_quarantine_enabled=false` and `ref_tail_clause_quarantine_enabled=false` unless explicitly debugging an auto-cloned voice.
- Ref cleanup belongs at voice-preparation time. Web UI character pages have `Prepare Reference`, which trims the stored audio, normalizes it, transcribes the resulting audio with Whisper, and rewrites the emotion `.txt` so the user can manually correct it afterward.
- TTS Preview shows a `Runtime Ref` block with the exact conditioning audio, runtime ref text, effective guard, and Whisper transcript. Use this to detect ref text/audio mismatch before changing generation heuristics.
- `generated_trim` trims generated chunk edges after vocoder decode before cross-fade. It trims leading/trailing silence only; it does not remove internal pauses.
- Chunk preview reports both `ref_duration_sec` and `ref_speech_duration_sec`. Guard silence is included in the total F5 window but excluded from speaker tempo estimation.
- Key config block:
  `semantic_chunking.{enabled,target_total_sec,hard_total_sec,max_gen_budget_sec,min_chunk_sec,duration_margin,frame_margin,ref_guard_enabled,ref_guard_silence_ms,ref_guard_speed_scale_ms,ref_guard_max_silence_ms,ref_tail_quarantine_enabled,ref_tail_max_units,ref_tail_min_silence_ms,ref_tail_keep_silence_ms,ref_tail_max_removed_ms,ref_tail_clause_quarantine_enabled,ref_tail_clause_min_speed,ref_tail_clause_max_units,ref_tail_clause_min_remaining_units,ref_tail_clause_max_removed_ms,weak_start_merge_enabled,weak_start_merge_slack_sec,weak_start_words,generated_trim,punctuation,comma_softening}`.

## NFE steps

| nfe_step | RTF vs default | Use case |
|----------|----------------|----------|
| **64 (default)** | **~1.6×** | **best quality, fixes long-sentence burble** |
| 32 | 1.0× | good quality, faster |
| 24 | ~0.77× | modest speedup, minor quality loss |
| **16** | **~0.55×** | **fast, minimal quality loss for short texts** |
| 8 | ~0.32× | fast, noticeable quality drop |

Benchmark (L4, 20s output): nfe=16 → ~2.5s gen, nfe=32 → ~4.5s gen.

Default NFE step stored in `config.json[default_nfe_step]` (default 64).
SkyrimNet requests use this default; TTS Preview slider overrides it.

## Speed (Ref audio atempo approach)

**Key insight:** F5-TTS has no "speak faster" parameter. The `speed` param just reduces `duration` frames passed to the DiT model. The model tries to fit the same text into fewer frames, which causes word dropout.

**Solution:** Instead of reducing frame budget, apply `atempo` (ffmpeg WSOLA time-stretch) to the **reference audio** before feeding it to the model. The model conditions on the fast-paced reference, copying its tempo naturally, while the frame budget stays at speed=1.0 level.

**Flow in `_tts_infer()` (`api_server.py:509`):**
1. `preprocess_ref_audio_text()` as normal (cache-friendly)
2. If `speed > 1.0`: ffmpeg `atempo={speed}` on the processed ref audio
3. `infer_process(ref_audio=atempo_path, speed=1.0)` — uses speed=1.0 frame budget (no dropout)
4. Total generated duration ≈ original_duration / speed (proportional)

**Results (test_speed_suite.py):**
| Voice | Speed=1.0 | Speed=1.5 | Speed=2.0 |
|-------|-----------|-----------|-----------|
| sacred_demoness | 100% all | 100% most | 94-100% ← great |
| sacred_dark_elf | 94-100% | 92-100% | 32-87.5% ← worse, voice-dependent |

- Proportionality is exact: speed=2.0 → exactly 2× shorter audio
- Most voices preserve words perfectly at speed=2.0
- Some voices (sacred_dark_elf) still show dropout — ref audio characteristics matter

## Settings page (Web UI `/config`)

| Section | Config keys | Purpose |
|---------|-------------|---------|
| **Emotion Tags** | `emotion_tag.open / close` | Tag delimiters for LLM emotion parsing |
| **Emotion Aliases** | `emotion_map` | Map LLM tags to core profiles (e.g., `furious → aggressive`) |
| **Generation Defaults** | `default_nfe_step` | Default NFE step for SkyrimNet requests (slider 8–64) |
| **Dynamic Speed** | `dynamic_speed.{enabled, min_rate, min_rate_length, max_rate, max_rate_length}` | Auto-adjust speed based on estimated gen_text duration using ref speaker's pace: `estimated_dur = gen_text_len * ref_dur / ref_text_len`. Linear interpolation between min/max thresholds. |
| **Speed Quality** | `speed_min_frame_ratio` | (Deprecated by atempo approach) Minimum frame budget ratio. Kept for fallback. |
| **Audio Trimming** | `trim_settings.{max_ms, min_silence_len, silence_thresh, keep_silence}` | Trim long ref audio to sentence boundaries via silence detection. Configure thresholds. |
| **Ref Loudness Normalization** | `ref_normalization.{target_dbfs, normalize_on_the_fly}` | Normalize all ref audio to consistent RMS dBFS level. Quieter refs = no clipping, more natural timbre. Applied on save and on-the-fly in float32 before conditioning. |
| **Custom Accent Dict** | `custom_accent_dict` | Override RUAccent stress for specific words (`word: stressed_word`). Bypasses NN, checked before `_process_accent()`. |
| **Text Preprocessing** | `fix_gen_text.{sentence_case, terminal_punctuation}` | Fix proper noun mispronunciation (sentence_case: first letter uppercase per sentence, rest lowercase) and hanging intonation (replace `...`/`,` → `.`, add `.` if missing). Applied in `_tts_infer()` after emotion tag parse, before RUAccent. |
| **Voice Routing** | `narration_voice_override` | Redirect `dlc1seranavoice` → any registered character |
| **Player Voice Filter** | `ignore_player_voice`, `ignored_voice_patterns` | Return silence for player voice TTS requests |

**Bug fix:** `emotion_map` only overwritten when form has `map_core` fields. Partial saves (curl without map_core) preserve existing aliases. `load_config()` in web_ui.py now properly merges defaults with file (was returning file-only config previously).

## Voice caching (VRAM)

- Model (DiT, 337M params) uses ~1.26 GB fp32; Vocos vocoder adds ~0.2 GB. Peak during inference ~4–6 GB. ~20 GB free on L4.
- Each API call re-loads ref audio from disk: `preprocess_ref_audio_text()` (~22ms) + `torchaudio.load()` (~14ms) + resample/norm/GPU transfer (~33ms) = **~60ms overhead per request**.
- A single voice tensor is **~2 MB** (fp32, mono, ~11s at 44.1kHz, ~1 MB after resample to 24kHz).
- 100 cached voices = ~200 MB = 1% of VRAM.
- Caching is not critical (60ms vs 1–2.5s inference), but trivial to implement and costs nothing in resources.

## Character emotional profiles (SkyrimNet custom voices)

- Characters with multiple emotional profiles stored in `~/models/f5-tts/voices/<character>/`
- Each emotion = `<emotion>.wav` + `<emotion>.txt` pair (e.g., `angry.wav` + `angry.txt`)
- `normal` emotion is required (fallback); all others optional
- `_save_single_voice()` saves as `normal.wav` + `normal.txt` (proper emotion format)
- Old format (`audio.wav` + `ref_text.txt`) auto-detected as `normal`-only character via backward compat in `load_voice_registry()`

### Emotion tag parsing

- LLM response tagged with emotion: `[angry] Я же говорил тебе не приходить сюда!`
- API parses tag from gen_text, selects matching ref audio, strips tag before TTS
- Tag delimiters configurable in `~/models/f5-tts/config.json`: `emotion_tag.open` / `emotion_tag.close`
- Default: `[...]` (can be `<...>`, `*...*`, `(...)` etc.)
- First tag wins; multiple tags log warning and first is used
- Emotion aliases map in `config.json.emotion_map`: `{core: [aliases]}` — e.g., `furious` → `aggressive`, `joyful` → `happy`. Built once at startup, O(1) lookup per request. Configurable without code changes.
- Unknown emotion → falls back to `normal` with warning
- No tag → uses `normal` emotion

### API reference

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/v1/audio/speech` | OpenAI TTS with `voice` + emotion tag in `input`. Optional: `seed`, `nfe_step` (default 64) |
| `POST` | `/v1/audio/voice` | Register single ref audio (legacy, creates `normal`-only) |
| `POST` | `/v1/audio/character` | **Bulk upload** character with multiple emotions (multipart) |
| `GET` | `/v1/audio/character/{name}` | Inspect character's emotions |
| `GET` | `/v1/audio/characters` | List all characters with their emotions |
| `DELETE` | `/v1/audio/character/{name}` | Delete a character |
| `POST` | `/v1/reload` | Reload config + voice registry from disk (no model restart needed) |
| `POST` | `/tts_to_audio` or `/tts_to_audio/` | **XTTS-native TTS** (JSON or multipart). Fields: `text`, `speaker_wav` (string or file), `language`, `save_path` |
| `POST` | `/create_and_store_latents` | **XTTS Live Cloning** (JSON or multipart). Fields: `speaker_name`, `speaker_wav` (string or file), `text`, `language` |
| `GET` | `/v1/voices` | List voices (flat array, XTTS format) |
| `POST` | `/v1/voices` | Create voice (multipart: `name`, `files`, `reference_text`) |
| `GET` | `/v1/languages` | Returns `["ru"]` |
| `GET` | `/speakers` | List speakers (flat array) |
| `GET` | `/health` | Health check with device/model/characters info |
| `POST` | `/v1/transcribe` | Whisper transcription (multipart: `audio`, `language`, `model` turbo/large) |
| `POST` | `/v1/audio/transcriptions` | OpenAI-compatible STT (used by SkyrimNet). `file`, `model`, `language` |

### XTTS JSON request formats

**tts_to_audio:**
```json
{"language":"ru","save_path":"output.wav","speaker_wav":"character_name","text":"Текст для озвучки"}
```
- `save_path` saved to `/tmp/f5-tts-cache/` (side effect, response is always WAV bytes)
- `speaker_wav` is a character name string (looked up in voice registry)

**create_and_store_latents:**
```json
{"language":"ru","speaker_name":"new_voice","speaker_wav":"source_voice","text":"референс текст"}
```
- Multipart: `speaker_wav` is an UploadFile (audio file)
- JSON: `speaker_wav` is a string (existing speaker name to clone from)

### Auto-transcription caching

- When ref_text is empty at inference time, Whisper transcribes the audio and `_tts_infer()` saves the result to `{audio_stem}.txt` (e.g., `normal.txt`) via `save_ref_text_to` parameter
- Subsequent calls skip Whisper, using the cached transcription
- RUAccent is applied to the transcription before saving

### CLI: `manage_character.py`

```bash
# Create with specific emotion profiles
python ~/models/f5-tts/manage_character.py create lidia \
  --emotion normal /data/refs/lidia_normal.wav "референс текст" \
  --emotion angry  /data/refs/lidia_angry.wav  "сердитый референс" \
  --emotion happy  /data/refs/lidia_happy.wav  "счастливый референс"

# Import from directory (auto-detects *.wav + *.txt pairs)
python ~/models/f5-tts/manage_character.py import lidia /path/to/lidia_refs/

# List all characters
python ~/models/f5-tts/manage_character.py list

# Show character details
python ~/models/f5-tts/manage_character.py show lidia

# Delete character
python ~/models/f5-tts/manage_character.py delete lidia
```

## Web UI (Character Manager)

- Code: `~/models/f5-tts/web_ui.py`
- Templates: `~/models/f5-tts/templates/`
- Static: `~/models/f5-tts/static/`
- Port 5000, independent FastAPI app (no ML dependencies, instant startup)
- After creating/editing/deleting a character, background thread calls `POST /v1/reload` on the main API to sync its in-memory registry

### Pages

| Route | Page |
|-------|------|
| `/` | Dashboard — card grid of all characters |
| `/character/new` | Create character — check emotions, upload audio + text |
| `/character/{name}` | Character detail — view/edit/delete each emotion |
| `/character/{name}/emotion/new` | Add new emotion profile to existing character |
| `/character/{name}/emotion/{e}/edit` | Replace audio or update text for one emotion |
| `/config` | Settings — tag delimiters, emotion_map aliases, prompt template |
| `/tts-test` | Preview — select character, enter tagged text, seed, NFE steps, speed slider (0.3–2.0), hear result |

### Known web UI bug (fixed)

- `confirmDeleteEmotion()` in `character_detail.html` had a JavaScript quote mismatch that broke the delete button. Fixed: `'Delete the "{{ name }} / ' + emo + ' emotion profile?'`

## Ref Audio Normalization

- F5-TTS copies the loudness of the ref audio — loud refs produce loud output (clipping/timbre loss), quiet refs produce natural output
- `normalize_loudness()` in `audio_utils.py` measures RMS dBFS via pydub and applies `apply_gain(target - current)` to the entire file
- **RMS dBFS** = perceived loudness. Peak dBFS only measures the loudest sample; RMS correlates with how loud it *sounds*. Normalization preserves dynamic range (whisper → shout ratio stays intact).
- **On-disk (batch):** `_normalize_all_voices()` called on startup and `/v1/reload` — scans all WAV/MP3/FLAC in voices/ and `_cloned/`, applies gain, overwrites files. Latency ~1–2ms per file. All existing files at every startup get normalized to the configured target.
- **On-save:** called in `_save_single_voice()`, `_save_cloned_voice()`, bulk character upload, and all web UI save paths — right after `trim_audio_to_sentence_boundary()`.
- **On-the-fly (inference):** `_normalize_audio()` called on `ref_audio_processed` (float32 cache copy) after `preprocess_ref_audio_text()` and before atempo. No quantization loss (float32 → float32). Controlled by `normalize_on_the_fly` toggle in Settings.
- **No quality loss:** gain in float32 has zero quantization error. On-disk 16-bit re-encode adds −96 dBFS noise floor — ~68 dB below speech RMS, inaudible.
- Config: `ref_normalization.{target_dbfs (default -28), normalize_on_the_fly (default true)}`
- Peak headroom: speech RMS ≈ −20 to −32 dBFS, peak ≈ 15 dB above RMS. Applying +4 dB gain (worst case: −32→−28) keeps peaks at ~−13 dBFS — no clipping risk.

## Audio trimming

- Long ref audio truncated at ≤12s via `_trim_audio_to_sentence_boundary()` in `audio_utils.py`
- Uses pydub `silence.split_on_silence()` to find sentence boundaries (no mid-word cuts)
- If no silence gaps found, hard-clips at `max_ms` (configurable)
- Applied in `_save_single_voice()`, `_save_cloned_voice()`, and bulk character upload
- Also applied in web_ui.py on character create/edit forms
- Config: `trim_settings.{max_ms, min_silence_len, silence_thresh, keep_silence}`

## Text Preprocessing

- Applied in `_tts_infer()` after emotion tag parse, before RUAccent `_apply_stress()`
- `_fix_terminal_punctuation()`: `...` at end → `.`, trailing `,`/`;`/`:`/`—` → `.`, missing terminal punctuation → adds `.`
- `_sentence_case()`: split by `[.!?]\s+`, first letter of each sentence uppercase, all other letters lowercase. Fixes proper noun mispronunciation (e.g., `Мясник` → `мясник` — model sees a regular word, not a name)
- Config: `fix_gen_text.{sentence_case (default true), terminal_punctuation (default true)}`
- **Order in `_tts_infer()`:** `gen_text` → emotion tag parse → `_fix_terminal_punctuation` → `_sentence_case` → `_apply_stress` → `infer_process`

## Duration floor patch

- **Patched** `~/f5-tts-env/lib/python3.10/site-packages/f5_tts/infer/utils_infer.py:522` — the duration heuristic `extra_frames = int(ref_audio_len / ref_text_len * gen_text_len / local_speed)` severely underestimates frames for short gen_text (e.g., `"Жураг-Нар. Моя родина."` at 38 bytes gets only 0.6s). Added a 10% safety margin and a 2-second minimum floor:
  ```python
  extra_frames = int(ref_audio_len / ref_text_len * gen_text_len / local_speed * 1.1)
  min_extra_frames = int(2.0 * target_sample_rate / hop_length)  # = 187 frames
  extra_frames = max(extra_frames, min_extra_frames)
  ```
  This fix will be lost on `pip install --upgrade f5-tts`.

## Test suite

- `~/models/f5-tts/test_speed_suite.py` — automated speed × word coverage testing
- Tests 2 voices × 3 speeds (1.0/1.5/2.0) × 5 text lengths
- Generates TTS, transcribes with Whisper turbo, compares word sets
- Outputs HTML report with inline audio players + WAV files
- Usage: `python ~/models/f5-tts/test_speed_suite.py --output /tmp/test`

## Known gotchas

- `pip install f5-tts` pulls `torchcodec` — version 0.14.0 requires CUDA 13 runtime libs (not shipped with torch 2.11.0+cu128 which has only CUDA 12.8). **Must pin `torchcodec==0.13.0`** to avoid `libnvrtc.so.13` errors.
- Audio longer than 12s gets truncated by `preprocess_ref_audio_text()` without matching text truncation. Provide audio <12s to avoid mismatch.
- `torchcodec` cannot save to BytesIO (needs file extension in path). API uses `tempfile.NamedTemporaryFile` as workaround.
- HF token not configured — set `HF_TOKEN` for faster downloads.
- `huggingface-cli` is deprecated; use `hf download` instead.
- **All patches to `utils_infer.py` and `ruaccent/accent_model.py` are lost on `pip install --upgrade`**.
- `api_server.py` has its own imports of `RUAccent` and request logging middleware — reapply if regenerating the file.
- `_save_single_voice` must save as `normal.wav` + `normal.txt` (emotion format), NOT `audio.wav` + `ref_text.txt` (legacy backward compat only).
