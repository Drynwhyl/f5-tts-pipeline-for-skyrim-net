#!/usr/bin/env python3
"""
CLI to manage F5-TTS character emotional profiles.

Usage:
  python manage_character.py create <character> --emotion <name> <audio> [--text "ref text"] ...
  python manage_character.py import <character> <directory>
  python manage_character.py list
  python manage_character.py show <character>
  python manage_character.py delete <character>
"""
import os
import sys
import json
import shutil
import argparse
from pathlib import Path

BASE_DIR = Path(os.environ.get("F5_TTS_BASE_DIR", "/workspace/f5-tts")).expanduser()
VOICES_DIR = Path(os.environ.get("F5_TTS_VOICES_DIR", os.environ.get("F5_VOICES_DIR", str(BASE_DIR / "voices")))).expanduser()


def cmd_create(args):
    if not args.emotions:
        print("Error: at least one --emotion is required")
        sys.exit(1)

    char_dir = VOICES_DIR / args.character
    char_dir.mkdir(parents=True, exist_ok=True)

    emotion_names = []
    for emotion_name, audio_path, ref_text in args.emotions:
        audio_src = Path(audio_path)
        if not audio_src.exists():
            print(f"Error: audio file not found: {audio_src}")
            sys.exit(1)

        ext = audio_src.suffix or ".wav"
        audio_dst = char_dir / f"{emotion_name}{ext}"
        shutil.copy2(audio_src, audio_dst)

        text_dst = char_dir / f"{emotion_name}.txt"
        text_dst.write_text(ref_text)

        emotion_names.append(emotion_name)
        print(f"  {emotion_name}: {audio_dst}")

    print(f"Character '{args.character}' created with emotions: {emotion_names}")


def cmd_import(args):
    src_dir = Path(args.directory)
    if not src_dir.is_dir():
        print(f"Error: directory not found: {src_dir}")
        sys.exit(1)

    audio_extensions = {".wav", ".mp3", ".flac"}
    found = []

    for f in sorted(src_dir.iterdir()):
        if f.is_file() and f.suffix in audio_extensions:
            stem = f.stem
            text_file = src_dir / f"{stem}.txt"
            ref_text = text_file.read_text().strip() if text_file.exists() else ""
            found.append((stem, str(f), ref_text))

    if not found:
        print(f"Error: no audio files (*.wav, *.mp3, *.flac) found in {src_dir}")
        sys.exit(1)

    char_dir = VOICES_DIR / args.character
    char_dir.mkdir(parents=True, exist_ok=True)

    emotion_names = []
    for emotion_name, audio_src, ref_text in found:
        ext = Path(audio_src).suffix
        audio_dst = char_dir / f"{emotion_name}{ext}"
        shutil.copy2(audio_src, audio_dst)

        text_dst = char_dir / f"{emotion_name}.txt"
        text_dst.write_text(ref_text)

        emotion_names.append(emotion_name)
        print(f"  {emotion_name}: {audio_dst}")

    print(f"Character '{args.character}' imported with emotions: {emotion_names}")


def cmd_list(args):
    if not VOICES_DIR.is_dir():
        print("No characters configured.")
        return

    for d in sorted(VOICES_DIR.iterdir()):
        if not d.is_dir():
            continue
        emotions = []
        for f in sorted(d.iterdir()):
            if f.suffix in {".wav", ".mp3", ".flac"}:
                emotions.append(f.stem)
        if emotions:
            default = "normal" if "normal" in emotions else emotions[0]
            print(f"  {d.name:<20} emotions={emotions}  default={default}")


def cmd_show(args):
    char_dir = VOICES_DIR / args.character
    if not char_dir.is_dir():
        print(f"Error: character '{args.character}' not found")
        sys.exit(1)

    audio_extensions = {".wav", ".mp3", ".flac"}
    emotions = []
    for f in sorted(char_dir.iterdir()):
        if f.is_file() and f.suffix in audio_extensions:
            stem = f.stem
            text_file = char_dir / f"{stem}.txt"
            ref_text = text_file.read_text().strip() if text_file.exists() else "(none)"
            size = f.stat().st_size
            emotions.append({
                "name": stem,
                "audio": str(f),
                "size_bytes": size,
                "text": ref_text,
            })

    if not emotions:
        print(f"Character '{args.character}' exists but has no audio files.")
        return

    print(f"Character: {args.character}")
    print(f"Directory: {char_dir}")
    print(f"Emotions ({len(emotions)}):")
    for e in emotions:
        text_preview = e["text"][:60] + "..." if len(e["text"]) > 60 else e["text"]
        print(f"  {e['name']:<12} {e['size_bytes']:>8} bytes  text: {text_preview}")


def cmd_delete(args):
    char_dir = VOICES_DIR / args.character
    if not char_dir.is_dir():
        print(f"Error: character '{args.character}' not found")
        sys.exit(1)

    shutil.rmtree(char_dir)
    print(f"Character '{args.character}' deleted.")


def main():
    parser = argparse.ArgumentParser(description="Manage F5-TTS character emotional profiles")
    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = sub.add_parser("create", help="Create a character with emotion profiles")
    p_create.add_argument("character", help="Character name")
    p_create.add_argument(
        "--emotion", "-e",
        action="append",
        nargs=3,
        metavar=("NAME", "AUDIO", "TEXT"),
        dest="emotions",
        help="Emotion profile: name audio.wav \"reference text\"",
    )

    # import
    p_import = sub.add_parser("import", help="Import character from directory of emotion files")
    p_import.add_argument("character", help="Character name")
    p_import.add_argument("directory", help="Directory containing emotion_name.wav + emotion_name.txt pairs")

    # list
    sub.add_parser("list", help="List all characters")

    # show
    p_show = sub.add_parser("show", help="Show character details")
    p_show.add_argument("character", help="Character name")

    # delete
    p_delete = sub.add_parser("delete", help="Delete a character")
    p_delete.add_argument("character", help="Character name")

    args = parser.parse_args()

    if args.command == "create":
        cmd_create(args)
    elif args.command == "import":
        cmd_import(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "show":
        cmd_show(args)
    elif args.command == "delete":
        cmd_delete(args)


if __name__ == "__main__":
    main()
