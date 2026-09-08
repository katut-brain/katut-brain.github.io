"""build_capture_index.py の受け入れテスト。

台帳が守るのは2点だけ。

  1. **一度観測した保存は記録から消えない。** captures.json は毎晩 Raindrop から
     作り直されるので、押せなかった日の保存が Raindrop 側で消えたり取り込みが
     不完全だったりすると、証拠ごと消える。台帳はそれを残す。
  2. **壊れた台帳を上書きしない。** 空として書き直すと、そこにしか無い過去の rid が
     永久に消える。

自動回収はしない（2026-09-09 の裁定）。台帳は「どの日が公開できていないか」を
後から人が調べるための記録であって、キューではない。
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

SCRIPT = Path(__file__).resolve().parent / "build_capture_index.py"


def jst_today():
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=9)).date()


class CaptureIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.today = jst_today()
        self.yesterday = self.today - datetime.timedelta(days=1)
        # 報告の開始日は実時計に依存しないよう十分前に置く
        self.since = self.yesterday - datetime.timedelta(days=60)
        os.mkdir(os.path.join(self.tmp, "reviews"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- 素材 -------------------------------------------------------------

    def _d(self, days_ago):
        return self.yesterday - datetime.timedelta(days=days_ago)

    def _write(self, rel, text):
        with open(os.path.join(self.tmp, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def _captures(self, mapping):
        records = [{"rid": rid, "date": day.isoformat(),
                    "source": f"https://x/{rid}"}
                   for day, rids in mapping.items() for rid in rids]
        self._write("captures.json", json.dumps(records))

    def _captures_without_rid(self, day, sources):
        self._write("captures.json", json.dumps(
            [{"date": day.isoformat(), "source": s} for s in sources]))

    def _index(self, mapping):
        self._write("capture_index.json", json.dumps(
            {"days": {d.isoformat(): {"rids": sorted(r)}
                      for d, r in mapping.items()}}))

    def _review(self, day):
        self._write(os.path.join("reviews", day.isoformat() + ".html"),
                    "<!doctype html></html>")

    def _read_index(self):
        with open(os.path.join(self.tmp, "capture_index.json"), encoding="utf-8") as fh:
            return {d: set(v["rids"]) for d, v in json.load(fh)["days"].items()}

    def _run(self, expect=0):
        env = dict(os.environ)
        env["CAPTURE_INDEX_SINCE"] = self.since.isoformat()
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.tmp, env=env,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, expect, r.stdout + r.stderr)
        return r.stdout

    # --- 記録として正しいこと ---------------------------------------------

    def test_writes_what_was_seen_tonight(self):
        self._captures({self.yesterday: [1, 2]})
        out = self._run()
        self.assertIn("INDEX:", out)
        self.assertEqual(self._read_index()[self.yesterday.isoformat()], {1, 2})

    def test_a_save_that_disappears_from_raindrop_stays_on_record(self):
        """押せなかった日の保存が Raindrop から消えても、台帳には残る。"""
        day = self._d(2)
        self._index({day: [1, 2]})
        self._captures({self.yesterday: [9]})     # 今夜は day の保存が見えない
        self._run()
        self.assertEqual(self._read_index()[day.isoformat()], {1, 2})

    def test_the_ledger_is_merged_not_replaced(self):
        day = self._d(3)
        self._index({day: [1, 2]})
        self._captures({day: [2, 3], self.yesterday: [9]})
        self._run()
        self.assertEqual(self._read_index()[day.isoformat()], {1, 2, 3})

    def test_records_without_a_rid_still_reach_the_ledger(self):
        """rid の無いレコードだけの日が、丸ごと落ちないこと。"""
        day = self.yesterday
        self._captures_without_rid(day, ["https://x/one", "https://x/two"])
        self._run()
        rids = self._read_index()[day.isoformat()]
        self.assertEqual(len(rids), 2)
        self.assertTrue(all(r < 0 for r in rids), "合成IDは負数にして実IDと区別する")

    def test_a_synthetic_rid_is_stable_across_nights(self):
        day = self.yesterday
        self._captures_without_rid(day, ["https://x/one"])
        self._run()
        first = self._read_index()[day.isoformat()]
        self._run()
        self.assertEqual(self._read_index()[day.isoformat()], first)

    # --- 報告 ---------------------------------------------------------------

    def test_reports_days_that_have_saves_but_no_review(self):
        missed = self._d(2)
        self._captures({self.yesterday: [9], missed: [1]})
        self._review(self.yesterday)
        out = self._run()
        self.assertIn("UNPUBLISHED:", out)
        self.assertIn(missed.isoformat(), out)
        self.assertIn("not a queue", out)

    def test_reports_none_when_everything_is_published(self):
        self._captures({self.yesterday: [9]})
        self._review(self.yesterday)
        self.assertIn("UNPUBLISHED: none", self._run())

    def test_days_with_no_saves_are_not_reported(self):
        """保存0件の日は review を作らないのが仕様。欠落ではない。"""
        self._captures({self.yesterday: [9]})
        self._review(self.yesterday)
        self._index({self._d(2): []})
        self.assertIn("UNPUBLISHED: none", self._run())

    def test_days_before_the_start_date_are_not_reported(self):
        """台帳を入れる前の欠落まで蒸し返さない（実際に6日ある）。"""
        old = self.since - datetime.timedelta(days=1)
        self._index({old: [1]})
        self._captures({self.yesterday: [9]})
        self._review(self.yesterday)
        self.assertIn("UNPUBLISHED: none", self._run())

    def test_today_is_not_reported_yet(self):
        """当日はまだ確定していない。"""
        self._captures({self.today: [1], self.yesterday: [9]})
        self._review(self.yesterday)
        self.assertIn("UNPUBLISHED: none", self._run())

    # --- 壊れた入力 ---------------------------------------------------------

    def test_a_broken_ledger_is_never_overwritten(self):
        self._write("capture_index.json", "not json")
        self._captures({self.yesterday: [1]})
        out = self._run(expect=1)
        self.assertIn("LEDGER_ERROR", out)
        with open(os.path.join(self.tmp, "capture_index.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "not json", "壊れた台帳を書き換えている")

    def test_a_ledger_with_a_bad_shape_is_never_overwritten(self):
        self._write("capture_index.json",
                    json.dumps({"days": {"2026-09-09": {"rids": "not a list"}}}))
        self._captures({self.yesterday: [1]})
        out = self._run(expect=1)
        self.assertIn("LEDGER_ERROR", out)
        self.assertIn("rids", out)

    def test_a_ledger_with_a_bad_day_key_is_never_overwritten(self):
        self._write("capture_index.json",
                    json.dumps({"days": {"not-a-date": {"rids": [1]}}}))
        self._captures({self.yesterday: [1]})
        self.assertIn("LEDGER_ERROR", self._run(expect=1))

    def test_a_missing_captures_file_is_not_an_error(self):
        out = self._run()
        self.assertIn("INDEX:", out)

    def test_a_broken_captures_file_is_not_an_error(self):
        self._write("captures.json", "{ this is not json")
        self.assertIn("INDEX:", self._run())

    def test_odd_filenames_in_reviews_are_ignored(self):
        self._captures({self.yesterday: [1]})
        self._write(os.path.join("reviews", "index.html"), "x")
        out = self._run()
        self.assertIn(self.yesterday.isoformat(), out)


if __name__ == "__main__":
    unittest.main()
