"""push_via_api.sh の受け入れテスト。

本物の GitHub は叩かない。PATH の先頭に偽の `gh`（Python製）を置いて、
スクリプトが「送って・読み返して・SHA-256 が一致したときだけ成功と言う」ことを確かめる。

確かめたいのは、2026-09-08 にこのスクリプトを入れた理由そのもの:
  1. 中身が途中で変わったのに成功と報告する経路が無いこと
  2. 大きいファイルでも壊れないこと（argv に本文を置くと約98KB で Argument list too long
     になり、122KB の index.html が壊れたのと同じサイズの崖ができる）
  3. 別経路（push_files フォールバック）で押したものも、同じ SHA-256 照合に掛けられること
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SCRIPT = REPO_ROOT / "push_via_api.sh"

# Windows のローカル実行でも git-bash があれば動く。無ければスキップする。
BASH = shutil.which("bash")
HAS_TOOLS = bool(BASH) and all(shutil.which(c) for c in ("sha256sum", "mktemp"))

FAKE_GH = textwrap.dedent(
    '''\
    import base64, json, os, sys

    BEHAVIOR = os.environ["FAKE_GH_BEHAVIOR"]
    STORE = os.environ["FAKE_GH_STORE"]
    argv = sys.argv[1:]

    def contract(cond, msg):
        """本物の GitHub API が受け付けない呼び方をしたら、その場で落とす。

        偽 gh が寛容だと「テストは緑だが本番だけ落ちる」型を見逃す（2026-09-08 の
        レビュー指摘 P1）。ここは本物の Contents API の契約を模して厳しくする。
        """
        if not cond:
            sys.stderr.write("FAKE_GH: contract violation: " + msg + "\\n")
            sys.exit(91)

    method = "GET"
    endpoint = ""
    body_file = None
    want_sha = False
    accept = None
    for i, a in enumerate(argv):
        if a == "-X":
            method = argv[i + 1]
        elif a == "--input":
            body_file = argv[i + 1]
        elif a == "-H":
            accept = argv[i + 1]
        elif a == ".sha":
            want_sha = True
        elif a.startswith("repos/"):
            endpoint = a
        elif a.startswith("content=") or a.startswith("-f") and "content=" in a:
            # 本文が argv に乗っていたら、それ自体が回帰。即座に落とす。
            sys.stderr.write("FAKE_GH: content must not be passed on argv\\n")
            sys.exit(90)

    contract("/contents/" in endpoint, "endpoint must address the contents API")
    rel, _, query = endpoint.split("/contents/", 1)[1].partition("?")
    target = os.path.join(STORE, rel)

    if method == "PUT":
        contract(body_file is not None, "PUT must send its body with --input")
        contract(query == "", "PUT takes branch in the body, not as ?ref=")
        if BEHAVIOR == "put_forbidden":
            sys.stderr.write("gh: Resource not accessible by integration (HTTP 403)\\n")
            sys.exit(1)
        payload = json.load(open(body_file, encoding="utf-8"))
        contract(set(payload) <= {"message", "branch", "content", "sha"},
                 "unexpected fields: " + repr(sorted(payload)))
        contract(payload.get("message"), "message is required")
        contract(payload.get("branch") == "main", "branch must be main, got " + repr(payload.get("branch")))
        contract("content" in payload, "content is required")
        # 本物の Contents API は、既存ファイルの更新に blob sha を要求し、
        # 新規作成では sha を受け付けない。
        exists = os.path.exists(target)
        contract(bool(payload.get("sha")) == exists,
                 "sha must be sent iff the file already exists (exists=%r, sha=%r)"
                 % (exists, payload.get("sha")))
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(base64.b64decode(payload["content"]))
        sys.stdout.write(json.dumps({"commit": {"sha": "deadbeef"}}))
        sys.exit(0)

    contract(query == "ref=main", "GET must pin the ref, got " + repr(query))
    if BEHAVIOR == "get_forbidden" or not os.path.exists(target):
        sys.stderr.write("gh: not found (HTTP 404)\\n")
        sys.exit(1)

    data = open(target, "rb").read()
    if want_sha:
        # blob sha を訊く GET は raw を要求してはいけない。
        contract(accept is None, "the metadata GET must not ask for raw")
        sys.stdout.write(hashlib.sha1(b"blob %d\\0" % len(data) + data).hexdigest())
        sys.exit(0)
    contract(accept == "Accept: application/vnd.github.raw",
             "the content GET must ask for raw, got " + repr(accept))
    if BEHAVIOR == "corrupt":
        # 全角括弧を半角に化けさせる。2026-09-04 に実際に起きた化け方。
        data = data.replace("（".encode(), b"(").replace("）".encode(), b")")
    sys.stdout.buffer.write(data)
    sys.exit(0)
    '''
)


@unittest.skipUnless(HAS_TOOLS, "bash / coreutils が無い環境ではスキップ")
class PushViaApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work = Path(self.tmp) / "work"
        self.work.mkdir()
        self.bin = Path(self.tmp) / "bin"
        self.bin.mkdir()
        self.store = Path(self.tmp) / "store"

        gh_py = self.bin / "fake_gh.py"
        gh_py.write_text("import hashlib\n" + FAKE_GH, encoding="utf-8")
        gh = self.bin / "gh"
        gh.write_text(f'#!/usr/bin/env sh\nexec "{sys.executable}" "{gh_py}" "$@"\n', encoding="utf-8")
        gh.chmod(0o755)

        # 実害と同じ形の本文（全角括弧を含む）
        self.rel = "reviews/2026-09-03.html"
        self.src = self.work / self.rel
        self.src.parent.mkdir(parents=True, exist_ok=True)
        self.src.write_text(
            "<!doctype html>\n<p>ハーネス（活性化・出典照合）の話</p>\n</html>\n",
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sha(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _env(self, behavior):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}" + env["PATH"]
        env["FAKE_GH_STORE"] = str(self.store)
        env["FAKE_GH_BEHAVIOR"] = behavior
        env["PUSH_VIA_API_PYTHON"] = sys.executable
        return env

    def _run(self, behavior, *args):
        return subprocess.run(
            [BASH, str(SCRIPT), *args],
            cwd=self.work,
            env=self._env(behavior),
            capture_output=True,
            text=True,
            # Windows のローカル実行では既定が cp932 になり、日本語を含むパスが
            # 出力に混じった瞬間にデコードで落ちる（テスト側の事故であって
            # スクリプトの不具合ではない）。CI の Ubuntu と揃えて UTF-8 で読む。
            encoding="utf-8",
            errors="replace",
        )

    def _push(self, behavior, *paths):
        return self._run(behavior, "katut-brain/x", "update: test", *(paths or (self.rel,)))

    # --- 主経路 -------------------------------------------------------------

    def test_normal_reports_match_and_exits_zero(self):
        r = self._push("normal")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verify=match", r.stdout)
        stored = self.store / self.rel
        self.assertEqual(self._sha(stored), self._sha(self.src))

    def test_corruption_is_detected_not_reported_as_success(self):
        """本番で起きた「全角括弧が半角に化ける」を、照合が必ず捕まえること。"""
        r = self._push("corrupt")
        self.assertNotEqual(r.returncode, 0, "化けているのに成功で返してはいけない")
        self.assertIn("verify=MISMATCH", r.stdout)
        self.assertNotIn("verify=match", r.stdout)

    def test_put_forbidden_is_a_failure_the_caller_can_see(self):
        r = self._push("put_forbidden")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("api=failed", r.stdout)
        self.assertIn("403", r.stdout)

    def test_unreadable_after_put_is_not_success(self):
        """送れても読み返せなければ成功と言わない（照合できていないため）。"""
        r = self._push("get_forbidden")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("verify=unreadable", r.stdout)

    def test_missing_local_file_is_reported(self):
        r = self._push("normal", "reviews/9999-01-01.html")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("local_file_missing", r.stdout)

    def test_usage_error_exits_two(self):
        r = self._run("normal", "katut-brain/x")
        self.assertEqual(r.returncode, 2)

    def test_updating_an_existing_file_sends_the_blob_sha(self):
        """2度目の送信は「更新」になる。blob sha を付けないと本物のAPIは422で落とす。

        偽 gh 側で「sha は既存のときだけ・新規では付けない」を契約として強制している
        ので、実装がこれを外したらこのテストが落ちる（2026-09-08 レビュー指摘 P1）。
        """
        first = self._push("normal")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        self.src.write_text(
            "<!doctype html>\n<p>二日目の本文（追記）</p>\n</html>\n", encoding="utf-8"
        )
        second = self._push("normal")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("verify=match", second.stdout)
        self.assertEqual(self._sha(self.store / self.rel), self._sha(self.src))

    def test_path_with_spaces_survives_quoting(self):
        odd = self.work / "reviews/2026-09-03 (再送).html"
        odd.write_text("<!doctype html>\n<p>括弧（と空白）</p>\n</html>\n", encoding="utf-8")
        r = self._push("normal", "reviews/2026-09-03 (再送).html")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verify=match", r.stdout)

    # --- サイズの崖（2026-09-08 レビュー指摘 P0） ---------------------------

    def test_large_file_does_not_hit_the_argv_limit(self):
        """本文を argv に置くと約98KB で `Argument list too long` になる。

        122KB の index.html が壊れたのと同じサイズの崖なので、その帯域を実際に通す。
        偽 gh 側でも `content=` が argv に現れたら exit 90 で落とすようにしてある。
        """
        big = self.work / "reviews/2026-06-14.html"
        body = "<p>括弧（テスト）と長い本文。</p>\n" * 6000  # 約 180KB
        big.write_text("<!doctype html>\n" + body + "</html>\n", encoding="utf-8")
        self.assertGreater(big.stat().st_size, 150_000)

        r = self._push("normal", "reviews/2026-06-14.html")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verify=match", r.stdout)
        self.assertEqual(self._sha(self.store / "reviews/2026-06-14.html"), self._sha(big))

    # --- 照合専用モード（フォールバックした夜のため） -----------------------

    def test_verify_only_confirms_a_file_pushed_by_another_route(self):
        """push_files で押した夜も、同じ SHA-256 照合に掛けられること。"""
        pushed = self.store / self.rel
        pushed.parent.mkdir(parents=True, exist_ok=True)
        pushed.write_bytes(self.src.read_bytes())

        r = self._run("normal", "--verify-only", "katut-brain/x", self.rel)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verify=match", r.stdout)

    def test_verify_only_catches_the_2026_09_04_corruption(self):
        pushed = self.store / self.rel
        pushed.parent.mkdir(parents=True, exist_ok=True)
        pushed.write_bytes(self.src.read_bytes())

        # corrupt は GET のときだけ化けさせる＝別経路で押した中身が化けていた状況
        r = self._run("corrupt", "--verify-only", "katut-brain/x", self.rel)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("verify=MISMATCH", r.stdout)

    def test_verify_only_usage_error(self):
        r = self._run("normal", "--verify-only", "katut-brain/x")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
