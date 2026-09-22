#!/usr/bin/env python3
# select_targets.py — クラウドRoutine 手順3の振り返り対象を選び直すための選定スクリプト。
#
# 旧仕様（手順3）は「captures.json の date == TARGET」だけを対象にしていたため、
# 過去に一度も reviews/*.html へ載らなかった保存（push失敗・生成スキップ等）が
# 永久に取りこぼされていた。このスクリプトは「captures.json に存在するレコードの
# うち、reviews/*.html のどのカードの data-rid にも載っていないもの全て」を対象に
# 選び直す（TARGET の日付そのものは reviews/<TARGET>.html のファイル名としてのみ
# 使われ続ける。母集団の絞り込みには使わない）。
#
# 手順書（cloud_routine_prompt.md）の書き換えは別担当。ここでは選定ロジックのみ。
#
# 設計は backfill.py / build_capture_index.py / check_disclosure.py の流儀に合わせる:
#   - JST は datetime.timezone(timedelta(hours=9)) で固定計算（ledger.py 等と同じ）。
#   - 例外はすべて握りつぶし、どんな失敗でも exit 0 で SELECT_STATUS 行を出す
#     （無人Routineのgateを止めないため。backfill.py の main() と同じ設計）。
#   - JSON読み込みは open(..., encoding="utf-8") + 失敗を許容するヘルパー
#     （build_capture_index.py の _load_json と同じ書き方）。
#
# rid が無いレコードの扱い（判断・コメントとして残す）:
#   build_capture_index.py の synthetic_rid() は「rid の無いレコードだけの日が
#   台帳から消える」事故を防ぐための合成負整数だが、このスクリプトの目的は
#   「本文材料のある未掲載レコードを拾って reviews へ書く」ことであり、
#   reviews のカードの data-rid は常に captures.json 由来の実 rid（正の整数、
#   手順5 のテンプレートで `data-rid="RAINDROP_ID"` として逐語コピーされる）しか
#   持ち得ない。rid の無いレコードを合成IDで選定対象に混ぜると、実際には
#   reviews 側で data-rid を書きようがないため「選ばれたのに永久に掲載済み
#   判定へ辿り着けない」レコードを量産する。よってここでは **rid の無いレコードは
#   選定対象から除外し、件数だけ captures_no_rid として報告する**（synthetic_rid
#   は使わない）。
#
# capture_index.json との関係: 参考情報として「captures.json には無いが
# capture_index.json にはある rid」を数える（index_only）。これは過去に captures.json
# から消えた（Raindrop側で削除等）が一度は観測された rid で、本文材料が無いので
# 選定はしない。

import sys
import os
import io
import json
import re
import glob
import argparse
import datetime
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JST = datetime.timezone(datetime.timedelta(hours=9))

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
CAPTURES = os.path.join(REPO_DIR, "captures.json")
CAPTURE_INDEX = os.path.join(REPO_DIR, "capture_index.json")
REVIEWS_DIR = os.path.join(REPO_DIR, "reviews")

# reviews/*.html の data-rid 抽出。check_disclosure.py の RID_ATTR_RE
# (r"""data-rid\s*=\s*["'](\d+)["']""") に合わせつつ、符号付き数値も拾えるように
# 拡張する（captures.json 側の rid は常に正の整数だが、抽出ロジックを壊す
# リグレッションテスト — 引用符の種類違い・負数・複数カード — に対して
# 頑健であることを別途テストするため、正規表現自体は負号も許容しておく）。
RID_ATTR_RE = re.compile(r"""data-rid\s*=\s*["'](-?\d+)["']""")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _load_json(path):
    try:
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _default_target():
    now = datetime.datetime.now(JST)
    yesterday = now.date() - datetime.timedelta(days=1)
    return yesterday.isoformat()


def load_captures(path=CAPTURES):
    """captures.json を読み、(records, no_rid_count) を返す。

    トップレベルは list（実測）または {"captures": [...]} のどちらでも許容する
    （build_capture_index.py の captures_by_day() と同じ寛容さ）。
    """
    data = _load_json(path)
    records = data if isinstance(data, list) else (data or {}).get("captures", [])
    if not isinstance(records, list):
        records = []
    out = []
    no_rid = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rid = rec.get("rid")
        if not isinstance(rid, int):
            # raindrop_id をフォールバックで見る（captures.json の実測フィールド名は
            # "rid" だが、他スクリプト（backfill等）は "raindrop_id" も見ているため
            # 念のため両対応。両方無ければ rid 無しとして除外する。
            rid = rec.get("raindrop_id")
        if not isinstance(rid, int):
            no_rid += 1
            continue
        date = rec.get("date")
        if not isinstance(date, str) or len(date) < 10:
            continue
        try:
            datetime.date.fromisoformat(date[:10])
        except ValueError:
            continue
        out.append((rid, date[:10], rec))
    return out, no_rid


def reviewed_rids(reviews_dir=REVIEWS_DIR):
    """全 reviews/*.html から data-rid の集合を作る。

    戻り値: (rids: set[int], unreadable_count: int, unreadable_files: list[str])
    読めないファイル（存在確認後の OSError・UnicodeDecodeError 等）は
    unreadable として数え、rid 抽出をスキップする（そのファイルに載っていた
    可能性がある rid を誤って「未掲載」にしてしまう危険が残るため、呼び出し側の
    STATUS 行で warn を出す）。
    """
    rids = set()
    unreadable = []
    if not os.path.isdir(reviews_dir):
        return rids, unreadable
    for path in sorted(glob.glob(os.path.join(reviews_dir, "*.html"))):
        try:
            with io.open(path, encoding="utf-8", newline="") as fh:
                raw = fh.read()
        except (OSError, UnicodeDecodeError, ValueError):
            unreadable.append(os.path.basename(path))
            continue
        for m in RID_ATTR_RE.finditer(raw):
            try:
                rids.add(int(m.group(1)))
            except ValueError:
                continue
    return rids, unreadable


def index_only_rids(captures_records, capture_index_path=CAPTURE_INDEX):
    """capture_index.json にはあるが captures.json には無い rid の件数を返す
    （参考情報。選定はしない — 本文材料が capture_index.json 側には無いため）。
    """
    idx = _load_json(capture_index_path)
    if not isinstance(idx, dict):
        return 0
    days = idx.get("days")
    if not isinstance(days, dict):
        return 0
    index_rids = set()
    for entry in days.values():
        if not isinstance(entry, dict):
            continue
        rl = entry.get("rids")
        if not isinstance(rl, list):
            continue
        for r in rl:
            if isinstance(r, int):
                index_rids.add(r)
    captures_rids = set(rid for rid, _date, _rec in captures_records)
    return len(index_rids - captures_rids)


def select(target, captures_path=CAPTURES, reviews_dir=REVIEWS_DIR,
           capture_index_path=CAPTURE_INDEX):
    """選定本体。戻り値は dict（呼び出し側が JSON化・STATUS整形する）。
    どんな例外も投げずに済むよう、呼び出し側 main() で最終的に捕捉する。"""
    captures_records, no_rid = load_captures(captures_path)
    reviewed, unreadable = reviewed_rids(reviews_dir)
    idx_only = index_only_rids(captures_records, capture_index_path)

    selected = []
    by_date = {}
    past = 0
    for rid, date, rec in captures_records:
        if rid in reviewed:
            continue
        selected.append((rid, date, rec))
        by_date[date] = by_date.get(date, 0) + 1
        if isinstance(target, str) and DATE_RE.match(target) and date < target:
            past += 1

    # 並び順: date 昇順 → rid 昇順（安定）。
    selected.sort(key=lambda t: (t[1], t[0]))

    return {
        "target": target,
        "rids": [rid for rid, _date, _rec in selected],
        "records": [rec for _rid, _date, rec in selected],
        "by_date": by_date,
        "_selected_count": len(selected),
        "_past_count": past,
        "_reviewed_count": len(reviewed),
        "_captures_count": len(captures_records),
        "_captures_no_rid": no_rid,
        "_index_only_count": idx_only,
        "_unreadable_reviews": unreadable,
    }


def main(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--target", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--captures", default=CAPTURES)
    parser.add_argument("--reviews-dir", default=REVIEWS_DIR)
    parser.add_argument("--capture-index", default=CAPTURE_INDEX)
    args = parser.parse_args(argv)

    target = args.target or _default_target()

    try:
        result = select(target, captures_path=args.captures,
                         reviews_dir=args.reviews_dir,
                         capture_index_path=args.capture_index)
        payload = {
            "target": result["target"],
            "rids": result["rids"],
            "records": result["records"],
            "by_date": result["by_date"],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
        if args.out:
            tmp = args.out + ".tmp"
            with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text + "\n")
            os.replace(tmp, args.out)
        else:
            print(text)

        warn = ",warn=unreadable" if result["_unreadable_reviews"] else ""
        print("SELECT_STATUS: target=%s selected=%d past=%d reviewed_rids=%d "
              "captures=%d index_only=%d unreadable_reviews=%d%s"
              % (target, result["_selected_count"], result["_past_count"],
                 result["_reviewed_count"], result["_captures_count"],
                 result["_index_only_count"], len(result["_unreadable_reviews"]),
                 warn))
        return 0
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print("SELECT_STATUS: target=%s selected=0 past=0 reviewed_rids=0 "
              "captures=0 index_only=0 unreadable_reviews=0 error=%s"
              % (target, type(e).__name__))
        return 0


if __name__ == "__main__":
    rc = 0
    try:
        rc = main(sys.argv[1:])
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print("SELECT_STATUS: target=unknown selected=0 past=0 reviewed_rids=0 "
              "captures=0 index_only=0 unreadable_reviews=0 error=%s" % type(e).__name__)
        rc = 0
    sys.stdout.flush()
    sys.exit(rc if isinstance(rc, int) else 0)
