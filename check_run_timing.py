#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""夜間Routineの所要時間コメント（run_timing.py が書く `run-timing` コメント）を
機械的に読み、所要時間の異常な膨張を検知するチェッカー（capture-pipeline
成功条件(b)）。

⚠️ **これは異常検知の閾値であり性能SLAではない**（「1ランの所要時間が20分以内」
というSLA的な数値は2026-09-08に撤回済み。ここで使う閾値は「これを超えたら
普段と様子が違うので人が見る」という異常検知のトリガーであって、遵守すべき
性能目標ではない）。

入力（正本は run_timing.py。パーサをここで独自に作らない）:
  run_timing.py が書くコメントは常に「文書中で唯一の `</footer>` の直前」に
  ある（run_timing.insert_comment の契約。footer が複数・無い場合や、直前が
  正規形コメントでない場合は run_timing.py 自身が書き込みを拒否する）。
  ここでの読み取りもその**同じ位置**だけを見る（2026-09-24 差し戻し P1-2）。
  本文中の `<script>`/`<pre>` 等に同形の文字列（`<!-- run-timing ... -->`）が
  あっても、それが `</footer>` の直前でなければ拾わない——これは
  run_timing.py 側が「消してよいのは footer 直前の正規形コメントだけ」という
  制約を守っている（test_run_timing.py の
  `test_same_looking_string_inside_script_is_not_touched` 等）のと対称的に、
  読み取り側も同じ位置規約を守ることで、本文中の同形文字列を誤って計測結果
  として読んでしまう事故を防ぐ。位置特定そのものは run_timing.py の
  `COMMENT_RE` / `UNCLOSED_RE` / `_strip_trailing_blank_lines` をそのまま
  re-use し、同じ判定を2箇所に重複させない。

判定:
  - footer 直前に run-timing コメントが**無い** review は対象外（計測導入前
    の日、または本文中に同形の文字列があるだけの場合を含む。--since で
    日付下限も別途指定できる）。
  - footer 直前のコメントが壊れている（`<!-- run-timing` はあるが正規形として
    閉じていない）・複数件並んでいる（本来 run_timing.py は1件しか書かない
    ので、複数あること自体が異常）・schema が v2 でない・review が読めない
    場合は status=parse_error とする。**これらは「計測装置そのものの故障」
    であり検知対象**（2026-09-24 差し戻し P1-1）: --github 指定時は
    ::warning を出す。no_comment（コメント自体が無い＝計測導入前）だけは
    引き続き対象外のまま warning を出さない。
  - `status` が `complete`/`incomplete` のどちらでもない値（コメントは正規形
    として読めたが、build_comment が本来出さないはずの値が入っている）は
    異常として reason=unknown_status で警告する（2026-09-24 差し戻し P1-3。
    「疑わしきは異常として報告する」という run_timing.py 自身の設計原則3と
    同じ姿勢を読み取り側にも適用する）。
  - `status=incomplete` → 常に ::warning（reason= の中身をそのまま出す）。
  - `status=complete` かつ `total_s/saves > 180` かつ `total_s > 1200` の
    **両方**を満たすとき → ::warning。
    `saves=0` は除算しない。0除算を避けるためだけでなく、"1件あたり秒数"
    という指標自体が saves=0 のときは意味を持たない（分母が無い）ため、
    この場合は `total_s > 1200` の単独条件で警告するかどうかを決める:
    **単独条件で警告する**を採用した。理由: saves=0 は「その夜 reviews へ
    書くべき新規保存が無かった」という意味であり、その状態で total_s が
    絶対閾値（1200秒=20分）を超えているならそれ自体が既に異常（何に
    2/3時間近く使ったのか、1件あたりコストでは説明できない）。両方の
    条件を課すと saves=0 の夜だけ実質チェックを無効化してしまう。

`--since` は YYYY-MM-DD 形式を検証する。不正な値は**黙って全件を抑止せず**、
無視した旨を出力に残してから since=None（全件を警告対象）として続行する
（2026-09-24 差し戻し P2-1）。

常に exit 0（公開を止めない。無人Routineの成果物公開ゲートの一部として使う
前提は check_disclosure.py と同じ）。
"""

import argparse
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_timing  # noqa: E402 - コメントの位置特定・正規形の判定を re-use する

DATE_RE = re.compile(r"^(\d{4}-\d\d-\d\d)$")

# `<!-- run-timing` で始まる箇所を素朴に数えるための検出用（footer直前領域の
# 中で「開始したのに正規形として閉じなかったコメント」を見分けるのに使う）。
# 正規形かどうかの判定は run_timing.COMMENT_RE の側に一任し、ここでは複製しない。
_RUN_TIMING_START_RE = re.compile(r"<!-- run-timing")

# saves/total_s が無いのに status=complete になっている（本来 build_comment の
# 仕様上は起き得ないが、コメントが手で書き換えられた等の異常に備える）ときの
# 扱い。ここでは数値を捏造せず、複数コメント・壊れたコメントと同じ
# 「値を使わず parse_error」に倒す。
_INT_RE = re.compile(r"^-?\d+$")

RATIO_THRESHOLD_S = 180   # 1件（save）あたりの所要秒。これ超過が異常判定の一部条件。
ABS_FLOOR_S = 1200        # 20分。ratio条件とAND、saves=0のときは単独条件になる。


class RunTimingResult(object):
    def __init__(self, date):
        self.date = date
        self.status = "ok"          # ok / no_comment / parse_error
        self.fields = {}
        self.warn = False
        self.reason = None

    def to_line(self):
        if self.status == "no_comment":
            return "RUN_TIMING_RESULT: date=%s status=no_comment" % self.date
        if self.status == "parse_error":
            return (
                "RUN_TIMING_RESULT: date=%s status=parse_error warn=%s reason=%s"
                % (self.date, "yes" if self.warn else "no", self.reason or "unknown")
            )
        return (
            "RUN_TIMING_RESULT: date=%s status=%s warn=%s reason=%s"
            % (self.date, self.fields.get("status", "unknown"),
               "yes" if self.warn else "no", self.reason or "-")
        )


def _parse_fields(comment_text):
    """`<!-- run-timing ... -->` 1件（run_timing.COMMENT_RE が拾った正規形）から
    key=value の辞書を作る。値の妥当性はここでは検証しない（呼び出し側が
    使う直前に個別チェックする）。
    """
    inner = comment_text.strip()
    inner = inner[len("<!-- run-timing"):].rstrip()
    if inner.endswith("-->"):
        inner = inner[: -len("-->")]
    fields = {}
    for token in inner.split():
        if "=" not in token:
            continue
        key, _, val = token.partition("=")
        fields[key] = val
    return fields


def _footer_head(html_text):
    """`</footer>` が文書中に唯一のときだけ、その直前までの文字列を返す。
    それ以外（無い・複数ある）は None（run_timing.insert_comment が
    書き込み自体を拒否するのと同じ状況なので、位置を特定できない）。
    """
    if html_text.count("</footer>") != 1:
        return None
    idx = html_text.find("</footer>")
    return html_text[:idx]


def _comments_immediately_before(head):
    """head（`</footer>` 直前までの文字列）の**末尾に連なって**いる run-timing
    の正規形コメントを、run_timing.insert_comment と同じ剥がし方
    （末尾の空白のみの行を飛ばしながら、末尾に接する正規形コメントだけを
    後ろから拾う）で集めて返す。本文中の同形文字列（<script> 等）は、その
    直後に本文が続く限り「末尾に接する」ことがないので混ざらない。
    """
    matches = []
    candidate = head
    while True:
        candidate = run_timing._strip_trailing_blank_lines(candidate)
        found = list(run_timing.COMMENT_RE.finditer(candidate))
        if not found or found[-1].end() != len(candidate):
            break
        m = found[-1]
        matches.append(candidate[m.start():m.end()])
        candidate = candidate[:m.start()]
    matches.reverse()  # 出現順（古い方が先）に戻す
    return matches


def read_run_timing_comment(html_text):
    """(status, fields, reason) を返す。

    見る位置は run_timing.py が書き込む位置（唯一の `</footer>` の直前）と
    完全に一致させる。本文中の同形文字列は拾わない。

    status は "ok"（footer直前に正規形コメントが1件だけ読めた）/
    "no_comment"（footer直前にコメントが無い＝対象外。`</footer>` が
    唯一でない場合もここに含める——run_timing.py 自身がその状態では
    書き込みを拒否するため、コメントが無いのと同じ扱いにする）/
    "parse_error"（footer直前で壊れている・複数ある・schema が v2 でない）
    のいずれか。
    """
    head = _footer_head(html_text)
    if head is None:
        return "no_comment", {}, None

    starts = list(_RUN_TIMING_START_RE.finditer(head))
    if not starts:
        return "no_comment", {}, None

    if run_timing.UNCLOSED_RE.search(head):
        # footer より前に閉じていない run-timing コメントがある＝
        # run_timing.py 自身もこの状態では書き込みを拒否する壊れ方。
        return "parse_error", {}, "malformed_comment"

    valid = _comments_immediately_before(head)
    if not valid:
        # `<!-- run-timing` はあるが footer の直前に連なっていない
        # （本文中の同形文字列、または直前だが正規形でない）。
        return "no_comment", {}, None
    if len(valid) > 1:
        return "parse_error", {}, "multiple_comments"

    fields = _parse_fields(valid[0])
    if fields.get("schema") != "v2":
        return "parse_error", {}, "unsupported_schema"
    return "ok", fields, None


def evaluate(fields):
    """(warn, reason) を返す。fields は read_run_timing_comment が返す辞書。"""
    status = fields.get("status")
    if status == "incomplete":
        reason = fields.get("reason", "unknown")
        return True, "incomplete:%s" % reason
    if status != "complete":
        # complete/incomplete のどちらでもない値。build_comment は本来
        # この値を出さないので、「疑わしきは異常」として報告する
        # （run_timing.py 自身の設計原則3と同じ姿勢。2026-09-24 P1-3）。
        return True, "unknown_status:%s" % (status if status else "missing")

    saves_raw = fields.get("saves")
    total_raw = fields.get("total_s")
    if not (_INT_RE.match(saves_raw or "") and _INT_RE.match(total_raw or "")):
        # complete を名乗るのに saves/total_s が数値でない＝コメントが
        # 本来ありえない形。数値を捏造せず異常として報告する。
        return True, "malformed_complete_fields"

    saves = int(saves_raw)
    total_s = int(total_raw)
    if saves < 0 or total_s < 0:
        return True, "malformed_complete_fields"

    if saves == 0:
        if total_s > ABS_FLOOR_S:
            return True, "total_s=%d>%d(saves=0)" % (total_s, ABS_FLOOR_S)
        return False, None

    ratio = total_s / saves
    if ratio > RATIO_THRESHOLD_S and total_s > ABS_FLOOR_S:
        return True, "total_s=%d saves=%d ratio=%.1f>%d and total_s>%d" % (
            total_s, saves, ratio, RATIO_THRESHOLD_S, ABS_FLOOR_S)
    return False, None


def process_date(date, reviews_dir):
    result = RunTimingResult(date)
    path = os.path.join(reviews_dir, "%s.html" % date)
    if not os.path.isfile(path):
        result.status = "no_comment"
        return result
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            html_text = fh.read()
    except OSError as exc:
        # review が読めない＝計測装置（このチェッカー自身）が機能しない状態。
        # コメント無しと違って「見えていない」だけなので検知対象にする
        # （2026-09-24 差し戻し P1-1）。
        result.status = "parse_error"
        result.warn = True
        result.reason = "unreadable:%s" % type(exc).__name__
        return result

    status, fields, reason = read_run_timing_comment(html_text)
    result.status = status
    result.fields = fields
    if status == "parse_error":
        result.warn = True
        result.reason = reason
        return result
    if status != "ok":
        result.reason = reason
        return result

    warn, warn_reason = evaluate(fields)
    result.warn = warn
    result.reason = warn_reason
    return result


def collect_dates(args, reviews_dir):
    dates = []
    if args.date:
        dates.extend(args.date)
    if args.all:
        for path in sorted(glob.glob(os.path.join(reviews_dir, "*.html"))):
            stem = os.path.basename(path)[:-5]
            if DATE_RE.match(stem):
                dates.append(stem)
    seen = set()
    ordered = []
    for d in dates:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


def _validate_since(raw):
    """(since, notice) を返す。raw が None ならそのまま (None, None)。
    形式が不正なら**黙って全件抑止しない**——since=None（全件を警告対象に
    戻す）にした上で、無視した旨を notice に入れて呼び出し側で出力させる
    （2026-09-24 差し戻し P2-1）。
    """
    if raw is None:
        return None, None
    if DATE_RE.match(raw):
        return raw, None
    return None, (
        "RUN_TIMING_CHECK: invalid --since %r; ignored (all dates enforced)"
        % raw
    )


def build_arg_parser():
    p = argparse.ArgumentParser(description="run-timing 異常検知チェッカー")
    p.add_argument("--date", action="append", help="対象日（複数可）")
    p.add_argument("--all", action="store_true", help="reviews 全日を対象")
    p.add_argument("--since", help="この日付以降のみ ::warning を出す（--all併用）")
    p.add_argument("--github", action="store_true", help="::warning 出力")
    p.add_argument("--reviews-dir", default="reviews")
    return p


def main(argv=None):
    try:
        return _main(argv)
    except SystemExit as exc:
        print("RUN_TIMING_CHECK: argument parsing failed (exit=%s)" % exc.code)
        return 0
    except Exception as exc:  # noqa: BLE001 - gateを止めないため必ず0で返す
        print("RUN_TIMING_CHECK: error %s: %s" % (type(exc).__name__, exc))
        return 0


def _main(argv):
    args = build_arg_parser().parse_args(argv)
    reviews_dir = args.reviews_dir
    dates = collect_dates(args, reviews_dir)
    if not dates:
        print("RUN_TIMING_CHECK: status=no_dates")
        return 0

    since, since_notice = _validate_since(args.since)
    if since_notice:
        print(since_notice)

    results = []
    for date in sorted(dates):
        results.append(process_date(date, reviews_dir))

    warned = 0
    checked = 0
    for r in results:
        print(r.to_line())
        if r.status == "no_comment":
            continue
        checked += 1
        enforce = (since is None) or (r.date >= since)
        if r.warn and enforce:
            warned += 1
            line = "RUN_TIMING_VIOLATION: date=%s status=%s reason=%s" % (
                r.date, r.status, r.reason)
            print(line)
            if args.github:
                print("::warning title=run-timing::%s" % line)

    print("RUN_TIMING_CHECK: checked=%d warned=%d skipped=%d" % (
        checked, warned, len(results) - checked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
