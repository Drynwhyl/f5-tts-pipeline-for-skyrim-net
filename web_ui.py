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
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


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

    for emotion in emotions:
        audio_file = form.get(f"audio_{emotion}")
        if not audio_file or not hasattr(audio_file, "filename") or not audio_file.filename:
            continue
        ext = Path(audio_file.filename).suffix or ".wav"
        content = await audio_file.read()
        with open(char_dir / f"{emotion}{ext}", "wb") as f:
            f.write(content)
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
        content = await audio_file.read()
        with open(char_dir / f"{emotion}{ext}", "wb") as f:
            f.write(content)

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
    content = await audio_file.read()
    with open(char_dir / f"{emotion}{ext}", "wb") as f:
        f.write(content)

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
    return templates.TemplateResponse(
        "tts_test.html", {
            "request": request, "characters": get_characters(),
            "config": load_config(),
            "audio_data": None, "selected_voice": None,
            "gen_input": None, "selected_seed": None,
            "nfe_step": 64, "message": None,
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
    nfe_step = int(nfe_step_raw) if nfe_step_raw else 64

    if not voice or not gen_input:
        return templates.TemplateResponse(
            "tts_test.html", {
                "request": request, "characters": get_characters(),
                "config": load_config(),
                "audio_data": None, "selected_voice": voice,
                "gen_input": gen_input, "selected_seed": user_seed,
                "random_seed": random_seed, "used_seed": None,
                "nfe_step": nfe_step,
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
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
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
                "nfe_step": nfe_step,
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
            "nfe_step": nfe_step, "message": None,
        },
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
