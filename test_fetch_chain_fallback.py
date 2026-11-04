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
for _k in ("GROKBOT_WEBHOOK_URL", "GROKBOT_WEBHOOK_KEY", "SLACK_GROK_TOKEN",
          "GROKBOT_SLACK_CHANNEL", "GROKBOT_POLL_TIMEOUT", "GROKBOT_RESULTS_REF"):
    os.environ.pop(_k, None)   # 秘密があると fetch_x が x_grokbot を先に試す。既存テストは未設定のまま

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
    def setUp(self):
        for k in ("GROKBOT_WEBHOOK_URL", "GROKBOT_WEBHOOK_KEY", "SLACK_GROK_TOKEN",
                  "GROKBOT_SLACK_CHANNEL", "GROKBOT_POLL_TIMEOUT", "GROKBOT_RESULTS_REF"):
            os.environ.pop(k, None)

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
        # 文字列でない本文・名前（リンクを辿る段まで通す）でも落ちない
        weird = {"tweet": {"text": "本文", "raw_text": {"text": 5}, "author": {"name": [], "screen_name": {}},
                           "media": {"photos": [{"url": 7}], "videos": [{"variants": [{"url": None}]}]}}}
        with patch("fetch_content._get", return_value=(503, "")), \
             patch.object(fc, "http_get", chain_http({"api.fxtwitter.com": (200, json.dumps(weird))})), \
             patch.object(fc, "_check_public", lambda url: None):
            r = fetch_content.fetch_x(URL, follow_links=True)
        self.assertTrue(r["ok"])
        self.assertEqual((r["text"], r["handle"], r["photos"]), ("本文", "", []))
        # 本文まで文字列でなければ、fetch-chain 側が「本文が空」で弾いて従来の失敗に戻る（落ちない）
        weird["tweet"]["text"] = []
        with patch("fetch_content._get", return_value=(503, "")), \
             patch.object(fc, "http_get", chain_http({"api.fxtwitter.com": (200, json.dumps(weird))})), \
             patch.object(fc, "_check_public", lambda url: None):
            r = fetch_content.fetch_x(URL, follow_links=True)
        self.assertEqual((r["ok"], r["fail_reason"]), (False, "x_syndication_http_503"))
        with patch("fetch_content._chain_tweet_inner", side_effect=KeyError("method")):
            r, _ = self.run_x((503, ""), {})
        self.assertEqual((r["ok"], r["fail_reason"], r["fetch_chain_tried"]),
                         (False, "x_syndication_http_503", "error:KeyError"))

    def test_no_fallback_when_time_is_short(self):
        with patch("fetch_content._remaining", return_value=30):
            r, _ = self.run_x((503, ""), {"api.fxtwitter.com": (200, json.dumps(FX))})
        self.assertFalse(r["ok"])
        self.assertEqual(r["fetch_chain_tried"], "skipped:deadline")

    def _enable_grok(self):
        os.environ["GROKBOT_WEBHOOK_URL"] = "https://hook.example/h"
        os.environ["GROKBOT_WEBHOOK_KEY"] = "k"
        os.environ["SLACK_GROK_TOKEN"] = "xoxb-k"
        os.environ["GROKBOT_SLACK_CHANNEL"] = "C0CHAN"
        os.environ["GROKBOT_POLL_TIMEOUT"] = "90"

    def _history(self, rid, payload):
        text = "grok:%s\n```json\n%s\n```" % (rid, json.dumps(payload))
        return 200, json.dumps({"ok": True, "messages": [{"text": text}]})

    def _clock(self):
        clock = [1_700_000_000.0]

        def now():
            return clock[0]

        def sleep(sec):
            clock[0] += sec

        return now, sleep

    def test_without_secrets_fetch_x_does_not_call_grokbot(self):
        posts = []
        with patch.object(fc, "_http_post_json", lambda *a, **k: posts.append(1) or 202), \
             patch.object(fc, "x_grokbot", side_effect=AssertionError("x_grokbot を呼んだ")):
            r, _ = self.run_x((200, json.dumps(SYND)), {})
        self.assertEqual(r["fetched_via"], "x_syndication")
        self.assertEqual(posts, [])

    def test_grokbot_maps_media_and_skips_syndication(self):
        self._enable_grok()
        posted = {}
        synd = []
        seen = {}

        def post(url, body, headers, timeout=15):
            posted["body"] = body
            posted["headers"] = dict(headers)
            return 202

        def get(url, timeout=15, max_bytes=2_000_000, headers=None):
            rid = posted["body"]["id"]
            self.assertIn("https://slack.com/api/conversations.history?", url)
            q = fc.urllib.parse.parse_qs(fc.urllib.parse.urlparse(url).query)
            self.assertEqual(q["channel"], ["C0CHAN"])
            self.assertEqual(q["limit"], ["50"])
            self.assertEqual(headers["Authorization"], "Bearer xoxb-k")
            return self._history(rid, {
                "id": rid, "url": URL, "text": "grok の本文 https://t.co/abc", "author": "h",
                "date": "2026-10-01T03:04:05.000Z",
                "media": [
                    {"type": "photo", "url": "&lt;https://pbs.twimg.com/media/p1.jpg&gt;"},
                    {"type": "image", "url": "https://pbs.twimg.com/media/p2.jpg"},
                    {"type": "video", "url": "&lt;https://video.twimg.com/a.mp4|v&gt;"},
                    {"type": "gif", "url": "https://video.twimg.com/b.mp4"},
                    {"type": "photo", "url": ""},
                    "nope",
                ],
            })

        now, sleep = self._clock()
        with patch.object(fc, "_http_post_json", post), \
             patch.object(fc, "http_get", get), \
             patch.object(fc, "_check_public", lambda url: None), \
             patch.object(fc.time, "time", now), \
             patch.object(fc.time, "sleep", sleep), \
             patch("fetch_content._get", side_effect=lambda *a, **k: synd.append(a) or (200, json.dumps(SYND))), \
             patch("fetch_content._gemini_video_understanding",
                   side_effect=lambda u, **k: (seen.setdefault("videos", []).append(u),
                                               seen.setdefault("hosts", k.get("allowed_hosts")),
                                               ("動画の中身", ""))[2]), \
             patch("fetch_content._gemini_image_understanding",
                   side_effect=lambda p, **k: (seen.setdefault("photos", list(p)), ("画像の中身", ""))[1]):
            r = fetch_content.fetch_x(URL, follow_links=False)
        self.assertEqual(synd, [])
        self.assertEqual(r["fetched_via"], "x_grokbot")
        self.assertTrue(r["text"].startswith("grok の本文 https://t.co/abc"))
        self.assertEqual((r["handle"], r["author"], r["date"]), ("h", "h", "2026-10-01"))
        self.assertEqual(posted["body"]["query"], "x_post_fetch")
        self.assertEqual(posted["body"]["url"], URL)
        self.assertRegex(posted["body"]["id"], r"^[0-9]+-[0-9]+$")
        self.assertEqual(posted["headers"]["Authorization"], "Bearer k")
        self.assertEqual(posted["headers"]["X-Webhook-Key"], "k")
        self.assertEqual(len(seen["photos"]), 2)
        self.assertIn("name=large", seen["photos"][0])
        self.assertTrue(r["cover"].startswith("https://pbs.twimg.com/media/p1.jpg"))
        self.assertEqual(seen["videos"], ["https://video.twimg.com/a.mp4"])
        self.assertEqual(seen["hosts"], ("twimg.com",))

    def test_grokbot_failure_falls_back_to_syndication_once(self):
        self._enable_grok()
        posts = []
        with patch.object(fc, "_http_post_json", lambda *a, **k: posts.append(1) or 500), \
             patch.object(fc, "_check_public", lambda url: None), \
             patch("fetch_content._get", return_value=(200, json.dumps(SYND))), \
             patch("fetch_content._gemini_video_understanding", return_value=("", "")), \
             patch("fetch_content._gemini_image_understanding", return_value=("", "")):
            r = fetch_content.fetch_x(URL, follow_links=False)
        self.assertEqual(r["fetched_via"], "x_syndication")
        self.assertEqual(posts, [1])
        self.assertTrue(r["text"].startswith("syndication の本文"))

    def test_grokbot_and_syndication_down_uses_fxtwitter_without_a_second_webhook(self):
        self._enable_grok()
        posts = []
        now, sleep = self._clock()

        def post(url, body, headers, timeout=15):
            posts.append(body["id"])
            return 202

        def get(url, timeout=15, max_bytes=2_000_000, headers=None):
            if "slack.com/api/conversations.history" in url:
                return self._history(posts[-1], {"id": "other", "error": "nope", "text": "bad"})
            if "api.fxtwitter.com" in url:
                return 200, json.dumps(FX)
            return None, ""
        with patch.object(fc, "_http_post_json", post), \
             patch.object(fc, "http_get", get), \
             patch.object(fc, "_check_public", lambda url: None), \
             patch.object(fc.time, "time", now), \
             patch.object(fc.time, "sleep", sleep), \
             patch("fetch_content._get", return_value=(503, "")), \
             patch("fetch_content._gemini_video_understanding", return_value=("動画の中身", "")), \
             patch("fetch_content._gemini_image_understanding", return_value=("画像の中身", "")):
            r = fetch_content.fetch_x(URL, follow_links=False)
        self.assertEqual(r["fetched_via"], "fetch_chain")
        self.assertEqual(len(posts), 1)
        self.assertIn("x_fxtwitter:ok", r["fetch_chain_tried"])
        self.assertNotIn("x_grokbot", r["fetch_chain_tried"])
        self.assertTrue(r["text"].startswith("fx の本文"))

    def test_short_budget_skips_grokbot(self):
        self._enable_grok()
        posts = []
        with patch("fetch_content._remaining", return_value=100), \
             patch.object(fc, "_http_post_json", lambda *a, **k: posts.append(1) or 202), \
             patch("fetch_content._get", return_value=(200, json.dumps(SYND))), \
             patch("fetch_content._gemini_video_understanding", return_value=("", "")), \
             patch("fetch_content._gemini_image_understanding", return_value=("", "")):
            r = fetch_content.fetch_x(URL, follow_links=False)
        self.assertEqual(posts, [])
        self.assertEqual(r["fetched_via"], "x_syndication")

    def test_embedded_quote_passes_a_short_deadline(self):
        seen = {}

        def spy(url, follow_links=True, understand_video=True, deadline=None, _quote=False):
            seen["args"] = (follow_links, understand_video, deadline)
            seen["quote"] = _quote
            return {"ok": True, "text": "引用本文", "title": "t"}

        with patch("fetch_content.fetch_x", spy):
            out = fetch_content._fetch_x_embedded_link_inner("https://x.com/a/status/99", timeout=45)
        self.assertEqual(out["text"], "引用本文")
        self.assertEqual(seen["args"][0], False)
        self.assertEqual(seen["args"][1], False)
        self.assertTrue(seen["quote"])   # 引用経路は x_grokbot を quote_path として飛ばす
        left = seen["args"][2] - fetch_content._now()
        self.assertGreater(left, 20)
        self.assertLess(left, 50)


if __name__ == "__main__":
    unittest.main()
