"""push_via_api.sh の受け入れテスト。

本物の GitHub は叩かない。PATH の先頭に偽の `gh` を置いて、
スクリプトが「送って・読み返して・SHA-256 が一致したときだけ成功と言う」ことを確かめる。

特に確かめたいのは、2026-09-08 にこのスクリプトを入れた理由そのもの——
**中身が途中で変わったのに成功と報告してしまう**経路が無いこと。
"""

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SCRIPT = REPO_ROOT / "push_via_api.sh"

# Windows のローカル実行でも git-bash があれば動く。無ければスキップする。
BASH = shutil.which("bash")
HAS_TOOLS = bool(BASH) and all(shutil.which(c) for c in ("base64", "sha256sum", "mktemp"))


def _fake_gh(behavior: str) -> str:
    """偽の `gh` の中身を返す。

    behavior:
      normal        … PUT を受けて保存し、GET でそのまま返す（正常系）
      corrupt       … PUT は受けるが、GET では1文字だけ変えて返す（今回直したい事故）
      put_forbidden … PUT が 403 で落ちる（Vaultリポが到達不能な場合）
      get_forbidden … PUT は通るが GET が落ちる（照合できない場合）
    """
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        # 偽の gh。$STORE 配下にファイルを置くだけ。
        BEHAVIOR="{behavior}"
        STORE="$FAKE_GH_STORE"
        mkdir -p "$STORE"

        # 引数から path とサブコマンドを取り出す
        method="GET"
        endpoint=""
        content=""
        for ((i=1; i<=$#; i++)); do
          a="${{!i}}"
          case "$a" in
            -X) j=$((i+1)); method="${{!j}}" ;;
            repos/*) endpoint="$a" ;;
            content=*) content="${{a#content=}}" ;;
          esac
        done

        # endpoint = repos/<owner>/<repo>/contents/<path>[?ref=...]
        rel="${{endpoint#repos/}}"
        rel="${{rel#*/}}"
        rel="${{rel#*/}}"
        rel="${{rel#contents/}}"
        rel="${{rel%%\\?*}}"
        target="$STORE/$rel"

        if [ "$method" = "PUT" ]; then
          if [ "$BEHAVIOR" = "put_forbidden" ]; then
            echo "gh: Resource not accessible by integration (HTTP 403)" >&2
            exit 1
          fi
          mkdir -p "$(dirname "$target")"
          printf '%s' "$content" | base64 -d > "$target"
          echo '{{"commit":{{"sha":"deadbeef"}}}}'
          exit 0
        fi

        # GET
        if [ "$BEHAVIOR" = "get_forbidden" ]; then
          echo "gh: not found (HTTP 404)" >&2
          exit 1
        fi
        if [ ! -f "$target" ]; then
          echo "gh: not found (HTTP 404)" >&2
          exit 1
        fi
        # sha を訊かれている場合（--jq .sha）
        for a in "$@"; do
          if [ "$a" = ".sha" ]; then
            printf '%s' "$(sha256sum "$target" | cut -c1-40)"
            exit 0
          fi
        done
        if [ "$BEHAVIOR" = "corrupt" ]; then
          # 全角括弧を半角に化けさせる。2026-09-04 に実際に起きた化け方。
          sed 's/（/(/g; s/）/)/g' "$target"
        else
          cat "$target"
        fi
        exit 0
        """
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

        # 実害と同じ形の本文（全角括弧を含む）
        self.rel = "reviews/2026-09-03.html"
        src = self.work / self.rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(
            "<!doctype html>\n<p>ハーネス（活性化・出典照合）の話</p>\n</html>\n",
            encoding="utf-8",
        )
        self.local_sha = hashlib.sha256(src.read_bytes()).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, behavior):
        gh = self.bin / "gh"
        gh.write_text(_fake_gh(behavior), encoding="utf-8")
        gh.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}" + env["PATH"]
        env["FAKE_GH_STORE"] = str(self.store)
        return subprocess.run(
            [BASH, str(SCRIPT), "katut-brain/x", "update: test", self.rel],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_normal_reports_match_and_exits_zero(self):
        r = self._run("normal")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verify=match", r.stdout)
        stored = (self.store / self.rel).read_bytes()
        self.assertEqual(hashlib.sha256(stored).hexdigest(), self.local_sha)

    def test_corruption_is_detected_not_reported_as_success(self):
        """本番で起きた「全角括弧が半角に化ける」を、照合が必ず捕まえること。"""
        r = self._run("corrupt")
        self.assertNotEqual(r.returncode, 0, "化けているのに成功で返してはいけない")
        self.assertIn("verify=MISMATCH", r.stdout)
        self.assertNotIn("verify=match", r.stdout)

    def test_put_forbidden_is_a_failure_the_caller_can_see(self):
        r = self._run("put_forbidden")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("api=failed", r.stdout)
        self.assertIn("403", r.stdout)

    def test_unreadable_after_put_is_not_success(self):
        """送れても読み返せなければ成功と言わない（照合できていないため）。"""
        r = self._run("get_forbidden")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("verify=unreadable", r.stdout)

    def test_missing_local_file_is_reported(self):
        gh = self.bin / "gh"
        gh.write_text(_fake_gh("normal"), encoding="utf-8")
        gh.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}" + env["PATH"]
        env["FAKE_GH_STORE"] = str(self.store)
        r = subprocess.run(
            [BASH, str(SCRIPT), "katut-brain/x", "m", "reviews/9999-01-01.html"],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("local_file_missing", r.stdout)

    def test_usage_error_exits_two(self):
        r = subprocess.run(
            [BASH, str(SCRIPT), "katut-brain/x"],
            cwd=self.work,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
