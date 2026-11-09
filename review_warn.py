#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reviews/<TARGET>.html の `</footer>` 直前に `<p class="notegen-warn">` 警告を足す
（夜間 Routine が手で書いていた3種類の警告を、1コマンドに置き換えるもの）。

なぜ作ったか: 警告を LLM が `</footer>` の直前へ手で足すと、finish（run_timing.py）の
後や同日再実行のときに、計測コメントと `</footer>` の間へ挟まる。すると次の
run_timing.insert_comment が既存0本と判定して2本目を足し、test.yml の
test_real_review_html_is_preserved が赤になる。書く位置を merge_review の規則
（**既存の run-timing コメントより前**）に揃えれば、いつ呼んでも計測コメントは
`</footer>` の直前に1本のまま残る。

使い方（cwd はリポ直下）:
  python3 review_warn.py --kind reel --target YYYY-MM-DD
  python3 review_warn.py --kind note_verify --target YYYY-MM-DD --rids 1234,5678
  python3 review_warn.py --kind merge_skipped --target YYYY-MM-DD

種類:
  - reel: 同じプロセスで reel_health.compute_status() を1回だけ呼ぶ。`REEL_VIDEO_STATUS:`
    行は reel_health と同じ字面で出す（手順7ではこのコマンドが reel_health.py の代わり）。
    warn=yes なら警告を書く／最新の判定で置き換える。facts を読めて warn=no なら既存の Reel 警告を消す
    （facts が無い・読めない・記録無効の夜は判定材料が無いので何もしない）。
  - note_verify: `--rids`（数字をカンマで繋いだものだけ）。既存の行があれば rid を足し合わせて1本にする。
  - merge_skipped: 固定の文面。既にあれば足さない。

同種の警告は常に1本（既存の行は本文の書き出しで見分ける。手書きでも同じ）。
既存の警告が計測コメントと `</footer>` の間に挟まった旧型の壊れ方をしていれば、呼んだついでに
コメントの前へ移す（result=replaced reason=moved_before_timing_comment）。照合は `<footer>` の内側だけ。

扱わない警告（理由）: 手順2の `IMPORT_STATUS: INCOMPLETE`・手順3の `warn=unreadable` の
警告は、手順5で `/tmp/review_new.html` の側へ書く。reviews へは merge_review.py が
計測コメントより前に差し込むので、順序で壊れない（finish より前の手順）。

出力: 必ず1行
  WARN_STATUS: kind=<k> result=written|replaced|removed|unchanged|skipped|error reason=<理由>
終了コードは常に 0（無人ランを止めない。run_timing.py・reel_health.py と同じ流儀）。
`</footer>` が文書中にちょうど1個でないとき（script や pre の中のものも数える）は書かない（skipped）。
一時ファイル＋os.replace で書き、改行コードは元のまま保つ。標準ライブラリだけ。
"""

import argparse
import io
import os
import re
import sys

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import merge_review  # noqa: E402 - 書く位置の規則の正本（写しは作らない）
import reel_health  # noqa: E402

KINDS = ("reel", "note_verify", "merge_skipped")
# 既存行を種類ごとに見分ける本文の書き出し（先頭の ⚠️ と空白は無視して比べる）
MARKERS = {
    "reel": "Instagram Reel の動画取得",
    "note_verify": "ノート生成: 検証失敗",
    "merge_skipped": "reviews構造検証エラー",
}
MERGE_SKIPPED_TEXT = ("⚠️ reviews構造検証エラーのため今回の統合をスキップ"
                      "（既存カードは保持済み・新規分は未反映の可能性あり）")
RIDS_RE = re.compile(r"[0-9]{1,20}(,[0-9]{1,20})*\Z")  # \d は全角数字も通す
WARN_RE = re.compile(r'<p class="notegen-warn">([^<]*)</p>')


def _marker_matches(body, kind):
    return body.lstrip("⚠️ \t").startswith(MARKERS[kind])


def _wrap(text):
    return '<p class="notegen-warn">%s</p>' % text


def _reel_text(status):
    if not reel_health.should_warn(status):
        return None
    detail = ", ".join("%s %d件" % (k, v) for k, v in status["reasons"].items())
    return ("⚠️ Instagram Reel の動画取得が全滅（%d件中0件成功・内訳: %s）。"
            "中継サービスの死亡を疑うこと" % (status["total"], detail))


def _note_text(rids):
    return "⚠️ ノート生成: 検証失敗 %d件（rid: %s）" % (len(rids), ", ".join(rids))


def _span(html, m):
    """マッチを消すときの範囲。行に単独で載っていれば行ごと消す。"""
    s, e = m.start(), m.end()
    ls = s
    while ls > 0 and html[ls - 1] in " \t":
        ls -= 1
    le = e
    while le < len(html) and html[le] in " \t":
        le += 1
    if (ls == 0 or html[ls - 1] == "\n") and html[le:le + 2] == "\r\n":
        return ls, le + 2
    if (ls == 0 or html[ls - 1] == "\n") and html[le:le + 1] == "\n":
        return ls, le + 1
    return s, e


def apply_warning(html, kind, new_text=None, rids=()):
    """(新HTML or None, result, reason) を返す。ファイルには触らない。
    new_text: reel/merge_skipped の本文（reel で None は「警告なし＝既存を消す」）。
    note_verify は rids（文字列のリスト）から組み立てる。
    """
    footer_idx, insert_at = merge_review._footer_insertion_point(html)
    if insert_at is None:
        return None, "skipped", "footer_not_unique"
    open_idx = html.rfind("<footer", 0, footer_idx)
    if open_idx < 0:
        return None, "skipped", "footer_open_not_found"
    # 旧型の事故（警告が計測コメントと </footer> の間に挟まった）を直す: コメントより前へ移す。
    # 照合は <footer> の内側だけ（カード本文や script 内の同形文字列を拾わない）
    cm = None  # 後ろが警告と空白だけ（1本以上の警告）になっている計測コメント
    for c in merge_review._RUN_TIMING_COMMENT_RE.finditer(html, open_idx, footer_idx):
        rest = html[c.end():footer_idx]
        if WARN_RE.search(rest) and not WARN_RE.sub("", rest).strip(" \t\r\n"):
            cm = c
    if cm is not None:
        moved = list(WARN_RE.finditer(html, cm.end(), footer_idx))
        keep = html
        for m in reversed(moved):
            s, e = _span(keep, m)
            keep = keep[:s] + keep[e:]
        eol = "\r\n" if "\r\n" in html else "\n"
        keep = keep[:cm.start()] + "".join(m.group(0) + eol for m in moved) + keep[cm.start():]
        out, result, reason = apply_warning(keep, kind, new_text, rids)
        if result == "unchanged":
            return out, "replaced", "moved_before_timing_comment"
        return out, result, reason
    mine = [m for m in WARN_RE.finditer(html, open_idx, footer_idx)
            if _marker_matches(m.group(1), kind)]

    if kind == "note_verify":
        merged = []
        for m in mine:
            merged += re.findall(r"[0-9]+", m.group(1).split("rid:", 1)[-1])
        for r in list(rids):
            if r not in merged:
                merged.append(r)
        new_text = _note_text(merged)
    elif kind == "merge_skipped":
        new_text = MERGE_SKIPPED_TEXT

    if new_text is None:  # reel で warn=no
        if not mine:
            return html, "unchanged", "no_warn_no_line"
        out = html
        for m in reversed(mine):
            s, e = _span(out, m)
            out = out[:s] + out[e:]
        return out, "removed", "warn_no"

    if not mine:
        eol = "\r\n" if "\r\n" in html else "\n"
        lead = "" if html[:insert_at].endswith("\n") or insert_at == 0 else eol
        return (html[:insert_at] + lead + _wrap(new_text) + eol + html[insert_at:],
                "written", "inserted_before_timing_comment")

    if len(mine) == 1 and mine[0].group(1) == new_text:
        return html, "unchanged", "same_text"
    if kind == "merge_skipped" and len(mine) == 1:
        return html, "unchanged", "already_present"
    # 先頭の1本を新しい文面に、残りは消す（同種は1本）。後ろから適用してずれを避ける
    out = html
    for i, m in reversed(list(enumerate(mine))):
        if i == 0:
            out = out[:m.start()] + _wrap(new_text) + out[m.end():]
        else:
            s, e = _span(out, m)
            out = out[:s] + out[e:]
    return out, "replaced", "latest_text"


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _status(kind, result, reason):
    reason = re.sub(r"\s+", "_", str(reason))
    print("WARN_STATUS: kind=%s result=%s reason=%s" % (kind, result, reason))


def build_arg_parser():
    p = argparse.ArgumentParser(description="reviews の footer 直前に警告を1本だけ足す")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--target", default=None,
                   help="対象日 YYYY-MM-DD（reel_health と同じ解決・書式検証）")
    p.add_argument("--rids", default=None, help="note_verify 用。数字をカンマで繋ぐ（例 1234,5678）")
    p.add_argument("--reviews", default=None,
                   help="書き込む reviews のパス（省略時: <リポ>/reviews/<TARGET>.html）")
    return p


def main(argv=None):
    kind = "unknown"
    try:
        args = build_arg_parser().parse_args(argv)
        kind = args.kind
        target, ignored = reel_health.resolve_target(args.target)
        new_text, rids = None, []
        if kind == "reel":
            # 判定は1回だけ。行は reel_health と同じ字面で出す
            status = reel_health.compute_status(target)
            reel_health.print_status_line(target, status, ignored)
            new_text = _reel_text(status)
            if status["note"] is not None:  # 判定材料が無い夜は既存の警告を消さない
                _status(kind, "unchanged", "no_facts_keep_existing")
                return 0
        elif kind == "note_verify":
            if not args.rids or not RIDS_RE.match(args.rids):
                _status(kind, "error", "invalid_rids")
                return 0
            rids = args.rids.split(",")
        path = args.reviews or os.path.join(REPO_DIR, "reviews", "%s.html" % target)
        if not os.path.isfile(path):
            _status(kind, "skipped", "no_review_file")
            return 0
        with io.open(path, encoding="utf-8", newline="") as f:
            html = f.read()
        out, result, reason = apply_warning(html, kind, new_text, rids)
        if out is not None and out != html:
            _atomic_write(path, out)
        _status(kind, result, reason)
    except SystemExit as exc:  # argparse の exit(2) も 0 で返す
        _status(kind, "error", "argument_parsing_failed_exit=%s" % exc.code)
    except Exception as exc:  # noqa: BLE001 - 完走優先。reviews は書き込み前なら無傷
        _status(kind, "error", "%s:%s" % (type(exc).__name__, exc))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
