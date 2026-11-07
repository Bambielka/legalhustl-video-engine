import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from renderer import (
    HEIGHT,
    WIDTH,
    LAYOUTS,
    VIDEO_TYPE_LABELS,
    _citation,
    _comparison_parts,
    _draw_frame,
    _extract_script_text,
    _fit,
    _find_font,
    _infer_video_type,
    _layout_for,
    _make_scenes,
    _scene_durations,
    _scene_label,
    _split_script,
    _visual_points,
    build_video,
)


class MotionEngineV41Tests(unittest.TestCase):
    def test_full_copy_is_preserved_in_order(self):
        source = (
            "Військовослужбовець має право знати зміст наказу. "
            "Зверніться з рапортом та збережіть підтвердження подання. "
            "Документ може містити інформацію з обмеженим доступом."
        )
        pieces = _split_script(source)
        self.assertEqual(" ".join(pieces).split(), source.split())

    def test_split_script_respects_word_and_character_limits(self):
        source = " ".join([f"фрагмент{i}" for i in range(80)])
        chunks = _split_script(source, max_words=12, max_chars=100)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(" ".join(chunks).split(), source.split())
        for chunk in chunks:
            self.assertLessEqual(len(chunk.split()), 16)  # small tail merge is permitted
            self.assertLessEqual(len(chunk), 165)  # small tail merge is permitted

    def test_long_copy_creates_adaptive_scene_count(self):
        short = _make_scenes({"format": "Reels", "headline": "Тест", "script": "Короткий правовий висновок."})
        long_text = " ".join(
            [f"Положення {i} визначає порядок звернення та підтвердження відповідної юридичної дії." for i in range(1, 16)]
        )
        long = _make_scenes({"format": "Reels", "headline": "Тест", "script": long_text})
        self.assertGreater(len(long), len(short))
        self.assertEqual(long[0]["kind"], "cover")
        self.assertEqual(long[-1]["kind"], "closing")

    def test_stage_directions_are_not_rendered(self):
        brief = (
            "СЦЕНА 1: 00:00–00:04\n"
            "ВІЗУАЛ: білий фон, анімація картки\n"
            "ТЕКСТ ДИКТОРА: Військовослужбовець має право отримати відповідь.\n"
            "СЦЕНА 2: 00:04–00:08\n"
            "ТЕКСТ НА ЕКРАНІ: Подайте рапорт письмово."
        )
        result = _extract_script_text(brief)
        self.assertNotIn("білий фон", result)
        self.assertNotIn("СЦЕНА", result)
        self.assertIn("Військовослужбовець має право", result)
        self.assertIn("Подайте рапорт письмово", result)

    def test_visual_notes_heading_and_instruction_block_are_removed(self):
        brief = (
            "ВІЗУАЛЬНІ ПРИМІТКИ: використовувати текстові картки, не показувати вигадані кадри чи документи.\n"
            "- Зробити плавний перехід.\n"
            "- Додати фіолетову плашку.\n"
            "ФАКТ: Кабінет Міністрів ухвалив постанову щодо програми.\n"
            "УМОВА: Механізм почне діяти після офіційного оприлюднення порядку.\n"
            "ЩО ВАЖЛИВО: Перевірте умови до подання заявки."
        )
        result = _extract_script_text(brief)
        self.assertNotIn("ВІЗУАЛЬНІ ПРИМІТКИ", result)
        self.assertNotIn("використовувати текстові картки", result.lower())
        self.assertNotIn("вигадані кадри", result.lower())
        self.assertNotIn("плавний перехід", result.lower())
        self.assertNotIn("фіолетову плашку", result.lower())
        self.assertIn("Кабінет Міністрів ухвалив постанову", result)
        self.assertIn("Механізм почне діяти", result)
        self.assertIn("Перевірте умови", result)

    def test_unlabelled_visual_continuation_is_removed_until_audience_text_field(self):
        source = (
            "ВІЗУАЛЬНІ ПРИМІТКИ:\n"
            "Білий фон, плавні переходи, поява текстових карток.\n"
            "На фоні з'являється фіолетова графіка.\n"
            "Без людей та реальних документів.\n"
            "ТЕКСТ НА ЕКРАНІ: Звернення потрібно перевірити перед поданням."
        )
        result = _extract_script_text(source)
        self.assertEqual(result, "Звернення потрібно перевірити перед поданням.")

    def test_numbered_step_labels_are_not_duplicated_in_graphic_cards(self):
        result = _extract_script_text("КРОК 1: Визначте адресата та мету.\nКРОК 2: Викладіть обставини.")
        self.assertEqual(result, "Визначте адресата та мету. Викладіть обставини.")

    def test_plain_prose_is_not_removed_as_a_director_note(self):
        source = "Не пропускайте строки оскарження та зберігайте копії документів."
        self.assertEqual(_extract_script_text(source), source)

    def test_technical_only_script_falls_back_to_headline(self):
        scenes = _make_scenes({
            "format": "Reels",
            "headline": "Як діяти у правовій ситуації",
            "script": "ВІЗУАЛЬНІ ПРИМІТКИ: використовувати картки та анімацію.\nНЕ ПОКАЗУВАТИ: обличчя та документи.",
        })
        self.assertEqual(scenes[1]["body"], "Як діяти у правовій ситуації")

    def test_citations_are_not_invented(self):
        self.assertEqual(_citation("Застосовується стаття 19 Конституції України."), "стаття 19")
        self.assertEqual(_citation("Частина 2 статті 19 визначає межі."), "Частина 2 статті 19")
        self.assertEqual(_citation("У цьому матеріалі немає номера статті."), "")

    def test_scene_label_uses_supplied_legal_reference(self):
        self.assertEqual(_scene_label("Відповідно до ч. 2 ст. 19 Закону.", 1, 3), "НОРМАТИВНА ОСНОВА")

    def test_duration_increases_with_text(self):
        short = _make_scenes({"format": "Reels", "headline": "Тест", "script": "Коротке речення."})
        long = _make_scenes({"format": "Reels", "headline": "Тест", "script": " ".join(["Юридичне пояснення з важливими деталями і строками."] * 8)})
        self.assertGreater(sum(_scene_durations(long, "reels")), sum(_scene_durations(short, "reels")))

    def test_single_long_word_fits_inside_frame(self):
        image = Image.new("RGBA", (WIDTH, HEIGHT), (255, 255, 255, 255))
        draw = ImageDraw.Draw(image)
        font, lines, _ = _fit(draw, "ВІЙСЬКОВОСЛУЖБОВЕЦЬ МАЄ ПРАВО", _find_font(True), 620, 170, 54, 27, 1.03)
        self.assertTrue(all(draw.textlength(word, font=font) <= 620 for word in "ВІЙСЬКОВОСЛУЖБОВЕЦЬ МАЄ ПРАВО".split()))

    def test_closing_preserves_full_cta(self):
        cta = "Збережіть матеріал і звертайтеся за фаховою правовою допомогою"
        scenes = _make_scenes({"format": "Reels", "headline": "Тест", "script": "Короткий матеріал.", "cta": cta})
        self.assertEqual(scenes[-1]["body"], cta)

    def test_myth_and_truth_labels_are_preserved(self):
        result = _extract_script_text("МІФ: Рішення не можна оскаржити.\nПРАВДА: Окремі рішення можуть бути оскаржені.")
        self.assertIn("МІФ: Рішення не можна оскаржити", result)
        self.assertIn("ПРАВДА: Окремі рішення можуть бути оскаржені", result)

    def test_editorial_video_type_detection(self):
        cases = [
            ({"headline": "Кабмін ухвалив зміни до програми", "script": "Постанову ухвалено."}, "news"),
            ({"headline": "Як подати рапорт", "script": "Спочатку підготуйте документ."}, "howto"),
            ({"headline": "Міф чи правда про оскарження", "script": "Міф: рішення остаточне."}, "myth_fact"),
            ({"headline": "Що вирішив суд у справі", "script": "Суд перевірив обставини спору."}, "case"),
            ({"headline": "Які документи потрібні", "script": "Перелік документів для звернення."}, "checklist"),
            ({"headline": "Було / стало: новий порядок", "script": "Було: особисто. Стало: онлайн."}, "comparison"),
            ({"headline": "Чи має право військовий на виплату?", "script": "Право залежить від підстав."}, "qa"),
            ({"headline": "Помилка під час подання скарги", "script": "Пропуск строку створює ризик."}, "risk"),
            ({"headline": "Як працює правовий механізм", "script": "Правовий механізм має декілька складових."}, "explainer"),
        ]
        for job, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_infer_video_type(job), expected)

    def test_qa_content_scene_retains_the_question_headline(self):
        scenes = _make_scenes({
            "format": "Reels", "video_type": "qa", "headline": "Чи можна оскаржити рішення?",
            "script": "Так, за наявності передбачених законом підстав.",
        })
        content = next(scene for scene in scenes if scene["kind"] == "content")
        self.assertEqual(content["headline"], "Чи можна оскаржити рішення?")
        self.assertIn("передбачених законом", content["body"])

    def test_explicit_video_type_has_priority(self):
        self.assertEqual(_infer_video_type({"video_type": "Судова практика", "headline": "Як подати заяву?"}), "case")
        self.assertEqual(_infer_video_type({"visual_type": "checklist", "headline": "Зміни ухвалено"}), "checklist")

    def test_layout_variant_is_stable_for_a_video_and_varies_between_ids(self):
        for video_type, variants in LAYOUTS.items():
            first = _layout_for(video_type, 1, "content-alpha")
            self.assertEqual(first, _layout_for(video_type, 2, "content-alpha"))
            second_id = next(f"content-{n}" for n in range(100) if _layout_for(video_type, 1, f"content-{n}") != first)
            self.assertIn(_layout_for(video_type, 1, second_id), variants)
            self.assertNotEqual(first, _layout_for(video_type, 1, second_id))

    def test_each_editorial_type_uses_a_distinct_layout_family(self):
        jobs = {
            "news": {"headline": "Офіційно ухвалено зміни до порядку", "script": "Зміни набрали чинності."},
            "howto": {"headline": "Як подати рапорт", "script": "Підготуйте рапорт та подайте його."},
            "myth_fact": {"headline": "Міф чи правда", "script": "Міф: це неможливо."},
            "case": {"headline": "Судове рішення у справі", "script": "Суд дійшов висновку."},
            "checklist": {"headline": "Перелік документів", "script": "Які документи потрібно підготувати."},
            "comparison": {"headline": "Було / стало", "script": "Було одне правило, стало інше."},
            "qa": {"headline": "Чи можна оскаржити рішення?", "script": "Так, за визначених умов."},
            "risk": {"headline": "Ризик пропуску строку", "script": "Не пропускайте строк."},
            "explainer": {"headline": "Правовий механізм", "script": "Механізм складається з декількох елементів."},
        }
        layouts = {}
        for expected, values in jobs.items():
            scenes = _make_scenes({"format": "Reels", "content_id": expected, **values})
            content = next(scene for scene in scenes if scene["kind"] == "content")
            self.assertEqual(content["video_type"], expected)
            layouts[expected] = content["layout"]
        self.assertEqual(len(set(layouts.values())), len(layouts))

    def test_visual_points_never_return_duplicate_or_empty_items(self):
        for sample in ("Одна фраза.", "Два слова", "Один, два, три, чотири.", ""):
            points = _visual_points(sample)
            self.assertLessEqual(len(points), 3)
            self.assertEqual(len(points), len(set(points)))
            self.assertTrue(all(point.strip() for point in points))
        self.assertEqual(len(_visual_points("Одна фраза.")), 1)
        self.assertEqual(len(_visual_points("Два слова")), 1)

    def test_comparison_visual_handles_missing_second_side(self):
        left, right = _comparison_parts("Лише один описаний аспект без другої сторони.")
        self.assertTrue(left)
        self.assertEqual(right, "")
        left, right = _comparison_parts("Було: паперова форма. Стало: електронна форма.")
        self.assertIn("паперова", left)
        self.assertIn("електронна", right)

    def test_short_howto_and_checklist_scenarios_render_without_index_errors(self):
        for video_type in ("howto", "checklist"):
            for copy in ("Так.", "Два слова", "Подайте рапорт."):
                scenes = _make_scenes({
                    "format": "Reels", "content_id": f"{video_type}-{len(copy)}",
                    "video_type": video_type, "headline": "Коротка правова підказка", "script": copy,
                })
                content = next(scene for scene in scenes if scene["kind"] == "content")
                for frame_no in (0, 20, 50):
                    frame = _draw_frame(content, 2, len(scenes), frame_no, frame_no, 100)
                    self.assertEqual(frame.size, (WIDTH, HEIGHT))

    def test_all_eighteen_layout_variants_render_a_frame(self):
        content_id_index = 0
        for video_type, variants in LAYOUTS.items():
            for expected_layout in variants:
                content_id = None
                for candidate_index in range(1000):
                    candidate = f"layout-{video_type}-{candidate_index}"
                    if _layout_for(video_type, 0, candidate) == expected_layout:
                        content_id = candidate
                        break
                self.assertIsNotNone(content_id, f"No content id found for layout {expected_layout}")
                scenes = _make_scenes({
                    "format": "Reels", "content_id": content_id, "video_type": video_type,
                    "headline": "Перевірка композиції", "script": "Факт: перший пункт. Другий пункт. Третій пункт.",
                })
                content = next(scene for scene in scenes if scene["kind"] == "content")
                self.assertEqual(content["layout"], expected_layout)
                frame = _draw_frame(content, 2, len(scenes), 45, 45, 120)
                self.assertEqual(frame.size, (WIDTH, HEIGHT))
                content_id_index += 1
        self.assertEqual(content_id_index, 18)

    def test_cover_and_closing_render_for_every_video_type(self):
        for video_type in VIDEO_TYPE_LABELS:
            scenes = _make_scenes({
                "format": "Reels", "video_type": video_type, "content_id": f"cover-{video_type}",
                "headline": "Правова тема для перевірки", "script": "Перший факт. Другий факт.",
            })
            self.assertEqual(_draw_frame(scenes[0], 1, len(scenes), 20, 20, 100).size, (WIDTH, HEIGHT))
            self.assertEqual(_draw_frame(scenes[-1], len(scenes), len(scenes), 20, 80, 100).size, (WIDTH, HEIGHT))

    def test_story_remains_single_story_scene(self):
        scenes = _make_scenes({"format": "Story", "headline": "Пам'ятка", "story_title": "Пам'ятка", "script": "Збережіть документ."})
        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0]["kind"], "story")
        frame = _draw_frame(scenes[0], 1, 1, 30, 30, 360)
        self.assertEqual(frame.size, (WIDTH, HEIGHT))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/ffprobe are required for MP4 smoke test")
    def test_build_video_produces_valid_mp4(self):
        job = {
            "job_id": "TEST-SMOKE-001", "content_id": "TEST-SMOKE-001", "format": "Story",
            "topic": "Збереження документів", "headline": "Підтвердження подання рапорту",
            "story_title": "ЗБЕРЕЖІТЬ ПІДТВЕРДЖЕННЯ", "script": "Зробіть копію рапорту та збережіть відмітку про його реєстрацію.",
            "cta": "Збережіть цю пам'ятку",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "smoke.mp4"
            build_video(job, target)
            self.assertTrue(target.exists())
            self.assertGreater(target.stat().st_size, 100_000)
            self.assertLessEqual(target.stat().st_size, 8_500_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
