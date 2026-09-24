#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_run_timing.py の回帰テスト（標準ライブラリ unittest のみ。他の
test_*.py と同じ流儀。pytest からも実行できる）。

ここでの閾値（total_s/saves > 180 かつ total_s > 1200）は異常検知のトリガー
であって性能SLAではない（20分基準そのものは2026-09-08に撤回済み。ここは
その撤回後に導入した「機械検知」側のテスト）。
"""

import contextlib
import glob
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import check_run_timing  # noqa: E402

FOOTER_HTML = (
    "<!doctype html>\n<html><body>\n"
    "<div class=\"meta\">テーマ</div>\n"
    "<footer>ふっだ</footer>\n</html>\n"
)


def _comment(**fields):
    parts = ["schema=v2"] + ["%s=%s" % (k, v) for k, v in fields.items()]
    return "<!-- run-timing %s -->" % " ".join(parts)


def _write_review(dirpath, date, comment_line):
    html_text, existing = check_run_timing.run_timing.insert_comment(
        FOOTER_HTML, comment_line
    ) if comment_line is not None else (FOOTER_HTML, 0)
    if comment_line is None:
        html_text = FOOTER_HTML
    with open(os.path.join(dirpath, "%s.html" % date), "w", encoding="utf-8") as fh:
        fh.write(html_text)


class ReadCommentTests(unittest.TestCase):
    def test_no_comment_is_out_of_scope(self):
        status, fields, reason = check_run_timing.read_run_timing_comment(FOOTER_HTML)
        self.assertEqual(status, "no_comment")

    def test_single_valid_comment(self):
        # run_timing.insert_comment は `</footer>`（閉じタグ）の直前に置くので、
        # テストもその位置に合わせる。
        html_text = FOOTER_HTML.replace(
            "</footer>", "\n" + _comment(
                status="complete", target="2026-09-18",
                run_id="3b462ccb", saves=4, total_s=371) + "\n</footer>"
        )
        status, fields, reason = check_run_timing.read_run_timing_comment(html_text)
        self.assertEqual(status, "ok")
        self.assertEqual(fields["status"], "complete")
        self.assertEqual(fields["saves"], "4")
        self.assertEqual(fields["total_s"], "371")

    def test_broken_comment_is_parse_error(self):
        # 閉じタグ `-->` が無い壊れたコメント。
        html_text = FOOTER_HTML.replace(
            "<footer>", "<!-- run-timing schema=v2 status=complete\n<footer>"
        )
        status, fields, reason = check_run_timing.read_run_timing_comment(html_text)
        self.assertEqual(status, "parse_error")
        self.assertEqual(reason, "malformed_comment")

    def test_multiple_comments_is_parse_error(self):
        one = _comment(status="complete", target="2026-09-18", run_id="3b462ccb",
                        saves=4, total_s=371)
        two = _comment(status="incomplete", target="2026-09-19", run_id="aaaaaaaa",
                        saves="unknown", reason="missing-step5")
        html_text = FOOTER_HTML.replace(
            "</footer>", "\n" + one + "\n" + two + "\n</footer>")
        status, fields, reason = check_run_timing.read_run_timing_comment(html_text)
        self.assertEqual(status, "parse_error")
        self.assertEqual(reason, "multiple_comments")

    def test_unsupported_schema_is_parse_error(self):
        html_text = FOOTER_HTML.replace(
            "</footer>",
            "\n<!-- run-timing schema=v1 step1=10 -->\n</footer>",
        )
        status, fields, reason = check_run_timing.read_run_timing_comment(html_text)
        self.assertEqual(status, "parse_error")
        self.assertEqual(reason, "unsupported_schema")

    def test_comment_outside_footer_position_is_not_picked_up(self):
        """P1-2: 本文（<script>）にある同形の文字列は、footer 直前でなければ
        拾わない。run_timing.py 自身が消してよいのを footer 直前だけに
        限定しているのと対称的に、読み取り側もその位置規約に合わせる。
        """
        js = ('<script>\nconst example = "<!-- run-timing schema=v2 '
              'status=complete -->";\n</script>\n')
        html_text = FOOTER_HTML.replace("<footer>", js + "<footer>")
        status, fields, reason = check_run_timing.read_run_timing_comment(html_text)
        self.assertEqual(status, "no_comment")

    def test_two_footers_is_no_comment_not_crash(self):
        """`</footer>` が複数あると run_timing.py 自身も書き込みを拒否する
        状態なので、位置を特定できないものとして no_comment 扱いにする
        （クラッシュしない・誤った値を読まないことを確認する）。
        """
        comment = _comment(status="complete", target="2026-09-18",
                            run_id="3b462ccb", saves=4, total_s=371)
        dirty = FOOTER_HTML.replace(
            "<footer>", comment + "\n<footer>"
        ).replace("</html>", "<script>var s = \"</footer>\";</script>\n</html>")
        status, fields, reason = check_run_timing.read_run_timing_comment(dirty)
        self.assertEqual(status, "no_comment")


class EvaluateTests(unittest.TestCase):
    def test_normal_complete_no_warn(self):
        fields = {"status": "complete", "saves": "10", "total_s": "470"}
        warn, reason = check_run_timing.evaluate(fields)
        self.assertFalse(warn)

    def test_ratio_boundary_180_exact_not_warn(self):
        # 1260/7 = 180 ちょうど（> ではない）。total_s も 1200 超だがratioが
        # 条件を満たさないので警告しない。
        fields = {"status": "complete", "saves": "7", "total_s": "1260"}
        warn, reason = check_run_timing.evaluate(fields)
        self.assertFalse(warn)

    def test_total_boundary_1200_exact_not_warn(self):
        # ratio(1200) は閾値超だが total_s は 1200 ちょうど（> ではない）。
        fields = {"status": "complete", "saves": "1", "total_s": "1200"}
        warn, reason = check_run_timing.evaluate(fields)
        self.assertFalse(warn)

    def test_both_conditions_exceeded_warns(self):
        fields = {"status": "complete", "saves": "1", "total_s": "1201"}
        warn, reason = check_run_timing.evaluate(fields)
        self.assertTrue(warn)

    def test_incomplete_always_warns(self):
        fields = {"status": "incomplete", "reason": "missing-step4_5"}
        warn, reason = check_run_timing.evaluate(fields)
        self.assertTrue(warn)
        self.assertIn("missing-step4_5", reason)

    def test_saves_zero_does_not_divide_uses_absolute_floor(self):
        fields = {"status": "complete", "saves": "0", "total_s": "1201"}
        warn, reason = check_run_timing.evaluate(fields)
        self.assertTrue(warn)

    def test_saves_zero_under_floor_no_warn(self):
        fields = {"status": "complete", "saves": "0", "total_s": "1200"}
        warn, reason = check_run_timing.evaluate(fields)
        self.assertFalse(warn)

    def test_real_2026_09_18_shape_no_warn(self):
        # 実データ 09-18 型: saves=4 total_s=371 -> 92.75、警告しない。
        fields = {"status": "complete", "saves": "4", "total_s": "371"}
        warn, reason = check_run_timing.evaluate(fields)
        self.assertFalse(warn)

    def test_real_2026_09_23_shape_no_warn(self):
        # 実データ 09-23 型: saves=22 total_s=1012 -> 46、警告しない。
        fields = {"status": "complete", "saves": "22", "total_s": "1012"}
        warn, reason = check_run_timing.evaluate(fields)
        self.assertFalse(warn)

    def test_malformed_complete_fields_flagged(self):
        fields = {"status": "complete", "saves": "unknown", "total_s": "371"}
        warn, reason = check_run_timing.evaluate(fields)
        self.assertTrue(warn)
        self.assertEqual(reason, "malformed_complete_fields")

    def test_unknown_status_warns(self):
        # P1-3: complete/incomplete のどちらでもない値は異常として警告する。
        fields = {"status": "running", "saves": "4", "total_s": "371"}
        warn, reason = check_run_timing.evaluate(fields)
        self.assertTrue(warn)
        self.assertEqual(reason, "unknown_status:running")


class ProcessDateAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="check_run_timing_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, date, comment_line):
        html_text, _ = check_run_timing.run_timing.insert_comment(FOOTER_HTML, comment_line)
        with open(os.path.join(self.tmp, "%s.html" % date), "w",
                  encoding="utf-8", newline="") as fh:
            fh.write(html_text)

    def test_no_review_file_is_no_comment(self):
        r = check_run_timing.process_date("2026-01-01", self.tmp)
        self.assertEqual(r.status, "no_comment")

    def test_incomplete_review_warns(self):
        comment = _comment(status="incomplete", target="2026-09-17",
                            run_id="5cfbafd2", saves=8, reason="missing-step4_5")
        self._write("2026-09-17", comment)
        r = check_run_timing.process_date("2026-09-17", self.tmp)
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.warn)

    def test_complete_review_within_bounds_no_warn(self):
        comment = _comment(status="complete", target="2026-09-18", run_id="3b462ccb",
                            saves=4, total_s=371)
        self._write("2026-09-18", comment)
        r = check_run_timing.process_date("2026-09-18", self.tmp)
        self.assertEqual(r.status, "ok")
        self.assertFalse(r.warn)

    def test_exit_code_always_zero_even_on_bad_args(self):
        rc = check_run_timing.main(["--reviews-dir", self.tmp, "--all",
                                     "--nonexistent-flag"])
        self.assertEqual(rc, 0)

    def test_parse_error_review_warns(self):
        # P1-1: 壊れたコメント（計測装置の故障）も検知対象として warn=True。
        broken = FOOTER_HTML.replace(
            "<footer>", "<!-- run-timing schema=v2 status=complete\n<footer>"
        )
        with open(os.path.join(self.tmp, "2026-09-19.html"), "w",
                  encoding="utf-8", newline="") as fh:
            fh.write(broken)
        r = check_run_timing.process_date("2026-09-19", self.tmp)
        self.assertEqual(r.status, "parse_error")
        self.assertTrue(r.warn)
        self.assertEqual(r.reason, "malformed_comment")

    def test_unknown_status_review_warns(self):
        comment = _comment(status="running", target="2026-09-19",
                            run_id="deadbeef", saves=4)
        self._write("2026-09-19", comment)
        r = check_run_timing.process_date("2026-09-19", self.tmp)
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.warn)
        self.assertEqual(r.reason, "unknown_status:running")


class CliContractTests(unittest.TestCase):
    """P2-2: main() の CLI 契約（::warning 出力・--since の抑止/許可・
    footer 外コメント・未知 status・parse_error の警告・不正 --since）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="check_run_timing_cli_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, date, comment_line):
        html_text, _ = check_run_timing.run_timing.insert_comment(FOOTER_HTML, comment_line)
        with open(os.path.join(self.tmp, "%s.html" % date), "w",
                  encoding="utf-8", newline="") as fh:
            fh.write(html_text)

    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = check_run_timing.main(argv)
        return rc, buf.getvalue()

    def test_github_warning_emitted_for_incomplete(self):
        comment = _comment(status="incomplete", target="2026-09-17",
                            run_id="5cfbafd2", saves=8, reason="missing-step4_5")
        self._write("2026-09-17", comment)
        rc, out = self._run(["--reviews-dir", self.tmp, "--all", "--github"])
        self.assertEqual(rc, 0)
        self.assertIn("::warning title=run-timing::", out)
        self.assertIn("incomplete:missing-step4_5", out)

    def test_github_warning_emitted_for_parse_error(self):
        broken = FOOTER_HTML.replace(
            "<footer>", "<!-- run-timing schema=v2 status=complete\n<footer>"
        )
        with open(os.path.join(self.tmp, "2026-09-19.html"), "w",
                  encoding="utf-8", newline="") as fh:
            fh.write(broken)
        rc, out = self._run(["--reviews-dir", self.tmp, "--all", "--github"])
        self.assertEqual(rc, 0)
        self.assertIn("::warning title=run-timing::", out)
        self.assertIn("malformed_comment", out)

    def test_github_warning_emitted_for_unknown_status(self):
        comment = _comment(status="running", target="2026-09-19",
                            run_id="deadbeef", saves=4)
        self._write("2026-09-19", comment)
        rc, out = self._run(["--reviews-dir", self.tmp, "--all", "--github"])
        self.assertEqual(rc, 0)
        self.assertIn("::warning title=run-timing::", out)
        self.assertIn("unknown_status:running", out)

    def test_no_comment_never_warns(self):
        rc, out = self._run(["--reviews-dir", self.tmp, "--date", "2026-09-19",
                              "--github"])
        self.assertEqual(rc, 0)
        self.assertIn("status=no_comment", out)
        self.assertNotIn("::warning", out)

    def test_since_suppresses_warning_before_cutoff(self):
        comment = _comment(status="incomplete", target="2026-09-17",
                            run_id="5cfbafd2", saves=8, reason="missing-step4_5")
        self._write("2026-09-17", comment)
        rc, out = self._run(["--reviews-dir", self.tmp, "--all", "--github",
                              "--since", "2026-09-18"])
        self.assertEqual(rc, 0)
        self.assertNotIn("::warning", out)
        self.assertIn("RUN_TIMING_CHECK: checked=1 warned=0", out)

    def test_since_allows_warning_on_or_after_cutoff(self):
        comment = _comment(status="incomplete", target="2026-09-17",
                            run_id="5cfbafd2", saves=8, reason="missing-step4_5")
        self._write("2026-09-17", comment)
        rc, out = self._run(["--reviews-dir", self.tmp, "--all", "--github",
                              "--since", "2026-09-17"])
        self.assertEqual(rc, 0)
        self.assertIn("::warning", out)

    def test_invalid_since_is_ignored_and_noted_not_silently_suppressed(self):
        comment = _comment(status="incomplete", target="2026-09-17",
                            run_id="5cfbafd2", saves=8, reason="missing-step4_5")
        self._write("2026-09-17", comment)
        rc, out = self._run(["--reviews-dir", self.tmp, "--all", "--github",
                              "--since", "not-a-date"])
        self.assertEqual(rc, 0)
        self.assertIn("invalid --since", out)
        self.assertIn("ignored", out)
        # 無視された結果、抑止されず警告が出ること（黙って全件抑止しない）。
        self.assertIn("::warning", out)


class RealReviewsTests(unittest.TestCase):
    """本物の reviews/*.html に対して実行し、既知の結果を確認する。"""

    def setUp(self):
        self.reviews_dir = os.path.join(HERE, "reviews")

    def test_known_complete_dates_do_not_warn(self):
        for date in ("2026-09-18", "2026-09-22", "2026-09-23"):
            path = os.path.join(self.reviews_dir, "%s.html" % date)
            if not os.path.isfile(path):
                self.skipTest("実データが無い: %s" % path)
            r = check_run_timing.process_date(date, self.reviews_dir)
            self.assertEqual(r.status, "ok", date)
            self.assertEqual(r.fields.get("status"), "complete", date)
            self.assertFalse(r.warn, "%s は警告しないはず (%s)" % (date, r.reason))

    def test_known_incomplete_dates_warn(self):
        for date in ("2026-09-17", "2026-09-21"):
            path = os.path.join(self.reviews_dir, "%s.html" % date)
            if not os.path.isfile(path):
                self.skipTest("実データが無い: %s" % path)
            r = check_run_timing.process_date(date, self.reviews_dir)
            self.assertEqual(r.status, "ok", date)
            self.assertEqual(r.fields.get("status"), "incomplete", date)
            self.assertTrue(r.warn, date)

    def test_inflated_copy_triggers_warning(self):
        """膨張を模した一時コピー（ratio/absolute両方を超える）で警告が出る
        ことを、実際の09-18(complete)コメントを差し替えて確認する。両方向
        （通常は警告しない実データ・膨張させると警告する）を1テストで示す。
        """
        src = os.path.join(self.reviews_dir, "2026-09-18.html")
        if not os.path.isfile(src):
            self.skipTest("実データが無い: %s" % src)
        with open(src, encoding="utf-8", newline="") as fh:
            html_text = fh.read()
        tmp = tempfile.mkdtemp(prefix="check_run_timing_inflate_")
        try:
            inflated = check_run_timing.run_timing.COMMENT_RE.sub(
                "", html_text, count=0
            )
            comment = _comment(status="complete", target="2026-09-18",
                                run_id="3b462ccb", saves=4,
                                step1_s=13, step2_s=23, step2_5_s=22, step3_s=5,
                                step4_s=62, step4_5_s=227, step5_s=5, step7_s=14,
                                total_s=9999)
            new_html, _ = check_run_timing.run_timing.insert_comment(inflated, comment)
            dst = os.path.join(tmp, "2026-09-18.html")
            with open(dst, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_html)
            r = check_run_timing.process_date("2026-09-18", tmp)
            self.assertEqual(r.status, "ok")
            self.assertTrue(r.warn)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
