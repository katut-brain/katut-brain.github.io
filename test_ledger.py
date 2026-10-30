#!/usr/bin/env python3
# test_ledger.py — ledger.py の回帰テスト（2026-09-24 legacy_migrated 対応で追加）。
#
# migrate_legacy_rid.py が付与する raindrop_id/rid_source="legacy_migrated" が
# 通常の rid グループへ合流すること、legacy_rid_unresolved が summary に
# 理由別件数で出ること、既存の --include-legacy の挙動を壊していないことを検査する。
# FETCH_FACTS_DIR を一時ディレクトリへ差し替えて本物の関数に通す（標準ライブラリの
# unittest のみ。他のテストと同じ流儀）。

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

import ledger  # noqa: E402


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=1))


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._orig_facts_dir = ledger.FACTS_DIR
        ledger.FACTS_DIR = self.tmpdir.name
        self._orig_include_legacy = ledger.INCLUDE_LEGACY
        ledger.INCLUDE_LEGACY = False

    def tearDown(self):
        ledger.FACTS_DIR = self._orig_facts_dir
        ledger.INCLUDE_LEGACY = self._orig_include_legacy
        self.tmpdir.cleanup()

    def _write_day(self, day, records):
        _write_json(os.path.join(self.tmpdir.name, "%s.json" % day), records)


class TestLegacyMigratedGrouping(LedgerTestBase):
    def test_legacy_migrated_record_joins_rid_group(self):
        """migrate_legacy_rid.py が付けた raindrop_id は int なので、
        --include-legacy を付けなくても通常の rid グループに合流する。"""
        self._write_day("2026-08-23", {
            "https://x.com/a/status/1": {
                "url": "https://x.com/a/status/1",
                "ok": True, "depth": "full", "missing": [],
                "fetched_at": "2026-08-23T00:00:00Z",
                "raindrop_id": 999,
                "rid_source": "legacy_migrated",
                "rid_evidence": {"method": "strong",
                                  "api_checked_at": "2026-09-24T00:00:00Z",
                                  "card_review_date": "2026-08-23"},
            }
        })
        records = ledger._load_all_records()
        groups = ledger._build_groups(records)
        self.assertIn(("rid", 999), groups)
        g = groups[("rid", 999)]
        self.assertEqual(g["state"], "resolved")

    def test_backfill_candidates_excludes_legacy_migrated_rid(self):
        """backfill.py の前提『rid無しの旧レコードには一切触らない』は、
        migrate_legacy_rid.py が事後にridを付与した後も守る。rid_source=="legacy_migrated"
        のグループはバックフィル対象から除外する（rid グループ集計自体には残る。
        2026-09-24 P1-1 ユーザー裁定: 回収対象にしない）。"""
        self._write_day("2026-08-23", {
            "https://x.com/a/status/1": {
                "url": "https://x.com/a/status/1",
                "ok": False, "depth": None, "missing": ["fetch_failed"],
                "fetched_at": "2026-08-23T00:00:00Z",
                "raindrop_id": 999,
                "rid_source": "legacy_migrated",
                "fail_reason": "timeout",
            }
        })
        cands = ledger.backfill_candidates("2026-08-24")
        rids = [g["key"][1] for g in cands]
        self.assertNotIn(999, rids)
        # 一方で rid グループの集計(_build_groups)には残る（ledger --summaryの
        # rid_source内訳から消えてはいけない）。
        records = ledger._load_all_records()
        groups = ledger._build_groups(records)
        self.assertIn(("rid", 999), groups)


class TestSummaryLegacyUnresolved(LedgerTestBase):
    def test_summary_counts_legacy_rid_unresolved_by_reason(self):
        self._write_day("2026-08-23", {
            "https://x.com/a/status/1": {
                "url": "https://x.com/a/status/1", "ok": True, "depth": "full",
                "missing": [], "fetched_at": "2026-08-23T00:00:00Z",
                "legacy_rid_unresolved": "raindrop_missing",
                "checked_at": "2026-09-24T00:00:00Z",
            },
            "https://x.com/a/status/2": {
                "url": "https://x.com/a/status/2", "ok": True, "depth": "full",
                "missing": [], "fetched_at": "2026-08-23T00:00:00Z",
                "legacy_rid_unresolved": "raindrop_missing",
                "checked_at": "2026-09-24T00:00:00Z",
            },
            "https://x.com/a/status/3": {
                "url": "https://x.com/a/status/3", "ok": True, "depth": "full",
                "missing": [], "fetched_at": "2026-08-23T00:00:00Z",
                "legacy_rid_unresolved": "card_rid_mismatch",
                "checked_at": "2026-09-24T00:00:00Z",
            },
        })
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ledger.cmd_summary()
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("raindrop_missing: 2", out)
        self.assertIn("card_rid_mismatch: 1", out)

    def test_summary_shows_none_when_no_legacy_unresolved(self):
        self._write_day("2026-08-23", {
            "https://x.com/a/status/1": {
                "url": "https://x.com/a/status/1", "ok": True, "depth": "full",
                "missing": [], "fetched_at": "2026-08-23T00:00:00Z",
                "raindrop_id": 1, "rid_source": "exact",
            },
        })
        buf = io.StringIO()
        with redirect_stdout(buf):
            ledger.cmd_summary()
        self.assertIn("(無し)", buf.getvalue())

    def test_legacy_migrated_excluded_from_reproducibility_warning(self):
        """legacy_migrated は再現可能な確定結果なので、captures.json救済の
        再現性警告（legacy_<元source>向け）を誤って出さない。"""
        self._write_day("2026-08-23", {
            "https://x.com/a/status/1": {
                "url": "https://x.com/a/status/1", "ok": True, "depth": "full",
                "missing": [], "fetched_at": "2026-08-23T00:00:00Z",
                "raindrop_id": 999, "rid_source": "legacy_migrated",
            },
        })
        buf = io.StringIO()
        with redirect_stdout(buf):
            ledger.cmd_summary()
        out = buf.getvalue()
        self.assertIn("legacy_migrated: 1", out)
        self.assertNotIn("⚠️ legacy_<元source>", out)

    def test_include_legacy_flag_still_resolves_via_captures(self):
        """--include-legacy の既存挙動（captures.json 経由の都度再解決）は
        legacy_migrated 追加後も壊れていない。"""
        captures_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(captures_dir, ignore_errors=True))
        captures_path = os.path.join(captures_dir, "captures.json")
        _write_json(captures_path, [
            {"rid": 555, "source": "https://x.com/a/status/2"},
        ])
        self._write_day("2026-08-23", {
            "https://x.com/a/status/2": {
                "url": "https://x.com/a/status/2", "ok": True, "depth": "full",
                "missing": [], "fetched_at": "2026-08-23T00:00:00Z",
            },
        })
        orig_captures_path = None
        import fetch_content
        orig_captures_path = fetch_content.CAPTURES_PATH
        orig_cache = fetch_content._RID_INDEX_CACHE
        fetch_content.CAPTURES_PATH = captures_path
        fetch_content._RID_INDEX_CACHE = None
        try:
            ledger.INCLUDE_LEGACY = True
            buf = io.StringIO()
            with redirect_stdout(buf):
                ledger.cmd_summary()
            out = buf.getvalue()
            self.assertIn("legacy_exact: 1", out)
        finally:
            fetch_content.CAPTURES_PATH = orig_captures_path
            fetch_content._RID_INDEX_CACHE = orig_cache


class TestImageReasonTransient(LedgerTestBase):
    """X 添付画像の読み取りが一過性の理由で失敗した記録(image_reason あり)は取り直す。
    試行は既存の上限(DEFAULT_MAX_ATTEMPTS=3)で止まる。image_reason の無い旧記録は従来どおり。"""

    URL = "https://x.com/a/status/9"

    def _rec(self, day, reason, rid=77, seq=1):
        rec = {"url": self.URL, "route": "x", "ok": True, "depth": "partial",
               "missing": ["image_content"], "fetched_at": "%sT19:00:00Z" % day,
               "raindrop_id": rid, "rid_source": "exact", "attempt_seq": seq}
        if reason is not None:
            rec["image_reason"] = reason
        return {self.URL: rec}

    def _cands(self):
        return [g["key"][1] for g in ledger.backfill_candidates("2026-10-20")]

    def test_transient_reason_is_candidate(self):
        for reason in ("exception:ServerError:503", "image_download_failed", "partial_download",
                       "deadline_skipped", "gemini_quota"):
            self.assertEqual(ledger._reason_kind(self._rec("2026-10-10", reason)[self.URL]), "transient", reason)
        self._write_day("2026-10-10", self._rec("2026-10-10", "exception:ServerError:503"))
        self.assertEqual(self._cands(), [77])

    def test_stops_at_existing_attempt_cap(self):
        for d, seq in (("2026-10-10", 1), ("2026-10-11", 1), ("2026-10-12", 1)):
            self._write_day(d, self._rec(d, "exception:ServerError:503", seq=seq))
        g = ledger._build_groups(ledger._load_all_records())[("rid", 77)]
        self.assertEqual(g["attempt_count"], 3)
        self.assertTrue(g["exhausted"])
        self.assertEqual(self._cands(), [])

    def test_permanent_reasons_and_legacy_records_stay_permanent(self):
        for reason in ("host_not_allowed", "too_large", "gemini_unavailable", None, ""):
            self.assertEqual(ledger._reason_kind(self._rec("2026-10-10", reason)[self.URL]), "permanent", reason)
        self._write_day("2026-10-10", self._rec("2026-10-10", None))
        self.assertEqual(self._cands(), [])


class TestGeminiOutageWindow(LedgerTestBase):
    """2026-10-06〜10-13 の gemini_unavailable（import 失敗という環境障害）だけを
    取り直し対象にする。期間外・既存の permanent は変えない。"""

    URL = "https://x.com/a/status/1"

    def _rec(self, day, rid=11, reason="gemini_unavailable", url=None, route="x",
             missing=("video_content",), ok=True, depth="partial"):
        url = url or self.URL
        return {url: {
            "url": url, "route": route, "ok": ok, "depth": depth,
            "missing": list(missing), "video_reason": reason,
            "fetched_at": "%sT19:00:00Z" % day, "raindrop_id": rid,
            "rid_source": "exact", "attempt_seq": 1,
        }}

    def _cands(self):
        return [g["key"][1] for g in ledger.backfill_candidates("2026-10-20")]

    def test_outage_days_are_transient_and_candidates(self):
        for day in ("2026-10-06", "2026-10-13"):          # 窓の両端を含む
            self._write_day(day, self._rec(day, rid=int(day[-2:]),
                                           url=self.URL + day))
        self.assertEqual(sorted(self._cands()), [6, 13])

    def test_outside_window_stays_permanent(self):
        for day, rid in (("2026-10-05", 5), ("2026-10-14", 14)):    # 窓のすぐ外
            self._write_day(day, self._rec(day, rid=rid, url=self.URL + day))
        self.assertEqual(self._cands(), [])
        g = ledger._build_groups(ledger._load_all_records())[("rid", 5)]
        self.assertEqual(g["reason_kind"], "permanent")

    def test_outage_attempts_do_not_count_toward_exhausted(self):
        # ServerError 2回 + 窓内の gemini_unavailable 1回: 数えると3回で exhausted になる。
        self._write_day("2026-10-04", self._rec("2026-10-04", reason="exception:ServerError"))
        self._write_day("2026-10-05", self._rec("2026-10-05", reason="exception:ServerError"))
        self._write_day("2026-10-06", self._rec("2026-10-06"))
        g = ledger._build_groups(ledger._load_all_records())[("rid", 11)]
        self.assertEqual(g["attempt_count"], 2)
        self.assertFalse(g["exhausted"])
        self.assertEqual(self._cands(), [11])

    def test_real_exhaustion_still_applies(self):
        for d in ("2026-10-02", "2026-10-03", "2026-10-04"):
            self._write_day(d, self._rec(d, reason="exception:ServerError"))
        self._write_day("2026-10-06", self._rec("2026-10-06"))
        g = ledger._build_groups(ledger._load_all_records())[("rid", 11)]
        self.assertTrue(g["exhausted"])
        self.assertEqual(self._cands(), [])

    def test_instagram_reel_in_window_is_candidate(self):
        url = "https://www.instagram.com/reel/AbC/"
        self._write_day("2026-10-07", self._rec("2026-10-07", url=url, route="instagram"))
        self.assertEqual(self._cands(), [11])

    def test_existing_permanents_unchanged(self):
        # 窓内でも gemini_unavailable 以外の permanent コードは変わらない。
        self._write_day("2026-10-07", self._rec("2026-10-07", rid=1, reason="too_large",
                                                url=self.URL + "tl"))
        # 理由コードを持たない旧 Instagram Reel の構造的断念。
        reel = "https://www.instagram.com/reel/Old/"
        self._write_day("2026-09-01", self._rec("2026-09-01", rid=2, url=reel,
                                                route="instagram", reason=""))
        # 構造的な穴だけの欠損。
        self._write_day("2026-10-08", self._rec("2026-10-08", rid=3, reason="",
                                                url="https://example.com/p", route="web",
                                                missing=("image_content",)))
        groups = ledger._build_groups(ledger._load_all_records())
        for rid in (1, 2, 3):
            self.assertEqual(groups[("rid", rid)]["reason_kind"], "permanent", rid)
        self.assertEqual(self._cands(), [])


if __name__ == "__main__":
    unittest.main()
