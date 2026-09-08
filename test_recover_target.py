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

# recover_target.MIGRATION_DATE と同じ値。これより前の日は「装置を入れる前」として
# 対象外になるので、テストの日付はすべてこれ以降に置く。
# 本番の移行日は 2026-09-08（この装置を入れた夜が扱う最初の対象日）。
# テストは実時計に依存しないよう、環境変数 RECOVER_MIGRATION_DATE で
# 「昨日の60日前」へ差し替える（setUp で self.migration に入る）。


def jst_today():
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=9)).date()


class RecoverTargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.today = jst_today()
        self.yesterday = self.today - datetime.timedelta(days=1)
        os.mkdir(os.path.join(self.tmp, "reviews"))
        self.migration = self.yesterday - datetime.timedelta(days=60)

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

    def _captures_without_rid(self, day, sources):
        records = [{"date": day.isoformat(), "source": src} for src in sources]
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

    def _read_index_raw(self):
        with open(os.path.join(self.tmp, "capture_index.json"), encoding="utf-8") as fh:
            return json.load(fh)["days"]

    def _snapshot(self, day, rids):
        d = os.path.join(self.tmp, "capture_days")
        os.makedirs(d, exist_ok=True)
        records = [{"rid": r, "date": day.isoformat(), "source": f"https://x/{r}",
                    "title": f"t{r}"} for r in rids]
        with open(os.path.join(d, day.isoformat() + ".json"), "w", encoding="utf-8") as fh:
            json.dump(records, fh)

    def _read_snapshot(self, day):
        with open(os.path.join(self.tmp, "capture_days", day.isoformat() + ".json"),
                  encoding="utf-8") as fh:
            return {r["rid"] for r in json.load(fh)}

    def _snapshot_exists(self, day):
        return os.path.exists(os.path.join(self.tmp, "capture_days",
                                           day.isoformat() + ".json"))

    def _run(self):
        env = dict(os.environ)
        env["RECOVER_MIGRATION_DATE"] = self.migration.isoformat()
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.tmp, env=env,
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
        """押せなかった日の保存が Raindrop から消えても拾う。

        captures.json は毎晩作り直されるので、台帳が無いと証拠ごと消える。
        ただし台帳（rid だけ）では作り直せないので、本文のスナップショットも要る
        （敵対的レビュー10周目で判明。当初この検査はスナップショット無しで
        「拾える」ことにしていたが、拾っても何も作れなかった）。
        """
        missed = self._d(2)
        self._index({missed: [1, 2]})          # 前の晩に観測して押した台帳
        self._snapshot(missed, [1, 2])         # 同じ晩に押した本文
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

    # --- 材料まで残す（レビュー10周目の指摘） ------------------------------

    def test_snapshot_is_written_for_the_day_being_processed(self):
        """今夜これから作る日の本文を残す。押せずに終わっても翌晩作り直せる。"""
        self._captures({self.yesterday: [1, 2]})
        _, out = self._run()
        self.assertIn("SNAPSHOT:", out)
        self.assertTrue(self._snapshot_exists(self.yesterday))

    def test_a_day_kept_only_by_its_snapshot_is_still_recovered(self):
        """Raindrop から消えても、本文のスナップショットがあれば回収し続ける。"""
        missed = self._d(2)
        self._index({missed: [1, 2]})
        self._snapshot(missed, [1, 2])
        self._captures({self.yesterday: [9]})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, missed.isoformat())
        self.assertNotIn("unrecoverable", out)

    def test_a_save_with_no_body_anywhere_is_dropped_from_what_the_day_must_cover(self):
        """本文がどこにも無い保存は、その日の期待から外す。

        外さないと、review はその rid を満たせないので永久に pending のまま
        その日に張り付き、新しい日の公開まで止まる。
        """
        day = self._d(3)
        self._index({day: [1, 2]})
        self._snapshot(day, [2])            # 1 の本文はどこにも無い
        self._captures({self.yesterday: [9]})
        self._review(day, [2])              # 手元にある 2 だけで公開した
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, self.yesterday.isoformat(),
                         "満たしようのない rid のせいで張り付いてはいけない")
        self.assertIn("no body on file", out)
        self.assertEqual(self._read_index_raw()[day.isoformat()]["missing"], [1],
                         "外した理由が台帳に残っていない")

    def test_a_day_with_no_bodies_at_all_is_skipped(self):
        """本文が1件も無い日は、作り直しても空になるので対象にしない。"""
        gone = self._d(3)
        self._index({gone: [1]})
        self._captures({self.yesterday: [9]})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, self.yesterday.isoformat())
        self.assertEqual(self._read_index_raw()[gone.isoformat()]["missing"], [1])

    def test_a_partially_returning_day_does_not_stall(self):
        """一部だけ戻ってきても張り付かないこと（レビュー13周目の指摘）。

        日単位の真偽値でやっていたときは、rid 1 の本文が無いまま rid 2 だけ
        見えるとフラグが解除され、しかも review は 1 を満たせないので、その日に
        毎晩張り付いて新しい日の公開まで止まった。
        """
        day = self._d(3)
        self._index({day: [1]})
        self._captures({self.yesterday: [9]})
        self._review(self.yesterday, [9])
        self._run()   # 1夜目: 1 の本文が無いので missing に落ちる

        # 2夜目: 同じ日の別の保存 2 だけが見えた
        self._captures({day: [2], self.yesterday: [9]})
        target, out = self._run()
        self.assertEqual(target, day.isoformat(), "2 は公開されていないので対象になる")

        # 3夜目: 2 を載せた review ができたら、1 は満たせないままでも先へ進む
        self._review(day, [2])
        self._captures({self.yesterday: [9]})
        target, out = self._run()
        self.assertEqual(target, self.yesterday.isoformat(),
                         "満たせない 1 のせいで張り付いてはいけない")

    def test_a_body_that_comes_back_is_expected_again(self):
        """一度 missing に落ちた保存でも、本文が戻れば期待に戻す。"""
        day = self._d(3)
        self._index({day: [1]})
        self._captures({self.yesterday: [9]})
        self._review(self.yesterday, [9])
        self._run()
        self.assertEqual(self._read_index_raw()[day.isoformat()]["missing"], [1])

        self._captures({day: [1], self.yesterday: [9]})   # 戻ってきた
        target, out = self._run()
        self.assertEqual(target, day.isoformat())
        self.assertIn("came back", out)
        self.assertNotIn("missing", self._read_index_raw()[day.isoformat()])
        self.assertEqual(self._read_snapshot(day), {1})

    def test_a_newer_pending_day_is_still_reached_after_a_dead_one(self):
        """材料の無い日を飛ばして、その次の pending へ進むこと。"""
        gone, alive = self._d(4), self._d(2)
        self._index({gone: [1], alive: [2]})
        self._snapshot(alive, [2])
        self._captures({self.yesterday: [9]})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, alive.isoformat())
        self.assertEqual(self._read_index_raw()[gone.isoformat()]["missing"], [1])

    def test_a_partial_import_does_not_shrink_the_snapshot(self):
        """取り込みが不完全な夜に、既に残した本文を削ってはいけない。

        実際に通る経路（敵対的レビュー11周目の指摘）:
          初夜  … A と B を観測 → 台帳とスナップショットは押せたが review は失敗
          翌晩  … 取り込みが INCOMPLETE で A しか見えない
          翌々晩… Raindrop から B が消える
        上書きにしていると2晩目でスナップショットが [A] に縮み、3晩目には B の本文が
        どこにも無くなる。しかも台帳には B の rid が残るので、その日は永久に pending の
        まま張り付く（材料が A だけ残るので unrecoverable にもならない）。
        """
        day = self._d(2)

        # 初夜: A と B を観測
        self._captures({day: [1, 2]})
        self._run()
        self.assertEqual(self._read_snapshot(day), {1, 2})

        # 翌晩: 取り込みが不完全で A しか見えない（review はまだ無い＝pending のまま）
        self._captures({day: [1], self.yesterday: [9]})
        self._review(self.yesterday, [9])
        target, _ = self._run()
        self.assertEqual(target, day.isoformat())
        self.assertEqual(self._read_snapshot(day), {1, 2},
                         "不完全な取り込みでスナップショットが縮んでいる")

        # 翌々晩: Raindrop から B が消えても、本文は残っている
        self._captures({self.yesterday: [9]})
        target, out = self._run()
        self.assertEqual(target, day.isoformat())
        self.assertEqual(self._read_snapshot(day), {1, 2})
        self.assertNotIn("no material left", out)

    def test_the_snapshot_keeps_growing_across_nights(self):
        """別々の晩に見えた保存が、すべて1つのスナップショットに溜まること。"""
        day = self._d(2)
        self._captures({day: [1]})
        self._run()
        self._captures({day: [2], self.yesterday: [9]})
        self._review(self.yesterday, [9])
        self._run()
        self.assertEqual(self._read_snapshot(day), {1, 2})

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

    def test_a_review_without_meta_after_migration_is_pending(self):
        """移行日以降に meta を書き忘れたら、公開済みと見なさない。

        無人LLMの書き忘れはいちばん起きやすい逸脱で、それを通すと rid が欠けた
        review が永久に正常扱いになる（敵対的レビュー9周目の指摘）。
        """
        day = self._d(1)
        self._captures({day: [1, 2], self.yesterday: [9]})
        self._review(day, legacy=True)
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, day.isoformat())
        self.assertIn("review-meta is missing", out)

    def test_the_ledger_does_not_get_ahead_of_the_snapshot(self):
        """本文を残せなかった夜は、その日の rid を台帳へ足さない。

        足すと「rid だけあって本文が無い」状態になり、翌晩その rid は missing へ落ちて
        回収対象から外れる（レビュー13周目の P1）。
        """
        os.makedirs(os.path.join(self.tmp, "capture_days"), exist_ok=True)
        # 書き込み先を塞ぐ: 同名のディレクトリを置いて os.replace を失敗させる
        os.makedirs(os.path.join(self.tmp, "capture_days",
                                 self.yesterday.isoformat() + ".json"), exist_ok=True)
        self._captures({self.yesterday: [1]})
        _, out = self._run()
        self.assertIn("SNAPSHOT_ERROR", out)
        self.assertIn("holding tonight's rids back", out)
        self.assertNotIn(self.yesterday.isoformat(), self._read_index_raw())

    def test_records_without_a_rid_still_reach_the_ledger(self):
        """`rid` の無いレコードだけの日が、台帳から丸ごと落ちないこと。

        台帳は rid の集合でその日を表すので、rid を持たないレコードを飛ばすと
        「保存0件の日」と区別がつかず、本文がスナップショットに残っていても
        回収の対象にならない（敵対的レビュー14周目の指摘）。
        実測では captures.json の320件すべてが整数 rid を持つが、通ったときに
        黙って落ちる作りにはしない。
        """
        day = self.yesterday
        self._captures_without_rid(day, ["https://x/one", "https://x/two"])
        target, out = self._run()
        self.assertEqual(target, day.isoformat())
        ledger = self._read_index_raw()
        self.assertIn(day.isoformat(), ledger, "rid が無い日が台帳に載っていない")
        self.assertEqual(len(ledger[day.isoformat()]["rids"]), 2)
        self.assertTrue(all(r < 0 for r in ledger[day.isoformat()]["rids"]),
                        "合成した識別子は負数にして実IDと区別する")
        self.assertEqual(len(self._read_snapshot(day)), 2)

    def test_a_synthetic_rid_is_stable_across_nights(self):
        """同じレコードなら毎晩同じ識別子になること（違うと毎晩増える）。"""
        day = self.yesterday
        self._captures_without_rid(day, ["https://x/one"])
        self._run()
        first = self._read_index_raw()[day.isoformat()]["rids"]
        self._run()
        self.assertEqual(self._read_index_raw()[day.isoformat()]["rids"], first)

    # --- 窓と壊れた入力 -----------------------------------------------------

    def test_pending_days_are_never_dropped_by_age(self):
        """一度 pending と分かった日は、何日経っても捨てない。

        以前は14日の窓を掛けていたが、回収しようとした晩にまた押せなかった日が
        翌晩には窓の外へ落ち、台帳に残っていても二度と拾われなかった
        （敵対的レビュー9周目の指摘。しかも旧テストがその挙動を「仕様」として
        固定していた）。
        """
        long_ago = self.migration
        self._index({long_ago: [1]})
        self._snapshot(long_ago, [1])
        self._captures({self.yesterday: [9]})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, long_ago.isoformat())
        self.assertIn("pending", out)

    def test_days_before_the_migration_date_are_left_alone(self):
        """装置を入れる前の欠落まで遡らない（実際に6日ある）。"""
        before = self.migration - datetime.timedelta(days=1)
        self._index({before: [1]})
        self._captures({self.yesterday: [9]})
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

    def test_a_broken_ledger_is_never_overwritten(self):
        """壊れた台帳を空として書き直すと、そこにしか無い過去の rid が永久に消える。"""
        self._write("capture_index.json", "not json either")
        self._captures({self.yesterday: [1]})
        self._review(self.yesterday, [1])
        target, out = self._run()
        self.assertEqual(target, self.yesterday.isoformat(), "その夜の処理は止めない")
        self.assertIn("LEDGER_ERROR", out)
        with open(os.path.join(self.tmp, "capture_index.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "not json either", "壊れた台帳を書き換えている")

    def test_a_ledger_with_a_bad_shape_is_never_overwritten(self):
        self._write("capture_index.json",
                    json.dumps({"days": {"2026-09-09": {"rids": "not a list"}}}))
        self._captures({self.yesterday: [1]})
        self._review(self.yesterday, [1])
        _, out = self._run()
        self.assertIn("LEDGER_ERROR", out)
        self.assertIn("rids", out)

    def test_a_ledger_with_a_bad_day_key_is_never_overwritten(self):
        self._write("capture_index.json",
                    json.dumps({"days": {"not-a-date": {"rids": [1]}}}))
        self._captures({self.yesterday: [1]})
        self._review(self.yesterday, [1])
        _, out = self._run()
        self.assertIn("LEDGER_ERROR", out)

    def test_broken_review_meta_is_pending_not_legacy(self):
        day = self._d(1)
        self._captures({day: [1], self.yesterday: [9]})
        self._write(os.path.join("reviews", day.isoformat() + ".html"),
                    "<!doctype html><!-- review-meta: {broken --></html>")
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, day.isoformat())
        self.assertIn("review-meta", out)

    def _raw_meta(self, day, meta):
        self._write(os.path.join("reviews", day.isoformat() + ".html"),
                    "<!doctype html><!-- review-meta: " + json.dumps(meta) + " --></html>")

    def test_meta_with_the_wrong_date_is_pending(self):
        day = self._d(1)
        self._captures({day: [1], self.yesterday: [9]})
        self._raw_meta(day, {"date": "1999-01-01", "rids": [1],
                             "count": 1, "import": "OK"})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, day.isoformat())
        self.assertIn("expected", out)

    def test_meta_with_a_count_that_does_not_match_is_pending(self):
        day = self._d(1)
        self._captures({day: [1, 2], self.yesterday: [9]})
        self._raw_meta(day, {"date": day.isoformat(), "rids": [1, 2],
                             "count": 5, "import": "OK"})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, day.isoformat())
        self.assertIn("count", out)

    def test_meta_with_a_bad_import_value_is_pending(self):
        day = self._d(1)
        self._captures({day: [1], self.yesterday: [9]})
        self._raw_meta(day, {"date": day.isoformat(), "rids": [1],
                             "count": 1, "import": "maybe"})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, day.isoformat())
        self.assertIn("import", out)

    def test_meta_with_duplicate_rids_is_pending(self):
        day = self._d(1)
        self._captures({day: [1], self.yesterday: [9]})
        self._raw_meta(day, {"date": day.isoformat(), "rids": [1, 1],
                             "count": 2, "import": "OK"})
        self._review(self.yesterday, [9])
        target, out = self._run()
        self.assertEqual(target, day.isoformat())
        self.assertIn("duplicates", out)

    def test_odd_filenames_in_reviews_are_ignored(self):
        self._captures({self.yesterday: [1]})
        self._review(self.yesterday, [1])
        self._write(os.path.join("reviews", "index.html"), "x")
        self._write(os.path.join("reviews", "notes.txt"), "x")
        target, _ = self._run()
        self.assertEqual(target, self.yesterday.isoformat())


if __name__ == "__main__":
    unittest.main()
