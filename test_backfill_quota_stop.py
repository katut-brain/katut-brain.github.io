# -*- coding: utf-8 -*-
"""Gemini 無料枠切れ（video_reason=="gemini_quota"）を踏んだ夜、残りの候補を
起動しないことの回帰テスト（2026-09-23 Codex 1周目レビュー P1 対応）。

なぜ要るか: gemini_quota は一過性(transient)分類だが、backfill.run() は起動する
たびに attempt_seq を加算する。無料枠が切れている間は同じプロセス内の全候補が
同じ理由で失敗し続ける可能性が高く、DEFAULT_MAX_ATTEMPTS（既定3）を早く
消費して exhausted に落ち、恒久的に候補から外れてしまう。1件で gemini_quota が
観測された時点で以降を起動しないことで、無駄な試行と早期 exhausted 化の両方を防ぐ。

テストダブル方針は test_backfill_evidence.py の CliEvidenceTest に合わせる:
  - backfill.RUN_ONE をまるごと差し替える（サブプロセスを起動しない）。
  - env だけでなく ledger.FACTS_DIR / fetch_content.FACTS_DIR も差し替える
    （import 時に1回だけ解決するモジュール定数のため）。
  - RUN_ONE の中では本物の fetch_content.record_facts() を使い、本番と同じ
    書き込み経路（attempt_seq の引き継ぎ等）を通す。
"""

import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import backfill  # noqa: E402
import ledger  # noqa: E402
import fetch_content  # noqa: E402


def _seed_candidate(facts_dir, seed_day, rid, url):
    """target とは別日に、rid持ち・open・非permanentな失敗レコードを1件置く
    （test_backfill_recovery.py の test_a と同じ形。fail_reason=timeout は
    _FAIL_REASON_KIND に無いコードなので unknown 扱い＝open のまま残る）。"""
    record = {
        "fetched_at": seed_day + "T00:00:00Z",
        "fetcher_version": "test",
        "url": url,
        "route": "x",
        "ok": False,
        "depth": "none",
        "missing": ["fetch_failed"],
        "fail_reason": "timeout",
        "reason": "接続タイムアウト",
        "raindrop_id": rid,
        "rid_source": "exact",
        "attempt_seq": 1,
    }
    path = os.path.join(facts_dir, "%s.json" % seed_day)
    store = {}
    if os.path.exists(path):
        with io.open(path, encoding="utf-8") as f:
            store = json.load(f)
    store[url] = record
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(store, ensure_ascii=False, indent=1) + "\n")


class QuotaStopTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bf-quota-")
        self.facts_dir = os.path.join(self.tmp, "fetch_facts")
        os.makedirs(self.facts_dir, exist_ok=True)

        self._old_env = os.environ.get("FETCH_FACTS_DIR")
        os.environ["FETCH_FACTS_DIR"] = self.facts_dir

        # ledger / fetch_content は FACTS_DIR を import 時に1回だけ解決するので、
        # env だけでは効かない（2026-09-18 に踏んだ事故と同じ穴。CliEvidenceTest 参照）。
        self._patched_mods = [
            (ledger, "FACTS_DIR", ledger.FACTS_DIR),
            (fetch_content, "FACTS_DIR", fetch_content.FACTS_DIR),
        ]
        ledger.FACTS_DIR = self.facts_dir
        fetch_content.FACTS_DIR = self.facts_dir

        self._old_run_one = backfill.RUN_ONE
        self.calls = []

        self.seed_day = "2026-01-10"
        self.target = "2026-01-15"
        self.target_plus1 = "2026-01-16"
        self.rids = [999101, 999102, 999103]
        self.urls = [
            "https://example.com/quota-candidate-1",
            "https://example.com/quota-candidate-2",
            "https://example.com/quota-candidate-3",
        ]
        self.url_to_rid = dict(zip(self.urls, self.rids))
        for rid, url in zip(self.rids, self.urls):
            _seed_candidate(self.facts_dir, self.seed_day, rid, url)

        # シードが実際に候補として抽出されることを確認してから本題に入る
        # （抽出できていなければテスト自体が無意味になる。既存テストの慣習に合わせる）。
        pre_candidates = ledger.backfill_candidates(self.target)
        self.assertEqual(len(pre_candidates), 3,
                          "シード3件が候補として抽出されていない（テスト自体が無意味になる）")

    def tearDown(self):
        backfill.RUN_ONE = self._old_run_one
        for mod, attr, old in self._patched_mods:
            setattr(mod, attr, old)
        if self._old_env is None:
            os.environ.pop("FETCH_FACTS_DIR", None)
        else:
            os.environ["FETCH_FACTS_DIR"] = self._old_env

    def _make_run_one(self, quota_urls):
        """quota_urls に含まれるURLは video_reason=gemini_quota で失敗、それ以外は
        成功（depth=full）で record_facts() を使って書く（本番と同じ経路）。"""
        def fake_run_one(url, timeout, env):
            self.calls.append(url)
            rid = self.url_to_rid[url]
            if url in quota_urls:
                facts = {
                    "fetched_at": "2026-01-15T00:00:00Z",
                    "fetcher_version": "test",
                    "url": url,
                    "route": "x",
                    "ok": False,
                    "depth": "none",
                    "missing": ["video_content"],
                    "video_reason": "gemini_quota",
                    "raindrop_id": rid,
                    "rid_source": "exact",
                }
            else:
                facts = {
                    "fetched_at": "2026-01-15T00:00:00Z",
                    "fetcher_version": "test",
                    "url": url,
                    "route": "x",
                    "ok": True,
                    "depth": "full",
                    "missing": [],
                    "video_reason": "",
                    "raindrop_id": rid,
                    "rid_source": "exact",
                }
            fetch_content.record_facts(facts, day=env.get("FACTS_DATE", self.target))
        return fake_run_one


class QuotaStopsRemainingCandidatesTest(QuotaStopTestBase):
    """1件目で gemini_quota が返ったら、2件目以降は起動しない。"""

    def test_quota_hit_on_first_stops_rest(self):
        backfill.RUN_ONE = self._make_run_one(quota_urls={self.urls[0]})

        buf = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(buf):
            rc = backfill.run(target=self.target, limit=5, max_attempts=3,
                               dry_run=False, timeout=30)
        self.assertEqual(rc, 0)
        output = buf.getvalue()

        # 1件目しか起動していない
        self.assertEqual(self.calls, [self.urls[0]])

        import re
        m = re.search(r"BACKFILL_STATUS:.*$", output, re.M)
        self.assertIsNotNone(m)
        fields = dict(re.findall(r"(\w+)=(\S+)", m.group(0)))
        self.assertEqual(fields["candidates"], "3")
        self.assertEqual(fields["attempted"], "1")
        self.assertEqual(fields["quota_stopped"], "2")
        self.assertEqual(fields["budget_stopped"], "0")

        # 起動しなかった2件は試行回数を消費していないので、翌晩も候補に残る
        post_candidates = ledger.backfill_candidates(self.target_plus1)
        post_urls = {g["latest"].get("url") for g in post_candidates}
        self.assertIn(self.urls[1], post_urls)
        self.assertIn(self.urls[2], post_urls)
        for g in post_candidates:
            if g["latest"].get("url") in (self.urls[1], self.urls[2]):
                self.assertEqual(g["attempt_count"], 1,
                                  "起動しなかった候補の attempt_count が変化している")

    def test_run_evidence_records_quota_stopped(self):
        """fetch_facts/runs/<日付>.json にも quota_stopped が残る（BACKFILL_STATUS と
        二重に持つ設計。ランのログが読めなくても成果物側から分かること）。"""
        backfill.RUN_ONE = self._make_run_one(quota_urls={self.urls[0]})
        buf = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(buf):
            backfill.run(target=self.target, limit=5, max_attempts=3,
                         dry_run=False, timeout=30)
        runs_path = os.path.join(self.facts_dir, "runs", "%s.json" % self.target)
        with io.open(runs_path, encoding="utf-8") as f:
            runs = json.load(f)["runs"]
        self.assertEqual(runs[-1]["quota_stopped"], 2)


class NonQuotaFailureDoesNotStopTest(QuotaStopTestBase):
    """gemini_quota 以外の失敗（例: 通常の一過性失敗）では止まらない。"""

    def test_non_quota_failure_does_not_stop(self):
        # quota_urls を空にする＝どのURLも gemini_quota を返さない（全部成功）
        backfill.RUN_ONE = self._make_run_one(quota_urls=set())

        buf = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(buf):
            rc = backfill.run(target=self.target, limit=5, max_attempts=3,
                               dry_run=False, timeout=30)
        self.assertEqual(rc, 0)
        output = buf.getvalue()

        # 3件とも起動している（quota で止まっていない）
        self.assertEqual(self.calls, self.urls)

        import re
        m = re.search(r"BACKFILL_STATUS:.*$", output, re.M)
        fields = dict(re.findall(r"(\w+)=(\S+)", m.group(0)))
        self.assertEqual(fields["candidates"], "3")
        self.assertEqual(fields["attempted"], "3")
        self.assertEqual(fields["quota_stopped"], "0")


if __name__ == "__main__":
    unittest.main()
