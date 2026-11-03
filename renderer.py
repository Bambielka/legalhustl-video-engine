"""LEGALHUSTL local-cost, cloud-executed short-form video renderer.

Creates 10–15 second 1080x1920 MP4s from approved legal copy using FFmpeg,
animated typography and an original algorithmically generated instrumental bed.
No generative-video API, licensed stock footage, or voice synthesis is used.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Dict, List

import numpy as np

WIDTH = 1080
HEIGHT = 1920
FPS = 30
DURATION_MIN = 10.0
DURATION_MAX = 15.0

BG_PALETTES = [
    ("0x07182A", "0xD9B65D", "0x234A69"),
    ("0x101C32", "0xE2C36B", "0x315A7C"),
    ("0x081D27", "0xC9A85C", "0x275B68"),
    ("0x151A2B", "0xE7C66A", "0x3B4C72"),
    ("0x0A1933", "0xD5B75F", "0x294B79"),
]


def _clean(value: object, limit: int = 4000) -> str:
    text = str(value or "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\[[^\]]{0,40}\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?;])\s+|\n+", text)
    return [p.strip(" \t-•0123456789.)") for p in parts if len(p.strip()) > 8]


def _wrap_words(text: str, max_chars: int = 23, max_lines: int = 4) -> str:
    words = _clean(text, 650).split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if len(candidate) > max_chars and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" ,;:") + "…"
    return "\n".join(lines)


def _chunks(script: str, headline: str, cta: str) -> List[Dict[str, str]]:
    sentences = _sentences(script)
    body = " ".join(sentences)
    if len(body.split()) < 12:
        body = _clean(script, 1300)
    words = body.split()
    # Split the core explanation across two short readable cards.
    first = " ".join(words[: min(15, max(8, len(words) // 2))])
    rest = " ".join(words[len(first.split()):])
    second = " ".join(rest.split()[:17])
    if not second:
        second = "Пояснюємо, кого стосується ця правова зміна."
    headline = _clean(headline, 100) or "Важлива новина військового права"
    cta = _clean(cta, 110) or "Збережіть, щоб не загубити важливе"
    return [
        {"label": "ЮРИДИЧНА НОВИНА", "title": headline, "body": "Що сталося — коротко і по суті."},
        {"label": "КЛЮЧОВИЙ ФАКТ", "title": first or headline, "body": "Без припущень. Лише перевірена інформація."},
        {"label": "ЩО ЦЕ ОЗНАЧАЄ", "title": second, "body": "Звертайте увагу на статус документа та дату."},
        {"label": "LEGALHUSTL", "title": cta, "body": "Військове право • Права • Захист"},
    ]


def _synth_music(path: Path, duration: float, seed_text: str) -> None:
    """Create an original subdued instrumental bed; no third-party music asset."""
    sr = 44100
    n = int(sr * duration)
    t = np.arange(n, dtype=np.float64) / sr
    rng = np.random.default_rng(int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:8], 16))
    audio = np.zeros(n, dtype=np.float64)
    # Four slowly changing minor/major suspended chords. Frequencies are Hz.
    chord_sets = [
        [110.00, 164.81, 220.00, 261.63],  # A minor
        [87.31, 130.81, 174.61, 220.00],   # F major
        [130.81, 164.81, 196.00, 261.63],  # C major
        [98.00, 146.83, 196.00, 246.94],   # G major
    ]
    segment = duration / 4.0
    for i, freqs in enumerate(chord_sets):
        mask = (t >= i * segment) & (t < (i + 1) * segment)
        local = t[mask] - i * segment
        fade = np.minimum(1.0, np.minimum(local / 0.35, (segment - local) / 0.55)).clip(0, 1)
        pad = np.zeros(local.size, dtype=np.float64)
        for j, f in enumerate(freqs):
            pad += np.sin(2 * np.pi * f * local + j * 0.3) / (j + 1.2)
            pad += 0.22 * np.sin(2 * np.pi * (f * 2.003) * local + j) / (j + 1.5)
        audio[mask] += 0.105 * pad * fade
    # Soft pulsing low notes on the beat, not a drum loop.
    beat = 60.0 / 92.0
    for i, start in enumerate(np.arange(0.0, duration, beat)):
        end = min(duration, start + 0.24)
        idx = (t >= start) & (t < end)
        local = t[idx] - start
        freq = [55.0, 43.65, 65.41, 49.0][int(start / segment) % 4]
        env = np.exp(-local * 12.0)
        audio[idx] += 0.045 * np.sin(2 * np.pi * freq * local) * env
    # Very quiet filtered-like shimmer from deterministic broadband noise.
    noise = rng.normal(0.0, 1.0, n)
    kernel = np.ones(350, dtype=np.float64) / 350.0
    soft_noise = np.convolve(noise, kernel, mode="same")
    audio += 0.008 * soft_noise
    fade_in = np.clip(t / 0.8, 0, 1)
    fade_out = np.clip((duration - t) / 1.2, 0, 1)
    audio *= fade_in * fade_out
    peak = float(np.max(np.abs(audio))) or 1.0
    audio = audio / peak * 0.38
    pcm = np.int16(np.clip(audio, -1, 1) * 32767)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        wav.writeframes(pcm.tobytes())


def _escape_text_path(path: Path) -> str:
    # drawtext's textfile option parses colons and backslashes specially.
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _write_text(path: Path, text: str) -> None:
    # Preserve deliberate line breaks from _wrap_words; clean each line only.
    value = str(text or "").replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    value = "\n".join(lines[:5]).strip()
    path.write_text(value[:650], encoding="utf-8")


def build_video(job: Dict[str, object], output_path: str | Path, ffmpeg: str = "ffmpeg") -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    content_id = _clean(job.get("content_id", "LEGALHUSTL"), 80)
    headline = _clean(job.get("headline") or job.get("topic"), 120)
    script = _clean(job.get("script") or job.get("caption") or headline, 1800)
    cta = _clean(job.get("cta"), 110)
    fmt = str(job.get("format") or "Reels").lower()
    duration = 12.0 if "story" in fmt else 14.0
    if not DURATION_MIN <= duration <= DURATION_MAX:
        raise ValueError("Duration must be 10–15 seconds")

    digest = hashlib.sha256((content_id + headline).encode("utf-8")).digest()
    bg, accent, secondary = BG_PALETTES[digest[0] % len(BG_PALETTES)]
    cards = _chunks(script, headline, cta)
    with tempfile.TemporaryDirectory(prefix="legalhustl-") as temp_str:
        temp = Path(temp_str)
        audio_file = temp / "music.wav"
        _synth_music(audio_file, duration, content_id + headline)
        font_regular = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if not Path(font_regular).exists():
            font_regular = "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
            font_bold = "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if not Path(font_regular).exists():
            raise RuntimeError("No suitable Unicode font found on the rendering runner")

        segment = duration / len(cards)
        branches = []
        split_outputs = "".join(f"[base{i}]" for i in range(len(cards)))
        graph = [f"[0:v]split={len(cards)}{split_outputs}"]
        for index, card in enumerate(cards):
            tag_file = temp / f"tag_{index}.txt"
            title_file = temp / f"title_{index}.txt"
            body_file = temp / f"body_{index}.txt"
            _write_text(tag_file, card["label"])
            _write_text(title_file, _wrap_words(card["title"], 18, 4))
            _write_text(body_file, _wrap_words(card["body"], 28, 3))
            # Each branch sees only one short scene; subtitles are therefore
            # rendered once per frame instead of evaluating every scene layer
            # across the whole 14-second video.
            chain = [
                f"[base{index}]trim=start={index*segment:.3f}:duration={segment:.3f}",
                "setpts=PTS-STARTPTS",
                "drawgrid=width=150:height=150:thickness=1:color=" + secondary + "@0.20",
                f"drawbox=x='mod(t*95\\,iw+300)-300':y=0:w=300:h=ih:color={accent}@0.045:t=fill",
                f"drawbox=x=0:y=0:w=iw:h=16:color={accent}@0.95:t=fill",
                f"drawtext=fontfile='{_escape_text_path(Path(font_bold))}':text='LEGALHUSTL':fontcolor=white:fontsize=43:x=74:y=112:shadowcolor=black@0.5:shadowx=2:shadowy=2",
                f"drawtext=fontfile='{_escape_text_path(Path(font_regular))}':text='ВІЙСЬКОВЕ ПРАВО  /  ПРАВА  /  ЗАХИСТ':fontcolor={accent}:fontsize=23:x=76:y=174",
                f"drawtext=fontfile='{_escape_text_path(Path(font_bold))}':textfile='{_escape_text_path(tag_file)}':fontcolor={accent}:fontsize=31:x=(w-text_w)/2:y=365:box=1:boxcolor=0x0A1C2ECC:boxborderw=24",
                f"drawtext=fontfile='{_escape_text_path(Path(font_bold))}':textfile='{_escape_text_path(title_file)}':fontcolor=white:fontsize=58:line_spacing=16:x=(w-text_w)/2:y=(h-text_h)/2-50:box=1:boxcolor=0x07182AE0:boxborderw=48:shadowcolor=black@0.55:shadowx=2:shadowy=3",
                f"drawtext=fontfile='{_escape_text_path(Path(font_regular))}':textfile='{_escape_text_path(body_file)}':fontcolor=0xE1EAF2:fontsize=34:line_spacing=12:x=(w-text_w)/2:y=(h-text_h)/2+330:box=1:boxcolor=0x0A1C2ACC:boxborderw=26",
                f"drawbox=x=0:y=ih-76:w='iw*t/{segment:.3f}':h=10:color={accent}:t=fill",
                "fade=t=in:st=0:d=0.22",
                f"fade=t=out:st={max(0.1,segment-0.25):.3f}:d=0.25",
                f"format=yuv420p[scene{index}]",
            ]
            graph.append(",".join(chain))
            branches.append(f"[scene{index}]")
        graph.append("".join(branches) + f"concat=n={len(cards)}:v=1:a=0[vout]")
        filter_graph = ";".join(graph)
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-filter_complex_threads", "2",
            "-f", "lavfi", "-i", f"color=c={bg}:s={WIDTH}x{HEIGHT}:r={FPS}:d={duration}",
            "-i", str(audio_file),
            "-filter_complex", filter_graph,
            "-map", "[vout]", "-map", "1:a:0",
            "-t", str(duration), "-r", str(FPS),
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-profile:v", "high",
            "-level", "4.1", "-b:v", "1900k", "-maxrate", "2200k", "-bufsize", "4400k",
            "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "112k", "-ar", "44100",
            "-movflags", "+faststart", str(output),
        ]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=150)
        if proc.returncode != 0:
            raise RuntimeError("FFmpeg render failed: " + (proc.stderr or proc.stdout)[-2500:])
    if not output.exists() or output.stat().st_size < 100_000:
        raise RuntimeError("Rendered MP4 is missing or unexpectedly small")
    return output


if __name__ == "__main__":
    import argparse, json
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--output", default="legalhustl.mp4")
    args = parser.parse_args()
    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    result = build_video(job, args.output)
    print(json.dumps({"file": str(result), "bytes": result.stat().st_size}, ensure_ascii=False))
