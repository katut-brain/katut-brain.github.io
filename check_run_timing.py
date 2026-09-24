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
  reviews/*.html の `</footer>` 直前に `run_timing.py` が書く
  `<!-- run-timing schema=v2 status=... target=... run_id=... saves=N
   step..._s=... total_s=... -->` を1件だけ読む。コメントの構文（許容文字・
  1行に収まった正規形）は run_timing.COMMENT_RE をそのまま re-use する
  （同じ書式判定を2箇所に書かない）。この関数が拾わない=「壊れている」
  とみなし、値を無理に読もうとしない（run_timing.py 自身の設計原則3
  「疑わしきは incomplete」と同じ姿勢）。

判定:
  - コメントが無い review は対象外（計測導入前の日。--since で日付下限も
    別途指定できる）。
  - コメントが壊れている（`<!-- run-timing` はあるが正規形として閉じて
    いない）・同一ファイルに複数件ある（本来 run_timing.py は1件しか
    書かないので、複数あること自体が異常）場合は status=parse_error とし、
    値を使わずに終える（誤った数値で警告も非警告も出さない）。
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
import run_timing  # noqa: E402 - コメントの正規形（COMMENT_RE）を re-use する

DATE_RE = re.compile(r"^(\d{4}-\d\d-\d\d)$")

# `<!-- run-timing` で始まる箇所を素朴に数えるための検出用（壊れ・複数検知に使う）。
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
            return "RUN_TIMING_RESULT: date=%s status=parse_error reason=%s" % (
                self.date, self.reason or "unknown")
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


def read_run_timing_comment(html_text):
    """(status, fields, reason) を返す。

    status は "ok"（正常に1件だけ読めた）/ "no_comment"（対象外） /
    "parse_error"（壊れている・複数ある・schema が v2 でない）のいずれか。
    """
    starts = list(_RUN_TIMING_START_RE.finditer(html_text))
    if not starts:
        return "no_comment", {}, None

    valid = list(run_timing.COMMENT_RE.finditer(html_text))
    # COMMENT_RE は末尾の空白・改行込みでマッチするが、開始位置の集合で
    # 「開始したのに正規形として閉じなかったコメントが無いか」を確認する。
    valid_starts = {m.start() for m in valid}
    all_starts = {m.start() for m in starts}
    if valid_starts != all_starts:
        return "parse_error", {}, "malformed_comment"
    if len(valid) == 0:
        return "parse_error", {}, "malformed_comment"
    if len(valid) > 1:
        return "parse_error", {}, "multiple_comments"

    fields = _parse_fields(valid[0].group(0))
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
        return False, None

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
        result.status = "parse_error"
        result.reason = "unreadable:%s" % type(exc).__name__
        return result

    status, fields, reason = read_run_timing_comment(html_text)
    result.status = status
    result.fields = fields
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

    since = args.since
    results = []
    for date in sorted(dates):
        results.append(process_date(date, reviews_dir))

    warned = 0
    checked = 0
    for r in results:
        print(r.to_line())
        enforce = (since is None) or (r.date >= since)
        if r.status == "ok":
            checked += 1
            if r.warn and enforce:
                warned += 1
                line = "RUN_TIMING_VIOLATION: date=%s reason=%s" % (r.date, r.reason)
                print(line)
                if args.github:
                    print("::warning title=run-timing::%s" % line)

    print("RUN_TIMING_CHECK: checked=%d warned=%d skipped=%d" % (
        checked, warned, len(results) - checked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
