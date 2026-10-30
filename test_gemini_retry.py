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


def _serve(script, models=None):
    """script = 応答のリスト。int=そのHTTPステータスで空JSON、"ok"=成功、"cut"=本文なしで切断。"""
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            hits.append(1)
            if models is not None:
                models.append(self.path.split("/models/")[1].split(":")[0])
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
    """主モデル(SDK再試行で最大2回)→だめなら予備モデルで1回。何回目の呼び出しがどのモデルかも見る。"""

    def _run(self, script):
        models = []
        srv, hits = _serve(script, models)
        self.addCleanup(srv.shutdown)
        env = {"GOOGLE_GEMINI_BASE_URL": "http://127.0.0.1:%d" % srv.server_port}
        with patch.dict(os.environ, env),              patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"),              patch("tenacity.nap.time.sleep"), patch("fetch_content.time.sleep"):   # 待ち(5秒)は飛ばす
            try:
                return fetch_content._gemini_generate("x"), None, models
            except Exception as e:  # noqa: BLE001
                return None, e, models

    def test_503_once_then_primary_succeeds(self):
        resp, err, models = self._run([503, "ok"])
        self.assertIsNone(err)
        self.assertEqual(models, ["gemini-flash-latest"] * 2)
        self.assertEqual(fetch_content._last_model, "gemini-flash-latest")

    def test_primary_exhausted_falls_back_to_lite_once(self):
        resp, err, models = self._run([503, 503, "ok"])
        self.assertIsNone(err)
        self.assertEqual(resp.text, "要約です")
        self.assertEqual(models, ["gemini-flash-latest"] * 2 + ["gemini-flash-lite-latest"])
        self.assertEqual(fetch_content._last_model, "gemini-flash-lite-latest")

    def test_429_not_retried_nor_fallback_and_detected_as_quota(self):
        _, err, models = self._run([429, "ok"])
        self.assertEqual(len(models), 1)
        self.assertTrue(fetch_content._is_quota_error(err))

    def test_all_fail_reports_primary_error_with_status(self):
        _, err, models = self._run([503])
        self.assertEqual(len(models), 3)    # 主2 + 予備1。それ以上は粘らない
        self.assertFalse(fetch_content._is_quota_error(err))
        reason = fetch_content._exception_reason(err)
        self.assertEqual(reason, "exception:ServerError:503")
        rec = {"video_reason": reason, "missing": ["video_content"], "ok": True, "route": "x"}
        self.assertEqual(ledger._video_reason_kind(rec), "transient")

    def test_connection_cut_falls_back_to_lite(self):
        resp, err, models = self._run(["cut", "ok"])
        self.assertIsNone(err)
        self.assertEqual(models, ["gemini-flash-latest", "gemini-flash-lite-latest"])

    def test_no_fallback_when_time_budget_spent(self):
        srv, hits = _serve([503])
        self.addCleanup(srv.shutdown)
        env = {"GOOGLE_GEMINI_BASE_URL": "http://127.0.0.1:%d" % srv.server_port}
        with patch.dict(os.environ, env), patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"),              patch("tenacity.nap.time.sleep"):
            with self.assertRaises(Exception):
                fetch_content._gemini_generate("x", t0=0.0)   # 開始が遥か昔＝330秒超過
        self.assertEqual(len(hits), 2)    # 主2回のみ


class ExceptionReasonTest(unittest.TestCase):
    def test_no_status_for_plain_exceptions(self):
        self.assertEqual(fetch_content._exception_reason(RuntimeError("x")), "exception:RuntimeError")

    def test_remote_protocol_error_keeps_type_name(self):
        import httpx
        self.assertEqual(fetch_content._exception_reason(httpx.RemoteProtocolError("cut")),
                         "exception:RemoteProtocolError")


if __name__ == "__main__":
    unittest.main()
