# -*- coding: utf-8 -*-
"""Gemini 呼び出しの再試行（fetch_content._gemini_client）と理由コードの回帰テスト。

本物の google-genai SDK を、手元のダミー HTTP サーバ（127.0.0.1）に向けて動かす。
503 が2回続いて3回目で成功／429 は再試行しない／使い切ると exception:ServerError:503／
接続が途中で切れても再試行する、を実物の再試行機構で確かめる。google-genai が無い環境では skip。
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fetch_content  # noqa: E402
import ledger  # noqa: E402

OK_BODY = {"candidates": [{"content": {"role": "model", "parts": [{"text": "要約です"}]}}]}


def _serve(script):
    """script = 応答のリスト。int=そのHTTPステータスで空JSON、"ok"=成功、"cut"=本文なしで切断。"""
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            hits.append(1)
            step = script[min(len(hits) - 1, len(script) - 1)]
            if step == "cut":
                self.connection.close()
                return
            code, body = (200, OK_BODY) if step == "ok" else (step, {"error": {"code": step, "message": "x", "status": "S"}})
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


@unittest.skipUnless(fetch_content._GEMINI_AVAILABLE, "google-genai 未導入")
class GeminiRetryTest(unittest.TestCase):
    def _run(self, script):
        srv, hits = _serve(script)
        self.addCleanup(srv.shutdown)
        env = {"GOOGLE_GEMINI_BASE_URL": "http://127.0.0.1:%d" % srv.server_port}
        with patch.dict(os.environ, env), \
             patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
             patch("tenacity.nap.time.sleep"), patch("fetch_content.time.sleep"):   # 待ち時間(5→10秒)は飛ばす
            client = fetch_content._gemini_client()
            try:
                return fetch_content._gemini_generate(client, "x"), None, hits
            except Exception as e:  # noqa: BLE001
                return None, e, hits

    def test_503_twice_then_success(self):
        resp, err, hits = self._run([503, 503, "ok"])
        self.assertIsNone(err)
        self.assertEqual(resp.text, "要約です")
        self.assertEqual(len(hits), 3)

    def test_429_not_retried_and_detected_as_quota(self):
        _, err, hits = self._run([429, "ok"])
        self.assertEqual(len(hits), 1)
        self.assertTrue(fetch_content._is_quota_error(err))

    def test_exhausted_503_reason_has_status(self):
        _, err, hits = self._run([503])
        self.assertEqual(len(hits), 3)
        self.assertFalse(fetch_content._is_quota_error(err))
        reason = fetch_content._exception_reason(err)
        self.assertEqual(reason, "exception:ServerError:503")
        # 新しい形でも ledger は transient（再取得対象）のまま
        rec = {"video_reason": reason, "missing": ["video_content"], "ok": True, "route": "x"}
        self.assertEqual(ledger._video_reason_kind(rec), "transient")

    def test_connection_cut_is_retried(self):
        resp, err, hits = self._run(["cut", "ok"])
        self.assertIsNone(err)
        self.assertEqual(len(hits), 2)


class ExceptionReasonTest(unittest.TestCase):
    def test_no_status_for_plain_exceptions(self):
        self.assertEqual(fetch_content._exception_reason(RuntimeError("x")), "exception:RuntimeError")

    def test_remote_protocol_error_keeps_type_name(self):
        import httpx
        self.assertEqual(fetch_content._exception_reason(httpx.RemoteProtocolError("cut")),
                         "exception:RemoteProtocolError")


if __name__ == "__main__":
    unittest.main()
