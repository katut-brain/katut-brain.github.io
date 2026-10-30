# -*- coding: utf-8 -*-
"""Gemini 呼び出しの再試行（fetch_content._gemini_client）と理由コードの回帰テスト。

本物の google-genai SDK を、手元のダミー HTTP サーバ（127.0.0.1）に向けて動かす。
503 が2回続いて3回目で成功／429 は再試行しない／使い切ると exception:ServerError:503／
接続が途中で切れても再試行する、を実物の再試行機構で確かめる。google-genai が無い環境では skip。
"""
import json
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
            if step == "slow":   # クライアントのタイムアウトを越えて黙る
                threading.Event().wait(1.0)   # time.sleep は tenacity 用に差し替わるので使わない
                step = "ok"
            if step == "cut":
                self.connection.close()
                return
            code, body = (200, OK_BODY) if step == "ok" else (step, {"error": {"code": step, "message": "x", "status": "S"}})
            raw = json.dumps(body).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                pass   # クライアントがタイムアウトで先に切った

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


class _MarkerMixin(unittest.TestCase):
    """主モデルの死活の印ファイルを、テストごとの一時パスに差し替える(実環境の印に触れない)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.marker = os.path.join(self._tmp.name, "primary_down")
        p = patch.object(fetch_content, "_PRIMARY_DOWN_FILE", self.marker)
        p.start()
        self.addCleanup(p.stop)


@unittest.skipUnless(fetch_content._GEMINI_AVAILABLE, "google-genai 未導入")
class GeminiRetryTest(_MarkerMixin):
    """主モデル(SDK再試行で最大2回)→一過性の失敗なら予備モデルで1回。どの呼び出しがどのモデルかも見る。"""

    def _run(self, script, timeout_s=None):
        models = []
        srv, hits = _serve(script, models)
        self.addCleanup(srv.shutdown)
        env = {"GOOGLE_GEMINI_BASE_URL": "http://127.0.0.1:%d" % srv.server_port}
        extra = [patch.object(fetch_content, "_GENERATE_TIMEOUT_S", timeout_s)] if timeout_s else []
        with patch.dict(os.environ, env), \
             patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
             patch("tenacity.nap.time.sleep"), patch("fetch_content.time.sleep"):   # 待ち(5秒)は飛ばす
            for e in extra:
                e.start()
                self.addCleanup(e.stop)
            try:
                return fetch_content._gemini_generate("x"), None, models
            except Exception as e:  # noqa: BLE001
                return None, e, models

    def test_503_once_then_primary_succeeds(self):
        resp, err, models = self._run([503, "ok"])
        self.assertIsNone(err)
        self.assertEqual(models, ["gemini-flash-latest"] * 2)
        self.assertEqual(fetch_content._last["model"], "gemini-flash-latest")
        self.assertFalse(os.path.exists(self.marker))

    def test_primary_exhausted_falls_back_to_lite_once(self):
        resp, err, models = self._run([503, 503, "ok"])
        self.assertIsNone(err)
        self.assertEqual(resp.text, "要約です")
        self.assertEqual(models, ["gemini-flash-latest"] * 2 + ["gemini-flash-lite-latest"])
        self.assertEqual(fetch_content._last["model"], "gemini-flash-lite-latest")
        # 予備で成功しても主の失敗理由が残る
        self.assertEqual(fetch_content._last["primary_reason"], "exception:ServerError:503")
        self.assertTrue(os.path.exists(self.marker))   # 主は死んでいる印

    def test_timeout_twice_then_fallback_once(self):
        # 主は1秒黙る→0.3秒のタイムアウトで2回切れ、3回目(予備)は即応答する想定。
        # サーバは "slow" を 1.0 秒後に ok で返すので、主2回はタイムアウトし予備も同様に黙る…ため
        # 予備側は即応答させる(3回目だけ ok)。
        resp, err, models = self._run(["slow", "slow", "ok"], timeout_s=0.3)
        self.assertEqual(models[:2], ["gemini-flash-latest"] * 2)
        self.assertEqual(models[2], "gemini-flash-lite-latest")
        self.assertTrue(fetch_content._last["primary_reason"].startswith("exception:"))

    def test_403_is_not_sent_to_fallback(self):
        _, err, models = self._run([403, "ok"])
        self.assertEqual(models, ["gemini-flash-latest"])    # 予備を呼ばない・再試行もしない
        self.assertEqual(fetch_content._exception_reason(err), "exception:ClientError:403")
        self.assertFalse(os.path.exists(self.marker))

    def test_429_not_retried_nor_fallback_and_detected_as_quota(self):
        _, err, models = self._run([429, "ok"])
        self.assertEqual(len(models), 1)
        self.assertTrue(fetch_content._is_quota_error(err))

    def test_fallback_429_is_quota(self):
        _, err, models = self._run([503, 503, 429])
        self.assertEqual(len(models), 3)
        self.assertTrue(fetch_content._is_quota_error(err))
        self.assertEqual(fetch_content._last["fallback_reason"], "gemini_quota")

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
        self.assertFalse(os.path.exists(self.marker))   # 接続断1回では死んでいる印を付けない

    def test_timeouts_twice_mark_primary_down(self):
        self._run(["slow", "slow", "ok"], timeout_s=0.3)
        self.assertTrue(os.path.exists(self.marker))

    def test_marker_lasts_15_minutes(self):
        self._run([503, 503, "ok"])
        with open(self.marker) as f:
            until = float(f.read())
        self.assertLessEqual(until - time.time(), 900 + 5)
        self.assertGreater(until - time.time(), 800)

    def test_marker_makes_next_call_skip_primary(self):
        self._run([503, 503, "ok"])                      # 主が死ぬ→印
        resp, err, models = self._run(["ok"])            # 次の呼び出しは最初から予備
        self.assertIsNone(err)
        self.assertEqual(models, ["gemini-flash-lite-latest"])
        self.assertTrue(fetch_content._last["primary_skipped"])

    def test_expired_or_broken_marker_is_ignored(self):
        with open(self.marker, "w") as f:
            f.write(str(time.time() - 10))               # 期限切れ
        self.assertFalse(fetch_content._primary_down())
        with open(self.marker, "w") as f:
            f.write("not a number")                      # 壊れている
        self.assertFalse(fetch_content._primary_down())

    def test_unwritable_marker_is_ignored(self):
        with patch.object(fetch_content, "_PRIMARY_DOWN_FILE", os.path.join(self._tmp.name, "no", "dir", "x")):
            fetch_content._mark_primary_down()           # 例外を出さない

    def test_deadline_already_spent_raises_without_calling(self):
        srv, hits = _serve(["ok"])
        self.addCleanup(srv.shutdown)
        env = {"GOOGLE_GEMINI_BASE_URL": "http://127.0.0.1:%d" % srv.server_port}
        with patch.dict(os.environ, env), patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"):
            with self.assertRaises(fetch_content._DeadlineSkipped):
                fetch_content._gemini_generate("x", deadline=fetch_content._now() - 1)
        self.assertEqual(hits, [])
        self.assertEqual(fetch_content._exception_reason(fetch_content._DeadlineSkipped()), "deadline_skipped")
        self.assertEqual(ledger._video_reason_kind(
            {"video_reason": "deadline_skipped", "missing": ["video_content"]}), "transient")

    def test_fallback_skipped_when_budget_runs_out_after_primary(self):
        srv, hits = _serve([503])
        self.addCleanup(srv.shutdown)
        env = {"GOOGLE_GEMINI_BASE_URL": "http://127.0.0.1:%d" % srv.server_port}
        clock = iter([0.0] + [430.0] * 20)          # 主の前は余裕あり、主の後は残り10秒
        with patch.dict(os.environ, env), patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
             patch("tenacity.nap.time.sleep"), patch.object(fetch_content, "_now", lambda: next(clock)):
            with self.assertRaises(Exception) as cm:
                fetch_content._gemini_generate("x", deadline=440.0)
        self.assertEqual(len(hits), 2)                    # 主2回のみ。予備は始めない
        self.assertEqual(fetch_content._exception_reason(cm.exception), "exception:ServerError:503")


class _FakeHttpResp:
    """video/mp4(または画像)を返す偽のレスポンス(取得に所定の秒数かかる)。"""
    def __init__(self, clock, secs, ctype="video/mp4"):
        clock[0] += secs
        self.headers = {"Content-Type": ctype}
        self._chunks = [b"x" * 10, b""]

    def read(self, n=-1):
        return self._chunks.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@unittest.skipUnless(fetch_content._GEMINI_AVAILABLE, "google-genai 未導入")
class ItemBudgetTest(_MarkerMixin):
    """動画も画像(4枚)もある X 投稿で、全部の呼び出しが「持ち時間いっぱい使って失敗」しても
    1件の所要が手順書の timeout 480 秒に収まる。時計・ネットワーク・SDK は偽物。"""

    TWEET = {"text": "t", "user": {}, "created_at": "2026-10-01T00:00:00Z",
             "mediaDetails": [{"type": "video", "video_info": {"variants": [
                 {"content_type": "video/mp4", "url": "https://video.twimg.com/a.mp4", "bitrate": 1}]}}]
             + [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/%d.jpg" % i} for i in range(4)]}

    def _run(self, video_dl_secs, upload_ok=False, links=False):
        clock = [0.0]
        embedded = []   # _fetch_x_embedded_link に渡された timeout

        class Models:
            def generate_content(inner, model, contents):
                clock[0] += inner.timeout * inner.attempts + 6 * (inner.attempts - 1)
                raise TimeoutError("slow")

        class Files:
            def upload(inner, file):
                if upload_ok:   # 動画も画像も取れて generate まで進む最悪経路
                    return SimpleNamespace(state=SimpleNamespace(name="ACTIVE"), name="f")
                clock[0] += inner.timeout
                raise TimeoutError("slow")

        def fake_client(timeout_ms=90_000, attempts=1):
            c = type("C", (), {})()
            c.timeout, c.attempts = timeout_ms / 1000.0, attempts
            c.models = Models()
            c.models.timeout, c.models.attempts = c.timeout, attempts
            c.files = Files()
            c.files.timeout = c.timeout
            return c

        class Opener:
            def open(inner, req, timeout=None):
                if upload_ok:
                    return _FakeHttpResp(clock, min(20, timeout), "image/jpeg")
                clock[0] += timeout
                raise TimeoutError("slow")

        def fake_urlopen(req, timeout=None):
            if "t.co" in req.full_url:   # t.co 展開: 持ち時間いっぱい使ってから本物の形の応答を返す
                clock[0] += timeout
                resp = _FakeHttpResp(clock, 0, "text/html")
                resp._chunks = [b'location.replace("https://example.com/a")', b""]
                resp.url = req.full_url
                return resp
            return _FakeHttpResp(clock, min(video_dl_secs, timeout))

        def fake_embedded(expanded, timeout=45):
            embedded.append(timeout)
            clock[0] += timeout          # 埋め込みリンク取得も持ち時間いっぱい
            return None

        def fake_get(url, ua, **k):
            clock[0] += 12
            return 200, json.dumps(dict(self.TWEET, text="t https://t.co/abc") if links else self.TWEET)

        with patch.object(fetch_content, "_now", lambda: clock[0]), \
             patch.object(fetch_content, "_GEMINI_API_KEY", "dummy"), \
             patch.object(fetch_content, "_gemini_client", fake_client), \
             patch.object(fetch_content, "_get", fake_get), \
             patch("urllib.request.urlopen", fake_urlopen), \
             patch("urllib.request.build_opener", lambda *a: Opener()), \
             patch.object(fetch_content, "_fetch_x_embedded_link", fake_embedded), patch.object(fetch_content, "_primary_down", lambda: False),              patch.object(fetch_content, "_mark_primary_down", lambda: None),              patch("fetch_content.time.sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            r = fetch_content.fetch_x("https://x.com/h/status/1", follow_links=links)
        self.embedded = embedded
        return clock[0], r

    def test_everything_slow_stays_under_480(self):
        for secs in (0, 30, 59):
            total, r = self._run(secs)
            self.assertLessEqual(total, 480, "video_dl=%s" % secs)
            self.assertGreater(total, 0)
            self.assertIn("video_content", r["missing"])

    def test_image_failure_leaves_reason(self):
        total, r = self._run(59)
        self.assertLessEqual(total, 480)
        self.assertTrue(r["image_reason"])           # 画像は読めず理由が残る
        self.assertIn("image_content", r["missing"])

    def test_video_and_images_both_reach_generate_worst_case(self):
        """動画の generate(主2回+予備)も、画像の generate も、毎回タイムアウトまで使う最悪経路。
        予算が無ければ 12+59+231+80+231=613 秒になるところを 440 秒で頭打ちにする。"""
        total, r = self._run(59, upload_ok=True)
        self.assertLessEqual(total, 480)
        self.assertGreater(total, 360)               # Gemini の持ち時間(380秒)を使い切るところまで走った
        self.assertIn("video_content", r["missing"])
        self.assertIn("image_content", r["missing"])


    def test_link_video_images_all_slow_stays_under_480(self):
        """リンク付き・動画あり・画像4枚あり。初回取得・動画・画像・t.co展開・埋め込みリンクの
        全部が持ち時間いっぱい粘る最悪ケース。Gemini は開始から380秒で止まり、後半に時間が残る。"""
        total, r = self._run(59, upload_ok=True, links=True)
        self.assertLess(total, 480)
        self.assertGreater(total, 400)               # 予算を使い切るところまで走った
        self.assertEqual(len(self.embedded), 1)      # 埋め込みリンクまで到達している
        self.assertLessEqual(self.embedded[0], 45)

    def test_links_skipped_and_recorded_when_budget_is_gone(self):
        """Gemini が終わった時点で残りが足りなければ、リンク段を飛ばして facts に残す。"""
        with patch.object(fetch_content, "_LINK_RESERVE_S", -10):   # 後半の予約を無くして予算を食い切らせる
            total, r = self._run(59, upload_ok=True, links=True)
        self.assertLess(total, 480)
        self.assertTrue(set(r["deadline_skipped"]) & {"tco_expand", "embedded_link"})
        facts = fetch_content._facts("https://x.com/h/status/1", 0, r)
        self.assertEqual(facts["deadline_skipped"], r["deadline_skipped"])


@unittest.skipUnless(fetch_content._GEMINI_AVAILABLE, "google-genai(httpx) 未導入")
class TransientJudgementTest(unittest.TestCase):
    """HTTP ステータスを持つ例外は、型が httpx でもステータスで決める(4xx は 408 以外すべて非一過性)。"""

    def _status_error(self, code):
        import httpx
        return httpx.HTTPStatusError("x", request=httpx.Request("GET", "http://x"),
                                     response=httpx.Response(code))

    def test_httpx_status_error_403_is_not_transient(self):
        self.assertFalse(fetch_content._is_transient_failure(self._status_error(403)))
        self.assertFalse(fetch_content._is_transient_failure(self._status_error(429)))

    def test_httpx_status_error_503_and_408_are_transient(self):
        self.assertTrue(fetch_content._is_transient_failure(self._status_error(503)))
        self.assertTrue(fetch_content._is_transient_failure(self._status_error(408)))

    def test_status_attribute_variants(self):
        for attr, code, want in (("code", 403, False), ("status_code", 403, False),
                                 ("code", 502, True), ("status_code", 504, True)):
            e = Exception("x")
            setattr(e, attr, code)
            self.assertEqual(fetch_content._is_transient_failure(e), want, (attr, code))

    def test_no_status_only_timeouts_and_connection_errors(self):
        import httpx
        for e in (httpx.ReadTimeout("t"), httpx.ConnectError("c"), httpx.ReadError("r"),
                  httpx.RemoteProtocolError("p"), TimeoutError(), ConnectionResetError()):
            self.assertTrue(fetch_content._is_transient_failure(e), type(e).__name__)
        for e in (ValueError("v"), RuntimeError("r"), FileNotFoundError("f")):
            self.assertFalse(fetch_content._is_transient_failure(e), type(e).__name__)

    def test_marker_judgement_excludes_plain_connection_errors(self):
        import httpx
        self.assertTrue(fetch_content._is_timeout_or_5xx(httpx.ReadTimeout("t")))
        self.assertTrue(fetch_content._is_timeout_or_5xx(self._status_error(503)))
        self.assertFalse(fetch_content._is_timeout_or_5xx(httpx.RemoteProtocolError("p")))
        self.assertFalse(fetch_content._is_timeout_or_5xx(httpx.ConnectError("c")))


class ExceptionReasonTest(unittest.TestCase):
    def test_no_status_for_plain_exceptions(self):
        self.assertEqual(fetch_content._exception_reason(RuntimeError("x")), "exception:RuntimeError")

    def test_remote_protocol_error_keeps_type_name(self):
        import httpx
        self.assertEqual(fetch_content._exception_reason(httpx.RemoteProtocolError("cut")),
                         "exception:RemoteProtocolError")


if __name__ == "__main__":
    unittest.main()


class GetDeadlineTest(unittest.TestCase):
    """_get に件単位の締切を渡すと、タイムアウトは min(既定値, 残り)、締切後は取りに行かない。"""

    def test_timeout_is_clamped_to_remaining(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["t"] = timeout
            return _FakeHttpResp([0.0], 0, "text/html")

        with patch("urllib.request.urlopen", fake_urlopen), patch.object(fetch_content, "_now", lambda: 100.0):
            fetch_content._get("https://x.example/", "ua", deadline=103.0)
        self.assertEqual(seen["t"], 3.0)

    def test_spent_deadline_does_not_fetch(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("fetched")), \
             patch.object(fetch_content, "_now", lambda: 100.0):
            self.assertEqual(fetch_content._get("https://x.example/", "ua", deadline=99.0), (None, ""))

    def test_slow_trickle_is_cut_by_deadline(self):
        class Trickle:
            status = 200
            headers = {}

            def __init__(self, clock):
                self.clock = clock

            def read(self, n=-1):
                self.clock[0] += 5.0      # 1チャンクに5秒かかる相手
                return b"x"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        clock = [0.0]
        with patch("urllib.request.urlopen", lambda req, timeout=None: Trickle(clock)), \
             patch.object(fetch_content, "_now", lambda: clock[0]):
            self.assertEqual(fetch_content._get("https://x.example/", "ua", deadline=12.0), (None, ""))
        self.assertLess(clock[0], 20)
