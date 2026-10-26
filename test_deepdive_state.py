"""deepdive_state.py の受け入れテスト。

守るのは3点: ①深掘り節のあるノートだけが「済み」になる ②Vault が読めない・
少なすぎる・一覧が急に減るときは書かない（全カードの印が一晩で消えるのを防ぐ）
③変わらないなら書き換えない（毎晩の無駄な push を出さない）。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "deepdive_state.py"


def note(rid, body=""):
    return f"---\ndate: 2026-10-01\nraindrop_id: {rid}\nsource: https://example.com\n---\n\n# t\n\n{body}\n"


class DeepdiveStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bm = Path(self.tmp, "vault", "Explore", "bookmarks")
        self.bm.mkdir(parents=True)
        self.out = str(Path(self.tmp, "state.json"))

    def fill(self, n=120):
        for i in range(n):
            (self.bm / f"rd-{i}-x.md").write_text(note(i), encoding="utf-8")

    def run_script(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(self.bm.parent.parent), "--out", self.out],
            capture_output=True, text=True, encoding="utf-8")

    def done(self):
        import json
        with open(self.out, encoding="utf-8") as fh:
            return json.load(fh)["done"]

    def test_only_deepdive_heading_counts(self):
        self.fill()
        (self.bm / "rd-1-x.md").write_text(note(1, "## 2026-10-03 深掘り\n本文"), encoding="utf-8")
        (self.bm / "rd-2-x.md").write_text(note(2, "## 事前リサーチ（試行・未検証）\n本文"), encoding="utf-8")
        (self.bm / "rd-3-x.md").write_text(note(3, "### 深掘りメモ\n三段見出しは対象外"), encoding="utf-8")
        (self.bm / "rd-4-x.md").write_text(note(4, "本文に深掘りと書いただけ"), encoding="utf-8")
        (self.bm / "rd-5-x.md").write_text(note(5, "## 深掘り\n日付なし見出し"), encoding="utf-8")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.done(), [1, 5])
        self.assertIn("status=ok", r.stdout)

    def test_crlf_note_counts(self):
        self.fill()
        (self.bm / "rd-7-x.md").write_bytes(note(7, "## 2026-10-03 深掘り\n本文").replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.done(), [7])

    def test_too_few_notes_writes_nothing(self):
        for i in range(10):
            (self.bm / f"rd-{i}-x.md").write_text(note(i, "## 深掘り"), encoding="utf-8")
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn("too_few_notes", r.stdout)
        self.assertFalse(os.path.exists(self.out))

    def test_missing_vault_writes_nothing(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--vault", str(Path(self.tmp, "nope")), "--out", self.out],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 1)
        self.assertIn("vault_not_found", r.stdout)
        self.assertFalse(os.path.exists(self.out))

    def test_big_drop_is_refused_and_old_file_kept(self):
        self.fill()
        for i in range(20):
            (self.bm / f"rd-{i}-x.md").write_text(note(i, "## 深掘り"), encoding="utf-8")
        self.assertEqual(self.run_script().returncode, 0)
        for i in range(20):  # 読み取りに失敗して節が見えなくなった状況を模す
            (self.bm / f"rd-{i}-x.md").write_text(note(i), encoding="utf-8")
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn("would_drop", r.stdout)
        self.assertEqual(len(self.done()), 20)

    def test_unchanged_is_not_rewritten(self):
        self.fill()
        (self.bm / "rd-1-x.md").write_text(note(1, "## 深掘り"), encoding="utf-8")
        self.run_script()
        mtime = os.stat(self.out).st_mtime_ns
        r = self.run_script()
        self.assertIn("changed=no", r.stdout)
        self.assertEqual(os.stat(self.out).st_mtime_ns, mtime)


    def test_bom_and_quoted_rid_count(self):
        self.fill()
        body = "---\ndate: 2026-10-01\nraindrop_id: \"11\"\n---\n\n## 2026-10-03 深掘り\n本文\n"
        (self.bm / "rd-11-x.md").write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        (self.bm / "rd-12-x.md").write_text(body.replace('"11"', "'12'"), encoding="utf-8")
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.done(), [11, 12])

    def test_ambiguous_vault_writes_nothing(self):
        """隣に Explore/bookmarks を持つ clone が2つあるときは、どちらも選ばない。"""
        base = Path(self.tmp, "ws")
        repo = base / "repo"
        repo.mkdir(parents=True)
        for name in ("vaultA", "vaultB"):
            d = base / name / "Explore" / "bookmarks"
            d.mkdir(parents=True)
            for i in range(120):
                (d / f"rd-{i}-x.md").write_text(note(i, "## 深掘り"), encoding="utf-8")
        r = subprocess.run([sys.executable, str(SCRIPT), "--out", self.out], cwd=repo,
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ambiguous", r.stdout)
        self.assertFalse(os.path.exists(self.out))

    def test_single_sibling_vault_is_found(self):
        base = Path(self.tmp, "ws2")
        repo = base / "repo"
        repo.mkdir(parents=True)
        d = base / "vault" / "Explore" / "bookmarks"
        d.mkdir(parents=True)
        for i in range(120):
            (d / f"rd-{i}-x.md").write_text(note(i, "## 深掘り" if i < 3 else ""), encoding="utf-8")
        out = str(repo / "s.json")
        r = subprocess.run([sys.executable, str(SCRIPT), "--out", out], cwd=repo,
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("done=3", r.stdout)

    def test_validate_is_strict(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import deepdive_state as ds
        self.assertIsNone(ds.validate({"done": [1, -2, 3]}))
        self.assertIsNone(ds.validate({"done": []}))
        for bad in ({"done": [1], "private_note": "x"}, {"done": [True]}, {"done": ["1"]},
                    {"done": [1.5]}, {"done": [1, 1]}, {"done": "1"}, {}, [1], None):
            self.assertIsNotNone(ds.validate(bad), bad)

    def test_publish_workflow_uses_validate(self):
        """Actions 側の検査が validate() を呼び続けること（外れると赤）。"""
        wf = (SCRIPT.parent / ".github" / "workflows" / "publish-from-branch.yml").read_text(encoding="utf-8")
        self.assertIn("dds.validate(state)", wf)
        # 検査コードは信頼済みの origin/main から読む（検証対象のブランチから import しない）
        self.assertIn("git\", \"show\", \"origin/main:deepdive_state.py", wf)
        self.assertNotIn("import deepdive_state", wf)
        self.assertIn("deepdive_state\\.json", wf)


if __name__ == "__main__":
    unittest.main()
