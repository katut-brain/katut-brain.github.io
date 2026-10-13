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
    r"yt_transcript=(ok|fail\(.+\)) pip_exit=(-?\d+|unknown)$")


class CheckDepsTest(unittest.TestCase):
    def test_line_format_and_pip_exit(self):
        self.assertRegex(check_deps.status_line(0), LINE_RE)
        self.assertTrue(check_deps.status_line(0).endswith("pip_exit=0"))
        self.assertTrue(check_deps.status_line("1").endswith("pip_exit=1"))
        self.assertTrue(check_deps.status_line(None).endswith("pip_exit=unknown"))
        self.assertTrue(check_deps.status_line("abc").endswith("pip_exit=unknown"))

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
        self.assertTrue(out[0].endswith("pip_exit=3"))


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
