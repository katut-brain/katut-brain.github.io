#!/usr/bin/env python3
# test_select_targets.py — select_targets.py の回帰テスト。
#
# 本番の選定関数（select_targets.select / .load_captures / .reviewed_index）を
# 一時ディレクトリに作った captures.json / reviews/*.html へ実際に通す
# （モックで差し替えない）。標準ライブラリの unittest のみを使う
# （test_backfill_recovery.py 等と同じ流儀）。

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
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
    def test_raindrop_id_only_is_treated_as_rid_missing(self):
        """2026-09-22 Codexレビュー2周目 指摘: "rid" フィールドのみを正として
        扱い、"raindrop_id" へはフォールバックしない（build_capture_index.py /
        stale-check.yml の台帳系と規則を揃える）。"raindrop_id" しか無い
        レコードは rid 無しとして synthetic_rid の経路へ入る。
        """
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "captures.json")
            _write_captures(path, [
                {"raindrop_id": 12345, "date": "2026-06-01",
                 "source": "https://x/12345"},
            ])
            records, synth_count = select_targets.load_captures(path)
            self.assertEqual(synth_count, 1)
            rid = records[0][0]
            self.assertLess(rid, 0)
            self.assertNotEqual(rid, 12345)

    def test_rid_missing_gets_synthetic_negative_rid(self):
        """rid の無いレコードは synthetic_rid() の負整数を振って選定対象に
        含める（2026-09-22 CEO裁定: 永久除外しない）。
        """
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "captures.json")
            _write_captures(path, [
                {"rid": 1, "date": "2026-06-01T00:00:00Z"},
                {"rid": 2, "date": "2026-06-02"},
                {"date": "2026-06-03", "source": "https://x/no-rid"},  # rid 無し
                {"rid": "x", "date": "2026-06-04"},  # rid が非整数 -> 合成
                {"rid": 3, "date": "not-a-date"},  # 不正な日付は除外
            ])
            records, synth_count = select_targets.load_captures(path)
            rids = sorted(r for r, _d, _rec in records)
            self.assertEqual(synth_count, 2)
            # rid=1,2 はそのまま。残り2件は負の合成ID。
            positive = [r for r in rids if r > 0]
            negative = [r for r in rids if r < 0]
            self.assertEqual(positive, [1, 2])
            self.assertEqual(len(negative), 2)

    def test_synthetic_rid_is_stable(self):
        rec = {"date": "2026-06-03", "source": "https://x/no-rid"}
        self.assertEqual(select_targets.synthetic_rid(rec),
                          select_targets.synthetic_rid(dict(rec)))


class TestReviewedIndex(unittest.TestCase):
    def test_extraction_quotes_multiple_cards_negative(self):
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            _write(os.path.join(reviews, "2026-06-01.html"), """
                <div class="vcard"><button data-rid="111">a</button></div>
                <div class="vcard"><button data-rid='222'>b</button></div>
                <div class="vcard"><button data-rid="-333">c</button></div>
            """)
            rids, url_keys, post_id_keys, unreadable = \
                select_targets.reviewed_index(reviews)
            self.assertEqual(rids, {111, 222, -333})
            self.assertEqual(url_keys, set())
            self.assertEqual(post_id_keys, set())
            self.assertEqual(unreadable, [])

    def test_unreadable_file_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            path = os.path.join(reviews, "2026-06-02.html")
            # 壊れたUTF-8バイト列を書く（UnicodeDecodeErrorを起こす）。
            with open(path, "wb") as f:
                f.write(b"<div data-rid=\"1\">\xff\xfe broken</div>")
            rids, url_keys, post_id_keys, unreadable = \
                select_targets.reviewed_index(reviews)
            self.assertEqual(rids, set())
            self.assertEqual(url_keys, set())
            self.assertEqual(post_id_keys, set())
            self.assertEqual(unreadable, ["2026-06-02.html"])

    def test_missing_reviews_dir(self):
        rids, url_keys, post_id_keys, unreadable = \
            select_targets.reviewed_index("/no/such/dir/xyz")
        self.assertEqual(rids, set())
        self.assertEqual(url_keys, set())
        self.assertEqual(post_id_keys, set())
        self.assertEqual(unreadable, [])

    def test_ridless_generic_card_href_becomes_url_key(self):
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            _write(os.path.join(reviews, "2026-06-01.html"),
                   '<div class="vcard"><a class="vlink" '
                   'href="https://example.com/foo/bar"></a></div>')
            rids, url_keys, post_id_keys, _unreadable = \
                select_targets.reviewed_index(reviews)
            self.assertEqual(rids, set())
            self.assertEqual(url_keys, {("generic", "https://example.com/foo/bar")})
            self.assertEqual(post_id_keys, set())

    def test_ridless_card_href_becomes_post_id_key(self):
        """強いIDキー（X等）を持つ href は post_id_keys に入る
        （url_keys には入らない。generic種別のみ url_keys）。
        """
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            _write(os.path.join(reviews, "2026-06-01.html"),
                   '<div class="vcard"><a class="vlink" '
                   'href="https://x.com/user/status/1757864748"></a></div>')
            rids, url_keys, post_id_keys, _unreadable = \
                select_targets.reviewed_index(reviews)
            self.assertEqual(rids, set())
            self.assertEqual(url_keys, set())
            self.assertEqual(post_id_keys, {("x", "1757864748")})

    def test_p1a_generic_card_with_data_rid_href_is_not_added_to_url_keys(self):
        """P1-a（generic種別に引き続き適用）: data-rid を持つカードの
        generic種別 href は URL照合に混ぜない。別の rid を持つカードが
        同じ generic URL を指しているだけで「掲載済み」と誤判定しては
        いけない（強いIDキーはこの制限を受けない。別テストを参照）。
        """
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            _write(os.path.join(reviews, "2026-06-01.html"),
                   '<div class="vcard"><a class="vlink" '
                   'href="https://example.com/foo/bar"></a>'
                   '<button data-rid="999"></button></div>')
            rids, url_keys, post_id_keys, _unreadable = \
                select_targets.reviewed_index(reviews)
            self.assertEqual(rids, {999})
            self.assertEqual(url_keys, set(),
                              "data-ridを持つカードのgeneric hrefはURLキー集合に入れない")
            self.assertEqual(post_id_keys, set())

    def test_strong_id_key_from_card_with_data_rid_is_added_to_post_id_keys(self):
        """強いIDキーは data-rid を持つカードの href からも集める
        （2026-09-22 ユーザー裁定: 同じ投稿の再保存は重複とみなす。
        別rid同士でも強いIDキーが一致すれば「同じ投稿」とみなしてよい）。
        """
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            _write(os.path.join(reviews, "2026-06-01.html"),
                   '<div class="vcard"><a class="vlink" '
                   'href="https://x.com/user/status/1757864748"></a>'
                   '<button data-rid="999"></button></div>')
            rids, url_keys, post_id_keys, _unreadable = \
                select_targets.reviewed_index(reviews)
            self.assertEqual(rids, {999})
            self.assertEqual(post_id_keys, {("x", "1757864748")})

    def test_card_index_preserves_per_date_info(self):
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            _write(os.path.join(reviews, "2026-06-01.html"),
                   '<div class="vcard"><button data-rid="111"></button></div>')
            _write(os.path.join(reviews, "2026-06-02.html"),
                   '<div class="vcard"><a class="vlink" '
                   'href="https://example.com/foo/bar"></a></div>')
            rid_dates, url_dates, post_id_dates, _unreadable = \
                select_targets.card_index(reviews)
            self.assertEqual(rid_dates, {111: {"2026-06-01"}})
            self.assertEqual(url_dates,
                              {("generic", "https://example.com/foo/bar"):
                               {"2026-06-02"}})
            self.assertEqual(post_id_dates, {})

    def test_ridless_generic_url_on_or_after_fallback_cutoff_is_not_used(self):
        """2026-09-22 Codexレビュー2周目 指摘: generic種別のURL照合
        フォールバックは URL_FALLBACK_BEFORE（2026-08-04）より前の reviews
        の、data-rid 無しカードだけが対象。8/4のretrofit以降はカードに必ず
        data-ridが付く前提なので、それ以降の日付のridless cardをURL照合に
        使わない（この制限は generic種別のみ。強いIDキーには適用しない）。
        """
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            # ちょうど cutoff 当日と、それ以降の日付。どちらも対象外。
            _write(os.path.join(reviews, "2026-08-04.html"),
                   '<div class="vcard"><a class="vlink" '
                   'href="https://example.com/foo/bar"></a></div>')
            _write(os.path.join(reviews, "2026-09-14.html"),
                   '<div class="vcard"><a class="vlink" '
                   'href="https://example.com/baz/qux"></a></div>')
            rid_dates, url_dates, _post_id_dates, _unreadable = \
                select_targets.card_index(reviews)
            self.assertEqual(rid_dates, {})
            self.assertEqual(url_dates, {})

    def test_ridless_generic_url_before_fallback_cutoff_is_used(self):
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            _write(os.path.join(reviews, "2026-08-03.html"),
                   '<div class="vcard"><a class="vlink" '
                   'href="https://example.com/foo/bar"></a></div>')
            rid_dates, url_dates, _post_id_dates, _unreadable = \
                select_targets.card_index(reviews)
            self.assertEqual(url_dates,
                              {("generic", "https://example.com/foo/bar"):
                               {"2026-08-03"}})

    def test_strong_id_key_is_collected_regardless_of_data_rid_or_date(self):
        """2026-09-22 ユーザー裁定: 同じ投稿の再保存は重複とみなす。強い
        IDキー（X status ID等）は data-rid の有無・日付を問わず全カードの
        hrefから集める（post_id_dates）。
        """
        with tempfile.TemporaryDirectory() as d:
            reviews = os.path.join(d, "reviews")
            os.makedirs(reviews)
            # data-rid ありのカード。日付は cutoff より後。
            _write(os.path.join(reviews, "2026-09-14.html"),
                   '<div class="vcard"><a class="vlink" '
                   'href="https://x.com/user/status/1757864748"></a>'
                   '<button data-rid="999"></button></div>')
            rid_dates, url_dates, post_id_dates, _unreadable = \
                select_targets.card_index(reviews)
            self.assertEqual(rid_dates, {999: {"2026-09-14"}})
            self.assertEqual(url_dates, {})
            self.assertEqual(post_id_dates,
                              {("x", "1757864748"): {"2026-09-14"}})


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
                    "2026-06-01.html": '<div class="vcard"><button data-rid="4"></button></div>',
                },
            )
            result = select_targets.select(
                "2026-09-21", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"),
                since="2020-01-01")
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
                capture_index_path=os.path.join(d, "capture_index.json"),
                since="2020-01-01")
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
                reviews_dir=reviews_dir, capture_index_path=index_path,
                since="2020-01-01")
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
                capture_index_path=os.path.join(d, "capture_index.json"),
                since="2020-01-01")
            self.assertEqual(result["_unreadable_reviews"], ["2026-06-02.html"])


class TestSinceFilter(unittest.TestCase):
    def _setup(self, d, records, review_files=None):
        captures_path = os.path.join(d, "captures.json")
        _write_captures(captures_path, records)
        reviews_dir = os.path.join(d, "reviews")
        os.makedirs(reviews_dir, exist_ok=True)
        for name, content in (review_files or {}).items():
            _write(os.path.join(reviews_dir, name), content)
        return captures_path, reviews_dir

    def test_records_before_default_since_are_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(d, records=[
                {"rid": 1, "date": "2026-06-13"},  # before default SINCE
                {"rid": 2, "date": "2026-06-14"},  # on SINCE, included
            ])
            result = select_targets.select(
                "2026-06-15", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [2])
            self.assertEqual(result["_before_since_count"], 1)

    def test_select_since_env_override(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(d, records=[
                {"rid": 1, "date": "2026-06-13"},
                {"rid": 2, "date": "2026-06-14"},
            ])
            with unittest.mock.patch.dict(os.environ, {"SELECT_SINCE": "2026-06-13"}):
                result = select_targets.select(
                    "2026-06-15", captures_path=captures_path,
                    reviews_dir=reviews_dir,
                    capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [1, 2])
            self.assertEqual(result["_before_since_count"], 0)

    def test_select_since_env_invalid_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(d, records=[
                {"rid": 1, "date": "2026-06-13"},
                {"rid": 2, "date": "2026-06-14"},
            ])
            with unittest.mock.patch.dict(os.environ, {"SELECT_SINCE": "not-a-date"}):
                result = select_targets.select(
                    "2026-06-15", captures_path=captures_path,
                    reviews_dir=reviews_dir,
                    capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [2])
            self.assertEqual(result["_before_since_count"], 1)


class TestUrlKey(unittest.TestCase):
    def test_x_status_id(self):
        self.assertEqual(
            select_targets.url_key("https://x.com/user/status/1757864748"),
            ("x", "1757864748"))
        self.assertEqual(
            select_targets.url_key("https://twitter.com/user/status/1757864748?s=12"),
            ("x", "1757864748"))

    def test_x_without_status_returns_none(self):
        self.assertIsNone(select_targets.url_key("https://x.com/someuser"))

    def test_instagram_shortcode(self):
        self.assertEqual(
            select_targets.url_key("https://www.instagram.com/reel/AbC123xy/"),
            ("instagram", "AbC123xy"))
        self.assertEqual(
            select_targets.url_key("https://instagram.com/p/AbC123xy/?utm=1"),
            ("instagram", "AbC123xy"))

    def test_threads_post_id(self):
        self.assertEqual(
            select_targets.url_key("https://www.threads.net/@user/post/DEF456"),
            ("threads", "DEF456"))

    def test_generic_scheme_host_path_drops_only_tracking_query(self):
        """フラグメントは除去し、追跡用でないクエリ(q=1)は残す
        （2026-09-22 Codexレビュー2周目 指摘対応）。
        """
        self.assertEqual(
            select_targets.url_key("https://Example.com/foo/bar/?q=1#frag"),
            ("generic", "https://example.com/foo/bar?q=1"))

    def test_generic_query_id_1_and_id_2_do_not_match(self):
        """`?id=1` と `?id=2` は別物としてキーが分かれる（クエリを全部
        落としていた旧実装のリグレッション防止）。
        """
        k1 = select_targets.url_key("https://example.com/foo/bar?id=1")
        k2 = select_targets.url_key("https://example.com/foo/bar?id=2")
        self.assertIsNotNone(k1)
        self.assertIsNotNone(k2)
        self.assertNotEqual(k1, k2)

    def test_generic_tracking_params_are_stripped(self):
        """utm_*・fbclid・gclid・igsh・img_index・s・t・ref・xmt は
        追跡用として落とされ、同じページを指すURLは一致する。
        """
        k1 = select_targets.url_key(
            "https://example.com/foo/bar?utm_source=x&fbclid=abc")
        k2 = select_targets.url_key("https://example.com/foo/bar")
        self.assertEqual(k1, k2)

    def test_generic_tracking_and_real_query_mixed(self):
        """追跡用パラメータと実質的なクエリが混在する場合、追跡用だけを
        落として実質的な方は残す。
        """
        k = select_targets.url_key(
            "https://example.com/foo/bar?id=1&utm_source=x")
        self.assertEqual(k, ("generic", "https://example.com/foo/bar?id=1"))

    def test_generic_root_path_returns_none(self):
        self.assertIsNone(select_targets.url_key("https://example.com/"))

    def test_none_and_empty(self):
        self.assertIsNone(select_targets.url_key(None))
        self.assertIsNone(select_targets.url_key(""))


class TestSelectUrlMatch(unittest.TestCase):
    def _setup(self, d, records, review_files):
        captures_path = os.path.join(d, "captures.json")
        _write_captures(captures_path, records)
        reviews_dir = os.path.join(d, "reviews")
        os.makedirs(reviews_dir, exist_ok=True)
        for name, content in review_files.items():
            _write(os.path.join(reviews_dir, name), content)
        return captures_path, reviews_dir

    def test_x_status_match_excludes_without_data_rid(self):
        """強いIDキー(X status ID)の一致は reviewed_by_post_id に数える
        （generic種別の reviewed_by_url とは別枠）。
        """
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(
                d,
                records=[{"rid": 1757864748, "date": "2026-06-14",
                          "source": "https://x.com/user/status/1757864748?s=12"}],
                review_files={
                    "2026-06-13.html": (
                        '<div class="vcard"><a class="vlink" '
                        'href="https://x.com/user/status/1757864748">t</a></div>'
                    ),
                },
            )
            result = select_targets.select(
                "2026-06-15", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [])
            self.assertEqual(result["_reviewed_by_post_id_count"], 1)
            self.assertEqual(result["_reviewed_by_url_count"], 0)

    def test_strong_id_match_excludes_even_with_different_data_rid_card(self):
        """2026-09-22 ユーザー裁定: 同じ投稿の再保存は重複とみなす。強い
        IDキー（X status ID等）は、それを持つカードが**別のrid**を持って
        いても一致すれば掲載済みとして除外する（Codex 1周目の懸念「別の
        記事なのに一致してしまう」は generic 側の制限で引き続き防ぐので、
        強いIDキーには適用しない。旧テスト
        test_url_shared_with_different_data_rid_card_does_not_exclude を
        新しい裁定に合わせて書き換えたもの）。
        """
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(
                d,
                records=[{"rid": 1757864748, "date": "2026-06-14",
                          "source": "https://x.com/user/status/1757864748?s=12"}],
                review_files={
                    "2026-06-13.html": (
                        '<div class="vcard"><a class="vlink" '
                        'href="https://x.com/user/status/1757864748">t</a>'
                        '<button data-rid="999"></button></div>'
                    ),
                },
            )
            result = select_targets.select(
                "2026-06-15", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [])
            self.assertEqual(result["_reviewed_by_post_id_count"], 1)

    def test_generic_url_shared_with_different_data_rid_card_does_not_exclude(self):
        """generic種別（P1-a）: 別の data-rid を持つカードが偶然同じ
        generic URL を指しているだけでは掲載済みにならない（そのカードは
        rid でのみ掲載判定される）。Codex 1周目の懸念「別の記事なのに一致
        してしまう」は generic 側でこのとおり引き続き防ぐ。
        """
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(
                d,
                records=[{"rid": 1, "date": "2026-06-14",
                          "source": "https://example.com/foo/bar"}],
                review_files={
                    "2026-06-13.html": (
                        '<div class="vcard"><a class="vlink" '
                        'href="https://example.com/foo/bar">t</a>'
                        '<button data-rid="999"></button></div>'
                    ),
                },
            )
            result = select_targets.select(
                "2026-06-15", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [1])
            self.assertEqual(result["_reviewed_by_url_count"], 0)
            self.assertEqual(result["_reviewed_by_post_id_count"], 0)

    def test_instagram_shortcode_match_ignores_query_diff(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(
                d,
                records=[{"rid": 1758030878, "date": "2026-06-14",
                          "source": "https://www.instagram.com/reel/AbC123xy/?igshid=xyz"}],
                review_files={
                    "2026-06-14.html": (
                        '<div class="vcard"><a class="vlink" '
                        'href="https://instagram.com/reel/AbC123xy/">t</a></div>'
                    ),
                },
            )
            result = select_targets.select(
                "2026-06-15", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [])
            self.assertEqual(result["_reviewed_by_post_id_count"], 1)

    def test_different_host_does_not_match(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(
                d,
                records=[{"rid": 1, "date": "2026-06-14",
                          "source": "https://example.com/foo/bar"}],
                review_files={
                    "2026-06-14.html": (
                        '<div class="vcard"><a class="vlink" '
                        'href="https://other.com/foo/bar">t</a></div>'
                    ),
                },
            )
            result = select_targets.select(
                "2026-06-15", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [1])
            self.assertEqual(result["_reviewed_by_url_count"], 0)

    def test_different_instagram_shortcode_does_not_match(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(
                d,
                records=[{"rid": 1, "date": "2026-06-14",
                          "source": "https://instagram.com/p/AAAA111/"}],
                review_files={
                    "2026-06-14.html": (
                        '<div class="vcard"><a class="vlink" '
                        'href="https://instagram.com/p/BBBB222/">t</a></div>'
                    ),
                },
            )
            result = select_targets.select(
                "2026-06-15", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [1])
            self.assertEqual(result["_reviewed_by_post_id_count"], 0)

    def test_1758030878_style_rid_drift_now_excluded_by_strong_id(self):
        """実データで見つかった rid ずれ（captures.json側1758030878、
        カード側data-rid=1758030881）の再現テスト。2026-09-22 ユーザー
        裁定後は、強いIDキー（Instagram shortcode）が一致するので
        「別rid」であっても掲載済みとして除外される。
        """
        with tempfile.TemporaryDirectory() as d:
            captures_path, reviews_dir = self._setup(
                d,
                records=[{"rid": 1758030878, "date": "2026-06-14",
                          "source": "https://www.instagram.com/p/DY5p4UKkuQc/"
                                    "?igsh=ZGQzMGo1ajJ4eHh4"}],
                review_files={
                    "2026-06-14.html": (
                        '<div class="vcard"><a class="vlink" '
                        'href="https://www.instagram.com/p/DY5p4UKkuQc/'
                        '?igsh=ZGQzMGo1ajJ4eHh4">t</a>'
                        '<button data-rid="1758030881"></button></div>'
                    ),
                },
            )
            result = select_targets.select(
                "2026-06-15", captures_path=captures_path,
                reviews_dir=reviews_dir,
                capture_index_path=os.path.join(d, "capture_index.json"))
            self.assertEqual(result["rids"], [])
            self.assertEqual(result["_reviewed_by_post_id_count"], 1)


class TestMain(unittest.TestCase):
    def test_status_line_emitted_and_exit_zero(self):
        with tempfile.TemporaryDirectory() as d:
            captures_path = os.path.join(d, "captures.json")
            _write_captures(captures_path, [{"rid": 1, "date": "2026-06-14"}])
            reviews_dir = os.path.join(d, "reviews")
            os.makedirs(reviews_dir)
            index_path = os.path.join(d, "capture_index.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = select_targets.main([
                    "--target", "2026-06-15",
                    "--captures", captures_path,
                    "--reviews-dir", reviews_dir,
                    "--capture-index", index_path,
                ])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("SELECT_STATUS: target=2026-06-15", out)
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
