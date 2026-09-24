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
  `COMMENT_RE` / `_strip_trailing_blank_lines` をそのまま re-use し、
  同じ判定を2箇所に重複させない。

  ⚠️ **未閉鎖判定の範囲（2026-09-24 2周目差し戻し P1-2）**: 「閉じていない
  run-timing コメント」の検出は、run_timing.py の `UNCLOSED_RE` を head
  全体に適用するのではなく、footer 直前の末尾に実際に連なっている場合
  だけに限定する（`_unclosed_adjacent_to_footer`）。`UNCLOSED_RE` を head
  全体にそのまま適用すると、`<script>const x="<!-- run-timing";</script>`
  のように本文中に閉じていない同形文字列があるだけで（footer からは
  遠く離れていても）誤って parse_error にしてしまう
  （run_timing.py 自身が `insert_comment` で head 全体を見るのは
  「書き込んでよいか」の保守的な自衛であり、読み取り側の「これは計測
  コメントか」の判定に転用すると過検知になる）。

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
    **解釈の確定（2026-09-24 2周目差し戻し）**: これは「saves=0 のときの
    比率は無限大＝常に180超とみなす」という解釈で確定する（この分岐は
    現状維持・変更しない）。

  - footer 直前に居座っているが `run_timing.COMMENT_RE` に一致しない
    run-timing 様のコメント（`schema=x` のような非数値バージョン・
    `schema=` フィールド自体の欠落・許容文字外の値など）も、footer 直前で
    途切れずに閉じている（`-->` を持つ）限り status=parse_error
    reason=malformed_comment とする（2026-09-24 2周目差し戻し P1-1の3）。
    「footer直前に居座っている」の判定は、正規形コメントの位置特定
    （`</footer>` 直前・空行はまたいでよい）と同じ規約を緩い正規表現
    （`_LOOSE_COMMENT_RE`）に適用して行う——本文中の同形文字列
    （`<script>` 等）は footer 直前まで連なっていない限り拾わない
    （下記 `--since` 検証と同様、この判定も「本文中の無関係な文字列を
    誤検知しない」ことを最優先にする）。

`--since` は `datetime.date.fromisoformat` で**暦日として**検証する
（2026-09-24 2周目差し戻し P2-1）。`YYYY-MM-DD` の形はしていても
`2026-99-99` のように暦として存在しない日付は不正として扱う。不正な値は
**黙って全件を抑止せず**、無視した旨を出力に残してから since=None
（全件を警告対象）として続行する。

常に exit 0（公開を止めない。無人Routineの成果物公開ゲートの一部として使う
前提は check_disclosure.py と同じ）。
"""

import argparse
import datetime
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

# footer直前で「閉じてはいるが COMMENT_RE の許容文字/形式に合わない」
# run-timing様コメント（schema=x・schema欠落等）を見つけるための緩い正規表現。
# 1行に収まる想定は本物のコメントと同じ（改行を跨がせない）。
_LOOSE_COMMENT_RE = re.compile(r"<!-- run-timing\b[^\n]*?-->[ \t]*\r?\n?")

# 「footer直前の末尾で閉じられないまま途切れている」と判定してよいのは、
# 開始位置から先が全てこの許容文字集合（+ 空白・改行）だけで構成されている
# ときだけ。本文中の同形文字列（<script> のJS文字列・タグ・日本語本文等）が
# 続く場合はここでは弾き、no_comment（対象外）に倒す
# （2026-09-24 2周目差し戻し P1-2）。
_UNCLOSED_TAIL_ALLOWED_RE = re.compile(r"^[0-9A-Za-z_=,\- \t\r\n]*$")

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


def _matches_immediately_before(head, pattern):
    """head（`</footer>` 直前までの文字列）の**末尾に連なって**いる pattern の
    マッチを、run_timing.insert_comment と同じ剥がし方（末尾の空白のみの行を
    飛ばしながら、末尾に接するマッチだけを後ろから拾う）で集めて返す。
    本文中の同形文字列（<script> 等）は、その直後に本文が続く限り
    「末尾に接する」ことがないので混ざらない。汎用ヘルパーとして正規形
    （COMMENT_RE）と緩い形（_LOOSE_COMMENT_RE）の両方で使う。
    """
    matches = []
    candidate = head
    while True:
        candidate = run_timing._strip_trailing_blank_lines(candidate)
        found = list(pattern.finditer(candidate))
        if not found or found[-1].end() != len(candidate):
            break
        m = found[-1]
        matches.append(candidate[m.start():m.end()])
        candidate = candidate[:m.start()]
    matches.reverse()  # 出現順（古い方が先）に戻す
    return matches


def _unclosed_adjacent_to_footer(candidate):
    """`candidate`（末尾の空白のみの行を除いた head）の末尾が、閉じられない
    まま途切れた run-timing コメントで終わっているかを判定する。

    本文中の同形文字列（<script> のJS文字列リテラル等）を誤検知しないよう、
    開始位置以降のテキストが全て許容文字（英数字・`_=,- ` と空白・改行）
    だけで構成され、かつ `-->` を一切含まない場合**だけ** True にする。
    他のタグ・引用符・日本語本文などが続く場合は「本文がその後も続いている」
    ＝ footer 直前で途切れた壊れたコメントではないと判断し False を返す
    （2026-09-24 2周目差し戻し P1-2。run_timing.UNCLOSED_RE を head 全体に
    適用すると、本文中の無関係な文字列まで拾ってしまう）。
    """
    idx = candidate.rfind("<!-- run-timing")
    if idx == -1:
        return False
    tail = candidate[idx + len("<!-- run-timing"):]
    if "-->" in tail:
        return False  # 閉じている（別の理由で異常な形はここでは見ない）
    return bool(_UNCLOSED_TAIL_ALLOWED_RE.match(tail))


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

    if not _RUN_TIMING_START_RE.search(head):
        return "no_comment", {}, None

    strict = _matches_immediately_before(head, run_timing.COMMENT_RE)
    if len(strict) > 1:
        return "parse_error", {}, "multiple_comments"
    if len(strict) == 1:
        fields = _parse_fields(strict[0])
        if fields.get("schema") != "v2":
            return "parse_error", {}, "unsupported_schema"
        return "ok", fields, None

    # 正規形として footer 直前に連なるものが無い場合、その位置で
    # ①閉じられないまま途切れているか、②閉じてはいるが正規形に合わない
    # （schema=x・schema欠落等）かを見る。どちらも「計測コメントのつもりで
    # 書かれたが壊れている」とみなし parse_error にする
    # （2026-09-24 2周目差し戻し P1-1の3・P1-2）。
    candidate = run_timing._strip_trailing_blank_lines(head)
    if _unclosed_adjacent_to_footer(candidate):
        return "parse_error", {}, "malformed_comment"

    loose = _matches_immediately_before(head, _LOOSE_COMMENT_RE)
    if loose:
        return "parse_error", {}, "malformed_comment"

    # `<!-- run-timing` はあるが footer の直前に連なっていない
    # （本文中の同形文字列であり、footer とは無関係）。
    return "no_comment", {}, None


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
    except (OSError, UnicodeDecodeError) as exc:
        # review が読めない・UTF-8としてデコードできない＝計測装置
        # （このチェッカー自身）が機能しない状態。コメント無しと違って
        # 「見えていない」だけなので検知対象にする（2026-09-24 差し戻し
        # P1-1）。**この日だけ** parse_error にし、他の日の処理には
        # 影響させない（呼び出し元の --all ループも1件ごとに独立させて
        # いるので、ここで例外を投げずに握りつぶすことが二重に効く）。
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

    形式（YYYY-MM-DD）だけでなく**暦日として実在するか**まで
    `datetime.date.fromisoformat` で検証する（2026-09-24 2周目差し戻し
    P2-1）。`2026-99-99` のように形はしていても暦に存在しない日付は不正
    として扱う。不正なら**黙って全件抑止しない**——since=None（全件を
    警告対象に戻す）にした上で、無視した旨を notice に入れて呼び出し側で
    出力させる。
    """
    if raw is None:
        return None, None
    if DATE_RE.match(raw):
        try:
            datetime.date.fromisoformat(raw)
            return raw, None
        except ValueError:
            pass
    return None, (
        "RUN_TIMING_CHECK: invalid --since %r (not a real calendar date); "
        "ignored (all dates enforced)" % raw
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
        try:
            r = process_date(date, reviews_dir)
        except Exception as exc:  # noqa: BLE001 - 1ファイルの異常で
            # --all 全体を1行のエラーに潰さない（2026-09-24 2周目差し戻し
            # P1-1）。process_date 自身も読み取り/デコード例外を捕まえて
            # いるが、それ以外の予期しない例外（バグ等）に対する最後の
            # 防波堤としてもここで1件ずつ隔離する。
            r = RunTimingResult(date)
            r.status = "parse_error"
            r.warn = True
            r.reason = "unreadable:%s" % type(exc).__name__
        results.append(r)

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
