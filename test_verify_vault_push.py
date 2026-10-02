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
        s = dict(kv.split("=", 1) for kv in lines[0].split()[1:])
        # status=ok は全件 match のときだけ。それ以外は failed（例外だけ error）
        if s["status"] != "error":
            self.assertEqual(s["status"] == "ok", s["match"] == s["total"], lines[0])
            self.assertIn(s["status"], ("ok", "failed"))
        # 渡したパス1つにつき WRITE_PATH 行がちょうど1行
        if s["status"] != "error":
            self.assertEqual(len([l for l in out.splitlines() if l.startswith("WRITE_PATH:")]),
                             int(s["total"]), out)
        return s

    def run_inproc(self, *args):
        """import した main() を本物の git リポジトリに対して呼ぶ（例外を差し込むテスト用）。"""
        buf = io.StringIO()
        with unittest.mock.patch.dict(os.environ, self.env), redirect_stdout(buf):
            rc = verify_vault_push.main(list(args))
        return rc, buf.getvalue()


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
        s = self.summary(out)
        self.assertEqual((s["status"], s["mismatch"]), ("failed", "1"))

    def test_same_length_change_is_mismatch(self):
        # バイト数が同じ化け（数字1文字）も見逃さない
        self.push(NOTE, BODY.replace("200", "300").encode("utf-8"))
        self.local(NOTE, BODY.encode("utf-8"))
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        n = len(BODY.encode("utf-8"))
        self.assertIn("verify=MISMATCH local_bytes=%d remote_bytes=%d" % (n, n), out)

    def test_trailing_newline_bom_and_unicode_form_are_mismatch(self):
        # push_files に文字列で渡すと起きやすい化け: 末尾改行の欠落・BOM・合成/分解形の違い。
        # 「正規化してから比べる」変更が入ったら赤になる
        import unicodedata
        body = BODY + "がぎぐ\n"
        cases = [
            (body.rstrip("\n").encode("utf-8"), body.encode("utf-8")),
            (body.encode("utf-8"), b"\xef\xbb\xbf" + body.encode("utf-8")),
            (unicodedata.normalize("NFD", body).encode("utf-8"),
             unicodedata.normalize("NFC", body).encode("utf-8")),
        ]
        for remote, local in cases:
            self.assertNotEqual(remote, local)
            self.push(NOTE, remote)
            self.local(NOTE, local)
            rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
            self.assertEqual(rc, 1, (remote[-12:], local[:12]))
            self.assertIn("verify=MISMATCH", out)

    def test_fetch_runs_with_a_timeout(self):
        # fetch が詰まっても無人ランを止めない（timeout を外す変更を止める）
        seen = []
        real_git = verify_vault_push._git

        def spy(top, args, timeout=verify_vault_push.GIT_TIMEOUT):
            if args[:1] == ["fetch"]:
                seen.append(timeout)
            return real_git(top, args, timeout)

        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        with unittest.mock.patch.object(verify_vault_push, "_git", spy):
            rc, out = self.run_inproc("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen, [verify_vault_push.FETCH_TIMEOUT])
        self.assertTrue(0 < verify_vault_push.FETCH_TIMEOUT <= 300)

        def hang(top, args, timeout=verify_vault_push.GIT_TIMEOUT):
            if args[:1] == ["fetch"]:
                raise subprocess.TimeoutExpired(args, timeout)
            return real_git(top, args, timeout)

        with unittest.mock.patch.object(verify_vault_push, "_git", hang):
            rc, out = self.run_inproc("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("verify=unreadable reason=fetch_failed", out)

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
        # 絶対パスでルートの外 → outside_repo。`..` で外へ出る形は、その前に dot_segment で弾く
        for p, reason in ((outside, "outside_repo"), ("../elsewhere.md", "dot_segment")):
            rc, out, _ = self.run_cli("--repo-dir", self.vault, p)
            self.assertEqual(rc, 1, p)
            self.assertIn("verify=skipped reason=%s" % reason, out)

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

    def _symlink_or_skip(self, target, link, is_dir=False):
        try:
            os.symlink(target, link, target_is_directory=is_dir)
        except (OSError, NotImplementedError) as e:
            self.skipTest("この環境ではシンボリックリンクを作れない: %s" % e)

    def test_symlinked_file_is_not_verified_as_its_target(self):
        # rd-A.md が rd-B.md へのリンクで、main 上の A は未着・B は正常。B を照合して match と
        # 言ってはいけない（Codex 指摘 P0）
        b = BODY.encode("utf-8")
        self.push(NOTE2, b)
        self.local(NOTE2, b)
        self._symlink_or_skip(os.path.basename(NOTE2), os.path.join(self.vault, NOTE))
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("WRITE_PATH: %s verify=skipped reason=symlink" % NOTE, out)
        self.assertNotIn("verify=match", out)

    def test_symlinked_directory_on_the_way_is_rejected(self):
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        real = os.path.join(self.root, "realdir")
        os.makedirs(real)
        with open(os.path.join(real, os.path.basename(NOTE)), "wb") as fh:
            fh.write(b)
        os.makedirs(os.path.join(self.vault, "Explore"), exist_ok=True)
        self._symlink_or_skip(real, os.path.join(self.vault, "Explore", "bookmarks"), is_dir=True)
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("verify=skipped reason=symlink", out)

    def test_symlink_check_without_real_links(self):
        # 本物のリンクを作れない環境（Windows の一般権限）でも判定の流れを通す
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        real_islink = os.path.islink
        for linked in (NOTE, "Explore/bookmarks", "Explore"):
            def fake_islink(p, linked=linked):
                return str(p).replace("\\", "/").endswith(linked) or real_islink(p)
            with unittest.mock.patch.object(verify_vault_push.os.path, "islink", fake_islink):
                rc, out = self.run_inproc("--repo-dir", self.vault, NOTE)
            self.assertEqual(rc, 1, linked)
            self.assertIn("WRITE_PATH: %s verify=skipped reason=symlink" % NOTE, out)
            self.assertEqual(self.summary(out)["status"], "failed")

    def test_dot_segments_are_rejected_before_normalizing(self):
        # `link/..` を正規化で消すと途中のリンクを見逃す（2周目 Codex 指摘 P0）。正規化の前に弾く
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        for p in ("Explore/link/../bookmarks/" + os.path.basename(NOTE), "./" + NOTE,
                  "Explore/./bookmarks/" + os.path.basename(NOTE),
                  os.path.join(self.vault, "Explore", "x", "..", "bookmarks", os.path.basename(NOTE))):
            rc, out, _ = self.run_cli("--repo-dir", self.vault, p)
            self.assertEqual(rc, 1, p)
            self.assertIn("verify=skipped reason=dot_segment", out)
            self.assertNotIn("verify=match", out)

    def test_symlink_then_dotdot_is_rejected(self):
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        real = os.path.join(self.root, "elsewhere")
        os.makedirs(real)
        self._symlink_or_skip(real, os.path.join(self.vault, "Explore", "link"), is_dir=True)
        rc, out, _ = self.run_cli("--repo-dir", self.vault,
                                  "Explore/link/../bookmarks/" + os.path.basename(NOTE))
        self.assertEqual(rc, 1)
        self.assertNotIn("verify=match", out)

    def test_duplicate_path_is_not_counted_twice(self):
        # 同じノートを2回渡すと、渡し漏れがあっても件数が合ってしまう
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE, NOTE.replace("/", "\\"))
        self.assertEqual(rc, 1)
        self.assertIn("WRITE_PATH: %s verify=skipped reason=duplicate" % NOTE, out)
        s = self.summary(out)
        self.assertEqual((s["status"], s["total"], s["match"], s["skipped"]), ("failed", "2", "1", "1"))

    def test_symlink_entry_on_main_is_not_a_match(self):
        # main 側がシンボリックリンク（mode 120000）で、リンク先の文字列が手元と同じ
        b = BODY.encode("utf-8")
        h = subprocess.run(["git", "-C", self.pusher, "hash-object", "-w", "--stdin"], input=b,
                           capture_output=True, env=self.env).stdout.decode().strip()
        self._git(self.pusher, "update-index", "--add", "--cacheinfo", "120000,%s,%s" % (h, NOTE))
        self._git(self.pusher, "commit", "--quiet", "-m", "link")
        self._git(self.pusher, "push", "--quiet", "origin", "main")
        self.local(NOTE, b)
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("WRITE_PATH: %s verify=MISMATCH reason=not_regular_file mode=120000" % NOTE, out)

    def test_executable_file_on_main_is_still_compared(self):
        b = BODY.encode("utf-8")
        h = subprocess.run(["git", "-C", self.pusher, "hash-object", "-w", "--stdin"], input=b,
                           capture_output=True, env=self.env).stdout.decode().strip()
        self._git(self.pusher, "update-index", "--add", "--cacheinfo", "100755,%s,%s" % (h, NOTE))
        self._git(self.pusher, "commit", "--quiet", "-m", "exec")
        self._git(self.pusher, "push", "--quiet", "origin", "main")
        self.local(NOTE, b)
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 0, out)

    def test_windows_junction_counts_as_link(self):
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)

        def fake_isjunction(p):
            return str(p).replace("\\", "/").endswith("Explore/bookmarks")

        with unittest.mock.patch.object(verify_vault_push.os.path, "isjunction", fake_isjunction, create=True):
            rc, out = self.run_inproc("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("verify=skipped reason=symlink", out)

    def _fresh_vault(self, *clone_args):
        v = os.path.join(self.root, "vault2")
        url = "file:///" + self.origin.replace("\\", "/").lstrip("/")
        self._git(self.root, "clone", "--quiet", *clone_args, url, v)
        return v

    def test_shallow_clone(self):
        v = self._fresh_vault("--depth", "1")
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        os.makedirs(os.path.join(v, os.path.dirname(NOTE)), exist_ok=True)
        with open(os.path.join(v, NOTE), "wb") as fh:
            fh.write(b)
        rc, out, _ = self.run_cli("--repo-dir", v, NOTE)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.summary(out)["status"], "ok")

    def test_single_branch_clone_and_detached_head(self):
        v = self._fresh_vault("--single-branch", "--branch", "main")
        self._git(v, "checkout", "--quiet", "--detach")
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        os.makedirs(os.path.join(v, os.path.dirname(NOTE)), exist_ok=True)
        with open(os.path.join(v, NOTE), "wb") as fh:
            fh.write(b)
        rc, out, _ = self.run_cli("--repo-dir", v, NOTE)
        self.assertEqual(rc, 0, out)

    def test_remote_with_another_name(self):
        self._git(self.vault, "remote", "rename", "origin", "upstream")
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.local(NOTE, b)
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("verify=unreadable reason=fetch_failed", out)
        rc, out, _ = self.run_cli("--repo-dir", self.vault, "--remote", "upstream", NOTE)
        self.assertEqual(rc, 0, out)

    def test_directory_on_main_is_missing_remote(self):
        self.push(NOTE + "/inner.md", b"x\n")  # main ではそのパスがディレクトリ
        self.local(NOTE, b"x\n")
        rc, out, _ = self.run_cli("--repo-dir", self.vault, NOTE)
        self.assertEqual(rc, 1)
        self.assertIn("WRITE_PATH: %s verify=missing_remote" % NOTE, out)

    def test_local_read_failure_is_reported_per_path(self):
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.push(NOTE2, b)
        self.local(NOTE, b)
        self.local(NOTE2, b)
        real_open = open

        def fake_open(path, *a, **k):
            if str(path).replace("\\", "/").endswith(NOTE):
                raise PermissionError("denied")
            return real_open(path, *a, **k)

        with unittest.mock.patch.object(verify_vault_push, "open", fake_open, create=True):
            rc, out = self.run_inproc("--repo-dir", self.vault, NOTE, NOTE2)
        self.assertEqual(rc, 1)
        self.assertIn("WRITE_PATH: %s verify=unreadable reason=local_read_failed" % NOTE, out)
        self.assertIn("WRITE_PATH: %s verify=match" % NOTE2, out)  # 残りのパスも照合する
        s = self.summary(out)
        self.assertEqual((s["status"], s["unreadable"], s["match"]), ("failed", "1", "1"))

    def test_cat_file_failure_is_reported_per_path(self):
        b = BODY.encode("utf-8")
        self.push(NOTE, b)
        self.push(NOTE2, b)
        self.local(NOTE, b)
        self.local(NOTE2, b)
        real_git = verify_vault_push._git

        def fake_git(top, args, timeout=verify_vault_push.GIT_TIMEOUT):
            if args[:2] == ["cat-file", "blob"] and args[2].endswith(NOTE):
                raise subprocess.TimeoutExpired(args, timeout)
            return real_git(top, args, timeout)

        with unittest.mock.patch.object(verify_vault_push, "_git", fake_git):
            rc, out = self.run_inproc("--repo-dir", self.vault, NOTE, NOTE2)
        self.assertEqual(rc, 1)
        self.assertIn("WRITE_PATH: %s verify=unreadable reason=cat_file_failed" % NOTE, out)
        self.assertIn("WRITE_PATH: %s verify=match" % NOTE2, out)
        self.assertEqual(self.summary(out)["status"], "failed")

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

    def test_step_8_5_completion_needs_status_ok_and_exit_0(self):
        # 「status=failed でも exit を見落として完了扱い」を手順書の側でも塞ぐ
        line = next((l for l in self.step.splitlines() if "完了と言えるのは" in l), "")
        for word in ("verify=match", "status=ok", "exit 0"):
            self.assertIn(word, line)
        self.assertIn("UNCHECKED", self.step)
        self.assertIn("GIVEUP", self.step)
        self.assertIn("今夜押したノート全部", self.step)
        # 照合できなかった件を残す書式・スクリプトが走らなかったときの扱い・最終報告
        self.assertIn("verify=UNCHECKED reason=<理由>", self.step)
        self.assertIn("verify=UNCHECKED reason=verify_not_run", self.step)
        self.assertIn("最終報告に、押したノートの数・照合した数（`total=`）・`GIVEUP` と `UNCHECKED` の件数を必ず書く", self.step)
        # 渡し漏れの検知: total と押した数の突き合わせ
        self.assertIn("`total=` が今夜 `push_files` で押したノートの数", line)
        self.assertIn("verify=UNCHECKED reason=not_listed", self.step)
        self.assertIn("実行するのは公開リポのディレクトリ", self.step)

    def test_step_8_5_no_longer_uses_cwd_relative_verify(self):
        self.assertNotIn("push_via_api.sh --verify-only katut-brain/obsidian-vault", self.step)


if __name__ == "__main__":
    unittest.main()
