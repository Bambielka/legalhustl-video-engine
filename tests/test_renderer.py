import unittest
from renderer import (
    _citation, _extract_script_text, _fit, _find_font, _make_scenes, _scene_durations,
    _split_script, _scene_label, _infer_video_type
)

class MotionEngineV4Tests(unittest.TestCase):
    def test_full_copy_is_preserved_in_order(self):
        source = (
            "Військовослужбовець має право знати зміст наказу. "
            "Зверніться з рапортом та збережіть підтвердження подання. "
            "Документ може містити інформацію з обмеженим доступом."
        )
        pieces = _split_script(source)
        self.assertEqual(" ".join(pieces).split(), source.split())

    def test_long_copy_creates_adaptive_scene_count(self):
        short = _make_scenes({"format":"Reels","headline":"Тест","script":"Короткий правовий висновок."})
        long_text = " ".join(
            [f"Положення {i} визначає порядок звернення та підтвердження відповідної юридичної дії." for i in range(1, 16)]
        )
        long = _make_scenes({"format":"Reels","headline":"Тест","script":long_text})
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

    def test_citations_are_not_invented(self):
        self.assertEqual(_citation("Застосовується стаття 19 Конституції України."), "стаття 19")
        self.assertEqual(_citation("У цьому матеріалі немає номера статті."), "")

    def test_scene_label_uses_supplied_legal_reference(self):
        self.assertEqual(_scene_label("Відповідно до ч. 2 ст. 19 Закону.", 1, 3), "НОРМАТИВНА ОСНОВА")

    def test_duration_increases_with_text(self):
        short = _make_scenes({"format":"Reels","headline":"Тест","script":"Коротке речення."})
        long = _make_scenes({"format":"Reels","headline":"Тест","script":" ".join(["Юридичне пояснення з важливими деталями і строками."] * 8)})
        self.assertGreater(sum(_scene_durations(long,"reels")), sum(_scene_durations(short,"reels")))


    def test_single_long_word_fits_inside_frame(self):
        from PIL import Image, ImageDraw
        image = Image.new("RGBA", (720, 1280), (255, 255, 255, 255))
        draw = ImageDraw.Draw(image)
        font, lines, line_height = _fit(draw, "ВІЙСЬКОВОСЛУЖБОВЕЦЬ МАЄ ПРАВО", _find_font(True), 620, 170, 54, 27, 1.03)
        self.assertTrue(all(draw.textlength(word, font=font) <= 620 for word in "ВІЙСЬКОВОСЛУЖБОВЕЦЬ МАЄ ПРАВО".split()))

    def test_closing_preserves_full_cta(self):
        cta = "Збережіть матеріал і звертайтеся за фаховою правовою допомогою"
        scenes = _make_scenes({"format":"Reels","headline":"Тест","script":"Короткий матеріал.","cta":cta})
        self.assertEqual(scenes[-1]["body"], cta)


    def test_visual_notes_heading_and_instruction_are_removed(self):
        brief = (
            "ВІЗУАЛЬНІ ПРИМІТКИ: використовувати текстові картки, не показувати вигадані кадри чи документи.\n"
            "ФАКТ: Кабінет Міністрів ухвалив постанову щодо програми.\n"
            "УМОВА: Механізм почне діяти після офіційного оприлюднення порядку.\n"
            "ЩО ВАЖЛИВО: Перевірте умови до подання заявки."
        )
        result = _extract_script_text(brief)
        self.assertNotIn("ВІЗУАЛЬНІ ПРИМІТКИ", result)
        self.assertNotIn("використовувати текстові картки", result.lower())
        self.assertNotIn("вигадані кадри", result.lower())
        self.assertIn("Кабінет Міністрів ухвалив постанову", result)
        self.assertIn("Механізм почне діяти", result)
        self.assertIn("Перевірте умови", result)

    def test_myth_and_truth_labels_are_preserved_for_layout(self):
        result = _extract_script_text("МІФ: Рішення не можна оскаржити.\nПРАВДА: Окремі рішення можуть бути оскаржені.")
        self.assertIn("МІФ: Рішення не можна оскаржити", result)
        self.assertIn("ПРАВДА: Окремі рішення можуть бути оскаржені", result)

    def test_editorial_video_type_detection(self):
        cases = [
            ({"headline":"Кабмін ухвалив зміни до програми","script":"Постанову ухвалено."}, "news"),
            ({"headline":"Як подати рапорт","script":"Спочатку підготуйте документ."}, "howto"),
            ({"headline":"Міф чи правда про оскарження","script":"Міф: рішення остаточне."}, "myth_fact"),
            ({"headline":"Що вирішив суд у справі","script":"Суд перевірив обставини спору."}, "case"),
            ({"headline":"Які документи потрібні","script":"Перелік документів для звернення."}, "checklist"),
            ({"headline":"Було / стало: новий порядок","script":"Було: особисто. Стало: онлайн."}, "comparison"),
            ({"headline":"Чи має право військовий на виплату?","script":"Право залежить від підстав."}, "qa"),
            ({"headline":"Помилка під час подання скарги","script":"Пропуск строку створює ризик."}, "risk"),
            ({"headline":"Як працює правовий механізм","script":"Правовий механізм має кілька складових."}, "explainer"),
        ]
        for job, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_infer_video_type(job), expected)

    def test_each_editorial_type_uses_a_distinct_layout_family(self):
        jobs = {
            "news": {"headline":"Офіційно ухвалено зміни до порядку","script":"Зміни набрали чинності."},
            "howto": {"headline":"Як подати рапорт","script":"Підготуйте рапорт та подайте його."},
            "myth_fact": {"headline":"Міф чи правда","script":"Міф: це неможливо."},
            "case": {"headline":"Судове рішення у справі","script":"Суд дійшов висновку."},
            "checklist": {"headline":"Перелік документів","script":"Які документи потрібно підготувати."},
            "comparison": {"headline":"Було / стало","script":"Було одне правило, стало інше."},
            "qa": {"headline":"Чи можна оскаржити рішення?","script":"Так, за визначених умов."},
            "risk": {"headline":"Ризик пропуску строку","script":"Не пропускайте строк."},
            "explainer": {"headline":"Правовий механізм","script":"Механізм складається з декількох елементів."},
        }
        layouts = {}
        for expected, values in jobs.items():
            scenes = _make_scenes({"format":"Reels", "content_id":expected, **values})
            content = next(scene for scene in scenes if scene["kind"] == "content")
            self.assertEqual(content["video_type"], expected)
            layouts[expected] = content["layout"]
        self.assertEqual(len(set(layouts.values())), len(layouts))

    def test_story_remains_single_story_scene(self):
        scenes = _make_scenes({"format":"Story","headline":"Пам'ятка","story_title":"Пам'ятка","script":"Збережіть документ."})
        self.assertEqual(len(scenes),1)
        self.assertEqual(scenes[0]["kind"],"story")

if __name__ == "__main__":
    unittest.main()
