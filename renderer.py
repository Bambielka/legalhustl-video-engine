"""SENTINEL LAW Motion Engine V4.

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
    """Return True for production notes, not audience-facing copy."""
    value = line.strip().lstrip("-•* ").strip()
    if not value:
        return True
    if re.match(r"^(?:сцена|scene|кадр)\s*\d+\b", value, re.I):
        return True
    if re.match(r"^\[?(?:0?\d{1,2}:\d{2}|\d{1,2}\s*[–-]\s*\d{1,2}\s*(?:с|сек|секунд))\]?\s*:?$", value, re.I):
        return True
    upper = value.upper()
    # Prefix matching is deliberate: catches headings such as "ВІЗУАЛЬНІ ПРИМІТКИ:".
    direction_patterns = (
        r"^(?:ВІЗУАЛ\w*|ВІДЕОРЯД|КАДР\w*|КАМЕРА|АНІМАЦІ\w*|ЕФЕКТ\w*|ПЕРЕХІД\w*|"
        r"МУЗИКА|ЗВУК|ТАЙМІНГ|ТРИВАЛІСТЬ|ГРАФІКА|КОМПОЗИЦІ\w*|ІНСТРУКЦІ\w*|"
        r"ПІДКАЗКА|ПЛАШКА|ФОН|СЦЕНАРІЙ|ФОРМАТ|ТЕХНІЧНЕ ЗАВДАННЯ|VIDEO PROMPT|"
        r"РЕЖИСЕРСЬКА ВКАЗІВКА|ОПИС СЦЕНИ|ВКАЗІВКИ ДЛЯ МОНТАЖУ)(?:\s*\([^)]*\))?\s*:",
        r"^(?:ВИКОРИСТОВУЙ|ВИКОРИСТОВУВАТИ|ПОКАЖИ|ПОКАЗАТИ|НЕ ПОКАЗУЙ|НЕ ПОКАЗУВАТИ|"
        r"ЗРОБИ|ЗРОБИТИ|ДОДАЙ|ДОДАТИ|ВИКОРИСТАЙ|ВИКОРИСТАТИ|АНІМУЙ|АНІМУВАТИ)\b",
    )
    return any(re.search(pattern, upper, re.I) for pattern in direction_patterns)


def _is_production_instruction(text: str) -> bool:
    value = re.sub(r"\s+", " ", text or "").strip().upper()
    if not value:
        return True
    patterns = (
        r"^(?:ВИКОРИСТОВУВАТИ|ВИКОРИСТОВУЙ|ВИКОРИСТАЙ|ПОКАЗАТИ|ПОКАЖИ|НЕ ПОКАЗУВАТИ|"
        r"НЕ ПОКАЗУЙ|ЗРОБИТИ|ЗРОБИ|ДОДАТИ|ДОДАЙ|АНІМУВАТИ|АНІМУЙ)\b",
        r"^(?:ВІЗУАЛЬНІ? ПРИМІТКИ|РЕЖИСЕРСЬКІ ВКАЗІВКИ|ТЕХНІЧНЕ ЗАВДАННЯ)\b",
    )
    return any(re.search(pattern, value, re.I) for pattern in patterns)


def _extract_script_text(script: str) -> str:
    """Extract only audience-facing content from free-form or production-style scripts.

    Supported script labels are retained after removing their labels. Production directions
    and their continuation lines are discarded. Plain prose remains untouched.
    """
    source = _clean(script)
    if not source:
        return ""
    keep_labels = (
        "ТЕКСТ НА ЕКРАНІ", "ТЕКСТ ДИКТОРА", "ОЗВУЧКА", "ДИКТОР",
        "РЕПЛІКА", "ПОВІДОМЛЕННЯ", "НА ЕКРАНІ", "ГОЛОС ЗА КАДРОМ", "СУБТИТРИ",
        "ФАКТ", "УМОВА", "ЩО ВАЖЛИВО", "МІФ", "ПРАВДА", "ПОМИЛКА", "ПРАВИЛО",
        "АЛГОРИТМ", "ДІЯ", "РИЗИК", "ВИСНОВОК", "ЗАПИТАННЯ", "ВІДПОВІДЬ",
        "ЩО ЗМІНИЛОСЯ", "ГОЛОВНЕ"
    )
    technical_heading = re.compile(
        r"^(?:ВІЗУАЛ\w*|ВІДЕОРЯД|КАМЕРА|АНІМАЦІ\w*|ЕФЕКТ\w*|ПЕРЕХІД\w*|"
        r"МУЗИКА|ЗВУК|ТАЙМІНГ|ТРИВАЛІСТЬ|ГРАФІКА|КОМПОЗИЦІ\w*|ІНСТРУКЦІ\w*|"
        r"РЕЖИСЕРСЬКА ВКАЗІВКА|ТЕХНІЧНЕ ЗАВДАННЯ|ОПИС СЦЕНИ|ПІДКАЗКА ДЛЯ МОНТАЖУ)"
        r"(?:\s*\([^)]*\))?\s*:\s*(.*)$", re.I
    )
    out: List[str] = []
    skip_continuation = False
    for raw in source.split("\n"):
        line = raw.strip().lstrip("-•* ").strip()
        if not line:
            continue
        # A new explicit audience-text field ends a preceding production-notes block.
        audience_match = None
        for label in keep_labels:
            m = re.match(r"^" + re.escape(label) + r"\s*:\s*(.*)$", line, re.I)
            if m:
                audience_match = m
                break
        if audience_match:
            value = audience_match.group(1).strip()
            skip_continuation = False
            if value and not _is_production_instruction(value):
                if label in ("МІФ", "ПРАВДА"):
                    out.append(label + ": " + value)
                else:
                    out.append(value)
            continue
        if technical_heading.match(line):
            skip_continuation = True
            continue
        if _is_direction(line):
            skip_continuation = True
            continue
        if skip_continuation:
            # Bulleted or imperative continuation lines still belong to director notes.
            if _is_production_instruction(line) or re.match(r"^(?:[-•*]|\d+[.)])\s*", raw.strip()):
                continue
            # Ordinary sentence resumes the actual script.
            skip_continuation = False
        if re.match(r"^(?:ведучий|ведуча|голос за кадром|завершення|вступ)\s*:\s*$", line, re.I):
            continue
        if _is_production_instruction(line):
            continue
        # Drop duplicated fragments of a direction accidentally embedded in a content field.
        line = re.sub(r"(?i)\bвізуальні? примітки\s*:?\s*", "", line).strip()
        line = re.sub(r"(?i)\bвикористовувати текстові картки\b[^.]*\.?", "", line).strip()
        if line:
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


VIDEO_TYPE_LABELS = {
    "news": "НОВИНА / ЗМІНИ",
    "explainer": "ПРАВОВЕ ПОЯСНЕННЯ",
    "howto": "ПОКРОКОВА ІНСТРУКЦІЯ",
    "myth_fact": "МІФ / ПРАВДА",
    "case": "СУДОВА ПРАКТИКА",
    "checklist": "ЧЕКЛІСТ",
    "comparison": "ПОРІВНЯННЯ",
    "qa": "ПИТАННЯ / ВІДПОВІДЬ",
    "risk": "ЮРИДИЧНИЙ РИЗИК",
}


def _infer_video_type(job: Dict[str, Any]) -> str:
    """Select an editorial video structure from explicit metadata or source wording."""
    raw = str(job.get("video_type") or job.get("visual_type") or job.get("content_type") or "").strip().lower()
    aliases = {
        "news": "news", "новина": "news", "зміни законодавства": "news", "законодавчі зміни": "news",
        "explainer": "explainer", "пояснення": "explainer", "роз'яснення": "explainer", "роз’яснення": "explainer",
        "howto": "howto", "інструкція": "howto", "алгоритм": "howto", "покрокова інструкція": "howto",
        "myth_fact": "myth_fact", "міф/правда": "myth_fact", "міф / правда": "myth_fact", "міф-факт": "myth_fact",
        "case": "case", "судова справа": "case", "судова практика": "case", "кейс": "case",
        "checklist": "checklist", "чекліст": "checklist", "перелік": "checklist",
        "comparison": "comparison", "порівняння": "comparison", "було/стало": "comparison",
        "qa": "qa", "питання-відповідь": "qa", "питання / відповідь": "qa",
        "risk": "risk", "ризики": "risk", "попередження": "risk",
    }
    if raw in aliases:
        return aliases[raw]
    text = " ".join(str(job.get(k) or "") for k in ("topic", "headline", "script", "caption")).lower()
    # Strong semantic signals take precedence over generic words such as "постанова".
    if re.search(r"\b(міф|правда чи міф|неправда|поширене твердження|насправді)\b", text):
        return "myth_fact"
    if re.search(r"(справа\s*№|єдрср|суд постановив|суд дійшов висновку|рішення суду|судове рішення|правова позиція суду|позивач|відповідач|у справі|суд.{0,30}справ)", text):
        return "case"
    if re.search(r"(чекліст|перелік документів|які документи|список документів|що підготувати|перевірте наявність)", text):
        return "checklist"
    if re.search(r"(було\s*/\s*стало|до\s*/\s*після|порівнян|відмінност|на відміну від|чим відрізняється)", text):
        return "comparison"
    if re.search(r"(не робіть|не можна ігнорувати|ризик|ризики|штраф|відповідальність|може призвести|небезпек|помилка коштує)", text):
        return "risk"
    if re.search(r"(як (?:отримати|подати|оскаржити|оформити|звернутися|діяти)|покроков|алгоритм дій|послідовність дій|етапи|що робити спочатку)", text):
        return "howto"
    if re.search(r"(чи має право|чи можна|чи повинен|що робити\??|як діяти\??|\?)", str(job.get("headline") or "").lower()):
        return "qa"
    if re.search(r"(ухвалено|ухвалила|ухвалив|набра(в|ла) чинності|законопроєкт|внесено зміни|затверджено|затвердив|запроваджено|оновлено порядок|оприлюднено)", text):
        return "news"
    return "explainer"


def _layout_for(video_type: str, index: int, content_id: str) -> str:
    """Cycle among related layouts so two videos of the same type need not look identical."""
    salt = sum(ord(ch) for ch in str(content_id))
    variant = (index + salt) % 2
    variants = {
        "news": ("news_bulletin", "news_timeline"),
        "explainer": ("concept_map", "editorial_layers"),
        "howto": ("steps_rail", "steps_cards"),
        "myth_fact": ("myth_split", "myth_reveal"),
        "case": ("case_timeline", "case_dossier"),
        "checklist": ("checklist_grid", "checklist_stack"),
        "comparison": ("comparison_split", "comparison_scale"),
        "qa": ("question_card", "answer_focus"),
        "risk": ("risk_alert", "risk_path"),
    }
    return variants.get(video_type, ("concept_map", "editorial_layers"))[variant]


def _make_scenes(job: Dict[str, Any]) -> List[Dict[str, str]]:
    fmt = str(job.get("format") or "Reels").strip().lower()
    headline = _clean(job.get("headline") or job.get("topic") or "Правовий розбір", 500)
    script = _extract_script_text(str(job.get("script") or job.get("caption") or headline))
    if not script:
        script = headline
    cta = _clean(job.get("cta") or "Збережіть матеріал, щоб повернутися до нього", 400)
    content_id = str(job.get("content_id") or job.get("job_id") or headline)
    video_type = _infer_video_type({**job, "script": script})
    type_label = VIDEO_TYPE_LABELS.get(video_type, "ПРАВОВИЙ РОЗБІР")
    if fmt == "story":
        return [{
            "kind": "story", "label": "SENTINEL LAW", "title": _clean(job.get("story_title") or headline, 400),
            "body": script, "video_type": video_type, "layout": _layout_for(video_type, 0, content_id),
        }]
    chunks = _split_script(script)
    scenes: List[Dict[str, str]] = [{
        "kind": "cover", "label": type_label, "title": headline, "body": "", "video_type": video_type,
    }]
    for i, chunk in enumerate(chunks, start=1):
        scenes.append({
            "kind": "content", "label": type_label, "title": "", "body": chunk,
            "video_type": video_type, "layout": _layout_for(video_type, i, content_id),
            "sequence": str(i),
        })
    cta_words = cta.split()
    closing_title = " ".join(cta_words[:2]) if len(cta_words) > 2 else cta
    scenes.append({"kind": "closing", "label": "SENTINEL LAW", "title": closing_title, "body": cta, "video_type": video_type})
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
            points.append(_visual_excerpt(value, 6))
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


def _visual_excerpt(text: str, max_words: int = 6) -> str:
    words = re.sub(r"\s+", " ", text or "").strip().split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip(" ,;:") + "…"


def _visual_points(text: str, limit: int = 3) -> List[str]:
    """Make non-repeating visual excerpts strictly from the supplied sentence."""
    source = re.sub(r"\s+", " ", text or "").strip()
    if not source:
        return []
    candidates = re.split(r"(?<=[.!?;])\s+|\s*,\s*|\s+(?:але|якщо|коли|тому|оскільки|водночас|також)\s+", source, flags=re.I)
    points: List[str] = []
    for value in candidates:
        value = value.strip(" .,:;—–-")
        if len(value) >= 8 and value.lower() not in [x.lower() for x in points]:
            points.append(_visual_excerpt(value, 6))
        if len(points) >= limit:
            return points[:limit]
    # If it is one uninterrupted sentence, partition its original words; do not clone copy.
    words = source.split()
    if len(points) < limit and len(words) > 1:
        chunk_size = max(1, math.ceil(len(words) / limit))
        points = [_visual_excerpt(" ".join(words[i:i + chunk_size]), 6)
                  for i in range(0, len(words), chunk_size)]
        points = [x for x in points if x]
    return points[:limit]


def _draw_cover_motif(draw: ImageDraw.ImageDraw, video_type: str, elapsed: float) -> None:
    """Distinct cover motif for each editorial format; no stock people or fabricated seals."""
    p = _spring((elapsed - .72) / .7)
    if video_type == "news":
        draw.rounded_rectangle((82, 800, 638, 910), radius=18, fill=(255,255,255,235), outline=(210,206,219,230), width=2)
        draw.rounded_rectangle((105, 825, 248, 865), radius=8, fill=(*PURPLE_LIGHT,255))
        draw.text((119, 836), "ОНОВЛЕННЯ", font=_font(_find_font(True),14), fill=PURPLE_DARK)
        draw.line((280,846,600,846), fill=(193,182,222,255), width=5)
        for x in (290,430,585):
            r=int(10*p); draw.ellipse((x-r,846-r,x+r,846+r),fill=(*PURPLE,255))
    elif video_type == "howto":
        for i,x in enumerate((150,360,570)):
            r=int(37*p); y=850
            draw.ellipse((x-r,y-r,x+r,y+r), fill=(132,82,247,245), outline=(102,54,211,255), width=2)
            f=_font(_find_font(True),26); label=f"{i+1:02d}"; w=draw.textlength(label,font=f)
            draw.text((x-w/2,y-17),label,font=f,fill=WHITE)
            if i<2: draw.line((x+45,y,x+165,y),fill=(193,182,222,255),width=4)
    elif video_type == "myth_fact":
        draw.rounded_rectangle((80,790,340,910),radius=16,fill=(255,255,255,240),outline=(210,206,219,255),width=2)
        draw.rounded_rectangle((380,790,640,910),radius=16,fill=(*PURPLE_LIGHT,255),outline=(*PURPLE,255),width=2)
        draw.text((132,829),"МІФ",font=_font(_find_font(True),26),fill=MUTED)
        draw.text((432,829),"ФАКТ",font=_font(_find_font(True),26),fill=PURPLE_DARK)
        draw.text((344,830),"/",font=_font(_find_font(True),26),fill=PURPLE_DARK)
    elif video_type == "case":
        draw.rounded_rectangle((125,785,595,925),radius=16,fill=(255,255,255,235),outline=(210,206,219,255),width=2)
        draw.line((170,820,170,885),fill=(*PURPLE,255),width=5)
        for y in (825,855,885): draw.ellipse((158,y-7,182,y+7),fill=(*PURPLE,255))
        for i,y in enumerate((820,850,880)):
            draw.rounded_rectangle((205,y,545,y+7),radius=3,fill=(200,196,211,220))
    elif video_type == "checklist":
        for i,(x,y) in enumerate(((110,800),(280,835),(450,800))):
            draw.rounded_rectangle((x,y,x+120,y+92),radius=14,fill=(255,255,255,240),outline=(210,206,219,255),width=2)
            draw.rounded_rectangle((x+25,y+25,x+95,y+33),radius=4,fill=(*PURPLE,240))
            draw.rounded_rectangle((x+25,y+48,x+82,y+56),radius=4,fill=(205,201,215,235))
    elif video_type == "comparison":
        draw.rounded_rectangle((75,800,340,920),radius=16,fill=(255,255,255,240),outline=(210,206,219,255),width=2)
        draw.rounded_rectangle((380,800,645,920),radius=16,fill=(*PURPLE_LIGHT,255),outline=(*PURPLE,255),width=2)
        draw.text((165,835),"A",font=_font(_find_font(True),46),fill=INK)
        draw.text((470,835),"B",font=_font(_find_font(True),46),fill=PURPLE_DARK)
    elif video_type == "qa":
        draw.ellipse((405,755,625,975),outline=(132,82,247,110),width=5)
        draw.text((468,760),"?",font=_font(_find_font(True),155),fill=(*PURPLE,230))
    elif video_type == "risk":
        points=[(540,760),(660,940),(420,940)]
        draw.polygon(points,fill=(*PURPLE_LIGHT,255),outline=(*PURPLE,255))
        draw.text((515,810),"!",font=_font(_find_font(True),95),fill=PURPLE_DARK)
    else:
        for i,(x,y) in enumerate(((150,850),(360,790),(570,850))):
            r=int(33*p); draw.ellipse((x-r,y-r,x+r,y+r),outline=(132,82,247,185),width=4)
            draw.line((x-16,y,x-4,y+13),fill=(*PURPLE_DARK,255),width=7)
            draw.line((x-4,y+13,x+20,y-18),fill=(*PURPLE_DARK,255),width=7)
            if i<2: draw.line((x+34,y,x+175,y-35 if i==0 else y+35),fill=(203,194,224,255),width=3)


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
    # Cover motif follows the editorial type instead of repeating one generic stamp.
    _draw_cover_motif(d, scene.get("video_type", "explainer"), elapsed)


def _draw_visual_card(draw: ImageDraw.ImageDraw, box, label: str, body: str,
                      elapsed: float, accent=True, body_size: int = 23) -> None:
    x1, y1, x2, y2 = box
    alpha = _ease_out((elapsed - 0.12) / 0.42)
    lift = int((1 - alpha) * 28)
    draw.rounded_rectangle((x1 + 6, y1 + lift + 8, x2 + 6, y2 + lift + 8), radius=18,
                           fill=(18, 14, 35, int(alpha * 22)))
    draw.rounded_rectangle((x1, y1 + lift, x2, y2 + lift), radius=18,
                           fill=_alpha(WHITE, alpha), outline=_alpha((208, 204, 218), alpha), width=2)
    f_label = _font(_find_font(True), 16)
    draw.text((x1 + 22, y1 + 18 + lift), label.upper(), font=f_label,
              fill=_alpha(PURPLE_DARK if accent else MUTED, alpha))
    draw.rounded_rectangle((x1 + 22, y1 + 47 + lift, x1 + 104, y1 + 53 + lift), radius=3,
                           fill=_alpha(PURPLE if accent else LINE, alpha))
    visual_body = _visual_excerpt(body, 6)
    font, lines, lh = _fit(draw, visual_body, _find_font(True), x2 - x1 - 44, y2 - y1 - 84, body_size, 17, 1.10)
    yy = y1 + 70 + lift
    for line in lines:
        draw.text((x1 + 22, yy), line, font=font, fill=_alpha(INK, alpha))
        yy += lh


def _draw_news_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, timeline=False) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    body = scene.get("body", "")
    p = _ease_out((elapsed - .15) / .5)
    d.rounded_rectangle((60, 460, 660, 900), radius=24, fill=(255,255,255,242), outline=(210,206,219,255), width=2)
    d.rounded_rectangle((84, 485, 240, 527), radius=9, fill=(*PURPLE_LIGHT,255))
    d.text((100, 497), "ОФІЦІЙНЕ ОНОВЛЕННЯ", font=_font(_find_font(True), 14), fill=PURPLE_DARK)
    d.text((85, 555), "ЩО ЗМІНИЛОСЯ", font=_font(_find_font(True), 27), fill=INK)
    excerpt = _visual_excerpt(body, 5)
    font, lines, lh = _fit(d, excerpt, _find_font(True), 500, 132, 27, 19, 1.12)
    y = 610
    for line in lines:
        d.text((86, y), line, font=font, fill=INK); y += lh
    if timeline:
        yline = 810
        d.line((100, yline, 615, yline), fill=(190,178,220,255), width=4)
        for i, x in enumerate((112, 365, 608)):
            r = int(12 * p)
            d.ellipse((x-r,yline-r,x+r,yline+r), fill=(*PURPLE,255))
        labels = ["ПОДІЯ", "НОРМА", "ЗАСТОСУВАННЯ"]
        for x, label in zip((112,365,608), labels):
            f = _font(_find_font(True), 13); w = d.textlength(label,font=f)
            d.text((x-w/2, 838),label,font=f,fill=MUTED)
    else:
        d.rounded_rectangle((86, 808, 610, 824), radius=7, fill=(239,235,246,255))
        d.rounded_rectangle((86, 808, 86 + int(524*p), 824), radius=7, fill=(*PURPLE,255))


def _draw_explainer_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, layered=False) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    body = scene.get("body", "")
    points = _visual_points(body) or [body]
    points = (points + _visual_points(body)[len(points):])[:3]
    if layered:
        boxes = [(70,455,650,575),(105,610,615,730),(70,765,650,885)]
        labels = ["КЛЮЧОВА ТЕЗА", "ПРАВОВИЙ КОНТЕКСТ", "ЩО ВАРТО ВРАХУВАТИ"]
        for i, (box, point, label) in enumerate(zip(boxes, points, labels)):
            _draw_visual_card(d, box, label, point, elapsed - i*.12, accent=(i==1), body_size=22)
    else:
        # Three connected nodes: visually explains dependencies without inventing extra legal content.
        d.line((155, 650, 565, 650), fill=(202,190,227,255), width=6)
        xs = (155,360,565)
        labels = ["01", "02", "03"]
        for i, (x, point) in enumerate(zip(xs, points)):
            a = _ease_out((elapsed-.18-i*.18)/.45)
            r = 43
            d.ellipse((x-r,607-r,x+r,607+r), fill=_alpha(PURPLE,a), outline=_alpha(PURPLE_DARK,a), width=2)
            f = _font(_find_font(True), 25); tw = d.textlength(labels[i],font=f)
            d.text((x-tw/2,592),labels[i],font=f,fill=_alpha(WHITE,a))
            _draw_visual_card(d,(x-92,690,x+92,870),"ЕЛЕМЕНТ",point,elapsed-.25-i*.14,accent=(i==1),body_size=17)


def _draw_howto_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, cards=False) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    points = _visual_points(scene.get("body", "")) or [scene.get("body", "")]
    points = (points + _visual_points(scene.get("body", ""))[len(points):])[:3]
    if cards:
        for i, y in enumerate((470, 615, 760)):
            x = 66 + (i%2)*24
            _draw_visual_card(d,(x,y,654-(i%2)*24,y+118),f"КРОК 0{i+1}",points[i],elapsed-i*.16,accent=True,body_size=20)
    else:
        d.line((110, 500, 110, 880), fill=(195,183,221,255), width=6)
        for i, y in enumerate((520, 690, 860)):
            a = _ease_out((elapsed-.15-i*.2)/.45)
            d.ellipse((80,y-30,140,y+30),fill=_alpha(PURPLE,a),outline=_alpha(PURPLE_DARK,a),width=2)
            f = _font(_find_font(True),20); label=f"{i+1:02d}"; w=d.textlength(label,font=f)
            d.text((110-w/2,y-13),label,font=f,fill=_alpha(WHITE,a))
            font, lines, lh = _fit(d,points[i],_find_font(True),455,105,24,18,1.1)
            yy=y-24
            for line in lines:
                d.text((172,yy),line,font=font,fill=_alpha(INK,a)); yy+=lh


def _draw_myth_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, reveal=False) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    body = scene.get("body", "")
    is_myth = bool(re.match(r"\s*(?:міф|твердження)\s*:", body, re.I))
    label = "МІФ / ТВЕРДЖЕННЯ" if is_myth else "ПРАВДА / ПРАВОВИЙ ФАКТ"
    if reveal:
        p = _ease_out((elapsed-.18)/.5)
        d.rounded_rectangle((70,470,650,900),radius=24,fill=_alpha(PURPLE_LIGHT,p),outline=_alpha(PURPLE,p),width=2)
        d.text((102,510),label,font=_font(_find_font(True),20),fill=_alpha(PURPLE_DARK,p))
        font,lines,lh=_fit(d,_visual_excerpt(body, 6),_find_font(True),480,220,31,21,1.12)
        y=605
        for line in lines:
            d.text((102,y),line,font=font,fill=_alpha(INK,p)); y+=lh
        d.line((102,830,610,830),fill=_alpha(PURPLE,.75),width=4)
    else:
        d.rounded_rectangle((70,470,650,900),radius=24,fill=(255,255,255,255),outline=(211,207,220,255),width=2)
        d.rounded_rectangle((70,470,650,535),radius=20,fill=(*PURPLE_LIGHT,255))
        d.text((102,490),label,font=_font(_find_font(True),20),fill=PURPLE_DARK)
        font,lines,lh=_fit(d,_visual_excerpt(body, 6),_find_font(True),490,220,31,21,1.12)
        y=610
        for line in lines:
            d.text((102,y),line,font=font,fill=INK); y+=lh
        d.text((102,830),"МІФ  /  ПРАВДА",font=_font(_find_font(True),16),fill=MUTED)

def _draw_case_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, dossier=False) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    body = scene.get("body", "")
    if dossier:
        d.rounded_rectangle((82,455,638,900),radius=18,fill=(255,255,255,255),outline=(205,201,215,255),width=2)
        d.rounded_rectangle((105,480,615,545),radius=10,fill=(*PURPLE_LIGHT,255))
        d.text((128,500),"СУДОВИЙ МАТЕРІАЛ",font=_font(_find_font(True),19),fill=PURPLE_DARK)
        d.text((110,585),"ФАКТИ СПРАВИ",font=_font(_find_font(True),17),fill=MUTED)
        font, lines, lh = _fit(d,_visual_excerpt(body, 7),_find_font(True),470,180,28,19,1.12)
        y=625
        for line in lines:
            d.text((110,y),line,font=font,fill=INK); y+=lh
        d.line((110,830,610,830),fill=(*PURPLE,255),width=4)
    else:
        d.line((115,500,115,870),fill=(191,179,222,255),width=6)
        entries=[("ОБСТАВИНИ",body), ("ПРАВОВЕ ПИТАННЯ",_short_heading(body)), ("ОЦІНКА",_citation(body) or "СУДОВИЙ КОНТЕКСТ")]
        for i,(label,value) in enumerate(entries):
            y=505+i*165; a=_ease_out((elapsed-.18-i*.18)/.45)
            d.ellipse((92,y-18,138,y+28),fill=_alpha(PURPLE,a))
            d.text((170,y-4),label,font=_font(_find_font(True),16),fill=_alpha(PURPLE_DARK,a))
            f, lines, lh=_fit(d,_visual_excerpt(value, 5),_find_font(True),445,88,22,17,1.1)
            yy=y+30
            for line in lines:
                d.text((170,yy),line,font=f,fill=_alpha(INK,a)); yy+=lh


def _draw_checklist_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, grid=False) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    points = _visual_points(scene.get("body", "")) or [scene.get("body", "")]
    points = (points + _visual_points(scene.get("body", ""))[len(points):])[:3]
    if grid:
        coords=[(66,470),(370,470),(66,680)]
        for i,(x,y) in enumerate(coords):
            a=_ease_out((elapsed-.12-i*.18)/.4)
            d.rounded_rectangle((x,y,x+284,y+190),radius=18,fill=_alpha(WHITE,a),outline=_alpha((207,203,216),a),width=2)
            d.rounded_rectangle((x+22,y+24,x+62,y+64),radius=10,fill=_alpha(PURPLE_LIGHT,a),outline=_alpha(PURPLE,a),width=2)
            f=_font(_find_font(True),22); d.text((x+33,y+31),"✓",font=f,fill=_alpha(PURPLE_DARK,a))
            font,lines,lh=_fit(d,points[i],_find_font(True),238,95,21,16,1.1)
            yy=y+88
            for line in lines:
                d.text((x+22,yy),line,font=font,fill=_alpha(INK,a)); yy+=lh
    else:
        for i,y in enumerate((485,625,765)):
            a=_ease_out((elapsed-.15-i*.18)/.4)
            d.rounded_rectangle((80,y,640,y+100),radius=16,fill=_alpha(WHITE,a),outline=_alpha((207,203,216),a),width=2)
            d.rounded_rectangle((103,y+28,145,y+70),radius=9,fill=_alpha(PURPLE_LIGHT,a),outline=_alpha(PURPLE,a),width=2)
            f=_font(_find_font(True),24); d.text((113,y+32),"✓",font=f,fill=_alpha(PURPLE_DARK,a))
            font,lines,lh=_fit(d,points[i],_find_font(True),455,75,23,17,1.1)
            yy=y+22
            for line in lines:
                d.text((168,yy),line,font=font,fill=_alpha(INK,a)); yy+=lh


def _comparison_parts(text: str) -> Tuple[str,str]:
    patterns = (r"\bбуло\s*[:—-]?\s*(.*?)\s+стало\s*[:—-]?\s*(.*)$",
                r"\bдо\s*[:—-]?\s*(.*?)\s+після\s*[:—-]?\s*(.*)$")
    for pattern in patterns:
        m=re.search(pattern,text,re.I)
        if m and m.group(1).strip() and m.group(2).strip():
            return m.group(1).strip(),m.group(2).strip()
    points=_key_points(text)
    if len(points)>=2:return points[0],points[1]
    return text[:max(1,len(text)//2)],text[max(1,len(text)//2):]


def _draw_comparison_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, scale=False) -> None:
    d=ImageDraw.Draw(canvas,"RGBA"); left,right=_comparison_parts(scene.get("body",""))
    if scale:
        d.line((360,510,360,860),fill=(190,179,220,255),width=5)
        d.line((185,560,535,560),fill=(*PURPLE,255),width=5)
        d.ellipse((340,840,380,880),fill=(*PURPLE,255))
        _draw_visual_card(d,(65,600,337,820),"ВАРІАНТ А",left,elapsed,accent=False,body_size=20)
        _draw_visual_card(d,(383,600,655,820),"ВАРІАНТ Б",right,elapsed-.1,accent=True,body_size=20)
    else:
        _draw_visual_card(d,(60,485,350,870),"ВАРІАНТ А",left,elapsed,accent=False,body_size=22)
        _draw_visual_card(d,(370,485,660,870),"ВАРІАНТ Б",right,elapsed-.15,accent=True,body_size=22)
        f=_font(_find_font(True),24); d.text((342,650),"≠",font=f,fill=PURPLE_DARK)


def _draw_qa_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, answer=False) -> None:
    d=ImageDraw.Draw(canvas,"RGBA"); body=scene.get("body","")
    if answer:
        p=_spring((elapsed-.18)/.65)
        d.ellipse((245,455,475,685),outline=_alpha(PURPLE,.55),width=8)
        f=_font(_find_font(True),130); d.text((300,465),"?",font=f,fill=_alpha(PURPLE,p))
        _draw_visual_card(d,(68,700,652,885),"ВІДПОВІДЬ",body,elapsed-.25,accent=True,body_size=22)
    else:
        d.rounded_rectangle((70,470,650,900),radius=28,fill=(255,255,255,255),outline=(208,204,218,255),width=2)
        f=_font(_find_font(True),150); d.text((93,480),"?",font=f,fill=(*PURPLE,255))
        font,lines,lh=_fit(d,_visual_excerpt(body, 6),_find_font(True),480,190,31,21,1.1)
        y=675
        for line in lines:
            d.text((110,y),line,font=font,fill=INK); y+=lh


def _draw_risk_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, path=False) -> None:
    d=ImageDraw.Draw(canvas,"RGBA"); body=scene.get("body","")
    p=_ease_out((elapsed-.12)/.45)
    if path:
        _draw_visual_card(d,(68,475,652,650),"РИЗИК",body,elapsed,accent=False,body_size=23)
        d.line((110,730,610,730),fill=(213,207,220,255),width=5)
        for i,x in enumerate((120,360,600)):
            a=_ease_out((elapsed-.65-i*.18)/.4)
            d.ellipse((x-18,712,x+18,748),fill=_alpha(PURPLE,a))
        labels=["ВИЯВИТИ","ОЦІНИТИ","ДІЯТИ"]
        for x,label in zip((120,360,600),labels):
            f=_font(_find_font(True),14); w=d.textlength(label,font=f); d.text((x-w/2,775),label,font=f,fill=MUTED)
    else:
        # Warning geometry, with the actual source sentence as the main information.
        triangle=[(360,460),(650,870),(70,870)]
        d.polygon(triangle,fill=_alpha(PURPLE_LIGHT,p),outline=_alpha(PURPLE,p))
        f=_font(_find_font(True),100); d.text((315,560),"!",font=f,fill=_alpha(PURPLE_DARK,p))
        _draw_visual_card(d,(98,745,622,900),"ЗВЕРНІТЬ УВАГУ",body,elapsed-.2,accent=True,body_size=19)


def _draw_content(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    tag_font = _font(_find_font(True), 18)
    a = _ease_out((elapsed - .06) / .30)
    label = scene.get("label", "ПРАВОВЕ ПОЯСНЕННЯ")
    d.text((48, 138), label, font=tag_font, fill=_alpha(PURPLE_DARK, a))
    d.rounded_rectangle((48, 174, 210, 181), radius=4, fill=_alpha(PURPLE, a))
    title = _short_heading(scene.get("body", ""))
    _draw_word_reveal(d, title, _find_font(True), (48, 218), 620, 170, 54,
                      elapsed - .15, INK, line_delay=.12)
    layout = scene.get("layout", "concept_map")
    if layout == "news_bulletin": _draw_news_visual(canvas, scene, elapsed, False)
    elif layout == "news_timeline": _draw_news_visual(canvas, scene, elapsed, True)
    elif layout == "concept_map": _draw_explainer_visual(canvas, scene, elapsed, False)
    elif layout == "editorial_layers": _draw_explainer_visual(canvas, scene, elapsed, True)
    elif layout == "steps_rail": _draw_howto_visual(canvas, scene, elapsed, False)
    elif layout == "steps_cards": _draw_howto_visual(canvas, scene, elapsed, True)
    elif layout == "myth_split": _draw_myth_visual(canvas, scene, elapsed, False)
    elif layout == "myth_reveal": _draw_myth_visual(canvas, scene, elapsed, True)
    elif layout == "case_timeline": _draw_case_visual(canvas, scene, elapsed, False)
    elif layout == "case_dossier": _draw_case_visual(canvas, scene, elapsed, True)
    elif layout == "checklist_grid": _draw_checklist_visual(canvas, scene, elapsed, True)
    elif layout == "checklist_stack": _draw_checklist_visual(canvas, scene, elapsed, False)
    elif layout == "comparison_split": _draw_comparison_visual(canvas, scene, elapsed, False)
    elif layout == "comparison_scale": _draw_comparison_visual(canvas, scene, elapsed, True)
    elif layout == "question_card": _draw_qa_visual(canvas, scene, elapsed, False)
    elif layout == "answer_focus": _draw_qa_visual(canvas, scene, elapsed, True)
    elif layout == "risk_alert": _draw_risk_visual(canvas, scene, elapsed, False)
    elif layout == "risk_path": _draw_risk_visual(canvas, scene, elapsed, True)
    else: _draw_explainer_visual(canvas, scene, elapsed, True)
    # The complete audience-facing copy remains visible in the lower caption area.
    box_top = 960
    slide = int((1 - _ease_out((elapsed - .50) / .42)) * 64)
    d.rounded_rectangle((42, box_top + slide + 8, WIDTH-42, 1198 + slide + 8),
                        radius=17, fill=(18, 14, 35, 20))
    d.rounded_rectangle((42, box_top + slide, WIDTH-42, 1198 + slide),
                        radius=17, fill=(255,255,255,250), outline=(150,145,164,255), width=2)
    d.rounded_rectangle((42, box_top + slide, 52, 1198 + slide), radius=5, fill=(*PURPLE,255))
    body = scene.get("body", "")
    body_font, body_lines, line_h = _fit(d, body, _find_font(True), 590, 190, 29, 21, 1.12)
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
    with tempfile.TemporaryDirectory(prefix="sentinel-motion-v4-") as td:
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
        "engine":"SENTINEL LAW Motion Engine V4",
        "video_type":_infer_video_type(job),
        "content_id":content_id,
        "scene_count":len(scenes),
        "duration_seconds":round(info.get("duration",duration),2),
        "file_bytes":info.get("size"),
        "resolution":f"{WIDTH}x{HEIGHT}",
        "fps":FPS,"audio":"AAC stereo",
        "text_policy":"all audience-facing script text retained",
        "visual_style":"type-specific editorial layouts / kinetic legal graphics"
    },ensure_ascii=False))
    return output


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--job",required=True)
    parser.add_argument("--output",default="sentinel-law-v4.mp4")
    args=parser.parse_args()
    payload=json.loads(Path(args.job).read_text(encoding="utf-8"))
    build_video(payload,args.output)
