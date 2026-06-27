import hashlib
import io
import json
import logging
import os
import re
import threading
import time
import uuid
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response


logger = logging.getLogger("f5-tts-api")
router = APIRouter()

CHATTERBOX_DATA_FIELDS = (
    "model_choice",
    "text",
    "language",
    "speaker_audio",
    "prefix_audio",
    "emotion_1",
    "emotion_2",
    "emotion_3",
    "emotion_4",
    "emotion_5",
    "emotion_6",
    "emotion_7",
    "emotion_8",
    "vq_single",
    "fmax",
    "pitch_std",
    "speaking_rate",
    "dnsmos_overall",
    "speaker_noised",
    "cfg_scale",
    "top_p",
    "top_k",
    "min_p",
    "temperature",
    "repetition_penalty",
    "exaggeration",
    "seed",
    "entity_uuid",
    "randomize_seed",
    "unconditional_keys",
)

UPLOAD_ROOT = Path(os.environ.get(
    "F5_CHATTERBOX_UPLOAD_DIR",
    "/workspace/f5-tts-cache/chatterbox_uploads",
))
GENERATED_AUDIO_NAME = "chatterbox-f5.wav"
PROBE_AUDIO_NAME = "chatterbox-probe-silence.wav"
PROBE_AUDIO_BYTES: bytes
AudioGenerator = Callable[[dict[str, Any]], Awaitable[bytes]]
_audio_generator: AudioGenerator | None = None

CHATTERBOX_TAG_EMOTIONS = {
    "angry": "aggressive",
    "fear": "scared",
    "surprised": "scared",
    "whispering": "calm",
    "advertisement": "normal",
    "dramatic": "aggressive",
    "narration": "normal",
    "happy": "happy",
    "sarcastic": "aggressive",
    "crying": "sad",
    "sigh": "sad",
    "shush": "calm",
    "groan": "sad",
    "sniff": "sad",
    "gasp": "scared",
    "chuckle": "happy",
    "laugh": "happy",
}


def _make_silence_wav(duration_ms: int = 100, sample_rate: int = 24000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * (sample_rate * duration_ms // 1000))
    return output.getvalue()


PROBE_AUDIO_BYTES = _make_silence_wav()


def set_audio_generator(generator: AudioGenerator | None) -> None:
    global _audio_generator
    _audio_generator = generator


def decode_chatterbox_data(data: list[Any]) -> dict[str, Any]:
    decoded = {
        field: data[index] if index < len(data) else None
        for index, field in enumerate(CHATTERBOX_DATA_FIELDS)
    }
    if len(data) > len(CHATTERBOX_DATA_FIELDS):
        decoded["extra_fields"] = data[len(CHATTERBOX_DATA_FIELDS):]
    return decoded


def parse_chatterbox_tags(
    text: str,
    emotion_aliases: dict[str, str] | None = None,
) -> tuple[str | None, str, list[str]]:
    tags = [match.strip().lower() for match in re.findall(r"\[([^\]]+)\]", text)]
    aliases = emotion_aliases or {}
    emotion = None
    for tag in tags:
        mapped = aliases.get(tag, CHATTERBOX_TAG_EMOTIONS.get(tag))
        if mapped:
            emotion = mapped
            break
    cleaned = re.sub(r"\[([^\]]+)\]", "", text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned).strip()
    return emotion, cleaned, tags


class ChatterboxProbeState:
    def __init__(self, max_events: int = 256, ttl_seconds: float = 600.0):
        self.max_events = max_events
        self.ttl_seconds = ttl_seconds
        self._events: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def create_event(self, raw_body: dict[str, Any], data: list[Any]) -> tuple[str, dict[str, Any]]:
        event_id = uuid.uuid4().hex
        decoded = decode_chatterbox_data(data)
        event = {
            "created_at": time.time(),
            "raw_body": raw_body,
            "data": data,
            "decoded": decoded,
        }
        with self._lock:
            self._prune_locked()
            self._events[event_id] = event
            while len(self._events) > self.max_events:
                self._events.popitem(last=False)
        return event_id, event

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._prune_locked()
            return self._events.get(event_id)

    def set_audio(self, event_id: str, audio: bytes) -> None:
        with self._lock:
            event = self._events.get(event_id)
            if event is not None:
                event["audio"] = audio

    def _prune_locked(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        stale = [
            event_id
            for event_id, event in self._events.items()
            if event["created_at"] < cutoff
        ]
        for event_id in stale:
            self._events.pop(event_id, None)


probe_state = ChatterboxProbeState()


def _safe_upload_name(filename: str | None) -> str:
    name = Path(filename or "voice-sample.wav").name
    return name or "voice-sample.wav"


def resolve_uploaded_file(gradio_path: str | None) -> Path | None:
    prefix = "/tmp/gradio/"
    if not gradio_path or not gradio_path.startswith(prefix):
        return None
    relative = Path(gradio_path[len(prefix):])
    parts = relative.parts
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
        return None
    local_path = UPLOAD_ROOT / parts[0] / parts[1]
    return local_path if local_path.is_file() else None


@router.post("/gradio_api/upload")
async def chatterbox_probe_upload(files: list[UploadFile] = File(...)):
    uploaded_paths = []
    for upload in files:
        content = await upload.read()
        digest = hashlib.sha256(content).hexdigest()
        filename = _safe_upload_name(upload.filename)
        gradio_path = f"/tmp/gradio/{digest}/{filename}"
        local_path = UPLOAD_ROOT / digest / filename
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if not local_path.exists():
            local_path.write_bytes(content)
        uploaded_paths.append(gradio_path)
        logger.info(
            "Chatterbox API upload filename=%s content_type=%s bytes=%d sha256=%s path=%s",
            filename,
            upload.content_type or "",
            len(content),
            digest,
            gradio_path,
        )
    return JSONResponse(uploaded_paths)


@router.post("/gradio_api/call/generate_audio")
async def chatterbox_probe_submit(request: Request):
    raw_body = await request.json()
    data = raw_body.get("data") if isinstance(raw_body, dict) else None
    if not isinstance(data, list):
        raise HTTPException(status_code=422, detail="Chatterbox payload must contain a 'data' array")

    event_id, event = probe_state.create_event(raw_body, data)
    decoded = event["decoded"]
    text = decoded.get("text")
    tags = re.findall(r"\[([^\]]+)\]", text) if isinstance(text, str) else []
    logger.info(
        "Chatterbox API submit event_id=%s data_fields=%d text=%r tags=%s language=%r speaker_audio=%s",
        event_id,
        len(data),
        text,
        tags,
        decoded.get("language"),
        json.dumps(decoded.get("speaker_audio"), ensure_ascii=False, default=str),
    )
    logger.info(
        "Chatterbox API decoded event_id=%s payload=%s",
        event_id,
        json.dumps(decoded, ensure_ascii=False, default=str),
    )
    logger.info(
        "Chatterbox API raw event_id=%s body=%s",
        event_id,
        json.dumps(raw_body, ensure_ascii=False, default=str),
    )
    return {"event_id": event_id}


@router.get("/gradio_api/call/generate_audio/{event_id}")
async def chatterbox_probe_result(event_id: str, request: Request):
    event = probe_state.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Unknown or expired Chatterbox event")

    seed = event["decoded"].get("seed")
    audio = event.get("audio")
    if audio is None:
        if _audio_generator is None:
            audio = PROBE_AUDIO_BYTES
        else:
            try:
                audio = await _audio_generator(event["decoded"])
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("Chatterbox F5 generation failed event_id=%s", event_id)
                raise HTTPException(status_code=500, detail=f"F5 generation failed: {exc}") from exc
        probe_state.set_audio(event_id, audio)

    file_path = f"/tmp/gradio/chatterbox-f5/{event_id}/{GENERATED_AUDIO_NAME}"
    file_url = f"{str(request.base_url).rstrip('/')}/gradio_api/file={file_path}"
    result = [
        {
            "path": file_path,
            "url": file_url,
            "size": len(audio),
            "orig_name": GENERATED_AUDIO_NAME,
            "mime_type": "audio/wav",
            "is_stream": False,
            "meta": {"_type": "gradio.FileData"},
        },
        seed,
    ]
    logger.info(
        "Chatterbox API result event_id=%s seed=%r bytes=%d file=%s",
        event_id,
        seed,
        len(audio),
        file_path,
    )
    body = f"event: complete\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
    return Response(
        content=body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/gradio_api/file={file_path:path}")
async def chatterbox_probe_file(file_path: str):
    audio = None
    if file_path.startswith("/tmp/gradio/chatterbox-f5/"):
        parts = Path(file_path).parts
        try:
            event_id = parts[4]
        except IndexError:
            event_id = ""
        event = probe_state.get_event(event_id)
        if event is not None:
            audio = event.get("audio")
    else:
        uploaded = resolve_uploaded_file(file_path)
        if uploaded is not None:
            audio = uploaded.read_bytes()

    if audio is None:
        raise HTTPException(status_code=404, detail="Unknown Chatterbox audio file")

    logger.info(
        "Chatterbox API file path=%s bytes=%d",
        file_path,
        len(audio),
    )
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"Content-Disposition": f'inline; filename="{Path(file_path).name}"'},
    )
