#!/usr/bin/env python3
"""reviews/<日付>.html のカード（掲載記録）が、直前の版から消えていないかを
確認する公開ゲート。

## 何のためにあるか

select_targets.py 導入後、reviews/*.html 自体が「掲載済み」の記録を兼ねる
（capture_index.json の日付台帳ベースの判定から、reviews の実掲載を直接見る
方式へ2026-09-22に変更済み）。そのため、reviews/<日付>.html が**同じファイル名
のまま上書きされてカードが一部消える**と、そのURL/rid は二度と「掲載済み」と
確認できなくなり、かつ select_targets.py はもう選ばない（一度掲載済みと
見なされたら、それを取り消す仕組みが無い）＝実質的にそのカードの記録が
永久に失われる。

build-feed.yml の既存の「Check reviews shrink」ステップは**ファイル単位**
（存在の有無・バイト数の縮小）でしか見ておらず、同じファイル名のまま一部の
カードだけが消える上書き（例: merge_review.py を使わずに手動で
reviews/<TARGET>.html を新規生成で丸ごと上書きしてしまった場合）を検知
できない。このスクリプトは**カード単位**（data-rid / 強いIDキー）で、
「直前の版にあったカードが新しい版にも全部含まれているか」を確認する。

## 使い方

  python3 check_review_preserved.py --old-ref <gitref> --path reviews/2026-09-21.html [--path ...] [--allow]

  --old-ref: 比較対象の古い版を読む git ref（例: HEAD^, origin/main）。
             そのrefにファイルが存在しない場合は「新規ファイル」として
             スキップする（比較対象が無い＝縮みようがない）。
  --path:    チェックする reviews/*.html のパス（作業ツリー上の現在の内容と
             比較する）。複数指定可。
  --allow:   カードの消失があっても検知結果は表示した上で exit 0 にする
             （コミットメッセージに [allow-review-shrink] があるときの
             呼び出し側の判断をここに反映する。マーカー文字列の検出自体は
             このスクリプトの責務にしない——呼び出し側のCIワークフローが
             `git log` でコミットメッセージを読み、その結果として --allow
             を渡すかどうかを決める）。

終了コード: カードの消失があり --allow が無ければ 1、それ以外は常に 0。
入力（--old-ref・--path）自体が不正な場合は原因をstderrへ出し、安全側として
exit 1 にする（判定できないものを黙って通さない）。

カードの識別は select_targets.card_identity() を正本にして re-use する
（rid一致優先、無ければ強いIDキー、無ければgenericキー。同じロジックを
複数箇所に書かない）。
"""

import argparse
import os
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import select_targets  # noqa: E402


def card_ids(html_text):
    """HTML文字列から (identities: set, unidentified_count: int) を返す。
    unidentified はrid・URLどちらからも識別できなかったカードの件数
    （比較のしようがないので missing 判定には使わない。参考値として返す）。
    """
    cards, _malformed = select_targets.extract_vcards(html_text)
    ids = set()
    unidentified = 0
    for c in cards:
        ident = select_targets.card_identity(c["raw"])
        if ident is None:
            unidentified += 1
            continue
        ids.add(ident)
    return ids, unidentified


def missing_cards(old_html, new_html):
    """old_html にあって new_html に無いカード識別子の集合を返す。"""
    old_ids, _ = card_ids(old_html)
    new_ids, _ = card_ids(new_html)
    return old_ids - new_ids


def _git_show(ref, path):
    """git show <ref>:<path> の標準出力を返す。存在しない/読めない場合は
    None（「新規ファイルで比較対象が無い」と「読めない」を区別しない——
    どちらの場合も安全側は「チェックをスキップ」で、失敗させる必要はない。
    誤って古い版が読めないことを『縮小』と誤検知しない）。
    """
    try:
        r = subprocess.run(
            ["git", "show", "%s:%s" % (ref, path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def check_path(old_ref, path):
    """1ファイル分のチェック結果を dict で返す。
    {"path", "status": "new"|"ok"|"shrunk"|"missing_now", "missing": [str,...]}
    """
    old_text = _git_show(old_ref, path)
    if old_text is None:
        return {"path": path, "status": "new", "missing": []}
    try:
        with open(path, encoding="utf-8") as fh:
            new_text = fh.read()
    except OSError:
        return {"path": path, "status": "missing_now", "missing": []}
    missing = missing_cards(old_text, new_text)
    if missing:
        return {"path": path, "status": "shrunk",
                "missing": sorted(str(m) for m in missing)}
    return {"path": path, "status": "ok", "missing": []}


def main(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--old-ref", required=True)
    parser.add_argument("--path", action="append", default=[], dest="paths")
    parser.add_argument("--allow", action="store_true",
                         help="消失を検知しても exit 0 にする（呼び出し側が"
                              " [allow-review-shrink] を確認済みのときに使う）")
    args = parser.parse_args(argv)

    if not args.paths:
        print("CHECK_REVIEW_PRESERVED: no --path given, nothing to check")
        return 0

    any_missing = False
    for path in args.paths:
        result = check_path(args.old_ref, path)
        if result["status"] == "shrunk":
            any_missing = True
            print("CHECK_REVIEW_PRESERVED: path=%s status=shrunk missing=%d ids=%s"
                  % (result["path"], len(result["missing"]), result["missing"]))
        elif result["status"] == "missing_now":
            any_missing = True
            print("CHECK_REVIEW_PRESERVED: path=%s status=missing_now "
                  "(existed before, file not found now)" % result["path"])
        else:
            print("CHECK_REVIEW_PRESERVED: path=%s status=%s"
                  % (result["path"], result["status"]))

    if any_missing:
        if args.allow:
            print("CHECK_REVIEW_PRESERVED: shrink detected but allowed (--allow)")
            return 0
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
