# refresh_cards.py の回帰テスト。実際の reviews のカードの形（vdesc 末尾の
# 「今回は動画の内容を取得できなかった。※動画の内容は未取得」）で、
# 開示が消えて内容が入ること・他のカードとページの残りが1バイトも変わらないこと・
# 回収できていない rid に触らないこと・上限を超えないことを見る。標準ライブラリのみ。
import json
import os
import shutil
import tempfile
import unittest

import check_disclosure
import check_review_preserved
import refresh_cards
import ledger

DISC = "今回は動画の内容を取得できなかった。※動画の内容は未取得"


def card(rid, desc):
    return (
        '        <div class="vcard">\r\n'
        '          <a class="vlink" href="https://x.com/u/status/%d?s=12" target="_blank" rel="noopener">\r\n'
        '            <div class="vbody"><div class="vtitle">t%d</div><div class="vdesc">%s</div>'
        '<div class="vsaved">保存 10/4 14:34</div></div>\r\n'
        '          </a>\r\n'
        '          <button class="deepdive" data-rid="%d">AI</button>\r\n'
        '        </div>\r\n' % (rid, rid, desc, rid))


def page(*cards):
    return ('<html><head><style>.a{color:red}</style></head><body>\r\n<p>前文 ☆</p>\r\n'
            + "".join(cards) + '<footer>end</footer>\r\n</body></html>\r\n')


def rec(rid, understood, summary="", missing=None, day="2026-10-14"):
    return {"url": "https://x.com/u/status/%d?s=12" % rid, "route": "x", "ok": True,
            "raindrop_id": rid, "fetched_at": day + "T10:00:00Z",
            "video_understood": understood, "video_summary": summary,
            "missing": missing if missing is not None else ([] if understood else ["video_content"]),
            "depth": "full" if understood else "partial", "attempt_seq": 1}


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rv = os.path.join(self.d, "reviews")
        self.ff = os.path.join(self.d, "facts")
        os.makedirs(self.rv)
        os.makedirs(self.ff)
        self._old = ledger.FACTS_DIR
        ledger.FACTS_DIR = self.ff

    def tearDown(self):
        ledger.FACTS_DIR = self._old
        shutil.rmtree(self.d, ignore_errors=True)

    def facts(self, *recs):
        with open(os.path.join(self.ff, "2026-10-14.json"), "w", encoding="utf-8") as f:
            json.dump({r["url"]: r for r in recs}, f, ensure_ascii=False)

    def review(self, name, html):
        p = os.path.join(self.rv, name)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(html)
        return p

    def read(self, p):
        with open(p, encoding="utf-8", newline="") as f:
            return f.read()

    def run_main(self, *extra):
        return refresh_cards.main(["--reviews-dir", self.rv, *extra])

    def test_disclosure_replaced_rest_untouched(self):
        a = card(1, "本文A。" + DISC)
        b = card(2, "本文B。" + DISC)   # 回収できていない
        c = card(3, "本文C。")
        old = page(a, b, c)
        p = self.review("2026-10-04.html", old)
        self.facts(rec(1, True, "**机の上で**組み立てる様子。<script>x</script>続き。"),
                   rec(2, False), rec(3, True, "x"))
        self.run_main("--apply")
        new = self.read(p)
        # 書き換わるのは card1 の vdesc の中身だけ。その前後は1バイトも変わらない
        pre = old[:old.index("本文A。")]
        post = old[old.index("本文A。" + DISC) + len("本文A。" + DISC):]
        self.assertTrue(new.startswith(pre))
        self.assertTrue(new.endswith(post))
        mid = new[len(pre):len(new) - len(post)]
        self.assertTrue(mid.startswith("本文A。動画の内容：机の上で組み立てる様子。"))
        self.assertNotIn("<", mid)
        self.assertNotIn("未取得", mid)
        self.assertIn(b, new)
        self.assertIn(c, new)
        # 他の検査が通る
        self.assertFalse(check_review_preserved.missing_cards(old, new))
        self.assertTrue(check_disclosure.text_disclosed(
            "本文B。" + DISC, "x_video", ["video_content"]))
        card1 = check_disclosure.extract_vcards(new)[0][0]["raw"]
        self.assertFalse(check_disclosure._marker_disclosed(
            check_disclosure.vdesc_text(card1), check_disclosure.MARKERS["video_content"]))

    def test_uncollected_rid_untouched(self):
        old = page(card(2, "本文B。" + DISC))
        p = self.review("2026-10-04.html", old)
        self.facts(rec(2, False))
        self.run_main("--apply")
        self.assertEqual(self.read(p), old)

    def test_understood_but_no_summary_untouched(self):
        old = page(card(2, "本文B。" + DISC))
        p = self.review("2026-10-04.html", old)
        self.facts(rec(2, True, ""))
        self.run_main("--apply")
        self.assertEqual(self.read(p), old)

    def test_missing_video_content_still_listed_untouched(self):
        old = page(card(2, "本文B。" + DISC))
        p = self.review("2026-10-04.html", old)
        self.facts(rec(2, True, "内容。", missing=["video_content"]))
        self.run_main("--apply")
        self.assertEqual(self.read(p), old)

    def test_limit(self):
        p = self.review("2026-10-04.html", page(*[card(i, "本文。" + DISC) for i in range(1, 9)]))
        self.facts(*[rec(i, True, "内容%d。" % i) for i in range(1, 9)])
        self.run_main("--apply", "--limit", "5")
        self.assertEqual(self.read(p).count("※動画の内容は未取得"), 3)
        self.run_main("--apply", "--limit", "5")
        self.assertEqual(self.read(p).count("※動画の内容は未取得"), 0)

    def test_detect_only_does_not_write(self):
        old = page(card(1, "本文。" + DISC))
        p = self.review("2026-10-04.html", old)
        self.facts(rec(1, True, "内容。"))
        self.run_main()
        self.assertEqual(self.read(p), old)

    def test_summary_length_capped(self):
        p = self.review("2026-10-04.html", page(card(1, "本文。" + DISC)))
        self.facts(rec(1, True, "あ" * 600))
        self.run_main("--apply")
        self.assertLess(len(self.read(p)), len(page(card(1, "本文。"))) + 130)

    def test_ip_style_other_sentence_kept_and_unknown_shape_skipped(self):
        old = page(card(1, "本文。別の説明" + "※動画の内容は未取得"))
        p = self.review("2026-10-04.html", old)
        self.facts(rec(1, True, "内容。"))
        self.run_main("--apply")
        self.assertEqual(self.read(p), old)

    def test_fetch_content_persists_video_summary(self):
        import fetch_content
        r = {"video_understood": True, "text": "cap\n\n動画の内容: " + "あ" * 700}
        self.assertEqual(len(fetch_content._video_summary(r)), fetch_content.VIDEO_SUMMARY_MAX)
        self.assertEqual(fetch_content._video_summary({"video_understood": False, "text": "x"}), "")


if __name__ == "__main__":
    unittest.main()
