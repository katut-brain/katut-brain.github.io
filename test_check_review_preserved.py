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
        self.assertEqual(missing, {("rid", 2), ("post_id", ("x", "222"))})

    def test_strong_id_key_counts_as_preserved_even_with_different_rid(self):
        """2026-09-22 Codexレビュー3周目 指摘対応: 保持判定は「rid・強いID
        キー(post_id)・genericキーのいずれか1つでも一致すれば保持」という
        OR判定にする（select_targets.card_matches_any() を正本にする）。
        rid が変わっても、href の強いIDキー(Instagram shortcode)が一致
        していれば「同じ投稿」とみなし、保持されているとする（別rid同士を
        別物と誤認して縮小を過検知しない）。
        """
        old = _doc('<div class="vcard"><a class="vlink" '
                   'href="https://www.instagram.com/p/DY5p4UKkuQc/"></a>'
                   '<button data-rid="1758030881"></button></div>')
        new = _doc('<div class="vcard"><a class="vlink" '
                   'href="https://www.instagram.com/p/DY5p4UKkuQc/"></a>'
                   '<button data-rid="1758030878"></button></div>')
        missing = crp.missing_cards(old, new)
        self.assertEqual(missing, set())

    def test_rid_only_change_with_no_shared_key_is_missing(self):
        """rid も href の強いIDキーも一致しない（別々のURLかつ別rid）場合は
        正しく missing として検出する（OR判定が過剰に緩くなっていない
        ことの確認）。
        """
        old = _doc(CARD_A)
        new = _doc(CARD_B)
        missing = crp.missing_cards(old, new)
        # CARD_A の rid=1・post_id=("x","111") はどちらも CARD_B に無いので
        # 両方 missing に含まれる。
        self.assertEqual(missing, {("rid", 1), ("post_id", ("x", "111"))})

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

    # --- --history モード（2026-09-22 Codexレビュー3周目 指摘対応） -------

    def _run_history(self, extra_args=()):
        return subprocess.run(
            [sys.executable, SCRIPT, "--history",
             "--path", "reviews/2026-09-21.html", *extra_args],
            cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
        )

    def test_history_detects_loss_hidden_by_a_middle_commit(self):
        """2026-09-08裁定と同じ理由: 差分ベース(HEAD^..HEAD)では、複数
        コミットのpushで『途中のコミット』が落としたカードを見逃す。
        commit1: A+B → commit2(途中でBを消す): A → commit3: A+C。
        HEAD^..HEAD (commit2..commit3) の差分は「Cが増えた」しか見えず
        Bの消失を検知できないが、--history は commit1〜3 全履歴を見るので
        Bの消失を検知できる。
        """
        self._write("reviews/2026-09-21.html", _doc(CARD_A + CARD_B))
        self._commit("commit1: A+B")
        self._write("reviews/2026-09-21.html", _doc(CARD_A))
        self._commit("commit2: drop B")
        card_c = ('<div class="vcard"><a class="vlink" '
                   'href="https://x.com/c/status/333"></a>'
                   '<button data-rid="3"></button></div>')
        self._write("reviews/2026-09-21.html", _doc(CARD_A + card_c))
        self._commit("commit3: add C")

        r = self._run_history()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("status=shrunk", r.stdout)
        self.assertIn("rid:2", r.stdout)

    def test_history_removing_commit_with_allow_marker_is_permitted(self):
        self._write("reviews/2026-09-21.html", _doc(CARD_A + CARD_B))
        self._commit("commit1: A+B")
        self._write("reviews/2026-09-21.html", _doc(CARD_A))
        self._commit("commit2: intentionally drop B [allow-review-shrink]")

        r = self._run_history()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("status=ok", r.stdout)
        self.assertIn("allowed=1", r.stdout)

    def test_history_strong_id_match_across_different_rid_is_preserved(self):
        """2026-09-22 Codexレビュー3周目 指摘対応: 別rid・同じ強いIDキー
        (Instagram shortcode)のカードは「保持されている」とみなし、
        誤って shrunk 扱いにしない。
        """
        card_v1 = ('<div class="vcard"><a class="vlink" '
                   'href="https://www.instagram.com/p/DY5p4UKkuQc/"></a>'
                   '<button data-rid="1758030881"></button></div>')
        card_v2 = ('<div class="vcard"><a class="vlink" '
                   'href="https://www.instagram.com/p/DY5p4UKkuQc/"></a>'
                   '<button data-rid="1758030878"></button></div>')
        self._write("reviews/2026-09-21.html", _doc(card_v1))
        self._commit("commit1")
        self._write("reviews/2026-09-21.html", _doc(card_v2))
        self._commit("commit2: rid corrected, same post")

        r = self._run_history()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("status=ok", r.stdout)

    def test_history_no_history_for_untracked_file(self):
        self._write("reviews/2026-09-21.html", _doc(CARD_A))
        # コミットしない（履歴ゼロ）。
        r = self._run_history()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("status=no_history", r.stdout)

    def test_history_and_old_ref_together_is_an_error(self):
        r = subprocess.run(
            [sys.executable, SCRIPT, "--history", "--old-ref", "HEAD",
             "--path", "reviews/2026-09-21.html"],
            cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_history_all_flag_globs_reviews_dir(self):
        self._write("reviews/2026-09-21.html", _doc(CARD_A + CARD_B))
        self._commit("commit1: A+B")
        self._write("reviews/2026-09-21.html", _doc(CARD_A))
        self._commit("commit2: drop B (no marker)")

        r = subprocess.run(
            [sys.executable, SCRIPT, "--history", "--all"],
            cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("reviews/2026-09-21.html", r.stdout)
        self.assertIn("status=shrunk", r.stdout)


if __name__ == "__main__":
    unittest.main()
