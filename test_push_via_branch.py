"""push_via_branch.sh の受け入れテスト。

本物の GitHub は叩かない。一時ディレクトリに bare リポ（=origin）と作業クローンを作り、
スクリプトを実際に走らせて次を確かめる:
  - 送ったファイルと .publish/manifest.json だけを載せた1コミットが claude/publish-* に届く
  - main は動かない。作業ツリーの未コミット変更（captures.json 等）を巻き込まない
  - 許可されていないパス・存在しないファイルは exit 2 で何も送らない
  - manifest の sha256 / blob が実ファイルと一致する
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "push_via_branch.sh")

HTML = "<!doctype html>\n<html><body><p>全角（括弧）「鉤括弧」——🌱\ttab \\ \"q\"</p>" + ("x" * 3000) + "</body></html>\n"


def run(cmd, cwd, env=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"{cmd} failed: {r.stdout}\n{r.stderr}")
    return r


class PushViaBranchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self.tmp, "remote.git")
        self.work = os.path.join(self.tmp, "work")
        run(["git", "init", "-q", "--bare", "-b", "main", self.remote], self.tmp)
        run(["git", "clone", "-q", self.remote, self.work], self.tmp)
        for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
            run(["git", "config", k, v], self.work)
        os.makedirs(os.path.join(self.work, "reviews"))
        with open(os.path.join(self.work, "captures.json"), "w") as f:
            f.write("[]")
        with open(os.path.join(self.work, "reviews", "2026-09-01.html"), "w") as f:
            f.write("<!doctype html><html></html>")
        run(["git", "add", "-A"], self.work)
        run(["git", "commit", "-q", "-m", "init"], self.work)
        run(["git", "push", "-q", "origin", "HEAD:main"], self.work)
        self.main_before = run(["git", "rev-parse", "HEAD"], self.work).stdout.strip()
        # Routine と同じ detached HEAD にしておく
        run(["git", "checkout", "-q", "--detach"], self.work)
        self.env = dict(os.environ, PUSH_VIA_BRANCH_WAIT="0")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.work, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return p

    def script(self, *args):
        return run(["bash", SCRIPT, *args], self.work, env=self.env, check=False)

    def remote_branches(self):
        out = run(["git", "ls-remote", self.remote, "refs/heads/claude/*"], self.tmp).stdout
        return [line.split("\t")[1] for line in out.splitlines() if line]

    def test_pushes_only_given_files_to_new_branch(self):
        self.write("reviews/2026-09-13.html", HTML)
        self.write("fetch_facts/2026-09-13.json", json.dumps({"a": "（）"}, ensure_ascii=False))
        self.write("captures.json", "[1,2,3]")  # 未コミット変更。送ってはいけない
        r = self.script("update: 2026-09-13", "fetch_facts/2026-09-13.json", "reviews/2026-09-13.html")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WRITE_COMMIT: ", r.stdout)
        self.assertIn("PUBLISHED: pending", r.stdout)

        branches = self.remote_branches()
        self.assertEqual(len(branches), 1)
        ref = branches[0]
        self.assertTrue(ref.startswith("refs/heads/claude/publish-"))

        # main は動いていない
        main_now = run(["git", "rev-parse", "refs/heads/main"], self.remote).stdout.strip()
        self.assertEqual(main_now, self.main_before)

        # 1コミット・変更は3ファイルだけ
        diff = run(["git", "diff", "--name-only", f"{self.main_before}..{ref}"], self.remote).stdout.split()
        self.assertEqual(sorted(diff), [".publish/manifest.json", "fetch_facts/2026-09-13.json", "reviews/2026-09-13.html"])
        count = run(["git", "rev-list", "--count", f"{self.main_before}..{ref}"], self.remote).stdout.strip()
        self.assertEqual(count, "1")

        # 届いたバイト列が原本と同一、manifest も一致
        manifest = json.loads(run(["git", "show", f"{ref}:.publish/manifest.json"], self.remote).stdout)
        self.assertEqual(manifest["base"], self.main_before)
        self.assertEqual(manifest["message"], "update: 2026-09-13")
        for f in manifest["files"]:
            local = open(os.path.join(self.work, f["path"]), "rb").read()
            remote = subprocess.run(["git", "show", f"{ref}:{f['path']}"], cwd=self.remote, capture_output=True).stdout
            self.assertEqual(local, remote)
            self.assertEqual(hashlib.sha256(local).hexdigest(), f["sha256"])
            self.assertEqual(len(local), f["bytes"])

        # 作業ツリーの未コミット変更は残ったまま
        self.assertEqual(open(os.path.join(self.work, "captures.json")).read(), "[1,2,3]")

    def test_rejects_disallowed_path(self):
        self.write("index.html", HTML)
        r = self.script("bad", "index.html")
        self.assertEqual(r.returncode, 2)
        self.assertIn("reason=path_not_allowed", r.stdout)
        self.assertEqual(self.remote_branches(), [])

    def test_rejects_missing_file(self):
        r = self.script("bad", "reviews/2026-09-13.html")
        self.assertEqual(r.returncode, 2)
        self.assertIn("reason=file_missing", r.stdout)
        self.assertEqual(self.remote_branches(), [])

    def test_reports_published_when_main_has_same_bytes(self):
        # Actions が取り込んだ後の状態を模す: main に同一内容を先に載せておく
        self.write("capture_index.json", '{"days": {}}\n')
        run(["git", "add", "capture_index.json"], self.work)
        run(["git", "commit", "-q", "-m", "pre"], self.work)
        run(["git", "push", "-q", "origin", "HEAD:main"], self.work)
        env = dict(self.env, PUSH_VIA_BRANCH_WAIT="15")
        r = run(["bash", SCRIPT, "index: x", "capture_index.json"], self.work, env=env, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PUBLISHED: yes", r.stdout)


if __name__ == "__main__":
    unittest.main()
