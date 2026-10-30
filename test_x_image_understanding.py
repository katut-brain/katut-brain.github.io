# -*- coding: utf-8 -*-
"""X 添付画像を Gemini で読む（fetch_content._gemini_image_understanding と fetch_x の配線）の回帰テスト。
ネットワークには出ない。fetch_x は syndication の応答だけをダブルにし、本物の関数に通す。"""
import json
import os
import sys
import unittest
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
    with patch("fetch_content._last_model", "gemini-flash-lite-latest"), patch("fetch_content._get", return_value=(200, json.dumps(tweet))), \
         patch("fetch_content._gemini_image_understanding", return_value=image_result) as m:
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
        # 画像だけの欠損は ledger では従来どおり構造的(取り直さない)扱い
        self.assertEqual(ledger._reason_kind(facts), "permanent")

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
        r.read.return_value = body
        r.__enter__.return_value = r
        return r

    def test_one_generate_call_with_all_images(self):
        urls = ["https://pbs.twimg.com/a.jpg", "https://pbs.twimg.com/b.jpg", "https://pbs.twimg.com/c.jpg"]
        resp = MagicMock(text="読めた")
        with patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
             patch("urllib.request.urlopen", side_effect=lambda *a, **k: self._resp("image/jpeg", b"\xff\xd8x")), \
\
             patch("fetch_content._gemini_generate", return_value=resp) as gen:
            out = fetch_content._gemini_image_understanding(urls)
        self.assertEqual(out, ("読めた", ""))
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(len(gen.call_args[0][0]), 4)   # 画像3 + 指示文1

    def test_non_image_or_failed_downloads_fail_without_calling_gemini(self):
        with patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
             patch("urllib.request.urlopen", side_effect=lambda *a, **k: self._resp("text/html", b"<html>")), \
             patch("fetch_content._gemini_generate") as gen:
            out = fetch_content._gemini_image_understanding(["https://pbs.twimg.com/a.jpg"])
        self.assertEqual(out, ("", "image_download_failed"))
        gen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
