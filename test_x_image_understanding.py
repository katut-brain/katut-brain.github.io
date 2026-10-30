# -*- coding: utf-8 -*-
"""X 添付画像を Gemini で読む（fetch_content._gemini_image_understanding と fetch_x の配線）の回帰テスト。
ネットワークには出ない。fetch_x は syndication の応答だけをダブルにし、本物の関数に通す。"""
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fetch_content  # noqa: E402
import ledger  # noqa: E402

TWEET = {
    "text": "図解です",
    "user": {"name": "N", "screen_name": "h"},
    "created_at": "2026-10-01T00:00:00.000Z",
    "mediaDetails": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/%d.jpg" % i}
                     for i in range(6)],
}


def _fetch_x(understand_video=True, tweet=TWEET, image_result=("画像の読み取り結果", "")):
    with patch("fetch_content._get", return_value=(200, json.dumps(tweet))), \
         patch("fetch_content._gemini_image_understanding",
               side_effect=lambda *a, **k: (fetch_content._last.update(model="gemini-flash-lite-latest"), image_result)[1]) as m:
        r = fetch_content.fetch_x("https://x.com/h/status/123", follow_links=False,
                                  understand_video=understand_video)
    return r, m


class FetchXImageTest(unittest.TestCase):
    def test_success_drops_image_content_and_exposes_summary(self):
        r, m = _fetch_x()
        self.assertEqual(len(m.call_args[0][0]), 4)          # 先頭4枚だけ
        self.assertNotIn("image_content", r["missing"])
        self.assertEqual(r["depth"], "full")
        self.assertEqual(r["image_summary"], "画像の読み取り結果")
        self.assertIn("添付画像の内容: 画像の読み取り結果", r["text"])
        facts = fetch_content._facts("https://x.com/h/status/123", 0, r)
        self.assertEqual(facts["image_summary"], "画像の読み取り結果")
        self.assertEqual(facts["image_reason"], "")
        self.assertEqual(facts["image_model"], "gemini-flash-lite-latest")   # 読めたモデルが facts に残る

    def test_failure_keeps_image_content_and_records_reason(self):
        r, _ = _fetch_x(image_result=("", "exception:ServerError:503"))
        self.assertIn("image_content", r["missing"])
        self.assertEqual(r["depth"], "partial")
        self.assertEqual(r["image_summary"], "")
        facts = fetch_content._facts("https://x.com/h/status/123", 0, r)
        self.assertEqual(facts["image_reason"], "exception:ServerError:503")
        self.assertEqual(facts["image_summary"], "")
        # 一過性の理由で読めなかった画像は取り直し対象(試行上限は既存の3回)
        self.assertEqual(ledger._reason_kind(facts), "transient")

    def test_partial_download_keeps_image_content_but_shows_summary(self):
        r, _ = _fetch_x(image_result=("一部だけ読めた", "partial_download"))
        self.assertIn("image_content", r["missing"])
        self.assertEqual(r["image_reason"], "partial_download")
        self.assertIn("添付画像の内容: 一部だけ読めた", r["text"])
        facts = fetch_content._facts("https://x.com/h/status/123", 0, r)
        self.assertEqual(facts["image_reason"], "partial_download")
        self.assertEqual(ledger._reason_kind(facts), "transient")

    def test_gemini_trace_fields_reach_facts(self):
        """主モデルを飛ばしたか・主/予備の失敗理由が facts に載る。"""
        def fake(*a, **k):
            fetch_content._last.update(model="gemini-flash-lite-latest", primary_skipped=True,
                                       primary_reason="exception:ServerError:503", fallback_reason="")
            return ("読めた", "")
        with patch("fetch_content._get", return_value=(200, json.dumps(TWEET))), \
             patch("fetch_content._gemini_image_understanding", side_effect=fake):
            r = fetch_content.fetch_x("https://x.com/h/status/123", follow_links=False)
        facts = fetch_content._facts("https://x.com/h/status/123", 0, r)
        self.assertTrue(facts["primary_skipped"])
        self.assertEqual(facts["primary_reason"], "exception:ServerError:503")
        self.assertEqual(facts["fallback_reason"], "")
        self.assertEqual(facts["image_model"], "gemini-flash-lite-latest")

    def test_quoted_tweet_path_does_not_read_images(self):
        r, m = _fetch_x(understand_video=False)
        m.assert_not_called()
        self.assertIn("image_content", r["missing"])

    def test_no_photos_no_call(self):
        r, m = _fetch_x(tweet=dict(TWEET, mediaDetails=[]))
        m.assert_not_called()
        self.assertEqual(r["missing"], [])

    def test_unavailable_behaves_like_before(self):
        with patch("fetch_content._GEMINI_API_KEY", ""), \
             patch("fetch_content._get", return_value=(200, json.dumps(TWEET))):
            r = fetch_content.fetch_x("https://x.com/h/status/123", follow_links=False)
        self.assertIn("image_content", r["missing"])
        self.assertEqual(r["image_reason"], "gemini_unavailable")


@unittest.skipUnless(fetch_content._GEMINI_AVAILABLE, "google-genai 未導入")
class ImageUnderstandingUnitTest(unittest.TestCase):
    def _resp(self, ctype, body):
        r = MagicMock()
        r.headers = {"Content-Type": ctype}
        r.read.side_effect = [body, b""]
        r.__enter__.return_value = r
        return r

    def _opener(self, side_effect):
        o = MagicMock()
        o.open.side_effect = side_effect
        return o

    def test_one_generate_call_with_all_images(self):
        urls = ["https://pbs.twimg.com/a.jpg", "https://pbs.twimg.com/b.jpg", "https://pbs.twimg.com/c.jpg"]
        opener = self._opener(lambda *a, **k: self._resp("image/jpeg", b"JPEG"))
        with patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
             patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content._gemini_generate", return_value=MagicMock(text="読めた")) as gen:
            out = fetch_content._gemini_image_understanding(urls)
        self.assertEqual(out, ("読めた", ""))
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(len(gen.call_args[0][0]), 4)   # 画像3 + 指示文1

    def test_non_image_or_failed_downloads_fail_without_calling_gemini(self):
        opener = self._opener(lambda *a, **k: self._resp("text/html", b"<html>"))
        with patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
             patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content._gemini_generate") as gen:
            out = fetch_content._gemini_image_understanding(["https://pbs.twimg.com/a.jpg"])
        self.assertEqual(out, ("", "image_download_failed"))
        gen.assert_not_called()

    def test_partial_download_reads_what_it_got_and_says_so(self):
        urls = ["https://pbs.twimg.com/a.jpg", "https://pbs.twimg.com/b.jpg"]
        opener = self._opener([self._resp("image/jpeg", b"JPEG"), OSError("boom")])
        with patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
             patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content._gemini_generate", return_value=MagicMock(text="半分")) as gen:
            out = fetch_content._gemini_image_understanding(urls)
        self.assertEqual(out, ("半分", "partial_download"))
        self.assertEqual(len(gen.call_args[0][0]), 2)   # 画像1 + 指示文1

    def test_start_url_on_disallowed_host_or_scheme_is_rejected_without_fetching(self):
        for bad in ("https://evil.example.com/a.jpg", "http://pbs.twimg.com/a.jpg",
                    "https://twimg.com.evil.example.com/a.jpg"):
            opener = self._opener(lambda *a, **k: self.fail("fetched"))
            with patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
                 patch("urllib.request.build_opener", return_value=opener), \
                 patch("fetch_content._gemini_generate") as gen:
                out = fetch_content._gemini_image_understanding([bad])
            self.assertEqual(out, ("", "host_not_allowed"), bad)
            gen.assert_not_called()

    def test_redirect_to_disallowed_host_is_rejected(self):
        # 実物の許可リスト付きリダイレクトハンドラ: 許可外ホスト・http へは追従しない
        h = fetch_content._AllowlistRedirectHandler(fetch_content._IMAGE_ALLOWED_HOSTS)
        req = urllib.request.Request("https://pbs.twimg.com/a.jpg")
        for bad in ("https://evil.example.com/a.jpg", "http://pbs.twimg.com/a.jpg"):
            with self.assertRaises(urllib.error.URLError):
                h.redirect_request(req, None, 302, "Found", {}, bad)
        self.assertIsNotNone(h.redirect_request(req, None, 302, "Found", {}, "https://abs.twimg.com/b.jpg"))

    def test_redirect_rejection_maps_to_host_not_allowed(self):
        opener = self._opener(urllib.error.URLError("redirect to disallowed host: evil.example.com"))
        with patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
             patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content._gemini_generate") as gen:
            out = fetch_content._gemini_image_understanding(["https://pbs.twimg.com/a.jpg"])
        self.assertEqual(out, ("", "host_not_allowed"))
        gen.assert_not_called()

    def test_total_size_cap(self):
        big = b"x" * 9_000_000
        urls = ["https://pbs.twimg.com/%d.jpg" % i for i in range(3)]   # 9+9+9 > 15MB
        opener = self._opener(lambda *a, **k: self._resp("image/jpeg", big))
        with patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
             patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content._gemini_generate", return_value=MagicMock(text="x")) as gen:
            out = fetch_content._gemini_image_understanding(urls)
        self.assertEqual(out[1], "partial_download")       # 2枚目以降は合計上限で落ちる
        self.assertEqual(len(gen.call_args[0][0]), 2)


if __name__ == "__main__":
    unittest.main()
