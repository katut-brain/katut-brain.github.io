#!/usr/bin/env python3
# check_review_style.py — reviews/<日付>.html の <style> が正規の CSS（review_style.css）と
# 同じかを確かめ、--fix なら正規の中身に揃える（手順7）。
#
#   python3 check_review_style.py --date YYYY-MM-DD [--fix]
#
# なぜこれが要るか（2026-10-02）:
#   reviews/2026-09-29.html の <style> の先頭に、手順書の地の文（「` ブロックをそのまま使う
#   （CSS変数・ダーク対応込み）。…」）が混入していた。その夜のエージェントが CSS を
#   `re.search(r'<style>.*?</style>', cloud_routine_prompt.md)` で手順書から抜き出し、本物の
#   <style> より前にある地の文の「`<style>` ブロック」に一致したため。CSS の最初の規則
#   （ライトモードの :root の色の定義）がまるごと無効になり、ライトモードで面の色とカードの
#   枠が消えた。CSS の取り方は夜ごとにエージェントが即興で決めていて（前日の reviews から／
#   手順書の行番号で）、手順7の自己検証も公開ゲートもこれを見ていなかった。
#   そこで CSS を手順書から review_style.css へ移し（エージェントが手順書から抜き出す必要を
#   無くす）、公開前にこのスクリプトで正規の中身に揃える。
#
# 判定:
#   最初の <style …>…</style> の中身と review_style.css を、空白の違いを無視して比べる
#   （インデントや改行の差は見た目に影響しないので ok とする）。
#   書き換えるのは最初の <style> 要素だけ。それ以外のバイトは1つも変えない。
#   <style> が無ければ </head> の直前に入れる（--fix のとき）。
#   **2026-09-22 より前の日付には書き込まない**（CANONICAL_SINCE）。CSS のテンプレートは
#   06-12〜08-03・08-04〜09-21・09-22 以降で3世代あり、今の review_style.css と完全に
#   一致するのは 09-22 以降だけ（2026-10-02 に公開中の71本で実測。09-29 だけが破損）。
#   最古の世代の .vcard はカード自体がリンクだった頃の規則（text-decoration:none 等）で、
#   今の CSS に置き換えると見た目が変わる。古い日付を作り直すラン（TARGET_OVERRIDE）で
#   過去のページを書き換えないよう、線より前は判定だけして書かない（reason=before_canonical）。
#
# 出力（1行・無人ランのログにそのまま残す）:
#   STYLE_CHECK: date=<日付> status=ok|fixed|mismatch|no_review|error [reason=…]
#     ok        … 正規の CSS と同じ（何も書かない）
#     fixed     … --fix で正規の中身に揃えた（reason=replaced|inserted）
#     mismatch  … --fix なしで、正規の CSS と違う（ファイルは変えない）。
#                 線より前の日付なら --fix でも書かず reason=before_canonical
#     no_review … reviews/<日付>.html が無い（手順5で作らなかった夜。何もしない）
#     error     … 読めない・</head> も <style> も無い・書けない・引数不正など
#
# 終了コード: --fix のときは常に 0（無人ランを止めない。status=error も行で判定する）。
#             --fix なしのときは ok/no_review で 0、それ以外で 1。

import argparse
import io
import os
import re
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
STYLE_FILE = os.path.join(REPO_DIR, "review_style.css")
DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
CANONICAL_SINCE = "2026-09-22"
STYLE_RE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.S | re.I)
HEAD_END_RE = re.compile(r"</head\s*>", re.I)


def _norm(css):
    return re.sub(r"\s+", " ", css).strip()


def _canonical():
    with io.open(STYLE_FILE, encoding="utf-8", newline="") as fh:
        return fh.read()


def _element(css):
    return "<style>\n" + css.rstrip("\n") + "\n</style>"


def check(path, date, fix):
    """(status, reason, new_text) を返す。new_text が None なら書かない。"""
    if not os.path.isfile(path):
        return "no_review", None, None
    with io.open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    css = _canonical()
    m = STYLE_RE.search(text)
    if m and _norm(m.group(1)) == _norm(css):
        return "ok", None, None
    if date < CANONICAL_SINCE:
        return "mismatch", "before_canonical", None
    if m:
        if not fix:
            return "mismatch", None, None
        return "fixed", "replaced", text[:m.start()] + _element(css) + text[m.end():]
    if not fix:
        return "mismatch", "no_style", None
    h = HEAD_END_RE.search(text)
    if not h:
        return "error", "no_style_no_head", None
    return "fixed", "inserted", text[:h.start()] + _element(css) + "\n" + text[h.start():]


def _write_atomic(path, text):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".style-", dir=d)
    try:
        with io.open(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--reviews-dir", default=os.path.join(REPO_DIR, "reviews"))
    fix = "--fix" in argv
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        if e.code == 0:
            return 0
        print("STYLE_CHECK: date=- status=error reason=bad_args")
        return 0 if fix else 1
    if not DATE_RE.match(args.date):
        print("STYLE_CHECK: date=%s status=error reason=bad_date" % args.date.strip()[:20])
        return 0 if args.fix else 1
    path = os.path.join(args.reviews_dir, args.date + ".html")
    try:
        status, reason, new_text = check(path, args.date, args.fix)
        if new_text is not None:
            _write_atomic(path, new_text)
    except Exception as e:  # 無人ランを traceback で止めない
        status, reason = "error", type(e).__name__
    line = "STYLE_CHECK: date=%s status=%s" % (args.date, status)
    if reason:
        line += " reason=%s" % reason
    print(line)
    if args.fix:
        return 0
    return 0 if status in ("ok", "no_review") else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rc = main(sys.argv[1:])
    sys.stdout.flush()
    sys.exit(rc)
