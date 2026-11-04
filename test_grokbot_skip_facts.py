# -*- coding: utf-8 -*-
"""x_grokbot を飛ばした理由を facts に残す（値は残さない）・canary の警告の回帰テスト。外には出ない。"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["FETCHCHAIN_STATE_DIR"] = tempfile.mkdtemp()
os.environ["FETCHCHAIN_ENV"] = "gha"
KEYS = ("GROKBOT_WEBHOOK_URL", "GROKBOT_WEBHOOK_KEY", "SLACK_GROK_TOKEN", "GROKBOT_SLACK_CHANNEL")
for _k in KEYS + ("GROKBOT_POLL_TIMEOUT",):
    os.environ.pop(_k, None)

import fetch_content  # noqa: E402
from fetch_chain import fetchchain as fc  # noqa: E402

URL = "https://x.com/h/status/123"
SYND = {"text": "本文", "user": {"name": "N", "screen_name": "h"},
        "created_at": "2026-10-01T00:00:00.000Z", "mediaDetails": []}
SECRET = "s3cr3t-value"


class SkipFactsTest(unittest.TestCase):
    def setUp(self):
        for k in KEYS + ("GROKBOT_POLL_TIMEOUT",):
            os.environ.pop(k, None)

    def tearDown(self):
        self.setUp()

    def _set_all(self):
        for k in KEYS:
            os.environ[k] = SECRET

    def run_x(self, **kw):
        with patch("fetch_content._get", return_value=(200, json.dumps(SYND))):
            return fetch_content.fetch_x(URL, follow_links=False, **kw)

    def test_missing_var_recorded_by_name_only(self):
        self._set_all()
        del os.environ["SLACK_GROK_TOKEN"]
        r = self.run_x()
        self.assertEqual(r["fetched_via"], "x_syndication")
        self.assertEqual(r["x_primary_skipped"], "x_grokbot")
        self.assertEqual(r["x_primary_reason"], "missing:SLACK_GROK_TOKEN")
        self.assertNotIn(SECRET, json.dumps(r, ensure_ascii=False))

    def test_all_missing_reports_first(self):
        r = self.run_x()
        self.assertEqual(r["x_primary_reason"], "missing:GROKBOT_WEBHOOK_URL")

    def test_invalid_var(self):
        self._set_all()
        os.environ["GROKBOT_SLACK_CHANNEL"] = "C1\nC2"
        r = self.run_x()
        self.assertEqual(r["x_primary_reason"], "invalid:GROKBOT_SLACK_CHANNEL")

    def test_insufficient_time(self):
        self._set_all()
        r = self.run_x(deadline=fetch_content._now() + 10)
        self.assertEqual(r["x_primary_reason"], "insufficient_time")

    def test_quote_path(self):
        self._set_all()
        r = self.run_x(_quote=True)
        self.assertEqual(r["x_primary_reason"], "quote_path")

    def test_nested_call_does_not_clobber_outer(self):
        fetch_content._x_skip.update({"x_primary_skipped": "x_grokbot", "x_primary_reason": "missing:A"})
        self._set_all()
        self.run_x(_quote=True)
        self.assertEqual(fetch_content._x_skip["x_primary_reason"], "missing:A")
        fetch_content._x_skip.clear()

    def test_attempted_has_no_skip_keys(self):
        self._set_all()
        with patch.object(fc, "x_grokbot", side_effect=fc.FetchError("x")):
            r = self.run_x()
        self.assertNotIn("x_primary_skipped", r)

    def test_canary_warning(self):
        self.assertIn("::warning", fc._grokbot_env_warning())
        self.assertIn("missing:GROKBOT_SLACK_CHANNEL", fc._grokbot_env_warning())
        self._set_all()
        self.assertEqual(fc._grokbot_env_warning(), "")
        os.environ["GROKBOT_WEBHOOK_KEY"] = "a\rb"
        w = fc._grokbot_env_warning()
        self.assertIn("invalid:GROKBOT_WEBHOOK_KEY", w)
        self.assertNotIn("a\rb", w)

    def test_canary_main_prints_warning(self):
        buf = io.StringIO()
        buf.reconfigure = lambda **k: None
        with patch.object(fc, "canary", return_value={"checked_at": "", "channels": {}}), \
             patch.object(fc, "render", return_value=""), redirect_stdout(buf), \
             patch.object(sys, "stderr", buf):
            fc.main(["x", "canary"])
        self.assertIn("::warning title=x_grokbot skipped::", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
