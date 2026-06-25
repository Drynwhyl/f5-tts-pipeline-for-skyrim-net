import re
from dataclasses import dataclass
from typing import Any

import numpy as np
import torch
import torchaudio
import tqdm

from f5_tts.infer.utils_infer import (
    cfg_strength as default_cfg_strength,
    cross_fade_duration as default_cross_fade_duration,
    fix_duration as default_fix_duration,
    hop_length,
    mel_spec_type as default_mel_spec_type,
    nfe_step as default_nfe_step,
    speed as default_speed,
    sway_sampling_coef as default_sway_sampling_coef,
    target_rms as default_target_rms,
    target_sample_rate,
)
from f5_tts.model.utils import convert_char_to_pinyin


DEFAULT_SEMANTIC_CHUNKING = {
    "enabled": True,
    "target_total_sec": 26.0,
    "hard_total_sec": 29.0,
    "max_gen_budget_sec": 10.0,
    "min_chunk_sec": 1.4,
    "duration_margin": 1.15,
    "frame_margin": 1.10,
    "ref_guard_enabled": True,
    "ref_guard_silence_ms": 300,
    "ref_guard_speed_scale_ms": 500,
    "ref_guard_max_silence_ms": 900,
    "ref_tail_quarantine_enabled": False,
    "ref_tail_max_units": 40,
    "ref_tail_min_silence_ms": 150,
    "ref_tail_keep_silence_ms": 200,
    "ref_tail_max_removed_ms": 4500,
    "ref_tail_clause_quarantine_enabled": False,
    "ref_tail_clause_min_speed": 1.05,
    "ref_tail_clause_max_units": 36,
    "ref_tail_clause_min_remaining_units": 35,
    "ref_tail_clause_max_removed_ms": 5500,
    "weak_start_merge_enabled": True,
    "weak_start_merge_slack_sec": 0.35,
    "weak_start_words": [
        "хотя",
        "что",
        "чтобы",
        "если",
        "когда",
        "пока",
        "потому",
        "который",
        "которая",
        "которое",
        "которые",
    ],
    "generated_trim": {
        "enabled": True,
        "leading_keep_ms": 300,
        "trailing_keep_ms": 160,
        "silence_thresh_db": -50,
    },
    "punctuation": {
        "comma": 0.20,
        "semicolon": 0.30,
        "colon": 0.30,
        "dash": 0.22,
        "sentence": 0.40,
        "ellipsis": 0.55,
    },
    "comma_softening": {
        "enabled": True,
        "vocative_enabled": True,
        "decorative_tail_enabled": True,
        "decorative_tails": [
            "блин",
            "черт",
            "чёрт",
            "конечно",
            "наверное",
            "пожалуй",
            "что ли",
            "да",
            "нет",
            "ладно",
        ],
    },
}


@dataclass
class ChunkPlan:
    text: str
    raw_text: str
    estimated_sec: float
    frame_sec: float
    budget_sec: float
    speech_units: float
    punctuation_pause_sec: float
    total_sec: float
    frame_total_sec: float
    budget_total_sec: float
    extra_frames: int
    reason: str


def semantic_defaults() -> dict[str, Any]:
    return _deep_copy(DEFAULT_SEMANTIC_CHUNKING)


def merge_semantic_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = semantic_defaults()
    if not config:
        return merged
    _deep_update(merged, config)
    return merged


def soften_commas_for_tts(text: str, cfg: dict[str, Any]) -> str:
    soft = cfg.get("comma_softening", {}) or {}
    if not soft.get("enabled", True):
        return text

    result = text
    if soft.get("vocative_enabled", True):
        result = _soften_initial_vocative(result)

    if soft.get("decorative_tail_enabled", True):
        tails = [str(t).strip() for t in soft.get("decorative_tails", []) if str(t).strip()]
        if tails:
            result = _soften_decorative_tail(result, tails)

    return result


def build_chunk_plan(text: str, ref_text: str, ref_duration_sec: float, config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = merge_semantic_config(config)
    processed_text = soften_commas_for_tts(text.strip(), cfg)
    ref_units = max(_speech_units(ref_text), 1.0)
    guard_sec = 0.0
    if cfg.get("ref_guard_enabled", True):
        guard_sec = max(0, int(cfg.get("effective_ref_guard_silence_ms", cfg.get("ref_guard_silence_ms", 300)))) / 1000.0
    ref_speech_duration_sec = max(ref_duration_sec - guard_sec, 0.1)
    ref_units_per_sec = ref_units / ref_speech_duration_sec

    max_gen_budget_sec = max(cfg["min_chunk_sec"], float(cfg.get("max_gen_budget_sec", 10.0)))
    target_gen_sec = min(max_gen_budget_sec, max(cfg["min_chunk_sec"], cfg["target_total_sec"] - ref_duration_sec))
    hard_gen_sec = min(max_gen_budget_sec, max(target_gen_sec, cfg["hard_total_sec"] - ref_duration_sec))

    chunks = _split_text(processed_text, ref_units_per_sec, target_gen_sec, hard_gen_sec, cfg)
    plans: list[ChunkPlan] = []
    for chunk_text, reason in chunks:
        metrics = _estimate_text(chunk_text, ref_units_per_sec, cfg)
        extra_frames = int(metrics["frame_sec"] * target_sample_rate / hop_length)
        plans.append(
            ChunkPlan(
                text=chunk_text,
                raw_text=chunk_text,
                estimated_sec=metrics["estimated_sec"],
                frame_sec=metrics["frame_sec"],
                budget_sec=metrics["budget_sec"],
                speech_units=metrics["speech_units"],
                punctuation_pause_sec=metrics["punctuation_pause_sec"],
                total_sec=ref_duration_sec + metrics["estimated_sec"],
                frame_total_sec=ref_duration_sec + metrics["frame_sec"],
                budget_total_sec=ref_duration_sec + metrics["budget_sec"],
                extra_frames=extra_frames,
                reason=reason,
            )
        )

    return {
        "enabled": cfg.get("enabled", True),
        "processed_text": processed_text,
        "ref_duration_sec": ref_duration_sec,
        "ref_speech_duration_sec": ref_speech_duration_sec,
        "ref_units": ref_units,
        "ref_units_per_sec": ref_units_per_sec,
        "target_total_sec": cfg["target_total_sec"],
        "hard_total_sec": cfg["hard_total_sec"],
        "max_gen_budget_sec": max_gen_budget_sec,
        "frame_margin": float(cfg.get("frame_margin", 1.0)),
        "ref_guard_enabled": cfg.get("ref_guard_enabled", True),
        "base_ref_guard_silence_ms": int(cfg.get("ref_guard_silence_ms", 300)),
        "ref_guard_silence_ms": int(cfg.get("effective_ref_guard_silence_ms", cfg.get("ref_guard_silence_ms", 300))),
        "generated_trim": cfg.get("generated_trim", {}),
        "chunks": [p.__dict__ for p in plans],
    }


def infer_process_semantic(
    ref_audio,
    ref_text,
    gen_text,
    model_obj,
    vocoder,
    semantic_config=None,
    mel_spec_type=default_mel_spec_type,
    show_info=print,
    progress=tqdm,
    target_rms=default_target_rms,
    cross_fade_duration=default_cross_fade_duration,
    nfe_step=default_nfe_step,
    cfg_strength=default_cfg_strength,
    sway_sampling_coef=default_sway_sampling_coef,
    speed=default_speed,
    fix_duration=default_fix_duration,
    device=None,
    gpu_task_runner=None,
):
    audio, sr = torchaudio.load(ref_audio)
    ref_duration_sec = audio.shape[-1] / sr
    plan = build_chunk_plan(gen_text, ref_text, ref_duration_sec, semantic_config)
    chunks = plan["chunks"]

    for i, chunk in enumerate(chunks):
        print(
            "gen_text %d %.2fs total=%.2fs frames=%d reason=%s %s"
            % (i, chunk["frame_sec"], chunk["frame_total_sec"], chunk["extra_frames"], chunk["reason"], chunk["text"])
        )
    print("\n")

    show_info(f"Generating audio in {len(chunks)} semantic batches...")
    if not chunks:
        show_info("No text batches to generate.")
        return None, target_sample_rate, None

    return next(
        _infer_batch_process_semantic(
            (audio, sr),
            ref_text,
            chunks,
            model_obj,
            vocoder,
            mel_spec_type=mel_spec_type,
            progress=progress,
            target_rms=target_rms,
            cross_fade_duration=cross_fade_duration,
            nfe_step=nfe_step,
            cfg_strength=cfg_strength,
            sway_sampling_coef=sway_sampling_coef,
            speed=speed,
            fix_duration=fix_duration,
            device=device,
            semantic_config=semantic_config,
            gpu_task_runner=gpu_task_runner,
        )
    )


def _infer_batch_process_semantic(
    ref_audio,
    ref_text,
    chunk_plans,
    model_obj,
    vocoder,
    mel_spec_type="vocos",
    progress=tqdm,
    target_rms=0.1,
    cross_fade_duration=0.15,
    nfe_step=32,
    cfg_strength=2.0,
    sway_sampling_coef=-1,
    speed=1,
    fix_duration=None,
    device=None,
    semantic_config=None,
    gpu_task_runner=None,
):
    cfg = merge_semantic_config(semantic_config)
    audio, sr = ref_audio
    if audio.shape[0] > 1:
        audio = torch.mean(audio, dim=0, keepdim=True)

    rms = torch.sqrt(torch.mean(torch.square(audio)))
    if rms < target_rms:
        audio = audio * target_rms / rms
    if sr != target_sample_rate:
        resampler = torchaudio.transforms.Resample(sr, target_sample_rate)
        audio = resampler(audio)
    audio = audio.to(device)

    if len(ref_text[-1].encode("utf-8")) == 1:
        ref_text = ref_text + " "

    generated_waves = []
    spectrograms = []

    def _infer_basic(chunk):
        gen_text = chunk["text"]
        local_speed = speed
        if len(gen_text.encode("utf-8")) < 10:
            local_speed = 0.3

        text_list = [ref_text + gen_text]
        final_text_list = convert_char_to_pinyin(text_list)
        ref_audio_len = audio.shape[-1] // hop_length
        if fix_duration is not None:
            duration = int(fix_duration * target_sample_rate / hop_length)
        else:
            extra_frames = int(chunk["extra_frames"] / local_speed)
            duration = ref_audio_len + extra_frames

        def _infer_gpu():
            with torch.inference_mode():
                generated, _ = model_obj.sample(
                    cond=audio,
                    text=final_text_list,
                    duration=duration,
                    steps=nfe_step,
                    cfg_strength=cfg_strength,
                    sway_sampling_coef=sway_sampling_coef,
                )
                del _
                generated = generated.to(torch.float32)
                generated = generated[:, ref_audio_len:, :]
                generated = generated.permute(0, 2, 1)
                if mel_spec_type == "vocos":
                    generated_wave = vocoder.decode(generated)
                else:
                    generated_wave = vocoder(generated)
                if rms < target_rms:
                    generated_wave = generated_wave * rms / target_rms
                generated_wave = generated_wave.squeeze().cpu().numpy()
                generated_wave = _trim_generated_edges(generated_wave, target_sample_rate, cfg)
                generated_cpu = generated[0].cpu().numpy()
                del generated
                return generated_wave, generated_cpu

        if gpu_task_runner is not None:
            return gpu_task_runner(_infer_gpu)
        return _infer_gpu()

    items = progress.tqdm(chunk_plans) if progress is not None else chunk_plans
    for chunk in items:
        result = _infer_basic(chunk)
        if result:
            generated_wave, generated_mel_spec = result
            generated_waves.append(generated_wave)
            spectrograms.append(generated_mel_spec)

    if not generated_waves:
        yield None, target_sample_rate, None
        return

    if cross_fade_duration <= 0:
        final_wave = np.concatenate(generated_waves)
    else:
        final_wave = generated_waves[0]
        for i in range(1, len(generated_waves)):
            prev_wave = final_wave
            next_wave = generated_waves[i]
            cross_fade_samples = int(cross_fade_duration * target_sample_rate)
            cross_fade_samples = min(cross_fade_samples, len(prev_wave), len(next_wave))
            if cross_fade_samples <= 0:
                final_wave = np.concatenate([prev_wave, next_wave])
                continue

            fade_out = np.linspace(1, 0, cross_fade_samples)
            fade_in = np.linspace(0, 1, cross_fade_samples)
            cross_faded_overlap = prev_wave[-cross_fade_samples:] * fade_out + next_wave[:cross_fade_samples] * fade_in
            final_wave = np.concatenate(
                [prev_wave[:-cross_fade_samples], cross_faded_overlap, next_wave[cross_fade_samples:]]
            )

    combined_spectrogram = np.concatenate(spectrograms, axis=1)
    yield final_wave, target_sample_rate, combined_spectrogram


def _split_text(text: str, ref_units_per_sec: float, target_sec: float, hard_sec: float, cfg: dict[str, Any]):
    text = text.strip()
    if not text:
        return []

    chunks = []
    start = 0
    while start < len(text):
        end, reason = _choose_boundary(text, start, ref_units_per_sec, target_sec, hard_sec, cfg)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((chunk, reason))
        start = end
        while start < len(text) and text[start].isspace():
            start += 1

    chunks = _merge_short_chunks(chunks, ref_units_per_sec, hard_sec, cfg)
    return _merge_weak_start_chunks(chunks, ref_units_per_sec, hard_sec, cfg)


def _choose_boundary(text: str, start: int, ref_units_per_sec: float, target_sec: float, hard_sec: float, cfg: dict[str, Any]):
    remainder = text[start:].strip()
    remainder_metrics = _estimate_text(remainder, ref_units_per_sec, cfg)
    remainder_budget = remainder_metrics["budget_sec"]
    if remainder_budget <= target_sec:
        return len(text), "remainder_fits_target"

    candidates = _candidate_boundaries(text, start)
    viable = []
    for pos, strength, reason in candidates:
        if pos <= start:
            continue
        segment = text[start:pos].strip()
        if not segment:
            continue
        estimate = _estimate_text(segment, ref_units_per_sec, cfg)["budget_sec"]
        tail = text[pos:].strip()
        tail_est = _estimate_text(tail, ref_units_per_sec, cfg)["budget_sec"] if tail else 0.0
        if estimate <= hard_sec and (tail_est == 0.0 or tail_est >= cfg.get("min_chunk_sec", 1.4)):
            viable.append((pos, strength, reason, estimate))

    under_target = [c for c in viable if c[3] <= target_sec]
    if under_target:
        return max(under_target, key=lambda c: (c[3], c[1]))[0], max(under_target, key=lambda c: (c[3], c[1]))[2]
    if viable:
        return max(viable, key=lambda c: (c[1], -abs(c[3] - target_sec)))[0], max(viable, key=lambda c: (c[1], -abs(c[3] - target_sec)))[2]

    if remainder_budget <= hard_sec:
        return len(text), "remainder_fits_hard"

    return _fallback_word_boundary(text, start, ref_units_per_sec, target_sec, hard_sec, cfg), "word_fallback"


def _candidate_boundaries(text: str, start: int):
    candidates = []
    for match in re.finditer(r"[.!?]+|[;:]|,|[—–-]", text[start:]):
        punct_start = start + match.start()
        punct_end = start + match.end()
        while punct_end < len(text) and text[punct_end].isspace():
            punct_end += 1

        punct = match.group(0)
        if "," in punct:
            if _protected_comma(text, start, punct_start):
                continue
            candidates.append((punct_end, 2, "comma"))
        elif punct in (";", ":"):
            candidates.append((punct_end, 4, "semicolon_or_colon"))
        elif punct in ("—", "–", "-"):
            candidates.append((punct_end, 1, "dash"))
        else:
            candidates.append((punct_end, 5, "sentence_end"))
    return candidates


def _protected_comma(text: str, start: int, comma_pos: int) -> bool:
    before = text[start:comma_pos].strip()
    after = text[comma_pos + 1 :].strip()
    before_words = re.findall(r"[A-Za-zА-Яа-яЁё]+", before)
    after_words = re.findall(r"[A-Za-zА-Яа-яЁё]+", after[:40])
    if len(before_words) <= 2 and len(before) <= 24:
        return True
    if after_words:
        tail = " ".join(after_words[:2]).lower()
        if tail in {"блин", "черт", "чёрт", "конечно", "наверное", "пожалуй", "что ли", "да", "нет", "ладно"}:
            return True
    return False


def _soften_initial_vocative(text: str) -> str:
    match = re.match(r"^(\+?[А-ЯЁA-Z][а-яёa-z+\-]{1,28}),\s+(.{3,})$", text)
    if not match:
        return text
    word = match.group(1).replace("+", "").lower()
    stopwords = {
        "да",
        "нет",
        "когда",
        "если",
        "хотя",
        "пока",
        "потому",
        "однако",
        "но",
        "и",
        "а",
        "что",
        "как",
        "где",
        "зачем",
        "почему",
        "видишь",
        "слушай",
    }
    if word in stopwords:
        return text
    return f"{match.group(1)} {match.group(2)}"


def _soften_decorative_tail(text: str, tails: list[str]) -> str:
    match = re.search(r",\s+([^,.!?;:]+)([.!?])$", text)
    if not match:
        return text
    tail = match.group(1).replace("+", "").strip().lower()
    allowed = {t.lower() for t in tails}
    if tail not in allowed:
        return text
    return text[: match.start()] + " " + match.group(1).strip() + match.group(2)


def _fallback_word_boundary(text: str, start: int, ref_units_per_sec: float, target_sec: float, hard_sec: float, cfg: dict[str, Any]):
    spaces = [m.end() for m in re.finditer(r"\s+", text[start:])]
    if not spaces:
        return len(text)
    absolute = [start + p for p in spaces]
    under_hard = [p for p in absolute if _estimate_text(text[start:p].strip(), ref_units_per_sec, cfg)["budget_sec"] <= hard_sec]
    under_target = [p for p in under_hard if _estimate_text(text[start:p].strip(), ref_units_per_sec, cfg)["budget_sec"] <= target_sec]
    if under_target:
        return max(under_target, key=lambda p: _estimate_text(text[start:p].strip(), ref_units_per_sec, cfg)["budget_sec"])
    if under_hard:
        return max(under_hard, key=lambda p: _estimate_text(text[start:p].strip(), ref_units_per_sec, cfg)["budget_sec"])
    return absolute[0]


def _merge_short_chunks(chunks, ref_units_per_sec: float, hard_sec: float, cfg: dict[str, Any]):
    if len(chunks) < 2:
        return chunks
    min_sec = cfg.get("min_chunk_sec", 1.4)
    merged = []
    for chunk, reason in chunks:
        if not merged:
            merged.append((chunk, reason))
            continue
        estimate = _estimate_text(chunk, ref_units_per_sec, cfg)["estimated_sec"]
        prev = merged[-1][0]
        joined = (prev + " " + chunk).strip()
        joined_est = _estimate_text(joined, ref_units_per_sec, cfg)["budget_sec"]
        if estimate < min_sec and joined_est <= hard_sec:
            merged[-1] = (joined, "merged_short_chunk")
        else:
            merged.append((chunk, reason))
    return merged


def _merge_weak_start_chunks(chunks, ref_units_per_sec: float, hard_sec: float, cfg: dict[str, Any]):
    if len(chunks) < 2 or not cfg.get("weak_start_merge_enabled", True):
        return chunks
    weak_words = {str(w).replace("+", "").lower() for w in cfg.get("weak_start_words", [])}
    slack = max(0.0, float(cfg.get("weak_start_merge_slack_sec", 0.35)))
    merged = []
    for chunk, reason in chunks:
        if not merged:
            merged.append((chunk, reason))
            continue

        first_word_match = re.search(r"[A-Za-zА-Яа-яЁё+]+", chunk)
        first_word = first_word_match.group(0).replace("+", "").lower() if first_word_match else ""
        prev = merged[-1][0]
        joined = (prev + " " + chunk).strip()
        joined_budget = _estimate_text(joined, ref_units_per_sec, cfg)["budget_sec"]
        if first_word in weak_words and joined_budget <= hard_sec + slack:
            merged[-1] = (joined, "merged_weak_start")
        else:
            merged.append((chunk, reason))
    return merged


def _estimate_text(text: str, ref_units_per_sec: float, cfg: dict[str, Any]):
    units = _speech_units(text)
    pause = _punctuation_pause(text, cfg)
    estimated_sec = units / max(ref_units_per_sec, 0.1) + pause
    frame_sec = max(2.0, estimated_sec * float(cfg.get("frame_margin", 1.0)))
    budget_sec = max(2.0, estimated_sec * float(cfg.get("duration_margin", 1.15)))
    return {
        "speech_units": units,
        "punctuation_pause_sec": pause,
        "estimated_sec": estimated_sec,
        "frame_sec": frame_sec,
        "budget_sec": budget_sec,
    }


def _trim_generated_edges(wave: np.ndarray, sample_rate: int, cfg: dict[str, Any]) -> np.ndarray:
    trim_cfg = cfg.get("generated_trim", {}) or {}
    if not trim_cfg.get("enabled", True) or wave.size == 0:
        return wave

    threshold_db = float(trim_cfg.get("silence_thresh_db", -50))
    threshold = 10 ** (threshold_db / 20.0)
    voiced = np.flatnonzero(np.abs(wave) > threshold)
    if voiced.size == 0:
        return wave

    leading_keep = int(max(0, int(trim_cfg.get("leading_keep_ms", 80))) * sample_rate / 1000)
    trailing_keep = int(max(0, int(trim_cfg.get("trailing_keep_ms", 160))) * sample_rate / 1000)
    start = max(0, int(voiced[0]) - leading_keep)
    end = min(wave.size, int(voiced[-1]) + trailing_keep + 1)
    if start == 0 and end == wave.size:
        return wave
    return wave[start:end]


def _speech_units(text: str) -> float:
    clean = text.replace("+", "")
    return float(len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]", clean)))


def _punctuation_pause(text: str, cfg: dict[str, Any]) -> float:
    p = cfg.get("punctuation", {}) or {}
    total = 0.0
    total += len(re.findall(r"\.{2,}|…", text)) * float(p.get("ellipsis", 0.55))
    total += text.count(",") * float(p.get("comma", 0.20))
    total += text.count(";") * float(p.get("semicolon", 0.30))
    total += text.count(":") * float(p.get("colon", 0.30))
    total += len(re.findall(r"[—–-]", text)) * float(p.get("dash", 0.22))
    total += len(re.findall(r"(?<!\.)[.!?](?!\.)", text)) * float(p.get("sentence", 0.40))
    return total


def _deep_copy(value):
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def _deep_update(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
