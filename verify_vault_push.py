#!/usr/bin/env python3
# verify_vault_push.py — 手順8.5（Vaultリポへ push_files で押したブックマークノート）の照合。
#
#   python3 verify_vault_push.py --repo-dir <Vaultリポのクローン> <パス> [<パス> ...]
#
#   <パス> はクローンのルートからの相対パス（例: Explore/bookmarks/rd-123-slug.md）。
#   ルートの中を指す絶対パスも受け付ける。どのディレクトリで実行しても結果は変わらない
#   （パスは --repo-dir から解決する）。
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
#   `git show origin/main:<パス>` が通ることは 2026-09-30 のランで実測済み（7件とも一致を確認して
#   いた）。一方 gh api で Vaultリポを読めるかは一度も測れていない。
#
# 照合の中身:
#   <remote> の <branch> を refs/remotes/<remote>/<branch> へ取り込み、各パスについて
#   その commit 上の blob（git cat-file blob ＝ 変換なしの生のバイト）と手元のファイルの
#   SHA-256 を比べる。
#
# 出力（標準出力・無人ランのログにそのまま残す）:
#   1ファイル1行:
#     WRITE_PATH: <パス> verify=match bytes=N
#     WRITE_PATH: <パス> verify=MISMATCH local_bytes=N remote_bytes=M
#     WRITE_PATH: <パス> verify=missing_remote        （main にそのパスが無い）
#     WRITE_PATH: <パス> verify=skipped reason=local_file_missing|outside_repo
#     WRITE_PATH: <パス> verify=unreadable reason=not_a_repo|fetch_failed
#   最後に1行:
#     VERIFY_SUMMARY: status=ok|error total=N match=N mismatch=N missing_remote=N skipped=N unreadable=N head=<sha>
#
# 終了コード:
#   0 = 渡した全件が verify=match
#   1 = 1件でも match 以外（照合できなかった場合を含む）。例外も traceback を出さずにここへ落とす
#   2 = 引数エラー（パスが1つも無い・--repo-dir が無い）
#   書き込みはしない（refs/remotes/<remote>/<branch> の更新を除く）。作業ツリーと index には触らない。

import argparse
import hashlib
import os
import subprocess
import sys

FETCH_TIMEOUT = 120
GIT_TIMEOUT = 30


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
    return os.path.realpath(top) if top else None


def _rel(top, path):
    """クローンのルートからの相対パス（区切りは /）。ルートの外なら None。"""
    p = path.replace("\\", "/")
    full = p if os.path.isabs(p) else os.path.join(top, p)
    rel = os.path.relpath(os.path.realpath(full), top).replace("\\", "/")
    if rel == "." or rel == ".." or rel.startswith("../"):
        return None
    return rel


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def verify(repo_dir, paths, remote="origin", branch="main"):
    """各パスの判定行と集計を返す。例外は呼び出し側で受ける。"""
    lines = []
    counts = {"match": 0, "mismatch": 0, "missing_remote": 0, "skipped": 0, "unreadable": 0}
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
    except (OSError, subprocess.SubprocessError):
        fetched = False
    if fetched:
        r = _git(top, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"])
        if r.returncode == 0:
            head = r.stdout.decode("ascii", "replace").strip()
        else:
            fetched = False
    if not fetched:
        for p in paths:
            lines.append("WRITE_PATH: %s verify=unreadable reason=fetch_failed" % p)
        counts["unreadable"] = len(paths)
        return lines, counts, head

    for p in paths:
        rel = _rel(top, p)
        if rel is None:
            lines.append("WRITE_PATH: %s verify=skipped reason=outside_repo" % p)
            counts["skipped"] += 1
            continue
        local_path = os.path.join(top, rel)
        if not os.path.isfile(local_path):
            lines.append("WRITE_PATH: %s verify=skipped reason=local_file_missing" % rel)
            counts["skipped"] += 1
            continue
        with open(local_path, "rb") as fh:
            local = fh.read()
        r = _git(top, ["cat-file", "blob", "%s:%s" % (head, rel)])
        if r.returncode != 0:
            lines.append("WRITE_PATH: %s verify=missing_remote" % rel)
            counts["missing_remote"] += 1
            continue
        remote_bytes = r.stdout
        if _sha256(remote_bytes) == _sha256(local):
            lines.append("WRITE_PATH: %s verify=match bytes=%d" % (rel, len(local)))
            counts["match"] += 1
        else:
            lines.append("WRITE_PATH: %s verify=MISMATCH local_bytes=%d remote_bytes=%d"
                         % (rel, len(local), len(remote_bytes)))
            counts["mismatch"] += 1
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

    empty = {"match": 0, "mismatch": 0, "missing_remote": 0, "skipped": 0, "unreadable": 0}
    try:
        lines, counts, head = verify(args.repo_dir, args.paths, args.remote, args.branch)
    except Exception as e:  # 無人ランを traceback で汚さない
        print(_summary("error", len(args.paths), dict(empty, unreadable=len(args.paths)), "-")
              + " error=" + type(e).__name__)
        return 1
    for line in lines:
        print(line)
    print(_summary("ok", len(args.paths), counts, head))
    return 0 if counts["match"] == len(args.paths) else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rc = main(sys.argv[1:])
    sys.stdout.flush()
    sys.exit(rc)
