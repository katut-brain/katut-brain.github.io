#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""review_warn.py の回帰テスト。標準ライブラリの unittest のみ。

守りたいこと: どの順で呼んでも（finish の後・同じ日の再実行）、計測コメントは
`</footer>` の直前にちょうど1本のまま、同種の警告も1本のまま。
"""

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import merge_review  # noqa: E402
import reel_health  # noqa: E402
import review_warn  # noqa: E402
import run_timing  # noqa: E402

C1 = "<!-- run-timing schema=v2 a=1 -->"
C2 = "<!-- run-timing schema=v2 a=2 -->"
STATUS_RE = re.compile(
    r"^WARN_STATUS: kind=(\w+) result=(written|replaced|removed|unchanged|skipped|error) reason=\S+$",
    re.M)


def doc(eol="\n", comment=C1, extra_head="", before_footer=""):
    parts = ["<!doctype html>", "<html><head>" + extra_head + "</head><body>",
             '<div class="vcard">card</div>', "<footer>毎朝の同期で自動生成"]
    if before_footer:
        parts.append(before_footer)
    if comment:
        parts.append(comment)
    parts += ["</footer>", "</body></html>", ""]
    return eol.join(parts)


def adjacent_comments(html):
    """`</footer>` の直前に連なる計測コメントの本数（insert_comment と同じ剥がし方）。"""
    return run_timing.insert_comment(html, C2)[1]


def warns(html, kind):
    return [m.group(1) for m in review_warn.WARN_RE.finditer(html)
            if review_warn._marker_matches(m.group(1), kind)]


class ApplyTest(unittest.TestCase):
    def test_written_before_timing_comment_and_finish_sees_one(self):
        html = doc()
        out, result, _ = review_warn.apply_warning(html, "merge_skipped")
        self.assertEqual(result, "written")
        self.assertLess(out.index("notegen-warn"), out.index("<!-- run-timing"))
        self.assertEqual(adjacent_comments(out), 1)
        again, existing = run_timing.insert_comment(out, C2)
        self.assertEqual(existing, 1)
        self.assertEqual(again.count("<!-- run-timing"), 1)

    def test_written_without_comment_goes_before_footer(self):
        out, result, _ = review_warn.apply_warning(doc(comment=""), "merge_skipped")
        self.assertEqual(result, "written")
        self.assertRegex(out, r'</p>\n</footer>')

    def test_same_day_rerun_merge_warn_finish(self):
        # merge（既存の警告を保つ）→ review_warn → finish を2周しても1本ずつ
        html = doc()
        for _ in range(2):
            html, _, _ = review_warn.apply_warning(html, "merge_skipped")
            html, _ = run_timing.insert_comment(html, C2)
            html, _, _ = review_warn.apply_warning(html, "note_verify", rids=["1"])
        self.assertEqual(html.count("<!-- run-timing"), 1)
        self.assertEqual(len(warns(html, "merge_skipped")), 1)
        self.assertEqual(len(warns(html, "note_verify")), 1)
        self.assertEqual(adjacent_comments(html), 1)

    def test_reel_replace_and_remove(self):
        old = '<p class="notegen-warn">⚠️ Instagram Reel の動画取得が全滅（3件中0件成功・内訳: x 3件）。古い</p>'
        html = doc(before_footer=old)
        text = "⚠️ Instagram Reel の動画取得が全滅（4件中0件成功・内訳: x 4件）。新しい"
        out, result, _ = review_warn.apply_warning(html, "reel", text)
        self.assertEqual(result, "replaced")
        self.assertEqual(warns(out, "reel"), [text])
        out2, result2, _ = review_warn.apply_warning(out, "reel", text)
        self.assertEqual((result2, out2), ("unchanged", out))
        out3, result3, _ = review_warn.apply_warning(out, "reel", None)
        self.assertEqual(result3, "removed")
        self.assertEqual(warns(out3, "reel"), [])
        self.assertEqual(out3, doc())  # 行ごと消え、計測コメントは無傷
        self.assertEqual(review_warn.apply_warning(out3, "reel", None)[1], "unchanged")

    def test_reel_duplicates_collapse_to_one(self):
        w = '<p class="notegen-warn">⚠️ Instagram Reel の動画取得が全滅（1件中0件成功・内訳: x 1件）。a</p>'
        html = doc(before_footer=w + "\n" + w)
        out, result, _ = review_warn.apply_warning(html, "reel", "⚠️ Instagram Reel の動画取得が全滅（2件中0件成功・内訳: x 2件）。b")
        self.assertEqual(result, "replaced")
        self.assertEqual(len(warns(out, "reel")), 1)

    def test_other_kinds_untouched_by_reel_remove(self):
        other = '<p class="notegen-warn">⚠️ 取り込み不完全: Raindrop取得エラー 1件</p>'
        html = doc(before_footer=other)
        out, result, _ = review_warn.apply_warning(html, "reel", None)
        self.assertEqual((result, out), ("unchanged", html))

    def test_note_verify_sums_rids(self):
        html, r1, _ = review_warn.apply_warning(doc(), "note_verify", rids=["1", "2"])
        self.assertEqual(r1, "written")
        out, r2, _ = review_warn.apply_warning(html, "note_verify", rids=["2", "3"])
        self.assertEqual(r2, "replaced")
        self.assertEqual(warns(out, "note_verify"), ["⚠️ ノート生成: 検証失敗 3件（rid: 1, 2, 3）"])
        out2, r3, _ = review_warn.apply_warning(out, "note_verify", rids=["3"])
        self.assertEqual((r3, out2), ("unchanged", out))

    def test_merge_skipped_not_duplicated(self):
        a, _, _ = review_warn.apply_warning(doc(), "merge_skipped")
        b, result, _ = review_warn.apply_warning(a, "merge_skipped")
        self.assertEqual((result, b), ("unchanged", a))

    def test_crlf_preserved(self):
        html = doc(eol="\r\n")
        out, _, _ = review_warn.apply_warning(html, "merge_skipped")
        self.assertEqual(out.count("\n"), out.count("\r\n"))
        self.assertEqual(adjacent_comments(out), 1)
        gone, result, _ = review_warn.apply_warning(
            review_warn.apply_warning(out, "reel", "⚠️ Instagram Reel の動画取得が全滅（1件中0件成功・内訳: x 1件）。z")[0],
            "reel", None)
        self.assertEqual(result, "removed")
        self.assertEqual(gone, out)

    def test_two_footers_skipped(self):
        html = doc() + "<script>var f='</footer>';</script>\n"
        out, result, reason = review_warn.apply_warning(html, "merge_skipped")
        self.assertEqual((out, result, reason), (None, "skipped", "footer_not_unique"))

    def test_script_strings_untouched(self):
        script = ('<script>var a="<!-- run-timing schema=v2 z=9 -->";'
                  'var b="<p class=\\"notegen-warn\\">x</p>";</script>')
        html = doc(extra_head=script)
        out, result, _ = review_warn.apply_warning(html, "merge_skipped")
        self.assertEqual(result, "written")
        self.assertIn(script, out)
        self.assertEqual(out.count("<!-- run-timing"), 2)
        self.assertEqual(adjacent_comments(out), 1)


class MisplacedTest(unittest.TestCase):
    """旧型の事故（警告が計測コメントの後ろに挟まった）は、呼ぶと正規位置へ直る。"""
    REEL = '<p class="notegen-warn">⚠️ Instagram Reel の動画取得が全滅（3件中0件成功・内訳: x 3件）。古い</p>'
    NEW = "⚠️ Instagram Reel の動画取得が全滅（3件中0件成功・内訳: x 3件）。古い"

    def broken(self, eol="\n"):
        return doc(eol=eol, comment=C1 + eol + self.REEL)

    def test_same_text_is_moved_not_left(self):
        self.assertEqual(adjacent_comments(self.broken()), 0)
        out, result, reason = review_warn.apply_warning(self.broken(), "reel", self.NEW)
        self.assertEqual((result, reason), ("replaced", "moved_before_timing_comment"))
        self.assertEqual(adjacent_comments(out), 1)
        self.assertEqual(len(warns(out, "reel")), 1)
        self.assertLess(out.index("notegen-warn"), out.index("<!-- run-timing"))

    def test_other_kind_call_also_repairs_and_crlf(self):
        out, result, _ = review_warn.apply_warning(self.broken("\r\n"), "merge_skipped")
        self.assertEqual(adjacent_comments(out), 1)
        self.assertEqual(len(warns(out, "reel")), 1)
        self.assertEqual(len(warns(out, "merge_skipped")), 1)
        self.assertNotIn("\n", out.replace("\r\n", ""))

    def test_remove_when_warn_no(self):
        out, result, _ = review_warn.apply_warning(self.broken(), "reel", None)
        self.assertEqual(result, "removed")
        self.assertEqual(out, doc())

    def test_warning_text_outside_footer_is_ignored(self):
        # カード本文にある同形の行は、既存警告と見なして消したり置き換えたりしない
        card = self.REEL
        html = doc().replace('<div class="vcard">card</div>', '<div class="vcard">' + card + '</div>')
        out, result, _ = review_warn.apply_warning(html, "reel", None)
        self.assertEqual((result, out), ("unchanged", html))
        out, result, _ = review_warn.apply_warning(html, "reel", self.NEW + "x")
        self.assertEqual(result, "written")
        self.assertIn(card, out)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "2026-11-01.html")
        self.facts = os.path.join(self.dir, "facts")
        os.makedirs(self.facts)

    def run_main(self, *argv, html=None):
        if html is not None:
            with io.open(self.path, "w", encoding="utf-8", newline="") as f:
                f.write(html)
        buf = io.StringIO()
        with mock.patch.object(reel_health, "FACTS_DIR", self.facts), \
                contextlib.redirect_stdout(buf):
            code = review_warn.main(["--target", "2026-11-01", "--reviews", self.path] + list(argv))
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertEqual(len(STATUS_RE.findall(out)), 1, out)
        return out

    def read(self):
        with io.open(self.path, encoding="utf-8", newline="") as f:
            return f.read()

    def facts_with(self, *reasons):
        recs = {"u%d" % i: {"route": "instagram", "has_video": True,
                            "video_understood": False, "video_reason": r}
                for i, r in enumerate(reasons)}
        with io.open(os.path.join(self.facts, "2026-11-01.json"), "w", encoding="utf-8") as f:
            json.dump(recs, f)

    def test_reel_flow_written_replaced_removed(self):
        self.facts_with("bad_content_type", "bad_content_type")
        out = self.run_main("--kind", "reel", html=doc())
        self.assertIn("REEL_VIDEO_STATUS: total=2 ok=0 reasons={'bad_content_type': 2} warn=yes", out)
        self.assertIn("result=written", out)
        self.assertIn("（2件中0件成功・内訳: bad_content_type 2件）", self.read())
        self.facts_with("bad_content_type", "bad_content_type", "exception")
        self.assertIn("result=replaced", self.run_main("--kind", "reel"))
        self.assertEqual(len(warns(self.read(), "reel")), 1)
        self.assertIn("3件中0件成功", self.read())
        self.assertIn("result=unchanged", self.run_main("--kind", "reel"))
        # 判定材料が無い夜（facts 無し）は既存の警告を消さない
        with_warn = self.read()
        os.remove(os.path.join(self.facts, "2026-11-01.json"))
        out = self.run_main("--kind", "reel")
        self.assertIn("warn=no", out)
        self.assertIn("result=unchanged reason=no_facts_keep_existing", out)
        self.assertEqual(self.read(), with_warn)
        # facts を読めて ok>=1 になったら消える
        with io.open(os.path.join(self.facts, "2026-11-01.json"), "w", encoding="utf-8") as f:
            json.dump({"u": {"route": "instagram", "has_video": True, "video_understood": True}}, f)
        out = self.run_main("--kind", "reel")
        self.assertIn("result=removed", out)
        self.assertEqual(self.read(), doc())

    def test_every_branch_prints_status(self):
        self.assertIn("result=skipped reason=no_review_file", self.run_main("--kind", "merge_skipped"))
        self.assertIn("result=skipped reason=footer_not_unique",
                      self.run_main("--kind", "merge_skipped", html=doc() + "</footer>"))
        self.assertIn("result=error reason=invalid_rids", self.run_main("--kind", "note_verify", html=doc()))
        with mock.patch.object(review_warn.io, "open", side_effect=OSError("boom")):
            self.assertIn("result=error reason=OSError:boom", self.run_main("--kind", "merge_skipped"))

    def test_argument_errors_still_status_and_exit0(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = review_warn.main(["--kind", "bogus"])
        self.assertEqual(code, 0)
        self.assertRegex(buf.getvalue(), STATUS_RE)

    def test_rids_validation_rejects_without_writing(self):
        original = doc()
        for bad in ("", "1,,2", "1, 2", "abc", "1;2", "<b>", "1,2,", "-1", "１２"):
            out = self.run_main("--kind", "note_verify", "--rids", bad, html=original)
            self.assertIn("result=error reason=invalid_rids", out, bad)
            self.assertEqual(self.read(), original, bad)
        self.assertIn("result=written", self.run_main("--kind", "note_verify", "--rids", "1234,5678"))
        self.assertIn("（rid: 1234, 5678）", self.read())

    def test_no_tmp_file_left(self):
        self.run_main("--kind", "merge_skipped", html=doc())
        self.assertEqual(sorted(os.listdir(self.dir)), ["2026-11-01.html", "facts"])


class RealReviewsTest(unittest.TestCase):
    def test_real_reviews_keep_comment_invariant(self):
        """本番の reviews 全件に警告を足しても書き込み可能で、直前の計測コメントの本数が変わらない
        （全件でちょうど1本という不変条件そのものは test_run_timing 側が検査する）。"""
        reviews_dir = os.path.join(HERE, "reviews")
        for name in sorted(os.listdir(reviews_dir)):
            if not name.endswith(".html"):
                continue
            with self.subTest(name):
                with io.open(os.path.join(reviews_dir, name), encoding="utf-8", newline="") as f:
                    html = f.read()
                out, result, reason = review_warn.apply_warning(html, "merge_skipped")
                self.assertIsNotNone(out, reason)
                self.assertIn(result, ("written", "unchanged", "replaced"))
                self.assertEqual(adjacent_comments(out), adjacent_comments(html))
                self.assertEqual(len(warns(out, "merge_skipped")), 1)


if __name__ == "__main__":
    unittest.main()
