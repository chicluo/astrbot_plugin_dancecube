# -*- coding: utf-8 -*-
"""最小单测：只测纯函数，不联网、不依赖 AstrBot。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import net  # noqa: E402
from core.client import compute_rating, difficulty_name, grade_of  # noqa: E402
from core.music import MusicLib  # noqa: E402
from core.safe import safe_cell, safe_error  # noqa: E402


class TestSafeCell(unittest.TestCase):
    def test_formula_prefix(self):
        for v in ("=1+1", "+cmd", "-2+3", "@x"):
            self.assertEqual(safe_cell(v), "'" + v)

    def test_normal(self):
        self.assertEqual(safe_cell("abc"), "abc")
        self.assertEqual(safe_cell(5), 5)
        self.assertEqual(safe_cell(None), "")


class TestSafeError(unittest.TestCase):
    def test_path_masked(self):
        msg = safe_error(ValueError("E:/secret/token.json 打不开"))
        self.assertIn("[路径]", msg)
        self.assertNotIn("secret", msg)

    def test_token_masked(self):
        self.assertIn("[凭证]", safe_error(ValueError("access_token 过期")))


class TestHostAllowed(unittest.TestCase):
    def test_allowed(self):
        self.assertTrue(net.host_allowed("https://dancedemo.shenghuayule.com/x"))
        self.assertTrue(net.host_allowed("https://thirdwx.qlogo.cn/a"))

    def test_blocked(self):
        self.assertFalse(net.host_allowed("http://dancedemo.shenghuayule.com/x"))
        self.assertFalse(net.host_allowed("file:///C:/x"))
        self.assertFalse(net.host_allowed("https://127.0.0.1/x"))


class TestRating(unittest.TestCase):
    def test_ap(self):
        self.assertEqual(compute_rating(15, 100), 1700)

    def test_zero_level(self):
        self.assertEqual(compute_rating(0, 100), 0)

    def test_grade(self):
        self.assertEqual(grade_of(100.0, 0), "AP")
        self.assertEqual(grade_of(99.0, 0), "FC")
        self.assertEqual(grade_of(90.0, 3), "")


class TestDifficulty(unittest.TestCase):
    def test_classic(self):
        self.assertEqual(difficulty_name(14, 2), "[高]大师")

    def test_show_plus(self):
        self.assertEqual(difficulty_name(104, None), "[S+]大师")

    def test_show_only(self):
        self.assertEqual(difficulty_name(13, -1), "[秀]专家")


class TestSearch(unittest.TestCase):
    def test_exact_prefix_contains(self):
        lib = MusicLib.__new__(MusicLib)
        records = [
            {"name": "abc", "musicId": 1},
            {"name": "ab", "musicId": 2},
            {"name": "xabcx", "musicId": 3},
        ]
        out = lib._search(records, "ab")
        self.assertEqual([r["musicId"] for r in out], [2, 1, 3])


if __name__ == "__main__":
    unittest.main()
