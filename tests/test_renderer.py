import unittest
from renderer import (
    _citation, _extract_script_text, _fit, _find_font, _make_scenes, _scene_durations,
    _split_script, _scene_label
)

class MotionEngineV3Tests(unittest.TestCase):
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

    def test_story_remains_single_story_scene(self):
        scenes = _make_scenes({"format":"Story","headline":"Пам'ятка","story_title":"Пам'ятка","script":"Збережіть документ."})
        self.assertEqual(len(scenes),1)
        self.assertEqual(scenes[0]["kind"],"story")

if __name__ == "__main__":
    unittest.main()
