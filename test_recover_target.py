"""recover_target.py の受け入れテスト。

守りたいのは1点だけ ——「保存があったのに公開されなかった日」を必ず拾い直すこと。
2026-09-08 に書き込み経路を切り替えたとき、「照合が通らなければ押さない」設計に
した代わりに、押せなかった夜のぶんが静かに欠ける経路が残った。それを塞ぐ装置。
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "recover_target.py"


def jst_today():
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=9)).date()


class RecoverTargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.today = jst_today()
        self.yesterday = self.today - datetime.timedelta(days=1)
        os.mkdir(os.path.join(self.tmp, "reviews"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _captures(self, *dates):
        records = [{"rid": i, "date": d.isoformat(), "source": "https://x/"}
                   for i, d in enumerate(dates)]
        with open(os.path.join(self.tmp, "captures.json"), "w", encoding="utf-8") as fh:
            json.dump(records, fh)

    def _published(self, *dates):
        for d in dates:
            path = os.path.join(self.tmp, "reviews", d.isoformat() + ".html")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("<!doctype html></html>")

    def _run(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.tmp,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = [l for l in r.stdout.splitlines() if l.startswith("TARGET=")]
        self.assertEqual(len(line), 1, "TARGET= の行はちょうど1本: " + r.stdout)
        return line[0][len("TARGET="):], r.stdout

    def _d(self, days_ago):
        return self.yesterday - datetime.timedelta(days=days_ago)

    # --- 通常運転 -----------------------------------------------------------

    def test_returns_yesterday_when_nothing_is_missing(self):
        self._captures(self.yesterday, self._d(1))
        self._published(self.yesterday, self._d(1))
        target, out = self._run()
        self.assertEqual(target, self.yesterday.isoformat())
        self.assertIn("nothing to recover", out)

    def test_days_with_no_saves_are_not_treated_as_missing(self):
        """保存0件の日は reviews を作らないのが仕様。欠落ではない。"""
        self._captures(self.yesterday)
        self._published(self.yesterday)
        target, out = self._run()
        self.assertEqual(target, self.yesterday.isoformat())
        self.assertIn("nothing to recover", out)

    # --- 回収 ---------------------------------------------------------------

    def test_picks_the_day_that_was_saved_but_never_published(self):
        missed = self._d(2)
        self._captures(self.yesterday, missed, self._d(1))
        self._published(self.yesterday, self._d(1))
        target, out = self._run()
        self.assertEqual(target, missed.isoformat())
        self.assertIn("saved but not published", out)

    def test_picks_the_oldest_when_several_are_missing(self):
        older, newer = self._d(4), self._d(2)
        self._captures(self.yesterday, older, newer)
        self._published(self.yesterday)
        target, out = self._run()
        self.assertEqual(target, older.isoformat(), "溜まっていたら古い方から消化する")
        self.assertIn("2 day(s)", out)

    def test_yesterday_itself_can_be_the_missing_day(self):
        """昨晩押せなかった場合、今夜もう一度その日をやり直す。"""
        self._captures(self.yesterday)
        target, _ = self._run()
        self.assertEqual(target, self.yesterday.isoformat())

    def test_does_not_look_further_back_than_the_window(self):
        """古すぎる欠落まで拾うと、いつまでも最新に追いつけない。"""
        ancient = self._d(30)
        self._captures(self.yesterday, ancient)
        self._published(self.yesterday)
        target, out = self._run()
        self.assertEqual(target, self.yesterday.isoformat())
        self.assertIn("nothing to recover", out)

    # --- 壊れた入力 ---------------------------------------------------------

    def test_missing_captures_file_falls_back_to_yesterday(self):
        target, _ = self._run()
        self.assertEqual(target, self.yesterday.isoformat())

    def test_broken_captures_file_falls_back_to_yesterday(self):
        with open(os.path.join(self.tmp, "captures.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        target, _ = self._run()
        self.assertEqual(target, self.yesterday.isoformat())

    def test_odd_filenames_in_reviews_are_ignored(self):
        self._captures(self.yesterday)
        with open(os.path.join(self.tmp, "reviews", "index.html"), "w") as fh:
            fh.write("x")
        with open(os.path.join(self.tmp, "reviews", "notes.txt"), "w") as fh:
            fh.write("x")
        target, _ = self._run()
        self.assertEqual(target, self.yesterday.isoformat(),
                         "日付でないファイル名を公開済みと数えてはいけない")


if __name__ == "__main__":
    unittest.main()
