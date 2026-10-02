#!/usr/bin/env python3
# test_verify_vault_push.py — verify_vault_push.py（手順8.5 の Vault 照合）の回帰テスト。
#
# 一時ディレクトリに本物の git リポジトリを3つ作って通す（モックで差し替えない）:
#   origin.git … GitHub 上の Vaultリポの代わり（bare）
#   pusher     … push_files の代わり。ここから main へ押す
#   vault      … 夜間ランの Vault クローンの代わり。押す前に clone しておくので、
#                照合するには fetch が要る（本番と同じ順序）
# スクリプトは CLI として別プロセスで呼ぶ（本番と同じ入口）。例外経路の1本だけ import して
# verify を差し替える。標準ライブラリの unittest のみ。
#
# git の設定はテストの外から漏れ込まないよう、空の global 設定を使い system 設定を切る。

import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(REPO_DIR, "verify_vault_push.py")
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import verify_vault_push  # noqa: E402

NOTE = "Explore/bookmarks/rd-1871926613-jp-stock-full-agent-portfolio.md"
NOTE2 = "Explore/bookmarks/rd-1871927376-agent-web-data-extraction-tools.md"
BODY = ("---\ndate: 2026-10-01\ntags: [type/bookmark, ai-agent]\nraindrop_id: 1871926613\n"
        "source: https://x.com/blog_uki/status/2105432136035750209?s=12\nrelated: []\n---\n\n"
        "日本株をフルエージェントで運用する投稿（決済と新規建てで約200注文）。\n\n## 関連ノート\n")


class _Repos(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        gcfg = os.path.join(self.root, "gitconfig")
        with open(gcfg, "w", encoding="utf-8") as fh:
            fh.write("[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=gcfg, GIT_CONFIG_NOSYSTEM="1",
                        GIT_TERMINAL_PROMPT="0")
        self.origin = os.path.join(self.root, "origin.git")
        self.pusher = os.path.join(self.root, "pusher")
        self.vault = os.path.join(self.root, "vault")
        self._git(self.root, "init", "--quiet", "--bare", self.origin)
        self._git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self._git(self.root, "clone", "--quiet", self.origin, self.pusher)
        self._git(self.pusher, "checkout", "--quiet", "-b", "main")
        self.push("RULES.md", b"# rules\n")
        # 夜間ランの clone は push_files より前に作られている
        self._git(self.root, "clone", "--quiet", self.origin, self.vault)

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, cwd, *args):
        r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, env=self.env)
        if r.returncode != 0:
            raise AssertionError("git %s failed: %s" % (args, r.stderr.decode("utf-8", "replace")))
        return r

    def push(self, rel, data):
        """push_files の代わり: pusher から main へ1ファイルを押す。"""
        full = os.path.join(self.pusher, rel)
        os.makedirs(os.path.dirname(full) or self.pusher, exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)
        self._git(self.pusher, "add", rel)
        self._git(self.pusher, "commit", "--quiet", "-m", "bookmark notes")
        self._git(self.pusher, "push", "--quiet", "origin", "main")

    def local(self, rel, data):
        """夜間ランが Vault クローンに書いた（未追跡の）ノート。"""
        full = os.path.join(self.vault, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)

    def run_cli(self, *args, cwd=None):
        r = subprocess.run([sys.executable, SCRIPT] + list(args), capture_output=True,
                           env=self.env, cwd=cwd or self.root)
        out = r.stdout.decode("utf-8")
        return r.returncode, out, r.stderr.decode("utf-8", "replace")

    def summary(self, out):
        lines = [l for l in out.splitlines() if l.startswith("VERIFY_SUMMARY:")]
        self.assertEqual(len(lines), 1, out)
        return dict(kv.split("=", 1) for kv in lines[0].split()[1:])


class TestVerify(_Repos):
    def test_match_after_fetch(self):
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        # 押す前の clone なので、fetch しなければ main 上に無い
        r = subprocess.run(["git", "-C", self.vault, "cat-file", "-e", "origin/main:" + NOTE],
                           capture_output=True, env=self.env)
        self.assertNotEqual(r.returncode, 0)
        rc, out, err = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("WRITE_PATH: %s verify=match bytes=%d" % (NOTE, len(b)), out)
        s = self.summary(out)
        self.assertEqual((s["status"], s["total"], s["match"]), ("ok", "1", "1"))
        self.assertNotIn("Traceback", err)

    def test_runs_from_another_directory(self):
        # 2026-09-29〜10-01 の空振りの再現: 公開リポのディレクトリで実行しても、
        # パスは --repo-dir から解決されるので照合できる
        public = os.path.join(self.root, "katut-brain.github.io")
        os.makedirs(public)
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE, cwd=public)
        self.assertEqual(rc, 0, out)
        self.assertIn("verify=match", out)
        self.assertNotIn("local_file_missing", out)

    def test_mismatch_when_transcription_changed_bytes(self):
        # push_files に渡した本文が化けた（全角括弧→半角）
        self.push(NOTE, BODY.replace("（", "(").replace("）", ")").encode("utf-8"))
        self.local(NOTE, BODY.encode("utf-8"))
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertRegex(out, r"WRITE_PATH: %s verify=MISMATCH local_bytes=\d+ remote_bytes=\d+"
                         % re.escape(NOTE))
        self.assertEqual(self.summary(out)["mismatch"], "1")

    def test_same_length_change_is_mismatch(self):
        # バイト数が同じ化け（数字1文字）も見逃さない
        self.push(NOTE, BODY.replace("200", "300").encode("utf-8"))
        self.local(NOTE, BODY.encode("utf-8"))
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        n = len(BODY.encode("utf-8"))
        self.assertIn("verify=MISMATCH local_bytes=%d remote_bytes=%d" % (n, n), out)

    def test_byte_exact_crlf_is_mismatch(self):
        self.push(NOTE, BODY.encode("utf-8"))
        self.local(NOTE, BODY.replace("\n", "\r\n").encode("utf-8"))
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("verify=MISMATCH", out)

    def test_autocrlf_does_not_change_comparison(self):
        # 照合は blob の生のバイト。clone 側に autocrlf があっても LF 同士なら一致
        self._git(self.vault, "config", "core.autocrlf", "true")
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 0, out)

    def test_missing_remote(self):
        self.local(NOTE, BODY.encode("utf-8"))  # 押していない
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("WRITE_PATH: %s verify=missing_remote" % NOTE, out)
        self.assertEqual(self.summary(out)["missing_remote"], "1")

    def test_local_file_missing(self):
        self.push(NOTE, BODY.encode("utf-8"))
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("WRITE_PATH: %s verify=skipped reason=local_file_missing" % NOTE, out)

    def test_absolute_path_inside_repo(self):
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        rc, out, _ = self.run_cli("--repo-dir", self.vault, os.path.join(self.vault, NOTE))
        self.assertEqual(rc, 0, out)
        self.assertIn("WRITE_PATH: %s verify=match" % NOTE, out)

    def test_path_outside_repo(self):
        outside = os.path.join(self.root, "elsewhere.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        for p in (outside, "../elsewhere.md"):
            rc, out, _ = self.run_cli("--repo-dir", self.vault, p)
            self.assertEqual(rc, 1, p)
            self.assertIn("verify=skipped reason=outside_repo", out)

    def test_mixed_results_fail_overall(self):
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        self.local(NOTE2, b"not pushed\n")
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE, NOTE2)
        self.assertEqual(rc, 1)
        s = self.summary(out)
        self.assertEqual((s["total"], s["match"], s["missing_remote"]), ("2", "1", "1"))

    def test_sees_latest_main_not_stale_tracking_ref(self):
        # 古い origin/main を持っていても、照合時点の main を見る
        self.push(NOTE, b"old\n")
        self._git(self.vault, "fetch", "--quiet", "origin")
        self.push(NOTE, BODY.encode("utf-8"))
        self.local(NOTE, BODY.encode("utf-8"))
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 0, out)
        head = self._git(self.pusher, "rev-parse", "HEAD").stdout.decode().strip()
        self.assertEqual(self.summary(out)["head"], head[:12])

    def test_fetch_failed(self):
        self.local(NOTE, BODY.encode("utf-8"))
        self._git(self.vault, "remote", "set-url", "origin", os.path.join(self.root, "nope.git"))
        rc, out, err = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("WRITE_PATH: %s verify=unreadable reason=fetch_failed" % NOTE, out)
        self.assertEqual(self.summary(out)["unreadable"], "1")
        self.assertNotIn("Traceback", err)

    def test_not_a_repo(self):
        plain = os.path.join(self.root, "plain")
        os.makedirs(plain)
        rc, out, err = self.run_cli("--repo-dir", plain, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("verify=unreadable reason=not_a_repo", out)
        self.assertNotIn("Traceback", err)

    def test_does_not_touch_worktree_or_index(self):
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        before = self._git(self.vault, "status", "--porcelain").stdout
        self.run_cli("--repo-dir", self.vault, NOTE)
        after = self._git(self.vault, "status", "--porcelain").stdout
        self.assertEqual(before, after)
        with open(os.path.join(self.vault, NOTE), "rb") as fh:
            self.assertEqual(fh.read(), b)


class TestArgsAndErrors(unittest.TestCase):
    def test_usage_errors_exit_2(self):
        for args in ([], ["--repo-dir", "."], ["Explore/x.md"]):
            buf = io.StringIO()
            with redirect_stdout(buf), unittest.mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(verify_vault_push.main(args), 2, args)

    def test_exception_is_reported_without_traceback(self):
        buf = io.StringIO()
        with unittest.mock.patch.object(verify_vault_push, "verify", side_effect=OSError("x")), \
                redirect_stdout(buf):
            rc = verify_vault_push.main(["--repo-dir", ".", NOTE])
        self.assertEqual(rc, 1)
        out = buf.getvalue()
        self.assertIn("VERIFY_SUMMARY: status=error total=1", out)
        self.assertIn("error=OSError", out)


class TestRoutinePrompt(unittest.TestCase):
    """手順書（cloud_routine_prompt.md）の手順8.5 がこのスクリプトを正しい形で呼んでいること。"""

    def setUp(self):
        with io.open(os.path.join(REPO_DIR, "cloud_routine_prompt.md"), encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r"^8\.5\. .*?(?=^## )", text, re.S | re.M)
        self.assertIsNotNone(m, "手順8.5 の節が見つからない")
        self.step = m.group(0)

    def test_step_8_5_uses_this_script_with_repo_dir(self):
        cmds = [l.strip() for l in self.step.splitlines()
                if l.strip().startswith("python3 verify_vault_push.py")]
        self.assertTrue(cmds, "手順8.5 に verify_vault_push.py の呼び出しが無い")
        for c in cmds:
            self.assertIn("--repo-dir ", c)

    def test_step_8_5_no_longer_uses_cwd_relative_verify(self):
        self.assertNotIn("push_via_api.sh --verify-only katut-brain/obsidian-vault", self.step)


if __name__ == "__main__":
    unittest.main()
