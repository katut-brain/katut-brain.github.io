# refresh_cards.py の回帰テスト。実際の reviews のカードの形（vdesc 末尾の
# 「今回は動画の内容を取得できなかった。※動画の内容は未取得」）で、
# 開示が消えて内容が入ること・他のカードとページの残りが1バイトも変わらないこと・
# 回収できていない rid に触らないこと・上限（rid 単位）を超えないこと・
# 書き直した後に check_disclosure が偽の違反を出さないことを見る。標準ライブラリのみ。
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from unittest import mock

import check_disclosure
import check_review_preserved
import fetch_content
import ledger
import refresh_cards

DISC = "今回は動画の内容を取得できなかった。※動画の内容は未取得"
MARK = "※動画の内容は未取得"


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


def rec(rid, understood, summary="", missing=None, day="2026-10-14", hour="10"):
    return {"url": "https://x.com/u/status/%d?s=12" % rid, "route": "x", "ok": True,
            "raindrop_id": rid, "fetched_at": "%sT%s:00:00Z" % (day, hour),
            "video_understood": understood, "video_summary": summary,
            "missing": missing if missing is not None else ([] if understood else ["video_content"]),
            "depth": "full" if understood else "partial", "attempt_seq": 1}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rv = os.path.join(self.d, "reviews")
        self.ff = os.path.join(self.d, "facts")
        self.fo = os.path.join(self.d, "files.txt")
        os.makedirs(self.rv)
        os.makedirs(self.ff)
        self._old = ledger.FACTS_DIR
        ledger.FACTS_DIR = self.ff

    def tearDown(self):
        ledger.FACTS_DIR = self._old
        shutil.rmtree(self.d, ignore_errors=True)

    def facts(self, *recs, **kw):
        day = kw.get("day", "2026-10-14")
        with open(os.path.join(self.ff, day + ".json"), "w", encoding="utf-8") as f:
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
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = refresh_cards.main(["--reviews-dir", self.rv, "--facts-dir", self.ff,
                                     "--files-out", self.fo, *extra])
        self.assertEqual(rc, 0)
        line = [l for l in buf.getvalue().splitlines() if l.startswith("CARD_REFRESH:")][-1]
        return dict(kv.split("=", 1) for kv in line.split()[1:])


class RefreshTest(Base):
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
        self.assertFalse(check_review_preserved.missing_cards(old, new))
        card1 = check_disclosure.extract_vcards(new)[0][0]["raw"]
        self.assertFalse(check_disclosure._marker_disclosed(
            check_disclosure.vdesc_text(card1), MARK))

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

    def test_summary_present_but_not_understood_untouched(self):
        old = page(card(2, "本文B。" + DISC))
        p = self.review("2026-10-04.html", old)
        self.facts(rec(2, False, "古い要約。", missing=[]))
        self.run_main("--apply")
        self.assertEqual(self.read(p), old)

    def test_missing_video_content_still_untouched(self):
        old = page(card(2, "本文B。" + DISC))
        p = self.review("2026-10-04.html", old)
        self.facts(rec(2, True, "内容。", missing=["video_content"]))
        self.run_main("--apply")
        self.assertEqual(self.read(p), old)

    def test_latest_record_decides(self):
        # 古い成功の後に新しい失敗が来たら書き直さない
        old = page(card(2, "本文B。" + DISC))
        p = self.review("2026-10-04.html", old)
        self.facts(rec(2, True, "内容。", day="2026-10-13"), day="2026-10-13")
        self.facts(rec(2, False, day="2026-10-14", hour="23"), day="2026-10-14")
        out = self.run_main("--apply")
        self.assertEqual(self.read(p), old)
        self.assertEqual(out["candidates"], "0")
        # 逆順（古い失敗の後に新しい成功）なら書き直す
        self.facts(rec(2, False, day="2026-10-13"), day="2026-10-13")
        self.facts(rec(2, True, "内容。", day="2026-10-14", hour="23"), day="2026-10-14")
        self.run_main("--apply")
        self.assertNotIn(MARK, self.read(p))

    def test_limit_counts_rids(self):
        p = self.review("2026-10-04.html", page(*[card(i, "本文。" + DISC) for i in range(1, 9)]))
        self.facts(*[rec(i, True, "内容%d。" % i) for i in range(1, 9)])
        out = self.run_main("--apply", "--limit", "5")
        self.assertEqual((out["rewritten"], out["cards"]), ("5", "5"))
        self.assertEqual(self.read(p).count(MARK), 3)
        self.run_main("--apply", "--limit", "5")
        self.assertEqual(self.read(p).count(MARK), 0)

    def test_limit_is_per_rid_not_per_card(self):
        # rid 1 は2日・複数カードにあるが1件と数え、全部書き直す。rid 2 は上限で残る
        p1 = self.review("2026-10-04.html", page(card(1, "A。" + DISC), card(2, "B。" + DISC)))
        p2 = self.review("2026-10-05.html", page(card(1, "C。" + DISC)))
        self.facts(rec(1, True, "内容1。"), rec(2, True, "内容2。"))
        out = self.run_main("--apply", "--limit", "1")
        self.assertEqual((out["rewritten"], out["cards"]), ("1", "2"))
        self.assertEqual(self.read(p1).count(MARK), 1)
        self.assertEqual(self.read(p2).count(MARK), 0)
        self.assertEqual(sorted(self.read(self.fo).split()),
                         sorted([p1.replace(os.sep, "/"), p2.replace(os.sep, "/")]))

    def test_unrewritable_shapes_do_not_eat_limit(self):
        bad = [card(i, "本文<b>x</b>。" + DISC) for i in range(1, 6)]     # タグ入り=書き直せない
        good = [card(10, "本文。" + DISC), card(11, "本文。" + DISC)]
        p = self.review("2026-10-04.html", page(*(bad + good)))
        self.facts(*[rec(i, True, "内容。") for i in (1, 2, 3, 4, 5, 10, 11)])
        out = self.run_main("--apply", "--limit", "5")
        self.assertEqual((out["candidates"], out["skipped"], out["rewritten"]), ("7", "5", "2"))
        self.assertEqual(self.read(p).count(MARK), 5)

    def test_detect_only_does_not_write(self):
        old = page(card(1, "本文。" + DISC))
        p = self.review("2026-10-04.html", old)
        self.facts(rec(1, True, "内容。"))
        out = self.run_main()
        self.assertEqual(out["candidates"], "1")
        self.assertEqual(self.read(p), old)
        self.assertFalse(os.path.exists(self.fo))

    def test_summary_length_capped(self):
        p = self.review("2026-10-04.html", page(card(1, "本文。" + DISC)))
        self.facts(rec(1, True, "あ" * 600))
        self.run_main("--apply")
        self.assertLessEqual(len(self.read(p)),
                             len(page(card(1, "本文。動画の内容："))) + 100 + 5)

    def test_unknown_shape_skipped(self):
        old = page(card(1, "本文。別の説明" + MARK))
        p = self.review("2026-10-04.html", old)
        self.facts(rec(1, True, "内容。"))
        out = self.run_main("--apply")
        self.assertEqual(self.read(p), old)
        self.assertEqual(out["skipped"], "1")

    def test_files_out_cleared_when_nothing_written(self):
        with open(self.fo, "w", encoding="utf-8") as f:
            f.write("reviews/stale.html\n")
        self.review("2026-10-04.html", page(card(1, "本文。")))
        self.run_main("--apply")
        self.assertEqual(self.read(self.fo), "")

    def test_replace_failure_is_contained(self):
        old = page(card(1, "本文。" + DISC))
        p = self.review("2026-10-04.html", old)
        self.facts(rec(1, True, "内容。"))
        with mock.patch("refresh_cards.os.replace", side_effect=PermissionError("locked")):
            out = self.run_main("--apply")
        self.assertEqual(out["errors"], "1")
        self.assertEqual(out["rewritten"], "0")
        self.assertEqual(self.read(p), old)
        self.assertEqual(os.listdir(self.rv), ["2026-10-04.html"])   # .refresh.tmp を残さない
        self.assertEqual(self.read(self.fo), "")


class DisclosureAfterRefreshTest(Base):
    """旧日付の fetch_facts には missing=[video_content] が残る。書き直した後に偽の違反を出さない。"""

    def setUp(self):
        Base.setUp(self)
        self.old_rec = rec(1, False, day="2026-10-04")
        self.old_rec["video_reason"] = "gemini_unavailable"
        self.facts(self.old_rec, day="2026-10-04")
        self.p = self.review("2026-10-04.html", page(card(1, "本文A。" + DISC)))

    def violations(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            check_disclosure.main(["--all", "--since", "2026-09-15", "--facts-dir", self.ff,
                                   "--reviews-dir", self.rv])
        m = re.search(r"date=2026-10-04 duty=(\d+) disclosed=(\d+) violation=(\d+) excluded=(\d+)",
                      buf.getvalue())
        return tuple(int(x) for x in m.groups())

    def test_before_recovery_card_is_disclosed(self):
        self.assertEqual(self.violations(), (1, 1, 0, 0))

    def test_after_refresh_no_false_violation(self):
        self.facts(rec(1, True, "机で組み立てる様子。", day="2026-10-14"), day="2026-10-14")
        self.run_main("--apply")
        self.assertNotIn(MARK, self.read(self.p))
        self.assertEqual(self.violations(), (0, 0, 0, 1))

    def test_recovered_reason_recorded(self):
        self.facts(rec(1, True, "内容。"))
        self.run_main("--apply")
        r = check_disclosure.process_date(
            "2026-10-04", self.ff, self.rv, {}, {},
            recovered=check_disclosure.recovered_rids(self.ff))
        self.assertEqual((r.violation, r.excluded), (0, 1))
        self.assertEqual(r.excluded_records[0]["reason"], "recovered")

    def test_unrecovered_missing_marker_is_still_a_violation(self):
        # 回収済みでない rid のカードからマーカーが消えていれば、従来どおり違反
        self.review("2026-10-04.html", page(card(1, "本文A。")))
        self.assertEqual(self.violations(), (1, 0, 1, 0))

    def test_later_failure_restores_duty(self):
        self.facts(rec(1, True, "内容。", day="2026-10-13"), day="2026-10-13")
        self.facts(rec(1, False, day="2026-10-14", hour="23"), day="2026-10-14")
        self.review("2026-10-04.html", page(card(1, "本文A。")))
        self.assertEqual(self.violations(), (1, 0, 1, 0))


class FactsPersistenceTest(Base):
    def test_facts_carry_video_summary_and_ledger_reads_it(self):
        d = os.path.join(self.d, "ff2")
        old = fetch_content.FACTS_DIR
        fetch_content.FACTS_DIR = d
        try:
            url = "https://x.com/u/status/1?s=12"
            res = {"ok": True, "type": "x", "video_understood": True, "has_video": True,
                   "text": "cap\n\n動画の内容: この動画は" + "机の上で組み立てる。" * 30,
                   "depth": "full", "missing": []}
            facts = fetch_content._facts(url, time.time(), res)
            fetch_content.record_facts(facts, day="2026-10-14")
        finally:
            fetch_content.FACTS_DIR = old
        self.assertLessEqual(len(facts["video_summary"]), 100)  # 公開リポに出す本文はカードに載る100字まで
        self.assertTrue(facts["video_summary"].startswith("机の上で"))
        ledger.FACTS_DIR = d
        recs = ledger._load_all_records()
        self.assertEqual(recs[0]["video_summary"], facts["video_summary"])
        # 動画なしは空
        res2 = dict(res, video_understood=False)
        self.assertEqual(fetch_content._facts(url, time.time(), res2)["video_summary"], "")

    def test_end_to_end_from_real_facts_file(self):
        p = self.review("2026-10-04.html", page(card(1, "本文。" + DISC)))
        url = "https://x.com/u/status/1?s=12"
        old = fetch_content.FACTS_DIR
        fetch_content.FACTS_DIR = self.ff
        try:
            res = {"ok": True, "type": "x", "video_understood": True, "has_video": True,
                   "text": "cap\n\n動画の内容: 机の上で組み立てる。", "depth": "full", "missing": []}
            with mock.patch.object(fetch_content, "_resolve_rid", return_value=(1, "exact")):
                facts = fetch_content._facts(url, time.time(), res)
            fetch_content.record_facts(facts, day="2026-10-14")
        finally:
            fetch_content.FACTS_DIR = old
        self.run_main("--apply")
        self.assertIn("動画の内容：机の上で組み立てる。", self.read(p))


if __name__ == "__main__":
    unittest.main()
