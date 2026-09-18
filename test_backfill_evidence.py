# -*- coding: utf-8 -*-
"""手順2.5 が起動したこと自体の証跡（fetch_facts/runs/<日付>.json）のテスト。

なぜ要るか: 候補が9件ある夜が3晩続いても fetch_facts に痕跡が1件も増えず、
「手順2.5 を実行していない」と「実行したが何も書かなかった」を成果物から
区別できなかった（2026-09-15〜17）。無人LLMが打つ run_timing の mark は
実行証跡ではない（mark だけ打ってコマンドを飛ばせる）。
"""

import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import backfill  # noqa: E402


class RunEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bf-evidence-")
        self._old = os.environ.get("FETCH_FACTS_DIR")
        os.environ["FETCH_FACTS_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FETCH_FACTS_DIR", None)
        else:
            os.environ["FETCH_FACTS_DIR"] = self._old

    def _path(self, day="2026-09-18"):
        return os.path.join(self.tmp, "runs", "%s.json" % day)

    def _read(self, day="2026-09-18"):
        with io.open(self._path(day), encoding="utf-8") as f:
            return json.load(f)

    def test_writes_evidence_with_target_and_schema(self):
        backfill._write_run_evidence("2026-09-18", status="started", limit=1)
        d = self._read()
        self.assertEqual(d["schema"], "v1")
        self.assertEqual(d["target"], "2026-09-18")
        self.assertEqual(d["status"], "started")
        self.assertEqual(d["limit"], 1)

    def test_second_write_replaces_the_first(self):
        """完了時に status=started を必ず上書きする。古い状態を残さない。"""
        backfill._write_run_evidence("2026-09-18", status="started")
        backfill._write_run_evidence("2026-09-18", status="completed", attempted=1)
        d = self._read()
        self.assertEqual(d["status"], "completed")
        self.assertEqual(d["attempted"], 1)

    def test_no_tmp_file_left_behind(self):
        """一時ファイル＋os.replace の原子的置換。.tmp を残さない。"""
        backfill._write_run_evidence("2026-09-18", status="started")
        self.assertFalse(os.path.exists(self._path() + ".tmp"))

    def test_bad_target_writes_nothing(self):
        """日付として妥当でない target ではファイル名を作らない（パス汚染よけ）。"""
        for bad in ("../evil", "2026-9-8", "", None, "2026-09-18/x"):
            backfill._write_run_evidence(bad, status="started")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "runs")))

    def test_never_raises_when_dir_is_unwritable(self):
        """証跡が書けなくても手順を止めない（例外を投げない）。"""
        os.environ["FETCH_FACTS_DIR"] = os.path.join(self.tmp, "nope.txt")
        with io.open(os.environ["FETCH_FACTS_DIR"], "w", encoding="utf-8") as f:
            f.write("not a directory")
        backfill._write_run_evidence("2026-09-18", status="started")   # 例外が出ないこと

    def test_evidence_dir_is_not_read_by_ledger(self):
        """runs/ は fetch_facts/*.json の glob に掛からない＝台帳の集計を汚さない。"""
        import ledger
        backfill._write_run_evidence("2026-09-18", status="completed", candidates=9)
        old = ledger.FACTS_DIR
        try:
            ledger.FACTS_DIR = self.tmp
            self.assertEqual(ledger._load_all_records(), [])
        finally:
            ledger.FACTS_DIR = old


if __name__ == "__main__":
    unittest.main()
