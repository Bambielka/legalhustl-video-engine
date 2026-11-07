"""SENTINEL LAW Motion Engine V3.

Editorial kinetic motion design inspired by the supplied Remotion reference:
light canvas, bold typography, violet accents, layered legal graphics,
progressive reveals, animated cards, kinetic labels and synchronized captions.
The existing Apps Script / worker job contract is preserved.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
import wave
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

WIDTH = 720
HEIGHT = 1280
FPS = 30
AUDIO_SR = 44100
MAX_UPLOAD_BYTES = 8_500_000
MAX_SCRIPT_CHARS = 12_000

BG = (249, 249, 251)
INK = (15, 15, 19)
MUTED = (101, 101, 112)
PURPLE = (132, 82, 247)
PURPLE_DARK = (102, 54, 211)
PURPLE_LIGHT = (239, 231, 255)
LINE = (219, 218, 226)
WHITE = (255, 255, 255)


def _clean(value: Any, limit: int = MAX_SCRIPT_CHARS) -> str:
    text = str(value or "").replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"```(?:text|markdown)?|```", "", text, flags=re.I)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?<!\*)\*\*(.*?)\*\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!\*)\*(.*?)\*(?!\*)", r"\1", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0E\uFE0F\u200D]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
    if len(text) > limit:
        raise ValueError(
            f"Сценарій завеликий: {len(text)} символів. Ліміт {limit}; "
            "текст не обрізано — скороти сценарій або створи окрему частину."
        )
    return text.strip()


def _is_direction(line: str) -> bool:
    value = line.strip().lstrip("-•* ").strip()
    if not value:
        return True
    if re.match(r"^(?:сцена|scene|кадр)\s*\d+\b", value, re.I):
        return True
    if re.match(r"^\[?(?:0?\d{1,2}:\d{2}|\d{1,2}\s*[–-]\s*\d{1,2}\s*(?:с|сек|секунд))\]?\s*:?$", value, re.I):
        return True
    directions = (
        "ВІЗУАЛ", "ВІДЕОРЯД", "КАДР", "КАМЕРА", "АНІМАЦІЯ", "ЕФЕКТ",
        "ПЕРЕХІД", "МУЗИКА", "ЗВУК", "ТАЙМІНГ", "ТРИВАЛІСТЬ", "ГРАФІКА",
        "КОМПОЗИЦІЯ", "ІНСТРУКЦІЯ", "ПІДКАЗКА", "ПЛАШКА", "ФОН",
        "СЦЕНАРІЙ", "ФОРМАТ", "ТЕХНІЧНЕ ЗАВДАННЯ", "VIDEO PROMPT",
        "РЕЖИСЕРСЬКА ВКАЗІВКА", "ОПИС СЦЕНИ"
    )
    upper = value.upper()
    return any(re.match(r"^" + re.escape(label) + r"\s*:", upper) for label in directions)


def _extract_script_text(script: str) -> str:
    source = _clean(script)
    if not source:
        return ""
    keep_labels = (
        "ТЕКСТ НА ЕКРАНІ", "ТЕКСТ ДИКТОРА", "ОЗВУЧКА", "ДИКТОР",
        "РЕПЛІКА", "ТЕКСТ", "ПОВІДОМЛЕННЯ", "НА ЕКРАНІ",
        "ГОЛОС ЗА КАДРОМ", "СУБТИТРИ"
    )
    out: List[str] = []
    for raw in source.split("\n"):
        line = raw.strip().lstrip("-•* ").strip()
        if not line or _is_direction(line):
            continue
        kept = False
        for label in keep_labels:
            m = re.match(r"^" + re.escape(label) + r"\s*:\s*(.*)$", line, re.I)
            if m:
                if m.group(1).strip():
                    out.append(m.group(1).strip())
                kept = True
                break
        if kept:
            continue
        if re.match(r"^(?:ведучий|ведуча|голос за кадром|завершення|вступ)\s*:\s*$", line, re.I):
            continue
        out.append(line)
    result = re.sub(r"\s+", " ", " ".join(out)).strip()
    return result


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?;])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _split_script(text: str, max_words: int = 19, max_chars: int = 145) -> List[str]:
    """Split into readable captions while preserving every word in order."""
    sentences = _sentences(text)
    chunks: List[str] = []
    current: List[str] = []
    for sentence in sentences:
        words = sentence.split()
        # Split a very long sentence into readable subparts, keeping punctuation.
        sentence_parts: List[str] = []
        part: List[str] = []
        for word in words:
            candidate = " ".join(part + [word])
            if part and (len(part) >= max_words or len(candidate) > max_chars):
                sentence_parts.append(" ".join(part))
                part = [word]
            else:
                part.append(word)
        if part:
            sentence_parts.append(" ".join(part))
        for piece in sentence_parts:
            piece_words = piece.split()
            candidate = " ".join(current + piece_words)
            if current and (len(current) + len(piece_words) > max_words or len(candidate) > max_chars):
                chunks.append(" ".join(current))
                current = piece_words
            else:
                current.extend(piece_words)
    if current:
        chunks.append(" ".join(current))
    # Merge tiny tail into previous segment if it remains readable.
    if len(chunks) > 1 and len(chunks[-1].split()) <= 4:
        merged = chunks[-2] + " " + chunks[-1]
        if len(merged) <= max_chars + 65 and len(merged.split()) <= max_words + 6:
            chunks[-2] = merged
            chunks.pop()
    return chunks


@lru_cache(maxsize=128)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=size)


def _find_font(bold: bool = False) -> str:
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/lato/Lato-Heavy.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ] if bold else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("Не знайдено шрифт із підтримкою української мови.")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> List[str]:
    lines: List[str] = []
    line = ""
    for word in str(text).split():
        candidate = (line + " " + word).strip()
        if line and draw.textlength(candidate, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def _fit(draw: ImageDraw.ImageDraw, text: str, path: str, width: int,
         max_height: int, preferred: int, minimum: int = 24,
         line_factor: float = 1.12) -> Tuple[ImageFont.FreeTypeFont, List[str], int]:
    for size in range(preferred, minimum - 1, -2):
        font = _font(path, size)
        # A single long Ukrainian word must also fit; never allow horizontal clipping.
        if any(draw.textlength(word, font=font) > width for word in str(text).split()):
            continue
        lines = _wrap(draw, text, font, width)
        line_h = int(size * line_factor)
        if len(lines) * line_h <= max_height:
            return font, lines, line_h
    size = minimum
    while size > 12:
        font = _font(path, size)
        if all(draw.textlength(word, font=font) <= width for word in str(text).split()):
            break
        size -= 1
    font = _font(path, size)
    lines = _wrap(draw, text, font, width)
    return font, lines, int(size * line_factor)


def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _spring(t: float, damping: float = 8.0, frequency: float = 11.0) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - math.exp(-damping * t) * math.cos(frequency * t)


def _alpha(color: Tuple[int, int, int], alpha: float) -> Tuple[int, int, int, int]:
    return (color[0], color[1], color[2], max(0, min(255, int(alpha * 255))))


def _round_rect(draw: ImageDraw.ImageDraw, box, radius: int,
                fill, outline=None, width: int = 1) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _citation(text: str) -> str:
    patterns = [
        r"ст(?:атті|аття|\.?)\s*\d{1,3}(?:[-–]\d{1,3})?",
        r"ч(?:астина|\.?)\s*\d{1,2}\s*ст(?:атті|аття|\.?)\s*\d{1,3}",
        r"п(?:ункт|\.?)\s*\d+(?:\.\d+)*",
        r"абзац(?:у)?\s+\d+",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(0)
    return ""


def _numbers(text: str) -> List[str]:
    # Only exact values present in the approved script are shown.
    matches = re.findall(r"(?<!\w)(?:\d{1,3}(?:[ .]\d{3})+|\d+(?:[,.]\d+)?)(?:\s?(?:%|грн|днів|дні|день|годин|години|місяців|років|€|\$))?", text, re.I)
    return matches[:3]


def _scene_label(text: str, index: int, total: int) -> str:
    t = text.lower()
    if _citation(text):
        return "НОРМАТИВНА ОСНОВА"
    if re.search(r"\b(строк|термін|днів|день|годин|місяц|років|до \d+)\b", t):
        return "СТРОКИ ТА ДАТИ"
    if re.search(r"\b(рапорт|заява|звернен|документ|наказ|копі|витяг)\b", t):
        return "ДОКУМЕНТИ ТА ЗВЕРНЕННЯ"
    if re.search(r"\b(оскарж|скарг|апеляц|суд|рішенн)\b", t):
        return "ПРАВОВИЙ ЗАХИСТ"
    if re.search(r"\b(потрібно|необхідно|подати|звернутися|надіслати|отримати|зберегти)\b", t):
        return "ЩО РОБИТИ"
    return "КЛЮЧОВИЙ ФАКТ" if index == 1 else "ПРАВОВЕ ПОЯСНЕННЯ"


def _make_scenes(job: Dict[str, Any]) -> List[Dict[str, str]]:
    fmt = str(job.get("format") or "Reels").strip().lower()
    headline = _clean(job.get("headline") or job.get("topic") or "Правовий розбір", 500)
    script = _extract_script_text(str(job.get("script") or job.get("caption") or headline))
    if not script:
        script = headline
    cta = _clean(job.get("cta") or "Збережіть матеріал, щоб повернутися до нього", 400)
    if fmt == "story":
        return [{
            "kind": "story", "label": "SENTINEL LAW",
            "title": _clean(job.get("story_title") or headline, 400),
            "body": script,
        }]
    chunks = _split_script(script)
    scenes: List[Dict[str, str]] = [{"kind": "cover", "label": "ПРАВОВИЙ РОЗБІР", "title": headline, "body": ""}]
    for i, chunk in enumerate(chunks, start=1):
        scenes.append({
            "kind": "content",
            "label": _scene_label(chunk, i, len(chunks)),
            "title": "",
            "body": chunk,
        })
    cta_words = cta.split()
    closing_title = " ".join(cta_words[:2]) if len(cta_words) > 2 else cta
    scenes.append({"kind": "closing", "label": "SENTINEL LAW", "title": closing_title, "body": cta})
    return scenes


def _scene_durations(scenes: Sequence[Dict[str, str]], fmt: str) -> List[float]:
    if fmt.lower() == "story":
        return [12.0]
    durations: List[float] = []
    for scene in scenes:
        if scene["kind"] == "cover":
            durations.append(3.5)
        elif scene["kind"] == "closing":
            durations.append(3.2)
        else:
            words = len(scene["body"].split())
            # Reading cadence is deliberately unhurried: the full legal sentence stays visible.
            durations.append(max(4.8, min(8.2, 2.8 + words / 3.4)))
    return durations


def _draw_header(canvas: Image.Image, scene_index: int, total: int, progress: float) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    regular = _font(_find_font(False), 15)
    bold = _font(_find_font(True), 17)
    d.text((42, 31), "SENTINEL LAW", font=bold, fill=INK)
    d.text((42, 54), "ПРАВО  /  АНАЛІТИКА  /  ЗАХИСТ", font=regular, fill=MUTED)
    right = f"{scene_index:02d}  /  {total:02d}"
    rw = d.textlength(right, font=regular)
    d.text((WIDTH - 42 - rw, 38), right, font=regular, fill=MUTED)
    d.line((42, 84, WIDTH - 42, 84), fill=(*LINE, 255), width=2)
    d.rounded_rectangle((42, 84, WIDTH - 42, 88), radius=2, fill=(238, 236, 243, 255))
    d.rounded_rectangle((42, 84, 42 + int((WIDTH - 84) * max(0, min(1, progress))), 88),
                        radius=2, fill=(*PURPLE, 255))


def _draw_background(canvas: Image.Image, frame: int, scene_index: int) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    phase = frame / FPS
    # Quiet animated violet forms create motion without competing with the copy.
    cx = 620 + int(28 * math.sin(phase * 0.62 + scene_index))
    cy = 930 + int(36 * math.cos(phase * 0.48 + scene_index * 0.4))
    d.ellipse((cx - 230, cy - 230, cx + 230, cy + 230), fill=(132, 82, 247, 14))
    d.ellipse((cx - 165, cy - 165, cx + 165, cy + 165), outline=(132, 82, 247, 30), width=2)
    d.ellipse((cx - 105, cy - 105, cx + 105, cy + 105), outline=(132, 82, 247, 22), width=2)
    for y in range(140, HEIGHT, 70):
        d.line((0, y, WIDTH, y), fill=(242, 241, 246, 255), width=1)


def _draw_word_reveal(
    draw: ImageDraw.ImageDraw, text: str, font_path: str,
    xy: Tuple[int, int], max_width: int, max_height: int,
    preferred: int, elapsed: float, color=INK, line_delay: float = 0.10
) -> int:
    font, lines, line_h = _fit(draw, text, font_path, max_width, max_height, preferred, 27, 1.03)
    y = xy[1]
    for i, line in enumerate(lines):
        local = (elapsed - i * line_delay) / 0.34
        alpha = _ease_out(local)
        if alpha <= 0:
            y += line_h
            continue
        lift = int((1 - alpha) * 28)
        draw.text((xy[0], y + lift), line, font=font, fill=_alpha(color, alpha))
        y += line_h
    return y


def _draw_document_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    p = _spring(elapsed / 0.72)
    x = int(340 + (1 - p) * 95)
    y = int(410 + (1 - p) * 55)
    # Shadow and paper sheet.
    d.rounded_rectangle((x + 9, y + 14, x + 312, y + 424), radius=18, fill=(20, 16, 35, 24))
    d.rounded_rectangle((x, y, x + 300, y + 410), radius=16, fill=(255, 255, 255, 255),
                        outline=(205, 202, 215, 255), width=2)
    d.rounded_rectangle((x + 24, y + 25, x + 276, y + 83), radius=9, fill=(*PURPLE_LIGHT, 255))
    f = _font(_find_font(True), 17)
    d.text((x + 39, y + 45), "НОРМАТИВНИЙ АКТ", font=f, fill=PURPLE_DARK)
    citation = _citation(scene.get("body", ""))
    if citation:
        cite = _font(_find_font(True), 34)
        d.text((x + 28, y + 110), citation[:24], font=cite, fill=INK)
        d.rounded_rectangle((x + 25, y + 165, x + 260, y + 215), radius=7, fill=(238, 231, 255, 255))
    else:
        title = _font(_find_font(True), 29)
        d.text((x + 28, y + 115), "ПРАВОВА", font=title, fill=INK)
        d.text((x + 28, y + 150), "ПОЗИЦІЯ", font=title, fill=INK)
    # Progressive paragraph rules; the purple highlight slides across the page.
    reveal = _ease_out((elapsed - 0.45) / 0.8)
    for i, yy in enumerate((255, 284, 313, 342, 371)):
        end = x + 60 + int(185 * reveal)
        d.rounded_rectangle((x + 28, y + yy - 5, end, y + yy + 3), radius=4,
                            fill=(155, 151, 165, 105) if i != 1 else (132, 82, 247, 170))
    # A moving violet bookmark tab.
    tab_y = y + 18 + int(22 * math.sin(elapsed * 2.0))
    d.rounded_rectangle((x + 246, tab_y, x + 270, tab_y + 68), radius=5, fill=(*PURPLE, 255))


def _draw_metric_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    p = _spring(elapsed / 0.7)
    x, y = 70, int(470 + (1 - p) * 60)
    d.rounded_rectangle((x + 8, y + 12, x + 570, y + 365), radius=24, fill=(25, 20, 45, 20))
    d.rounded_rectangle((x, y, x + 560, y + 350), radius=22, fill=(255, 255, 255, 255),
                        outline=(210, 207, 219, 255), width=2)
    label_font = _font(_find_font(True), 19)
    d.text((x + 30, y + 28), "КЛЮЧОВІ ДАНІ", font=label_font, fill=PURPLE_DARK)
    values = _numbers(scene.get("body", ""))
    if values:
        big = _font(_find_font(True), 88)
        value = values[0]
        # Keep exact source value; don't infer a unit.
        d.text((x + 30, y + 90), value[:12], font=big, fill=INK)
        body = scene.get("body", "")
        tail = body[:74] + ("…" if len(body) > 74 else "")
        small, lines, lh = _fit(d, tail, _find_font(False), 485, 100, 22, 18, 1.2)
        for i, line in enumerate(lines[:3]):
            d.text((x + 32, y + 235 + i * lh), line, font=small, fill=MUTED)
    else:
        big = _font(_find_font(True), 61)
        d.text((x + 30, y + 104), "ФАКТ", font=big, fill=INK)
        d.rounded_rectangle((x + 32, y + 205, x + 510, y + 218), radius=6, fill=(*PURPLE, 255))
        d.rounded_rectangle((x + 32, y + 246, x + 390, y + 257), radius=6, fill=(220, 217, 228, 255))
    # Decorative progress bars animate to the beat-like scene rhythm.
    barw = 430
    base_y = y + 305
    for i, height in enumerate((20, 34, 50, 28, 43, 25)):
        amp = 0.35 + 0.65 * abs(math.sin(elapsed * 2.2 + i * 0.66))
        bw = int(36 * _ease_out((elapsed - 0.3 - i * 0.07) / 0.35))
        xx = x + 32 + i * 68
        d.rounded_rectangle((xx, base_y - int(height * amp), xx + max(4, bw), base_y),
                            radius=5, fill=(*PURPLE, 235))


def _action_words(text: str) -> List[str]:
    candidates = re.findall(
        r"\b(?:подати|подайте|звернутися|зверніться|отримати|отримайте|зберегти|збережіть|"
        r"надіслати|надішліть|перевірити|перевірте|оскаржити|оскаржте|додати|додайте|"
        r"зафіксувати|зафіксуйте|вимагати|вимагайте)\b",
        text, flags=re.I
    )
    return list(dict.fromkeys(x.upper() for x in candidates))[:3]


def _draw_steps_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    x0, y0 = 76, 475
    verbs = _action_words(scene.get("body", ""))
    defaults = ["ПІДГОТУВАТИ", "ПОДАТИ", "ЗБЕРЕГТИ"]
    labels = verbs if len(verbs) >= 2 else defaults
    labels = (labels + defaults)[:3]
    line_y = y0 + 112
    d.line((x0 + 75, line_y, x0 + 495, line_y), fill=(190, 177, 224, 255), width=5)
    for i, label in enumerate(labels):
        t = (elapsed - 0.22 - i * 0.22) / 0.45
        alpha = _ease_out(t)
        cx = x0 + 80 + i * 205
        cy = line_y + int((1 - alpha) * 35)
        radius = 39
        d.ellipse((cx-radius, cy-radius, cx+radius, cy+radius),
                  fill=_alpha(PURPLE, alpha), outline=_alpha(PURPLE_DARK, alpha), width=2)
        numfont = _font(_find_font(True), 27)
        num = f"0{i+1}"
        nw = d.textlength(num, font=numfont)
        d.text((cx - nw/2, cy - 18), num, font=numfont, fill=_alpha(WHITE, alpha))
        f = _font(_find_font(True), 16)
        label = label[:17]
        tw = d.textlength(label, font=f)
        d.text((cx - tw/2, cy + 65), label, font=f, fill=_alpha(INK, alpha))
    # Three floating cards give the timeline a tactile interface feel.
    for i in range(3):
        bob = int(5 * math.sin(elapsed * 2.1 + i))
        x = 65 + i * 205
        y = 660 + bob + int((1 - _ease_out((elapsed - 0.45 - i * 0.14) / .4)) * 35)
        d.rounded_rectangle((x, y, x + 176, y + 76), radius=14,
                            fill=(255,255,255,235), outline=(219,216,229,255), width=2)
        d.rounded_rectangle((x + 16, y + 18, x + 56, y + 25), radius=3, fill=(*PURPLE,255))
        d.rounded_rectangle((x + 16, y + 40, x + 145, y + 47), radius=3, fill=(218,215,226,255))


def _key_points(text: str) -> List[str]:
    fragments = re.split(r"\s*(?:[,;:—–]|\b(?:але|якщо|коли|тому|оскільки|водночас|також)\b)\s*", text, flags=re.I)
    points: List[str] = []
    for fragment in fragments:
        value = re.sub(r"\s+", " ", fragment).strip(" .-")
        if len(value) < 9:
            continue
        if len(value) > 54:
            words = value.split()
            value = " ".join(words[:7])
        if value.lower() not in [p.lower() for p in points]:
            points.append(value)
        if len(points) == 3:
            break
    if len(points) < 3:
        words = text.split()
        while words and len(points) < 3:
            part = words[:5]
            words = words[5:]
            candidate = " ".join(part).strip(" .,-")
            if candidate and candidate.lower() not in [p.lower() for p in points]:
                points.append(candidate)
    return points[:3]


def _short_heading(text: str) -> str:
    words = text.strip().split()
    if not words:
        return "ПРАВОВИЙ ФАКТ"
    # Use the actual beginning of the approved sentence rather than inventing a legal conclusion.
    chosen = words[:5]
    phrase = " ".join(chosen).strip(" ,;:")
    if len(phrase) < len(text) and phrase:
        phrase += "…"
    return phrase.upper()


def _draw_generic_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    points = _key_points(scene.get("body", ""))
    if len(points) < 3:
        points = (points + ["ПРАВОВА ІНФОРМАЦІЯ", "ВАЖЛИВІ ОБСТАВИНИ", "ПРАКТИЧНИЙ ВИСНОВОК"])[:3]
    cards = [
        (70, 472, 570, 174, "ФАКТ", PURPLE_LIGHT),
        (112, 605, 570, 174, "УМОВА", (255,255,255)),
        (70, 738, 570, 174, "ЩО ВАЖЛИВО", (255,255,255)),
    ]
    for i, (x, y, w, h, label, fill) in enumerate(cards):
        t = (elapsed - 0.16 - i * 0.19) / 0.45
        alpha = _ease_out(t)
        slide = int((1 - alpha) * (70 if i % 2 == 0 else -70))
        yy = y + int(4 * math.sin(elapsed * 2 + i)) + int((1-alpha)*18)
        # Soft, composited-looking shadows; the frame is flattened over the light canvas at the end.
        d.rounded_rectangle((x + slide + 5, yy + 7, x+w+slide+5, yy+h+7), radius=18,
                            fill=(25, 20, 40, int(alpha*28)))
        d.rounded_rectangle((x + slide, yy, x+w+slide, yy+h), radius=18,
                            fill=_alpha(fill, alpha), outline=_alpha((205,201,216), alpha), width=2)
        f = _font(_find_font(True), 17)
        d.text((x + slide + 22, yy + 17), label, font=f,
               fill=_alpha(PURPLE_DARK if i == 0 else INK, alpha))
        d.rounded_rectangle((x + slide + 22, yy + 47, x + slide + 126, yy + 53),
                            radius=3, fill=_alpha(PURPLE, alpha))
        point_font, lines, lh = _fit(d, points[i], _find_font(True), w-48, 82, 24, 18, 1.10)
        py = yy + 69
        for j, line in enumerate(lines):
            d.text((x + slide + 22, py + j*lh + int((1-alpha)*10)), line,
                   font=point_font, fill=_alpha(INK, alpha))


def _draw_cover(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    label_font = _font(_find_font(True), 18)
    p = _ease_out((elapsed - .10) / .35)
    d.rounded_rectangle((44, 142, 282, 188), radius=9, fill=_alpha(PURPLE_LIGHT[:3], p))
    d.text((60, 154), scene["label"], font=label_font, fill=_alpha(PURPLE_DARK, p))
    # Large editorial title; each line lands with a stagger.
    y_end = _draw_word_reveal(
        d, scene["title"].upper(), _find_font(True), (48, 250),
        max_width=620, max_height=400, preferred=76, elapsed=elapsed - .22,
        color=INK, line_delay=.15
    )
    # Animated document / legal stamp motif, no human face or stock person.
    stamp_p = _spring((elapsed - .72) / .7)
    cx, cy = 550, 890
    r = int(125 * stamp_p)
    d.ellipse((cx-r, cy-r, cx+r, cy+r), outline=_alpha(PURPLE, .55), width=5)
    d.ellipse((cx-int(r*.75), cy-int(r*.75), cx+int(r*.75), cy+int(r*.75)),
              outline=_alpha(PURPLE, .32), width=3)
    d.line((cx-66, cy, cx-18, cy+45), fill=_alpha(PURPLE_DARK, stamp_p), width=13)
    d.line((cx-18, cy+45, cx+76, cy-65), fill=_alpha(PURPLE_DARK, stamp_p), width=13)
    # A small stack of document cards slides from the edge.
    for i in range(3):
        offset = int((1-_ease_out((elapsed-.95-i*.12)/.48))*100)
        x, y = 90+i*28+offset, 770+i*18
        _round_rect(d, (x, y, x+210, y+118), 12, (255,255,255,230),
                    (207,203,217,220), 2)
        d.rounded_rectangle((x+16,y+20,x+95,y+27),radius=3,fill=(132,82,247,190))
        d.rounded_rectangle((x+16,y+42,x+180,y+49),radius=3,fill=(207,204,215,230))
        d.rounded_rectangle((x+16,y+65,x+145,y+72),radius=3,fill=(207,204,215,230))


def _draw_content(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    tag_font = _font(_find_font(True), 18)
    a = _ease_out((elapsed - .06) / .30)
    d.text((48, 138), scene["label"], font=tag_font, fill=_alpha(PURPLE_DARK, a))
    d.rounded_rectangle((48, 174, 210, 181), radius=4, fill=_alpha(PURPLE, a))
    title = _short_heading(scene.get("body", "")) 
    _draw_word_reveal(d, title, _find_font(True), (48, 218), 620, 170, 54,
                      elapsed - .15, INK, line_delay=.12)
    body = scene.get("body", "")
    if _citation(body):
        _draw_document_visual(canvas, scene, elapsed)
    elif _numbers(body):
        _draw_metric_visual(canvas, scene, elapsed)
    elif _action_words(body):
        _draw_steps_visual(canvas, scene, elapsed)
    else:
        _draw_generic_visual(canvas, scene, elapsed)
    # Captions are the actual approved copy, not generated paraphrases.
    box_top = 960
    slide = int((1 - _ease_out((elapsed - .50) / .42)) * 64)
    d.rounded_rectangle((42, box_top + slide + 8, WIDTH-42, 1198 + slide + 8),
                        radius=17, fill=(18, 14, 35, 20))
    d.rounded_rectangle((42, box_top + slide, WIDTH-42, 1198 + slide),
                        radius=17, fill=(255,255,255,250), outline=(150,145,164,255), width=2)
    d.rounded_rectangle((42, box_top + slide, 52, 1198 + slide), radius=5, fill=(*PURPLE,255))
    body_font, body_lines, line_h = _fit(d, body, _find_font(True), 590, 190, 31, 23, 1.14)
    # Stagger by line to create a readable subtitle-style text reveal.
    yy = box_top + slide + max(22, int((216-len(body_lines)*line_h)/2))
    for i, line in enumerate(body_lines):
        alpha = _ease_out((elapsed - .66 - i*.07) / .35)
        d.text((72, yy + int((1-alpha)*12)), line, font=body_font, fill=_alpha(INK, alpha))
        yy += line_h


def _draw_story(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    _draw_cover(canvas, scene, elapsed * .55)
    d = ImageDraw.Draw(canvas, "RGBA")
    body = scene.get("body", "")
    box_top = 915
    d.rounded_rectangle((42, box_top, WIDTH-42, 1195), radius=18,
                        fill=(255,255,255,246), outline=(155,148,171,255), width=2)
    f, lines, lh = _fit(d, body, _find_font(True), 590, 220, 30, 22, 1.12)
    y = box_top + 30
    for i,line in enumerate(lines):
        alpha = _ease_out((elapsed - .8 - i*.08) / .36)
        d.text((68, y + int((1-alpha)*12)), line, font=f, fill=_alpha(INK, alpha))
        y += lh


def _draw_closing(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    p = _spring(elapsed / .65)
    y = 710 + int((1-p)*100)
    d.rounded_rectangle((-35, y, WIDTH+35, 1170), radius=42, fill=(132,82,247,255))
    title_text = scene.get("title", "ЗБЕРЕЖІТЬ МАТЕРІАЛ").upper()
    _draw_word_reveal(d, title_text, _find_font(True), (48, 250), 620, 360,
                      78, elapsed - .12, INK, line_delay=.15)
    brand_font = _font(_find_font(True), 22)
    d.text((52, y + 52), "SENTINEL LAW", font=brand_font, fill=WHITE)
    # Full original CTA is retained in the closing panel, in a smaller but readable size.
    body = scene.get("body", "")
    body_font, body_lines, line_h = _fit(d, body, _find_font(True), 600, 160, 31, 22, 1.12)
    yy = y + 132
    for i, line in enumerate(body_lines):
        alpha = _ease_out((elapsed - .55 - i*.08) / .38)
        d.text((52, yy + int((1-alpha)*14)), line, font=body_font, fill=_alpha(WHITE, alpha))
        yy += line_h
    # Kinetic square markers drift gently, echoing the reference's UI elements.
    for i in range(6):
        offset = int(10 * math.sin(elapsed*2.8+i))
        xx = 510 + (i%3)*48
        yy2 = 380 + (i//3)*48 + offset
        d.rounded_rectangle((xx, yy2, xx+27, yy2+27), radius=7, fill=(132,82,247,115))


def _draw_frame(scene: Dict[str, str], scene_index: int, total_scenes: int,
                local_frame: int, global_frame: int, total_frames: int) -> Image.Image:
    elapsed = local_frame / FPS
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    _draw_background(canvas, global_frame, scene_index)
    progress = global_frame / max(1, total_frames - 1)
    _draw_header(canvas, scene_index, total_scenes, progress)
    kind = scene["kind"]
    if kind == "cover":
        _draw_cover(canvas, scene, elapsed)
    elif kind == "closing":
        _draw_closing(canvas, scene, elapsed)
    elif kind == "story":
        _draw_story(canvas, scene, elapsed)
    else:
        _draw_content(canvas, scene, elapsed)
    base = Image.new("RGBA", (WIDTH, HEIGHT), (*BG, 255))
    return Image.alpha_composite(base, canvas).convert("RGB")


def _synth_music(path: Path, duration: float, seed_text: str, bpm: float = 112.0) -> None:
    """Create original stereo music and short synthetic transition accents."""
    n = int(AUDIO_SR * duration)
    t = np.arange(n, dtype=np.float32) / AUDIO_SR
    rng = np.random.default_rng(int(hashlib.sha256(seed_text.encode()).hexdigest()[:8], 16))
    beat = 60.0 / bpm
    bar = beat * 4
    chords = [
        (55.0, [220.0, 261.63, 329.63]),
        (43.65, [174.61, 220.0, 261.63]),
        (65.41, [196.0, 261.63, 329.63]),
        (49.0, [196.0, 246.94, 293.66]),
    ]
    left = np.zeros(n, dtype=np.float32)
    right = np.zeros(n, dtype=np.float32)
    chord_idx = (t / bar).astype(np.int32) % 4
    local = np.mod(t, bar)
    fade = np.minimum(1, np.minimum(local / .45, (bar-local)/.55)).astype(np.float32)
    for i, (_, notes) in enumerate(chords):
        mask = chord_idx == i
        for j, freq in enumerate(notes):
            amp = .020 / (1 + j*.25)
            left[mask] += amp * np.sin(2*np.pi*freq*t[mask] + j*.28)
            right[mask] += amp * np.sin(2*np.pi*freq*1.002*t[mask] + j*.35)
    left *= fade
    right *= fade

    # Bass pulse + kick, clap and high-hat.
    kick_len = int(.22*AUDIO_SR)
    kt = np.arange(kick_len, dtype=np.float32)/AUDIO_SR
    kick = (.24*np.sin(2*np.pi*(58-30*kt/.22)*kt)*np.exp(-kt*19)).astype(np.float32)
    clap_len = int(.16*AUDIO_SR)
    ct = np.arange(clap_len, dtype=np.float32)/AUDIO_SR
    clap = (.065*rng.normal(0,1,clap_len)*np.exp(-ct*24) +
            .035*np.sin(2*np.pi*190*ct)*np.exp(-ct*21)).astype(np.float32)
    hat_len = int(.045*AUDIO_SR)
    ht = np.arange(hat_len, dtype=np.float32)/AUDIO_SR
    hat = (.018*rng.normal(0,1,hat_len)*np.exp(-ht*85)).astype(np.float32)

    def add(sample: np.ndarray, start: float, pan: float=0.0, gain: float=1.0):
        i0 = int(start*AUDIO_SR)
        if i0 >= n: return
        i1 = min(n, i0+len(sample))
        part = sample[:i1-i0]*gain
        left[i0:i1] += part*(1-max(0,pan))
        right[i0:i1] += part*(1+min(0,pan))

    for k, start in enumerate(np.arange(0, duration, beat)):
        chord = chords[int(start/bar)%4]
        bass_len = int(.28*AUDIO_SR)
        bt = np.arange(bass_len,dtype=np.float32)/AUDIO_SR
        bass = (.052*np.sin(2*np.pi*chord[0]*bt)*np.exp(-bt*8)).astype(np.float32)
        add(bass,start)
        if k%4 in (0,2): add(kick,start,gain=.76)
        else: add(clap,start,pan=.07 if k%2 else -.07)
        add(hat,start,pan=.18 if k%2 else -.18)
        add(hat,start+beat/2,pan=-.18 if k%2 else .18)

    # Short whoosh/click cues before each scene. Entirely synthesized; no external sample.
    transition_starts = np.arange(0.0, duration, 5.2)
    for j, start in enumerate(transition_starts):
        length = int(.28*AUDIO_SR)
        q = np.arange(length,dtype=np.float32)/AUDIO_SR
        noise = rng.normal(0,1,length).astype(np.float32)
        sweep = noise*np.sin(np.pi*np.minimum(1,q/.28))*np.exp(-q*7)*.035
        add(sweep,start,pan=(-.15 if j%2 else .15))
        click = (.045*np.sin(2*np.pi*(900+120*j)*q)*np.exp(-q*40)).astype(np.float32)
        add(click,start+.18,pan=(.2 if j%2 else -.2))

    # Peak-safe master before AAC encoding. FFmpeg applies final loudness target.
    peak = max(float(np.max(np.abs(left))), float(np.max(np.abs(right))), 1e-6)
    gain = min(1.0, .72/peak)
    left *= gain
    right *= gain
    envelope = np.clip(t/.55,0,1)*np.clip((duration-t)/.85,0,1)
    stereo = np.stack((left*envelope,right*envelope),axis=1)
    pcm = np.int16(np.clip(stereo,-1,1)*32767)
    with wave.open(str(path),"wb") as wav:
        wav.setnchannels(2); wav.setsampwidth(2); wav.setframerate(AUDIO_SR)
        wav.writeframes(pcm.tobytes())


def _probe(path: Path, ffprobe: str = "ffprobe") -> Dict[str, Any]:
    if not shutil.which(ffprobe):
        return {"size": path.stat().st_size}
    p = subprocess.run([
        ffprobe,"-v","error","-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height,sample_rate,channels",
        "-of","json",str(path)
    ],capture_output=True,text=True,timeout=25)
    if p.returncode:
        raise RuntimeError("ffprobe не зміг перевірити MP4: "+p.stderr[-1000:])
    data=json.loads(p.stdout or "{}")
    streams=data.get("streams",[])
    video=next((s for s in streams if s.get("codec_type")=="video"),None)
    audio=next((s for s in streams if s.get("codec_type")=="audio"),None)
    if not video or video.get("codec_name")!="h264" or (video.get("width"),video.get("height"))!=(WIDTH,HEIGHT):
        raise RuntimeError("Відеопотік не відповідає контракту H.264 720×1280.")
    if not audio or audio.get("codec_name")!="aac" or audio.get("channels")!=2:
        raise RuntimeError("Аудіопотік не відповідає контракту AAC stereo.")
    return {"size":int(data.get("format",{}).get("size",path.stat().st_size)),
            "duration":float(data.get("format",{}).get("duration",0)),"streams":streams}


def build_video(job: Dict[str, Any], output_path: str | Path,
                ffmpeg: str = "ffmpeg") -> Path:
    output=Path(output_path); output.parent.mkdir(parents=True,exist_ok=True)
    if not shutil.which(ffmpeg):
        raise RuntimeError("FFmpeg не встановлений.")
    fmt=str(job.get("format") or "Reels").strip().lower()
    if fmt not in ("reels","story"): fmt="reels"
    content_id=_clean(job.get("content_id","SENTINEL-LAW"),120)
    scenes=_make_scenes(job)
    durations=_scene_durations(scenes,fmt)
    frame_counts=[max(1,round(x*FPS)) for x in durations]
    total_frames=sum(frame_counts)
    duration=total_frames/FPS
    total_bytes_budget=MAX_UPLOAD_BYTES*.91
    audio_bps=112_000
    video_bps=max(420_000,min(1_450_000,int((total_bytes_budget*8/duration)-audio_bps)))
    maxrate=int(video_bps*1.2)
    seed=content_id+str(job.get("headline",""))
    with tempfile.TemporaryDirectory(prefix="sentinel-motion-v3-") as td:
        tmp=Path(td); audio=tmp/"music.wav"
        _synth_music(audio,duration,seed)
        cmd=[
            ffmpeg,"-hide_banner","-loglevel","error","-y",
            "-f","rawvideo","-pix_fmt","rgb24","-s",f"{WIDTH}x{HEIGHT}",
            "-r",str(FPS),"-i","pipe:0","-i",str(audio),
            "-map","0:v:0","-map","1:a:0","-t",f"{duration:.3f}",
            "-c:v","libx264","-preset","ultrafast","-tune","animation",
            "-profile:v","high","-level","4.1","-pix_fmt","yuv420p",
            "-b:v",str(video_bps),"-maxrate",str(maxrate),"-bufsize",str(video_bps*2),
            "-g",str(FPS*2),"-keyint_min",str(FPS*2),"-sc_threshold","0",
            "-threads","2","-c:a","aac","-b:a",str(audio_bps),"-ar",str(AUDIO_SR),"-ac","2",
            "-af","loudnorm=I=-15:TP=-2.5:LRA=7",
            "-movflags","+faststart",str(output)
        ]
        proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        frame_global=0
        try:
            assert proc.stdin is not None
            for i,(scene,count) in enumerate(zip(scenes,frame_counts),start=1):
                for local in range(count):
                    frame=_draw_frame(scene,i,len(scenes),local,frame_global,total_frames)
                    proc.stdin.write(frame.tobytes())
                    frame_global+=1
            proc.stdin.close()
            stderr=proc.stderr.read() if proc.stderr else b""
            code=proc.wait(timeout=max(180,int(duration*8)))
        except Exception:
            proc.kill()
            raise
        if code != 0:
            raise RuntimeError("FFmpeg render failed: "+stderr.decode("utf-8","replace")[-2500:])
    if not output.exists() or output.stat().st_size<100_000:
        raise RuntimeError("MP4 відсутній або має некоректний розмір.")
    info=_probe(output)
    if info.get("size",0)>MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"MP4 має {info['size']} байтів, ліміт {MAX_UPLOAD_BYTES}. "
            "Скороти сценарій — текст автоматично не обрізається."
        )
    print(json.dumps({
        "engine":"SENTINEL LAW Motion Engine V3",
        "content_id":content_id,
        "scene_count":len(scenes),
        "duration_seconds":round(info.get("duration",duration),2),
        "file_bytes":info.get("size"),
        "resolution":f"{WIDTH}x{HEIGHT}",
        "fps":FPS,"audio":"AAC stereo",
        "text_policy":"all audience-facing script text retained",
        "visual_style":"editorial kinetic typography / violet legal infographics"
    },ensure_ascii=False))
    return output


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--job",required=True)
    parser.add_argument("--output",default="sentinel-law-v3.mp4")
    args=parser.parse_args()
    payload=json.loads(Path(args.job).read_text(encoding="utf-8"))
    build_video(payload,args.output)
