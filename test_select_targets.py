#!/usr/bin/env python3
# test_select_targets.py — select_targets.py の回帰テスト。
#
# 本番の選定関数（select_targets.select / .load_captures / .reviewed_rids）を
# 一時ディレクトリに作った captures.json / reviews/*.html へ実際に通す
# （モックで差し替えない）。標準ライブラリの unittest のみを使う
# （test_backfill_recovery.py 等と同じ流儀）。

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import select_targets  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _write_captures(path, records):
    _write(path, json.dumps(records, ensure_ascii=False))


class TestLoadCaptures(unittest.TestCase):
    def test_rid_and_no_rid_split(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "captures.json")
            _write_captures(path, [
                {"rid": 1, "date": "2026-06-01T00:00:00Z"},
                {"rid": 2, "date": "2026-06-02"},
                {"date": "2026-06-03"},  # rid 無し
                {"rid": "x", "date": "2026-06-04"},  # rid が非整数
                {"rid": 3, "date": "not-a-date"},  # 不正な日付は除外
            ])
            records, no_rid = select_targets.load_captures(path)
            self.assertEqual(sorted(r for r, _d, _rec in records), [1, 2])
            self.assertEqual(no_rid, 2)


class TestReviewedRids(unittest.TestCase):
    def test_extraction_quotes_multiple_cards_negative(self):
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            _write(os.path.join(reviews, "2026-06-01.html"), """
                <div class="vcard"><button data-rid="111">a</button></div>
                <div class="vcard"><button data-rid='222'>b</button></div>
                <div class="vcard"><button data-rid="-333">c</button></div>
            """)
            rids, unreadable = select_targets.reviewed_rids(reviews)
            self.assertEqual(rids, {111, 222, -333})
            self.assertEqual(unreadable, [])

    def test_unreadable_file_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            path = os.path.join(reviews, "2026-06-02.html")
            # 壊れたUTF-8バイト列を書く（UnicodeDecodeErrorを起こす）。
            with open(path, "wb") as f:
                f.write(b"<div data-rid=\"1\">\xff\xfe broken</div>")
            rids, unreadable = select_targets.reviewed_rids(reviews)
            self.assertEqual(rids, set())
            self.assertEqual(unreadable, ["2026-06-02.html"])

    def test_missing_reviews_dir(self):
        rids, unreadable = select_targets.reviewed_rids("/no/such/dir/xyz")
        self.assertEqual(rids, set())
        self.assertEqual(unreadable, [])


class TestSelect(unittest.TestCase):
    def _setup(self, d, records, review_files):
        captures_path = os.path.join(d, "captures.json")
        _write_captures(captures_path, records)
        reviews_dir = os.path.join(d, "reviews")
        os.makedirs(reviews_dir, exist_ok=True)
        for name, content in review_files.items():
            _write(os.path.join(reviews_dir, name), content)
        return captures_path, reviews_dir

    def test_past_current_future_all_selected_and_reviewed_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(
                d,
                records=[
                    {"rid": 1, "date": "2026-06-01"},   # past, unreviewed -> selected
                    {"rid": 2, "date": "2026-09-21"},   # target day, unreviewed -> selected
                    {"rid": 3, "date": "2026-09-22"},   # future, unreviewed -> selected
                    {"rid": 4, "date": "2026-06-01"},   # past, already reviewed -> excluded
                ],
                review_files={
                    "2026-06-01.html": '<div data-rid="4"></div>',
                },
            )
            result = select_targets.select(
                "2026-09-21", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [1, 2, 3])
            self.assertEqual(result["_selected_count"], 3)
            self.assertEqual(result["_past_count"], 1)
            self.assertEqual(result["by_date"],
                              {"2026-06-01": 1, "2026-09-21": 1, "2026-09-22": 1})

    def test_sort_order_date_then_rid(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(
                d,
                records=[
                    {"rid": 30, "date": "2026-06-01"},
                    {"rid": 10, "date": "2026-06-01"},
                    {"rid": 5, "date": "2026-05-01"},
                ],
                review_files={},
            )
            result = select_targets.select(
                "2026-06-02", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [5, 10, 30])

    def test_index_only_reported(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(
                d, records=[{"rid": 1, "date": "2026-06-01"}], review_files={})
            index_path = os.path.join(d, "capture_index.json")
            _write(index_path, json.dumps(
                {"days": {"2026-06-01": {"rids": [1, 999]}}}))
            result = select_targets.select(
                "2026-06-02", captures_path=captures_path,
                reviews_dir=reviews_dir, capture_index_path=index_path)
            self.assertEqual(result["_index_only_count"], 1)

    def test_unreadable_review_file_surfaces_in_result(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(
                d, records=[{"rid": 1, "date": "2026-06-01"}], review_files={})
            broken = os.path.join(reviews_dir, "2026-06-02.html")
            with open(broken, "wb") as f:
                f.write(b"\xff\xfe not utf8")
            result = select_targets.select(
                "2026-06-03", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["_unreadable_reviews"], ["2026-06-02.html"])


class TestMain(unittest.TestCase):
    def test_status_line_emitted_and_exit_zero(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path = os.path.join(d, "captures.json")
            _write_captures(captures_path, [{"rid": 1, "date": "2026-06-01"}])
            reviews_dir = os.path.join(d, "reviews")
            os.makedirs(reviews_dir)
            index_path = os.path.join(d, "capture_index.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = select_targets.main([
                    "--target", "2026-06-02",
                    "--captures", captures_path,
                    "--reviews-dir", reviews_dir,
                    "--capture-index", index_path,
                ])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("SELECT_STATUS: target=2026-06-02", out)
            self.assertIn("selected=1", out)

    def test_warn_unreadable_flag_in_status(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path = os.path.join(d, "captures.json")
            _write_captures(captures_path, [{"rid": 1, "date": "2026-06-01"}])
            reviews_dir = os.path.join(d, "reviews")
            os.makedirs(reviews_dir)
            with open(os.path.join(reviews_dir, "2026-06-02.html"), "wb") as f:
                f.write(b"\xff\xfe not utf8")
            index_path = os.path.join(d, "capture_index.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = select_targets.main([
                    "--target", "2026-06-03",
                    "--captures", captures_path,
                    "--reviews-dir", reviews_dir,
                    "--capture-index", index_path,
                ])
            self.assertEqual(rc, 0)
            self.assertIn("warn=unreadable", buf.getvalue())

    def test_exception_in_select_still_exits_zero_with_status(self):
        # captures 引数に「存在するがディレクトリ」を渡し、内部の open() が
        # 例外的な経路を通っても main() が exit 0 で STATUS を出すことを確認する。
        with tempfile.TemporaryDirectory() as d:
            captures_path = d  # ディレクトリを渡す -> open() は失敗するが
            # load_captures は _load_json の (OSError, ValueError) 捕捉で
            # None を返すだけなので通常は例外にならない。ここでは main() の
            # 例外安全性そのものを、select を壊す形で直接確認する。
            orig_select = select_targets.select

            def _boom(*a, **kw):
                raise RuntimeError("boom")

            select_targets.select = _boom
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = select_targets.main(["--target", "2026-06-01",
                                               "--captures", captures_path])
                self.assertEqual(rc, 0)
                self.assertIn("SELECT_STATUS: target=2026-06-01", buf.getvalue())
                self.assertIn("error=RuntimeError", buf.getvalue())
            finally:
                select_targets.select = orig_select


if __name__ == "__main__":
    unittest.main()
