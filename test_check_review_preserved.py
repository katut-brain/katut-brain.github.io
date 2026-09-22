#!/usr/bin/env python3
# test_check_review_preserved.py — check_review_preserved.py の回帰テスト。
#
# 本番の missing_cards()/main() を実際に通す（モックで差し替えない）。
# git の履歴比較は一時的な git リポジトリを作って本物の git コマンドを
# 走らせる（--old-ref を実際の git ref として渡す経路も検証する）。

import io
import os
import subprocess
import sys
import tempfile
import unittest

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import check_review_preserved as crp  # noqa: E402

SCRIPT = os.path.join(REPO_DIR, "check_review_preserved.py")

CARD_A = ('<div class="vcard"><a class="vlink" '
          'href="https://x.com/a/status/111"></a>'
          '<button data-rid="1"></button></div>')
CARD_B = ('<div class="vcard"><a class="vlink" '
          'href="https://x.com/b/status/222"></a>'
          '<button data-rid="2"></button></div>')


def _doc(cards_html):
    return ('<!doctype html><html><body><div class="wrap">'
            '<div class="cards">%s</div><footer>f</footer>'
            '</div></body></html>' % cards_html)


class TestMissingCards(unittest.TestCase):
    def test_no_missing_when_all_cards_kept(self):
        old = _doc(CARD_A)
        new = _doc(CARD_A + CARD_B)
        self.assertEqual(crp.missing_cards(old, new), set())

    def test_missing_when_card_dropped(self):
        old = _doc(CARD_A + CARD_B)
        new = _doc(CARD_A)
        missing = crp.missing_cards(old, new)
        self.assertEqual(missing, {("rid", 2)})

    def test_strong_id_key_counts_as_preserved_even_with_different_rid(self):
        """rid が変わっても、強いIDキー(post_id)が一致していれば別カードの
        識別子としては同一に見えないが、card_identity() は rid を優先する
        ため rid が変わると別物扱いになる——これは安全側（縮小を過検知して
        止める）であることを確認する。
        """
        old = _doc('<div class="vcard"><a class="vlink" '
                   'href="https://www.instagram.com/p/DY5p4UKkuQc/"></a>'
                   '<button data-rid="1758030881"></button></div>')
        new = _doc('<div class="vcard"><a class="vlink" '
                   'href="https://www.instagram.com/p/DY5p4UKkuQc/"></a>'
                   '<button data-rid="1758030878"></button></div>')
        missing = crp.missing_cards(old, new)
        self.assertEqual(missing, {("rid", 1758030881)})

    def test_unidentified_cards_do_not_count_as_missing(self):
        no_rid_no_href = '<div class="vcard"><span>no link</span></div>'
        old = _doc(no_rid_no_href)
        new = _doc("")
        self.assertEqual(crp.missing_cards(old, new), set())


class TestGitIntegration(unittest.TestCase):
    """本物の git リポジトリを一時ディレクトリに作り、--old-ref 経由の
    比較が実際の git 履歴に対して動くことを確認する。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._run_git(["init", "-q"])
        self._run_git(["config", "user.email", "test@example.com"])
        self._run_git(["config", "user.name", "test"])
        os.makedirs(os.path.join(self.tmp, "reviews"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_git(self, args):
        r = subprocess.run(["git"] + args, cwd=self.tmp,
                            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def _write(self, rel, content):
        path = os.path.join(self.tmp, rel)
        with io.open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)

    def _commit(self, msg):
        self._run_git(["add", "-A"])
        self._run_git(["commit", "-q", "-m", msg])

    def test_ok_when_new_commit_keeps_all_cards(self):
        self._write("reviews/2026-09-21.html", _doc(CARD_A))
        self._commit("first")
        old_ref = self._run_git(["rev-parse", "HEAD"]).strip()
        self._write("reviews/2026-09-21.html", _doc(CARD_A + CARD_B))
        self._commit("second")

        r = subprocess.run(
            [sys.executable, SCRIPT, "--old-ref", old_ref,
             "--path", "reviews/2026-09-21.html"],
            cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("status=ok", r.stdout)

    def test_fails_when_card_disappears(self):
        self._write("reviews/2026-09-21.html", _doc(CARD_A + CARD_B))
        self._commit("first")
        old_ref = self._run_git(["rev-parse", "HEAD"]).strip()
        self._write("reviews/2026-09-21.html", _doc(CARD_A))
        self._commit("second (accidental overwrite)")

        r = subprocess.run(
            [sys.executable, SCRIPT, "--old-ref", old_ref,
             "--path", "reviews/2026-09-21.html"],
            cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("status=shrunk", r.stdout)

    def test_allow_flag_permits_shrink(self):
        self._write("reviews/2026-09-21.html", _doc(CARD_A + CARD_B))
        self._commit("first")
        old_ref = self._run_git(["rev-parse", "HEAD"]).strip()
        self._write("reviews/2026-09-21.html", _doc(CARD_A))
        self._commit("second [allow-review-shrink]")

        r = subprocess.run(
            [sys.executable, SCRIPT, "--old-ref", old_ref,
             "--path", "reviews/2026-09-21.html", "--allow"],
            cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("status=shrunk", r.stdout)
        self.assertIn("allowed", r.stdout)

    def test_new_file_with_no_prior_version_is_ok(self):
        # 履歴上に reviews ファイルが一切無いコミット（orphanブランチ）を
        # old-ref に見立てる。「そのパスが存在しない ref」を渡したときに
        # new 扱いになることを確認する。
        self._run_git(["checkout", "-q", "--orphan", "empty-branch"])
        self._write(".gitkeep", "")
        self._commit("empty")
        empty_ref = self._run_git(["rev-parse", "HEAD"]).strip()
        self._run_git(["checkout", "-q", "-b", "main-work"])

        self._write("reviews/2026-09-22.html", _doc(CARD_A))
        self._commit("first ever review commit")

        r = subprocess.run(
            [sys.executable, SCRIPT, "--old-ref", empty_ref,
             "--path", "reviews/2026-09-22.html"],
            cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("status=new", r.stdout)

    def test_no_paths_given_is_noop_exit_zero(self):
        r = subprocess.run(
            [sys.executable, SCRIPT, "--old-ref", "HEAD"],
            cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
