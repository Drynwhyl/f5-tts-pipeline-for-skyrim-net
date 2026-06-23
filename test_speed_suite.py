#!/usr/bin/env python3
"""Test speed proportionality and word dropout across voices, speeds, and text lengths.

Usage:
    python test_speed_suite.py            # run all tests
    python test_speed_suite.py --listen   # print HTML report for manual listening
"""

import json
import os
import re
import sys
import time
import base64
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

OUT_DIR = Path(os.environ.get("F5_TTS_SPEED_TEST_DIR", "/tmp/f5-tts-speed-test"))
API_URL = os.environ.get("F5_TTS_API_URL", "http://localhost:8000")

VOICES = ["sacred_demoness", "sacred_dark_elf"]
SPEEDS = [1.0, 1.5, 2.0]

TEXTS = {
    "very_short": "Привет мир.",
    "short": "Я иду в лес за дровами для камина.",
    "medium": (
        "Туман медленно поднимался над болотами, скрывая тропинку между корнями "
        "вековых деревьев. Я остановился прислушиваясь к тишине."
    ),
    "long": (
        "Стены имеют грубую текстуру светлого камня или оштукатуренной поверхности "
        "с заметными трещинами, которые укреплены массивными вертикальными "
        "деревянными балками, а пол выложен широкими потертыми деревянными досками, "
        "что подчеркивает функциональный и приземленный характер интерьера."
    ),
    "very_long": (
        "Здесь, в гулких коридорах этого древнего сооружения, кажется что само время "
        "замедлило свой бег. Каждый шаг отдаётся эхом под сводчатыми потолками, "
        "украшенными искусной лепниной. Массивные колонны поддерживают арочные "
        "перекрытия, а высокие стрельчатые окна пропускают внутрь рассеянный свет. "
        "Каменные плиты пола истёрты ногами множества поколений, оставивших свой след "
        "в этой бесконечной летописи истории."
    ),
}

CLIENT = None


def _client():
    global CLIENT
    import httpx
    if CLIENT is None:
        CLIENT = httpx.Client(timeout=120, base_url=API_URL)
    return CLIENT


def words_set(text: str) -> set[str]:
    """Lowercased words, punctuation stripped."""
    return set(re.sub(r"[^\w\s]", "", text).lower().split())


def tts_and_transcribe(voice: str, text: str, speed: float) -> dict:
    """Generate TTS, transcribe with Whisper, return results."""
    c = _client()
    # TTS
    body = {
        "model": "F5TTS_v1_Base_v2",
        "voice": voice,
        "input": text,
        "response_format": "wav",
        "speed": speed,
        "nfe_step": 64,
    }
    t0 = time.perf_counter()
    resp = c.post("/v1/audio/speech", json=body)
    tts_ms = (time.perf_counter() - t0) * 1000
    if resp.status_code != 200:
        return {"error": f"TTS HTTP {resp.status_code}: {resp.text[:200]}"}

    audio_bytes = resp.content

    # Whisper transcribe
    t0 = time.perf_counter()
    trans_resp = c.post("/v1/audio/transcriptions", files={
        "file": ("test.wav", audio_bytes, "audio/wav"),
    }, data={"model": "turbo"})
    trans_ms = (time.perf_counter() - t0) * 1000
    if trans_resp.status_code != 200:
        return {"error": f"Transcription HTTP {trans_resp.status_code}"}

    trans_text = trans_resp.json().get("text", "")

    # Word comparison
    orig_words = words_set(text)
    trans_words = words_set(trans_text)
    missing = orig_words - trans_words
    present = orig_words & trans_words
    coverage = len(present) / len(orig_words) * 100 if orig_words else 100

    return {
        "audio": base64.b64encode(audio_bytes).decode(),
        "duration_s": len(audio_bytes) / (24000 * 2) if audio_bytes else 0,
        "transcribed": trans_text,
        "missing_words": sorted(missing),
        "coverage_pct": round(coverage, 1),
        "tts_ms": round(tts_ms, 0),
        "trans_ms": round(trans_ms, 0),
    }


def run_all() -> dict:
    """Run all test cases, return nested dict keyed by voice/speed/length."""
    results = {}
    cases = [(v, s, ln, TEXTS[ln]) for v in VOICES for s in SPEEDS for ln in TEXTS]
    total = len(cases)

    print(f"Running {total} tests across {len(VOICES)} voices × {len(SPEEDS)} speeds × {len(TEXTS)} lengths")
    print(f"{'='*70}")

    futures = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for voice, speed, label, text in cases:
            key = (voice, speed, label)
            futures[pool.submit(tts_and_transcribe, voice, text, speed)] = key

        done = 0
        for future in as_completed(futures):
            voice, speed, label = futures[future]
            try:
                data = future.result()
            except Exception as e:
                data = {"error": str(e)}

            if voice not in results:
                results[voice] = {}
            if speed not in results[voice]:
                results[voice][speed] = {}
            results[voice][speed][label] = data

            done += 1
            err = data.get("error")
            cov = data.get("coverage_pct", 0)
            status = f"ERROR: {err[:60]}" if err else f"{cov:>5.1f}% coverage"
            print(f"  [{done:2d}/{total}] {voice:20s} × speed {speed:3.1f} × {label:12s} → {status}")

    print(f"{'='*70}")
    return results


def print_report(results: dict, audio_dir: Path = OUT_DIR):
    """Print a human-readable report."""
    audio_dir.mkdir(parents=True, exist_ok=True)

    overall_issues = []

    for voice in VOICES:
        vdata = results.get(voice, {})
        print(f"\n{'='*70}")
        print(f"  VOICE: {voice}")
        print(f"{'='*70}")
        print(f"{'Speed':>7} {'Length':<14} {'Duration':>10} {'TTLms':>8} {'WPM':>8} {'Coverage':>10}  Missing")
        print(f"{'─'*7} {'─'*14} {'─'*10} {'─'*8} {'─'*8} {'─'*10}  {'─'*20}")

        for speed in SPEEDS:
            for label in TEXTS:
                data = vdata.get(speed, {}).get(label, {})
                if data.get("error"):
                    print(f"{speed:>7.1f} {label:<14} {'ERROR':>10} {'':>8} {'':>8} {'':>10}  {data['error'][:60]}")
                    overall_issues.append(f"{voice}×speed{speed}×{label}: {data['error']}")
                    continue

                dur_s = data.get("duration_s", 0)
                tts_ms = data.get("tts_ms", 0)
                n_words = len(words_set(TEXTS[label]))
                wpm = round(n_words / dur_s * 60) if dur_s > 0 else 0
                cov = data.get("coverage_pct", 0)
                missing = data.get("missing_words", [])

                print(f"{speed:>7.1f} {label:<14} {dur_s:>8.2f}s {tts_ms:>8.0f} {wpm:>8d} {cov:>8.1f}%  {', '.join(missing[:10])}")

                if missing and speed >= 1.0:
                    overall_issues.append(f"{voice}×speed{speed}×{label}: missing {missing}")

    # Summary
    print(f"\n{'='*70}")
    print("  SUMMARY")
    print(f"{'='*70}")
    total = 0
    good = 0
    for voice in VOICES:
        for speed in SPEEDS:
            for label in TEXTS:
                total += 1
                data = results.get(voice, {}).get(speed, {}).get(label, {})
                if data.get("coverage_pct", 0) >= 90 and not data.get("error"):
                    good += 1

    print(f"  Passed: {good}/{total} (coverage ≥ 90%)")
    if overall_issues:
        print(f"  Issues ({len(overall_issues)}):")
        for issue in overall_issues[:15]:
            print(f"    • {issue}")
        if len(overall_issues) > 15:
            print(f"    ... and {len(overall_issues) - 15} more")

    # Save audio files for listening
    audio_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for voice in VOICES:
        for speed in SPEEDS:
            for label in TEXTS:
                data = results.get(voice, {}).get(speed, {}).get(label, {})
                if data.get("audio"):
                    filename = f"{voice}_speed{speed}_{label}.wav"
                    path = audio_dir / filename
                    with open(path, "wb") as f:
                        f.write(base64.b64decode(data["audio"]))
                    count += 1
    print(f"\n  Audio saved: {count} files → {audio_dir}")

    # Generate HTML report
    html = _generate_html(results)
    html_path = audio_dir / "report.html"
    html_path.write_text(html)
    print(f"  HTML report: {html_path}")


def _generate_html(results: dict) -> str:
    rows = []
    for voice in VOICES:
        for speed in SPEEDS:
            for label in TEXTS:
                data = results.get(voice, {}).get(speed, {}).get(label, {})
                dur = f"{data.get('duration_s', 0):.1f}s"
                cov = data.get("coverage_pct", 0)
                missing = ", ".join(data.get("missing_words", []))
                trans = data.get("transcribed", "")
                audio_b64 = data.get("audio", "")
                err = data.get("error", "")

                audio_html = f'<audio src="data:audio/wav;base64,{audio_b64}" controls preload="none"></audio>' if audio_b64 else "—"
                color = "#2ea043" if cov >= 90 else "#d29922" if cov >= 70 else "#da3633"
                rows.append(f"""<tr>
  <td>{voice}</td>
  <td>{speed}</td>
  <td>{label}</td>
  <td>{audio_html}</td>
  <td>{dur}</td>
  <td style="color:{color};font-weight:bold">{cov}%</td>
  <td>{missing}</td>
  <td style="font-size:12px;max-width:300px;overflow:hidden">{trans[:100]}{'…' if len(trans)>100 else ''}</td>
  <td style="font-size:12px;color:#da3633">{err[:60]}</td>
</tr>""")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>F5-TTS Speed Test Report</title>
<style>
body {{ font-family: sans-serif; background: #0d1117; color: #c9d1d9; margin: 20px; }}
h1 {{ color: #58a6ff; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #30363d; padding: 8px 12px; text-align: left; }}
th {{ background: #161b22; }}
tr:nth-child(even) {{ background: #0d1117; }}
tr:nth-child(odd) {{ background: #161b22; }}
audio {{ width: 180px; height: 32px; }}
</style></head><body>
<h1>F5-TTS Speed Test Report</h1>
<p>Generated: {time.strftime("%Y-%m-%d %H:%M")}</p>
<table><thead><tr>
<th>Voice</th><th>Speed</th><th>Length</th><th>Audio</th><th>Duration</th><th>Coverage</th><th>Missing</th><th>Transcribed</th><th>Error</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table></body></html>"""


def parse_args():
    import argparse
    p = argparse.ArgumentParser(description="F5-TTS speed test suite")
    p.add_argument("--listen", action="store_true", help="Open HTML report in browser")
    p.add_argument("--output", default=str(OUT_DIR), help="Output directory")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    out = Path(args.output)
    print(f"Output: {out}")
    results = run_all()
    print_report(results, out)

    if args.listen:
        import webbrowser
        webbrowser.open(f"file://{out / 'report.html'}")
