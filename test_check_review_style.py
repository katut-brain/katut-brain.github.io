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
import unittest.mock
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
        self.assertTrue(check_review_style._same(self.style_inner(fixed), _css()))
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
        self.assertTrue(check_review_style._same(self.style_inner(fixed), _css()))

    def test_missing_style_without_fix(self):
        self.write(_page(""))
        rc, line = self.run_main()
        self.assertEqual((rc, line), (1, "STYLE_CHECK: date=%s status=mismatch reason=no_style" % DATE))

    def test_no_head_but_body_inserts_before_body(self):
        page = "<!doctype html><html><body>x</body></html>\n"
        self.write(page)
        rc, line = self.run_main("--fix")
        self.assertEqual(line, "STYLE_CHECK: date=%s status=fixed reason=inserted" % DATE)
        fixed = self.read()
        self.assertLess(fixed.index("<style>"), fixed.index("<body>"))
        self.assertTrue(fixed.endswith("<body>x</body></html>\n"))

    def test_no_style_no_head_no_body_is_error_and_untouched(self):
        page = "<!doctype html><html>x</html>\n"
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

    def _real_old_css(self, date):
        """リポジトリに実在する古い世代のページから CSS を取り出す。"""
        with io.open(os.path.join(REPO_DIR, "reviews", date + ".html"), encoding="utf-8", newline="") as fh:
            t = fh.read()
        found, _, _ = check_review_style._locate(t)
        return t[found[1]:found[2]]

    def test_before_canonical_old_generations_are_never_written(self):
        # 2026-09-22 より前の、実在する2世代の CSS なら作り直しのランでも書き換えない
        old = "2026-09-21"
        path = os.path.join(self.reviews, old + ".html")
        for src in ("2026-06-12", "2026-09-21"):
            css = self._real_old_css(src)
            self.assertIn(check_review_style._fingerprint(css), check_review_style.KNOWN_OLD_GENERATIONS)
            page = _page("<style>%s</style>" % css)
            with io.open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(page)
            for extra in (["--fix"], []):
                rc, line = self.run_main(*extra, date=old)
                self.assertEqual(line, "STYLE_CHECK: date=%s status=mismatch reason=before_canonical" % old)
                self.assertEqual(rc, 0 if extra else 1)
                with io.open(path, encoding="utf-8", newline="") as fh:
                    self.assertEqual(fh.read(), page)

    def test_before_canonical_unknown_css_is_fixed(self):
        # 線より前の日付でも、古い2世代のどちらでもない CSS（その夜に新しく作ったページ等）は揃える
        old = "2026-09-20"
        path = os.path.join(self.reviews, old + ".html")
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(_page("<style>.vcard { display: flex; gap: 14px; }</style>"))
        rc, line = self.run_main("--fix", date=old)
        self.assertEqual(line, "STYLE_CHECK: date=%s status=fixed reason=replaced" % old)

    def test_extra_style_in_head_is_reported_not_written(self):
        page = _page("<style>\n%s</style>\n<style>:root { --bg: transparent; }</style>" % _css())
        self.write(page)
        for extra in (["--fix"], []):
            rc, line = self.run_main(*extra)
            self.assertEqual(line, "STYLE_CHECK: date=%s status=mismatch reason=extra_style:2" % DATE)
            self.assertEqual(self.read(), page)

    def test_nbsp_at_line_start_is_not_ignored(self):
        # CSS では NBSP は識別子の文字。行頭の NBSP で規則が効かなくなるので ok にしない
        css = _css().replace(".wrap {", " .wrap {", 1)
        self.assertNotEqual(css, _css())
        self.write(_page("<style>\n%s</style>" % css))
        rc, line = self.run_main()
        self.assertEqual(rc, 1)

    def test_style_with_type_attribute_is_ok(self):
        page = _page('<style type="text/css">\n%s</style>' % _css())
        self.write(page)
        rc, line = self.run_main("--fix")
        self.assertEqual(line, "STYLE_CHECK: date=%s status=ok" % DATE)
        self.assertEqual(self.read(), page)

    def test_post_write_verification_blocks_a_bad_result(self):
        # 置き換えた結果を読み直して、正規の <style> が head にちょうど1つ無ければ書かない
        page = _page("<style>broken</style>")
        self.write(page)
        for bad in ("<style>still broken</style>",
                    "<style>\n%s</style><style>.x{}</style>" % _css(),
                    "<!-- -->"):
            with unittest.mock.patch.object(check_review_style, "_element", lambda css, bad=bad: bad):
                rc, line = self.run_main("--fix")
            self.assertEqual((rc, line), (0, "STYLE_CHECK: date=%s status=error reason=verify_failed" % DATE), bad)
            self.assertEqual(self.read(), page)

    def test_os_replace_failure_keeps_file_and_leaves_no_temp(self):
        # _write_atomic 自体は差し替えず、置き換えの瞬間だけ失敗させる
        page = _page("<style>broken</style>")
        self.write(page)
        with unittest.mock.patch.object(check_review_style.os, "replace", side_effect=OSError("busy")):
            rc, line = self.run_main("--fix")
        self.assertEqual((rc, line), (0, "STYLE_CHECK: date=%s status=error reason=OSError" % DATE))
        self.assertEqual(self.read(), page)
        self.assertEqual(os.listdir(self.reviews), [DATE + ".html"])

    def test_before_canonical_but_broken_is_fixed(self):
        # 線より前でも、CSS が無い・地の文や HTML が混入しているなら直す（Codex 指摘 P1）
        old = "2026-09-21"
        path = os.path.join(self.reviews, old + ".html")
        bad = re.search(r"<style>.*?</style>", _old_prompt_excerpt(), re.S).group(0)
        for page, reason in ((_page(""), "inserted"), (_page(bad), "replaced"),
                             (_page("<style>   </style>"), "replaced")):
            with io.open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(page)
            rc, line = self.run_main("--fix", date=old)
            self.assertEqual(line, "STYLE_CHECK: date=%s status=fixed reason=%s" % (old, reason))
            with io.open(path, encoding="utf-8", newline="") as fh:
                self.assertTrue(check_review_style._same(self.style_inner(fh.read()), _css()))

    # --- HTML として読む（コメント・script・属性を本物の要素と取り違えない。Codex 指摘 P0/P1） ---

    def test_canonical_inside_comment_does_not_hide_broken_style(self):
        page = _page("<!-- <style>\n%s</style> -->\n<style>broken</style>" % _css())
        self.write(page)
        rc, line = self.run_main()
        self.assertEqual((rc, line), (1, "STYLE_CHECK: date=%s status=mismatch" % DATE))
        self.run_main("--fix")
        fixed = self.read()
        self.assertNotIn("broken", fixed)
        self.assertIn("<!-- <style>\n%s</style> -->" % _css(), fixed)  # コメントは不変

    def test_canonical_inside_script_does_not_hide_broken_style(self):
        js = '<script>var s = "<style>%s</style>";</script>' % _css().replace("\n", " ")
        page = _page(js + "\n<style>broken</style>")
        self.write(page)
        rc, line = self.run_main()
        self.assertEqual(rc, 1)
        self.run_main("--fix")
        fixed = self.read()
        self.assertIn(js, fixed)
        self.assertNotIn("broken", fixed)

    def test_broken_string_in_script_is_not_touched_when_real_style_is_canonical(self):
        js = '<script>var s = "<style>broken</style>";</script>'
        page = _page(js + "\n<style>\n%s</style>" % _css())
        self.write(page)
        rc, line = self.run_main("--fix")
        self.assertEqual(line, "STYLE_CHECK: date=%s status=ok" % DATE)
        self.assertEqual(self.read(), page)

    def test_head_end_inside_script_is_not_the_insert_point(self):
        js = '<script>var marker = "</head>";</script>'
        page = _page(js)
        self.write(page)
        rc, line = self.run_main("--fix")
        self.assertEqual(line, "STYLE_CHECK: date=%s status=fixed reason=inserted" % DATE)
        fixed = self.read()
        self.assertIn(js, fixed)  # script は不変
        real_head_end = fixed.index("</head>", fixed.index(js) + len(js))
        self.assertLess(fixed.index("<style>\n:root"), real_head_end)
        self.assertGreater(fixed.index("<style>\n:root"), fixed.index(js))

    def test_attribute_with_gt_and_uppercase_tag(self):
        page = _page('<STYLE data-note="a > b">\n%s</STYLE>' % _css())
        self.write(page)
        rc, line = self.run_main()
        self.assertEqual((rc, line), (0, "STYLE_CHECK: date=%s status=ok" % DATE))
        page = _page('<STYLE data-note="a > b">broken</STYLE>')
        self.write(page)
        self.run_main("--fix")
        fixed = self.read()
        self.assertNotIn("broken", fixed)
        self.assertNotIn("data-note", fixed)  # 素の <style> にする

    def test_style_only_in_body_counts_as_missing_in_head(self):
        page = _page("", body="<style>.x{}</style><p>本文</p>")
        self.write(page)
        rc, line = self.run_main("--fix")
        self.assertEqual(line, "STYLE_CHECK: date=%s status=fixed reason=inserted" % DATE)
        fixed = self.read()
        self.assertLess(fixed.index("<style>\n:root"), fixed.index("</head>"))
        self.assertIn("<style>.x{}</style><p>本文</p>", fixed)

    def test_unclosed_style_is_error_and_untouched(self):
        page = '<!doctype html><html><head><style>:root{}\n</head><body><div class="vcard">x</div></body></html>\n'
        self.write(page)
        rc, line = self.run_main("--fix")
        self.assertEqual((rc, line), (0, "STYLE_CHECK: date=%s status=error reason=unclosed_style" % DATE))
        self.assertEqual(self.read(), page)

    def test_space_inside_a_value_is_compared(self):
        # 空白を無視するのは行の前後と空行だけ。行の中の空白は CSS の値になりうるので比べる
        css = _css().replace("font-size: 15px;", "font-size:  15px;", 1)
        self.assertNotEqual(css, _css())
        self.write(_page("<style>\n%s</style>" % css))
        rc, line = self.run_main()
        self.assertEqual(rc, 1)

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


class TestGateAll(_Dir):
    """公開ゲート（build-feed.yml）用の --all --github。書かない・警告だけ。"""

    def _put(self, date, page):
        with io.open(os.path.join(self.reviews, date + ".html"), "w", encoding="utf-8", newline="") as fh:
            fh.write(page)
        return page

    def _run_all(self, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = check_review_style.main(["--all", "--github", "--reviews-dir", self.reviews] + list(extra))
        return rc, buf.getvalue().splitlines()

    def test_warns_only_for_broken_days_and_never_writes(self):
        good = self._put("2026-09-30", _page("<style>\n%s</style>" % _css()))
        broken = self._put("2026-09-29", _page("<style>broken</style>"))
        old = self._put("2026-06-01", _page("<style>.vcard{}</style>"))  # since より前は見ない
        rc, lines = self._run_all()
        self.assertEqual(rc, 1)
        warns = [l for l in lines if l.startswith("::warning")]
        self.assertEqual(warns, ["::warning title=review-style::STYLE_CHECK: date=2026-09-29 status=mismatch"])
        self.assertIn("STYLE_CHECK_SUMMARY: since=2026-09-22 warnings=1", lines)
        self.assertFalse(any("2026-06-01" in l for l in lines))
        for d, page in (("2026-09-30", good), ("2026-09-29", broken), ("2026-06-01", old)):
            with io.open(os.path.join(self.reviews, d + ".html"), encoding="utf-8", newline="") as fh:
                self.assertEqual(fh.read(), page)

    def test_all_ok_is_exit_0_without_warning(self):
        self._put("2026-09-30", _page("<style>\n%s</style>" % _css()))
        rc, lines = self._run_all()
        self.assertEqual(rc, 0)
        self.assertFalse(any(l.startswith("::warning") for l in lines))

    def test_since_and_old_generation(self):
        # --since を下げても、古い2世代の CSS は警告しない（before_canonical）
        # 06-12 の世代は補修用の2つ目の <style> を元から持つが、それでも警告しない
        for d in ("2026-06-12", "2026-09-21"):
            with io.open(os.path.join(REPO_DIR, "reviews", d + ".html"), encoding="utf-8", newline="") as fh:
                self._put(d, fh.read())
        rc, lines = self._run_all("--since", "2026-06-01")
        self.assertEqual(rc, 0, lines)
        for d in ("2026-06-12", "2026-09-21"):
            self.assertIn("STYLE_CHECK: date=%s status=mismatch reason=before_canonical" % d, lines)

    def test_all_with_fix_or_date_is_rejected(self):
        for extra in (["--fix"], ["--date", "2026-09-29"], ["--since", "bad"]):
            rc, lines = self._run_all(*extra)
            self.assertEqual(rc, 1, extra)
            self.assertEqual(lines, ["STYLE_CHECK: date=- status=error reason=bad_args"])

    def test_build_feed_runs_the_gate_as_warning_only(self):
        with io.open(os.path.join(REPO_DIR, ".github", "workflows", "build-feed.yml"), encoding="utf-8") as fh:
            yml = fh.read()
        m = re.search(r"- name: Check review CSS\n\s+continue-on-error: true\n\s+run: (.+)\n", yml)
        self.assertIsNotNone(m, "build-feed.yml に CSS 検査の警告ステップが無い")
        self.assertIn("check_review_style.py --all --github", m.group(1))
        self.assertNotIn("--fix", m.group(1))


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

    def test_prompt_has_no_extractable_style_pair(self):
        # 2026-09-29 と同じ抜き出し方を今の手順書に当てても、何も取れないこと
        self.assertIsNone(re.search(r"<style\b[^>]*>.*?</style", self.text, re.S | re.I))

    def test_step5_points_to_css_file(self):
        self.assertIn("review_style.css", self._step("5.", "6."))

    def test_step7_runs_fix(self):
        step7 = self._step("7.", "7.5.")
        cmds = [l.strip() for l in step7.splitlines() if "check_review_style.py" in l and "--date" in l]
        self.assertTrue(any("--fix" in c for c in cmds), cmds)


if __name__ == "__main__":
    unittest.main()
