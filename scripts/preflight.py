#!/usr/bin/env python3
"""Fast migration preflight checks for the Vast.ai runtime."""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"preflight failed: {message}")


def module_text(module_name: str) -> str:
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        fail(f"cannot find module {module_name}")
    return Path(spec.origin).read_text()


def check_paths() -> None:
    base = Path(os.environ.get("F5_TTS_BASE_DIR", "/workspace/f5-tts"))
    model_dir = Path(os.environ.get("F5_TTS_MODEL_DIR", str(base / "F5TTS_v1_Base_v2")))
    voices_dir = Path(os.environ.get("F5_TTS_VOICES_DIR", str(base / "voices")))
    config_path = Path(os.environ.get("F5_TTS_CONFIG_PATH", str(base / "config.json")))
    cache_dir = Path(os.environ.get("F5_TTS_CACHE_DIR", "/workspace/f5-tts-cache"))
    ruaccent_dir = Path(os.environ.get("F5_TTS_RUACCENT_DIR", str(base / "ruaccent-data")))

    for path in (cache_dir, ruaccent_dir):
        path.mkdir(parents=True, exist_ok=True)
    for path in (base, voices_dir, cache_dir, ruaccent_dir):
        if not path.exists():
            fail(f"missing directory: {path}")
        test_file = path / ".preflight-write-test"
        test_file.write_text("ok")
        test_file.unlink()
    for path in (model_dir / "model_last_inference.safetensors", model_dir / "vocab.txt", config_path):
        if not path.exists():
            fail(f"missing file: {path}")


def check_python() -> None:
    import torch
    import torchaudio
    import torchcodec
    import fastapi
    import gradio
    import ruaccent

    print(f"python={sys.version.split()[0]}")
    print(f"torch={torch.__version__} cuda={torch.version.cuda} available={torch.cuda.is_available()}")
    print(f"torchaudio={torchaudio.__version__}")
    print(f"torchcodec={getattr(torchcodec, '__version__', 'unknown')}")
    print(f"fastapi={fastapi.__version__} gradio={gradio.__version__} ruaccent={getattr(ruaccent, '__version__', 'unknown')}")
    if not torch.__version__.startswith("2.11.0"):
        fail("unexpected torch version")
    if not torchaudio.__version__.startswith("2.11.0"):
        fail("unexpected torchaudio version")


def check_patches() -> None:
    ruaccent_text = module_text("ruaccent.accent_model")
    f5_text = module_text("f5_tts.infer.utils_infer")
    if 'inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])' not in ruaccent_text:
        fail("ruaccent token_type_ids patch is missing")
    if "min_extra_frames = int(2.0 * target_sample_rate / hop_length)" not in f5_text:
        fail("f5_tts duration floor patch is missing")


def check_gpu(skip_load: bool) -> None:
    import torch

    if not torch.cuda.is_available():
        fail("torch cannot see CUDA")
    name = torch.cuda.get_device_name(0)
    total_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    minimum_gb = float(os.environ.get("F5_TTS_MIN_VRAM_GB", "14"))
    recommended_gb = float(os.environ.get("F5_TTS_RECOMMENDED_VRAM_GB", "20"))
    print(f"gpu={name} vram={total_gb:.1f}GB")
    if total_gb < minimum_gb:
        fail(f"GPU VRAM is below the {minimum_gb:g} GB minimum")
    if total_gb < recommended_gb:
        print(
            f"warning: GPU VRAM is below the recommended {recommended_gb:g} GB; "
            "avoid concurrent Gradio inference and Whisper Large",
            file=sys.stderr,
        )
    if not skip_load:
        x = torch.ones((1,), device="cuda")
        print(f"cuda-smoke={float(x.item())}")
    try:
        smi = subprocess.run(["nvidia-smi", "-L"], check=False, text=True, capture_output=True)
        print(smi.stdout.strip() or smi.stderr.strip())
    except FileNotFoundError:
        print("nvidia-smi not found; relying on torch cuda check")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-gpu", action="store_true")
    parser.add_argument("--skip-gpu-load", action="store_true")
    args = parser.parse_args()

    check_paths()
    check_python()
    check_patches()
    if not args.skip_gpu:
        check_gpu(skip_load=args.skip_gpu_load)
    print("preflight ok")


if __name__ == "__main__":
    main()
