"""discover.py（壊れた取り方の代わりを探して足す）の検査。外には出ない: 通信（http_get）・名前解決・pip・venv は偽物。
並べ替え・仕様の検査・試用への追加・昇格・vet.py の判定・記録は本物のコードを通す。"""
import ipaddress, json, os, pathlib, shutil, socket, tempfile, unittest, zipfile
import fetchchain as fc
import discover as dc
import vet

def _fake_gai(host, *a, **k):
    """名前解決を外へ出さない。IP の直書きはそのまま（内部アドレスの検査を本物で通す）、名前は公開の IP。"""
    try:
        ip = str(ipaddress.ip_address(host))
    except ValueError:
        ip = "8.8.8.8"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


SYND_OK = (200, json.dumps({"text": "just setting up my twttr"}))


def fake_http(table):
    def _get(url, timeout=15, max_bytes=2_000_000):
        for key, resp in table.items():
            if key in url:
                return resp
        return None, ""
    return _get


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._orig = (fc.STATE, fc.HEALTH, fc.STATUS, fc.AUTO_PLUGINS, fc.http_get, dc.CANDIDATES)
        self._env, self._gai = os.environ.get("FETCHCHAIN_ENV"), fc.socket.getaddrinfo
        os.environ["FETCHCHAIN_ENV"] = "gha"
        fc.socket.getaddrinfo = _fake_gai
        fc.STATE, fc.HEALTH, fc.STATUS = self.tmp, self.tmp / "health.jsonl", self.tmp / "status.json"
        fc.AUTO_PLUGINS = self.tmp / "plugins"
        dc.CANDIDATES = self.tmp / "candidates.json"
        self.cands([{"name": "x_publish_oembed", "channel": "x_post", "kind": "http", "format": "json",
                     "url": "https://publish.twitter.com/oembed?url={url}", "map": {"text": "html"}}])

    def tearDown(self):
        fc.STATE, fc.HEALTH, fc.STATUS, fc.AUTO_PLUGINS, fc.http_get, dc.CANDIDATES = self._orig
        fc.socket.getaddrinfo = self._gai
        if self._env is None:
            os.environ.pop("FETCHCHAIN_ENV", None)
        else:
            os.environ["FETCHCHAIN_ENV"] = self._env
        fc.load_registry()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cands(self, items):
        dc.CANDIDATES.write_text(json.dumps({"candidates": items}), encoding="utf-8")

    def fail_canary(self, method, n=3, env="gha"):
        os.environ["FETCHCHAIN_ENV"] = env
        try:
            for i in range(n):
                self.log(method, False, day=i)
        finally:
            os.environ["FETCHCHAIN_ENV"] = "gha"

    def log(self, method, ok, day):
        """day 日目の canary の結果を1行足す（1日1票の数え方を試すため日付を指定する）。"""
        with fc.HEALTH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-11-%02dT06:37:00+00:00" % (day + 1), "env": fc.where_am_i(),
                                "kind": "canary", "channel": "x_post", "method": method, "ok": ok}) + "\n")

    def assert_events_have_no_urls(self):
        p = fc.STATE / "events.jsonl"
        if p.exists():
            self.assertNotRegex(p.read_text(encoding="utf-8"), r"[A-Za-z]+://")


class WindowTest(Base):
    def test_nothing_happens_while_healthy(self):
        fc.log_health("canary", "x_post", "x_fxtwitter", False, 1, "x")
        fc.http_get = fake_http({"publish.twitter.com": (200, json.dumps({"html": "just setting up my twttr"}))})
        self.assertEqual(dc.run(), [])
        self.assertFalse((fc.AUTO_PLUGINS / "x_publish_oembed.json").exists())

    def test_broken_method_brings_in_a_trial_that_then_serves_fetches(self):
        self.fail_canary("x_fxtwitter")
        fc.http_get = fake_http({"publish.twitter.com": (200, json.dumps({"html": "<p>just setting up my twttr</p>"}))})
        lines = dc.run()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("added_trial channel=x_post name=x_publish_oembed replaces=x_fxtwitter"))
        spec = json.loads((fc.AUTO_PLUGINS / "x_publish_oembed.json").read_text(encoding="utf-8"))
        self.assertEqual((spec["status"], spec["where"]), ("trial", ["cloud"]))
        # 足した手段は次の取得から使われる（標準の3手段が全部だめでも取れる）
        out = fc.fetch("https://x.com/jack/status/20")
        self.assertEqual((out["ok"], out["method"]), (True, "x_publish_oembed"))
        # 見つけた場所（cloud）以外では使わない
        os.environ["FETCHCHAIN_ENV"] = "pc"
        try:
            self.assertNotIn("x_publish_oembed", [t["method"] for t in fc.fetch("https://x.com/jack/status/20")["tried"]])
        finally:
            os.environ["FETCHCHAIN_ENV"] = "gha"
        self.assertEqual(dc.run(), [])  # 2回目は同じ候補を足し直さない

    def test_reruns_on_one_day_do_not_trigger(self):
        for _ in range(3):
            self.log("x_fxtwitter", False, 0)   # 同じ日に3回落ちても「3日続けて」ではない
        fc.http_get = fake_http({"publish.twitter.com": (200, json.dumps({"html": "just setting up my twttr"}))})
        self.assertEqual(dc.run(), [])

    def test_only_approved_hosts_and_never_internal_urls(self):
        self.cands([{"name": "x_other_host", "channel": "x_post", "kind": "http", "format": "json",
                     "url": "https://unknown.example/{url}", "map": {"text": "t"}}])
        self.fail_canary("x_fxtwitter")
        seen = []
        fc.http_get = lambda url, **k: (seen.append(url), (200, json.dumps({"t": "just setting up my twttr"})))[1]
        lines = dc.run()
        self.assertTrue(lines[0].startswith("candidate_invalid") and "APPROVED_HOSTS" in lines[0])
        self.assertEqual(seen, [])
        # 見本が内部アドレスなら、承認済みの窓口にも渡さない
        self.cands([{"name": "x_publish_oembed", "channel": "x_post", "kind": "http", "format": "json",
                     "url": "https://publish.twitter.com/oembed?url={url}", "map": {"text": "html"}}])
        channels = fc.load_registry()
        channels["x_post"]["canary"]["url"] = "http://127.0.0.1/status/1"
        lines = dc.try_window_candidates(channels, {"x_post": ["x_fxtwitter"]})
        self.assertTrue(lines[0].startswith("candidate_failed"))
        self.assertEqual(seen, [])
        self.assert_events_have_no_urls()

    def test_events_never_contain_urls(self):
        self.cands([{"name": "x_publish_oembed", "channel": "x_post", "kind": "http", "format": "json",
                     "url": "https://publish.twitter.com/oembed?url={url}", "map": {"text": "html"}},
                    {"name": "x_bad", "channel": "x_post", "kind": "http", "format": "json",
                     "url": "http://publish.twitter.com/{url}", "map": {"text": "t"}}])
        self.fail_canary("x_fxtwitter")

        def boom(url, **k):
            raise fc.FetchError("失敗: %s" % url)   # 理由の文に URL が混ざる場合
        fc.http_get = boom
        lines = dc.run()
        self.assertEqual([l.split()[0] for l in lines], ["candidate_failed", "candidate_invalid"])
        self.assert_events_have_no_urls()

    def test_failures_elsewhere_do_not_trigger(self):
        self.fail_canary("x_fxtwitter", env="pc")
        fc.http_get = fake_http({"publish.twitter.com": (200, json.dumps({"html": "just setting up my twttr"}))})
        self.assertEqual(dc.run(), [])

    def test_failing_or_invalid_candidates_are_not_added(self):
        self.cands([{"name": "x_bad_spec", "channel": "x_post", "kind": "http", "format": "json",
                     "url": "https://evil.example/{url}", "map": {"text": "t"}, "ua": "Mozilla/5.0"},
                    {"name": "x_wrong_text", "channel": "x_post", "kind": "http", "format": "json",
                     "url": "https://noembed.com/embed?url={url}", "map": {"text": "t"}}])
        self.fail_canary("x_fxtwitter")
        fc.http_get = fake_http({"noembed.com": (200, json.dumps({"t": "something else"}))})
        lines = dc.run()
        self.assertTrue(lines[0].startswith("candidate_invalid") and "ua" in lines[0])
        self.assertTrue(lines[1].startswith("candidate_failed") and "期待する文字列" in lines[1])
        self.assertEqual(list(fc.AUTO_PLUGINS.glob("*.json")), [])
        self.assertNotIn("https://", (fc.STATE / "events.jsonl").read_text(encoding="utf-8"))

    def test_promotion_needs_seven_clean_days_in_this_place(self):
        fc.AUTO_PLUGINS.mkdir()
        spec = {"name": "x_publish_oembed", "channel": "x_post", "kind": "http", "format": "json", "status": "trial",
                "url": "https://publish.twitter.com/oembed?url={url}", "map": {"text": "html"}, "where": ["cloud"]}
        p = fc.AUTO_PLUGINS / "x_publish_oembed.json"
        p.write_text(json.dumps(spec), encoding="utf-8")
        for d in range(6):
            self.log("x_publish_oembed", True, d)
        self.assertEqual(dc.promote_trials(), [])
        self.log("x_publish_oembed", False, 6)
        for d in range(7, 13):
            self.log("x_publish_oembed", True, d)
        self.assertEqual(dc.promote_trials(), [])  # 途中で1回落ちたら数え直し
        for _ in range(5):
            self.log("x_publish_oembed", True, 12)  # 同じ日に何度通っても1日は1日
        self.assertEqual(dc.promote_trials(), [])
        self.log("x_publish_oembed", True, 13)
        self.assertTrue(dc.promote_trials()[0].startswith("promoted"))
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["status"], "active")

    def test_plugin_where_is_validated(self):
        ch = fc.load_registry()
        base = {"name": "x_w", "channel": "x_post", "kind": "http", "format": "json", "status": "trial",
                "url": "https://a.example/{url}", "map": {"text": "t"}}
        self.assertEqual(fc.validate_spec(dict(base, where=["cloud"]), ch), [])
        for bad in (["mars"], [], "cloud"):
            self.assertTrue(fc.validate_spec(dict(base, where=bad), ch), bad)


def make_wheel(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)


class WheelTest(Base):
    def run_wheel(self, files, sha=None, rc=0, approve=False):
        made = []

        def download(cmd):
            make_wheel(pathlib.Path(cmd[cmd.index("-d") + 1]) / "pkg-1.0-py3-none-any.whl", files)
            return rc
        tmpw = self.tmp / "w.whl"
        make_wheel(tmpw, files)
        self.cands([{"name": "pkg", "kind": "wheel", "package": "pkg", "version": "1.0",
                     "sha256": sha or dc._sha256(tmpw)}])
        lines = dc.wheel("pkg", pip_download=download, make_venv=lambda v, w: made.append(v), approve=approve)
        return lines, made, download

    def test_clean_wheel_is_installed_into_its_own_venv(self):
        lines, made, _ = self.run_wheel({"pkg/__init__.py": "def hello():\n    return 1\n"})
        self.assertTrue(lines[0].startswith("wheel_installed"))
        self.assertEqual(made, [fc.STATE / "venvs" / "pkg"])

    def test_only_binary_no_deps_and_pinned_version(self):
        seen = []

        def download(cmd):
            seen.append(cmd)
            return 1
        self.cands([{"name": "pkg", "kind": "wheel", "package": "pkg", "version": "1.0", "sha256": "0" * 64}])
        dc.wheel("pkg", pip_download=download, make_venv=lambda v, w: None)
        self.assertIn("--only-binary=:all:", seen[0])
        self.assertIn("--no-deps", seen[0])
        self.assertIn("pkg==1.0", seen[0])

    def test_hash_mismatch_is_refused(self):
        lines, made, _ = self.run_wheel({"pkg/__init__.py": "x = 1\n"}, sha="0" * 64)
        self.assertTrue(lines[0].startswith("wheel_hash_mismatch"))
        self.assertEqual(made, [])

    def test_dangerous_wheel_is_rejected(self):
        lines, made, _ = self.run_wheel({"pkg/__init__.py": "import os\np = os.path.expanduser('~/.ssh/id_rsa')\n"})
        self.assertTrue(lines[0].startswith("wheel_rejected"), lines)
        self.assertEqual(made, [])

    def test_suspicious_wheel_gets_a_review_request_for_codex(self):
        lines, made, _ = self.run_wheel({"pkg/__init__.py": "import subprocess\nsubprocess.run(['ls'])\n"})
        self.assertTrue(lines[0].startswith("wheel_needs_review"), lines)
        self.assertEqual(made, [])
        req = (fc.STATE / "review" / "pkg-1.0.md").read_text(encoding="utf-8")
        self.assertIn("逐語で引用", req)
        self.assertIn("pkg/__init__.py:", req)
        self.assertIn("2| subprocess.run(['ls'])", req)   # 該当行の前後のコードが依頼文に入る
        self.assertTrue((fc.STATE / "review" / "pkg-1.0-py3-none-any.whl").exists())   # 読む側が実物を開ける
        self.assertIn("pkg-1.0-py3-none-any.whl", req)

    def test_person_can_approve_after_reading_the_review(self):
        files = {"pkg/__init__.py": "import subprocess\nsubprocess.run(['ls'])\n"}
        made = []

        def download(cmd):
            make_wheel(pathlib.Path(cmd[cmd.index("-d") + 1]) / "pkg-1.0-py3-none-any.whl", files)
            return 0
        tmpw = self.tmp / "w.whl"
        make_wheel(tmpw, files)
        self.cands([{"name": "pkg", "kind": "wheel", "package": "pkg", "version": "1.0", "sha256": dc._sha256(tmpw)}])
        lines = dc.wheel("pkg", pip_download=download, make_venv=lambda v, w: made.append(v), approve=True)
        self.assertIn("approved_by_person=yes", lines[0])
        self.assertEqual(len(made), 1)

    def test_approval_never_overrides_reject(self):
        lines, made, _ = self.run_wheel({"pkg/__init__.py": "import os\np = os.path.expanduser('~/.ssh/id_rsa')\n"},
                                        approve=True)
        self.assertTrue(lines[0].startswith("wheel_rejected"))
        self.assertEqual(made, [])

    def test_names_that_would_leave_the_folder_are_refused(self):
        made = []
        for cand in ({"name": "../x", "package": "pkg", "version": "1.0"},
                     {"name": "pkg", "package": "../../evil", "version": "1.0"},
                     {"name": "pkg", "package": "pkg", "version": "1.0/../../x"},
                     {"name": "pkg", "package": "a/b", "version": "1.0"},
                     {"name": "pkg", "package": "pkg", "version": "1.0\\x"}):
            self.cands([dict(cand, kind="wheel", sha256="0" * 64)])
            lines = dc.wheel(cand["name"], pip_download=lambda c: self.fail("落としに行った"),
                             make_venv=lambda v, w: made.append(v))
            self.assertTrue(lines[0].startswith("wheel_invalid"), cand)
        self.assertEqual(made, [])

    def test_incomplete_candidate_and_failed_download(self):
        self.cands([{"name": "pkg", "kind": "wheel", "package": "pkg"}])
        self.assertTrue(dc.wheel("pkg")[0].startswith("wheel_invalid"))
        lines, made, _ = self.run_wheel({"pkg/__init__.py": "x = 1\n"}, rc=1)
        self.assertTrue(lines[0].startswith("wheel_download_failed"))


if __name__ == "__main__":
    unittest.main()
