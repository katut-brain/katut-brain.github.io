#!/usr/bin/env python3
# test_merge_review.py — merge_review.py の回帰テスト。
#
# 本番の merge()/main() を実際に通す（モックで差し替えない）。標準ライブラリ
# の unittest のみを使う（test_select_targets.py 等と同じ流儀）。

import datetime
import io
import os
import subprocess
import sys
import tempfile
import unittest

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import merge_review  # noqa: E402
import select_targets  # noqa: E402

SCRIPT = os.path.join(REPO_DIR, "merge_review.py")

NOW = datetime.datetime(2026, 9, 22, 10, 0, tzinfo=merge_review.JST)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _doc(*, meta="テーマA", summary_p="本文。", notes=True,
         cards='<div class="vcard"><a class="vlink" '
               'href="https://x.com/a/status/111"></a>'
               '<button data-rid="1"></button></div>',
         footer_extra="", li="既存item"):
    notes_block = ""
    if notes:
        notes_block = (
            '<h2>やりたい・気になったこと</h2>\n'
            '<section class="notes"><ul class="ilist"><li>%s</li></ul></section>\n'
            % li
        )
    return (
        '<!doctype html><html><body><div class="wrap">\n'
        '<div class="meta">%s</div>\n'
        '<section class="summary"><h2>まとめ</h2><p>%s</p></section>\n'
        '%s'
        '<div class="cards">\n%s\n</div>\n'
        '<footer>毎朝の同期で自動生成%s</footer>\n'
        '</div></body></html>'
    ) % (meta, summary_p, notes_block, cards, footer_extra)


class TestMerge(unittest.TestCase):
    def test_existing_cards_are_kept(self):
        existing = _doc()
        new = _doc(cards='<div class="vcard"><a class="vlink" '
                          'href="https://x.com/b/status/222"></a>'
                          '<button data-rid="2"></button></div>')
        merged, stats = merge_review.merge(existing, new, NOW)
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(stats["added"], 1)
        self.assertIn('data-rid="1"', merged)
        self.assertIn('data-rid="2"', merged)

    def test_duplicate_by_rid_is_skipped(self):
        existing = _doc()
        new = _doc(cards='<div class="vcard"><a class="vlink" '
                          'href="https://x.com/a/status/111-changed"></a>'
                          '<button data-rid="1"></button></div>')
        merged, stats = merge_review.merge(existing, new, NOW)
        self.assertEqual(stats["skipped_dup"], 1)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(merged.count('data-rid="1"'), 1)

    def test_duplicate_by_strong_id_key_is_skipped_even_with_different_rid(self):
        """2026-09-22 ユーザー裁定: 同じ投稿の再保存は重複とみなす。強い
        IDキー（Instagram shortcode）が一致すれば、rid が違っても重複扱い。
        """
        existing = _doc(cards='<div class="vcard"><a class="vlink" '
                               'href="https://www.instagram.com/p/DY5p4UKkuQc/">'
                               '</a><button data-rid="1758030881"></button></div>')
        new = _doc(cards='<div class="vcard"><a class="vlink" '
                          'href="https://www.instagram.com/p/DY5p4UKkuQc/">'
                          '</a><button data-rid="1758030878"></button></div>')
        merged, stats = merge_review.merge(existing, new, NOW)
        self.assertEqual(stats["skipped_dup"], 1)
        self.assertEqual(stats["added"], 0)

    def test_new_card_without_overlap_is_added_with_heading(self):
        existing = _doc()
        new = _doc(cards='<div class="vcard"><a class="vlink" '
                          'href="https://x.com/b/status/222"></a>'
                          '<button data-rid="2"></button></div>')
        merged, stats = merge_review.merge(existing, new, NOW)
        self.assertIn("追記（09/22 10:00 JST）", merged)
        self.assertLess(merged.index("</footer>"), len(merged))
        # 追記見出し・カードは既存カードの後、footer より前に来る。
        self.assertLess(merged.index('data-rid="1"'), merged.index("追記（"))
        self.assertLess(merged.index("追記（"), merged.index("</footer>"))

    def test_summary_p_appended_with_prefix(self):
        existing = _doc(summary_p="既存の本文。")
        new = _doc(summary_p="新しい視点の本文。")
        merged, _stats = merge_review.merge(existing, new, NOW)
        self.assertIn("<p>既存の本文。</p>", merged)
        self.assertIn("<p>追記 09/22 10:00 JST: 新しい視点の本文。</p>", merged)

    def test_meta_merges_new_tokens_only(self):
        existing = _doc(meta="テーマA・テーマB")
        new = _doc(meta="テーマB・テーマC")
        merged, _stats = merge_review.merge(existing, new, NOW)
        meta_block = merge_review.extract_blocks(merged, "div", "meta")[0]["raw"]
        self.assertIn("テーマA", meta_block)
        self.assertIn("テーマB", meta_block)
        self.assertIn("テーマC", meta_block)
        # テーマBは重複せず1回だけ。
        self.assertEqual(meta_block.count("テーマB"), 1)

    def test_notes_li_merged_dedup(self):
        existing = _doc(li="既存item")
        new = _doc(li="既存item")  # 完全一致=重複
        merged, _stats = merge_review.merge(existing, new, NOW)
        self.assertEqual(merged.count("<li>既存item</li>"), 1)

        new2 = _doc(li="新規item")
        merged2, _stats2 = merge_review.merge(existing, new2, NOW)
        self.assertIn("<li>既存item</li>", merged2)
        self.assertIn("<li>新規item</li>", merged2)

    def test_new_notes_section_appended_when_absent_in_existing(self):
        existing = _doc(notes=False)
        new_html = (
            '<!doctype html><html><body><div class="wrap">\n'
            '<div class="meta">テーマA</div>\n'
            '<section class="summary"><h2>まとめ</h2><p>本文。</p></section>\n'
            '<h2>やりたい・気になったこと</h2>\n'
            '<section class="notes"><ul class="ilist"><li>新規item</li></ul>'
            '</section>\n'
            '<div class="cards">\n'
            '<div class="vcard"><a class="vlink" '
            'href="https://x.com/a/status/111"></a>'
            '<button data-rid="1"></button></div>\n</div>\n'
            '<footer>毎朝の同期で自動生成</footer>\n'
            '</div></body></html>'
        )
        merged, _stats = merge_review.merge(existing, new_html, NOW)
        self.assertIn("新規item", merged)
        self.assertIn("やりたい・気になったこと", merged)

    def test_notegen_warn_union(self):
        existing = _doc(footer_extra='<p class="notegen-warn">A</p>')
        new = _doc(footer_extra='<p class="notegen-warn">A</p>'
                                 '<p class="notegen-warn">B</p>')
        merged, _stats = merge_review.merge(existing, new, NOW)
        self.assertEqual(merged.count('<p class="notegen-warn">A</p>'), 1)
        self.assertIn('<p class="notegen-warn">B</p>', merged)

    def test_run_timing_comment_from_new_is_discarded_existing_kept(self):
        existing = _doc(
            footer_extra='<!-- run-timing schema=v2 status=complete target=x -->')
        new = _doc(
            footer_extra='<!-- run-timing schema=v2 status=complete target=y -->')
        merged, _stats = merge_review.merge(existing, new, NOW)
        self.assertIn("target=x", merged)
        self.assertNotIn("target=y", merged)
        self.assertEqual(merged.count("<!-- run-timing"), 1)

    def test_added_cards_inserted_before_existing_run_timing_comment(self):
        """run_timing.py の insert_comment() は『</footer>直前にちょうど1件』
        を前提にしているため、追記物は既存の run-timing コメントより前に
        入らなければならない。
        """
        existing = _doc(
            footer_extra='<!-- run-timing schema=v2 status=complete target=x -->')
        new = _doc(cards='<div class="vcard"><a class="vlink" '
                          'href="https://x.com/b/status/222"></a>'
                          '<button data-rid="2"></button></div>')
        merged, _stats = merge_review.merge(existing, new, NOW)
        comment_pos = merged.index("<!-- run-timing")
        added_pos = merged.index("追記（")
        footer_close = merged.index("</footer>")
        self.assertLess(added_pos, comment_pos)
        self.assertLess(comment_pos, footer_close)

    def test_malformed_existing_raises_and_caller_must_not_write(self):
        """`</footer>` が無い/複数ある既存ファイルは統合できない
        （安全側で例外を投げる。呼び出し側 main() はこれを捕捉して
        既存ファイルへ一切書き込まない）。
        """
        broken = "<html><body>no footer at all</body></html>"
        new = _doc()
        with self.assertRaises(ValueError):
            merge_review.merge(broken, new, NOW)

        broken2 = "<footer>a</footer><footer>b</footer>"
        with self.assertRaises(ValueError):
            merge_review.merge(broken2, new, NOW)


class TestMainCLI(unittest.TestCase):
    def _run(self, args, expect=0):
        cmd = [sys.executable, SCRIPT] + args
        r = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, expect, r.stdout + r.stderr)
        return r.stdout

    def test_new_file_is_moved_as_is(self):
        with tempfile.TemporaryDirectory() as d:
            reviews_dir = os.path.join(d, "reviews")
            os.makedirs(reviews_dir)
            new_path = os.path.join(d, "new.html")
            _write(new_path, _doc())
            out = self._run(["--target", "2026-09-21", "--new", new_path,
                              "--reviews-dir", reviews_dir])
            self.assertIn("MERGE_STATUS: target=2026-09-21 mode=new "
                           "kept=0 added=1 skipped_dup=0", out)
            dest = os.path.join(reviews_dir, "2026-09-21.html")
            self.assertTrue(os.path.exists(dest))
            self.assertIn('data-rid="1"', _read(dest))

    def test_merge_existing_and_new_via_cli(self):
        with tempfile.TemporaryDirectory() as d:
            reviews_dir = os.path.join(d, "reviews")
            os.makedirs(reviews_dir)
            dest = os.path.join(reviews_dir, "2026-09-21.html")
            _write(dest, _doc())
            new_path = os.path.join(d, "new.html")
            _write(new_path, _doc(cards='<div class="vcard"><a class="vlink" '
                                         'href="https://x.com/b/status/222">'
                                         '</a><button data-rid="2"></button>'
                                         '</div>'))
            out = self._run(["--target", "2026-09-21", "--new", new_path,
                              "--reviews-dir", reviews_dir])
            self.assertIn("MERGE_STATUS: target=2026-09-21 mode=merged "
                           "kept=1 added=1 skipped_dup=0", out)
            content = _read(dest)
            self.assertIn('data-rid="1"', content)
            self.assertIn('data-rid="2"', content)

    def test_broken_existing_file_is_left_untouched_on_error(self):
        with tempfile.TemporaryDirectory() as d:
            reviews_dir = os.path.join(d, "reviews")
            os.makedirs(reviews_dir)
            dest = os.path.join(reviews_dir, "2026-09-21.html")
            broken_content = "<html><body>no footer here</body></html>"
            _write(dest, broken_content)
            new_path = os.path.join(d, "new.html")
            _write(new_path, _doc())
            out = self._run(["--target", "2026-09-21", "--new", new_path,
                              "--reviews-dir", reviews_dir], expect=1)
            self.assertIn("mode=error", out)
            # 既存ファイルは一切変更されていない。
            self.assertEqual(_read(dest), broken_content)

    def test_unreadable_new_file_leaves_dest_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            reviews_dir = os.path.join(d, "reviews")
            os.makedirs(reviews_dir)
            dest = os.path.join(reviews_dir, "2026-09-21.html")
            _write(dest, _doc())
            out = self._run(["--target", "2026-09-21",
                              "--new", os.path.join(d, "does-not-exist.html"),
                              "--reviews-dir", reviews_dir], expect=1)
            self.assertIn("mode=error", out)
            self.assertIn('data-rid="1"', _read(dest))


if __name__ == "__main__":
    unittest.main()
