"""切り替えの検査。偽にするのは「外の世界の応答」（http_get）だけ。
本文の取り出し・失敗判定・並べ替え・記録は本物のコードを通す（fetch-test-double-bypass-trap の教訓）。"""
import json, os, pathlib, shutil, socket, tempfile, unittest
import fetchchain as fc

# 名前解決も外へ出さない。内部アドレスを表す名前だけ内部の IP、ほかは公開の IP（文書用の 192.0.2.x は is_global でないので使わない）
_INTERNAL = {"localhost": "127.0.0.1"}


def _fake_getaddrinfo(host, *a, **k):
    try:
        ip = str(__import__("ipaddress").ip_address(host))
    except ValueError:
        ip = _INTERNAL.get(host, "8.8.8.8")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


fc.socket.getaddrinfo = _fake_getaddrinfo
_TMPS = []
_mkdtemp = tempfile.mkdtemp


def _tracked_mkdtemp(*a, **k):
    d = _mkdtemp(*a, **k)
    _TMPS.append(d)
    return d


tempfile.mkdtemp = _tracked_mkdtemp


def tearDownModule():
    for d in _TMPS:
        shutil.rmtree(d, ignore_errors=True)

os.environ["FETCHCHAIN_ENV"] = "pc"   # 実行場所の自動判定に左右されない
fc._ytdlp = lambda: None              # 本物の yt-dlp を呼ばない（外の世界に出ない）

TWEET = {"tweet": {"text": "hello from fxtwitter", "author": {"screen_name": "u"}, "created_at": "d"}}
SYND = {"text": "hello from syndication", "user": {"screen_name": "u"}, "created_at": "d"}
URL = "https://x.com/u/status/123456789"


def fake_http(table):
    """URL の部分文字列 → (status, body)。当たらなければ通信失敗 (None, "")。"""
    def _get(url, timeout=15, max_bytes=2_000_000, ua=None):
        for key, resp in table.items():
            if key in url:
                return resp
        return None, ""
    return _get


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._orig = (fc.HEALTH, fc.STATUS, fc.http_get)
        fc.HEALTH, fc.STATUS = self.tmp / "health.jsonl", self.tmp / "status.json"

    def tearDown(self):
        fc.HEALTH, fc.STATUS, fc.http_get = self._orig

    def test_primary_ok(self):
        fc.http_get = fake_http({"api.fxtwitter.com": (200, json.dumps(TWEET))})
        out = fc.fetch(URL)
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "x_fxtwitter")
        self.assertEqual(out["data"]["text"], "hello from fxtwitter")

    def test_failover_to_next_method(self):
        fc.http_get = fake_http({"api.fxtwitter.com": (503, ""),
                                 "syndication.twimg.com": (200, json.dumps(SYND))})
        out = fc.fetch(URL)
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "x_syndication")
        self.assertEqual([t["method"] for t in out["tried"]], ["x_fxtwitter", "x_syndication"])
        self.assertFalse(out["tried"][0]["ok"])

    def test_empty_body_counts_as_failure(self):
        # 200 でも本文が空なら「取れた」と数えない
        empty = {"tweet": {"text": "", "author": {}, "created_at": ""}}
        fc.http_get = fake_http({"api.fxtwitter.com": (200, json.dumps(empty)),
                                 "syndication.twimg.com": (200, json.dumps(SYND))})
        self.assertEqual(fc.fetch(URL)["method"], "x_syndication")

    def test_all_fail_reports_every_attempt(self):
        fc.http_get = fake_http({})
        out = fc.fetch(URL)
        self.assertFalse(out["ok"])
        self.assertEqual(len(out["tried"]), 3)

    def test_demote_after_three_failures_then_recover(self):
        cfg = fc.load_registry()["x_post"]
        for _ in range(3):
            fc.log_health("fetch", "x_post", "x_fxtwitter", False, 1, "x")
        self.assertEqual(fc.ordered_methods(cfg)[-1], "x_fxtwitter")  # 最後尾へ
        fc.log_health("canary", "x_post", "x_fxtwitter", True, 1)     # 復活を実測で拾えば戻る
        self.assertEqual(fc.ordered_methods(cfg)[0], "x_fxtwitter")

    def test_two_failures_do_not_demote(self):
        cfg = fc.load_registry()["x_post"]
        for _ in range(2):
            fc.log_health("fetch", "x_post", "x_fxtwitter", False, 1, "x")
        self.assertEqual(fc.ordered_methods(cfg)[0], "x_fxtwitter")

    def test_cloud_skips_methods_marked_pc_only(self):
        # PC でしか効かない手段は、クラウドでは使わず・壊れたとも数えない
        reg = json.loads(fc.REGISTRY.read_text(encoding="utf-8"))
        reg["channels"]["x_post"]["where"] = {"x_fxtwitter": ["pc"]}
        reg_path = self.tmp / "sources.json"
        reg_path.write_text(json.dumps(reg), encoding="utf-8")
        orig_reg = fc.REGISTRY
        fc.REGISTRY = reg_path
        os.environ["FETCHCHAIN_ENV"] = "gha"
        try:
            fc.http_get = fake_http({"api.fxtwitter.com": (503, ""),
                                     "syndication.twimg.com": (200, json.dumps({"text": "just setting up my twttr"})),
                                     "api.vxtwitter.com": (200, json.dumps({"text": "just setting up my twttr"}))})
            out = fc.fetch(URL)
            self.assertEqual(out["method"], "x_syndication")
            self.assertNotIn("x_fxtwitter", [t["method"] for t in out["tried"]])
            rep = fc.canary()
            x = rep["channels"]["x_post"]
            self.assertEqual(x["level"], "ok")  # fxtwitter は 503 だが、ここでは測らない
            self.assertIsNone({m["method"]: m for m in x["methods"]}["x_fxtwitter"]["ok"])
            self.assertEqual(rep["env"], "gha")
        finally:
            fc.REGISTRY = orig_reg
            os.environ["FETCHCHAIN_ENV"] = "pc"

    def test_demotion_counts_only_this_place(self):
        # クラウドで3回落ちても、PC の並び順は変わらない（場所ごとに数える）
        cfg = fc.load_registry()["x_post"]
        os.environ["FETCHCHAIN_ENV"] = "gha"
        try:
            for _ in range(3):
                fc.log_health("canary", "x_post", "x_fxtwitter", False, 1, "x")
            self.assertEqual(fc.ordered_methods(cfg)[-1], "x_fxtwitter")
        finally:
            os.environ["FETCHCHAIN_ENV"] = "pc"
        self.assertEqual(fc.ordered_methods(cfg)[0], "x_fxtwitter")
        rows = [json.loads(l) for l in fc.HEALTH.read_text(encoding="utf-8").splitlines()]
        self.assertEqual({r["env"] for r in rows}, {"gha"})
        self.assertNotIn("url", rows[0])  # 何を読んだかは残さない

    def test_old_rows_without_env_count_as_pc(self):
        cfg = fc.load_registry()["x_post"]
        fc.HEALTH.write_text("".join(json.dumps({"method": "x_fxtwitter", "ok": False}) + "\n" for _ in range(3))
                             + "{broken\n", encoding="utf-8")
        self.assertEqual(fc.ordered_methods(cfg)[-1], "x_fxtwitter")

    def test_unavailable_tool_is_not_counted_as_broken(self):
        fc.http_get = fake_http({"youtube.com/oembed": (200, json.dumps({"title": "Never Gonna Give You Up"}))})
        out = fc.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(out["method"], "yt_oembed")
        self.assertTrue(out["tried"][0]["unavailable"])
        rep = fc.canary()
        self.assertEqual(rep["channels"]["youtube"]["level"], "ok")
        self.assertFalse(fc.HEALTH.exists() and "yt_dlp_subs" in fc.HEALTH.read_text(encoding="utf-8"))

    def test_fxtwitter_article_body_and_raw(self):
        art = {"tweet": {"text": "告知", "author": {"screen_name": "u"}, "created_at": "d",
                         "article": {"content": {"blocks": [{"text": "見出し"}, {"text": "本文の段落"}]}}}}
        fc.http_get = fake_http({"api.fxtwitter.com": (200, json.dumps(art))})
        data = fc.fetch(URL)["data"]
        self.assertIn("本文の段落", data["text"])
        self.assertEqual(data["raw"]["tweet"]["author"]["screen_name"], "u")  # 生データをそのまま返す

    def test_instagram_oembed_caption_and_date(self):
        ig = {"title": "005 thinking about what a gallery could become", "author_name": "larisabulibasa",
              "media_id": "3962865267889923596_4845036"}
        fc.http_get = fake_http({"instagram.com/api/v1/oembed": (200, json.dumps(ig))})
        out = fc.fetch("https://www.instagram.com/p/DX4dpjOiEsz/")
        self.assertEqual((out["channel"], out["method"]), ("instagram", "ig_oembed"))
        self.assertEqual(out["data"]["author"], "larisabulibasa")
        self.assertEqual(out["data"]["date"], "2026-08-13T14:12:54+00:00")  # Vault の実例と同じ値

    def test_instagram_falls_back_to_og_meta(self):
        page = '<meta property="og:description" content="u on May 3: &quot;caption&quot;" />'
        fc.http_get = fake_http({"instagram.com/api/v1/oembed": (429, ""), "instagram.com/p/": (200, page)})
        out = fc.fetch("https://www.instagram.com/p/DX4dpjOiEsz/")
        self.assertEqual(out["method"], "og_meta")
        self.assertEqual(out["data"]["text"], 'u on May 3: "caption"')

    def test_canary_flags_wrong_content(self):
        # 取れたが期待する文字列が無い（ページが別物に変わった等）→ 失敗扱い
        fc.http_get = fake_http({"api.fxtwitter.com": (200, json.dumps(TWEET)),
                                 "syndication.twimg.com": (200, json.dumps(SYND)),
                                 "api.vxtwitter.com": (200, json.dumps({"text": "other"})),
                                 "youtube.com/oembed": (200, json.dumps({"title": "Never Gonna Give You Up"})),
                                 "en.wikipedia.org": (200, "<title>x</title>" + "web scraping " * 40),
                                 "r.jina.ai": (200, "Title: t\n" + "web scraping " * 40)})
        rep = fc.canary()
        self.assertEqual(rep["channels"]["x_post"]["level"], "down")  # 期待文字列が3つとも無い
        self.assertEqual(rep["channels"]["youtube"]["level"], "ok")
        self.assertEqual(rep["channels"]["web"]["level"], "ok")


PLUGIN = {"name": "x_example", "channel": "x_post", "kind": "http", "format": "json", "status": "trial",
          "url": "https://api.example.test/{user}/status/{id}",
          "vars_from_url": {"user": r"x\.com/([^/]+)/status", "id": r"/status/(\d+)"},
          "map": {"text": "post.body", "author": "post.who"}}


class PluginTest(unittest.TestCase):
    """自動で足される手段は「宣言だけ」。他人のコードは動かず、標準の手段を上書きできず、壊れた仕様は黙って無視しない。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        (self.tmp / "plugins").mkdir()
        self._orig = (fc.HEALTH, fc.STATUS, fc.PLUGINS, fc.http_get)
        fc.HEALTH, fc.STATUS, fc.PLUGINS = self.tmp / "health.jsonl", self.tmp / "status.json", self.tmp / "plugins"

    def tearDown(self):
        fc.HEALTH, fc.STATUS, fc.PLUGINS, fc.http_get = self._orig
        fc.load_registry()  # 次のテストに別の手段が残らないよう元に戻す

    @staticmethod
    def fresh_skipped():
        """sources.json を読み直し、プラグインを入れ直して、読み飛ばされたものを返す。"""
        channels = json.loads(fc.REGISTRY.read_text(encoding="utf-8"))["channels"]
        return fc.load_plugins(channels)

    def put(self, spec, name=None):
        (fc.PLUGINS / ((name or spec.get("name", "bad")) + ".json")).write_text(json.dumps(spec), encoding="utf-8")

    def test_trial_plugin_is_last_resort_and_works(self):
        self.put(PLUGIN)
        fc.http_get = fake_http({"api.example.test/u/status/123456789": (200, json.dumps({"post": {"body": "from plugin", "who": "u"}}))})
        out = fc.fetch(URL)  # 標準の3手段は全部失敗する状況
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "x_example")
        self.assertEqual([t["method"] for t in out["tried"]][-1], "x_example")
        self.assertEqual(out["data"]["text"], "from plugin")
        self.assertEqual(out["data"]["author"], "u")
        self.assertEqual(out["data"]["title"], "from plugin")

    def test_core_method_still_preferred_over_trial(self):
        self.put(PLUGIN)
        fc.http_get = fake_http({"api.fxtwitter.com": (200, json.dumps(TWEET)),
                                 "api.example.test": (200, json.dumps({"post": {"body": "plugin"}}))})
        self.assertEqual(fc.fetch(URL)["method"], "x_fxtwitter")

    def test_cannot_override_core_method(self):
        bad = dict(PLUGIN, name="x_fxtwitter")
        self.put(bad)
        fc.http_get = fake_http({"api.fxtwitter.com": (200, json.dumps(TWEET))})
        out = fc.fetch(URL)
        self.assertEqual(out["data"]["text"], "hello from fxtwitter")  # 標準の関数のまま
        self.assertIn("標準の手段と同じ名前", " ".join(" ".join(e) for _, e in self.fresh_skipped()))

    def test_invalid_specs_are_rejected(self):
        ch = fc.load_registry()
        cases = {
            "http でない": dict(PLUGIN, url="http://api.example.test/{id}"),
            "ホスト名に置換": dict(PLUGIN, url="https://{user}.example.test/{id}"),
            "書式式の悪用": dict(PLUGIN, url="https://api.example.test/{id.__class__}"),
            "取り出し方が無い置換": dict(PLUGIN, url="https://api.example.test/{nope}"),
            "未知のチャンネル": dict(PLUGIN, channel="nowhere"),
            "kind が違う": dict(PLUGIN, kind="python"),
            "text が無い map": dict(PLUGIN, map={"author": "a"}),
            "不正な取り出しパス": dict(PLUGIN, map={"text": "a/b"}),
            "不正な名前": dict(PLUGIN, name="X-Bad"),
            "不正な正規表現": dict(PLUGIN, vars_from_url=dict(PLUGIN["vars_from_url"], id="(")),
            "グループが2つ": dict(PLUGIN, vars_from_url=dict(PLUGIN["vars_from_url"], id=r"(\d)(\d)")),
            "status が違う": dict(PLUGIN, status="admin"),
            "ua に制御文字": dict(PLUGIN, ua="a\r\nX: 1"),
            "名乗りの偽装": dict(PLUGIN, ua="Mozilla/5.0"),
        }
        for label, spec in cases.items():
            self.assertTrue(fc.validate_spec(spec, ch), label)
        self.assertEqual(fc.validate_spec(PLUGIN, ch), [])

    def test_broken_plugin_is_reported_and_does_not_break_core(self):
        (fc.PLUGINS / "garbage.json").write_text("{not json", encoding="utf-8")
        self.put(dict(PLUGIN, url="http://insecure.test/{id}"), name="insecure")
        self.put(PLUGIN)
        skipped = self.fresh_skipped()
        self.assertEqual({n for n, _ in skipped}, {"garbage.json", "insecure.json"})
        fc.http_get = fake_http({"api.fxtwitter.com": (200, json.dumps(TWEET))})
        self.assertTrue(fc.fetch(URL)["ok"])
        self.assertIn("x_example", fc.METHODS)  # 有効なものは生きている

    def test_removed_plugin_disappears_on_reload(self):
        self.put(PLUGIN)
        fc.load_registry()
        self.assertIn("x_example", fc.METHODS)
        (fc.PLUGINS / "x_example.json").unlink()
        fc.load_registry()
        self.assertNotIn("x_example", fc.METHODS)
        self.assertNotIn("x_example", fc.PLUGIN_STATUS)

    def test_plugin_to_private_host_is_refused_without_connecting(self):
        # 仕様の接続先が内部アドレスでも、実際の通信の手前で止まる（偽装なしの本物の http_get）
        self.put(dict(PLUGIN, url="https://127.0.0.1/{id}"))
        real = self._orig[3]
        # 内部アドレス宛だけ本物の http_get（検査で止まる）、標準の手段は外へ出さず失敗させる
        fc.http_get = lambda url, **kw: real(url, **kw) if "127.0.0.1" in url else (None, "")
        out = fc.fetch(URL)
        self.assertFalse(out["ok"])
        self.assertIn("公開ホストでない", [t for t in out["tried"] if t["method"] == "x_example"][0]["error"])

    def test_substitution_never_uses_format_expressions(self):
        # 検証をすり抜けた仕様が来ても、{id.__class__} のような書式式は展開されない（二重の備え）
        seen = []

        def spy(url, timeout=15, max_bytes=2_000_000, ua=None):
            seen.append(url)
            return 200, json.dumps({"post": {"body": "ok"}})
        fc.http_get = spy
        spec = dict(PLUGIN, url="https://api.example.test/{id.__class__}/{id}")
        fc.make_http_method(spec)(URL)
        self.assertIn("{id.__class__}", seen[0])
        self.assertNotIn("class", seen[0].replace("{id.__class__}", ""))

    def test_values_put_into_url_are_encoded(self):
        seen = []

        def spy(url, timeout=15, max_bytes=2_000_000, ua=None):
            seen.append(url)
            return 200, json.dumps({"post": {"body": "ok"}})
        fc.http_get = spy
        spec = dict(PLUGIN, url="https://api.example.test/q?v={q}", vars_from_url={"q": r"\?q=(.+)$"})
        fc.make_http_method(spec)("https://x.com/u/status/1?q=a b&c=1#d")
        self.assertEqual(seen[0], "https://api.example.test/q?v=a%20b%26c%3D1%23d")

    def test_min_chars_rejects_too_short_body(self):
        self.put(dict(PLUGIN, min_chars=50))
        fc.http_get = fake_http({"api.example.test": (200, json.dumps({"post": {"body": "short"}}))})
        out = fc.fetch(URL)
        self.assertFalse(out["ok"])
        self.assertIn("短すぎる", [t for t in out["tried"] if t["method"] == "x_example"][0]["error"])

    def test_canary_covers_plugins_and_marks_trial(self):
        self.put(dict(PLUGIN, map={"text": "post.body"}))
        fc.http_get = fake_http({"api.fxtwitter.com": (200, json.dumps({"tweet": {"text": "just setting up my twttr"}})),
                                 "api.example.test": (200, json.dumps({"post": {"body": "nothing here"}}))})
        rep = fc.canary()
        names = {m["method"]: m for m in rep["channels"]["x_post"]["methods"]}
        self.assertIn("x_example", names)
        self.assertFalse(names["x_example"]["ok"])  # 期待文字列が無い → 試用手段も同じ基準で落ちる
        self.assertIn("試用中", fc.render(rep))


class YtDlpTest(unittest.TestCase):
    def setUp(self):
        self._orig = (fc._ytdlp, fc.subprocess.run)
        fc._ytdlp = lambda: "yt-dlp"

    def tearDown(self):
        fc._ytdlp, fc.subprocess.run = self._orig

    def fake_run(self, files, rc=0, stdout="Title\tUploader\t20091025\n"):
        def run(cmd, **kw):
            self.cmd = cmd
            out = pathlib.Path(cmd[cmd.index("-o") + 1]).parent
            for name, segs in files.items():
                (out / name).write_text(json.dumps({"events": [{"segs": [{"utf8": t} for t in segs]}]}), encoding="utf-8")
            return type("P", (), {"returncode": rc, "stdout": stdout, "stderr": "ERROR: Sign in to confirm"})()
        return run

    def test_prefers_original_language_subtitles(self):
        fc.subprocess.run = self.fake_run({"s.en.json3": ["english"], "s.ja-orig.json3": ["にほん", "ご"]})
        d = fc.yt_dlp_subs("https://www.youtube.com/watch?v=x")
        self.assertEqual((d["text"], d["title"], d["author"], d["date"]), ("にほんご", "Title", "Uploader", "20091025"))

    def test_japanese_before_english_and_honest_user_agent(self):
        fc.subprocess.run = self.fake_run({"s.en.json3": ["english"], "s.ja.json3": ["日本語"]})
        self.assertEqual(fc.yt_dlp_subs("https://www.youtube.com/watch?v=x")["text"], "日本語")
        self.assertEqual(self.cmd[self.cmd.index("--user-agent") + 1], fc.UA)  # yt-dlp にも名乗りを偽らせない

    def test_original_language_beats_translated_japanese(self):
        # 英語の動画の「機械翻訳の日本語字幕」より、元の英語の字幕を選ぶ
        fc.subprocess.run = self.fake_run({"s.ja.json3": ["翻訳"], "s.en-orig.json3": ["original"]})
        self.assertEqual(fc.yt_dlp_subs("https://www.youtube.com/watch?v=x")["text"], "original")

    def test_failure_and_no_subtitles_are_errors(self):
        fc.subprocess.run = self.fake_run({}, rc=1)
        with self.assertRaisesRegex(fc.FetchError, "Sign in"):
            fc.yt_dlp_subs("https://www.youtube.com/watch?v=x")
        fc.subprocess.run = self.fake_run({})
        with self.assertRaisesRegex(fc.FetchError, "字幕が無い"):
            fc.yt_dlp_subs("https://www.youtube.com/watch?v=x")


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._orig = (fc.HEALTH, fc.STATUS, fc.http_get)
        fc.HEALTH, fc.STATUS = self.tmp / "health.jsonl", self.tmp / "status.json"

    def tearDown(self):
        fc.HEALTH, fc.STATUS, fc.http_get = self._orig

    def test_internal_url_never_reaches_a_relay(self):
        # 内部アドレスの URL は、Jina などの中継にもプラグインにも渡らない（どの手段も呼ばれない）
        seen = []
        fc.http_get = lambda url, **kw: (seen.append(url), (200, "x" * 500))[1]
        out = fc.fetch("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(out["ok"])
        self.assertEqual(seen, [])
        with self.assertRaises(fc.FetchError):
            fc.web_jina("http://127.0.0.1/admin")

    def test_status_file_never_contains_urls(self):
        reg = {"channels": {"web": {"match": ".*", "methods": ["web_direct"],
                                    "canary": {"url": "http://localhost/secret-path", "expect": "x"}}}}
        reg_path = self.tmp / "sources.json"
        reg_path.write_text(json.dumps(reg), encoding="utf-8")
        orig = fc.REGISTRY
        fc.REGISTRY = reg_path
        try:
            fc.canary()
        finally:
            fc.REGISTRY = orig
        body = fc.STATUS.read_text(encoding="utf-8")
        self.assertIn("公開ホストでない", body)
        self.assertNotIn("secret-path", body)
        self.assertNotIn("localhost", body)

    def test_canary_with_internal_url_reaches_no_method(self):
        # 健康診断の見本が内部アドレスでも、どの手段（oEmbed・yt-dlp・プラグイン）にも渡らない
        reg = {"channels": {"youtube": {"match": ".*", "methods": ["yt_oembed", "yt_dlp_subs"],
                                        "canary": {"url": "http://127.0.0.1/admin", "expect": "x"}}}}
        reg_path = self.tmp / "sources.json"
        reg_path.write_text(json.dumps(reg), encoding="utf-8")
        seen = []
        orig = (fc.REGISTRY, fc._ytdlp)
        fc.REGISTRY, fc._ytdlp = reg_path, (lambda: seen.append("yt-dlp") or None)
        fc.http_get = lambda url, **kw: (seen.append(url), (200, "{}"))[1]
        try:
            rep = fc.canary()
        finally:
            fc.REGISTRY, fc._ytdlp = orig
        self.assertEqual(seen, [])
        self.assertEqual(rep["channels"]["youtube"]["level"], "down")

    def test_health_record_never_contains_urls(self):
        fc.log_health("fetch", "web", "web_direct", False, 1, "http/https 以外は取らない: file:///home/u/secret.txt")
        fc.log_health("fetch", "web", "web_direct", False, 1, "公開ホストでない（intranet.example → 10.0.0.1）")
        fc.log_health("fetch", "youtube", "yt_dlp_subs", False, 1, "yt-dlp 失敗: ERROR: [youtube] https://www.youtube.com/watch?v=abc: x")
        body = fc.HEALTH.read_text(encoding="utf-8")
        for leaked in ("secret.txt", "file:", "intranet.example", "10.0.0.1", "watch?v=abc", "https://"):
            self.assertNotIn(leaked, body)
        self.assertIn("公開ホストでない", body)  # 理由の種類は残す

    def test_http_get_sends_the_honest_user_agent(self):
        # 送るヘッダそのものを見る（外へは出ない）
        sent = []

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b"ok"

        class FakeOpener:
            def open(self, req, timeout):
                sent.append(req.get_header("User-agent"))
                return FakeResp()
        orig = (fc._check_public, fc.urllib.request.build_opener)
        fc._check_public = lambda url: None
        fc.urllib.request.build_opener = lambda *a: FakeOpener()
        try:
            self._orig[2]("https://example.com/")
        finally:
            fc._check_public, fc.urllib.request.build_opener = orig
        self.assertEqual(sent, [fc.UA])
        self.assertTrue(fc.UA.startswith("fetch-chain/"))

    def test_rejects_non_public_and_non_http(self):
        for bad in ("http://127.0.0.1/", "http://localhost/", "file:///etc/passwd", "http://169.254.169.254/"):
            with self.assertRaises(fc.FetchError, msg=bad):
                fc._check_public(bad)


if __name__ == "__main__":
    unittest.main()
