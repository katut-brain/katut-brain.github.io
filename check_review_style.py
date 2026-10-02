#!/usr/bin/env python3
# check_review_style.py — reviews/<日付>.html の <style> が正規の CSS（review_style.css）と
# 同じかを確かめ、--fix なら正規の中身に揃える（手順7）。
#
#   python3 check_review_style.py --date YYYY-MM-DD [--fix]      （手順7・無人ラン）
#   python3 check_review_style.py --all [--since D] --github       （公開ゲート build-feed.yml・書かない）
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
# 要素の見つけ方:
#   正規表現でなく標準ライブラリの html.parser で読む。コメント・<script> の中・属性値に
#   書かれた "<style>" や "</head>" を本物と取り違えないため（2026-10-02 の Codex 指摘）。
#   対象は <head> の中（</head> も <body> も無ければ文書全体）で最初の <style> 要素。
#   挿入先は本物の </head> の直前（無ければ <body> の直前）。
#
# 判定:
#   最初の <style> 要素の中身と review_style.css を、各行の前後の空白と空行だけを無視して
#   比べる（インデントの差は見た目に影響しない。行の中の空白は CSS の文字列の値になりうる
#   ので比べる）。
#   書き換えるのはその <style> 要素だけ（属性は落として素の <style> にする）。それ以外の
#   バイトは1つも変えない。書き換えた後に読み直し、正規の中身の <style> が head にあることを
#   確かめてから書く（確かめられなければ書かずに status=error）。
#
# 2026-09-22 より前の日付（CANONICAL_SINCE）:
#   CSS のテンプレートは 06-12〜08-03・08-04〜09-21・09-22 以降で3世代あり、今の
#   review_style.css と完全に一致するのは 09-22 以降だけ（2026-10-02 に公開中の71本で実測。
#   09-29 だけが破損）。最古の世代の .vcard はカード自体がリンクだった頃の規則で、今の CSS に
#   置き換えると見た目が変わる。古い日付を作り直すラン（TARGET_OVERRIDE）で過去のページを
#   書き換えないよう、線より前の日付で、CSS が**古い2世代のどちらかと同じ**（指紋が
#   KNOWN_OLD_GENERATIONS に一致）なら判定だけして書かない（reason=before_canonical）。
#   どちらとも違う（壊れている・その夜に新しく作ったページ等）なら、日付にかかわらず揃える。
#
# head の中に <style> が2つ以上あるとき:
#   2つ目が正規の規則を上書きしうるが、どれを残すかは機械で決めない。書き換えずに
#   status=mismatch reason=extra_style:N を出す（手順書はログに残して先へ進む扱い）。
#
# 出力（1行・無人ランのログにそのまま残す）:
#   STYLE_CHECK: date=<日付> status=ok|fixed|mismatch|no_review|error [reason=…]
#     ok        … 正規の CSS と同じ（何も書かない）
#     fixed     … --fix で正規の中身に揃え、読み直して確かめた（reason=replaced|inserted）
#     mismatch  … --fix なしで正規の CSS と違う（ファイルは変えない）。
#                 線より前の古い世代なら --fix でも書かず reason=before_canonical。
#                 head に <style> が2つ以上なら --fix でも書かず reason=extra_style:N
#     no_review … reviews/<日付>.html が無い（手順5で作らなかった夜。何もしない）
#     error     … 読めない・</head> も <body> も無い・<style> が閉じていない（unclosed_style）・
#                 書き換え後の確認に落ちた（verify_failed）・書けない・引数不正など
#
# 終了コード: --fix のときは常に 0（無人ランを止めない。status=error も行で判定する）。
#             --fix なしのときは ok/no_review で 0、それ以外で 1。

import argparse
import hashlib
import io
import os
import re
import sys
import tempfile
from html.parser import HTMLParser

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
STYLE_FILE = os.path.join(REPO_DIR, "review_style.css")
DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
CANONICAL_SINCE = "2026-09-22"
# 2026-09-22 より前の2世代の CSS の指紋（_fingerprint の値）。公開中の61本は必ずどちらかに
# 一致する（2026-10-02 実測: 06-12〜08-03 の24本／08-04〜09-21 の37本）。
KNOWN_OLD_GENERATIONS = {
    "cb5c3d72caf7a025a17fc9d7dda1a0e0137f974faba37ec6f43bdefc8a4f5c23",  # 06-12〜08-03
    "32755489f46f2862ee13a708107ed8cc0af3d415a6d50754cad28554bea67bc8",  # 08-04〜09-21
}
CSS_SPACE = " \t\r\n\f"  # CSS が空白とみなす文字（NBSP や全角空白は識別子の一部なので含めない）


def _lines(css):
    out = []
    for line in css.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip(CSS_SPACE)
        if line:
            out.append(line)
    return out


def _same(css_a, css_b):
    return _lines(css_a) == _lines(css_b)


def _fingerprint(css):
    return hashlib.sha256("\n".join(_lines(css)).encode("utf-8")).hexdigest()


def _canonical():
    with io.open(STYLE_FILE, encoding="utf-8", newline="") as fh:
        return fh.read()


def _element(css):
    return "<style>\n" + css.rstrip("\n") + "\n</style>"


class _Locator(HTMLParser):
    """本物の <style> 要素・</head>・<body> の位置を (行, 桁) で記録する。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.styles = []          # [(開始タグの位置, 開始タグの文字列, 終了タグの位置 or None)]
        self.head_end = None
        self.body_start = None
        self._open = None

    def handle_starttag(self, tag, attrs):
        if tag == "style":
            self._open = [self.getpos(), self.get_starttag_text(), None]
            self.styles.append(self._open)
        elif tag == "body" and self.body_start is None:
            self.body_start = self.getpos()

    def handle_startendtag(self, tag, attrs):
        if tag == "body" and self.body_start is None:
            self.body_start = self.getpos()

    def handle_endtag(self, tag):
        if tag == "style" and self._open is not None:
            self._open[2] = self.getpos()
            self._open = None
        elif tag == "head" and self.head_end is None:
            self.head_end = self.getpos()


class _Unclosed(Exception):
    pass


def _offsets(text):
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return lambda pos: starts[pos[0] - 1] + pos[1]


def _locate(text):
    """head の中の <style> を探す。

    戻り値: (最初の style の (開始, 中身の開始, 中身の終了, 要素の終了) or None,
             挿入位置 or None, head の中の style 要素の数)
    """
    p = _Locator()
    p.feed(text)
    p.close()
    off = _offsets(text)
    limit = None
    if p.head_end is not None:
        limit = off(p.head_end)
    elif p.body_start is not None:
        limit = off(p.body_start)
    found = None
    count = 0
    for start_pos, start_text, end_pos in p.styles:
        s = off(start_pos)
        if limit is not None and s >= limit:
            break
        if end_pos is None:  # 閉じていない <style>。置き換えると本文まで消えるので触らない
            raise _Unclosed()
        count += 1
        if found is None:
            e = off(end_pos)
            close = text.find(">", e)
            found = (s, s + len(start_text), e, (close + 1) if close != -1 else len(text))
    return found, limit, count


def check(path, date, fix):
    """(status, reason, new_text) を返す。new_text が None なら書かない。"""
    if not os.path.isfile(path):
        return "no_review", None, None
    with io.open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    css = _canonical()
    try:
        found, insert_at, count = _locate(text)
    except _Unclosed:
        return "error", "unclosed_style", None
    current = text[found[1]:found[2]] if found else None
    # 古い世代のページはそのまま残す（最古の世代は補修用の2つ目の <style> を元から持つ）
    if (date < CANONICAL_SINCE and current is not None
            and _fingerprint(current) in KNOWN_OLD_GENERATIONS):
        return "mismatch", "before_canonical", None
    if count > 1:
        # 2つ目の <style> が正規の規則を上書きしうる。どれを残すかは機械で決めない
        return "mismatch", "extra_style:%d" % count, None
    if current is not None and _same(current, css):
        return "ok", None, None
    if not fix:
        return "mismatch", (None if current is not None else "no_style"), None
    if found:
        new_text, reason = text[:found[0]] + _element(css) + text[found[3]:], "replaced"
    elif insert_at is not None:
        new_text, reason = text[:insert_at] + _element(css) + "\n" + text[insert_at:], "inserted"
    else:
        return "error", "no_style_no_head", None
    # 書く前に読み直して確かめる（本物の head の中に正規の <style> があるか）
    try:
        again, _, again_count = _locate(new_text)
    except _Unclosed:
        again, again_count = None, 0
    if not again or again_count != 1 or not _same(new_text[again[1]:again[2]], css):
        return "error", "verify_failed", None
    return "fixed", reason, new_text


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


def _line(date, status, reason):
    line = "STYLE_CHECK: date=%s status=%s" % (date, status)
    if reason:
        line += " reason=%s" % reason
    return line


def _check_all(reviews_dir, since, github):
    """公開ゲート用（build-feed.yml）: since 以降の全日付を --fix なしで調べる。書き込まない。

    ok と before_canonical 以外の日を ::warning で出す（--github のとき）。1日でもあれば exit 1
    （ワークフロー側は continue-on-error。公開は止めない＝開示チェックと同じ扱い）。
    """
    try:
        names = sorted(n for n in os.listdir(reviews_dir) if n.endswith(".html"))
    except OSError as e:
        print(_line("-", "error", "no_reviews_dir:" + type(e).__name__))
        return 1
    bad = 0
    for name in names:
        date = name[:-len(".html")]
        if not DATE_RE.match(date) or date < since:
            continue
        try:
            status, reason, _ = check(os.path.join(reviews_dir, name), date, False)
        except Exception as e:
            status, reason = "error", type(e).__name__
        line = _line(date, status, reason)
        print(line)
        if status != "ok" and reason != "before_canonical":
            bad += 1
            if github:
                print("::warning title=review-style::%s" % line)
    print("STYLE_CHECK_SUMMARY: since=%s warnings=%d" % (since, bad))
    return 1 if bad else 0


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--all", action="store_true", help="公開ゲート用: --since 以降の全日付を調べる（書かない）")
    parser.add_argument("--since", default=CANONICAL_SINCE)
    parser.add_argument("--github", action="store_true", help="--all のとき ::warning を出す")
    parser.add_argument("--reviews-dir", default=os.path.join(REPO_DIR, "reviews"))
    fix = "--fix" in argv
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        if e.code == 0:
            return 0
        print("STYLE_CHECK: date=- status=error reason=bad_args")
        return 0 if fix else 1
    if args.all:
        if args.fix or args.date or not DATE_RE.match(args.since):
            print("STYLE_CHECK: date=- status=error reason=bad_args")
            return 1
        return _check_all(args.reviews_dir, args.since, args.github)
    if not args.date:
        print("STYLE_CHECK: date=- status=error reason=bad_args")
        return 0 if args.fix else 1
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
    print(_line(args.date, status, reason))
    if args.fix:
        return 0
    return 0 if status in ("ok", "no_review") else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rc = main(sys.argv[1:])
    sys.stdout.flush()
    sys.exit(rc)
