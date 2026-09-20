# -*- coding: utf-8 -*-
"""Instagram Reel 動画取得の復活（2026-09-18 ユーザー裁定・中継 kkinstagram.com）の回帰テスト。

ダブルで差し込むのは中継のHTTP応答だけ。理由コードの分類(_video_reason_kind/_reason_kind)は
本物のledger.py関数に、mp4取得(_fetch_instagram_reel_video_url)は本物のfetch_content.py関数に
通す（導出結果を手書きしない）。ネットワークには一切出ない。
"""

import http.client
import io
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fetch_content  # noqa: E402
import ledger  # noqa: E402


def _http_error_with_location(location):
    hdrs = http.client.HTTPMessage()
    if location is not None:
        hdrs.add_header("Location", location)
    return urllib.error.HTTPError("https://www.kkinstagram.com/reel/ABC123/", 302, "Found", hdrs, None)


def _http_error_no_location(code=504):
    hdrs = http.client.HTTPMessage()  # Locationヘッダ無し
    return urllib.error.HTTPError("https://www.kkinstagram.com/reel/ABC123/", code, "Gateway Timeout", hdrs, None)


class FakeOpener:
    """urllib.request.build_opener() の戻り値の偽物。.open() を差し替える。"""
    def __init__(self, side_effects):
        self._side_effects = list(side_effects)
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


class ReelVideoUrlTest(unittest.TestCase):
    """_fetch_instagram_reel_video_url() 単体。中継のHTTP応答だけをダブルで差し込む。"""

    def test_relay_redirects_to_fbcdn_mp4(self):
        mp4 = "https://instagram.fisb6-2.fna.fbcdn.net/o1/v/t2/f2/m69/XYZ.mp4?sig=abc"
        opener = FakeOpener([_http_error_with_location(mp4)])
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content.time.sleep") as mock_sleep:
            url, reason = fetch_content._fetch_instagram_reel_video_url(
                "https://www.instagram.com/reel/ABC123/")
        self.assertEqual(url, mp4)
        self.assertEqual(reason, "")
        self.assertEqual(opener.calls, 1)
        mock_sleep.assert_not_called()

    def test_relay_sends_back_to_instagram_official(self):
        """中継は応答したが転送先が本家instagram.com（fbcdn系のmp4ではない）。"""
        official = "https://www.instagram.com/reel/ABC123/"
        opener = FakeOpener([_http_error_with_location(official)])
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content.time.sleep") as mock_sleep:
            url, reason = fetch_content._fetch_instagram_reel_video_url(
                "https://www.instagram.com/reel/ABC123/")
        self.assertIsNone(url)
        self.assertEqual(reason, "instagram_relay_not_video")
        # 転送先が判明した時点で確定するのでリトライはしない
        self.assertEqual(opener.calls, 1)
        mock_sleep.assert_not_called()

    def test_relay_504_no_location_retries_once_then_unavailable(self):
        opener = FakeOpener([_http_error_no_location(504), _http_error_no_location(504)])
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content.time.sleep") as mock_sleep:
            url, reason = fetch_content._fetch_instagram_reel_video_url(
                "https://www.instagram.com/reel/ABC123/")
        self.assertIsNone(url)
        self.assertEqual(reason, "instagram_relay_unavailable")
        # 合計2回の試行（初回+リトライ1回）
        self.assertEqual(opener.calls, 2)
        mock_sleep.assert_called_once_with(2)

    def test_relay_timeout_exception(self):
        opener = FakeOpener([TimeoutError("timed out"), TimeoutError("timed out")])
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content.time.sleep") as mock_sleep:
            url, reason = fetch_content._fetch_instagram_reel_video_url(
                "https://www.instagram.com/reel/ABC123/")
        self.assertIsNone(url)
        self.assertEqual(reason, "instagram_relay_unavailable")
        self.assertEqual(opener.calls, 2)
        mock_sleep.assert_called_once_with(2)

    def test_shortcode_unparsable(self):
        url, reason = fetch_content._fetch_instagram_reel_video_url(
            "https://www.instagram.com/p/not-a-reel/")
        self.assertIsNone(url)
        self.assertEqual(reason, "instagram_relay_not_video")

    # ---- 修正1: Location のスキーム検証（2026-09-20 Codex敵対的レビュー指摘・SSRF） ----
    # ホスト名だけで判定すると、平文HTTPや相対/プロトコル相対URL(ホストだけ持ちスキームを
    # 持たない形)がホスト照合を素通りしてしまう。https 以外は無条件で instagram_relay_not_video。

    def test_relay_returns_plain_http_location_is_rejected(self):
        """平文HTTPのLocationは経路上で差し替え可能なので拒否する。"""
        plain_http = "http://instagram.fisb6-2.fna.fbcdn.net/o1/v/t2/f2/m69/XYZ.mp4?sig=abc"
        opener = FakeOpener([_http_error_with_location(plain_http)])
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content.time.sleep") as mock_sleep:
            url, reason = fetch_content._fetch_instagram_reel_video_url(
                "https://www.instagram.com/reel/ABC123/")
        self.assertIsNone(url)
        self.assertEqual(reason, "instagram_relay_not_video")
        mock_sleep.assert_not_called()

    def test_relay_returns_relative_location_is_rejected(self):
        """相対URL("/foo.mp4")はスキームもホストも空。拒否する。"""
        opener = FakeOpener([_http_error_with_location("/foo.mp4")])
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content.time.sleep") as mock_sleep:
            url, reason = fetch_content._fetch_instagram_reel_video_url(
                "https://www.instagram.com/reel/ABC123/")
        self.assertIsNone(url)
        self.assertEqual(reason, "instagram_relay_not_video")
        mock_sleep.assert_not_called()

    def test_relay_returns_protocol_relative_location_is_rejected(self):
        """プロトコル相対URL("//host/path")はホストだけ持ちスキームを持たない。
        urllib.parse.urlsplit()はhostnameを正しく拾ってしまうため、ホスト照合だけでは
        fbcdn.netへの一致として素通りしてしまう(スキーム検証が無いと通ってしまう実例)。"""
        opener = FakeOpener([_http_error_with_location(
            "//instagram.fisb6-2.fna.fbcdn.net/a.mp4")])
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("fetch_content.time.sleep") as mock_sleep:
            url, reason = fetch_content._fetch_instagram_reel_video_url(
                "https://www.instagram.com/reel/ABC123/")
        self.assertIsNone(url)
        self.assertEqual(reason, "instagram_relay_not_video")
        mock_sleep.assert_not_called()


# ---------- 修正2: _NoFollowRedirectHandler の実urllib機構での検証 ----------
# 上のReelVideoUrlTestはurllib.request.build_opener自体をFakeOpenerへ丸ごと差し替えており、
# redirect_request()がNoneを返したときurllibが本当にHTTPErrorを上げるか・e.headersに
# Locationが残るかを一度も確かめていない。ここでは本物のurllib.request.build_opener() +
# 本物の_NoFollowRedirectHandlerを通し、ソケットレベルだけを差し替える
# (http.client.HTTPSConnection.connect()を偽のin-memoryソケットに差し替え、実際のHTTPレスポンス
# バイト列をhttp.client.HTTPResponseの本物のパーサに通す。ネットワークには一切出ない)。

class _FakeSocket:
    """http.client.HTTPConnectionが期待するソケットの最小限の偽物。
    makefile()が返すBytesIOに、あらかじめ用意したHTTPレスポンスの生バイト列を積んでおく。"""
    def __init__(self, response_bytes):
        self._buf = io.BytesIO(response_bytes)

    def makefile(self, mode, *a, **kw):
        return self._buf

    def sendall(self, data):
        pass

    def settimeout(self, t):
        pass

    def close(self):
        pass


def _canned_http_response(status, reason, header_lines, body=b""):
    """本物のhttp.client.HTTPResponseにパースさせるための生バイト列を組み立てる。"""
    lines = [("HTTP/1.1 %d %s" % (status, reason)).encode()]
    lines += [h.encode() for h in header_lines]
    lines.append(b"")
    lines.append(body)
    return b"\r\n".join(lines)


class RealUrllibRedirectHandlerTest(unittest.TestCase):
    def _open_with_canned_response(self, response_bytes, timeout=5):
        """本物のbuild_opener(_NoFollowRedirectHandler())を、ソケットだけ差し替えて実行する。"""
        def _fake_connect(conn_self):
            conn_self.sock = _FakeSocket(response_bytes)

        opener = urllib.request.build_opener(fetch_content._NoFollowRedirectHandler())
        req = urllib.request.Request(
            "https://www.kkinstagram.com/reel/ABC123/",
            headers={"User-Agent": fetch_content._REEL_RELAY_UA})
        with patch.object(http.client.HTTPSConnection, "connect", _fake_connect):
            return opener.open(req, timeout=timeout)

    def test_302_is_raised_as_http_error_with_location_header(self):
        """①302がHTTPErrorとして上がってくる ②e.headers.get('Location')に値が入っている。"""
        data = _canned_http_response(
            302, "Found",
            ["Location: https://instagram.fisb6-2.fna.fbcdn.net/a.mp4", "Content-Length: 0"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._open_with_canned_response(data)
        e = ctx.exception
        self.assertEqual(e.code, 302)
        self.assertEqual(e.headers.get("Location"),
                          "https://instagram.fisb6-2.fna.fbcdn.net/a.mp4")

    def test_200_does_not_raise_and_yields_no_location(self):
        """③200が返ったとき例外は出ず、呼び出し元(location抽出)はNoneのままになる。
        本文はダウンロードしない実装なので、ここでもボディは読まない。"""
        data = _canned_http_response(200, "OK", ["Content-Length: 5"], b"hello")
        resp = self._open_with_canned_response(data)
        try:
            self.assertEqual(resp.status, 200)
        finally:
            resp.close()

    def test_200_end_to_end_retries_then_unavailable(self):
        """③の続き: 実urllib機構を通したとき、200(Location無し)が続くと
        _fetch_instagram_reel_video_url()がリトライを経てinstagram_relay_unavailableになる。"""
        data = _canned_http_response(200, "OK", ["Content-Length: 0"])

        def _fake_connect(conn_self):
            conn_self.sock = _FakeSocket(data)

        with patch.object(http.client.HTTPSConnection, "connect", _fake_connect), \
             patch("fetch_content.time.sleep") as mock_sleep:
            url, reason = fetch_content._fetch_instagram_reel_video_url(
                "https://www.instagram.com/reel/ABC123/")
        self.assertIsNone(url)
        self.assertEqual(reason, "instagram_relay_unavailable")
        mock_sleep.assert_called_once_with(2)


class AllowlistRedirectHandlerSchemeTest(unittest.TestCase):
    """修正B(2026-09-20 3周目指摘・SSRF): _AllowlistRedirectHandler がスキームを見ていなかった。
    入口(中継のLocation)はhttps限定済みだが、_gemini_video_understanding()がmp4本体を取りに行く
    際の**後続リダイレクト**は、許可済みホスト自身がhttp://へ3xxを返すと素通りしていた。"""

    def _handler(self):
        return fetch_content._AllowlistRedirectHandler(("fbcdn.net",))

    def test_http_redirect_to_allowed_host_is_rejected(self):
        handler = self._handler()
        req = urllib.request.Request("https://instagram.fisb6-2.fna.fbcdn.net/a.mp4")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(
                req, None, 302, "Found", {},
                "http://instagram.fisb6-2.fna.fbcdn.net/a.mp4")

    def test_https_redirect_to_allowed_host_is_accepted(self):
        handler = self._handler()
        req = urllib.request.Request("https://instagram.fisb6-2.fna.fbcdn.net/a.mp4")
        new_req = handler.redirect_request(
            req, None, 302, "Found", {},
            "https://instagram.other-edge.fna.fbcdn.net/a.mp4")
        self.assertEqual(new_req.full_url, "https://instagram.other-edge.fna.fbcdn.net/a.mp4")


class GeminiVideoUnderstandingSchemeTest(unittest.TestCase):
    """修正B: _gemini_video_understanding() 開始URLのスキーム検証(allowed_hosts指定時のみ)。
    新しい理由コードは増やさず、既存の host_not_allowed を流用する(CEO指示どおり)。"""

    def test_start_url_http_on_allowed_host_is_rejected(self):
        with patch.object(fetch_content, "_GEMINI_AVAILABLE", True), \
             patch.object(fetch_content, "_GEMINI_API_KEY", "test-key"):
            text, reason = fetch_content._gemini_video_understanding(
                "http://instagram.fisb6-2.fna.fbcdn.net/a.mp4",
                allowed_hosts=fetch_content._INSTAGRAM_VIDEO_ALLOWED_HOSTS)
        self.assertEqual(text, "")
        self.assertEqual(reason, "host_not_allowed")

    def test_start_url_https_on_allowed_host_passes_scheme_check(self):
        """https:// なら弾かれずホスト照合を通過し、その先(ダウンロード)まで進むことを、
        ネットワークに出さずopener.open()への到達で確認する。"""
        opener = FakeOpener([RuntimeError("reached download layer")])
        with patch.object(fetch_content, "_GEMINI_AVAILABLE", True), \
             patch.object(fetch_content, "_GEMINI_API_KEY", "test-key"), \
             patch("urllib.request.build_opener", return_value=opener):
            text, reason = fetch_content._gemini_video_understanding(
                "https://instagram.fisb6-2.fna.fbcdn.net/a.mp4",
                allowed_hosts=fetch_content._INSTAGRAM_VIDEO_ALLOWED_HOSTS)
        self.assertEqual(text, "")
        self.assertEqual(reason, "exception:RuntimeError")
        self.assertEqual(opener.calls, 1)


class GeminiVideoUnderstandingXPathUnaffectedTest(unittest.TestCase):
    """修正B: allowed_hosts が None(=X動画経路。fetch_content.py:442 は allowed_hosts を渡さず
    呼んでいる)のとき、新設のスキーム検証の対象外であることを確認する
    (今回の変更が実質Instagram専用であることの回帰テスト)。"""

    def test_allowed_hosts_none_bypasses_scheme_check(self):
        with patch.object(fetch_content, "_GEMINI_AVAILABLE", True), \
             patch.object(fetch_content, "_GEMINI_API_KEY", "test-key"), \
             patch("urllib.request.urlopen", side_effect=RuntimeError("reached network layer")):
            text, reason = fetch_content._gemini_video_understanding(
                "http://video.twimg.com/a.mp4")  # allowed_hosts省略=None。scheme=httpでも弾かれない
        self.assertEqual(text, "")
        self.assertEqual(reason, "exception:RuntimeError")


class HandlerRegistrationTest(unittest.TestCase):
    """修正C(2026-09-20 3周目指摘): 本番関数が_NoFollowRedirectHandlerを実際に渡していることを
    検証する。既存の18件は urllib.request.build_opener を丸ごとFakeOpenerへ差し替えるか、
    テスト側で自前にopenerを組んでおり、本番関数が「何のハンドラを渡したか」を一度も見ていなかった
    ため、fetch_content.py側でハンドラ登録を外しても全部緑のままになっていた。"""

    def test_build_opener_is_called_with_nofollow_handler_instance(self):
        opener = FakeOpener([_http_error_with_location(
            "https://instagram.fisb6-2.fna.fbcdn.net/a.mp4")])
        with patch("urllib.request.build_opener", return_value=opener) as mock_build, \
             patch("fetch_content.time.sleep"):
            fetch_content._fetch_instagram_reel_video_url(
                "https://www.instagram.com/reel/ABC123/")
        mock_build.assert_called_once()
        args, _kwargs = mock_build.call_args
        self.assertTrue(
            any(isinstance(a, fetch_content._NoFollowRedirectHandler) for a in args),
            "build_opener() に _NoFollowRedirectHandler のインスタンスが渡されていない")


class ProductionFunctionRealUrllibTest(unittest.TestCase):
    """修正C: _fetch_instagram_reel_video_url() 自体（本番関数そのもの）を実urllib機構
    (ソケット差し替え)に通し、302→Location取得→許可ホスト照合までを一気通貫で確認する。
    上の RealUrllibRedirectHandlerTest はテスト側で自前に opener を組んでおり本番の
    build_opener() 呼び出しを経由していなかったため、これとは別に本番関数を直接呼ぶ。"""

    def test_production_function_end_to_end_via_real_socket(self):
        """302がハンドラで即座にHTTPErrorとして返り、1回のHTTP試行だけで完結することまで見る。
        （ハンドラ登録が外れて追従されてしまうと、2本目の接続で"最終応答"を受け取り、
        例外が上がらず location=None のままリトライ→instagram_relay_unavailable になる。
        この崩れ方を検出できるよう、単に url/reason だけでなく接続回数も確認する。
        実際にハンドラ登録を外すmutationでこのテストが赤くなることを確認済み——報告参照）"""
        redirect_data = _canned_http_response(
            302, "Found",
            ["Location: https://instagram.fisb6-2.fna.fbcdn.net/a.mp4", "Content-Length: 0"])
        # 追従されてしまった場合にだけ使われる「最終応答」。ハンドラが正しく効いていれば
        # 1回目の302が即座にHTTPErrorとして返るため、これは一度も使われないはず。
        followed_final = _canned_http_response(200, "OK", ["Content-Length: 0"])
        seq = [redirect_data, followed_final, followed_final, followed_final]
        connect_calls = []

        def _fake_connect(conn_self):
            idx = len(connect_calls)
            connect_calls.append(1)
            data = seq[idx] if idx < len(seq) else followed_final
            conn_self.sock = _FakeSocket(data)

        with patch.object(http.client.HTTPSConnection, "connect", _fake_connect), \
             patch("fetch_content.time.sleep"):
            url, reason = fetch_content._fetch_instagram_reel_video_url(
                "https://www.instagram.com/reel/ABC123/")
        self.assertEqual(url, "https://instagram.fisb6-2.fna.fbcdn.net/a.mp4")
        self.assertEqual(reason, "")
        self.assertEqual(len(connect_calls), 1,
                          "ハンドラが効いていれば1回のHTTP試行(=1接続)で完結するはず"
                          "(追従されると2接続目以降が発生する)")


class ReasonKindClassificationTest(unittest.TestCase):
    """新規コードは transient、歴史的コード/理由コード無しのレガシーは permanent のまま
    （どちらも本物のledger._reason_kind()に通す。導出結果を手書きしない）。"""

    def _reel_rec(self, video_reason=None, ok=True):
        rec = {
            "ok": ok,
            "route": "instagram",
            "url": "https://www.instagram.com/reel/ABC123/",
            "missing": ["video_content"],
            "depth": "partial",
        }
        if video_reason is not None:
            rec["video_reason"] = video_reason
        return rec

    def test_instagram_relay_unavailable_is_transient(self):
        rec = self._reel_rec(video_reason="instagram_relay_unavailable")
        self.assertEqual(ledger._reason_kind(rec), "transient")

    def test_instagram_relay_not_video_is_transient(self):
        rec = self._reel_rec(video_reason="instagram_relay_not_video")
        self.assertEqual(ledger._reason_kind(rec), "transient")

    def test_legacy_abandoned_code_stays_permanent(self):
        """既存レコードの据え置き: 歴史的コードを持つレコードは permanent のまま。"""
        rec = self._reel_rec(video_reason="instagram_reel_abandoned")
        self.assertEqual(ledger._reason_kind(rec), "permanent")

    def test_legacy_record_without_video_reason_stays_permanent(self):
        """video_reasonを一切持たない2026-09-02以前の旧/reel/レコードも permanent のまま
        （_video_reason_kindがNoneを返し、URL形からの分岐に落ちる＝レガシー専用経路）。"""
        rec = self._reel_rec(video_reason=None)
        self.assertNotIn("video_reason", rec)
        self.assertEqual(ledger._reason_kind(rec), "permanent")


class FetchInstagramCaptionFallbackTest(unittest.TestCase):
    """動画理解に失敗しても fetch_instagram() は ok:True でキャプションを返す
    （caption-only フォールバック）。中継のダブルは _fetch_instagram_reel_video_url() に閉じ、
    fetch_instagram() 自体は本物の関数を通す。"""

    _OG_HTML = (
        '<html><head>'
        '<meta property="og:title" content="Kalypso on Instagram: &quot;Sites for designers&quot;">'
        '<meta property="og:description" content="1,745 likes, 6 comments - kalypsodesigns on '
        'June 14, 2026: &quot;Sites for designers&quot;. ">'
        '<meta property="og:image" content="https://scontent.cdninstagram.com/cover.jpg">'
        '</head></html>'
    )

    def test_relay_unavailable_falls_back_to_caption_only(self):
        with patch("fetch_content._get", return_value=(200, self._OG_HTML)), \
             patch("fetch_content._fetch_instagram_reel_video_url",
                   return_value=(None, "instagram_relay_unavailable")):
            result = fetch_content.fetch_instagram("https://www.instagram.com/reel/ABC123/")
        self.assertTrue(result["ok"])
        self.assertEqual(result["video_reason"], "instagram_relay_unavailable")
        self.assertFalse(result["video_understood"])
        self.assertTrue(result["has_video"])
        self.assertEqual(result["depth"], "partial")
        self.assertEqual(result["missing"], ["video_content"])
        self.assertNotIn("動画の内容:", result["text"])
        self.assertIn("Sites for designers", result["text"])

    def test_relay_success_with_gemini_understanding_yields_full_depth(self):
        mp4 = "https://instagram.fisb6-2.fna.fbcdn.net/o1/v/t2/f2/m69/XYZ.mp4?sig=abc"
        with patch("fetch_content._get", return_value=(200, self._OG_HTML)), \
             patch("fetch_content._fetch_instagram_reel_video_url",
                   return_value=(mp4, "")), \
             patch("fetch_content._gemini_video_understanding",
                   return_value=("誰かが机の上でノートPCを操作している。", "")) as mock_gvu:
            result = fetch_content.fetch_instagram("https://www.instagram.com/reel/ABC123/")
        self.assertTrue(result["ok"])
        self.assertEqual(result["video_reason"], "")
        self.assertTrue(result["video_understood"])
        self.assertEqual(result["depth"], "full")
        self.assertEqual(result["missing"], [])
        self.assertIn("動画の内容: 誰かが机の上でノートPCを操作している。", result["text"])
        # 修正G(2026-09-20 4周目指摘): fetch_instagram()の呼び出し地点
        # (fetch_content.py:679付近)が_gemini_video_understanding()へ実際に
        # allowed_hosts=_INSTAGRAM_VIDEO_ALLOWED_HOSTSを渡していることを検証する。
        # ここを渡さなくなると多段で塞いだallowlist/HTTPS強制がまるごと無効化されるが、
        # _gemini_video_understanding自体をモックで潰す他のテストは渡された引数を
        # 見ていないため、その回帰を検出できていなかった。
        mock_gvu.assert_called_once_with(
            mp4, allowed_hosts=fetch_content._INSTAGRAM_VIDEO_ALLOWED_HOSTS)

    def test_video_reason_never_empty_when_missing_video_content(self):
        """video_reasonが空文字のままmissing:["video_content"]になる経路が無いことの確認
        （_gemini_video_understandingが空文字理由を返す異常系を想定してもガードが効く）。"""
        mp4 = "https://instagram.fisb6-2.fna.fbcdn.net/o1/v/t2/f2/m69/XYZ.mp4?sig=abc"
        with patch("fetch_content._get", return_value=(200, self._OG_HTML)), \
             patch("fetch_content._fetch_instagram_reel_video_url",
                   return_value=(mp4, "")), \
             patch("fetch_content._gemini_video_understanding",
                   return_value=("", "")):
            result = fetch_content.fetch_instagram("https://www.instagram.com/reel/ABC123/")
        self.assertEqual(result["missing"], ["video_content"])
        self.assertNotEqual(result["video_reason"], "")
        self.assertEqual(result["video_reason"], "instagram_relay_unavailable")


if __name__ == "__main__":
    unittest.main()
