import unittest
from renderer import _chunks, _wrap_words

class RendererTests(unittest.TestCase):
    def test_four_scene_structure(self):
        scenes = _chunks("Кабмін ухвалив зміни до порядку. Вони стосуються військовозобов'язаних.", "Зміни до порядку", "Збережіть матеріал")
        self.assertEqual(len(scenes), 4)
        self.assertIn("Зміни", scenes[0]["title"])

    def test_wrap_limits_lines(self):
        wrapped = _wrap_words("Це довге речення для перевірки коректного перенесення тексту на вертикальному відео", 18, 3)
        self.assertLessEqual(len(wrapped.splitlines()), 3)

    def test_duration_contract(self):
        from renderer import DURATION_MIN, DURATION_MAX
        self.assertEqual((DURATION_MIN, DURATION_MAX), (10.0, 15.0))

if __name__ == "__main__":
    unittest.main()
