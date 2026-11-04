# -*- coding: utf-8 -*-
"""夜間メディア理解の配管（Content-Type、引用の帰属、Groq、Instagram の順、段ごとの facts）。

ネットワークには出ない。Gemini SDK も要らない。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

for _k in ("GROKBOT_WEBHOOK_URL", "GROKBOT_WEBHOOK_KEY", "SLACK_GROK_TOKEN",
           "GROKBOT_SLACK_CHANNEL", "GROKBOT_POLL_TIMEOUT", "GROKBOT_RESULTS_REF",
           "GROQ_API_KEY"):
    os.environ.pop(_k, None)

import fetch_content  # noqa: E402
import groq_transcribe  # noqa: E402


def _mp4(n=32):
    return b"\x00\x00\x00\x18ftypisom" + b"\x00" * n


class VideoPayloadTest(unittest.TestCase):
    def test_octet_stream_with_ftyp_is_video(self):
        self.assertEqual(fetch_content._classify_video_payload("application/octet-stream", _mp4()), "")
        self.assertEqual(fetch_content._classify_video_payload("binary/octet-stream", _mp4()), "")
        self.assertEqual(fetch_content._classify_video_payload("", _mp4()), "")

    def test_video_header_case_and_parameters(self):
        self.assertEqual(fetch_content._classify_video_payload("Video/MP4", b"x" * 16), "")
        self.assertEqual(fetch_content._classify_video_payload("video/mp4; charset=binary", b"x" * 8), "")

    def test_html_json_hls_and_images_are_rejected_even_if_labeled_video(self):
        for ctype, body in (
            ("video/mp4", b"<html>login</html>"),
            ("video/mp4", b"<!DOCTYPE html>"),
            ("application/octet-stream", b'{"error":1}'),
            ("video/mp4", b"#EXTM3U\n"),
            ("application/octet-stream", b"\xff\xd8\xff\x00"),
            ("image/jpeg", _mp4()),
            ("text/html", _mp4()),
            ("application/octet-stream", b"x" * 64),
        ):
            self.assertEqual(fetch_content._classify_video_payload(ctype, body), "bad_content_type", ctype)

    def test_webm_magic_under_octet_stream(self):
        self.assertEqual(
            fetch_content._classify_video_payload("application/octet-stream", b"\x1a\x45\xdf\xa3" + b"\x00" * 16),
            "")


class QuotedTweetTest(unittest.TestCase):
    TWEET = {
        "text": "引用する",
        "user": {"name": "Me", "screen_name": "me"},
        "created_at": "2026-11-01T00:00:00.000Z",
        "mediaDetails": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/own.jpg"}],
        "quoted_tweet": {
            "text": "引用元の本文",
            "user": {"name": "Other", "screen_name": "quoted"},
            "mediaDetails": [
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/q1.jpg"},
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/q2.jpg"},
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/q3.jpg"},
            ],
        },
    }

    def test_quote_media_is_separate_and_keeps_order(self):
        seen = []

        def fake(urls, **k):
            seen.append(list(urls))
            return ("説明%d" % len(seen), "")

        with patch("fetch_content._get", return_value=(200, json.dumps(self.TWEET))), \
             patch("fetch_content._gemini_image_understanding", side_effect=fake):
            r = fetch_content.fetch_x("https://x.com/me/status/9", follow_links=False)
        self.assertEqual(len(seen), 2)
        self.assertIn("own.jpg", seen[0][0])
        self.assertEqual(
            [u.split("/")[-1].split("?")[0] for u in seen[1]],
            ["q1.jpg", "q2.jpg", "q3.jpg"])
        self.assertLess(r["text"].find("添付画像の内容"), r["text"].find("引用元 @quoted の添付画像の内容"))
        self.assertIn("引用元 @quoted の本文", r["text"])
        self.assertEqual(r["quoted_handle"], "quoted")
        self.assertNotIn("q1.jpg", seen[0][0])

    def test_disabled_understanding_does_not_read_quote_media(self):
        with patch("fetch_content._get", return_value=(200, json.dumps(self.TWEET))), \
             patch("fetch_content._gemini_image_understanding") as img:
            r = fetch_content.fetch_x("https://x.com/me/status/9", follow_links=False, understand_video=False)
        img.assert_not_called()
        self.assertIn("image_content", r["missing"])


class GroqLogicTest(unittest.TestCase):
    def test_missing_key_warns_and_does_not_raise(self):
        groq_transcribe._warned_missing_key = False
        buf = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
        with patch.dict(os.environ, env, clear=True), redirect_stderr(buf):
            out = groq_transcribe.transcribe_file("/no/such/file")
        self.assertEqual(out["reason"], "no_api_key")
        self.assertFalse(out["ok"])
        self.assertIn("::warning::GROQ_API_KEY is unset", buf.getvalue())
        self.assertNotIn("sk-", buf.getvalue())

    def test_stock_phrase_is_flagged_not_deleted(self):
        text, n = groq_transcribe.stitch_segments([
            (0, [{"start": 0, "end": 2, "text": "こんにちは"},
                 {"start": 2, "end": 5, "text": "ご視聴ありがとうございました", "compression_ratio": 1.1}]),
        ])
        self.assertIn("こんにちは", text)
        self.assertIn("〔要確認〕ご視聴ありがとうございました", text)
        self.assertEqual(n, 1)

    def test_low_chars_and_compression_are_flagged(self):
        _, slow = groq_transcribe.flag_segment("あ", 0, 10, {})
        _, cr = groq_transcribe.flag_segment("普通の文です", 0, 2, {"compression_ratio": 2.5})
        _, ok = groq_transcribe.flag_segment("これは普通の長さの発話です", 0, 2, {"compression_ratio": 1.2})
        self.assertTrue(slow)
        self.assertTrue(cr)
        self.assertFalse(ok)

    def test_stitch_adds_chunk_offset(self):
        text, _ = groq_transcribe.stitch_segments([
            (0, [{"start": 1, "end": 2, "text": "先"}]),
            (30, [{"start": 1, "end": 2, "text": "後"}]),
        ])
        self.assertIn("[0:01 → 0:02] 先", text)
        self.assertIn("[0:31 → 0:32] 後", text)

    def test_split_offsets_when_over_25mb(self):
        one = groq_transcribe.split_offsets(1000, 10, max_bytes=25 * 1024 * 1024)
        many = groq_transcribe.split_offsets(50 * 1024 * 1024, 1000, max_bytes=24 * 1024 * 1024)
        self.assertEqual(one, [0.0])
        self.assertGreater(len(many), 1)
        self.assertEqual(many[0], 0.0)
        self.assertLess(many[1], 1000)

    def test_silence_and_music_gates(self):
        self.assertEqual(groq_transcribe.classify_audio_levels({"max_volume": -50, "mean_volume": -60}, {}), "silent")
        self.assertEqual(groq_transcribe.classify_audio_levels(
            {"max_volume": -5, "mean_volume": -20}, {"mean_volume": -45}), "music_only")
        self.assertEqual(groq_transcribe.classify_audio_levels(
            {"max_volume": -5, "mean_volume": -18}, {"mean_volume": -20}), "")

    def test_ffprobe_requires_an_audio_stream(self):
        self.assertFalse(groq_transcribe.parse_ffprobe_has_audio('{"streams":[{"codec_type":"video"}]}'))
        self.assertTrue(groq_transcribe.parse_ffprobe_has_audio(
            '{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}'))

    def test_budget_overflow_does_not_call_the_api(self):
        groq_transcribe._warned_budget = False
        with tempfile.TemporaryDirectory() as d:
            audio = os.path.join(d, "a.mp4")
            with open(audio, "wb") as f:
                f.write(_mp4())
            bud = os.path.join(d, "bud.json")
            with open(bud, "w", encoding="utf-8") as f:
                json.dump({"date": "2026-11-04", "used_s": 100}, f)
            env = {"GROQ_API_KEY": "secret-key", "GROQ_BUDGET_FILE": bud, "GROQ_BUDGET_SECONDS": "30"}
            with patch.dict(os.environ, env), \
                 patch.object(groq_transcribe.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                 patch.object(groq_transcribe, "_ffprobe_json", return_value='{"streams":[{"codec_type":"audio"}]}'), \
                 patch.object(groq_transcribe, "_duration_sec", return_value=40), \
                 patch.object(groq_transcribe, "_volumedetect", return_value={"max_volume": -10, "mean_volume": -20}), \
                 patch.object(groq_transcribe, "post_transcription", side_effect=AssertionError("api")):
                out = groq_transcribe.transcribe_file(audio, today="2026-11-04", budget_path=bud)
        self.assertEqual(out["reason"], "budget")
        self.assertNotIn("secret-key", json.dumps(out))

    def test_no_audio_stream_skips_before_api(self):
        with tempfile.TemporaryDirectory() as d:
            audio = os.path.join(d, "a.mp4")
            with open(audio, "wb") as f:
                f.write(_mp4())
            with patch.dict(os.environ, {"GROQ_API_KEY": "k"}), \
                 patch.object(groq_transcribe.shutil, "which", return_value="/usr/bin/ffprobe"), \
                 patch.object(groq_transcribe, "_ffprobe_json", return_value='{"streams":[{"codec_type":"video"}]}'), \
                 patch.object(groq_transcribe, "post_transcription", side_effect=AssertionError("api")):
                out = groq_transcribe.transcribe_file(audio)
        self.assertEqual(out["reason"], "no_audio_stream")

    def test_youtube_over_15_min_does_not_download(self):
        class Proc:
            stdout = b"1200\n"
            returncode = 0
        with patch.dict(os.environ, {"GROQ_API_KEY": "k"}), \
             patch.object(groq_transcribe, "ytdlp_bin", return_value="yt-dlp"), \
             patch.object(groq_transcribe, "_run", return_value=Proc()) as run, \
             patch.object(groq_transcribe, "transcribe_file", side_effect=AssertionError("file")):
            out = groq_transcribe.transcribe_youtube("https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(out["reason"], "duration_cap")
        self.assertEqual(run.call_count, 1)
        cmd = run.call_args[0][0]
        self.assertIn("--no-cookies", cmd)
        self.assertNotIn("--cookies", cmd)
        self.assertNotIn("--username", cmd)

    def test_audio_argv_caps_duration_and_refuses_cookies(self):
        cmd = groq_transcribe.ytdlp_audio_argv("https://youtu.be/abcdefghijk", "/tmp/a.%(ext)s", 900)
        self.assertIn("--no-cookies", cmd)
        self.assertNotIn("--cookies", cmd)
        self.assertNotIn("--cookies-from-browser", cmd)
        self.assertIn("duration <= 900", cmd)
        self.assertIn("--max-filesize", cmd)
        self.assertFalse(groq_transcribe._argv_has_secret_flags(cmd))
        self.assertTrue(cmd[-2].startswith("/tmp/"))


class InstagramAcquireTest(unittest.TestCase):
    def test_order_is_ytdlp_then_fix_then_instaloader_and_stops(self):
        calls = []

        def ytdlp(url, deadline):
            calls.append("ytdlp")
            return "/tmp/v.mp4" if calls.count("ytdlp") == 1 and stop else None

        stop = True
        with patch("fetch_content._ytdlp_download_reel", side_effect=ytdlp), \
             patch("fetch_content._fetch_instagramfix_video_url", side_effect=lambda *a, **k: calls.append("fix") or (None, "x")), \
             patch("fetch_content._instaloader_fetch", side_effect=lambda *a, **k: calls.append("ilo") or {"ok": False}):
            local, remote, reason, source = fetch_content._acquire_instagram_reel(
                "https://www.instagram.com/reel/ABC/", None)
        self.assertEqual((calls, source, local), (["ytdlp"], "ytdlp", "/tmp/v.mp4"))

        calls.clear()
        stop = False
        with patch("fetch_content._ytdlp_download_reel", side_effect=ytdlp), \
             patch("fetch_content._fetch_instagramfix_video_url",
                   side_effect=lambda *a, **k: calls.append("fix") or ("https://cdn.example/a.mp4", "")), \
             patch("fetch_content._instaloader_fetch", side_effect=AssertionError("ilo")):
            local, remote, reason, source = fetch_content._acquire_instagram_reel(
                "https://www.instagram.com/reel/ABC/", None)
        self.assertEqual(calls, ["ytdlp", "fix"])
        self.assertEqual(source, "instagramfix")
        self.assertEqual(remote, "https://cdn.example/a.mp4")

    def test_ytdlp_argv_has_no_cookie_file(self):
        cmd = fetch_content._ytdlp_reel_argv("https://www.instagram.com/reel/ABC/", "/tmp/v.%(ext)s")
        self.assertIn("--no-cookies", cmd)
        self.assertNotIn("--cookies", cmd)
        self.assertNotIn("--username", cmd)
        self.assertIn("100M", cmd)

    def test_instaloader_does_not_login_and_keeps_sidecar_order(self):
        nodes = [
            SimpleNamespace(is_video=False, display_url="https://instagram.f.fbcdn.net/1.jpg"),
            SimpleNamespace(is_video=False, display_url="https://instagram.f.fbcdn.net/2.jpg"),
        ]

        class Post:
            typename = "GraphSidecar"

            @staticmethod
            def from_shortcode(ctx, code):
                return Post()

            def get_sidecar_nodes(self):
                return nodes

        loader = MagicMock()
        loader.context = object()
        instaloader = SimpleNamespace(Instaloader=MagicMock(return_value=loader), Post=Post)
        with tempfile.TemporaryDirectory() as d:
            got = fetch_content._instaloader_read_post(
                "https://www.instagram.com/p/ABC/", d, instaloader, "ABC")
        loader.login.assert_not_called()
        loader.load_session_from_file.assert_not_called()
        self.assertEqual(got["images"], [
            "https://instagram.f.fbcdn.net/1.jpg",
            "https://instagram.f.fbcdn.net/2.jpg",
        ])


class AuxIsolationTest(unittest.TestCase):
    def test_nested_instagram_fetch_restores_outer_transcript(self):
        fetch_content._aux["transcript"] = {"ok": True, "text": "outer", "reason": "", "flagged": 0, "duration_s": 1}
        fetch_content._aux["download"] = {"status": "ok", "reason": ""}
        try:
            with patch("fetch_content._get", return_value=(404, "")):
                fetch_content.fetch_instagram("https://www.instagram.com/p/ABC/")
            self.assertEqual(fetch_content._aux["transcript"]["text"], "outer")
            self.assertEqual(fetch_content._aux["download"]["status"], "ok")
        finally:
            fetch_content._reset_aux()


class FactsStageTest(unittest.TestCase):
    def test_unique_success_survives_a_later_failure(self):
        url = "https://x.com/a/status/1?utm_source=x"
        with tempfile.TemporaryDirectory() as d:
            with patch.object(fetch_content, "FACTS_DIR", d):
                ok = fetch_content._facts(url, 0, {
                    "ok": True, "success": True, "type": "x",
                    "stages": {"download": {"status": "ok", "reason": ""},
                               "frames": {"status": "skipped", "reason": "gemini_native"},
                               "transcription": {"status": "skipped", "reason": "no_api_key"},
                               "understanding": {"status": "ok", "reason": ""}}})
                self.assertNotIn("audio_transcript", ok)
                self.assertIn("stages", ok)
                fetch_content.record_facts(ok, day="2026-11-04")
                bad = dict(ok)
                bad["success"] = False
                bad["stages"] = dict(ok["stages"])
                fetch_content.record_facts(bad, day="2026-11-04")
                with open(os.path.join(d, "2026-11-04.json"), encoding="utf-8") as f:
                    store = json.load(f)
        rec = store[url]
        self.assertFalse(rec["success"])
        self.assertTrue(rec["unique_success"])
        self.assertTrue(rec["prior_success"])
        self.assertEqual(rec["attempt_seq"], 2)
        self.assertEqual(rec["stages"]["transcription"]["reason"], "no_api_key")
        self.assertNotIn("ご視聴", json.dumps(rec))


if __name__ == "__main__":
    unittest.main()
