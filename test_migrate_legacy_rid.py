#!/usr/bin/env python3
# test_migrate_legacy_rid.py — migrate_legacy_rid.py の回帰テスト。
#
# Raindrop API はモック（http_get を差し替える）。reviews/*.html・
# fetch_facts/*.json は一時ディレクトリに実物を書いて、本番の関数
# （resolve_record/apply_resolution/run）にそのまま通す。標準ライブラリの
# unittest のみを使う（他のテストと同じ流儀）。

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

import migrate_legacy_rid as mlr  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _write_json(path, obj):
    _write(path, json.dumps(obj, ensure_ascii=False, indent=1))


def _vcard(rid, href):
    rid_attr = (' data-rid="%d"' % rid) if rid is not None else ""
    return (
        '<div class="vcard"%s>'
        '<a class="vlink" href="%s">link</a>'
        '</div>'
    ) % (rid_attr, href)


def _review_html(cards):
    return "<html><body>%s</body></html>" % "".join(cards)


def _make_http_get(pages_by_url_substring=None, items=None, count=None):
    """テスト用の http_get。呼ばれるたびに (url, token) を受け取り、
    固定の {"count", "items"} を返す（ページングテストでは呼び出し回数で分岐）。
    """
    calls = []

    def http_get(url, token):
        calls.append(url)
        if pages_by_url_substring is not None:
            for substr, payload in pages_by_url_substring:
                if substr in url:
                    return payload
            return {"count": 0, "items": []}
        # 単一ページぶんのitemsとして扱う: page=0でのみ返し、以降は空にして
        # ページングループを自然に終端させる（実APIと同じ挙動）。
        if "page=0" in url:
            return {"count": count if count is not None else len(items or []),
                    "items": items or []}
        return {"count": count if count is not None else len(items or []), "items": []}

    http_get.calls = calls
    return http_get


class TestIdentityKey(unittest.TestCase):
    def test_x_status_strong_key_ignores_query(self):
        a = mlr.identity_key("https://x.com/foo/status/12345?s=12")
        b = mlr.identity_key("https://x.com/foo/status/12345")
        self.assertEqual(a, b)
        self.assertEqual(a[0], "strong")

    def test_generic_normalizes_via_fetch_content(self):
        a = mlr.identity_key("https://youtu.be/PKfZ1gnVJ44")
        b = mlr.identity_key("https://youtu.be/PKfZ1gnVJ44")
        self.assertEqual(a, b)
        self.assertEqual(a[0], "norm")


class TestFetchRaindrops(unittest.TestCase):
    def test_single_page_complete(self):
        http_get = _make_http_get(items=[{"_id": 1, "link": "https://x.com/a/status/1"}],
                                   count=1)
        items, complete, errors = mlr.fetch_raindrops("created:>2026-01-01", "tok",
                                                        http_get=http_get)
        self.assertEqual(len(items), 1)
        self.assertTrue(complete)
        self.assertEqual(errors, [])

    def test_pagination_follows_multiple_pages(self):
        """perpage=50 の天井を超える件数は複数ページに分けて全件取得する
        （raindrop-api-pagination-50limit.md の回帰）。"""
        page0_items = [{"_id": i, "link": "https://x.com/a/status/%d" % i}
                        for i in range(50)]
        page1_items = [{"_id": 100, "link": "https://x.com/a/status/100"}]

        def http_get(url, token):
            if "page=0" in url:
                return {"count": 51, "items": page0_items}
            if "page=1" in url:
                return {"count": 51, "items": page1_items}
            return {"count": 51, "items": []}

        items, complete, errors = mlr.fetch_raindrops("created:>2026-01-01", "tok",
                                                        http_get=http_get)
        self.assertEqual(len(items), 51)
        self.assertTrue(complete)

    def test_count_mismatch_reports_incomplete(self):
        http_get = _make_http_get(items=[{"_id": 1, "link": "https://x.com/a/status/1"}],
                                   count=5)
        items, complete, errors = mlr.fetch_raindrops("created:>2026-01-01", "tok",
                                                        http_get=http_get)
        self.assertFalse(complete)
        self.assertTrue(any("count不一致" in e for e in errors))


class TestResolveRecord(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.reviews_dir = os.path.join(self.tmpdir.name, "reviews")
        os.makedirs(self.reviews_dir, exist_ok=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_zero_candidates_is_raindrop_missing(self):
        r = mlr.resolve_record("https://x.com/a/status/123", "2026-08-23", [],
                                reviews_dir=self.reviews_dir)
        self.assertEqual(r["status"], "unresolved")
        self.assertEqual(r["reason"], "raindrop_missing")

    def test_multiple_candidates_is_raindrop_ambiguous(self):
        candidates = [
            {"_id": 1, "link": "https://x.com/a/status/123"},
            {"_id": 2, "link": "https://x.com/a/status/123?s=12"},
        ]
        r = mlr.resolve_record("https://x.com/a/status/123", "2026-08-23", candidates,
                                reviews_dir=self.reviews_dir)
        self.assertEqual(r["status"], "unresolved")
        self.assertEqual(r["reason"], "raindrop_ambiguous")
        self.assertEqual(sorted(r["candidate_rids"]), [1, 2])

    def test_single_candidate_confirmed_by_matching_card_rid(self):
        _write(os.path.join(self.reviews_dir, "2026-08-23.html"),
               _review_html([_vcard(999, "https://x.com/a/status/123")]))
        candidates = [{"_id": 999, "link": "https://x.com/a/status/123?s=12"}]
        r = mlr.resolve_record("https://x.com/a/status/123?s=12", "2026-08-23", candidates,
                                reviews_dir=self.reviews_dir)
        self.assertEqual(r["status"], "resolved")
        self.assertEqual(r["rid"], 999)
        self.assertEqual(r["card_review_date"], "2026-08-23")

    def test_card_rid_mismatch_is_unresolved(self):
        """カードのdata-ridがAPI一致結果と食い違う場合、確定させない
        （直前の別カードを誤って拾わないこと、を含めてhref一致だけを見る）。"""
        _write(os.path.join(self.reviews_dir, "2026-08-23.html"),
               _review_html([
                   _vcard(111, "https://x.com/other/status/999"),  # 別カード(誤って拾わない)
                   _vcard(222, "https://x.com/a/status/123"),      # 食い違うrid
               ]))
        candidates = [{"_id": 999, "link": "https://x.com/a/status/123"}]
        r = mlr.resolve_record("https://x.com/a/status/123", "2026-08-23", candidates,
                                reviews_dir=self.reviews_dir)
        self.assertEqual(r["status"], "unresolved")
        self.assertEqual(r["reason"], "card_rid_mismatch")
        self.assertEqual(r["api_rid"], 999)
        self.assertEqual(r["card_rid"], 222)

    def test_no_matching_card_still_confirms_via_api_alone(self):
        _write(os.path.join(self.reviews_dir, "2026-08-23.html"),
               _review_html([_vcard(111, "https://x.com/other/status/999")]))
        candidates = [{"_id": 999, "link": "https://x.com/a/status/123"}]
        r = mlr.resolve_record("https://x.com/a/status/123", "2026-08-23", candidates,
                                reviews_dir=self.reviews_dir)
        self.assertEqual(r["status"], "resolved")
        self.assertEqual(r["rid"], 999)
        self.assertIsNone(r["card_review_date"])

    def test_card_without_data_rid_does_not_block_confirmation(self):
        _write(os.path.join(self.reviews_dir, "2026-08-23.html"),
               _review_html([_vcard(None, "https://x.com/a/status/123")]))
        candidates = [{"_id": 999, "link": "https://x.com/a/status/123"}]
        r = mlr.resolve_record("https://x.com/a/status/123", "2026-08-23", candidates,
                                reviews_dir=self.reviews_dir)
        self.assertEqual(r["status"], "resolved")
        self.assertEqual(r["rid"], 999)
        self.assertIsNone(r["card_review_date"])

    def test_missing_review_file_still_confirms_via_api_alone(self):
        candidates = [{"_id": 999, "link": "https://x.com/a/status/123"}]
        r = mlr.resolve_record("https://x.com/a/status/123", "2026-08-23", candidates,
                                reviews_dir=self.reviews_dir)
        self.assertEqual(r["status"], "resolved")
        self.assertIsNone(r["card_review_date"])


class TestApplyAndIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.facts_dir = os.path.join(self.tmpdir.name, "fetch_facts")
        self.reviews_dir = os.path.join(self.tmpdir.name, "reviews")
        os.makedirs(self.facts_dir, exist_ok=True)
        os.makedirs(self.reviews_dir, exist_ok=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_facts_file(self, day, url, extra=None):
        rec = {"url": url, "ok": True, "route": "x", "fetched_at": "2026-08-23T00:00:00Z"}
        if extra:
            rec.update(extra)
        path = os.path.join(self.facts_dir, "%s.json" % day)
        _write_json(path, {url: rec})
        return path

    def test_apply_writes_expected_keys_and_preserves_existing(self):
        url = "https://x.com/a/status/123"
        path = self._make_facts_file("2026-08-23", url,
                                      {"missing": [], "depth": "full", "text_chars": 42})
        _write(os.path.join(self.reviews_dir, "2026-08-23.html"),
               _review_html([_vcard(999, url)]))
        http_get = _make_http_get(items=[{"_id": 999, "link": url}], count=1)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mlr.run(facts_dir=self.facts_dir, reviews_dir=self.reviews_dir,
                          apply=True, token="tok", http_get=http_get)
        self.assertEqual(rc, 0)

        with io.open(path, encoding="utf-8") as f:
            store = json.load(f)
        rec = store[url]
        # 既存キーは変えない
        self.assertEqual(rec["depth"], "full")
        self.assertEqual(rec["text_chars"], 42)
        self.assertEqual(rec["fetched_at"], "2026-08-23T00:00:00Z")
        # 新規キー
        self.assertEqual(rec["raindrop_id"], 999)
        self.assertEqual(rec["rid_source"], "legacy_migrated")
        self.assertEqual(rec["rid_evidence"]["method"], "strong")
        self.assertEqual(rec["rid_evidence"]["card_review_date"], "2026-08-23")
        self.assertIn("api_checked_at", rec["rid_evidence"])

    def test_apply_writes_unresolved_reason_and_checked_at(self):
        url = "https://x.com/a/status/123"
        path = self._make_facts_file("2026-08-23", url)
        http_get = _make_http_get(items=[], count=0)

        buf = io.StringIO()
        with redirect_stdout(buf):
            mlr.run(facts_dir=self.facts_dir, reviews_dir=self.reviews_dir,
                    apply=True, token="tok", http_get=http_get)

        with io.open(path, encoding="utf-8") as f:
            store = json.load(f)
        rec = store[url]
        self.assertEqual(rec["legacy_rid_unresolved"], "raindrop_missing")
        self.assertIn("checked_at", rec)
        self.assertNotIn("raindrop_id", rec)

    def test_rerun_is_idempotent(self):
        """既に raindrop_id / legacy_rid_unresolved を持つレコードは
        再実行しても一切変わらない（冪等）。"""
        url = "https://x.com/a/status/123"
        path = self._make_facts_file("2026-08-23", url,
                                      {"missing": [], "depth": "full"})
        _write(os.path.join(self.reviews_dir, "2026-08-23.html"),
               _review_html([_vcard(999, url)]))
        http_get = _make_http_get(items=[{"_id": 999, "link": url}], count=1)

        buf = io.StringIO()
        with redirect_stdout(buf):
            mlr.run(facts_dir=self.facts_dir, reviews_dir=self.reviews_dir,
                    apply=True, token="tok", http_get=http_get)
        with io.open(path, encoding="utf-8") as f:
            after_first = json.load(f)

        # 2回目: APIが違う結果を返しても(仮に)、既に処理済みのレコードは触らない
        http_get2 = _make_http_get(items=[{"_id": 888, "link": url}], count=1)
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            mlr.run(facts_dir=self.facts_dir, reviews_dir=self.reviews_dir,
                    apply=True, token="tok", http_get=http_get2)
        with io.open(path, encoding="utf-8") as f:
            after_second = json.load(f)

        self.assertEqual(after_first, after_second)
        self.assertEqual(after_second[url]["raindrop_id"], 999)

    def test_dry_run_does_not_write(self):
        url = "https://x.com/a/status/123"
        path = self._make_facts_file("2026-08-23", url)
        _write(os.path.join(self.reviews_dir, "2026-08-23.html"),
               _review_html([_vcard(999, url)]))
        http_get = _make_http_get(items=[{"_id": 999, "link": url}], count=1)

        with io.open(path, encoding="utf-8") as f:
            before = f.read()

        buf = io.StringIO()
        with redirect_stdout(buf):
            mlr.run(facts_dir=self.facts_dir, reviews_dir=self.reviews_dir,
                    apply=False, token="tok", http_get=http_get)

        with io.open(path, encoding="utf-8") as f:
            after = f.read()
        self.assertEqual(before, after)
        self.assertIn("--apply すると", buf.getvalue())


class TestCollectLegacyTargets(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.facts_dir = os.path.join(self.tmpdir.name, "fetch_facts")
        os.makedirs(self.facts_dir, exist_ok=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_skips_records_with_raindrop_id_or_unresolved_tag(self):
        _write_json(os.path.join(self.facts_dir, "2026-08-23.json"), {
            "https://a/1": {"raindrop_id": 1},
            "https://a/2": {"legacy_rid_unresolved": "raindrop_missing"},
            "https://a/3": {"ok": True},
        })
        targets = mlr.collect_legacy_targets(facts_dir=self.facts_dir)
        urls = [t[1] for t in targets]
        self.assertEqual(urls, ["https://a/3"])

    def test_since_until_filters_by_filename_date(self):
        _write_json(os.path.join(self.facts_dir, "2026-08-22.json"), {"https://a/1": {}})
        _write_json(os.path.join(self.facts_dir, "2026-08-23.json"), {"https://a/2": {}})
        _write_json(os.path.join(self.facts_dir, "2026-08-24.json"), {"https://a/3": {}})
        targets = mlr.collect_legacy_targets(facts_dir=self.facts_dir,
                                              since="2026-08-23", until="2026-08-23")
        urls = [t[1] for t in targets]
        self.assertEqual(urls, ["https://a/2"])


if __name__ == "__main__":
    unittest.main()
