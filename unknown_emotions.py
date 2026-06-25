import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - this service runs on Linux, keep fallback import-safe.
    fcntl = None


CORE_EMOTIONS = ("normal", "calm", "happy", "sad", "aggressive", "scared")


def default_unknown_emotions_path(config_path: str | Path) -> Path:
    override = os.environ.get("F5_TTS_UNKNOWN_EMOTIONS_PATH")
    if override:
        return Path(override).expanduser()
    return Path(config_path).expanduser().with_name("unknown_emotion_aliases.json")


def normalize_emotion_word(word: str | None) -> str:
    return (word or "").strip().lower()


def known_emotion_words(config: dict) -> set[str]:
    words = {normalize_emotion_word(w) for w in CORE_EMOTIONS}
    for core, aliases in (config.get("emotion_map", {}) or {}).items():
        core_word = normalize_emotion_word(core)
        if core_word:
            words.add(core_word)
        for alias in aliases or []:
            alias_word = normalize_emotion_word(alias)
            if alias_word:
                words.add(alias_word)
    return words


def is_unknown_emotion_word(word: str | None, config: dict) -> bool:
    normalized = normalize_emotion_word(word)
    return bool(normalized and normalized not in known_emotion_words(config))


def append_emotion_alias(config: dict, core: str | None, alias: str | None) -> bool:
    core = normalize_emotion_word(core)
    alias = normalize_emotion_word(alias)
    if core not in CORE_EMOTIONS or not alias:
        return False

    emotion_map = config.setdefault("emotion_map", {})
    for existing_core, aliases in list(emotion_map.items()):
        cleaned = [normalize_emotion_word(a) for a in aliases or []]
        cleaned = [a for a in cleaned if a and a != alias]
        if cleaned:
            emotion_map[existing_core] = cleaned
        else:
            emotion_map.pop(existing_core, None)

    aliases = emotion_map.setdefault(core, [])
    if alias not in [normalize_emotion_word(a) for a in aliases]:
        aliases.append(alias)
    return True


def _empty_queue() -> dict:
    return {"pending": {}, "ignored": {}}


@contextmanager
def _locked_queue(path: str | Path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            queue = load_unknown_emotions(path)
            yield queue
            save_unknown_emotions(path, queue)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_unknown_emotions(path: str | Path) -> dict:
    path = Path(path).expanduser()
    if not path.exists():
        return _empty_queue()
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _empty_queue()

    pending = data.get("pending", {}) if isinstance(data, dict) else {}
    ignored = data.get("ignored", {}) if isinstance(data, dict) else {}
    return {
        "pending": pending if isinstance(pending, dict) else {},
        "ignored": ignored if isinstance(ignored, dict) else {},
    }


def save_unknown_emotions(path: str | Path, queue: dict) -> None:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(queue, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def record_unknown_emotion(
    path: str | Path,
    word: str | None,
    config: dict,
    voice: str | None = None,
    input_text: str | None = None,
) -> bool:
    normalized = normalize_emotion_word(word)
    if not is_unknown_emotion_word(normalized, config):
        return False

    now = datetime.now(timezone.utc).isoformat()
    excerpt = (input_text or "").strip().replace("\n", " ")[:240]

    with _locked_queue(path) as queue:
        ignored = queue.setdefault("ignored", {})
        if normalized in ignored:
            return False

        pending = queue.setdefault("pending", {})
        entry = pending.get(normalized) or {
            "word": normalized,
            "count": 0,
            "first_seen": now,
        }
        entry["count"] = int(entry.get("count", 0)) + 1
        entry.setdefault("first_seen", now)
        entry["last_seen"] = now
        if voice:
            entry["last_voice"] = voice
        if excerpt:
            entry["last_input_excerpt"] = excerpt
        pending[normalized] = entry
    return True


def resolve_unknown_emotion(path: str | Path, word: str | None, action: str) -> bool:
    normalized = normalize_emotion_word(word)
    if not normalized:
        return False
    with _locked_queue(path) as queue:
        pending = queue.setdefault("pending", {})
        ignored = queue.setdefault("ignored", {})
        existed = pending.pop(normalized, None) is not None
        if action == "ignore":
            ignored[normalized] = {
                "word": normalized,
                "ignored_at": datetime.now(timezone.utc).isoformat(),
            }
            return True
        return existed
