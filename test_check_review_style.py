#!/usr/bin/env python3
# test_check_review_style.py — check_review_style.py（手順7・reviews の CSS を正規の中身に揃える）
# の回帰テスト。一時ディレクトリの reviews/ に本番と同じ形の HTML を置き、本番の main() を通す
# （モックで差し替えない）。2026-09-29 の破損は、当夜と同じ抜き出し方を手順書の旧版の形に
# 当てて再現する。標準ライブラリの unittest のみ。

import io
import os
import re
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import check_review_style  # noqa: E402

DATE = "2026-09-29"


def _css():
    with io.open(os.path.join(REPO_DIR, "review_style.css"), encoding="utf-8", newline="") as fh:
        return fh.read()


def _page(style_element, body="<div class=\"wrap\"><div class=\"meta\">テーマ</div></div>"):
    return ('<!doctype html>\n<html lang="ja"><head><meta charset="utf-8">\n'
            '<title>%s の振り返り</title>\n%s\n</head><body>%s\n'
            '<script>function openChat(btn){}</script>\n</body></html>\n' % (DATE, style_element, body))


def _old_prompt_excerpt():
    """手順書の旧版（2026-10-02 まで）の手順5の形: 地の文に `<style>` があり、その後に本物のブロック。"""
    block = "\n".join("       " + l if l else "" for l in _css().rstrip("\n").split("\n"))
    return ("   - **スタイル**：以下の `<style>` ブロックをそのまま使う（CSS変数・ダーク対応込み）。"
            "`<title><TARGET> の振り返り</title>`。\n     ```html\n     <style>\n"
            + block + "\n     </style>\n     ```\n")


class _Dir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.reviews = os.path.join(self._tmp.name, "reviews")
        os.makedirs(self.reviews)
        self.path = os.path.join(self.reviews, DATE + ".html")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text):
        with io.open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def read(self):
        with io.open(self.path, encoding="utf-8", newline="") as fh:
            return fh.read()

    def run_main(self, *extra, date=DATE):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = check_review_style.main(["--date", date, "--reviews-dir", self.reviews] + list(extra))
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("STYLE_CHECK: "), lines)
        return rc, lines[0]

    def style_inner(self, text):
        return re.search(r"<style\b[^>]*>(.*?)</style>", text, re.S).group(1)


class TestCheckReviewStyle(_Dir):
    def test_reproduce_2026_09_29_and_fix(self):
        # 当夜の抜き出し方をそのまま当てる
        bad = re.search(r"<style>.*?</style>", _old_prompt_excerpt(), re.S).group(0)
        self.assertTrue(bad.startswith("<style>` ブロック"))  # 再現できている
        page = _page(bad)
        self.write(page)
        rc, line = self.run_main()  # --fix なし
        self.assertEqual((rc, line), (1, "STYLE_CHECK: date=%s status=mismatch" % DATE))
        self.assertEqual(self.read(), page)  # --fix なしでは書かない
        rc, line = self.run_main("--fix")
        self.assertEqual((rc, line), (0, "STYLE_CHECK: date=%s status=fixed reason=replaced" % DATE))
        fixed = self.read()
        self.assertTrue(self.style_inner(fixed).lstrip().startswith(":root {"))
        self.assertNotIn("`", self.style_inner(fixed))
        self.assertEqual(check_review_style._norm(self.style_inner(fixed)), check_review_style._norm(_css()))
        # <style> 要素の外は1バイトも変えない
        m_old = re.search(r"<style>.*?</style>", page, re.S)
        m_new = re.search(r"<style>.*?</style>", fixed, re.S)
        self.assertEqual(page[:m_old.start()], fixed[:m_new.start()])
        self.assertEqual(page[m_old.end():], fixed[m_new.end():])

    def test_fix_is_idempotent(self):
        self.write(_page("<style>body{}</style>"))
        self.run_main("--fix")
        once = self.read()
        rc, line = self.run_main("--fix")
        self.assertEqual(line, "STYLE_CHECK: date=%s status=ok" % DATE)
        self.assertEqual(self.read(), once)

    def test_canonical_with_other_indentation_is_ok(self):
        indented = "\n".join("    " + l for l in _css().split("\n"))
        page = _page("<style>\n%s\n  </style>" % indented)
        self.write(page)
        rc, line = self.run_main("--fix")
        self.assertEqual(line, "STYLE_CHECK: date=%s status=ok" % DATE)
        self.assertEqual(self.read(), page)  # ok のときは書かない

    def test_one_value_changed_is_mismatch(self):
        css = _css().replace("--accent: #0071e3", "--accent: #0071e4", 1)
        self.assertNotEqual(css, _css())
        self.write(_page("<style>\n%s</style>" % css))
        rc, line = self.run_main()
        self.assertEqual((rc, line), (1, "STYLE_CHECK: date=%s status=mismatch" % DATE))

    def test_missing_style_is_inserted_before_head_end(self):
        page = _page("")
        self.write(page)
        rc, line = self.run_main("--fix")
        self.assertEqual(line, "STYLE_CHECK: date=%s status=fixed reason=inserted" % DATE)
        fixed = self.read()
        self.assertLess(fixed.index("<style>"), fixed.index("</head>"))
        self.assertEqual(check_review_style._norm(self.style_inner(fixed)), check_review_style._norm(_css()))

    def test_missing_style_without_fix(self):
        self.write(_page(""))
        rc, line = self.run_main()
        self.assertEqual((rc, line), (1, "STYLE_CHECK: date=%s status=mismatch reason=no_style" % DATE))

    def test_no_style_no_head_is_error_and_untouched(self):
        page = "<!doctype html><html><body>x</body></html>\n"
        self.write(page)
        rc, line = self.run_main("--fix")
        self.assertEqual((rc, line), (0, "STYLE_CHECK: date=%s status=error reason=no_style_no_head" % DATE))
        self.assertEqual(self.read(), page)

    def test_only_first_style_element_is_replaced(self):
        page = _page("<style>broken</style>", body="<style>.later{}</style><p>本文</p>")
        self.write(page)
        self.run_main("--fix")
        fixed = self.read()
        self.assertIn("<style>.later{}</style><p>本文</p>", fixed)
        self.assertNotIn("broken", fixed)

    def test_crlf_outside_style_is_preserved(self):
        page = _page("<style>broken</style>").replace("\n", "\r\n")
        self.write(page)
        self.run_main("--fix")
        fixed = self.read()
        tail = page[page.index("</style>") + len("</style>"):]
        self.assertTrue(fixed.endswith(tail))
        self.assertIn("\r\n", fixed)

    def test_before_canonical_is_never_written(self):
        # 2026-09-22 より前は古いテンプレートの世代。作り直しのランでも書き換えない
        old = "2026-09-21"
        path = os.path.join(self.reviews, old + ".html")
        for page in (_page("<style>.vcard { display: flex; gap: 14px; }</style>"), _page("")):
            with io.open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(page)
            for extra in (["--fix"], []):
                rc, line = self.run_main(*extra, date=old)
                self.assertEqual(line, "STYLE_CHECK: date=%s status=mismatch reason=before_canonical" % old)
                self.assertEqual(rc, 0 if extra else 1)
                with io.open(path, encoding="utf-8", newline="") as fh:
                    self.assertEqual(fh.read(), page)

    def test_canonical_since_boundary_is_fixed(self):
        d = check_review_style.CANONICAL_SINCE
        path = os.path.join(self.reviews, d + ".html")
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(_page("<style>broken</style>"))
        rc, line = self.run_main("--fix", date=d)
        self.assertEqual(line, "STYLE_CHECK: date=%s status=fixed reason=replaced" % d)

    def test_before_canonical_but_already_canonical_is_ok(self):
        old = "2026-09-01"
        with io.open(os.path.join(self.reviews, old + ".html"), "w", encoding="utf-8", newline="") as fh:
            fh.write(_page("<style>\n%s</style>" % _css()))
        rc, line = self.run_main(date=old)
        self.assertEqual((rc, line), (0, "STYLE_CHECK: date=%s status=ok" % old))

    def test_no_review(self):
        rc, line = self.run_main("--fix")
        self.assertEqual((rc, line), (0, "STYLE_CHECK: date=%s status=no_review" % DATE))
        rc, line = self.run_main()
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.path))

    def test_bad_date(self):
        for d in ("2026-9-29", "../x", "2026-09-29\n"):
            rc, line = self.run_main("--fix", date=d)
            self.assertEqual(rc, 0)
            self.assertIn("status=error reason=bad_date", line)

    def test_write_failure_reports_error_and_keeps_file(self):
        page = _page("<style>broken</style>")
        self.write(page)
        orig = check_review_style._write_atomic
        check_review_style._write_atomic = lambda p, t: (_ for _ in ()).throw(PermissionError("x"))
        try:
            rc, line = self.run_main("--fix")
        finally:
            check_review_style._write_atomic = orig
        self.assertEqual((rc, line), (0, "STYLE_CHECK: date=%s status=error reason=PermissionError" % DATE))
        self.assertEqual(self.read(), page)
        self.assertEqual(os.listdir(self.reviews), [DATE + ".html"])

    def test_no_temp_file_left_after_fix(self):
        self.write(_page("<style>broken</style>"))
        self.run_main("--fix")
        self.assertEqual(os.listdir(self.reviews), [DATE + ".html"])


class TestCanonicalCss(unittest.TestCase):
    def test_css_file_starts_with_light_root(self):
        css = _css()
        self.assertTrue(css.startswith(":root {"))
        self.assertIn("--bg: #ffffff", css)
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertNotIn("<", css)
        self.assertNotIn("`", css)


class TestRoutinePrompt(unittest.TestCase):
    """手順書（cloud_routine_prompt.md）が CSS を review_style.css に任せ、手順7で揃えること。"""

    def setUp(self):
        with io.open(os.path.join(REPO_DIR, "cloud_routine_prompt.md"), encoding="utf-8") as fh:
            self.text = fh.read()

    def _step(self, head, nxt):
        m = re.search(r"^%s .*?(?=^%s )" % (re.escape(head), re.escape(nxt)), self.text, re.S | re.M)
        self.assertIsNotNone(m, head)
        return m.group(0)

    def test_prompt_has_no_inline_css_block(self):
        # 手順書に CSS 本体が残っていると、また抜き出し方の即興が生まれる
        self.assertNotIn("--bg: #ffffff", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*<style>\s*$")

    def test_step5_points_to_css_file(self):
        self.assertIn("review_style.css", self._step("5.", "6."))

    def test_step7_runs_fix(self):
        step7 = self._step("7.", "7.5.")
        cmds = [l.strip() for l in step7.splitlines() if "check_review_style.py" in l and "--date" in l]
        self.assertTrue(any("--fix" in c for c in cmds), cmds)


if __name__ == "__main__":
    unittest.main()
