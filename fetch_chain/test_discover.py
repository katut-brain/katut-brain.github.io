"""discover.py（壊れた取り方の代わりを探して足す）の検査。外には出ない: 通信（http_get）・名前解決・pip・venv は偽物。
並べ替え・仕様の検査・試用への追加・昇格・vet.py の判定・記録は本物のコードを通す。"""
import json, os, pathlib, shutil, socket, tempfile, unittest, zipfile
import fetchchain as fc
import discover as dc
import vet

os.environ["FETCHCHAIN_ENV"] = "gha"
fc.socket.getaddrinfo = lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
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
        fc.STATE, fc.HEALTH, fc.STATUS = self.tmp, self.tmp / "health.jsonl", self.tmp / "status.json"
        fc.AUTO_PLUGINS = self.tmp / "plugins"
        dc.CANDIDATES = self.tmp / "candidates.json"
        self.cands([{"name": "x_publish_oembed", "channel": "x_post", "kind": "http", "format": "json",
                     "url": "https://publish.twitter.com/oembed?url={url}", "map": {"text": "html"}}])

    def tearDown(self):
        fc.STATE, fc.HEALTH, fc.STATUS, fc.AUTO_PLUGINS, fc.http_get, dc.CANDIDATES = self._orig
        fc.load_registry()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cands(self, items):
        dc.CANDIDATES.write_text(json.dumps({"candidates": items}), encoding="utf-8")

    def fail_canary(self, method, n=3, env="gha"):
        os.environ["FETCHCHAIN_ENV"] = env
        try:
            for _ in range(n):
                fc.log_health("canary", "x_post", method, False, 1, "HTTP 503")
        finally:
            os.environ["FETCHCHAIN_ENV"] = "gha"


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

    def test_failures_elsewhere_do_not_trigger(self):
        self.fail_canary("x_fxtwitter", env="pc")
        fc.http_get = fake_http({"publish.twitter.com": (200, json.dumps({"html": "just setting up my twttr"}))})
        self.assertEqual(dc.run(), [])

    def test_failing_or_invalid_candidates_are_not_added(self):
        self.cands([{"name": "x_bad_spec", "channel": "x_post", "kind": "http", "format": "json",
                     "url": "https://evil.example/{url}", "map": {"text": "t"}, "ua": "Mozilla/5.0"},
                    {"name": "x_wrong_text", "channel": "x_post", "kind": "http", "format": "json",
                     "url": "https://other.example/{url}", "map": {"text": "t"}}])
        self.fail_canary("x_fxtwitter")
        fc.http_get = fake_http({"other.example": (200, json.dumps({"t": "something else"}))})
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
        for _ in range(6):
            fc.log_health("canary", "x_post", "x_publish_oembed", True, 1)
        self.assertEqual(dc.promote_trials(), [])
        fc.log_health("canary", "x_post", "x_publish_oembed", False, 1, "x")
        for _ in range(6):
            fc.log_health("canary", "x_post", "x_publish_oembed", True, 1)
        self.assertEqual(dc.promote_trials(), [])  # 途中で1回落ちたら数え直し
        fc.log_health("canary", "x_post", "x_publish_oembed", True, 1)
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

    def test_incomplete_candidate_and_failed_download(self):
        self.cands([{"name": "pkg", "kind": "wheel", "package": "pkg"}])
        self.assertTrue(dc.wheel("pkg")[0].startswith("wheel_invalid"))
        lines, made, _ = self.run_wheel({"pkg/__init__.py": "x = 1\n"}, rc=1)
        self.assertTrue(lines[0].startswith("wheel_download_failed"))


if __name__ == "__main__":
    unittest.main()
