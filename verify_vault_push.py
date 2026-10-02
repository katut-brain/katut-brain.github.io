#!/usr/bin/env python3
# verify_vault_push.py — 手順8.5（Vaultリポへ push_files で押したブックマークノート）の照合。
#
#   python3 verify_vault_push.py --repo-dir <Vaultリポのクローン> <パス> [<パス> ...]
#
#   <パス> はクローンのルートからの相対パス（例: Explore/bookmarks/rd-123-slug.md）。
#   ルートの中を指す絶対パスも受け付ける（`.` や `..` の要素は受け付けない）。どのディレクトリで
#   実行しても結果は変わらない（パスは --repo-dir を含むクローンのルートから解決する）。
#
# なぜこれが要るか（2026-10-02）:
#   手順8.5 の照合は `push_via_api.sh --verify-only` で行う手順だったが、あのスクリプトは
#   照合元のファイルを「実行したディレクトリ」から探す。スクリプトは公開リポにあるので、
#   公開リポで実行すると Vault のパスは必ず見つからず `verify=skipped reason=local_file_missing`
#   になる。2026-09-29〜10-01 の3晩とも照合が成立していなかった（2晩は省略、1晩はこの空振り）。
#   Vaultリポには Actions の検証ゲートが無いので、ここが外部脳に入る前の唯一の確認になる。
#
# 読み出しの経路:
#   gh api ではなく git fetch を使う。クラウドの実行環境で Vault クローンの `git fetch` と
#   `git show origin/main:<パス>` が通ることは 2026-09-30 のランで実測済み（手順8.5 の照合では
#   なく、終了時の後片付けでエージェントが自分で使い、7件とも一致を見ていた）。一方 gh api で
#   Vaultリポを読めるかは一度も測れていない。
#
# 照合の中身:
#   <remote> の <branch> を refs/remotes/<remote>/<branch> へ取り込み、各パスについて
#   その commit 上の blob（git cat-file blob ＝ 変換なしの生のバイト）と手元のファイルの
#   SHA-256 を比べる。**渡されたパスそのもの**を両側で読む（シンボリックリンクを解決して
#   別のパスを照合しない。経路の途中にリンクがあれば照合せず skipped にする）。
#
# 出力（標準出力・無人ランのログにそのまま残す）:
#   渡したパス1つにつき必ず1行:
#     WRITE_PATH: <パス> verify=match bytes=N
#     WRITE_PATH: <パス> verify=MISMATCH local_bytes=N remote_bytes=M
#     WRITE_PATH: <パス> verify=MISMATCH reason=not_regular_file mode=<M> （main 側がリンク等）
#     WRITE_PATH: <パス> verify=missing_remote        （main にそのパスのファイルが無い）
#     WRITE_PATH: <パス> verify=skipped reason=local_file_missing|outside_repo|symlink|dot_segment|duplicate
#     WRITE_PATH: <パス> verify=unreadable reason=not_a_repo|fetch_failed|local_read_failed|cat_file_failed
#   最後に1行:
#     VERIFY_SUMMARY: status=ok|failed|error total=N match=N mismatch=N missing_remote=N skipped=N unreadable=N head=<sha>
#     status=ok は全件 match のときだけ。1件でも match 以外なら failed。スクリプト自体の
#     例外は error（このときだけ WRITE_PATH 行が欠けうる）。
#
# 終了コード:
#   0 = 渡した全件が verify=match（このときだけ status=ok）
#   1 = それ以外。例外も traceback を出さずにここへ落とす
#   2 = 引数エラー（パスが1つも無い・--repo-dir が無い）
#   書き込みはしない（refs/remotes/<remote>/<branch> の更新を除く）。作業ツリーと index には触らない。
#
# 照合が保証すること・しないこと:
#   保証するのは「照合した時点の手元のファイル」と「main 上の同じパスの実体」のバイト一致。
#   push_files に渡した本文そのものは見ていない。手順7.5 で書いたノートを push 後に
#   書き換える手順は無いので、手元のファイル＝押そうとした原本、とみなしている。

import argparse
import hashlib
import os
import subprocess
import sys

FETCH_TIMEOUT = 120
GIT_TIMEOUT = 30
COUNT_KEYS = ("match", "mismatch", "missing_remote", "skipped", "unreadable")


def _git(top, args, timeout=GIT_TIMEOUT):
    return subprocess.run(["git", "-C", top] + args, capture_output=True, timeout=timeout)


def _toplevel(repo_dir):
    try:
        r = _git(repo_dir, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    top = r.stdout.decode("utf-8", "replace").strip()
    return os.path.normpath(top) if top else None


def _rel(top, path):
    """クローンのルートからの相対パス（区切りは /）。ルートの外なら None。

    シンボリックリンクは解決しない（解決すると、渡されたのと別のパスを照合してしまう）。
    """
    p = path.replace("\\", "/")
    full = os.path.normpath(p if os.path.isabs(p) else os.path.join(top, p))
    candidates = [top]
    real_top = os.path.realpath(top)
    if real_top != top:
        candidates.append(real_top)
    for base in candidates:
        try:
            rel = os.path.relpath(full, base).replace("\\", "/")
        except ValueError:  # Windows でドライブが違う
            continue
        if rel not in (".", "..") and not rel.startswith("../"):
            return rel
    return None


def _has_dot_segment(path):
    """`.` や `..` の要素を含むか。正規化で `link/..` が消えると途中のリンクを見逃すので、
    正規化する前に弾く（手順8.5 のパスにこれらは要らない）。"""
    return any(part in (".", "..") for part in path.replace("\\", "/").split("/"))


def _is_link(path):
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)  # Windows のジャンクション（3.12〜）
    return bool(isjunction and isjunction(path))


def _has_symlink(top, rel):
    cur = top
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if _is_link(cur):
            return True
    return False


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _verify_one(top, head, p, seen):
    """(判定行, 集計キー) を返す。例外は投げない。"""
    if _has_dot_segment(p):
        return "WRITE_PATH: %s verify=skipped reason=dot_segment" % p, "skipped"
    rel = _rel(top, p)
    if rel is None:
        return "WRITE_PATH: %s verify=skipped reason=outside_repo" % p, "skipped"
    if rel in seen:
        # 同じノートを2回数えると、渡し漏れがあっても件数が合ってしまう
        return "WRITE_PATH: %s verify=skipped reason=duplicate" % rel, "skipped"
    seen.add(rel)
    if _has_symlink(top, rel):
        return "WRITE_PATH: %s verify=skipped reason=symlink" % rel, "skipped"
    local_path = os.path.join(top, rel)
    if not os.path.isfile(local_path):
        return "WRITE_PATH: %s verify=skipped reason=local_file_missing" % rel, "skipped"
    try:
        with open(local_path, "rb") as fh:
            local = fh.read()
    except OSError:
        return "WRITE_PATH: %s verify=unreadable reason=local_read_failed" % rel, "unreadable"
    try:
        t = _git(top, ["ls-tree", head, "--", rel])
        if t.returncode != 0:
            return "WRITE_PATH: %s verify=unreadable reason=cat_file_failed" % rel, "unreadable"
        entry = t.stdout.decode("utf-8", "replace").strip()
        fields = entry.split(None, 2)
        if not entry or fields[1:2] != ["blob"]:
            return "WRITE_PATH: %s verify=missing_remote" % rel, "missing_remote"
        if fields[0] not in ("100644", "100755"):
            # main 側がシンボリックリンク（120000）等。中身の文字列が一致しても通常のファイルではない
            return ("WRITE_PATH: %s verify=MISMATCH reason=not_regular_file mode=%s"
                    % (rel, fields[0]), "mismatch")
        r = _git(top, ["cat-file", "blob", "%s:%s" % (head, rel)])
    except (OSError, subprocess.SubprocessError, IndexError):
        return "WRITE_PATH: %s verify=unreadable reason=cat_file_failed" % rel, "unreadable"
    if r.returncode != 0:
        return "WRITE_PATH: %s verify=unreadable reason=cat_file_failed" % rel, "unreadable"
    remote_bytes = r.stdout
    if _sha256(remote_bytes) == _sha256(local):
        return "WRITE_PATH: %s verify=match bytes=%d" % (rel, len(local)), "match"
    return ("WRITE_PATH: %s verify=MISMATCH local_bytes=%d remote_bytes=%d"
            % (rel, len(local), len(remote_bytes)), "mismatch")


def verify(repo_dir, paths, remote="origin", branch="main"):
    """各パスの判定行と集計を返す。"""
    lines = []
    counts = dict.fromkeys(COUNT_KEYS, 0)
    head = "-"

    top = _toplevel(repo_dir)
    if top is None:
        for p in paths:
            lines.append("WRITE_PATH: %s verify=unreadable reason=not_a_repo" % p)
        counts["unreadable"] = len(paths)
        return lines, counts, head

    ref = "refs/remotes/%s/%s" % (remote, branch)
    try:
        f = _git(top, ["fetch", "--quiet", remote, "+refs/heads/%s:%s" % (branch, ref)],
                 timeout=FETCH_TIMEOUT)
        fetched = f.returncode == 0
        if fetched:
            r = _git(top, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"])
            fetched = r.returncode == 0
            if fetched:
                head = r.stdout.decode("ascii", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        fetched = False
    if not fetched:
        for p in paths:
            lines.append("WRITE_PATH: %s verify=unreadable reason=fetch_failed" % p)
        counts["unreadable"] = len(paths)
        return lines, counts, head

    seen = set()
    for p in paths:
        line, key = _verify_one(top, head, p, seen)
        lines.append(line)
        counts[key] += 1
    return lines, counts, head


def _summary(status, total, counts, head):
    return ("VERIFY_SUMMARY: status=%s total=%d match=%d mismatch=%d missing_remote=%d "
            "skipped=%d unreadable=%d head=%s"
            % (status, total, counts["match"], counts["mismatch"], counts["missing_remote"],
               counts["skipped"], counts["unreadable"], head[:12]))


def main(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("paths", nargs="+")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2

    total = len(args.paths)
    try:
        lines, counts, head = verify(args.repo_dir, args.paths, args.remote, args.branch)
    except Exception as e:  # 無人ランを traceback で汚さない
        counts = dict.fromkeys(COUNT_KEYS, 0)
        counts["unreadable"] = total
        print(_summary("error", total, counts, "-") + " error=" + type(e).__name__)
        return 1
    for line in lines:
        print(line)
    all_match = counts["match"] == total
    print(_summary("ok" if all_match else "failed", total, counts, head))
    return 0 if all_match else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rc = main(sys.argv[1:])
    sys.stdout.flush()
    sys.exit(rc)
