# -*- coding: utf-8 -*-
"""Gemini 無料枠切れ(429/RESOURCE_EXHAUSTED)の判定の回帰テスト
（2026-09-23 Codex 2周目レビュー P1 対応）。

元の判定は `str(e)` に "429"・"RESOURCE_EXHAUSTED"・"quota" のどれかが含まれる
かだけを見ていた。これだと ResourceExhausted("resource exhausted") のように
メッセージに "429" が出ない例外や、`.code=429` を持つのにメッセージへ数字が
出ない例外（google-genai の errors.ClientError 等）を取りこぼし、
backfill.py 側の quota_stopped 打ち切りが発火しなかった。

このファイルは3層を検査する:
  1. fetch_content._is_quota_error(e) 単体（型名・属性・メッセージのどれからでも判定）
  2. fetch_content._gemini_video_understanding() の例外→理由コード変換（通し）
  3. backfill._is_quota_reason(video_reason) 単体（gemini_quota に加え
     exception:ResourceExhausted / exception:TooManyRequests も拾う多重防御）
"""

import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import fetch_content  # noqa: E402
import backfill  # noqa: E402


class ResourceExhausted(Exception):
    """google.api_core.exceptions.ResourceExhausted 相当の偽物。
    型名だけで判定できることを確かめるため、メッセージには "429" 等を一切含めない。
    ⚠️ クラス名を本物と同じ "ResourceExhausted" にする（型名判定は
    `type(e).__name__` を見るため、テスト用に先頭へ `_Fake` 等を付けると
    別の型名になってしまい判定対象からずれる）。"""
    def __init__(self, message="resource exhausted"):
        super().__init__(message)


class TooManyRequests(Exception):
    """型名だけで判定できることを確かめる別ケース。クラス名は本物と同じにする
    （理由は ResourceExhausted と同じ）。"""
    def __init__(self, message="slow down"):
        super().__init__(message)


class _FakeClientError(Exception):
    """google.genai.errors.ClientError 相当の偽物。code(int)/status(str)/message(str)を
    持つが、メッセージ本文には枠切れを示す語を一切含めない（属性だけで判定できることの確認）。"""
    def __init__(self, code, status, message="unrelated failure text"):
        self.code = code
        self.status = status
        self.message = message
        super().__init__(message)


class IsQuotaErrorTest(unittest.TestCase):
    """fetch_content._is_quota_error(e) 単体テスト。"""

    def test_type_name_resource_exhausted_without_429_in_message(self):
        e = ResourceExhausted("resource exhausted")
        self.assertNotIn("429", str(e))
        self.assertTrue(fetch_content._is_quota_error(e))

    def test_type_name_too_many_requests(self):
        e = TooManyRequests("slow down")
        self.assertTrue(fetch_content._is_quota_error(e))

    def test_code_attribute_429_with_unrelated_message(self):
        e = _FakeClientError(code=429, status=None, message="something else entirely")
        self.assertNotIn("429", e.message)
        self.assertTrue(fetch_content._is_quota_error(e))

    def test_status_attribute_resource_exhausted(self):
        e = _FakeClientError(code=None, status="RESOURCE_EXHAUSTED", message="unrelated")
        self.assertTrue(fetch_content._is_quota_error(e))

    def test_status_attribute_lowercase_still_detected(self):
        """属性値の大文字小文字表記の揺れを吸収する。"""
        e = _FakeClientError(code=None, status="resource_exhausted", message="unrelated")
        self.assertTrue(fetch_content._is_quota_error(e))

    def test_existing_string_match_still_works(self):
        """既存の文字列判定（後方互換）は残っている。"""
        e = RuntimeError("HTTP 429 Too Many Requests")
        self.assertTrue(fetch_content._is_quota_error(e))

    def test_unrelated_exception_is_not_quota(self):
        e = ConnectionError("connection reset by peer")
        self.assertFalse(fetch_content._is_quota_error(e))

    def test_unrelated_exception_with_code_attribute_is_not_quota(self):
        """code属性があっても429でなければ枠切れと誤判定しない。"""
        e = _FakeClientError(code=500, status="INTERNAL", message="server error")
        self.assertFalse(fetch_content._is_quota_error(e))


class GeminiVideoUnderstandingQuotaIntegrationTest(unittest.TestCase):
    """_gemini_video_understanding() が例外を gemini_quota へ変換することを通しで確認する。
    test_reel_video.py の GeminiVideoUnderstandingXPathUnaffectedTest と同じ方式
    （urllib.request.urlopen を直接例外で落として except ブロックへ到達させる）。"""

    def test_resource_exhausted_type_name_becomes_gemini_quota(self):
        with patch.object(fetch_content, "_GEMINI_AVAILABLE", True), \
             patch.object(fetch_content, "_GEMINI_API_KEY", "test-key"), \
             patch("urllib.request.urlopen",
                   side_effect=ResourceExhausted("resource exhausted")):
            text, reason = fetch_content._gemini_video_understanding(
                "https://video.twimg.com/a.mp4")  # allowed_hosts省略=None(X動画経路)
        self.assertEqual(text, "")
        self.assertEqual(reason, "gemini_quota")

    def test_client_error_code_429_becomes_gemini_quota(self):
        with patch.object(fetch_content, "_GEMINI_AVAILABLE", True), \
             patch.object(fetch_content, "_GEMINI_API_KEY", "test-key"), \
             patch("urllib.request.urlopen",
                   side_effect=_FakeClientError(code=429, status=None,
                                                message="unrelated text")):
            text, reason = fetch_content._gemini_video_understanding(
                "https://video.twimg.com/a.mp4")
        self.assertEqual(text, "")
        self.assertEqual(reason, "gemini_quota")

    def test_unrelated_runtime_error_is_not_reclassified(self):
        """無関係な例外は従来どおり exception:<型名> のまま
        （既存の GeminiVideoUnderstandingXPathUnaffectedTest と同じ経路の裏取り）。"""
        with patch.object(fetch_content, "_GEMINI_AVAILABLE", True), \
             patch.object(fetch_content, "_GEMINI_API_KEY", "test-key"), \
             patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
            text, reason = fetch_content._gemini_video_understanding(
                "https://video.twimg.com/a.mp4")
        self.assertEqual(text, "")
        self.assertEqual(reason, "exception:RuntimeError")


class BackfillIsQuotaReasonTest(unittest.TestCase):
    """backfill._is_quota_reason(video_reason) 単体テスト（多重防御側）。"""

    def test_gemini_quota_is_quota_reason(self):
        self.assertTrue(backfill._is_quota_reason("gemini_quota"))

    def test_exception_resource_exhausted_prefix_is_quota_reason(self):
        self.assertTrue(backfill._is_quota_reason("exception:ResourceExhausted"))

    def test_exception_too_many_requests_prefix_is_quota_reason(self):
        self.assertTrue(backfill._is_quota_reason("exception:TooManyRequests"))

    def test_case_insensitive(self):
        self.assertTrue(backfill._is_quota_reason("exception:resourceexhausted"))

    def test_unrelated_exception_reason_is_not_quota(self):
        self.assertFalse(backfill._is_quota_reason("exception:ConnectionError"))

    def test_empty_or_none_is_not_quota(self):
        self.assertFalse(backfill._is_quota_reason(""))
        self.assertFalse(backfill._is_quota_reason(None))

    def test_other_video_reason_codes_are_not_quota(self):
        for code in ("gemini_timeout", "truncated_download", "host_not_allowed",
                      "instagram_relay_unavailable"):
            self.assertFalse(backfill._is_quota_reason(code), code)


if __name__ == "__main__":
    unittest.main()
