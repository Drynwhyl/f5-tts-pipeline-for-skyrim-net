import os
import re
import json
import base64
import random
import shutil
import logging
import threading
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("f5-tts-web")

from audio_utils import trim_audio_to_sentence_boundary, normalize_loudness, DEFAULT_MAX_MS, DEFAULT_MIN_SILENCE_LEN, DEFAULT_SILENCE_THRESH, DEFAULT_KEEP_SILENCE, DEFAULT_NORMALIZATION_TARGET, DEFAULT_NORMALIZE_ON_FLY, DEFAULT_SPECTRAL_PENALTY
from semantic_infer import DEFAULT_SEMANTIC_CHUNKING, merge_semantic_config

BASE_DIR = Path("/home/drynw/models/f5-tts")
VOICES_DIR = BASE_DIR / "voices"
CONFIG_PATH = BASE_DIR / "config.json"
API_URL = "http://localhost:8000"
CORE_EMOTIONS = ["normal", "calm", "happy", "sad", "aggressive", "scared"]
CLONED_VOICES_DIR_NAME = "_cloned"

app = FastAPI(title="F5-TTS Character Manager")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
os.makedirs(VOICES_DIR, exist_ok=True)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _reload_api():
    for attempt in range(3):
        try:
            with httpx.Client(timeout=5) as client:
                r = client.post(f"{API_URL}/v1/reload")
                if r.status_code == 200:
                    return
                logger.warning(f"Reload attempt {attempt+1}: HTTP {r.status_code}")
        except Exception as e:
            logger.warning(f"Reload attempt {attempt+1} failed: {e}")
    logger.error("Failed to reload API after 3 attempts")


def load_config() -> dict:
    default = {
        "emotion_tag": {"open": "[", "close": "]"},
        "emotion_map": {},
        "ignore_player_voice": True,
        "ignored_voice_patterns": ["player voice", "player_voice"],
        "cloned_voices_dir": "_cloned",
        "trim_settings": {
            "max_ms": 12000,
            "min_silence_len": 1000,
            "silence_thresh": -50,
            "keep_silence": 500,
        },
        "default_nfe_step": 64,
        "narration_voice_override": "",
        "dynamic_speed": {
            "enabled": False,
            "min_rate": 1.0,
            "min_rate_length": 3.0,
            "max_rate": 2.0,
            "max_rate_length": 15.0,
        },
        "voice_overrides": {},
        "ref_normalization": {
            "target_dbfs": DEFAULT_NORMALIZATION_TARGET,
            "normalize_on_the_fly": DEFAULT_NORMALIZE_ON_FLY,
            "spectral_penalty": DEFAULT_SPECTRAL_PENALTY,
        },
        "custom_accent_dict": {},
        "fix_gen_text": {
            "sentence_case": True,
            "terminal_punctuation": True,
        },
        "semantic_chunking": DEFAULT_SEMANTIC_CHUNKING,
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            merged = default.copy()
            merged.update(cfg)
            merged["semantic_chunking"] = merge_semantic_config(cfg.get("semantic_chunking", {}))
            return merged
        except Exception:
            pass
    return default


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _get_trim_settings() -> dict:
    """Read trim settings from config, falling back to defaults."""
    ts = load_config().get("trim_settings", {})
    return {
        "max_ms": ts.get("max_ms", 12000),
        "min_silence_len": ts.get("min_silence_len", 1000),
        "silence_thresh": ts.get("silence_thresh", -50),
        "keep_silence": ts.get("keep_silence", 500),
    }


def _normalize_audio_wrapper(audio_path: str):
    """Normalize a single audio file per config settings."""
    cfg = load_config()
    norm = cfg.get("ref_normalization", {}) or {}
    target = float(norm.get("target_dbfs", DEFAULT_NORMALIZATION_TARGET))
    sp = bool(norm.get("spectral_penalty", DEFAULT_SPECTRAL_PENALTY))
    msg = normalize_loudness(audio_path, target_dbfs=target, spectral_penalty=sp)
    if msg:
        logger.info("Normalized '%s': %s", audio_path, msg)


def get_characters(type_filter: str = "all") -> list[dict]:
    os.makedirs(VOICES_DIR, exist_ok=True)
    chars = []

    dirs_to_scan = [VOICES_DIR]
    if type_filter in ("all", "cloned"):
        cloned = VOICES_DIR / CLONED_VOICES_DIR_NAME
        if cloned.is_dir() and dirs_to_scan[0] != cloned:
            dirs_to_scan.append(cloned)
    if type_filter == "cloned":
        dirs_to_scan = [VOICES_DIR / CLONED_VOICES_DIR_NAME]

    for base_dir in dirs_to_scan:
        if not base_dir.is_dir():
            continue
        for d in sorted(base_dir.iterdir()):
            if not d.is_dir():
                continue
            if type_filter == "premade" and base_dir.name == CLONED_VOICES_DIR_NAME:
                continue
            if d.name.startswith("_"):
                continue
            emotions = set()
            for f in d.iterdir():
                if f.suffix in {".wav", ".mp3", ".flac"}:
                    emotions.add(f.stem)
            if emotions:
                chars.append({
                    "name": d.name,
                    "emotions": sorted(emotions),
                    "type": "cloned" if base_dir.name == CLONED_VOICES_DIR_NAME else "premade",
                })
    return chars


AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac"}
AUDIO_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac"}


def find_audio(char_dir: Path, emotion: str) -> Path | None:
    for ext in AUDIO_EXTENSIONS:
        p = char_dir / f"{emotion}{ext}"
        if p.exists():
            return p
    return None


def _find_char_dir(name: str) -> Path | None:
    for base in (VOICES_DIR, VOICES_DIR / CLONED_VOICES_DIR_NAME):
        d = base / name
        if d.is_dir():
            return d
    return None


def get_character_emotions(name: str) -> list[dict]:
    for base in (VOICES_DIR, VOICES_DIR / CLONED_VOICES_DIR_NAME):
        char_dir = base / name
        if char_dir.is_dir():
            result = []
            for f in sorted(char_dir.iterdir()):
                if f.suffix not in AUDIO_EXTENSIONS:
                    continue
                stem = f.stem
                text_file = char_dir / f"{stem}.txt"
                ref_text = text_file.read_text().strip() if text_file.exists() else ""
                result.append({"name": stem, "text": ref_text, "audio_url": f"/audio/{name}/{stem}"})
            return result
    return []


@app.get("/audio/{character}/{emotion}")
def serve_audio(character: str, emotion: str):
    for base in (VOICES_DIR, VOICES_DIR / CLONED_VOICES_DIR_NAME):
        char_dir = base / character
        audio_path = find_audio(char_dir, emotion)
        if audio_path:
            mime = AUDIO_MIME.get(audio_path.suffix, "audio/wav")
            return FileResponse(str(audio_path), media_type=mime)
    raise HTTPException(status_code=404, detail="Audio not found")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    all_chars = get_characters("all")
    premade = [c for c in all_chars if c["type"] == "premade"]
    cloned = [c for c in all_chars if c["type"] == "cloned"]
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "characters": premade, "cloned_characters": cloned, "message": None},
    )


@app.get("/character/new", response_class=HTMLResponse)
def new_character_form(request: Request):
    return templates.TemplateResponse(
        "character_new.html",
        {"request": request, "core_emotions": CORE_EMOTIONS, "message": None},
    )


@app.post("/character/new")
async def create_character(request: Request):
    form = await request.form()
    name = form.get("name", "").strip()

    if not name or not re.match(r"^[a-zA-Z0-9_-]+$", name):
        return templates.TemplateResponse(
            "character_new.html", {
                "request": request, "core_emotions": CORE_EMOTIONS,
                "message": {"type": "error", "text": "Invalid name. Use letters, numbers, hyphens, underscores."},
            },
        )

    emotions = form.getlist("emotions")
    if "normal" not in emotions:
        return templates.TemplateResponse(
            "character_new.html", {
                "request": request, "core_emotions": CORE_EMOTIONS,
                "message": {"type": "error", "text": "'normal' emotion is required."},
            },
        )

    char_dir = VOICES_DIR / name
    if char_dir.exists():
        return templates.TemplateResponse(
            "character_new.html", {
                "request": request, "core_emotions": CORE_EMOTIONS,
                "message": {"type": "error", "text": f"Character '{name}' already exists."},
            },
        )

    char_dir.mkdir(parents=True)
    saved = 0

    trim_notice = ""
    for emotion in emotions:
        audio_file = form.get(f"audio_{emotion}")
        if not audio_file or not hasattr(audio_file, "filename") or not audio_file.filename:
            continue
        ext = Path(audio_file.filename).suffix or ".wav"
        content = await audio_file.read()
        audio_path = char_dir / f"{emotion}{ext}"
        with open(audio_path, "wb") as f:
            f.write(content)
        trim_msg = trim_audio_to_sentence_boundary(str(audio_path), **_get_trim_settings())
        if trim_msg:
            trim_notice += f"{emotion}: {trim_msg}\n"
        _normalize_audio_wrapper(str(audio_path))
        ref_text = form.get(f"text_{emotion}", "").strip()
        (char_dir / f"{emotion}.txt").write_text(ref_text)
        saved += 1

    if saved == 0:
        shutil.rmtree(char_dir)
        return templates.TemplateResponse(
            "character_new.html", {
                "request": request, "core_emotions": CORE_EMOTIONS,
                "message": {"type": "error", "text": "No audio files uploaded."},
            },
        )

    if trim_notice:
        logger.info("Trim notice for '%s':\n%s", name, trim_notice.strip())

    threading.Thread(target=_reload_api, daemon=True).start()
    return RedirectResponse(url=f"/character/{name}", status_code=303)


@app.get("/character/{name}", response_class=HTMLResponse)
def character_detail(request: Request, name: str):
    emotions = get_character_emotions(name)
    if not emotions:
        return templates.TemplateResponse(
            "index.html", {
                "request": request, "characters": get_characters(),
                "message": {"type": "error", "text": f"Character '{name}' not found."},
            },
        )
    return templates.TemplateResponse(
        "character_detail.html",
        {"request": request, "name": name, "emotions": emotions, "message": None},
    )


@app.post("/character/{name}/delete")
def delete_character(name: str):
    for base in (VOICES_DIR, VOICES_DIR / CLONED_VOICES_DIR_NAME):
        char_dir = base / name
        if char_dir.exists():
            shutil.rmtree(char_dir)
    threading.Thread(target=_reload_api, daemon=True).start()
    return RedirectResponse(url="/", status_code=303)


@app.get("/character/{name}/emotion/{emotion}/edit", response_class=HTMLResponse)
def edit_emotion_form(request: Request, name: str, emotion: str):
    emotions = get_character_emotions(name)
    data = next((e for e in emotions if e["name"] == emotion), None)
    if not data:
        return RedirectResponse(url=f"/character/{name}", status_code=303)
    return templates.TemplateResponse(
        "emotion_edit.html",
        {"request": request, "character": name, "emotion": emotion,
         "text": data["text"], "message": None},
    )


@app.post("/character/{name}/emotion/{emotion}/edit")
async def edit_emotion_submit(request: Request, name: str, emotion: str):
    form = await request.form()
    char_dir = _find_char_dir(name)
    if not char_dir:
        return RedirectResponse(url="/", status_code=303)

    audio_file = form.get("audio")
    if audio_file and hasattr(audio_file, "filename") and audio_file.filename:
        for old in char_dir.glob(f"{emotion}.*"):
            if old.suffix in {".wav", ".mp3", ".flac"}:
                old.unlink()
                break
        ext = Path(audio_file.filename).suffix or ".wav"
        audio_path = char_dir / f"{emotion}{ext}"
        content = await audio_file.read()
        with open(audio_path, "wb") as f:
            f.write(content)
        trim_msg = trim_audio_to_sentence_boundary(str(audio_path), **_get_trim_settings())
        if trim_msg:
            logger.info("Edit trim for '%s/%s': %s", name, emotion, trim_msg)
        _normalize_audio_wrapper(str(audio_path))

    ref_text = form.get("text", "").strip()
    (char_dir / f"{emotion}.txt").write_text(ref_text)

    threading.Thread(target=_reload_api, daemon=True).start()
    return RedirectResponse(url=f"/character/{name}", status_code=303)


@app.post("/character/{name}/emotion/{emotion}/delete")
def delete_emotion(name: str, emotion: str):
    char_dir = _find_char_dir(name)
    if char_dir:
        for f in char_dir.iterdir():
            if f.stem == emotion:
                f.unlink()
    threading.Thread(target=_reload_api, daemon=True).start()
    return RedirectResponse(url=f"/character/{name}", status_code=303)


@app.get("/character/{name}/emotion/new", response_class=HTMLResponse)
def add_emotion_form(request: Request, name: str):
    char_dir = _find_char_dir(name)
    if not char_dir:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        "emotion_add.html",
        {"request": request, "character": name, "core_emotions": CORE_EMOTIONS, "message": None},
    )


@app.post("/character/{name}/emotion/new")
async def add_emotion_submit(request: Request, name: str):
    form = await request.form()
    char_dir = _find_char_dir(name)
    if not char_dir:
        return RedirectResponse(url="/", status_code=303)

    emotion = form.get("emotion", "").strip().lower()
    if not emotion or not re.match(r"^[a-zA-Z0-9_-]+$", emotion):
        return templates.TemplateResponse(
            "emotion_add.html", {
                "request": request, "character": name, "core_emotions": CORE_EMOTIONS,
                "message": {"type": "error", "text": "Invalid emotion name. Use letters, numbers, hyphens, underscores."},
            },
        )

    audio_file = form.get("audio")
    if not audio_file or not hasattr(audio_file, "filename") or not audio_file.filename:
        return templates.TemplateResponse(
            "emotion_add.html", {
                "request": request, "character": name, "core_emotions": CORE_EMOTIONS,
                "message": {"type": "error", "text": "Audio file is required."},
            },
        )

    ext = Path(audio_file.filename).suffix or ".wav"
    audio_path = char_dir / f"{emotion}{ext}"
    content = await audio_file.read()
    with open(audio_path, "wb") as f:
        f.write(content)
    trim_msg = trim_audio_to_sentence_boundary(str(audio_path), **_get_trim_settings())
    if trim_msg:
        logger.info("Add emotion trim for '%s/%s': %s", name, emotion, trim_msg)
    _normalize_audio_wrapper(str(audio_path))

    ref_text = form.get("text", "").strip()
    (char_dir / f"{emotion}.txt").write_text(ref_text)

    threading.Thread(target=_reload_api, daemon=True).start()
    return RedirectResponse(url=f"/character/{name}", status_code=303)


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    return templates.TemplateResponse(
        "config.html",
        {"request": request, "config": load_config(), "message": None},
    )


@app.post("/config")
async def config_save(request: Request):
    form = await request.form()
    cfg = load_config()

    cfg["emotion_tag"] = {
        "open": form.get("tag_open", "[").strip(),
        "close": form.get("tag_close", "]").strip(),
    }

    cores = form.getlist("map_core")
    if cores and cores[0]:
        aliases_list = form.getlist("map_aliases")
        emotion_map = {}
        for core, aliases_str in zip(cores, aliases_list):
            core = core.strip().lower()
            if not core:
                continue
            aliases = [a.strip().lower() for a in aliases_str.split(",") if a.strip()]
            if aliases:
                emotion_map[core] = aliases
        cfg["emotion_map"] = emotion_map

    cfg["ignore_player_voice"] = form.get("ignore_player_voice") == "1"
    raw_patterns = form.get("ignored_voice_patterns", "").strip()
    cfg["ignored_voice_patterns"] = [p.strip() for p in raw_patterns.split(",") if p.strip()]

    cfg["trim_settings"] = {
        "max_ms": int(form.get("trim_max_ms", 12000)),
        "min_silence_len": int(form.get("trim_min_silence_len", 1000)),
        "silence_thresh": int(form.get("trim_silence_thresh", -50)),
        "keep_silence": int(form.get("trim_keep_silence", 500)),
    }

    cfg["default_nfe_step"] = int(form.get("default_nfe_step", 64))
    cfg["narration_voice_override"] = form.get("narration_voice_override", "").strip()
    cfg["dynamic_speed"] = {
        "enabled": form.get("dynamic_speed_enabled") == "1",
        "min_rate": float(form.get("ds_min_rate", 1.0)),
        "min_rate_length": float(form.get("ds_min_rate_length", 3.0)),
        "max_rate": float(form.get("ds_max_rate", 1.4)),
        "max_rate_length": float(form.get("ds_max_rate_length", 15.0)),
    }
    tails_raw = form.get("semantic_decorative_tails", "").strip()
    weak_start_words_raw = form.get("semantic_weak_start_words", "").strip()
    cfg["semantic_chunking"] = {
        "enabled": form.get("semantic_enabled") == "1",
        "target_total_sec": float(form.get("semantic_target_total_sec", 26.0)),
        "hard_total_sec": float(form.get("semantic_hard_total_sec", 29.0)),
        "max_gen_budget_sec": float(form.get("semantic_max_gen_budget_sec", 10.0)),
        "min_chunk_sec": float(form.get("semantic_min_chunk_sec", 1.4)),
        "duration_margin": float(form.get("semantic_duration_margin", 1.15)),
        "frame_margin": float(form.get("semantic_frame_margin", 1.10)),
        "ref_guard_enabled": form.get("semantic_ref_guard_enabled") == "1",
        "ref_guard_silence_ms": int(form.get("semantic_ref_guard_silence_ms", 300)),
        "ref_guard_speed_scale_ms": int(form.get("semantic_ref_guard_speed_scale_ms", 500)),
        "ref_guard_max_silence_ms": int(form.get("semantic_ref_guard_max_silence_ms", 900)),
        "ref_tail_quarantine_enabled": form.get("semantic_ref_tail_quarantine_enabled") == "1",
        "ref_tail_max_units": int(form.get("semantic_ref_tail_max_units", 40)),
        "ref_tail_min_silence_ms": int(form.get("semantic_ref_tail_min_silence_ms", 150)),
        "ref_tail_keep_silence_ms": int(form.get("semantic_ref_tail_keep_silence_ms", 200)),
        "ref_tail_max_removed_ms": int(form.get("semantic_ref_tail_max_removed_ms", 4500)),
        "ref_tail_clause_quarantine_enabled": form.get("semantic_ref_tail_clause_quarantine_enabled") == "1",
        "ref_tail_clause_min_speed": float(form.get("semantic_ref_tail_clause_min_speed", 1.05)),
        "ref_tail_clause_max_units": int(form.get("semantic_ref_tail_clause_max_units", 36)),
        "ref_tail_clause_min_remaining_units": int(form.get("semantic_ref_tail_clause_min_remaining_units", 35)),
        "ref_tail_clause_max_removed_ms": int(form.get("semantic_ref_tail_clause_max_removed_ms", 5500)),
        "weak_start_merge_enabled": form.get("semantic_weak_start_merge_enabled") == "1",
        "weak_start_merge_slack_sec": float(form.get("semantic_weak_start_merge_slack_sec", 0.35)),
        "weak_start_words": [w.strip().lower() for w in re.split(r"[,\n]", weak_start_words_raw) if w.strip()],
        "generated_trim": {
            "enabled": form.get("semantic_generated_trim_enabled") == "1",
            "leading_keep_ms": int(form.get("semantic_generated_trim_leading_keep_ms", 300)),
            "trailing_keep_ms": int(form.get("semantic_generated_trim_trailing_keep_ms", 160)),
            "silence_thresh_db": int(form.get("semantic_generated_trim_silence_thresh_db", -50)),
        },
        "punctuation": {
            "comma": float(form.get("semantic_punct_comma", 0.20)),
            "semicolon": float(form.get("semantic_punct_semicolon", 0.30)),
            "colon": float(form.get("semantic_punct_colon", 0.30)),
            "dash": float(form.get("semantic_punct_dash", 0.22)),
            "sentence": float(form.get("semantic_punct_sentence", 0.40)),
            "ellipsis": float(form.get("semantic_punct_ellipsis", 0.55)),
        },
        "comma_softening": {
            "enabled": form.get("comma_softening_enabled") == "1",
            "vocative_enabled": form.get("comma_softening_vocative") == "1",
            "decorative_tail_enabled": form.get("comma_softening_tail") == "1",
            "decorative_tails": [t.strip().lower() for t in re.split(r"[,\n]", tails_raw) if t.strip()],
        },
    }
    # Voice speed overrides
    override_names = form.getlist("vo_name")
    override_speeds = form.getlist("vo_max_speed")
    voice_overrides = {}
    for name, speed_str in zip(override_names, override_speeds):
        name = name.strip().lower()
        if not name:
            continue
        try:
            ms = float(speed_str)
            if ms > 0:
                voice_overrides[name] = {"max_speed": ms}
        except (ValueError, TypeError):
            pass
    cfg["voice_overrides"] = voice_overrides

    cfg["ref_normalization"] = {
        "target_dbfs": float(form.get("norm_target_dbfs", -28)),
        "normalize_on_the_fly": form.get("normalize_on_the_fly") == "1",
        "spectral_penalty": form.get("norm_spectral_penalty") == "1",
    }

    raw = form.get("custom_accent_dict_text", "").strip()
    custom_accent = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            word, stressed = line.split(":", 1)
            word = word.strip().lower()
            stressed = stressed.strip()
            if word and stressed:
                custom_accent[word] = stressed
    cfg["custom_accent_dict"] = custom_accent

    cfg["fix_gen_text"] = {
        "sentence_case": form.get("fix_sentence_case") == "1",
        "terminal_punctuation": form.get("fix_terminal_punct") == "1",
    }

    save_config(cfg)
    threading.Thread(target=_reload_api, daemon=True).start()

    return templates.TemplateResponse(
        "config.html", {
            "request": request, "config": cfg,
            "message": {"type": "success", "text": "Settings saved."},
        },
    )


@app.get("/tts-test", response_class=HTMLResponse)
def tts_test_page(request: Request):
    cfg = load_config()
    return templates.TemplateResponse(
        "tts_test.html", {
            "request": request, "characters": get_characters(),
            "config": cfg,
            "audio_data": None, "selected_voice": None,
            "gen_input": None, "selected_seed": None,
            "nfe_step": cfg.get("default_nfe_step", 64),
            "speed": 1.0, "message": None, "chunk_plan": None,
        },
    )


@app.post("/tts-test", response_class=HTMLResponse)
async def tts_test_submit(request: Request):
    form = await request.form()
    voice = form.get("voice", "").strip()
    gen_input = form.get("input", "").strip()
    seed_raw = form.get("seed", "").strip()
    random_seed = form.get("random_seed") == "1"
    nfe_step_raw = form.get("nfe_step", "").strip()

    user_seed = int(seed_raw) if seed_raw else None
    used_seed = random.randint(0, 2**31 - 1) if random_seed or user_seed is None else user_seed
    nfe_step = int(nfe_step_raw) if nfe_step_raw else load_config().get("default_nfe_step", 64)
    speed_raw = form.get("speed", "").strip()
    speed = float(speed_raw) if speed_raw else 1.0

    if not voice or not gen_input:
        return templates.TemplateResponse(
            "tts_test.html", {
                "request": request, "characters": get_characters(),
                "config": load_config(),
                "audio_data": None, "selected_voice": voice,
                "gen_input": gen_input, "selected_seed": user_seed,
                "random_seed": random_seed, "used_seed": None,
                "nfe_step": nfe_step, "speed": speed,
                "chunk_plan": None,
                "message": {"type": "error", "text": "Voice and input are required."},
            },
        )

    body = {
        "model": "F5TTS_v1_Base_v2",
        "voice": voice,
        "input": gen_input,
        "response_format": "wav",
        "seed": used_seed,
        "nfe_step": nfe_step,
        "speed": speed,
    }

    chunk_plan = None
    preview_error = None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                preview = await client.post(f"{API_URL}/v1/audio/chunks/preview", json=body)
                preview.raise_for_status()
                chunk_plan = preview.json()
            except Exception as e:
                preview_error = f"Chunk preview failed: {e}"
                logger.warning(preview_error)
            resp = await client.post(f"{API_URL}/v1/audio/speech", json=body)
            resp.raise_for_status()
            audio_b64 = base64.b64encode(resp.content).decode()
    except Exception as e:
        logger.warning(f"TTS preview failed: {e}")
        return templates.TemplateResponse(
            "tts_test.html", {
                "request": request, "characters": get_characters(),
                "config": load_config(),
                "audio_data": None, "selected_voice": voice,
                "gen_input": gen_input, "selected_seed": user_seed,
                "random_seed": random_seed, "used_seed": None,
                "nfe_step": nfe_step, "speed": speed,
                "chunk_plan": chunk_plan,
                "message": {"type": "error", "text": f"TTS request failed: {e}"},
            },
        )

    return templates.TemplateResponse(
        "tts_test.html", {
            "request": request, "characters": get_characters(),
            "config": load_config(),
            "audio_data": audio_b64, "selected_voice": voice,
            "gen_input": gen_input, "selected_seed": user_seed,
            "random_seed": random_seed, "used_seed": used_seed,
            "nfe_step": nfe_step, "speed": speed, "chunk_plan": chunk_plan,
            "message": {"type": "error", "text": preview_error} if preview_error else None,
        },
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
