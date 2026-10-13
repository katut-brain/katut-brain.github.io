# -*- coding: utf-8 -*-
"""手順1.5 の依存点検(check_deps.py)と、gemini_unavailable の原因補足
(fetch_content._video_reason_detail / fetch_facts の video_reason_detail)の回帰テスト。
2026-10-06 夜から X動画が全件 gemini_unavailable になった原因（ImportErrorの握りつぶし）を
次回は1行で切り分けられるようにするためのもの。標準ライブラリのみ。
"""
import os
import re
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import check_deps  # noqa: E402
import fetch_content  # noqa: E402

LINE_RE = re.compile(
    r"^DEPS_STATUS: python=\d+\.\d+\.\d+ genai=(ok|fail\(.+\)) "
    r"yt_transcript=(ok|fail\(.+\)) gemini_key=(set|unset) "
    r"pip_exit=(-?\d+|unknown) pip_retry=(none|bsp)$")


class CheckDepsTest(unittest.TestCase):
    def test_line_format_and_pip_exit(self):
        self.assertRegex(check_deps.status_line(0), LINE_RE)
        self.assertIn("pip_exit=0 pip_retry=none", check_deps.status_line(0))
        self.assertIn("pip_exit=1 ", check_deps.status_line("1"))
        self.assertIn("pip_exit=unknown ", check_deps.status_line(None))
        self.assertIn("pip_exit=unknown ", check_deps.status_line("abc"))
        self.assertTrue(check_deps.status_line(0, "bsp").endswith("pip_retry=bsp"))

    def test_gemini_key_flag_never_shows_value(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "topsecret"}):
            line = check_deps.status_line(0)
        self.assertIn("gemini_key=set", line)
        self.assertNotIn("topsecret", line)
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            self.assertIn("gemini_key=unset", check_deps.status_line(0))

    def test_secrets_redacted_in_exception(self):
        key = "AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2"
        for k, text in ((key, "boom " + key + " tail"),
                        ("plainkey", "boom plainkey tail"),
                        ("", "boom " + key)):
            with patch.dict(os.environ, {"GEMINI_API_KEY": k}),                  patch.object(check_deps, "exec", create=True, side_effect=RuntimeError(text)):
                line = check_deps.status_line(0)
            self.assertIn("RuntimeError: boom ***", line)
            self.assertNotIn("plainkey" if k == "plainkey" else key, line)

    def test_redact_before_truncate(self):
        key = "AIza" + "x" * 30
        out = check_deps.redact("y" * 190 + key)   # 切り詰めで鍵が途中で切れても漏れない
        self.assertNotIn("AIza", out)

    def _install(self, results):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            rc, out = results[len(calls) - 1]
            return subprocess.CompletedProcess(cmd, rc, out, "")
        with patch.object(subprocess, "run", side_effect=fake):
            return check_deps.install(), calls

    def test_install_ok_no_retry(self):
        (rc, retry), calls = self._install([(0, "")])
        self.assertEqual((rc, retry, len(calls)), (0, "none", 1))
        self.assertEqual(calls[0][1:4], ["-m", "pip", "install"])

    def test_install_pep668_retries_once_with_bsp(self):
        (rc, retry), calls = self._install(
            [(1, "error: externally-managed-environment"), (0, "")])
        self.assertEqual((rc, retry, len(calls)), (0, "bsp", 2))
        self.assertIn("--break-system-packages", calls[1])
        self.assertNotIn("--break-system-packages", calls[0])

    def test_install_other_failure_no_retry_and_tail(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            (rc, retry), calls = self._install([(2, "\n".join("l%d" % i for i in range(9)))])
        self.assertEqual((rc, retry, len(calls)), (2, "none", 1))
        self.assertIn("l8", buf.getvalue())
        self.assertNotIn("l3", buf.getvalue())

    def test_install_pip_cannot_start(self):
        with patch.object(subprocess, "run", side_effect=OSError("nope")):
            rc, retry = check_deps.install()
        self.assertEqual((rc, retry), (127, "none"))

    def test_import_failure_reports_type_and_message(self):
        with patch.dict(sys.modules, {"google": None, "youtube_transcript_api": None}):
            line = check_deps.status_line(7)
        self.assertRegex(line, r"genai=fail\((ModuleNotFoundError|ImportError): ")
        self.assertRegex(line, r"yt_transcript=fail\((ModuleNotFoundError|ImportError): ")
        self.assertIn("pip_exit=7", line)
        self.assertRegex(line, LINE_RE)

    def test_message_truncated_to_200(self):
        with patch.object(check_deps, "exec", create=True,
                          side_effect=ImportError("x" * 500)):
            line = check_deps.status_line(0)
        m = re.search(r"genai=fail\(ImportError: (x+)\)", line)
        self.assertEqual(len(m.group(1)), 200)

    def test_script_exits_zero_one_line(self):
        env = dict(os.environ, PIP_EXIT="3")
        r = subprocess.run([sys.executable, os.path.join(HERE, "check_deps.py")],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        out = r.stdout.strip().splitlines()
        self.assertEqual(len(out), 1)
        self.assertRegex(out[0], LINE_RE)
        self.assertIn("pip_exit=3 ", out[0])

    def test_install_mode_joined_with_and_chain_exit0_and_prints(self):
        """--install は pip が失敗しても（偽の空 requirements 環境でも）exit 0 かつ DEPS_STATUS を出す。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:   # requirements.txt が無い→pip は失敗する
            r = subprocess.run([sys.executable, os.path.join(HERE, "check_deps.py"), "--install"],
                               capture_output=True, text=True, cwd=d)
        self.assertEqual(r.returncode, 0)
        last = r.stdout.strip().splitlines()[-1]
        self.assertRegex(last, LINE_RE)
        self.assertNotIn("pip_exit=0 ", last)


class VideoReasonDetailTest(unittest.TestCase):
    def _facts(self, reason):
        return fetch_content._facts("https://x.com/a/status/1", time.time(),
                                    {"ok": True, "video_reason": reason})

    def test_import_error_detail(self):
        with patch.object(fetch_content, "_GEMINI_AVAILABLE", False), \
             patch.object(fetch_content, "_GEMINI_IMPORT_ERROR",
                          "ModuleNotFoundError:No module named 'google'"):
            f = self._facts("gemini_unavailable")
        self.assertEqual(f["video_reason"], "gemini_unavailable")
        self.assertEqual(f["video_reason_detail"],
                         "import_error:ModuleNotFoundError:No module named 'google'")

    def test_module_level_import_error_is_captured(self):
        """fetch_content を google なしで読み直すと型とメッセージが保持される。"""
        import importlib
        try:
            with patch.dict(sys.modules, {"google": None}):
                importlib.reload(fetch_content)
                self.assertFalse(fetch_content._GEMINI_AVAILABLE)
                self.assertRegex(fetch_content._GEMINI_IMPORT_ERROR,
                                 r"^(ModuleNotFoundError|ImportError):.+")
                self.assertTrue(fetch_content._video_reason_detail(
                    "gemini_unavailable").startswith("import_error:"))
        finally:
            importlib.reload(fetch_content)

    def _reload_with_google_raising(self, exc):
        """`from google import genai` の時点で exc を投げさせて fetch_content を読み直す。"""
        import builtins
        import importlib
        real = builtins.__import__

        def fake(name, *a, **kw):
            if name == "google":
                raise exc
            return real(name, *a, **kw)
        with patch.object(builtins, "__import__", fake):
            importlib.reload(fetch_content)
        return fetch_content._video_reason_detail("gemini_unavailable")

    def test_non_importerror_at_import_does_not_crash(self):
        """google 側が AttributeError/RuntimeError で壊れても fetch_content は落ちず、型付きで残る。"""
        import importlib
        try:
            for exc in (AttributeError("boom attr"), RuntimeError("boom rt")):
                d = self._reload_with_google_raising(exc)
                self.assertFalse(fetch_content._GEMINI_AVAILABLE)
                self.assertEqual(d, "import_error:%s:%s" % (type(exc).__name__, exc))
        finally:
            importlib.reload(fetch_content)

    def test_import_error_secret_redacted_in_detail(self):
        import importlib
        key = "AIza" + "Z9y8X7w6V5u4T3s2R1q0P9o8"
        try:
            with patch.dict(os.environ, {"GEMINI_API_KEY": key}):
                d = self._reload_with_google_raising(RuntimeError("failed with key=" + key))
            self.assertIn("key=***", d)
            self.assertNotIn(key, d)
        finally:
            importlib.reload(fetch_content)

    def test_no_api_key_detail(self):
        with patch.object(fetch_content, "_GEMINI_AVAILABLE", True), \
             patch.object(fetch_content, "_GEMINI_API_KEY", ""):
            f = self._facts("gemini_unavailable")
            self.assertEqual(f["video_reason_detail"], "no_api_key")
            self.assertEqual(fetch_content._gemini_video_understanding("https://video.twimg.com/a.mp4"),
                             ("", "gemini_unavailable"))

    def test_other_reasons_have_empty_detail(self):
        with patch.object(fetch_content, "_GEMINI_AVAILABLE", False):
            self.assertEqual(self._facts("")["video_reason_detail"], "")
            self.assertEqual(self._facts("gemini_quota")["video_reason_detail"], "")

    def test_detail_never_contains_api_key(self):
        secret = "SECRET-KEY-VALUE-123"
        with patch.object(fetch_content, "_GEMINI_AVAILABLE", True), \
             patch.object(fetch_content, "_GEMINI_API_KEY", secret):
            self.assertEqual(self._facts("gemini_unavailable")["video_reason_detail"], "no_api_key")
        with patch.object(fetch_content, "_GEMINI_AVAILABLE", False), \
             patch.object(fetch_content, "_GEMINI_API_KEY", secret):
            self.assertNotIn(secret, self._facts("gemini_unavailable")["video_reason_detail"])


if __name__ == "__main__":
    unittest.main()
