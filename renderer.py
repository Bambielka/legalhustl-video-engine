"""SENTINEL LAW Motion Engine V4.2.

Type-driven editorial motion graphics for Ukrainian legal Instagram Reels and Stories.
The public build_video(job, output_path) contract is kept compatible with worker.py.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import wave
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, FPS = 720, 1280, 30
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

LAYOUTS = {
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


def _clean(value: Any, limit: int = MAX_SCRIPT_CHARS) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
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


_TECHNICAL_HEADING = re.compile(
    r"^(?:ВІЗУАЛЬНІ\s+ПРИМІТКИ|ВІЗУАЛ\w*|ВІДЕОРЯД|КАМЕРА|АНІМАЦІ\w*|ЕФЕКТ\w*|ПЕРЕХІД\w*|"
    r"МУЗИКА|ЗВУК|ТАЙМІНГ|ТРИВАЛІСТЬ|ГРАФІКА|КОМПОЗИЦІ\w*|ІНСТРУКЦІ\w*|"
    r"РЕЖИСЕРСЬК\w*|ВКАЗІВКИ ДЛЯ МОНТАЖУ|ТЕХНІЧНЕ ЗАВДАННЯ|ОПИС СЦЕНИ|ПІДКАЗКА ДЛЯ МОНТАЖУ|"
    r"VISUAL NOTES|VISUAL|VIDEO PROMPT|SHOT LIST|EDITING NOTES)(?:\s*\([^)]*\))?\s*:", re.I
)
_AUDIENCE_LABELS = (
    "ТЕКСТ НА ЕКРАНІ", "ТЕКСТ ДЛЯ ОЗВУЧКИ", "ТЕКСТ ДИКТОРА", "ГОЛОС ЗА КАДРОМ",
    "ПОВІДОМЛЕННЯ", "СУБТИТРИ", "НА ЕКРАНІ", "ОЗВУЧКА", "РЕПЛІКА", "ДИКТОР",
    "ЩО ЗМІНИЛОСЯ", "ЩО ВАЖЛИВО", "ЗАПИТАННЯ", "ВІДПОВІДЬ", "ВИСНОВОК",
    "АЛГОРИТМ", "ПРАВИЛО", "ПОМИЛКА", "ГОЛОВНЕ", "ФАКТ", "УМОВА", "МІФ", "ПРАВДА",
    "ДІЯ", "РИЗИК", "ТЕКСТ",
)
_PRODUCTION_START = re.compile(
    r"^(?:ВИКОРИСТОВУВАТИ|ВИКОРИСТОВУЙ|ВИКОРИСТАЙ|ПОКАЗАТИ|ПОКАЖИ|НЕ ПОКАЗУВАТИ|"
    r"НЕ ПОКАЗУЙ|ЗРОБИТИ|ЗРОБИ|ДОДАТИ|ДОДАЙ|АНІМУВАТИ|АНІМУЙ|ЗМОНТУВАТИ|"
    r"РОЗМІСТИТИ|ПОСТАВИТИ|ЗАСТОСУВАТИ|USE|SHOW|DO NOT SHOW|AVOID|ADD|ANIMATE)\b",
    re.I,
)


def _is_direction(line: str) -> bool:
    value = line.strip().lstrip("-•* ").strip()
    if not value:
        return True
    if re.match(r"^(?:сцена|scene|кадр|shot)\s*\d+\b", value, re.I):
        return True
    if re.match(r"^\[?(?:0?\d{1,2}:\d{2}|\d{1,2}\s*[–-]\s*\d{1,2}\s*(?:с|сек|секунд))\]?\s*:?$", value, re.I):
        return True
    return bool(_TECHNICAL_HEADING.match(value) or _PRODUCTION_START.match(value))


def _is_production_instruction(text: str) -> bool:
    value = re.sub(r"\s+", " ", text or "").strip()
    return not value or _is_direction(value)


def _looks_like_visual_continuation(line: str) -> bool:
    value = re.sub(r"\s+", " ", line or "").strip()
    patterns = (
        r"^(?:білий|світлий|темний|однотонний|нейтральний)\s+фон\b",
        r"^(?:плавн\w*\s+)?(?:перехід|анімаці)\w*\b",
        r"^(?:поява|з'являється|з\u2019являється|зміна кадру|рух камери)\b",
        r"^(?:на фоні|у кадрі|камера|ліворуч|праворуч|зліва|справа)\b",
        r"^(?:текстов\w*\s+картк|картк\w*\s+(?:появ|виїжд|змін))",
        r"^(?:фіолетов\w*\s+акцент|шрифт|композиція|кольорова схема|кольори)\b",
        r"^(?:без\s+(?:людей|облич|реальних осіб|стокових людей)|не показувати)\b",
    )
    return any(re.search(pattern, value, re.I) for pattern in patterns)


_SCENE_START = re.compile(
    r"^\s*(?:СЦЕНА|SCENE)\s*(\d+)\b(?:\s*\(([^)]*)\))?\s*:\s*(.*)$", re.I
)
_SUBTITLE_MARKER = re.compile(
    r"\b(?:СУБТИТРИ?|ТЕКСТ\s+ДИКТОРА|ТЕКСТ\s+ДЛЯ\s+ОЗВУЧКИ|ГОЛОС\s+ЗА\s+КАДРОМ|"
    r"РЕПЛІКА|ОЗВУЧКА|ТЕКСТ\s+НА\s+ЕКРАНІ)\s*:\s*(.+)", re.I
)
_QUOTED_TEXT = re.compile(r"[«“\"]([^»”\"]+)[»”\"]")


def _duration_from_scene_range(value: str) -> float | None:
    """Read scene duration from forms such as 0–4 с or 00:04–00:09."""
    text = str(value or "").strip()
    clock = re.search(r"(\d{1,2}):(\d{2})\s*[–—-]\s*(\d{1,2}):(\d{2})", text)
    if clock:
        start = int(clock.group(1)) * 60 + int(clock.group(2))
        end = int(clock.group(3)) * 60 + int(clock.group(4))
        return float(end - start) if end > start else None
    seconds = re.search(
        r"(\d+(?:[.,]\d+)?)\s*[–—-]\s*(\d+(?:[.,]\d+)?)\s*(?:с|сек(?:унд[а-я]*)?)?\b",
        text, re.I,
    )
    if seconds:
        start = float(seconds.group(1).replace(",", "."))
        end = float(seconds.group(2).replace(",", "."))
        return end - start if end > start else None
    return None


def _unquote_copy(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    match = _QUOTED_TEXT.search(value)
    if match:
        return match.group(1).strip()
    return value.strip(" \t\"'«»“”")


def _parse_storyboard_scenes(script: str) -> List[Dict[str, Any]]:
    """Parse numbered storyboard scenes while retaining the spoken/subtitle copy.

    Example accepted line:
      Сцена 3 (9–14 с): Картка з цифрами «до 1 млрд грн» і «5,5%».
      Субтитр: «Анонсовано кредитування з компенсацією ставки 5,5%».
    """
    source = _clean(script)
    if not source:
        return []

    blocks: List[Dict[str, Any]] = []
    current: Dict[str, Any] | None = None
    for raw in source.split("\n"):
        line = raw.strip().lstrip("-•* ").strip()
        if not line:
            continue
        start = _SCENE_START.match(line)
        if start:
            if current:
                blocks.append(current)
            current = {
                "number": int(start.group(1)),
                "range": start.group(2) or "",
                "lines": [start.group(3).strip()] if start.group(3).strip() else [],
            }
            # Timecode may be written immediately after the colon instead of in parentheses.
            if not current["range"]:
                current["range"] = start.group(3).strip()
            continue
        if current is None:
            continue
        # Global technical notes after the storyboard are not scene content.
        if _TECHNICAL_HEADING.match(line) or re.match(
            r"^(?:РЕЖИСЕРСЬКІ ВКАЗІВКИ|ТЕХНІЧНЕ ЗАВДАННЯ)\b", line, re.I
        ):
            blocks.append(current)
            current = None
            continue
        current["lines"].append(line)
    if current:
        blocks.append(current)

    parsed: List[Dict[str, Any]] = []
    for block in blocks:
        block_lines = [line for line in block["lines"] if line]
        raw_text = " ".join(block_lines)
        subtitle_match = _SUBTITLE_MARKER.search(raw_text)

        if subtitle_match:
            body = _unquote_copy(subtitle_match.group(1))
            visual_description = raw_text[:subtitle_match.start()]
        else:
            # Fallback to any explicitly audience-facing label in a multiline scene.
            body = ""
            visual_description = raw_text
            for label in _AUDIENCE_LABELS:
                match = re.search(r"\b" + re.escape(label) + r"\s*:\s*(.+)", raw_text, re.I)
                if match:
                    body = _unquote_copy(match.group(1))
                    visual_description = raw_text[:match.start()]
                    break

        quoted_visuals = [_unquote_copy(item) for item in _QUOTED_TEXT.findall(visual_description)]
        visual_title = " · ".join(item for item in quoted_visuals if item)
        if not body and visual_title:
            body = visual_title
        if not body:
            continue
        scene_range = str(block.get("range") or "")
        duration = _duration_from_scene_range(scene_range)
        parsed.append({
            "number": str(block["number"]),
            "body": body,
            "visual_title": visual_title,
            "duration_seconds": duration,
        })

    # Do not silently treat arbitrary mentions of "Сцена 1" as a storyboard.
    return parsed if len(parsed) >= 2 else []


def _extract_script_text(script: str) -> str:
    """Remove production directions while retaining all explicitly labelled audience copy."""
    storyboard = _parse_storyboard_scenes(script)
    if storyboard:
        return " ".join(scene["body"] for scene in storyboard).strip()
    source = _clean(script)
    if not source:
        return ""
    out: List[str] = []
    skip_director_block = False
    for raw in source.split("\n"):
        stripped = raw.strip()
        line = stripped.lstrip("-•* ").strip()
        if not line:
            continue

        # Explicit audience-facing labels always end a preceding director-note block.
        step_match = re.match(r"^(?:КРОК|ЕТАП)\s*\d+\s*:\s*(.*)$", line, re.I)
        if step_match:
            skip_director_block = False
            value = step_match.group(1).strip()
            if value and not _is_production_instruction(value):
                out.append(value)
            continue

        matched_label = None
        matched_value = ""
        for label in _AUDIENCE_LABELS:
            match = re.match(r"^" + re.escape(label) + r"\s*:\s*(.*)$", line, re.I)
            if match:
                matched_label, matched_value = label, match.group(1).strip()
                break
        if matched_label:
            skip_director_block = False
            if matched_value and not _is_production_instruction(matched_value):
                if matched_label in ("МІФ", "ПРАВДА"):
                    out.append(matched_label + ": " + matched_value)
                else:
                    out.append(matched_value)
            continue

        if _TECHNICAL_HEADING.match(line) or re.match(r"^(?:режисерські вказівки|технічне завдання)\b", line, re.I):
            skip_director_block = True
            continue
        if _is_direction(line):
            skip_director_block = True
            continue
        if skip_director_block:
            # Lists and instruction-style continuation lines belong to the technical block.
            if (stripped.startswith(("-", "•", "*")) or re.match(r"^\d+[.)]\s*", stripped)
                    or _is_production_instruction(line) or _looks_like_visual_continuation(line)):
                continue
            skip_director_block = False

        if re.match(r"^(?:ведучий|ведуча|голос за кадром|завершення|вступ)\s*:\s*$", line, re.I):
            continue
        if _is_production_instruction(line):
            continue
        # Prevent common technical fragments pasted into a prose line from leaking on screen.
        line = re.sub(r"(?i)\bвізуальні? примітки\s*:?\s*", "", line).strip()
        line = re.sub(r"(?i)\bвикористовувати текстові картки\b[^.]*\.?", "", line).strip()
        if line:
            out.append(line)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def _sentences(text: str) -> List[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?;])\s+|\n+", text) if part.strip()]


def _split_script(text: str, max_words: int = 19, max_chars: int = 145) -> List[str]:
    """Split text into readable scene captions without dropping or reordering words."""
    if not text.strip():
        return []
    chunks: List[str] = []
    current: List[str] = []
    for sentence in _sentences(text):
        for word in sentence.split():
            candidate = " ".join(current + [word])
            if current and (len(current) >= max_words or len(candidate) > max_chars):
                chunks.append(" ".join(current))
                current = [word]
            else:
                current.append(word)
    if current:
        chunks.append(" ".join(current))
    # Merge a very short tail only when the resulting caption remains readable.
    if len(chunks) > 1 and len(chunks[-1].split()) <= 4:
        merged = chunks[-2] + " " + chunks[-1]
        if len(merged) <= max_chars + 65 and len(merged.split()) <= max_words + 6:
            chunks[-2:] = [merged]
    return chunks


@lru_cache(maxsize=256)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=size)


def _find_font(bold: bool = False) -> str:
    candidates = ([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/lato/Lato-Heavy.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ] if bold else [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ])
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("Не знайдено шрифт із підтримкою української мови (DejaVu Sans / Lato).")


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
         max_height: int, preferred: int, minimum: int = 18,
         line_factor: float = 1.12) -> Tuple[ImageFont.FreeTypeFont, List[str], int]:
    content = str(text or "").strip() or " "
    minimum = max(10, int(minimum))
    for size in range(max(int(preferred), minimum), minimum - 1, -2):
        font = _font(path, size)
        if any(draw.textlength(word, font=font) > width for word in content.split()):
            continue
        lines = _wrap(draw, content, font, width)
        line_height = max(1, int(size * line_factor))
        if len(lines) * line_height <= max_height:
            return font, lines, line_height
    for size in range(minimum, 9, -1):
        font = _font(path, size)
        if all(draw.textlength(word, font=font) <= width for word in content.split()):
            lines = _wrap(draw, content, font, width)
            return font, lines, max(1, int(size * line_factor))
    raise ValueError("Не вдалося розмістити слово в межах текстового блоку.")


def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _spring(t: float, damping: float = 8.0, frequency: float = 11.0) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - math.exp(-damping * t) * math.cos(frequency * t)


def _alpha(color: Tuple[int, int, int], amount: float) -> Tuple[int, int, int, int]:
    return (*color, max(0, min(255, int(amount * 255))))


def _citation(text: str) -> str:
    patterns = (
        r"ч(?:астина|\.)\s*\d{1,2}\s*ст(?:атті|аття|\.?)\s*\d{1,3}",
        r"ст(?:атті|аття|\.?)\s*\d{1,3}(?:[-–]\d{1,3})?",
        r"п(?:ункт|\.)\s*\d+(?:\.\d+)*",
        r"абзац(?:у)?\s+\d+",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(0)
    return ""


def _numbers(text: str) -> List[str]:
    matches = re.findall(
        r"(?<!\w)(?:\d{1,3}(?:[ .]\d{3})+|\d+(?:[,.]\d+)?)(?:\s?(?:%|грн|днів|дні|день|годин|години|місяців|років|€|\$))?",
        text, re.I
    )
    return matches[:3]


def _scene_label(text: str, index: int, total: int) -> str:
    value = text.lower()
    if _citation(text):
        return "НОРМАТИВНА ОСНОВА"
    if re.search(r"\b(строк|термін|днів|день|годин|місяц|років)\b", value):
        return "СТРОКИ ТА ДАТИ"
    if re.search(r"\b(рапорт|заява|звернен|документ|наказ|копі|витяг)\b", value):
        return "ДОКУМЕНТИ ТА ЗВЕРНЕННЯ"
    if re.search(r"\b(оскарж|скарг|апеляц|суд|рішенн)\b", value):
        return "ПРАВОВИЙ ЗАХИСТ"
    if re.search(r"\b(потрібно|необхідно|подати|звернутися|надіслати|отримати|зберегти)\b", value):
        return "ЩО РОБИТИ"
    return "КЛЮЧОВИЙ ФАКТ" if index == 1 else "ПРАВОВЕ ПОЯСНЕННЯ"


def _infer_video_type(job: Dict[str, Any]) -> str:
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
    text = " ".join(str(job.get(key) or "") for key in ("topic", "headline", "script", "caption")).lower()
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
    """Choose one stable visual variant per video; different IDs can use the other variant."""
    options = LAYOUTS.get(video_type, LAYOUTS["explainer"])
    digest = hashlib.sha256(str(content_id or "SENTINEL-LAW").encode("utf-8")).digest()
    variant = digest[0] % len(options)
    return options[variant]


def _make_scenes(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    fmt = str(job.get("format") or "Reels").strip().lower()
    headline = _clean(job.get("headline") or job.get("topic") or "Правовий розбір", 500)
    source_script = str(
        job.get("script") or job.get("caption") or job.get("source_text")
        or job.get("full_text") or job.get("article_text") or job.get("body") or job.get("text") or headline
    )
    storyboard = _parse_storyboard_scenes(source_script)
    script = (" ".join(scene["body"] for scene in storyboard) if storyboard
              else (_extract_script_text(source_script) or headline))
    cta = _clean(job.get("cta") or "Збережіть матеріал, щоб повернутися до нього", 400)
    content_id = str(job.get("content_id") or job.get("job_id") or headline)
    video_type = _infer_video_type({**job, "script": script})
    label = VIDEO_TYPE_LABELS.get(video_type, VIDEO_TYPE_LABELS["explainer"])
    layout = _layout_for(video_type, 0, content_id)

    if fmt == "story":
        return [{
            "kind": "story", "label": "SENTINEL LAW",
            "title": _clean(job.get("story_title") or headline, 400),
            "body": script, "video_type": video_type, "layout": layout,
        }]

    # A storyboard is already a scene-by-scene editorial plan. Keep its scene count,
    # audience copy, on-screen labels and supplied timing instead of flattening it.
    if storyboard:
        scenes: List[Dict[str, Any]] = []
        for index, item in enumerate(storyboard):
            duration = item.get("duration_seconds")
            common: Dict[str, Any] = {
                "label": label, "video_type": video_type, "layout": layout,
                "sequence": str(index + 1),
            }
            if duration is not None:
                common["duration_seconds"] = str(duration)
            if index == 0:
                scenes.append({
                    **common, "kind": "cover",
                    "title": item.get("visual_title") or item["body"],
                    "body": "", "headline": headline,
                })
            else:
                scenes.append({
                    **common, "kind": "content", "title": "",
                    "visual_title": item.get("visual_title", ""),
                    "headline": headline, "body": item["body"],
                })
        return scenes

    scenes: List[Dict[str, str]] = [{
        "kind": "cover", "label": label, "title": headline, "body": "",
        "video_type": video_type, "layout": layout,
    }]
    chunks = _split_script(script)
    for index, chunk in enumerate(chunks, start=1):
        scenes.append({
            "kind": "content", "label": label, "title": "", "headline": headline, "body": chunk,
            "video_type": video_type, "layout": layout, "sequence": str(index),
        })
    cta_words = cta.split()
    closing_title = " ".join(cta_words[:2]) if len(cta_words) > 2 else cta
    scenes.append({
        "kind": "closing", "label": "SENTINEL LAW", "title": closing_title,
        "body": cta, "video_type": video_type, "layout": layout,
    })
    return scenes


def _scene_durations(scenes: Sequence[Dict[str, str]], fmt: str) -> List[float]:
    if fmt.lower() == "story":
        return [12.0]
    durations: List[float] = []
    for scene in scenes:
        requested = scene.get("duration_seconds")
        if requested not in (None, ""):
            try:
                requested_value = float(requested)
            except (TypeError, ValueError):
                requested_value = 0.0
            if requested_value > 0:
                durations.append(requested_value)
                continue
        if scene["kind"] == "cover":
            durations.append(3.5)
        elif scene["kind"] == "closing":
            durations.append(3.2)
        else:
            words = len(scene["body"].split())
            durations.append(max(4.8, min(8.2, 2.8 + words / 3.4)))
    return durations


def _draw_header(canvas: Image.Image, scene_index: int, total: int, progress: float) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    regular, bold = _font(_find_font(False), 15), _font(_find_font(True), 17)
    draw.text((42, 31), "SENTINEL LAW", font=bold, fill=INK)
    draw.text((42, 54), "ПРАВО  /  АНАЛІТИКА  /  ЗАХИСТ", font=regular, fill=MUTED)
    right = f"{scene_index:02d}  /  {total:02d}"
    draw.text((WIDTH - 42 - draw.textlength(right, font=regular), 38), right, font=regular, fill=MUTED)
    draw.line((42, 84, WIDTH - 42, 84), fill=(*LINE, 255), width=2)
    draw.rounded_rectangle((42, 84, WIDTH - 42, 88), radius=2, fill=(238, 236, 243, 255))
    draw.rounded_rectangle((42, 84, 42 + int((WIDTH - 84) * max(0, min(1, progress))), 88),
                           radius=2, fill=(*PURPLE, 255))


def _draw_background(canvas: Image.Image, frame: int, scene_index: int) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    phase = frame / FPS
    cx = 620 + int(28 * math.sin(phase * 0.62 + scene_index))
    cy = 930 + int(36 * math.cos(phase * 0.48 + scene_index * 0.4))
    draw.ellipse((cx - 230, cy - 230, cx + 230, cy + 230), fill=(132, 82, 247, 14))
    draw.ellipse((cx - 165, cy - 165, cx + 165, cy + 165), outline=(132, 82, 247, 30), width=2)
    draw.ellipse((cx - 105, cy - 105, cx + 105, cy + 105), outline=(132, 82, 247, 22), width=2)
    for y in range(140, HEIGHT, 70):
        draw.line((0, y, WIDTH, y), fill=(242, 241, 246, 255), width=1)


def _draw_word_reveal(draw: ImageDraw.ImageDraw, text: str, font_path: str,
                      xy: Tuple[int, int], max_width: int, max_height: int,
                      preferred: int, elapsed: float, color=INK, line_delay: float = 0.10) -> int:
    font, lines, line_height = _fit(draw, text, font_path, max_width, max_height, preferred, 22, 1.03)
    y = xy[1]
    for index, line in enumerate(lines):
        alpha = _ease_out((elapsed - index * line_delay) / 0.34)
        if alpha > 0:
            draw.text((xy[0], y + int((1 - alpha) * 28)), line, font=font, fill=_alpha(color, alpha))
        y += line_height
    return y


def _visual_excerpt(text: str, max_words: int = 6) -> str:
    words = re.sub(r"\s+", " ", text or "").strip().split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip(" ,;:") + "…"


def _visual_points(text: str, limit: int = 3) -> List[str]:
    """Return up to `limit` sentence-level source excerpts without inventing subdivisions."""
    source = re.sub(r"\s+", " ", text or "").strip()
    if not source or limit <= 0:
        return []
    candidates = re.split(r"(?<=[.!?;])\s+", source)
    points: List[str] = []
    for candidate in candidates:
        value = candidate.strip(" .,:;—–-")
        if value and value.lower() not in [item.lower() for item in points]:
            points.append(_visual_excerpt(value, 8))
        if len(points) >= limit:
            break
    return points



def _draw_visual_card(draw: ImageDraw.ImageDraw, box: Tuple[int, int, int, int],
                      label: str, body: str, elapsed: float, accent: bool = False,
                      body_size: int = 22) -> None:
    x1, y1, x2, y2 = box
    alpha = _ease_out((elapsed - 0.10) / 0.42)
    slide = int((1 - alpha) * 30)
    fill = PURPLE_LIGHT if accent else WHITE
    draw.rounded_rectangle((x1 + 5, y1 + 8 + slide, x2 + 5, y2 + 8 + slide), radius=18,
                           fill=(18, 14, 35, int(alpha * 22)))
    draw.rounded_rectangle((x1, y1 + slide, x2, y2 + slide), radius=18,
                           fill=_alpha(fill, alpha), outline=_alpha((207, 203, 216), alpha), width=2)
    label_font = _font(_find_font(True), 15)
    draw.text((x1 + 20, y1 + 17 + slide), label[:26], font=label_font,
              fill=_alpha(PURPLE_DARK if accent else MUTED, alpha))
    font, lines, line_height = _fit(draw, body, _find_font(True), x2 - x1 - 42,
                                    max(45, y2 - y1 - 62), body_size, 16, 1.08)
    y = y1 + 48 + slide
    for line in lines:
        draw.text((x1 + 20, y), line, font=font, fill=_alpha(INK, alpha))
        y += line_height


def _draw_explainer_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, layered: bool = False) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    points = _visual_points(scene.get("body", "")) or [scene.get("body", "")]
    if layered:
        boxes = [(70, 465, 650, 575), (105, 610, 615, 720), (70, 755, 650, 865)]
        labels = ["ТЕЗА", "ДЕТАЛЬ", "ФРАГМЕНТ"]
        for idx, point in enumerate(points):
            _draw_visual_card(draw, boxes[idx], labels[idx], point, elapsed - idx * 0.12,
                              accent=(idx == 1), body_size=20)
    else:
        count = len(points)
        xs = ([360] if count == 1 else [220, 500] if count == 2 else [155, 360, 565])
        draw.line((xs[0], 650, xs[-1], 650), fill=(202, 190, 227, 255), width=6)
        for idx, (x, point) in enumerate(zip(xs, points)):
            alpha = _ease_out((elapsed - 0.18 - idx * 0.18) / 0.45)
            radius = 36
            draw.ellipse((x-radius, 614-radius, x+radius, 614+radius),
                         fill=_alpha(PURPLE, alpha), outline=_alpha(PURPLE_DARK, alpha), width=2)
            number = f"{idx + 1:02d}"
            number_font = _font(_find_font(True), 23)
            draw.text((x - draw.textlength(number, font=number_font) / 2, 600), number,
                      font=number_font, fill=_alpha(WHITE, alpha))
            left, right = max(58, x - 100), min(662, x + 100)
            _draw_visual_card(draw, (left, 700, right, 865), "ЕЛЕМЕНТ", point,
                              elapsed - 0.22 - idx * 0.12, accent=(idx == 1), body_size=17)


def _draw_howto_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, cards: bool = False) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    points = _visual_points(scene.get("body", "")) or [scene.get("body", "")]
    count = len(points)
    ys = [int(500 + idx * (350 / max(1, count - 1))) for idx in range(count)]
    if cards:
        for idx, (point, y) in enumerate(zip(points, ys)):
            x = 70 + (idx % 2) * 18
            _draw_visual_card(draw, (x, y - 24, 650 - (idx % 2) * 18, y + 96),
                              f"КРОК 0{idx + 1}", point, elapsed - idx * 0.16,
                              accent=(idx % 2 == 0), body_size=19)
    else:
        if len(ys) > 1:
            draw.line((110, ys[0], 110, ys[-1]), fill=(195, 183, 221, 255), width=6)
        for idx, (point, y) in enumerate(zip(points, ys)):
            alpha = _ease_out((elapsed - 0.15 - idx * 0.20) / 0.45)
            draw.ellipse((80, y - 30, 140, y + 30), fill=_alpha(PURPLE, alpha),
                         outline=_alpha(PURPLE_DARK, alpha), width=2)
            number = f"{idx + 1:02d}"
            number_font = _font(_find_font(True), 20)
            draw.text((110 - draw.textlength(number, font=number_font) / 2, y - 13), number,
                      font=number_font, fill=_alpha(WHITE, alpha))
            font, lines, line_height = _fit(draw, point, _find_font(True), 455, 100, 23, 17, 1.08)
            for line_idx, line in enumerate(lines):
                draw.text((172, y - 22 + line_idx * line_height), line, font=font, fill=_alpha(INK, alpha))


def _draw_myth_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, reveal: bool = False) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    body = scene.get("body", "")
    is_myth = bool(re.match(r"\s*(?:міф|твердження)\s*:", body, re.I))
    is_truth = bool(re.match(r"\s*правда\s*:", body, re.I))
    label = "МІФ / ТВЕРДЖЕННЯ" if is_myth else (
        "ПРАВДА / ПРАВОВИЙ ФАКТ" if is_truth else "ПЕРЕВІРЯЄМО ТВЕРДЖЕННЯ"
    )
    if reveal:
        alpha = _ease_out((elapsed - 0.18) / 0.5)
        draw.rounded_rectangle((70, 470, 650, 900), radius=24, fill=_alpha(PURPLE_LIGHT, alpha),
                               outline=_alpha(PURPLE, alpha), width=2)
        draw.text((102, 510), label, font=_font(_find_font(True), 20), fill=_alpha(PURPLE_DARK, alpha))
        font, lines, line_height = _fit(draw, _visual_excerpt(body, 8), _find_font(True), 480, 220, 30, 20, 1.12)
        for idx, line in enumerate(lines):
            draw.text((102, 605 + idx * line_height), line, font=font, fill=_alpha(INK, alpha))
        draw.line((102, 830, 610, 830), fill=_alpha(PURPLE, 0.75), width=4)
    else:
        draw.rounded_rectangle((70, 470, 650, 900), radius=24, fill=WHITE,
                               outline=(211, 207, 220, 255), width=2)
        draw.rounded_rectangle((70, 470, 650, 535), radius=20, fill=PURPLE_LIGHT)
        draw.text((102, 490), label, font=_font(_find_font(True), 20), fill=PURPLE_DARK)
        font, lines, line_height = _fit(draw, _visual_excerpt(body, 8), _find_font(True), 490, 220, 30, 20, 1.12)
        for idx, line in enumerate(lines):
            draw.text((102, 610 + idx * line_height), line, font=font, fill=INK)
        draw.text((102, 830), "МІФ  /  ПРАВДА", font=_font(_find_font(True), 16), fill=MUTED)


def _draw_case_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, dossier: bool = False) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    body = scene.get("body", "")
    if dossier:
        draw.rounded_rectangle((82, 455, 638, 900), radius=18, fill=WHITE, outline=(205, 201, 215, 255), width=2)
        draw.rounded_rectangle((105, 480, 615, 545), radius=10, fill=PURPLE_LIGHT)
        draw.text((128, 500), "СУДОВИЙ МАТЕРІАЛ", font=_font(_find_font(True), 19), fill=PURPLE_DARK)
        draw.text((110, 585), "ФРАГМЕНТ СЦЕНАРІЮ", font=_font(_find_font(True), 16), fill=MUTED)
        font, lines, line_height = _fit(draw, _visual_excerpt(body, 14), _find_font(True), 470, 180, 27, 19, 1.12)
        for idx, line in enumerate(lines):
            draw.text((110, 625 + idx * line_height), line, font=font, fill=INK)
        draw.line((110, 830, 610, 830), fill=(*PURPLE, 255), width=4)
    else:
        draw.line((115, 500, 115, 870), fill=(191, 179, 222, 255), width=6)
        segments = _visual_points(body)
        if not segments:
            segments = [body]
        labels = ["ФРАГМЕНТ 1", "ФРАГМЕНТ 2", "ФРАГМЕНТ 3"]
        ys = [int(515 + idx * (330 / max(1, len(segments) - 1))) for idx in range(len(segments))]
        for idx, (value, y) in enumerate(zip(segments, ys)):
            alpha = _ease_out((elapsed - 0.18 - idx * 0.18) / 0.45)
            draw.ellipse((92, y - 18, 138, y + 28), fill=_alpha(PURPLE, alpha))
            draw.text((170, y - 4), labels[min(idx, 2)], font=_font(_find_font(True), 15), fill=_alpha(PURPLE_DARK, alpha))
            font, lines, line_height = _fit(draw, value, _find_font(True), 445, 82, 21, 16, 1.08)
            for line_idx, line in enumerate(lines):
                draw.text((170, y + 26 + line_idx * line_height), line, font=font, fill=_alpha(INK, alpha))


def _draw_checklist_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, grid: bool = False) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    points = _visual_points(scene.get("body", "")) or [scene.get("body", "")]
    count = len(points)
    if grid:
        positions = [(66, 485), (370, 485), (218, 700)] if count == 3 else (
            [(90, 525), (370, 525)] if count == 2 else [(140, 575)]
        )
        for idx, (point, (x, y)) in enumerate(zip(points, positions)):
            alpha = _ease_out((elapsed - 0.12 - idx * 0.18) / 0.4)
            draw.rounded_rectangle((x, y, x + 284, y + 178), radius=18,
                                   fill=_alpha(WHITE, alpha), outline=_alpha((207, 203, 216), alpha), width=2)
            draw.rounded_rectangle((x + 22, y + 22, x + 62, y + 62), radius=10,
                                   fill=_alpha(PURPLE_LIGHT, alpha), outline=_alpha(PURPLE, alpha), width=2)
            draw.text((x + 31, y + 27), "✓", font=_font(_find_font(True), 22), fill=_alpha(PURPLE_DARK, alpha))
            font, lines, line_height = _fit(draw, point, _find_font(True), 238, 94, 20, 16, 1.08)
            for line_idx, line in enumerate(lines):
                draw.text((x + 22, y + 82 + line_idx * line_height), line, font=font, fill=_alpha(INK, alpha))
    else:
        if count == 1:
            ys = [620]
        elif count == 2:
            ys = [535, 720]
        else:
            ys = [485, 635, 785]
        for idx, (point, y) in enumerate(zip(points, ys)):
            alpha = _ease_out((elapsed - 0.15 - idx * 0.18) / 0.4)
            draw.rounded_rectangle((80, y, 640, y + 108), radius=16,
                                   fill=_alpha(WHITE, alpha), outline=_alpha((207, 203, 216), alpha), width=2)
            draw.rounded_rectangle((103, y + 29, 145, y + 71), radius=9,
                                   fill=_alpha(PURPLE_LIGHT, alpha), outline=_alpha(PURPLE, alpha), width=2)
            draw.text((112, y + 33), "✓", font=_font(_find_font(True), 24), fill=_alpha(PURPLE_DARK, alpha))
            font, lines, line_height = _fit(draw, point, _find_font(True), 455, 78, 22, 16, 1.08)
            for line_idx, line in enumerate(lines):
                draw.text((168, y + 22 + line_idx * line_height), line, font=font, fill=_alpha(INK, alpha))


def _comparison_parts(text: str) -> Tuple[str, str]:
    patterns = (
        r"\bбуло\s*[:—-]?\s*(.*?)\s+стало\s*[:—-]?\s*(.*)$",
        r"\bдо\s*[:—-]?\s*(.*?)\s+після\s*[:—-]?\s*(.*)$",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            left, right = match.group(1).strip(" .,:;"), match.group(2).strip(" .,:;")
            if left and right:
                return left, right
    # Do not manufacture a second comparison side by splitting an ordinary sentence.
    parts = [part.strip(" .,:;") for part in re.split(
        r"(?<=[.!?;])\s+|\s*,\s*|\s+(?:але|натомість|водночас)\s+", text, flags=re.I
    ) if part.strip(" .,:;")]
    if len(parts) > 1:
        return _visual_excerpt(parts[0], 8), _visual_excerpt(parts[1], 8)
    return _visual_excerpt(text, 8), ""


def _draw_comparison_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, scale: bool = False) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    left, right = _comparison_parts(scene.get("body", ""))
    if scale:
        draw.line((360, 510, 360, 860), fill=(190, 179, 220, 255), width=5)
        draw.line((185, 560, 535, 560), fill=(*PURPLE, 255), width=5)
        draw.ellipse((340, 840, 380, 880), fill=(*PURPLE, 255))
        _draw_visual_card(draw, (65, 610, 337, 830), "ВАРІАНТ А", left, elapsed, body_size=19)
        if right:
            _draw_visual_card(draw, (383, 610, 655, 830), "ВАРІАНТ Б", right, elapsed - 0.1, accent=True, body_size=19)
    else:
        _draw_visual_card(draw, (60, 485, 350, 870), "ВАРІАНТ А", left, elapsed, body_size=21)
        if right:
            _draw_visual_card(draw, (370, 485, 660, 870), "ВАРІАНТ Б", right, elapsed - 0.15, accent=True, body_size=21)
            draw.text((342, 650), "≠", font=_font(_find_font(True), 24), fill=PURPLE_DARK)


def _draw_qa_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, answer: bool = False) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    body = scene.get("body", "")
    if answer:
        alpha = _spring((elapsed - 0.18) / 0.65)
        draw.ellipse((245, 455, 475, 685), outline=_alpha(PURPLE, 0.55), width=8)
        question_font = _font(_find_font(True), 130)
        draw.text((300, 465), "?", font=question_font, fill=_alpha(PURPLE, alpha))
        _draw_visual_card(draw, (68, 700, 652, 885), "ВІДПОВІДЬ", body, elapsed - 0.25, accent=True, body_size=21)
    else:
        draw.rounded_rectangle((70, 470, 650, 900), radius=28, fill=WHITE, outline=(208, 204, 218, 255), width=2)
        question_font = _font(_find_font(True), 150)
        draw.text((93, 480), "?", font=question_font, fill=(*PURPLE, 255))
        question = scene.get("headline") or body
        font, lines, line_height = _fit(draw, _visual_excerpt(question, 12), _find_font(True), 480, 190, 30, 20, 1.1)
        for idx, line in enumerate(lines):
            draw.text((110, 675 + idx * line_height), line, font=font, fill=INK)


def _draw_risk_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, path: bool = False) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    body = scene.get("body", "")
    alpha = _ease_out((elapsed - 0.12) / 0.45)
    if path:
        _draw_visual_card(draw, (68, 475, 652, 650), "РИЗИК", body, elapsed, body_size=21)
        draw.line((110, 730, 610, 730), fill=(213, 207, 220, 255), width=5)
        for idx, x in enumerate((120, 360, 600)):
            point_alpha = _ease_out((elapsed - 0.65 - idx * 0.18) / 0.4)
            draw.ellipse((x - 18, 712, x + 18, 748), fill=_alpha(PURPLE, point_alpha))
        for x, label in zip((120, 360, 600), ("ВИЯВИТИ", "ОЦІНИТИ", "ДІЯТИ")):
            font = _font(_find_font(True), 14)
            draw.text((x - draw.textlength(label, font=font) / 2, 775), label, font=font, fill=MUTED)
    else:
        draw.polygon([(360, 460), (650, 870), (70, 870)], fill=_alpha(PURPLE_LIGHT, alpha), outline=_alpha(PURPLE, alpha))
        draw.text((315, 560), "!", font=_font(_find_font(True), 100), fill=_alpha(PURPLE_DARK, alpha))
        _draw_visual_card(draw, (98, 745, 622, 900), "ЗВЕРНІТЬ УВАГУ", body,
                          elapsed - 0.2, accent=True, body_size=18)


def _draw_news_visual(canvas: Image.Image, scene: Dict[str, str], elapsed: float, timeline: bool = False) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    body = scene.get("body", "")
    progress = _ease_out((elapsed - 0.15) / 0.5)
    draw.rounded_rectangle((60, 460, 660, 900), radius=24, fill=(255, 255, 255, 242), outline=(210, 206, 219, 255), width=2)
    draw.rounded_rectangle((84, 485, 268, 527), radius=9, fill=(*PURPLE_LIGHT, 255))
    draw.text((99, 497), "ОФІЦІЙНЕ ОНОВЛЕННЯ", font=_font(_find_font(True), 13), fill=PURPLE_DARK)
    draw.text((85, 555), "ЩО ЗМІНИЛОСЯ", font=_font(_find_font(True), 27), fill=INK)
    font, lines, line_height = _fit(draw, _visual_excerpt(body, 18), _find_font(True), 500, 132, 26, 18, 1.1)
    for idx, line in enumerate(lines):
        draw.text((86, 610 + idx * line_height), line, font=font, fill=INK)
    if timeline:
        y = 810
        draw.line((100, y, 615, y), fill=(190, 178, 220, 255), width=4)
        for x in (112, 365, 608):
            radius = int(12 * progress)
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=(*PURPLE, 255))
        for x, label in zip((112, 365, 608), ("ПОДІЯ", "НОРМА", "ЗАСТОСУВАННЯ")):
            font = _font(_find_font(True), 13)
            draw.text((x - draw.textlength(label, font=font) / 2, 838), label, font=font, fill=MUTED)
    else:
        draw.rounded_rectangle((86, 808, 610, 824), radius=7, fill=(239, 235, 246, 255))
        draw.rounded_rectangle((86, 808, 86 + int(524 * progress), 824), radius=7, fill=(*PURPLE, 255))


def _draw_cover_motif(draw: ImageDraw.ImageDraw, video_type: str, elapsed: float) -> None:
    p = _spring((elapsed - 0.72) / 0.7)
    if video_type == "news":
        draw.rounded_rectangle((82, 800, 638, 910), radius=18, fill=(255, 255, 255, 235), outline=(210, 206, 219, 230), width=2)
        draw.rounded_rectangle((105, 825, 248, 865), radius=8, fill=(*PURPLE_LIGHT, 255))
        draw.text((119, 836), "ОНОВЛЕННЯ", font=_font(_find_font(True), 14), fill=PURPLE_DARK)
        draw.line((280, 846, 600, 846), fill=(193, 182, 222, 255), width=5)
        for x in (290, 430, 585):
            radius = int(10 * p)
            draw.ellipse((x-radius, 846-radius, x+radius, 846+radius), fill=(*PURPLE, 255))
    elif video_type == "howto":
        for idx, x in enumerate((150, 360, 570)):
            radius = int(37 * p)
            draw.ellipse((x-radius, 813-radius, x+radius, 813+radius), fill=(*PURPLE, 245), outline=(*PURPLE_DARK, 255), width=2)
            label = f"{idx + 1:02d}"
            font = _font(_find_font(True), 26)
            draw.text((x - draw.textlength(label, font=font) / 2, 796), label, font=font, fill=WHITE)
            if idx < 2:
                draw.line((x + 45, 813, x + 165, 813), fill=(193, 182, 222, 255), width=4)
    elif video_type == "myth_fact":
        draw.rounded_rectangle((80, 790, 340, 910), radius=16, fill=(255, 255, 255, 240), outline=(210, 206, 219, 255), width=2)
        draw.rounded_rectangle((380, 790, 640, 910), radius=16, fill=(*PURPLE_LIGHT, 255), outline=(*PURPLE, 255), width=2)
        draw.text((132, 829), "МІФ", font=_font(_find_font(True), 26), fill=MUTED)
        draw.text((432, 829), "ФАКТ", font=_font(_find_font(True), 26), fill=PURPLE_DARK)
    elif video_type == "case":
        draw.rounded_rectangle((125, 785, 595, 925), radius=16, fill=(255, 255, 255, 235), outline=(210, 206, 219, 255), width=2)
        draw.line((170, 820, 170, 885), fill=(*PURPLE, 255), width=5)
        for y in (825, 855, 885):
            draw.ellipse((158, y-7, 182, y+7), fill=(*PURPLE, 255))
        for y in (820, 850, 880):
            draw.rounded_rectangle((205, y, 545, y+7), radius=3, fill=(200, 196, 211, 220))
    elif video_type == "checklist":
        for x, y in ((110, 800), (280, 835), (450, 800)):
            draw.rounded_rectangle((x, y, x+120, y+92), radius=14, fill=(255, 255, 255, 240), outline=(210, 206, 219, 255), width=2)
            draw.rounded_rectangle((x+25, y+25, x+95, y+33), radius=4, fill=(*PURPLE, 240))
            draw.rounded_rectangle((x+25, y+48, x+82, y+56), radius=4, fill=(205, 201, 215, 235))
    elif video_type == "comparison":
        draw.rounded_rectangle((75, 800, 340, 920), radius=16, fill=(255, 255, 255, 240), outline=(210, 206, 219, 255), width=2)
        draw.rounded_rectangle((380, 800, 645, 920), radius=16, fill=(*PURPLE_LIGHT, 255), outline=(*PURPLE, 255), width=2)
        draw.text((165, 835), "A", font=_font(_find_font(True), 46), fill=INK)
        draw.text((470, 835), "B", font=_font(_find_font(True), 46), fill=PURPLE_DARK)
    elif video_type == "qa":
        draw.ellipse((405, 755, 625, 975), outline=(132, 82, 247, 110), width=5)
        draw.text((468, 760), "?", font=_font(_find_font(True), 155), fill=(*PURPLE, 230))
    elif video_type == "risk":
        draw.polygon([(540, 760), (660, 940), (420, 940)], fill=(*PURPLE_LIGHT, 255), outline=(*PURPLE, 255))
        draw.text((515, 810), "!", font=_font(_find_font(True), 95), fill=PURPLE_DARK)
    else:
        for idx, (x, y) in enumerate(((150, 850), (360, 790), (570, 850))):
            radius = int(33 * p)
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), outline=(132, 82, 247, 185), width=4)
            draw.line((x-16, y, x-4, y+13), fill=(*PURPLE_DARK, 255), width=7)
            draw.line((x-4, y+13, x+20, y-18), fill=(*PURPLE_DARK, 255), width=7)
            if idx < 2:
                draw.line((x+34, y, x+175, y-35 if idx == 0 else y+35), fill=(203, 194, 224, 255), width=3)


def _draw_cover(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    video_type = scene.get("video_type", "explainer")
    label_font = _font(_find_font(True), 18)
    progress = _ease_out((elapsed - 0.10) / 0.35)
    draw.rounded_rectangle((44, 142, 320, 188), radius=9, fill=_alpha(PURPLE_LIGHT, progress))
    draw.text((60, 154), scene.get("label", "ПРАВОВИЙ РОЗБІР"), font=label_font, fill=_alpha(PURPLE_DARK, progress))
    _draw_word_reveal(draw, scene.get("title", "ПРАВОВИЙ РОЗБІР").upper(), _find_font(True),
                      (48, 250), 620, 400, 72, elapsed - 0.22, INK, line_delay=0.15)
    _draw_cover_motif(draw, video_type, elapsed)


def _short_heading(text: str) -> str:
    words = text.strip().split()
    if not words:
        return "ПРАВОВИЙ ФАКТ"
    selected = words[:5]
    phrase = " ".join(selected).strip(" ,;:")
    return phrase + ("…" if len(phrase) < len(text) else "")


def _content_heading(scene: Dict[str, str]) -> str:
    headings = {
        "news": "ЩО ВАЖЛИВО В ОНОВЛЕННІ",
        "explainer": "СУТЬ ПРАВОВОГО ПИТАННЯ",
        "howto": "ПОСЛІДОВНІСТЬ ДІЙ",
        "myth_fact": "ПЕРЕВІРКА ТВЕРДЖЕННЯ",
        "case": "МАТЕРІАЛ СУДОВОЇ СПРАВИ",
        "checklist": "ПУНКТИ ДЛЯ ПЕРЕВІРКИ",
        "comparison": "ПОРІВНЯННЯ УМОВ",
        "qa": "ВІДПОВІДЬ НА ЗАПИТАННЯ",
        "risk": "ЗОНА ПРАВОВОГО РИЗИКУ",
    }
    return headings.get(scene.get("video_type", "explainer"), headings["explainer"])


def _draw_content(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    label = scene.get("label", "ПРАВОВЕ ПОЯСНЕННЯ")
    alpha = _ease_out((elapsed - 0.06) / 0.30)
    draw.text((48, 138), label, font=_font(_find_font(True), 17), fill=_alpha(PURPLE_DARK, alpha))
    draw.rounded_rectangle((48, 174, 240, 181), radius=4, fill=_alpha(PURPLE, alpha))
    heading = scene.get("visual_title") or _content_heading(scene)
    _draw_word_reveal(draw, heading.upper(), _find_font(True), (48, 218), 620, 170, 51,
                      elapsed - 0.15, INK, line_delay=0.12)

    layout = scene.get("layout", "concept_map")
    if layout == "news_bulletin":
        _draw_news_visual(canvas, scene, elapsed, False)
    elif layout == "news_timeline":
        _draw_news_visual(canvas, scene, elapsed, True)
    elif layout == "concept_map":
        _draw_explainer_visual(canvas, scene, elapsed, False)
    elif layout == "editorial_layers":
        _draw_explainer_visual(canvas, scene, elapsed, True)
    elif layout == "steps_rail":
        _draw_howto_visual(canvas, scene, elapsed, False)
    elif layout == "steps_cards":
        _draw_howto_visual(canvas, scene, elapsed, True)
    elif layout == "myth_split":
        _draw_myth_visual(canvas, scene, elapsed, False)
    elif layout == "myth_reveal":
        _draw_myth_visual(canvas, scene, elapsed, True)
    elif layout == "case_timeline":
        _draw_case_visual(canvas, scene, elapsed, False)
    elif layout == "case_dossier":
        _draw_case_visual(canvas, scene, elapsed, True)
    elif layout == "checklist_grid":
        _draw_checklist_visual(canvas, scene, elapsed, True)
    elif layout == "checklist_stack":
        _draw_checklist_visual(canvas, scene, elapsed, False)
    elif layout == "comparison_split":
        _draw_comparison_visual(canvas, scene, elapsed, False)
    elif layout == "comparison_scale":
        _draw_comparison_visual(canvas, scene, elapsed, True)
    elif layout == "question_card":
        _draw_qa_visual(canvas, scene, elapsed, False)
    elif layout == "answer_focus":
        _draw_qa_visual(canvas, scene, elapsed, True)
    elif layout == "risk_alert":
        _draw_risk_visual(canvas, scene, elapsed, False)
    elif layout == "risk_path":
        _draw_risk_visual(canvas, scene, elapsed, True)
    else:
        raise ValueError(f"Невідомий тип композиції: {layout}")

    # The news panel already carries the complete short storyboard subtitle.
    # Do not repeat it in a second card; retain the full-width copy box for prose chunks
    # and for long storyboards that exceed the excerpt capacity.
    body = scene.get("body", "")
    complete_in_news_panel = (
        layout in ("news_bulletin", "news_timeline")
        and bool(scene.get("visual_title"))
        and len(body.split()) <= 18
    )
    if not complete_in_news_panel:
        box_top = 960
        slide = int((1 - _ease_out((elapsed - 0.50) / 0.42)) * 64)
        draw.rounded_rectangle((42, box_top + slide + 8, WIDTH - 42, 1198 + slide + 8), radius=17,
                               fill=(18, 14, 35, 20))
        draw.rounded_rectangle((42, box_top + slide, WIDTH - 42, 1198 + slide), radius=17,
                               fill=(255, 255, 255, 250), outline=(150, 145, 164, 255), width=2)
        draw.rounded_rectangle((42, box_top + slide, 52, 1198 + slide), radius=5, fill=(*PURPLE, 255))
        font, lines, line_height = _fit(draw, body, _find_font(True), 590, 190, 29, 19, 1.12)
        y = box_top + slide + max(22, int((216 - len(lines) * line_height) / 2))
        for idx, line in enumerate(lines):
            opacity = _ease_out((elapsed - 0.66 - idx * 0.07) / 0.35)
            draw.text((72, y + int((1 - opacity) * 12)), line, font=font, fill=_alpha(INK, opacity))
            y += line_height


def _draw_story(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    _draw_cover(canvas, scene, elapsed * 0.55)
    draw = ImageDraw.Draw(canvas, "RGBA")
    body = scene.get("body", "")
    top = 915
    draw.rounded_rectangle((42, top, WIDTH - 42, 1195), radius=18, fill=(255, 255, 255, 246),
                           outline=(155, 148, 171, 255), width=2)
    font, lines, line_height = _fit(draw, body, _find_font(True), 590, 220, 30, 19, 1.12)
    for idx, line in enumerate(lines):
        opacity = _ease_out((elapsed - 0.8 - idx * 0.08) / 0.36)
        draw.text((68, top + 30 + idx * line_height + int((1 - opacity) * 12)), line,
                  font=font, fill=_alpha(INK, opacity))


def _draw_closing(canvas: Image.Image, scene: Dict[str, str], elapsed: float) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    progress = _spring(elapsed / 0.65)
    top = 710 + int((1 - progress) * 100)
    draw.rounded_rectangle((-35, top, WIDTH + 35, 1170), radius=42, fill=(*PURPLE, 255))
    _draw_word_reveal(draw, scene.get("title", "ЗБЕРЕЖІТЬ МАТЕРІАЛ").upper(), _find_font(True),
                      (48, 250), 620, 360, 76, elapsed - 0.12, INK, line_delay=0.15)
    brand_font = _font(_find_font(True), 22)
    draw.text((52, top + 52), "SENTINEL LAW", font=brand_font, fill=WHITE)
    body = scene.get("body", "")
    font, lines, line_height = _fit(draw, body, _find_font(True), 600, 160, 29, 19, 1.12)
    for idx, line in enumerate(lines):
        opacity = _ease_out((elapsed - 0.55 - idx * 0.08) / 0.38)
        draw.text((52, top + 132 + idx * line_height + int((1 - opacity) * 14)),
                  line, font=font, fill=_alpha(WHITE, opacity))
    for idx in range(6):
        offset = int(10 * math.sin(elapsed * 2.8 + idx))
        x = 510 + (idx % 3) * 48
        y = 380 + (idx // 3) * 48 + offset
        draw.rounded_rectangle((x, y, x + 27, y + 27), radius=7, fill=(132, 82, 247, 115))


def _draw_frame(scene: Dict[str, str], scene_index: int, total_scenes: int,
                local_frame: int, global_frame: int, total_frames: int) -> Image.Image:
    elapsed = local_frame / FPS
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    _draw_background(canvas, global_frame, scene_index)
    _draw_header(canvas, scene_index, total_scenes, global_frame / max(1, total_frames - 1))
    kind = scene["kind"]
    if kind == "cover":
        _draw_cover(canvas, scene, elapsed)
    elif kind == "closing":
        _draw_closing(canvas, scene, elapsed)
    elif kind == "story":
        _draw_story(canvas, scene, elapsed)
    elif kind == "content":
        _draw_content(canvas, scene, elapsed)
    else:
        raise ValueError(f"Невідомий тип сцени: {kind}")
    base = Image.new("RGBA", (WIDTH, HEIGHT), (*BG, 255))
    return Image.alpha_composite(base, canvas).convert("RGB")


def _synth_music(path: Path, duration: float, seed_text: str, bpm: float = 112.0) -> None:
    """Create an original stereo bed and short synthetic transition accents."""
    sample_count = max(1, int(AUDIO_SR * duration))
    t = np.arange(sample_count, dtype=np.float32) / AUDIO_SR
    rng = np.random.default_rng(int(hashlib.sha256(seed_text.encode()).hexdigest()[:8], 16))
    beat = 60.0 / bpm
    bar = beat * 4
    chords = [
        (55.0, [220.0, 261.63, 329.63]),
        (43.65, [174.61, 220.0, 261.63]),
        (65.41, [196.0, 261.63, 329.63]),
        (49.0, [196.0, 246.94, 293.66]),
    ]
    left = np.zeros(sample_count, dtype=np.float32)
    right = np.zeros(sample_count, dtype=np.float32)
    chord_index = (t / bar).astype(np.int32) % len(chords)
    local = np.mod(t, bar)
    fade = np.minimum(1, np.minimum(local / 0.45, (bar - local) / 0.55)).astype(np.float32)
    for index, (_, notes) in enumerate(chords):
        mask = chord_index == index
        for note_index, frequency in enumerate(notes):
            amplitude = 0.020 / (1 + note_index * 0.25)
            left[mask] += amplitude * np.sin(2 * np.pi * frequency * t[mask] + note_index * 0.28)
            right[mask] += amplitude * np.sin(2 * np.pi * frequency * 1.002 * t[mask] + note_index * 0.35)
    left *= fade
    right *= fade

    kick_len = int(0.22 * AUDIO_SR)
    kt = np.arange(kick_len, dtype=np.float32) / AUDIO_SR
    kick = (0.24 * np.sin(2 * np.pi * (58 - 30 * kt / 0.22) * kt) * np.exp(-kt * 19)).astype(np.float32)
    clap_len = int(0.16 * AUDIO_SR)
    ct = np.arange(clap_len, dtype=np.float32) / AUDIO_SR
    clap = (0.065 * rng.normal(0, 1, clap_len) * np.exp(-ct * 24) +
            0.035 * np.sin(2 * np.pi * 190 * ct) * np.exp(-ct * 21)).astype(np.float32)
    hat_len = int(0.045 * AUDIO_SR)
    ht = np.arange(hat_len, dtype=np.float32) / AUDIO_SR
    hat = (0.018 * rng.normal(0, 1, hat_len) * np.exp(-ht * 85)).astype(np.float32)

    def add(sample: np.ndarray, start: float, pan: float = 0.0, gain: float = 1.0) -> None:
        start_index = int(start * AUDIO_SR)
        if start_index >= sample_count:
            return
        end_index = min(sample_count, start_index + len(sample))
        part = sample[:end_index - start_index] * gain
        left[start_index:end_index] += part * (1 - max(0, pan))
        right[start_index:end_index] += part * (1 + min(0, pan))

    for beat_index, start in enumerate(np.arange(0, duration, beat)):
        chord = chords[int(start / bar) % len(chords)]
        bass_t = np.arange(int(0.28 * AUDIO_SR), dtype=np.float32) / AUDIO_SR
        bass = (0.052 * np.sin(2 * np.pi * chord[0] * bass_t) * np.exp(-bass_t * 8)).astype(np.float32)
        add(bass, start)
        add(kick if beat_index % 4 in (0, 2) else clap, start, gain=0.76 if beat_index % 4 in (0, 2) else 1.0)
        add(hat, start, pan=0.18 if beat_index % 2 else -0.18)
        add(hat, start + beat / 2, pan=-0.18 if beat_index % 2 else 0.18)

    for index, start in enumerate(np.arange(0.0, duration, 5.2)):
        cue_t = np.arange(int(0.28 * AUDIO_SR), dtype=np.float32) / AUDIO_SR
        noise = rng.normal(0, 1, len(cue_t)).astype(np.float32)
        sweep = noise * np.sin(np.pi * np.minimum(1, cue_t / 0.28)) * np.exp(-cue_t * 7) * 0.035
        add(sweep, start, pan=(-0.15 if index % 2 else 0.15))
        click = (0.045 * np.sin(2 * np.pi * (900 + 120 * index) * cue_t) * np.exp(-cue_t * 40)).astype(np.float32)
        add(click, start + 0.18, pan=(0.2 if index % 2 else -0.2))

    peak = max(float(np.max(np.abs(left))), float(np.max(np.abs(right))), 1e-6)
    gain = min(1.0, 0.72 / peak)
    envelope = np.clip(t / 0.55, 0, 1) * np.clip((duration - t) / 0.85, 0, 1)
    stereo = np.stack((left * gain * envelope, right * gain * envelope), axis=1)
    pcm = np.int16(np.clip(stereo, -1, 1) * 32767)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(AUDIO_SR)
        audio.writeframes(pcm.tobytes())


def _probe(path: Path, ffprobe: str = "ffprobe") -> Dict[str, Any]:
    if not shutil.which(ffprobe):
        return {"size": path.stat().st_size}
    result = subprocess.run([
        ffprobe, "-v", "error", "-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height,sample_rate,channels",
        "-of", "json", str(path)
    ], capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise RuntimeError("ffprobe не зміг перевірити MP4: " + result.stderr[-1000:])
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video or video.get("codec_name") != "h264" or (video.get("width"), video.get("height")) != (WIDTH, HEIGHT):
        raise RuntimeError("Відеопотік не відповідає контракту H.264 720×1280.")
    if not audio or audio.get("codec_name") != "aac" or audio.get("channels") != 2:
        raise RuntimeError("Аудіопотік не відповідає контракту AAC stereo.")
    return {
        "size": int(data.get("format", {}).get("size", path.stat().st_size)),
        "duration": float(data.get("format", {}).get("duration", 0)),
        "streams": streams,
    }


def build_video(job: Dict[str, Any], output_path: str | Path, ffmpeg: str = "ffmpeg") -> Path:
    """Render one Cloud Video Bridge job to MP4. Signature is compatible with worker.py."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not shutil.which(ffmpeg):
        raise RuntimeError("FFmpeg не встановлений.")
    fmt = str(job.get("format") or "Reels").strip().lower()
    if fmt not in ("reels", "story"):
        fmt = "reels"
    content_id = _clean(job.get("content_id") or "SENTINEL-LAW", 120)
    scenes = _make_scenes(job)
    if not scenes:
        raise ValueError("Після очищення сценарію не залишилося тексту для відео.")
    durations = _scene_durations(scenes, fmt)
    frame_counts = [max(1, round(seconds * FPS)) for seconds in durations]
    total_frames = sum(frame_counts)
    duration = total_frames / FPS
    target_bytes = MAX_UPLOAD_BYTES * 0.91
    audio_bps = 112_000
    video_bps = max(420_000, min(1_450_000, int((target_bytes * 8 / duration) - audio_bps)))
    maxrate = int(video_bps * 1.2)
    seed = content_id + str(job.get("headline", ""))

    with tempfile.TemporaryDirectory(prefix="sentinel-motion-v4-") as temp_dir:
        temp = Path(temp_dir)
        audio_path = temp / "music.wav"
        _synth_music(audio_path, duration, seed)
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}",
            "-r", str(FPS), "-i", "pipe:0", "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0", "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "animation",
            "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
            "-b:v", str(video_bps), "-maxrate", str(maxrate), "-bufsize", str(video_bps * 2),
            "-g", str(FPS * 2), "-keyint_min", str(FPS * 2), "-sc_threshold", "0",
            "-threads", "2", "-c:a", "aac", "-b:a", str(audio_bps), "-ar", str(AUDIO_SR), "-ac", "2",
            "-af", "loudnorm=I=-15:TP=-2.5:LRA=7", "-movflags", "+faststart", str(output),
        ]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        global_frame = 0
        try:
            if process.stdin is None:
                raise RuntimeError("Не вдалося відкрити відеопотік FFmpeg.")
            for scene_index, (scene, frame_count) in enumerate(zip(scenes, frame_counts), start=1):
                for local_frame in range(frame_count):
                    frame = _draw_frame(scene, scene_index, len(scenes), local_frame, global_frame, total_frames)
                    process.stdin.write(frame.tobytes())
                    global_frame += 1
            process.stdin.close()
            error_output = process.stderr.read() if process.stderr else b""
            if process.stderr:
                process.stderr.close()
            return_code = process.wait(timeout=max(180, int(duration * 8)))
        except Exception:
            process.kill()
            process.wait()
            if process.stderr:
                process.stderr.close()
            raise
        if return_code != 0:
            details = error_output.decode("utf-8", "replace")[-2500:]
            raise RuntimeError("FFmpeg render failed: " + details)

    if not output.exists() or output.stat().st_size < 100_000:
        raise RuntimeError("MP4 відсутній або має некоректний розмір.")
    info = _probe(output)
    if info.get("size", 0) > MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"MP4 має {info['size']} байтів, ліміт {MAX_UPLOAD_BYTES}. "
            "Скороти сценарій — текст автоматично не обрізається."
        )
    print(json.dumps({
        "engine": "SENTINEL LAW Motion Engine V4.2",
        "video_type": scenes[0].get("video_type", _infer_video_type(job)),
        "content_id": content_id,
        "scene_count": len(scenes),
        "duration_seconds": round(info.get("duration", duration), 2),
        "file_bytes": info.get("size"),
        "resolution": f"{WIDTH}x{HEIGHT}",
        "fps": FPS,
        "audio": "AAC stereo",
        "text_policy": "all audience-facing script text retained",
        "visual_style": "type-specific editorial layouts / kinetic legal graphics",
    }, ensure_ascii=False))
    return output


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SENTINEL LAW Motion Engine V4.2")
    parser.add_argument("--job", required=True, help="Path to a JSON job payload")
    parser.add_argument("--output", default="sentinel-law-v4.mp4", help="Output MP4 path")
    args = parser.parse_args()
    payload = json.loads(Path(args.job).read_text(encoding="utf-8"))
    build_video(payload, args.output)
