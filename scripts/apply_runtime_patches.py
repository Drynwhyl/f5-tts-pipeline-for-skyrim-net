#!/usr/bin/env python3
"""Apply local compatibility patches to installed third-party packages."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


def _module_file(module_name: str) -> Path:
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        raise SystemExit(f"Cannot find module {module_name!r}")
    return Path(spec.origin)


def _patch_ruaccent(dry_run: bool) -> bool:
    path = _module_file("ruaccent.accent_model")
    text = path.read_text()
    marker = 'inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])'
    if marker in text:
        return False
    old = '        inputs = {k: v.astype(np.int64) for k, v in inputs.items()}\n        outputs = self.session.run(None, inputs)\n'
    new = (
        '        inputs = {k: v.astype(np.int64) for k, v in inputs.items()}\n'
        '        if "token_type_ids" not in inputs:\n'
        '            inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])\n'
        '        outputs = self.session.run(None, inputs)\n'
    )
    if old not in text:
        raise SystemExit(f"RUAccent patch context not found in {path}")
    if not dry_run:
        path.write_text(text.replace(old, new))
    print(f"patched {path}")
    return True


def _patch_f5_duration(dry_run: bool) -> bool:
    path = _module_file("f5_tts.infer.utils_infer")
    text = path.read_text()
    marker = "min_extra_frames = int(2.0 * target_sample_rate / hop_length)"
    if marker in text:
        return False
    old = (
        "            ref_text_len = len(ref_text.encode(\"utf-8\"))\n"
        "            gen_text_len = len(gen_text.encode(\"utf-8\"))\n"
        "            duration = ref_audio_len + int(ref_audio_len / ref_text_len * gen_text_len / local_speed)\n"
    )
    new = (
        "            # Calculate duration with safety margin and minimum floor\n"
        "            ref_text_len = len(ref_text.encode(\"utf-8\"))\n"
        "            gen_text_len = len(gen_text.encode(\"utf-8\"))\n"
        "            extra_frames = int(ref_audio_len / ref_text_len * gen_text_len / local_speed * 1.1)\n"
        "            min_extra_frames = int(2.0 * target_sample_rate / hop_length)\n"
        "            extra_frames = max(extra_frames, min_extra_frames)\n"
        "            duration = ref_audio_len + extra_frames\n"
    )
    if old not in text:
        raise SystemExit(f"F5-TTS duration patch context not found in {path}")
    if not dry_run:
        path.write_text(text.replace(old, new))
    print(f"patched {path}")
    return True


def verify() -> None:
    ruaccent_text = _module_file("ruaccent.accent_model").read_text()
    f5_text = _module_file("f5_tts.infer.utils_infer").read_text()
    missing = []
    if 'inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])' not in ruaccent_text:
        missing.append("ruaccent token_type_ids patch")
    if "min_extra_frames = int(2.0 * target_sample_rate / hop_length)" not in f5_text:
        missing.append("f5_tts duration floor patch")
    if missing:
        raise SystemExit("Missing runtime patches: " + ", ".join(missing))
    print("runtime patches verified")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true", help="Only verify patches are present")
    parser.add_argument("--dry-run", action="store_true", help="Check patch contexts without writing")
    args = parser.parse_args()

    if args.verify:
        verify()
        return
    _patch_ruaccent(args.dry_run)
    _patch_f5_duration(args.dry_run)
    verify()


if __name__ == "__main__":
    main()
