# -*- coding: utf-8 -*-
"""fetch_x の syndication が失敗したときだけ fetch-chain に回す（2026-10-30）の回帰テスト。
外には出ない。syndication の応答（fetch_content._get）と fetch-chain の通信（fetchchain.http_get）だけを偽にし、
本物の fetch_x・fetch-chain・_facts・ledger に通す。"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["FETCHCHAIN_STATE_DIR"] = tempfile.mkdtemp()   # 健康記録をテストの外に出さない
os.environ["FETCHCHAIN_ENV"] = "gha"

import fetch_content  # noqa: E402
import ledger  # noqa: E402
from fetch_chain import fetchchain as fc  # noqa: E402

URL = "https://x.com/h/status/123"
SYND = {"text": "syndication の本文 https://t.co/abc", "user": {"name": "N", "screen_name": "h"},
        "created_at": "2026-10-01T00:00:00.000Z", "mediaDetails": []}
FX = {"tweet": {"text": "展開済みの本文 https://example.com/x", "raw_text": {"text": "fx の本文 https://t.co/abc"},
                "author": {"name": "N", "screen_name": "h"}, "created_at": "Wed Oct 01 00:00:00 +0000 2026",
                "created_timestamp": 1790812800,
                "likes": 7,
                "media": {"photos": [{"url": "https://pbs.twimg.com/media/p1.jpg?name=orig"}],
                          "videos": [{"thumbnail_url": "https://pbs.twimg.com/t.jpg",
                                      "variants": [{"content_type": "application/x-mpegURL", "bitrate": 0, "url": "https://video.twimg.com/a.m3u8"},
                                                   {"content_type": "video/mp4", "bitrate": 632000, "url": "https://video.twimg.com/a.mp4"}]}]}}}
VX = {"text": "vx の本文", "user_name": "N", "user_screen_name": "h", "date": "Wed Oct 01 00:00:00 +0000 2026",
      "date_epoch": 1790812800, "likes": 3,
      "media_extended": [{"type": "image", "url": "https://pbs.twimg.com/media/v1.jpg"}]}


def chain_http(table):
    def _get(url, timeout=15, max_bytes=2_000_000):
        for key, resp in table.items():
            if key in url:
                return resp
        return None, ""
    return _get


class FallbackTest(unittest.TestCase):
    def run_x(self, synd, chain_table):
        seen = {}
        with patch("fetch_content._get", return_value=synd), \
             patch.object(fc, "http_get", chain_http(chain_table)), \
             patch.object(fc, "_check_public", lambda url: None), \
             patch("fetch_content._gemini_video_understanding",
                   side_effect=lambda u, **k: (seen.setdefault("video", u), seen.setdefault("hosts", k.get("allowed_hosts")),
                                               ("動画の中身", ""))[2]), \
             patch("fetch_content._gemini_image_understanding",
                   side_effect=lambda p, **k: (seen.setdefault("photos", p), ("画像の中身", ""))[1]):
            r = fetch_content.fetch_x(URL, follow_links=False)
        return r, seen

    def test_normal_path_does_not_touch_fetch_chain(self):
        def boom(*a, **k):
            raise AssertionError("fetch-chain を呼んだ")
        with patch.object(fc, "fetch", boom):
            r, _ = self.run_x((200, json.dumps(SYND)), {})
        self.assertTrue(r["ok"])
        self.assertEqual(r["fetched_via"], "x_syndication")
        self.assertNotIn("fetch_chain_tried", r)
        self.assertTrue(r["text"].startswith("syndication の本文"))
        self.assertEqual(r["date"], "2026-10-01")

    def test_syndication_down_falls_back_to_fxtwitter_with_media(self):
        r, seen = self.run_x((503, ""), {"api.fxtwitter.com": (200, json.dumps(FX))})
        self.assertTrue(r["ok"])
        self.assertEqual(r["fetched_via"], "fetch_chain")
        self.assertIn("x_fxtwitter:ok", r["fetch_chain_tried"])
        self.assertTrue(r["text"].startswith("fx の本文 https://t.co/abc"))  # t.co が残る形（リンクを辿れる）
        self.assertEqual((r["handle"], r["author"], r["likes"]), ("h", "N", 7))
        self.assertEqual(seen["video"], "https://video.twimg.com/a.mp4")   # mp4 のうち最小の bitrate
        self.assertEqual(seen["hosts"], ("twimg.com",))                    # 中継由来の動画は twimg.com だけ
        self.assertEqual(r["date"], "2026-10-01")                          # syndication と同じ形の日付
        self.assertIn("name=large", seen["photos"][0])
        facts = fetch_content._facts(URL, 0, r)
        self.assertEqual((facts["fetched_via"], facts["ok"]), ("fetch_chain", True))

    def test_falls_through_to_vxtwitter(self):
        r, seen = self.run_x((None, ""), {"api.fxtwitter.com": (404, ""), "api.vxtwitter.com": (200, json.dumps(VX))})
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"].split("\n")[0], "vx の本文")
        self.assertEqual(r["date"], "2026-10-01")
        self.assertNotIn("x_syndication", r["fetch_chain_tried"])          # syndication は二度試さない
        self.assertEqual(len(seen["photos"]), 1)

    def test_everything_down_keeps_the_old_failure_code(self):
        r, _ = self.run_x((404, ""), {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["fail_reason"], "x_syndication_http_404")       # 台帳の分類は従来どおり
        self.assertIn("x_fxtwitter:x", r["fetch_chain_tried"])
        facts = fetch_content._facts(URL, 0, r)
        self.assertEqual(ledger._reason_kind(facts), ledger._reason_kind(
            fetch_content._facts(URL, 0, {"ok": False, "type": "x", "fail_reason": "x_syndication_http_404"})))
        r, _ = self.run_x((200, "{broken"), {})
        self.assertEqual(r["fail_reason"], "x_syndication_bad_json")

    def test_broken_fetch_chain_never_breaks_the_night(self):
        with patch.object(fc, "fetch", side_effect=RuntimeError("壊れた")):
            r, _ = self.run_x((503, ""), {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["fail_reason"], "x_syndication_http_503")
        self.assertEqual(r["fetch_chain_tried"], "error:RuntimeError")

    def test_normal_path_video_keeps_no_host_limit(self):
        synd = dict(SYND, mediaDetails=[{"type": "video", "video_info": {"variants": [
            {"content_type": "video/mp4", "bitrate": 1, "url": "https://video.twimg.com/s.mp4"}]}}])
        r, seen = self.run_x((200, json.dumps(synd)), {})
        self.assertEqual((seen["video"], seen["hosts"]), ("https://video.twimg.com/s.mp4", None))  # 普段の経路は従来どおり

    def test_odd_shaped_answers_fall_back_to_the_old_failure(self):
        odd = {"tweet": {"text": "t", "author": None, "media": {"photos": [None, 3], "videos": ["x"]}}}
        r, _ = self.run_x((503, ""), {"api.fxtwitter.com": (200, json.dumps(odd))})
        self.assertTrue(r["ok"])                     # 変な要素は飛ばして本文だけ使う
        with patch("fetch_content._chain_tweet_inner", side_effect=KeyError("method")):
            r, _ = self.run_x((503, ""), {})
        self.assertEqual((r["ok"], r["fail_reason"], r["fetch_chain_tried"]),
                         (False, "x_syndication_http_503", "error:KeyError"))

    def test_no_fallback_when_time_is_short(self):
        with patch("fetch_content._remaining", return_value=30):
            r, _ = self.run_x((503, ""), {"api.fxtwitter.com": (200, json.dumps(FX))})
        self.assertFalse(r["ok"])
        self.assertEqual(r["fetch_chain_tried"], "skipped:deadline")


if __name__ == "__main__":
    unittest.main()
