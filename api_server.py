import io
import os
import re
import time
import uuid
import json
import shutil
import tempfile
import logging
import mimetypes
from pathlib import Path
from typing import Optional, List

import torch
import torchaudio
import uvicorn
from pydub import AudioSegment, silence
from ruaccent import RUAccent
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Body
from fastapi.responses import Response, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from transformers import pipeline as hf_pipeline

from f5_tts.model import DiT
from f5_tts.infer.utils_infer import (
    load_model,
    load_vocoder,
    preprocess_ref_audio_text,
    infer_process,
)
from semantic_infer import DEFAULT_SEMANTIC_CHUNKING, build_chunk_plan, infer_process_semantic, merge_semantic_config
from audio_utils import trim_audio_to_sentence_boundary, normalize_loudness, DEFAULT_MAX_MS, DEFAULT_MIN_SILENCE_LEN, DEFAULT_SILENCE_THRESH, DEFAULT_KEEP_SILENCE, DEFAULT_NORMALIZATION_TARGET, DEFAULT_NORMALIZE_ON_FLY, DEFAULT_SPECTRAL_PENALTY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("f5-tts-api")

MODEL_PATH = "/home/drynw/models/f5-tts/F5TTS_v1_Base_v2/model_last_inference.safetensors"
VOCAB_PATH = "/home/drynw/models/f5-tts/F5TTS_v1_Base_v2/vocab.txt"
VOICES_DIR = "/home/drynw/models/f5-tts/voices"
CONFIG_PATH = "/home/drynw/models/f5-tts/config.json"
CLONED_VOICES_DIR_NAME = "_cloned"
DEFAULT_REF_AUDIO = os.environ.get("F5_DEFAULT_REF_AUDIO", "")

MODEL_CFG = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)

app = FastAPI(title="F5-TTS OpenAI-Compatible API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request, call_next):
    body = None
    content_type = request.headers.get("content-type", "")
    is_multipart = "multipart" in content_type
    if request.method in ("POST", "PUT", "PATCH") and not is_multipart:
        body_bytes = await request.body()
        body = body_bytes.decode("utf-8", errors="replace")[:2000]

    safe_headers = {k: v for k, v in request.headers.items() if k.lower() not in ("authorization", "cookie")}
    logger.info(
        ">>> %s %s\n    headers: %s\n    query: %s\n    body: %s",
        request.method, request.url.path,
        json.dumps(safe_headers, default=str),
        dict(request.query_params),
        body or "(multipart or empty)",
    )

    response = await call_next(request)

    resp_body = b""
    async for chunk in response.body_iterator:
        resp_body += chunk

    logger.info(
        "<<< %s %s -> %s\n    body: %s",
        request.method, request.url.path, response.status_code,
        resp_body[:500].decode("utf-8", errors="replace"),
    )

    from starlette.responses import Response as StarResponse
    return StarResponse(
        content=resp_body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )


class TTSRequest(BaseModel):
    model: str = "F5TTS_v1_Base_v2"
    input: str
    voice: str = "default"
    response_format: str = "wav"
    speed: Optional[float] = Field(default=None)
    seed: Optional[int] = None
    nfe_step: int = Field(default_factory=lambda: DEFAULT_NFE_STEP)


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "user"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelInfo]


device = "cuda" if torch.cuda.is_available() else "cpu"
model_obj = None
vocoder = None
voice_registry: dict[str, dict] = {}
app_config: dict = {}
app_emotion_map: dict[str, str] = {}
accentor: Optional[RUAccent] = None
whisper_pipelines: dict[str, any] = {}
DEFAULT_NFE_STEP = 64

WHISPER_MODELS = {
    "turbo": "openai/whisper-large-v3-turbo",
    "large": "openai/whisper-large-v3",
}


def _get_whisper_pipeline(model: str):
    if model not in whisper_pipelines:
        model_id = WHISPER_MODELS.get(model)
        if not model_id:
            raise HTTPException(status_code=400, detail=f"Unknown whisper model: {model}, choose from {list(WHISPER_MODELS.keys())}")
        logger.info("Loading Whisper model '%s' (%s)...", model, model_id)
        whisper_pipelines[model] = hf_pipeline(
            "automatic-speech-recognition",
            model=model_id,
            torch_dtype=torch.float16 if "cuda" in device else torch.float32,
            device=device,
        )
        logger.info("Whisper '%s' loaded", model)
    return whisper_pipelines[model]


def _apply_stress(text: str) -> str:
    if not text or accentor is None:
        return text
    if "+" in text:
        return text
    try:
        result = accentor.process_all(text)
        return result
    except Exception as e:
        logger.warning("RUAccent failed on '%s': %s", text[:50], e)
        return text


def _fix_terminal_punctuation(text: str) -> str:
    text = text.rstrip()
    if not text:
        return text
    text = re.sub(r'[.…]{2,}$', '.', text)
    text = re.sub(r'[,;:\-–—]+$', '.', text)
    if not re.search(r'[.!?]$', text):
        text += '.'
    return text


def _sentence_case(text: str) -> str:
    sentences = re.split(r'(?<=[.!?])\s+', text)
    result = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        result.append(s[0].upper() + s[1:].lower())
    return ' '.join(result)


def _apply_fix_gen_text(text: str) -> str:
    cfg = app_config or {}
    fix = cfg.get("fix_gen_text", {}) or {}
    if fix.get("terminal_punctuation", True):
        text = _fix_terminal_punctuation(text)
    if fix.get("sentence_case", True):
        text = _sentence_case(text)
    return text


def _build_emotion_map(cfg: dict) -> dict[str, str]:
    mapping = {}
    for core, aliases in cfg.get("emotion_map", {}).items():
        for alias in aliases:
            mapping[alias.lower()] = core.lower()
    return mapping


def _transcribe_audio_file(audio_path: str) -> str:
    """Transcribe a single audio file using Whisper turbo."""
    try:
        pipe = _get_whisper_pipeline("turbo")
        result = pipe(audio_path, return_timestamps=False, generate_kwargs={"task": "transcribe"})
        return result["text"].strip()
    except Exception as e:
        logger.warning("Transcription failed for '%s': %s", audio_path, e)
        return ""


def _transcribe_empty_refs():
    """Scan all voice .txt files that are empty and transcribe the matching audio."""
    count = 0
    for base in (Path(VOICES_DIR), Path(VOICES_DIR) / app_config.get("cloned_voices_dir", "_cloned")):
        if not base.is_dir():
            continue
        for voice_dir in base.iterdir():
            if not voice_dir.is_dir():
                continue
            for txt_file in voice_dir.glob("*.txt"):
                content = txt_file.read_text().strip()
                if content:
                    continue
                stem = txt_file.stem
                audio_file = None
                for ext in (".wav", ".mp3", ".flac"):
                    p = voice_dir / f"{stem}{ext}"
                    if p.exists():
                        audio_file = p
                        break
                if not audio_file:
                    continue
                logger.info("Transcribing empty ref text for %s/%s...", voice_dir.name, stem)
                text = _transcribe_audio_file(str(audio_file))
                if text:
                    txt_file.write_text(text)
                    count += 1
    if count:
        logger.info("Auto-transcribed %d reference text(s)", count)


def _stress_ref_texts():
    """Scan all voice .txt files and apply RUAccent to any without + marks."""
    if accentor is None:
        return
    count = 0
    for voice_dir in Path(VOICES_DIR).iterdir():
        if not voice_dir.is_dir():
            continue
        for txt_file in voice_dir.glob("*.txt"):
            content = txt_file.read_text().strip()
            if content and "+" not in content:
                stressed = _apply_stress(content)
                if stressed != content:
                    txt_file.write_text(stressed)
                    count += 1
    cloned_name = app_config.get("cloned_voices_dir", "_cloned") if app_config else "_cloned"
    cloned_dir = Path(VOICES_DIR) / cloned_name
    if cloned_dir.is_dir():
        for voice_dir in cloned_dir.iterdir():
            if not voice_dir.is_dir():
                continue
            for txt_file in voice_dir.glob("*.txt"):
                content = txt_file.read_text().strip()
                if content and "+" not in content:
                    stressed = _apply_stress(content)
                    if stressed != content:
                        txt_file.write_text(stressed)
                        count += 1
    if count:
        logger.info("Applied stress to %d reference text(s)", count)


def load_app_config() -> dict:
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
    config_path = Path(CONFIG_PATH)
    if config_path.exists():
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            merged = default.copy()
            merged.update(cfg)
            merged["semantic_chunking"] = merge_semantic_config(cfg.get("semantic_chunking", {}))
            return merged
        except Exception as e:
            logger.warning(f"Failed to load config.json: {e}, using defaults")
    else:
        with open(config_path, "w") as f:
            json.dump(default, f, indent=2)
        logger.info(f"Created default config.json at {CONFIG_PATH}")
    return default


def _get_trim_settings() -> dict:
    """Return trim settings from app_config, falling back to defaults."""
    ts = app_config.get("trim_settings", {}) if app_config else {}
    return {
        "max_ms": ts.get("max_ms", 12000),
        "min_silence_len": ts.get("min_silence_len", 1000),
        "silence_thresh": ts.get("silence_thresh", -50),
        "keep_silence": ts.get("keep_silence", 500),
    }


def parse_emotion_tag(text: str, tag_open: str, tag_close: str) -> tuple[Optional[str], str]:
    pattern = re.escape(tag_open) + r"(\w+)" + re.escape(tag_close)
    matches = list(re.finditer(pattern, text))

    if not matches:
        return None, text

    matched_emotion = matches[0].group(1)

    if len(matches) > 1:
        remaining = [m.group(0) for m in matches[1:]]
        logger.warning(
            f"Multiple emotion tags found in input, using first '{matched_emotion}', "
            f"ignoring: {remaining}"
        )

    cleaned = re.sub(pattern, "", text).strip()
    return matched_emotion.lower(), cleaned


def load_voice_registry():
    global voice_registry
    os.makedirs(VOICES_DIR, exist_ok=True)
    voice_registry = {}

    cloned_name = app_config.get("cloned_voices_dir", "_cloned") if app_config else "_cloned"

    def _scan_dir(base_dir: Path, voice_type: str):
        for voice_dir in sorted(base_dir.iterdir()):
            if not voice_dir.is_dir():
                continue
            name = voice_dir.name
            if name.startswith("_") and voice_type == "premade":
                continue  # skip system dirs when scanning premade
            emotions = {}
            audio_files = list(voice_dir.glob("*.wav")) + list(voice_dir.glob("*.mp3")) + list(voice_dir.glob("*.flac"))
            for af in audio_files:
                stem = af.stem
                text_file = voice_dir / f"{stem}.txt"
                ref_text = text_file.read_text().strip() if text_file.exists() else ""
                emotions[stem] = {"ref_audio": str(af), "ref_text": ref_text}
            legacy_text = voice_dir / "ref_text.txt"
            if not emotions and legacy_text.exists():
                legacy_audio = list(voice_dir.glob("audio.*"))
                if legacy_audio:
                    emotions["normal"] = {
                        "ref_audio": str(legacy_audio[0]),
                        "ref_text": legacy_text.read_text().strip(),
                    }
            if emotions:
                default_emotion = "normal" if "normal" in emotions else list(emotions.keys())[0]
                voice_registry[name] = {
                    "emotions": emotions,
                    "default_emotion": default_emotion,
                    "type": voice_type,
                }
                logger.info(f"Registered {voice_type} character '{name}': emotions={list(emotions.keys())}")

    _scan_dir(Path(VOICES_DIR), "premade")
    cloned_dir = Path(VOICES_DIR) / cloned_name
    if cloned_dir.is_dir():
        _scan_dir(cloned_dir, "cloned")


def load_models():
    global model_obj, vocoder
    logger.info(f"Loading model on {device}...")
    model_obj = load_model(
        model_cls=DiT,
        model_cfg=MODEL_CFG,
        ckpt_path=MODEL_PATH,
        vocab_file=VOCAB_PATH,
        device=device,
    )
    logger.info("Loading vocoder...")
    vocoder = load_vocoder(device=device)
    logger.info("Models loaded successfully")


@app.on_event("startup")
def startup():
    global app_config, app_emotion_map, accentor, DEFAULT_NFE_STEP
    app_config = load_app_config()
    DEFAULT_NFE_STEP = app_config.get("default_nfe_step", 64)
    app_emotion_map = _build_emotion_map(app_config)
    load_models()
    load_voice_registry()
    logger.info("Loading RUAccent...")
    try:
        accentor = RUAccent()
        custom_dict = app_config.get("custom_accent_dict", {}) or {}
        accentor.load(use_dictionary=True, custom_dict=custom_dict)
        logger.info("RUAccent loaded successfully")
        _transcribe_empty_refs()
        _stress_ref_texts()
        load_voice_registry()
        _normalize_all_voices()
    except Exception as e:
        logger.warning("Failed to load RUAccent: %s", e)
        accentor = None


@app.get("/v1/models")
def list_models():
    return ModelList(data=[ModelInfo(id="F5TTS_v1_Base_v2")])


SILENCE_SENTINEL = "__silence__"


def _get_dynamic_speed(ref_audio_path: str, ref_text: str, gen_text: str, base_speed: float) -> float:
    """Interpolate speech rate based on estimated gen_text duration using ref speaker's pace.

    Uses ref_audio duration and ref_text length to compute chars/sec, then estimates
    gen_text duration. Applies configured speed curve:
      estimated_dur <= min_rate_length → min_rate
      estimated_dur >= max_rate_length → max_rate
      otherwise → linear interpolation

    Returns base_speed * interpolated_rate.
    """
    if not app_config:
        logger.warning("Dynamic speed: app_config is empty")
        return base_speed
    ds = app_config.get("dynamic_speed", {})
    if not ds or not ds.get("enabled", False):
        return base_speed

    min_rate = float(ds.get("min_rate", 1.0))
    min_dur = float(ds.get("min_rate_length", 3.0))
    max_rate = float(ds.get("max_rate", 1.4))
    max_dur = float(ds.get("max_rate_length", 15.0))

    try:
        ref_dur = float(__import__("subprocess").check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", ref_audio_path]
        ).decode().strip())
    except Exception:
        return base_speed

    ref_text_len = len(ref_text.strip())
    gen_text_len = len(gen_text.strip())

    if ref_text_len == 0 or ref_dur <= 0:
        return base_speed

    estimated_dur = gen_text_len * ref_dur / ref_text_len

    if estimated_dur <= min_dur:
        rate = min_rate
    elif estimated_dur >= max_dur:
        rate = max_rate
    else:
        ratio = (estimated_dur - min_dur) / (max_dur - min_dur)
        rate = min_rate + (max_rate - min_rate) * ratio

    rate = max(0.3, min(rate, 2.0))
    result = base_speed * rate
    result = max(0.3, min(result, 2.0))

    logger.info(
        "Dynamic speed: estimated=%.1fs rate=%.2f speed=%.2f "
        "(gen=%d ref=%d ref_dur=%.1fs min_r=%.1f@%.1fs max_r=%.1f@%.1fs)",
        estimated_dur, rate, result,
        gen_text_len, ref_text_len, ref_dur, min_rate, min_dur, max_rate, max_dur,
    )
    return result


HOP_LENGTH = 256
TARGET_SR = 24000


def _apply_voice_speed_override(voice_name: str, speed: float) -> float:
    """Cap speed per-character via config[voice_overrides][char][max_speed]."""
    if speed <= 1.0 or not app_config:
        return speed
    overrides = app_config.get("voice_overrides", {}) or {}
    vo = overrides.get(voice_name, {}) or {}
    max_speed = vo.get("max_speed", 0)
    if max_speed > 0 and speed > max_speed:
        logger.info("Voice override: %s speed capped %.1f→%.1f", voice_name, speed, max_speed)
        return max_speed
    return speed


def _tts_infer(ref_audio_path: str, ref_text: str, gen_text: str, speed: Optional[float] = None, nfe_step: Optional[int] = None, seed: Optional[int] = None, save_ref_text_to: Optional[str] = None) -> tuple[bytes, int]:
    if nfe_step is None:
        nfe_step = DEFAULT_NFE_STEP
    if speed is None:
        speed = 1.0
    if ref_audio_path == SILENCE_SENTINEL:
        sr = TARGET_SR
        silence = torch.zeros(1, sr)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            torchaudio.save(tmp.name, silence, sr)
            tmp.seek(0)
            return tmp.read(), sr
    if seed is not None:
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
    gen_text = _apply_fix_gen_text(gen_text)
    gen_text = _apply_stress(gen_text)
    ref_audio_processed, ref_text_processed = preprocess_ref_audio_text(
        ref_audio_path, ref_text, show_info=logger.info
    )
    ref_text_processed = _apply_stress(ref_text_processed)

    # On-the-fly loudness normalization (only for files in voices dir, not temp atempo)
    if ref_audio_processed != SILENCE_SENTINEL and ref_audio_path.startswith(VOICES_DIR):
        _normalize_audio(ref_audio_processed)
    if save_ref_text_to and not ref_text and ref_text_processed:
        Path(save_ref_text_to).write_text(ref_text_processed)
        logger.info("Saved auto-transcribed ref_text to %s", save_ref_text_to)

    semantic_cfg = merge_semantic_config((app_config or {}).get("semantic_chunking", {}))
    use_semantic = semantic_cfg.get("enabled", True)
    cleanup_paths = []

    if use_semantic and ref_audio_processed != SILENCE_SENTINEL:
        ref_audio_processed, ref_text_processed, quarantine_tmp = _maybe_quarantine_ref_tail(
            ref_audio_processed, ref_text_processed, semantic_cfg
        )
        if quarantine_tmp:
            cleanup_paths.append(quarantine_tmp)

    if speed > 1.0 and ref_audio_processed != SILENCE_SENTINEL:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            atempo_path = tmp.name
        __import__("subprocess").run(
            ["ffmpeg", "-y", "-i", ref_audio_processed,
             "-filter:a", f"atempo={speed}",
             "-ac", "1", "-ar", str(TARGET_SR), atempo_path],
            capture_output=True, check=True,
        )
        ref_audio_processed = atempo_path
        cleanup_paths.append(atempo_path)

        logger.info("Ref atempo speed=%.1f applied", speed)

    if use_semantic and semantic_cfg.get("ref_guard_enabled", True) and ref_audio_processed != SILENCE_SENTINEL:
        guard_ms = int(semantic_cfg.get("ref_guard_silence_ms", 700))
        if guard_ms > 0:
            ref_audio_processed = _append_ref_guard_silence(ref_audio_processed, guard_ms)
            cleanup_paths.append(ref_audio_processed)
            logger.info("Ref guard silence appended: %dms", guard_ms)

    infer_fn = infer_process_semantic if use_semantic else infer_process
    kwargs = {"semantic_config": semantic_cfg} if use_semantic else {}
    try:
        out_wave, sr, _ = infer_fn(
            ref_audio=ref_audio_processed,
            ref_text=ref_text_processed,
            gen_text=gen_text,
            model_obj=model_obj,
            vocoder=vocoder,
            device=device,
            speed=1.0,
            nfe_step=nfe_step,
            **kwargs,
        )
    finally:
        for path in cleanup_paths:
            try:
                os.unlink(path)
            except Exception:
                pass

    if out_wave is None:
        raise HTTPException(status_code=500, detail="Failed to generate audio")
    audio_tensor = torch.from_numpy(out_wave).unsqueeze(0)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        torchaudio.save(tmp.name, audio_tensor, sr)
        tmp.seek(0)
        content = tmp.read()
    return content, sr


def _append_ref_guard_silence(audio_path: str, guard_ms: int) -> str:
    audio, sr = torchaudio.load(audio_path)
    silence = torch.zeros(audio.shape[0], int(sr * guard_ms / 1000), dtype=audio.dtype)
    guarded = torch.cat([audio, silence], dim=-1)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out_path = tmp.name
    torchaudio.save(out_path, guarded, sr)
    return out_path


def _maybe_quarantine_ref_tail(audio_path: str, ref_text: str, semantic_cfg: dict) -> tuple[str, str, Optional[str]]:
    if not semantic_cfg.get("ref_tail_quarantine_enabled", True):
        return audio_path, ref_text, None

    sentences = _split_ref_sentences(ref_text)
    if len(sentences) < 2:
        return audio_path, ref_text, None

    max_units = int(semantic_cfg.get("ref_tail_max_units", 18))
    max_removed_ms = int(semantic_cfg.get("ref_tail_max_removed_ms", 4500))
    if max_units <= 0 or max_removed_ms <= 0:
        return audio_path, ref_text, None

    try:
        aseg = AudioSegment.from_file(audio_path)
        removed_sentences = []
        removed_ms = 0
        keep_ms = int(semantic_cfg.get("ref_tail_keep_silence_ms", 200))
        min_silence_ms = int(semantic_cfg.get("ref_tail_min_silence_ms", 150))

        while len(sentences) >= 2:
            last_sentence = sentences[-1].strip()
            if _speech_units_for_tail(last_sentence) > max_units:
                break

            ranges = silence.detect_nonsilent(
                aseg,
                min_silence_len=min_silence_ms,
                silence_thresh=-50,
                seek_step=10,
            )
            if len(ranges) < 2:
                break

            final_start, final_end = ranges[-1]
            prev_end = ranges[-2][1]
            if final_start <= prev_end:
                break

            cut_at = min(final_start, prev_end + max(0, keep_ms))
            step_removed = len(aseg) - cut_at
            if cut_at < 1000 or step_removed < 250 or removed_ms + step_removed > max_removed_ms:
                break

            aseg = aseg[:cut_at]
            sentences = sentences[:-1]
            removed_ms += step_removed
            removed_sentences.append(last_sentence)

        if not removed_sentences:
            return audio_path, ref_text, None

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out_path = tmp.name
        aseg.export(out_path, format="wav")
        new_ref_text = " ".join(sentences).strip()
        if new_ref_text and not new_ref_text.endswith(" "):
            new_ref_text += " "
        logger.info(
            "Ref tail quarantined: removed %d final sentence(s) %s and %.0fms audio tail",
            len(removed_sentences), removed_sentences, removed_ms,
        )
        return out_path, new_ref_text, out_path
    except Exception as e:
        logger.warning("Ref tail quarantine failed: %s", e)
        return audio_path, ref_text, None


def _split_ref_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.findall(r".+?(?:[.!?]+|$)(?:\s+|$)", text.strip()) if s.strip()]


def _speech_units_for_tail(text: str) -> int:
    return len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]", text.replace("+", "")))


def _lookup_voice(voice_name: str, emotion: Optional[str] = None) -> tuple[str, str]:
    if app_config.get("ignore_player_voice", False):
        ignored = [p.lower() for p in app_config.get("ignored_voice_patterns", [])]
        if voice_name.lower() in ignored:
            logger.info("Ignored player voice '%s', returning silence", voice_name)
            return ("__silence__", "")

    # Narration voice override
    override = (app_config or {}).get("narration_voice_override", "")
    if override and voice_name == "dlc1seranavoice":
        logger.info("Narration voice override: '%s' -> '%s'", voice_name, override)
        voice_name = override

    if voice_name in voice_registry:
        voice_data = voice_registry[voice_name]
        emotions = voice_data["emotions"]
        default_emotion = voice_data["default_emotion"]

        if emotion and emotion in emotions:
            selected = emotions[emotion]
        else:
            if emotion and emotion not in emotions:
                logger.warning(
                    f"Emotion '{emotion}' not configured for character '{voice_name}', "
                    f"falling back to '{default_emotion}'"
                )
            selected = emotions[default_emotion]

        return selected["ref_audio"], selected["ref_text"]
    elif DEFAULT_REF_AUDIO and os.path.exists(DEFAULT_REF_AUDIO):
        return DEFAULT_REF_AUDIO, ""
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Character '{voice_name}' not found. Available: {list(voice_registry.keys()) or 'none'}",
        )


@app.post("/v1/audio/speech")
def text_to_speech(req: TTSRequest):
    if model_obj is None or vocoder is None:
        raise HTTPException(status_code=500, detail="Model not loaded")

    tag_cfg = app_config.get("emotion_tag", {"open": "[", "close": "]"})
    emotion, gen_text_clean = parse_emotion_tag(req.input, tag_cfg["open"], tag_cfg["close"])
    if emotion:
        emotion = app_emotion_map.get(emotion, emotion)

    ref_audio_path, ref_text = _lookup_voice(req.voice, emotion)
    save_ref = str(Path(ref_audio_path).with_suffix(".txt")) if ref_audio_path.startswith(VOICES_DIR) else None
    speed = req.speed if req.speed is not None else _get_dynamic_speed(ref_audio_path, ref_text, gen_text_clean, 1.0)
    speed = _apply_voice_speed_override(req.voice, speed)
    content, _ = _tts_infer(ref_audio_path, ref_text, gen_text_clean, speed, req.nfe_step, req.seed, save_ref_text_to=save_ref)

    media_type = "audio/mpeg" if req.response_format == "mp3" else "audio/wav"
    return Response(content=content, media_type=media_type)


@app.post("/v1/audio/chunks/preview")
def preview_chunks(req: TTSRequest):
    tag_cfg = app_config.get("emotion_tag", {"open": "[", "close": "]"})
    emotion, gen_text_clean = parse_emotion_tag(req.input, tag_cfg["open"], tag_cfg["close"])
    if emotion:
        emotion = app_emotion_map.get(emotion, emotion)

    ref_audio_path, ref_text = _lookup_voice(req.voice, emotion)
    speed = req.speed if req.speed is not None else _get_dynamic_speed(ref_audio_path, ref_text, gen_text_clean, 1.0)
    speed = _apply_voice_speed_override(req.voice, speed)

    gen_text = _apply_fix_gen_text(gen_text_clean)
    gen_text = _apply_stress(gen_text)
    ref_audio_processed, ref_text_processed = preprocess_ref_audio_text(
        ref_audio_path, ref_text, show_info=logger.info
    )
    ref_text_processed = _apply_stress(ref_text_processed)

    semantic_cfg = merge_semantic_config((app_config or {}).get("semantic_chunking", {}))
    preview_cleanup = None
    if semantic_cfg.get("enabled", True) and ref_audio_processed != SILENCE_SENTINEL:
        ref_audio_processed, ref_text_processed, preview_cleanup = _maybe_quarantine_ref_tail(
            ref_audio_processed, ref_text_processed, semantic_cfg
        )
    if ref_audio_processed == SILENCE_SENTINEL:
        ref_duration = 1.0
    else:
        audio, sr = torchaudio.load(ref_audio_processed)
        ref_duration = audio.shape[-1] / sr
        if speed > 1.0:
            ref_duration = ref_duration / speed
        if semantic_cfg.get("enabled", True) and semantic_cfg.get("ref_guard_enabled", True):
            ref_duration += max(0, int(semantic_cfg.get("ref_guard_silence_ms", 700))) / 1000.0

    plan = build_chunk_plan(gen_text, ref_text_processed, ref_duration, semantic_cfg)
    if preview_cleanup:
        try:
            os.unlink(preview_cleanup)
        except Exception:
            pass
    plan.update({
        "voice": req.voice,
        "emotion": emotion or "normal",
        "speed": speed,
        "input_text": req.input,
        "clean_text": gen_text_clean,
        "stressed_text": gen_text,
        "ref_text": ref_text_processed,
    })
    logger.info(
        "Chunk preview voice=%s emotion=%s speed=%.2f chunks=%d",
        req.voice, emotion or "normal", speed, len(plan["chunks"]),
    )
    for i, chunk in enumerate(plan["chunks"]):
        logger.info(
            "Chunk preview %d: %.2fs total=%.2fs frames=%d reason=%s text=%s",
            i, chunk["estimated_sec"], chunk["total_sec"], chunk["extra_frames"], chunk["reason"], chunk["text"],
        )
    return JSONResponse(plan)


@app.post("/v1/transcribe")
async def transcribe_audio(
    audio: UploadFile = File(...),
    language: Optional[str] = Form(None),
    model: str = Form("turbo"),
):
    if model not in WHISPER_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model}', choose from {list(WHISPER_MODELS.keys())}")

    pipe = _get_whisper_pipeline(model)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(await audio.read())
        tmp.close()

        audio_dur = float(
            __import__("subprocess").check_output(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", tmp.name]
            ).decode().strip()
        )

        torch.cuda.synchronize() if "cuda" in device else None
        t0 = time.perf_counter()
        result = pipe(
            tmp.name,
            return_timestamps=False,
            generate_kwargs={"task": "transcribe", "language": language} if language else {"task": "transcribe"},
        )
        torch.cuda.synchronize() if "cuda" in device else None
        elapsed = (time.perf_counter() - t0) * 1000

        text = result["text"].strip()
        detected_lang = result.get("chunks", [{}])[0].get("language", "") if result.get("chunks") else ""

        return {
            "text": text,
            "detected_language": detected_lang or language or "unknown",
            "model": model,
            "inference_ms": round(elapsed, 1),
            "audio_duration_s": round(audio_dur, 2),
        }
    finally:
        os.unlink(tmp.name)


@app.post("/v1/audio/transcriptions")
async def openai_transcriptions(
    file: UploadFile = File(...),
    model: str = Form("whisper-1"),
    language: Optional[str] = Form(None),
):
    """OpenAI-compatible transcription endpoint (used by SkyrimNet).
    model: 'whisper-1' or 'turbo' -> turbo, 'large' -> large-v3
    """
    MODEL_MAP = {"whisper-1": "turbo", "turbo": "turbo", "large": "large"}
    whisper_model = MODEL_MAP.get(model)
    if not whisper_model:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model}', use 'whisper-1', 'turbo', or 'large'")

    pipe = _get_whisper_pipeline(whisper_model)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(await file.read())
        tmp.close()

        torch.cuda.synchronize() if "cuda" in device else None
        result = pipe(
            tmp.name,
            return_timestamps=False,
            generate_kwargs={"task": "transcribe", "language": language} if language else {"task": "transcribe"},
        )
        text = result["text"].strip()
        return {"text": text}
    finally:
        os.unlink(tmp.name)


# ── XTTS-native endpoints (Coqui API format) ──────────────────────────

from starlette.requests import Request as StarRequest


def _get_speaker_ref(speaker_wav_val: str) -> tuple[str, str]:
    """Resolve a speaker name (string) from the XTTS JSON format to ref audio/text."""
    name = Path(speaker_wav_val).stem
    # Check player voice ignore before any lookup
    if app_config.get("ignore_player_voice", False):
        ignored = [p.lower() for p in app_config.get("ignored_voice_patterns", [])]
        if name.lower() in ignored or speaker_wav_val.lower() in ignored:
            logger.info("Ignored player voice '%s', returning silence", speaker_wav_val)
            return ("__silence__", "")
    if name in voice_registry:
        return _lookup_voice(name)
    # If not in registry, try the raw path
    p = Path(speaker_wav_val)
    if p.is_file():
        return str(p), ""
    raise HTTPException(status_code=400, detail=f"Speaker '{speaker_wav_val}' not found")


@app.post("/tts_to_audio")
@app.post("/tts_to_audio/")
async def tts_to_audio(request: StarRequest):
    if model_obj is None or vocoder is None:
        raise HTTPException(status_code=500, detail="Model not loaded")

    is_json = "application/json" in request.headers.get("content-type", "")

    if is_json:
        body = await request.json()
        gen_text = body.get("text", "")
        speaker_wav = body.get("speaker_wav", "")
        language = body.get("language", "ru")
        save_path = body.get("save_path", "")

        if not gen_text:
            raise HTTPException(status_code=422, detail="Field 'text' is required")
        if not speaker_wav:
            raise HTTPException(status_code=422, detail="Field 'speaker_wav' is required")

        ref_audio_path, ref_text = _get_speaker_ref(speaker_wav)
        save_ref = str(Path(ref_audio_path).with_suffix(".txt")) if ref_audio_path.startswith(VOICES_DIR) else None
        speed = _get_dynamic_speed(ref_audio_path, ref_text, gen_text, 1.0)
        speed = _apply_voice_speed_override(Path(speaker_wav).stem, speed)
        content, _ = _tts_infer(ref_audio_path, ref_text, gen_text, speed, save_ref_text_to=save_ref)

        if save_path:
            save_dir = Path("/tmp/f5-tts-cache")
            save_dir.mkdir(parents=True, exist_ok=True)
            dest = save_dir / save_path
            with open(dest, "wb") as f:
                f.write(content)
    else:
        form = await request.form()
        gen_text = form.get("text", "")
        speaker_name = form.get("speaker_name", "")
        speaker_wav_file = form.get("speaker_wav")
        language = form.get("language", "ru")

        if not gen_text:
            raise HTTPException(status_code=422, detail="Field 'text' is required")

        ref_audio_path = ""
        ref_text = ""
        cleanup_tmp = False

        # Player voice check
        if app_config.get("ignore_player_voice", False):
            ignored = [p.lower() for p in app_config.get("ignored_voice_patterns", [])]
            check_name = speaker_name or ""
            if check_name.lower() in ignored:
                ref_audio_path = "__silence__"

        if speaker_name and not ref_audio_path:
            name_no_ext = Path(speaker_name).stem
            if name_no_ext in voice_registry:
                ref_audio_path, ref_text = _lookup_voice(name_no_ext)

        if not ref_audio_path and speaker_wav_file and hasattr(speaker_wav_file, "read"):
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.write(await speaker_wav_file.read())
            tmp.close()
            ref_audio_path = tmp.name
            ref_text = ""
            cleanup_tmp = True

        if not ref_audio_path:
            raise HTTPException(status_code=400, detail="No speaker provided")

        save_ref = str(Path(ref_audio_path).with_suffix(".txt")) if ref_audio_path.startswith(VOICES_DIR) else None
        try:
            speed = _get_dynamic_speed(ref_audio_path, ref_text, gen_text, 1.0)
            voice_name = Path(speaker_name).stem if speaker_name else ""
            speed = _apply_voice_speed_override(voice_name, speed)
            content, _ = _tts_infer(ref_audio_path, ref_text, gen_text, speed, save_ref_text_to=save_ref)
        finally:
            if cleanup_tmp:
                os.unlink(ref_audio_path)

    return Response(content=content, media_type="audio/wav")


@app.get("/speakers")
def list_speakers():
    return list(voice_registry.keys())


class CreateLatentsJSON(BaseModel):
    language: str = "ru"
    speaker_name: str = ""
    speaker_wav: str = ""
    text: str = ""


def _normalize_audio(audio_path: str):
    """Normalize a single audio file per config settings."""
    cfg = app_config or {}
    norm = cfg.get("ref_normalization", {}) or {}
    target = float(norm.get("target_dbfs", DEFAULT_NORMALIZATION_TARGET))
    sp = bool(norm.get("spectral_penalty", DEFAULT_SPECTRAL_PENALTY))
    msg = normalize_loudness(audio_path, target_dbfs=target, spectral_penalty=sp)
    if msg:
        logger.info("Normalized '%s': %s", audio_path, msg)


def _normalize_all_voices():
    """Batch-normalize all voice audio files to the configured target."""
    if not app_config:
        return
    norm = app_config.get("ref_normalization", {}) or {}
    if not norm.get("normalize_on_the_fly", DEFAULT_NORMALIZE_ON_FLY):
        return
    count = 0
    cloned_name = app_config.get("cloned_voices_dir", "_cloned")
    for base in (Path(VOICES_DIR), Path(VOICES_DIR) / cloned_name):
        if not base.is_dir():
            continue
        for voice_dir in base.iterdir():
            if not voice_dir.is_dir():
                continue
            if voice_dir.name.startswith("_"):
                continue
            for audio_file in voice_dir.glob("*"):
                if audio_file.suffix.lower() not in (".wav", ".mp3", ".flac"):
                    continue
                _normalize_audio(str(audio_file))
                count += 1
    if count:
        logger.info("Batch-normalized %d audio files", count)


def _save_cloned_voice(name: str, audio_file: UploadFile, ref_text: str, cloned_base: Path):
    voice_dir = cloned_base / name
    voice_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(audio_file.filename).suffix if audio_file.filename else ".wav"
    audio_path = voice_dir / f"normal{ext}"
    with open(audio_path, "wb") as f:
        f.write(audio_file.file.read())

    trim_msg = trim_audio_to_sentence_boundary(str(audio_path), **_get_trim_settings())
    if trim_msg:
        logger.info("Trimmed cloned audio for '%s': %s", name, trim_msg)
        ref_text = _transcribe_audio_file(str(audio_path))
    elif not ref_text.strip():
        logger.info("Empty ref text for cloned '%s', transcribing audio...", name)
        ref_text = _transcribe_audio_file(str(audio_path))

    _normalize_audio(str(audio_path))

    ref_text = _apply_stress(ref_text)
    text_path = voice_dir / "normal.txt"
    text_path.write_text(ref_text)
    voice_registry[name] = {
        "emotions": {"normal": {"ref_audio": str(audio_path), "ref_text": ref_text}},
        "default_emotion": "normal",
        "type": "cloned",
    }
    logger.info(f"Registered cloned character '{name}'")


@app.post("/create_and_store_latents")
async def create_and_store_latents(request: StarRequest):
    is_json = "application/json" in request.headers.get("content-type", "")
    speaker_name = ""
    source_speaker = ""
    text = ""
    audio_file = None

    if is_json:
        body = await request.json()
        speaker_name = body.get("speaker_name", "")
        source_speaker = body.get("speaker_wav", "")
        text = body.get("text", "")
    else:
        form = await request.form()
        logger.info("    form keys: %s", list(form.keys()))
        for k, v in form.multi_items():
            log_val = v if isinstance(v, str) else f"<UploadFile: {v.filename}>"
            logger.info("    form[%s] = %s", k, log_val)

        speaker_name = form.get("speaker_name", "")
        text = form.get("text", "")

        for key in ("speaker_wav", "wav_file", "audio_file", "file", "audio"):
            val = form.get(key)
            if val and hasattr(val, "read"):
                audio_file = val
                source_speaker = key
                break

    if not speaker_name:
        raise HTTPException(status_code=422, detail="Field 'speaker_name' is required")

    # Silently ignore player voice
    if app_config.get("ignore_player_voice", False):
        ignored = [p.lower() for p in app_config.get("ignored_voice_patterns", [])]
        if speaker_name.lower() in ignored:
            logger.info("Ignored latents request for player voice '%s'", speaker_name)
            return {"speaker_name": speaker_name, "status": "ignored"}

    # Protect premade voices from being overwritten by cloning
    existing = voice_registry.get(speaker_name)
    if existing and existing.get("type") == "premade":
        logger.info("Ignored latents request for premade voice '%s' (protected)", speaker_name)
        return {"speaker_name": speaker_name, "status": "ignored", "reason": "premade voice protected"}

    if not source_speaker and not audio_file:
        raise HTTPException(status_code=422, detail="Field 'speaker_wav' (or audio file) is required")

    cloned_dir_name = app_config.get("cloned_voices_dir", "_cloned")
    cloned_base = Path(VOICES_DIR) / cloned_dir_name
    os.makedirs(cloned_base, exist_ok=True)

    if audio_file:
        _save_cloned_voice(speaker_name, audio_file, text, cloned_base)
        logger.info(f"Registered cloned character '{speaker_name}' via latents (upload)")
    else:
        ref_audio_path, ref_text = _get_speaker_ref(source_speaker)
        if not ref_text:
            ref_text = _apply_stress(text)
        voice_dir = cloned_base / speaker_name
        voice_dir.mkdir(parents=True, exist_ok=True)
        voice_registry[speaker_name] = {
            "emotions": {"normal": {"ref_audio": ref_audio_path, "ref_text": ref_text}},
            "default_emotion": "normal",
            "type": "cloned",
        }
        logger.info(f"Registered cloned character '{speaker_name}' via latents (cloned from '{source_speaker}')")

    return {"speaker_name": speaker_name, "status": "OK"}


def _save_single_voice(name: str, audio_file: UploadFile, ref_text: str):
    voice_dir = Path(VOICES_DIR) / name
    voice_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(audio_file.filename).suffix if audio_file.filename else ".wav"
    audio_path = voice_dir / f"normal{ext}"
    with open(audio_path, "wb") as f:
        f.write(audio_file.file.read())

    trim_msg = trim_audio_to_sentence_boundary(str(audio_path), **_get_trim_settings())
    if trim_msg:
        logger.info("Trimmed audio for '%s': %s", name, trim_msg)
        ref_text = _transcribe_audio_file(str(audio_path))
    elif not ref_text.strip():
        logger.info("Empty ref text for '%s', transcribing audio...", name)
        ref_text = _transcribe_audio_file(str(audio_path))

    _normalize_audio(str(audio_path))

    ref_text = _apply_stress(ref_text)
    text_path = voice_dir / "normal.txt"
    text_path.write_text(ref_text)

    voice_registry[name] = {
        "emotions": {"normal": {"ref_audio": str(audio_path), "ref_text": ref_text}},
        "default_emotion": "normal",
    }
    logger.info(f"Registered character '{name}' with single voice (normal emotion)")
    return name, str(audio_path)


@app.post("/v1/audio/voice")
def upload_voice(
    audio: UploadFile = File(...),
    name: str = Form(...),
    text: str = Form(""),
):
    voice_name, audio_path = _save_single_voice(name, audio, text)
    return {"status": "ok", "voice": voice_name, "audio_path": audio_path}


@app.post("/v1/voices")
async def create_voice_xtts(
    name: str = Form(...),
    files: List[UploadFile] = File(...),
    reference_text: str = Form(""),
):
    audio_file = files[0] if files else None
    if audio_file is None:
        raise HTTPException(status_code=400, detail="No audio file provided")
    voice_name, audio_path = _save_single_voice(name, audio_file, reference_text)
    return {"status": "ok", "voice": voice_name, "path": audio_path}


@app.get("/v1/voices")
@app.get("/v1/audio/voices")
def list_voices():
    return list(voice_registry.keys())


@app.get("/v1/languages")
def list_languages():
    return ["ru"]


@app.get("/v1/audio/languages")
def list_languages_legacy():
    return {"languages": ["ru"]}


@app.delete("/v1/audio/voice/{name}")
def delete_voice(name: str):
    if name not in voice_registry:
        raise HTTPException(status_code=404, detail=f"Character '{name}' not found")
    shutil.rmtree(Path(VOICES_DIR) / name)
    del voice_registry[name]
    return {"status": "deleted", "character": name}


@app.post("/v1/audio/character")
async def upload_character(
    character: str = Form(...),
    emotions: str = Form(...),
    audio_normal: UploadFile = File(...),
    text_normal: str = Form(""),
    audio_angry: Optional[UploadFile] = None,
    text_angry: str = Form(""),
    audio_calm: Optional[UploadFile] = None,
    text_calm: str = Form(""),
    audio_aggressive: Optional[UploadFile] = None,
    text_aggressive: str = Form(""),
    audio_anxious: Optional[UploadFile] = None,
    text_anxious: str = Form(""),
    audio_happy: Optional[UploadFile] = None,
    text_happy: str = Form(""),
    audio_excited: Optional[UploadFile] = None,
    text_excited: str = Form(""),
    audio_sad: Optional[UploadFile] = None,
    text_sad: str = Form(""),
    audio_scared: Optional[UploadFile] = None,
    text_scared: str = Form(""),
):
    emotion_list = json.loads(emotions)
    if "normal" not in emotion_list:
        raise HTTPException(status_code=400, detail="'normal' emotion is required")

    voice_dir = Path(VOICES_DIR) / character
    voice_dir.mkdir(parents=True, exist_ok=True)

    saved_emotions = {}

    for emotion_name in emotion_list:
        audio_field = f"audio_{emotion_name}"
        text_field = f"text_{emotion_name}"

        audio_file = locals().get(audio_field)
        if audio_file is None:
            continue

        ext = Path(audio_file.filename).suffix if audio_file.filename else ".wav"
        audio_path = voice_dir / f"{emotion_name}{ext}"
        with open(audio_path, "wb") as f:
            f.write(await audio_file.read())

        trim_msg = trim_audio_to_sentence_boundary(str(audio_path), **_get_trim_settings())
        ref_text = locals().get(text_field, "")
        if trim_msg:
            logger.info("Trimmed %s/%s: %s", character, emotion_name, trim_msg)
            ref_text = _transcribe_audio_file(str(audio_path))

        _normalize_audio(str(audio_path))

        text_path = voice_dir / f"{emotion_name}.txt"
        text_path.write_text(ref_text)

        saved_emotions[emotion_name] = {"ref_audio": str(audio_path), "ref_text": ref_text}

    if not saved_emotions:
        raise HTTPException(status_code=400, detail="No audio files were saved")

    voice_registry[character] = {
        "emotions": saved_emotions,
        "default_emotion": "normal",
    }
    logger.info(f"Registered character '{character}' with emotions: {list(saved_emotions.keys())}")
    return {
        "status": "ok",
        "character": character,
        "emotions": list(saved_emotions.keys()),
        "default_emotion": "normal",
    }


@app.get("/v1/audio/character/{name}")
def get_character(name: str):
    if name not in voice_registry:
        raise HTTPException(status_code=404, detail=f"Character '{name}' not found")
    data = voice_registry[name]
    return {
        "character": name,
        "emotions": list(data["emotions"].keys()),
        "default_emotion": data["default_emotion"],
    }


@app.get("/v1/audio/characters")
def list_characters():
    result = []
    for name, data in voice_registry.items():
        result.append({
            "character": name,
            "emotions": list(data["emotions"].keys()),
            "default_emotion": data["default_emotion"],
        })
    return {"characters": result}


@app.post("/v1/reload")
def reload_registry():
    global app_config, app_emotion_map, DEFAULT_NFE_STEP
    app_config = load_app_config()
    DEFAULT_NFE_STEP = app_config.get("default_nfe_step", 64)
    app_emotion_map = _build_emotion_map(app_config)
    if accentor is not None:
        _transcribe_empty_refs()
        _stress_ref_texts()
        custom_dict = app_config.get("custom_accent_dict", {}) or {}
        accentor.custom_dict = custom_dict
        accentor.accents.update(custom_dict)
        logger.info("Updated RUAccent custom dict with %d entries", len(custom_dict))
    _normalize_all_voices()
    load_voice_registry()
    logger.info("Config and voice registry reloaded from disk")
    return {"status": "ok", "characters": list(voice_registry.keys())}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "device": device,
        "model_loaded": model_obj is not None,
        "vocoder_loaded": vocoder is not None,
        "characters": list(voice_registry.keys()),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
