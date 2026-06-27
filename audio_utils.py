import logging
import tempfile
from pathlib import Path

import numpy as np

logger = logging.getLogger("f5-tts-audio")

DEFAULT_MAX_MS = 12000
DEFAULT_MIN_SILENCE_LEN = 1000
DEFAULT_SILENCE_THRESH = -50
DEFAULT_KEEP_SILENCE = 500
DEFAULT_NORMALIZATION_TARGET = -28
DEFAULT_NORMALIZE_ON_FLY = True
DEFAULT_SPECTRAL_PENALTY = True


def trim_audio_to_sentence_boundary(
    audio_path: str,
    max_ms: int = DEFAULT_MAX_MS,
    min_silence_len: int = DEFAULT_MIN_SILENCE_LEN,
    silence_thresh: int = DEFAULT_SILENCE_THRESH,
    keep_silence: int = DEFAULT_KEEP_SILENCE,
) -> str:
    """Trim audio to ≤max_ms, finding sentence boundaries via silence detection.

    Parameters mirror pydub.silence.split_on_silence():
      min_silence_len  — minimum length of silence (ms) to be considered a break
      silence_thresh   — silence threshold in dBFS (e.g. -50)
      keep_silence     — how much silence (ms) to keep at segment boundaries

    Saves result in-place (overwrites original file).
    Returns empty string if no trimming occurred, or a description like
    'Audio trimmed from 45.2s to 11.8s'.
    """
    try:
        from pydub import AudioSegment, silence

        with open(audio_path, "rb") as source:
            audio = AudioSegment.from_file(source)
        orig_len = len(audio)

        if orig_len <= max_ms:
            return ""

        segments = silence.split_on_silence(
            audio,
            min_silence_len=min_silence_len,
            silence_thresh=silence_thresh,
            keep_silence=keep_silence,
            seek_step=10,
        )

        if not segments:
            segments = [audio]

        trimmed = AudioSegment.silent(duration=0)
        for seg in segments:
            if len(trimmed) + len(seg) > max_ms:
                break
            trimmed += seg

        if len(trimmed) == 0:
            trimmed = audio[:max_ms]
        elif len(trimmed) > max_ms:
            trimmed = trimmed[:max_ms]

        if len(trimmed) >= orig_len:
            return ""

        min_useful_ms = min(max_ms, max(3000, max_ms // 2))
        if len(trimmed) < min_useful_ms:
            trimmed = audio[:max_ms]

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            with open(tmp_path, "wb") as output:
                trimmed.export(output, format="wav")
            Path(tmp_path).replace(audio_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        orig_s = orig_len / 1000.0
        new_s = len(trimmed) / 1000.0
        msg = f"Audio trimmed from {orig_s:.1f}s to {new_s:.1f}s"
        logger.info("%s: %s", audio_path, msg)
        return msg

    except Exception as e:
        logger.warning("Failed to trim audio '%s': %s", audio_path, e)
        return ""


def _band_rms_dbfs(samples: np.ndarray, sr: int, lo_hz: int, hi_hz: int) -> float:
    """Band-limited RMS in dBFS for a float sample array (-1..1)."""
    from scipy.signal import butter, sosfilt
    nyq = sr / 2
    lo = max(1, lo_hz)
    hi = min(hi_hz, nyq - 10)
    if lo >= hi:
        return -float("inf")
    sos = butter(4, [lo / nyq, hi / nyq], btype="band", output="sos")
    filtered = sosfilt(sos, samples.astype(np.float64))
    rms = np.sqrt(np.mean(filtered ** 2))
    return 20.0 * np.log10(rms + 1e-12)


def _compute_spectral_penalty(samples: np.ndarray, sr: int) -> float:
    """Harshness penalty based on mid-to-bass energy ratio.

    Positive mid-bass ratio means the file is mid-forward (harsh/bright).
    Returns penalty in dB (0 = neutral, >0 = reduce gain).
    """
    bass = _band_rms_dbfs(samples, sr, 20, 500)
    mid = _band_rms_dbfs(samples, sr, 500, 4000)
    mid_bass = mid - bass
    penalty = max(0.0, mid_bass)
    return penalty


def normalize_loudness(
    audio_path: str,
    target_dbfs: float = DEFAULT_NORMALIZATION_TARGET,
    spectral_penalty: bool = DEFAULT_SPECTRAL_PENALTY,
    max_gain: float = 12.0,
) -> str:
    """Normalize audio RMS loudness with optional spectral-balance correction.

    Uses flat RMS gain + optional harshness penalty based on mid-to-bass ratio.
    Files that are mid-forward (bright/harsh) get extra gain reduction.

    Saves result in-place (overwrites original file).
    Returns description like 'Normalized from -20.3 dBFS to -28.0 dBFS
    (spectral penalty -2.1 dB)',
    or empty string if no change needed.
    """
    try:
        from pydub import AudioSegment

        audio = AudioSegment.from_file(audio_path)
        current = audio.dBFS

        gain = target_dbfs - current
        details = ""

        if spectral_penalty:
            raw = np.array(audio.get_array_of_samples(), dtype=np.float64)
            if audio.channels > 1:
                raw = raw.reshape((-1, audio.channels)).mean(axis=1)
            max_val = 2 ** (8 * audio.sample_width - 1)
            samples = raw / max_val

            penalty = _compute_spectral_penalty(samples, audio.frame_rate)
            if penalty > 0:
                gain = gain - penalty
                details = f" (spectral penalty -{penalty:.1f} dB)"

        gain = max(-max_gain, min(gain, max_gain))

        if abs(gain) < 0.5:
            return ""

        normalized = audio.apply_gain(gain)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            normalized.export(tmp_path, format="wav")
            Path(tmp_path).replace(audio_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        msg = f"Normalized from {current:.1f} dBFS to {target_dbfs:.1f} dBFS ({gain:+.1f} dB){details}"
        logger.info("%s: %s", audio_path, msg)
        return msg

    except Exception as e:
        logger.warning("Failed to normalize audio '%s': %s", audio_path, e)
        return ""
