"""recover_target.py の受け入れテスト。

守りたいのは2点。

  1. 「保存があったのに公開できなかった日」を必ず拾い直すこと。
     2026-09-08 に書き込み経路を切り替えたとき、「照合が通らなければ押さない」設計に
     した代わりに、押せなかった夜のぶんが静かに欠ける経路が残った。それを塞ぐ装置。
  2. その判定の根拠が **Raindrop の現在の状態に依存しないこと**。
     captures.json は毎晩作り直されるので、押せなかった日の保存が Raindrop 側で
     消えると証拠ごと消える。台帳（capture_index.json）で残す。
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

    # --- 素材 ---------------------------------------------------------------

    def _d(self, days_ago):
        return self.yesterday - datetime.timedelta(days=days_ago)

    def _captures(self, mapping):
        """mapping: {date: [rid, ...]}"""
        records = []
        for day, rids in mapping.items():
            for rid in rids:
                records.append({"rid": rid, "date": day.isoformat(),
                                "source": f"https://x/{rid}"})
        self._write("captures.json", json.dumps(records))

    def _index(self, mapping):
        payload = {"days": {d.isoformat(): {"rids": sorted(r)}
                            for d, r in mapping.items()}}
        self._write("capture_index.json", json.dumps(payload))

    def _review(self, day, rids=None, import_status="OK", legacy=False):
        body = "<!doctype html>\n<p>ふりかえり</p>\n"
        if not legacy:
            meta = {"date": day.isoformat(), "rids": sorted(rids or []),
                    "count": len(rids or []), "import": import_status}
            body += "<!-- review-meta: " + json.dumps(meta) + " -->\n"
        body += "</html>\n"
        self._write(os.path.join("reviews", day.isoformat() + ".html"), body)

    def _write(self, rel, text):
        with open(os.path.join(self.tmp, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def _read_index(self):
        with open(os.path.join(self.tmp, "capture_index.json"), encoding="utf-8") as fh:
            return {d: set(v["rids"]) for d, v in json.load(fh)["days"].items()}

    def _run(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.tmp,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = [l for l in r.stdout.splitlines() if l.startswith("TARGET=")]
        self.assertEqual(len(line), 1, "TARGET= の行はちょうど1本: " + r.stdout)
        return line[0][len("TARGET="):], r.stdout

    # --- 通常運転 -----------------------------------------------------------

    def test_returns_yesterday_when_nothing_is_pending(self):
        self._captures({self.yesterday: [1, 2]})
        self._review(self.yesterday, [1, 2])
        target, out = self._run()
        self.assertEqual(target, self.yesterday.isoformat())
        self.assertIn("nothing to recover", out)

    def test_days_with_no_saves_are_not_pending(self):
        """保存0件の日は reviews を作らないのが仕様。欠落ではない。"""
        self._captures({self.yesterday: [1]})
        self._review(self.yesterday, [1])
        target, _ = self._run()
        self.assertEqual(target, self.yesterday.isoformat())

    # --- 回収 ---------------------------------------------------------------

    def test_picks_the_day_that_was_saved_but_never_published(self):
        missed = self._d(2)
        self._captures({self.yesterday: [9], missed: [1, 2]})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, missed.isoformat())
        self.assertIn("no review file", out)

    def test_picks_the_oldest_when_several_are_pending(self):
        older, newer = self._d(5), self._d(2)
        self._captures({self.yesterday: [9], older: [1], newer: [2]})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, older.isoformat())
        self.assertIn("2 day(s) pending", out)

    # --- 台帳が根拠であること（レビュー8周目の指摘） ------------------------

    def test_ledger_survives_the_save_disappearing_from_raindrop(self):
        """押せなかった日の保存が Raindrop から消えても、台帳が残っていれば拾う。

        captures.json は毎晩作り直されるので、これが無いと証拠ごと消える。
        """
        missed = self._d(2)
        self._index({missed: [1, 2]})          # 前の晩に観測して押した台帳
        self._captures({self.yesterday: [9]})  # 今夜の Raindrop には missed の保存が無い
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, missed.isoformat(),
                         "Raindrop から消えたら回収しない、では意味がない")
        self.assertIn("no review file", out)

    def test_ledger_is_merged_not_replaced(self):
        """一度観測した rid は、今夜見えなくても台帳から消さない。"""
        old_day = self._d(3)
        self._index({old_day: [1, 2]})
        self._captures({old_day: [2, 3], self.yesterday: [9]})
        self._review(old_day, [1, 2, 3])
        self._review(self.yesterday, [9])
        self._run()
        self.assertEqual(self._read_index()[old_day.isoformat()], {1, 2, 3})

    def test_ledger_is_written_even_on_a_normal_night(self):
        self._captures({self.yesterday: [7]})
        self._review(self.yesterday, [7])
        _, out = self._run()
        self.assertIn("INDEX:", out)
        self.assertEqual(self._read_index()[self.yesterday.isoformat()], {7})

    # --- 「ファイルがあれば公開済み」にしない（同 P1） ----------------------

    def test_a_review_missing_some_saves_is_pending(self):
        day = self._d(1)
        self._captures({day: [1, 2, 3], self.yesterday: [9]})
        self._review(day, [1])           # 3件のうち1件しか載っていない
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, day.isoformat())
        self.assertIn("not in the published review", out)

    def test_incomplete_import_is_redone_only_when_new_saves_appear(self):
        day = self._d(1)
        # 取り込みが不完全だった夜に、見えていた2件だけで公開した
        self._review(day, [1, 2], import_status="INCOMPLETE")
        self._index({day: [1, 2]})
        self._captures({self.yesterday: [9]})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, self.yesterday.isoformat(),
                         "足せるものが無いのに毎晩やり直しては前へ進めない")
        self.assertIn("nothing to recover", out)

        # 3件目が見えるようになったら、その日をやり直す
        self._captures({day: [1, 2, 3], self.yesterday: [9]})
        target, out = self._run()
        self.assertEqual(target, day.isoformat())
        self.assertIn("incomplete", out)

    def test_legacy_reviews_without_meta_are_left_alone(self):
        """review-meta を書く前のファイルまで遡って作り直さない。"""
        day = self._d(3)
        self._captures({day: [1, 2], self.yesterday: [9]})
        self._review(day, legacy=True)
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, self.yesterday.isoformat())
        self.assertIn("nothing to recover", out)

    # --- 窓と壊れた入力 -----------------------------------------------------

    def test_does_not_look_further_back_than_the_window(self):
        ancient = self._d(40)
        self._captures({self.yesterday: [9], ancient: [1]})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, self.yesterday.isoformat())
        self.assertIn("nothing to recover", out)

    def test_missing_captures_file_falls_back_to_yesterday(self):
        target, _ = self._run()
        self.assertEqual(target, self.yesterday.isoformat())

    def test_broken_captures_file_falls_back_to_yesterday(self):
        self._write("captures.json", "{ this is not json")
        target, _ = self._run()
        self.assertEqual(target, self.yesterday.isoformat())

    def test_broken_index_file_does_not_stop_the_run(self):
        self._write("capture_index.json", "not json either")
        self._captures({self.yesterday: [1]})
        self._review(self.yesterday, [1])
        target, _ = self._run()
        self.assertEqual(target, self.yesterday.isoformat())

    def test_broken_review_meta_is_treated_as_legacy(self):
        day = self._d(1)
        self._captures({day: [1], self.yesterday: [9]})
        self._write(os.path.join("reviews", day.isoformat() + ".html"),
                    "<!doctype html><!-- review-meta: {broken --></html>")
        self._review(self.yesterday, [9])
        target, _ = self._run()
        self.assertEqual(target, self.yesterday.isoformat())

    def test_odd_filenames_in_reviews_are_ignored(self):
        self._captures({self.yesterday: [1]})
        self._review(self.yesterday, [1])
        self._write(os.path.join("reviews", "index.html"), "x")
        self._write(os.path.join("reviews", "notes.txt"), "x")
        target, _ = self._run()
        self.assertEqual(target, self.yesterday.isoformat())


if __name__ == "__main__":
    unittest.main()
