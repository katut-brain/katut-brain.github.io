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


if __name__ == "__main__":
    unittest.main()
