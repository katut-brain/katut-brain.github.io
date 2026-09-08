"""push_via_api.sh の受け入れテスト。

本物の GitHub は叩かない。PATH の先頭に偽の `gh`（Python製・Git Data API を模した
小さな実装）を置いて、スクリプトの約束が本当に守られているかを確かめる。

確かめたいのは、2026-09-08 にこのスクリプトを入れた理由そのもの:

  1. 中身が途中で変わったのに成功と報告する経路が無いこと
  2. **照合が通らなかった夜、main が一切動かないこと**（敵対的レビュー3周目の指摘。
     Contents API で先に PUT する設計だと、照合が失敗しても未検証の中身が main に残る）
  3. 大きいファイルでも壊れないこと（argv に本文を置くと約98KB で
     Argument list too long になり、122KB の index.html が壊れたのと同じ崖ができる）
  4. 別経路（push_files フォールバック）で押したものも、同じ SHA-256 照合に掛けられること

偽 `gh` は寛容にしない。本物の API が受け付けない呼び方をしたら exit 91 で落とす。
偽実装が甘いと「テストは緑だが本番だけ落ちる」型を見逃すため。
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SCRIPT = REPO_ROOT / "push_via_api.sh"

# Windows のローカル実行でも git-bash があれば動く。無ければスキップする。
BASH = shutil.which("bash")
HAS_TOOLS = bool(BASH) and all(shutil.which(c) for c in ("sha256sum", "mktemp"))

FAKE_GH = r'''
import base64, hashlib, json, os, sys

BEHAVIOR = os.environ["FAKE_GH_BEHAVIOR"]
STORE = os.environ["FAKE_GH_STORE"]
argv = sys.argv[1:]


def die(msg, code=91):
    sys.stderr.write("FAKE_GH: " + msg + "\n")
    sys.exit(code)


def contract(cond, msg):
    """本物の GitHub API が受け付けない呼び方をしたら、その場で落とす。"""
    if not cond:
        die("contract violation: " + msg)


def obj_path(kind, sha):
    return os.path.join(STORE, kind, sha)


def put_obj(kind, data):
    sha = hashlib.sha1(kind.encode() + b"\0" + data).hexdigest()
    d = os.path.join(STORE, kind)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sha), "wb") as fh:
        fh.write(data)
    return sha


def get_obj(kind, sha):
    with open(obj_path(kind, sha), "rb") as fh:
        return fh.read()


REF = os.path.join(STORE, "ref")


def read_ref():
    with open(REF) as fh:
        return fh.read().strip()


def write_ref(sha):
    with open(REF, "w") as fh:
        fh.write(sha)


# --- 引数の解析 -------------------------------------------------------------
method = "GET"
endpoint = ""
body_file = None
jq = None
accept = None
i = 0
while i < len(argv):
    a = argv[i]
    if a == "-X":
        method = argv[i + 1]; i += 2; continue
    if a == "--input":
        body_file = argv[i + 1]; i += 2; continue
    if a == "--jq":
        jq = argv[i + 1]; i += 2; continue
    if a == "-H":
        accept = argv[i + 1]; i += 2; continue
    if a.startswith("content=") or (a == "-f" and i + 1 < len(argv) and argv[i + 1].startswith("content=")):
        die("content must not be passed on argv", 90)
    if a.startswith("repos/"):
        endpoint = a
    i += 1

contract(endpoint.startswith("repos/"), "endpoint must start with repos/")
rest, _, query = endpoint.partition("?")
parts = rest.split("/")
tail = "/".join(parts[3:])  # repos/<owner>/<repo>/<tail>

body = None
if body_file is not None:
    with open(body_file, encoding="utf-8") as fh:
        body = json.load(fh)


def emit(payload):
    """--jq が来ていればそのスカラだけ返す（本物の gh の挙動に合わせる）。"""
    if jq is None:
        sys.stdout.write(json.dumps(payload))
        return
    cur = payload
    for key in jq.lstrip(".").split("."):
        cur = cur[key]
    sys.stdout.write(str(cur))


# --- ref の初期化 -----------------------------------------------------------
if not os.path.exists(REF):
    os.makedirs(STORE, exist_ok=True)
    empty_tree = put_obj("trees", json.dumps({}).encode())
    first = put_obj("commits", json.dumps({"tree": empty_tree, "parents": []}).encode())
    write_ref(first)


# --- Git Data API -----------------------------------------------------------
if tail == "git/ref/heads/main":
    contract(method == "GET", "reading a ref must be a GET")
    emit({"object": {"sha": read_ref()}})
    sys.exit(0)

if tail.startswith("git/commits/") and method == "GET":
    sha = tail.split("/")[-1]
    commit = json.loads(get_obj("commits", sha))
    emit({"tree": {"sha": commit["tree"]}, "parents": commit["parents"]})
    sys.exit(0)

if tail == "git/blobs" and method == "POST":
    contract(body_file is not None, "creating a blob must send its body with --input")
    contract(set(body) == {"content", "encoding"}, "blob body fields: " + repr(sorted(body)))
    contract(body["encoding"] == "base64", "blob encoding must be base64")
    if BEHAVIOR == "put_forbidden":
        sys.stderr.write("gh: Resource not accessible by integration (HTTP 403)\n")
        sys.exit(1)
    emit({"sha": put_obj("blobs", base64.b64decode(body["content"]))})
    sys.exit(0)

if tail.startswith("git/blobs/") and method == "GET":
    if BEHAVIOR == "get_forbidden":
        sys.stderr.write("gh: not found (HTTP 404)\n")
        sys.exit(1)
    contract(accept == "Accept: application/vnd.github.raw",
             "reading a blob's bytes must ask for raw, got " + repr(accept))
    data = get_obj("blobs", tail.split("/")[-1])
    if BEHAVIOR == "corrupt":
        # 全角括弧を半角に化けさせる。2026-09-04 に実際に起きた化け方。
        data = data.replace("（".encode(), b"(").replace("）".encode(), b")")
    sys.stdout.buffer.write(data)
    sys.exit(0)

if tail == "git/trees" and method == "POST":
    contract(body_file is not None, "creating a tree must send its body with --input")
    contract(set(body) == {"base_tree", "tree"}, "tree body fields: " + repr(sorted(body)))
    tree = json.loads(get_obj("trees", body["base_tree"]))
    for entry in body["tree"]:
        contract(set(entry) == {"path", "mode", "type", "sha"}, "tree entry fields: " + repr(sorted(entry)))
        contract(entry["mode"] == "100644", "tree entry mode must be 100644")
        contract(entry["type"] == "blob", "tree entry type must be blob")
        tree[entry["path"]] = entry["sha"]
    emit({"sha": put_obj("trees", json.dumps(tree, sort_keys=True).encode())})
    sys.exit(0)

if tail == "git/commits" and method == "POST":
    contract(body_file is not None, "creating a commit must send its body with --input")
    contract(set(body) == {"message", "tree", "parents"}, "commit body fields: " + repr(sorted(body)))
    contract(body["message"], "commit message is required")
    contract(len(body["parents"]) == 1, "expected exactly one parent")
    emit({"sha": put_obj("commits", json.dumps(
        {"tree": body["tree"], "parents": body["parents"]}, sort_keys=True).encode())})
    sys.exit(0)

if tail == "git/refs/heads/main" and method == "PATCH":
    contract(body_file is not None, "moving a ref must send its body with --input")
    contract(set(body) == {"sha", "force"}, "ref body fields: " + repr(sorted(body)))
    contract(body["force"] is False, "this pipeline must never force-update main")
    commit = json.loads(get_obj("commits", body["sha"]))
    if commit["parents"][0] != read_ref():
        # 早送りできない＝ラン中に main が動いた
        sys.stderr.write("gh: Update is not a fast forward (HTTP 422)\n")
        sys.exit(1)
    write_ref(body["sha"])
    emit({"object": {"sha": body["sha"]}})
    sys.exit(0)

# --- Contents API（--verify-only の読み出しだけに使う） ---------------------
if tail.startswith("contents/") and method == "GET":
    contract(query == "ref=main", "the contents GET must pin the ref, got " + repr(query))
    contract(accept == "Accept: application/vnd.github.raw",
             "the contents GET must ask for raw, got " + repr(accept))
    if BEHAVIOR == "get_forbidden":
        sys.stderr.write("gh: not found (HTTP 404)\n")
        sys.exit(1)
    path = tail[len("contents/"):]
    tree = json.loads(get_obj("commits", read_ref()))
    tree = json.loads(get_obj("trees", tree["tree"]))
    if path not in tree:
        sys.stderr.write("gh: not found (HTTP 404)\n")
        sys.exit(1)
    data = get_obj("blobs", tree[path])
    if BEHAVIOR == "corrupt":
        data = data.replace("（".encode(), b"(").replace("）".encode(), b")")
    sys.stdout.buffer.write(data)
    sys.exit(0)

die("unhandled endpoint: " + method + " " + endpoint)
'''


@unittest.skipUnless(HAS_TOOLS, "bash / coreutils が無い環境ではスキップ")
class PushViaApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work = Path(self.tmp) / "work"
        self.work.mkdir()
        self.bin = Path(self.tmp) / "bin"
        self.bin.mkdir()
        self.store = Path(self.tmp) / "store"
        self.store.mkdir()

        gh_py = self.bin / "fake_gh.py"
        gh_py.write_text(FAKE_GH, encoding="utf-8")
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

    # --- ヘルパ -------------------------------------------------------------

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

    def _head(self):
        """偽リポジトリの main の先端 commit sha。"""
        return (self.store / "ref").read_text().strip() if (self.store / "ref").exists() else None

    def _on_main(self, path):
        """main から見えるファイルの中身。無ければ None。"""
        ref = self._head()
        if ref is None:
            return None
        import json
        commit = json.loads((self.store / "commits" / ref).read_bytes())
        tree = json.loads((self.store / "trees" / commit["tree"]).read_bytes())
        if path not in tree:
            return None
        return (self.store / "blobs" / tree[path]).read_bytes()

    def _prime(self):
        """偽リポジトリを初期化して、その時点の main の先端を返す。"""
        self._run("normal", "--verify-only", "katut-brain/x", self.rel)
        return self._head()

    # --- 主経路 -------------------------------------------------------------

    def test_normal_reports_match_and_moves_main(self):
        r = self._push("normal")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verify=match", r.stdout)
        self.assertIn("WRITE_COMMIT: ", r.stdout)
        self.assertNotIn("WRITE_COMMIT: none", r.stdout)
        self.assertEqual(self._on_main(self.rel), self.src.read_bytes())

    def test_two_files_land_in_one_commit(self):
        """Contents API 方式では2コミットに割れて Actions が二重発火していた。"""
        other = self.work / "fetch_facts/2026-09-03.json"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text('{"ok": true}\n', encoding="utf-8")

        before = self._prime()
        r = self._push("normal", "fetch_facts/2026-09-03.json", self.rel)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("files=2", r.stdout)

        import json
        head = self._head()
        commit = json.loads((self.store / "commits" / head).read_bytes())
        self.assertEqual(commit["parents"], [before], "1コミットで両方が載るはず")
        self.assertEqual(self._on_main(self.rel), self.src.read_bytes())
        self.assertEqual(self._on_main("fetch_facts/2026-09-03.json"), other.read_bytes())

    # --- 「照合が通らなければ main は動かない」（レビュー3周目の本題） -------

    def test_corruption_is_detected_and_main_does_not_move(self):
        """本番で起きた「全角括弧が半角に化ける」を捕まえ、かつ main を汚さないこと。"""
        before = self._prime()
        r = self._push("corrupt")
        self.assertNotEqual(r.returncode, 0, "化けているのに成功で返してはいけない")
        self.assertIn("verify=MISMATCH", r.stdout)
        self.assertIn("main_untouched", r.stdout)
        self.assertEqual(self._head(), before, "照合に落ちたのに main が動いている")
        self.assertIsNone(self._on_main(self.rel))

    def test_unreadable_readback_does_not_move_main(self):
        """読み返せなければ、押せていても main には載せない。"""
        before = self._prime()
        r = self._push("get_forbidden")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("verify=unreadable", r.stdout)
        self.assertEqual(self._head(), before)
        self.assertIsNone(self._on_main(self.rel))

    def test_blob_creation_forbidden_leaves_main_alone(self):
        before = self._prime()
        r = self._push("put_forbidden")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("api=failed", r.stdout)
        self.assertIn("403", r.stdout)
        self.assertIn("WRITE_COMMIT: none", r.stdout)
        self.assertEqual(self._head(), before)

    def test_concurrent_move_of_main_is_not_forced_over(self):
        """ラン中に main が動いたら、早送りできないので諦める（上書きしない）。"""
        # 1回目を成功させたあと、その時点の commit を親とする2回目を作る途中で
        # main を別の commit へ進める、という状況を模す。
        self._push("normal")
        moved_from = self._head()

        # 別の誰かが main を進めた
        import json
        extra_blob = hashlib.sha1(b"blobs\0other").hexdigest()
        (self.store / "blobs").mkdir(exist_ok=True)
        (self.store / "blobs" / extra_blob).write_bytes(b"other")
        commit = json.loads((self.store / "commits" / moved_from).read_bytes())
        tree = json.loads((self.store / "trees" / commit["tree"]).read_bytes())
        tree["other.txt"] = extra_blob
        new_tree = hashlib.sha1(b"trees\0" + json.dumps(tree, sort_keys=True).encode()).hexdigest()
        (self.store / "trees" / new_tree).write_bytes(json.dumps(tree, sort_keys=True).encode())
        payload = json.dumps({"tree": new_tree, "parents": [moved_from]}, sort_keys=True).encode()
        new_commit = hashlib.sha1(b"commits\0" + payload).hexdigest()
        (self.store / "commits" / new_commit).write_bytes(payload)

        # スクリプトが親を読んだ後で main が動く状況を、事前に動かして近似する
        # （PATCH の親チェックが働くことを確かめるのが目的）
        self.src.write_text("<!doctype html>\n<p>二日目（追記）</p>\n</html>\n", encoding="utf-8")
        r = self._push("normal")
        self.assertEqual(r.returncode, 0, "通常は早送りできるので成功する: " + r.stdout + r.stderr)

        # 親が現在の先端でない commit へ ref を進めようとすると 422 になることを直接確認
        stale = self.store / "stale.json"
        stale.write_text(json.dumps({"sha": new_commit, "force": False}), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(self.bin / "fake_gh.py"), "api", "-X", "PATCH",
             "repos/katut-brain/x/git/refs/heads/main", "--input", str(stale)],
            env=self._env("normal"), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("fast forward", proc.stderr)

    # --- 引数・入力 ---------------------------------------------------------

    def test_missing_local_file_is_reported_without_touching_main(self):
        before = self._prime()
        r = self._push("normal", "reviews/9999-01-01.html")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("local_file_missing", r.stdout)
        self.assertEqual(self._head(), before)

    def test_usage_error_exits_two(self):
        r = self._run("normal", "katut-brain/x")
        self.assertEqual(r.returncode, 2)

    def test_verify_only_usage_error(self):
        r = self._run("normal", "--verify-only", "katut-brain/x")
        self.assertEqual(r.returncode, 2)

    def test_updating_an_existing_file_keeps_the_rest_of_the_tree(self):
        """base_tree を土台にしないと、他のファイルが消えたコミットができる。"""
        other = self.work / "fetch_facts/2026-09-02.json"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text('{"day": 1}\n', encoding="utf-8")
        self._push("normal", "fetch_facts/2026-09-02.json")

        self.src.write_text("<!doctype html>\n<p>翌日（追記）</p>\n</html>\n", encoding="utf-8")
        r = self._push("normal")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._on_main("fetch_facts/2026-09-02.json"), other.read_bytes(),
                         "前の日のファイルが消えている＝base_tree を使っていない")
        self.assertEqual(self._on_main(self.rel), self.src.read_bytes())

    def test_path_with_spaces_survives_quoting(self):
        odd = self.work / "reviews/2026-09-03 (再送).html"
        odd.write_text("<!doctype html>\n<p>括弧（と空白）</p>\n</html>\n", encoding="utf-8")
        r = self._push("normal", "reviews/2026-09-03 (再送).html")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verify=match", r.stdout)

    # --- サイズの崖（2026-09-08 レビュー1周目の指摘 P0） --------------------

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
        self.assertEqual(self._on_main("reviews/2026-06-14.html"), big.read_bytes())

    # --- 照合専用モード（フォールバックした夜のため） -----------------------

    def test_verify_only_confirms_a_file_pushed_by_another_route(self):
        """push_files で押した夜も、同じ SHA-256 照合に掛けられること。"""
        self._push("normal")
        r = self._run("normal", "--verify-only", "katut-brain/x", self.rel)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verify=match", r.stdout)

    def test_verify_only_catches_the_2026_09_04_corruption(self):
        self._push("normal")
        r = self._run("corrupt", "--verify-only", "katut-brain/x", self.rel)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("verify=MISMATCH", r.stdout)

    def test_verify_only_reports_an_unreadable_channel(self):
        """フォールバックの可否判定に使う。読めないなら押さない、を支える。"""
        self._push("normal")
        r = self._run("get_forbidden", "--verify-only", "katut-brain/x", self.rel)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("verify=unreadable", r.stdout)


if __name__ == "__main__":
    unittest.main()
