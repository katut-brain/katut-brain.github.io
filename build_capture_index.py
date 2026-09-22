#!/usr/bin/env python3
"""日別台帳 `capture_index.json` を更新し、公開できていない日を報告する。

    {"days": {"2026-09-07": {"rids": [123, 456]}, ...}}

毎晩「既存の台帳 ∪ 今夜の captures.json」で更新して push する。日付と rid だけの
小さなファイル（実測 43日分で 7.3KB）なので、毎晩押しても負担にならない
（`captures.json` 本体は 560KB あるので押さない）。

## 何のためにあるか

書き込み経路を `push_via_api.sh` へ切り替えたとき、「照合が通らなければ押さない」
設計にした（壊れた本文を main に載せないため）。その代わり、押せなかった夜のぶんは
公開されない。**この台帳は、その日を後から人が見つけられるようにするための記録**。

`captures.json` は毎晩 Raindrop から作り直されるので、押せなかった日の保存が
Raindrop 側で消えたり取り込みが `INCOMPLETE` だったりすると、証拠ごと消える。
台帳は一度観測した rid を消さないので、リポジトリ側に根拠が残る。

## 何をしないか（2026-09-09 の裁定）

**自動回収はしない。** 対象日を過去へ差し替えることも、本文のスナップショットを
残すことも、pending の状態遷移を持つこともしない。

一度その方向で作ったが、装置自身が新しい欠落経路を次々に生んだ
（材料の無い日に永久に張り付く／不完全な取り込みでスナップショットが縮む／
日単位のフラグでは部分的な状態を表せない、など）。元の依頼は「書き込み経路を
手転記から外す」であって「日次記録の完全性を保証する」ではなく、**押せなかった日が
失われるのは変更前から同じ**なので、そこは悪化しない。

自動回収を要件にするなら、独立した設計として別に起こす。
"""

import datetime
import hashlib
import json
import os
import re
import sys

# published 判定（rid/URL照合）は select_targets.py を正本にして re-use する
# （2026-09-22 CEO裁定。同じ照合ロジックを2箇所に書かない）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import select_targets  # noqa: E402

CAPTURES = "captures.json"
INDEX = "capture_index.json"
REVIEWS_DIR = "reviews"

# これより前の日は、この台帳を入れる前のもの。公開されていなくても報告しない
# （実際 2026-06-15 など、保存があるのに振り返りが無い日が6日ある。当時の
#  取りこぼしであって、今から気にする対象ではない）。
SINCE = datetime.date.fromisoformat(os.environ.get("CAPTURE_INDEX_SINCE", "2026-09-08"))


def jst_today() -> datetime.date:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=9)).date()


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def synthetic_rid(rec) -> int:
    """`rid` を持たないレコードに、安定した識別子を振る。

    Raindrop の rid は正の整数なので、**負数**にして合成だと見て分かるようにする。
    無いとその日が台帳から丸ごと落ちる（台帳は rid の集合で日を表すため、
    rid の無いレコードだけの日は「保存0件の日」と区別がつかない）。
    実測では captures.json の320件すべてが整数 rid を持つ（2026-09-09）。
    """
    parts = [str(rec.get("date") or "")[:10], str(rec.get("source") or "")]
    key = chr(31).join(parts)   # 日付にも URL にも現れない区切り
    return -int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:12], 16)


def captures_by_day():
    """captures.json から ({日付文字列: set(rid)}, {rid: source_url}) を作る。

    source_url は published 判定の URL 照合（select_targets.url_key 経由）で使う
    （2026-09-22 CEO裁定。data-rid が付いていない/rid未確定のレコードでも、
    URL一致で「実際にreviewsへ載っている」ことを確認できるようにするため）。
    captures.json は毎晩作り直されるため、ここで拾えるのは**今夜の
    captures.json に載っている rid だけ**（過去に captures.json から消えた
    rid の source は分からない＝そのぶんはrid一致のみで判定する）。
    """
    data = _load_json(CAPTURES)
    records = data if isinstance(data, list) else (data or {}).get("captures", [])
    out = {}
    url_by_rid = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        value = rec.get("date")
        if not isinstance(value, str) or len(value) < 10:
            continue
        try:
            datetime.date.fromisoformat(value[:10])
        except ValueError:
            continue
        rid = rec.get("rid")
        if not isinstance(rid, int):
            rid = synthetic_rid(rec)
        out.setdefault(value[:10], set()).add(rid)
        source = rec.get("source")
        if isinstance(source, str) and source:
            url_by_rid[rid] = source
    return out, url_by_rid


class LedgerBroken(Exception):
    """台帳が読めない・形が違う。**この場合は絶対に上書きしない。**

    壊れた台帳を空として受け入れて書き直すと、そこにしか無かった過去の rid が
    永久に消える。読めないなら触らないほうが安全。
    """


def load_index() -> dict:
    """台帳を厳格に読む。ファイルが無ければ空、壊れていれば LedgerBroken。"""
    if not os.path.exists(INDEX):
        return {}
    data = _load_json(INDEX)
    if data is None:
        raise LedgerBroken("not valid JSON")
    if not isinstance(data, dict):
        raise LedgerBroken("top level is not an object")
    days = data.get("days")
    if not isinstance(days, dict):
        raise LedgerBroken('"days" is not an object')
    out = {}
    for day, entry in days.items():
        try:
            datetime.date.fromisoformat(day)
        except (TypeError, ValueError):
            raise LedgerBroken(f"bad day key: {day!r}")
        if not isinstance(entry, dict):
            raise LedgerBroken(f"{day}: entry is not an object")
        rids = entry.get("rids")
        if not isinstance(rids, list) or not all(isinstance(r, int) for r in rids):
            raise LedgerBroken(f"{day}: rids must be a list of integers")
        out[day] = set(rids)
    return out


def save_index(index: dict) -> None:
    """一時ファイルへ書いてから置き換える（途中で落ちても台帳を壊さない）。"""
    payload = {"days": {day: {"rids": sorted(rids)}
                        for day, rids in sorted(index.items())}}
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, INDEX)


def unpublished_days(index: dict, url_by_rid: dict, since: str, yesterday: str) -> list:
    """SINCE以降の日で、その日の保存(rid)のうち reviews/*.html のどれにも
    掲載されていない（data-rid一致でもURL一致でも見つからない）rid が
    1件でも残っている日の一覧を返す（2026-09-22 CEO裁定）。

    旧実装は「reviews/<日付>.html というファイルがその日の分だけ公開する」
    前提で `day not in published_days()` を見ていたが、その前提は
    select_targets.py 導入後は成り立たない（過去日の保存が別日のreviewsへ
    まとめて載るため）。ここでは reviews の実掲載（select_targets.py の
    reviewed_rids/reviewed_urls・url_key を再利用）を直接見る。
    """
    reviewed_rids, _unreadable = select_targets.reviewed_rids(REVIEWS_DIR)
    reviewed_urls = select_targets.reviewed_urls(REVIEWS_DIR)

    out = []
    for day, rids in sorted(index.items()):
        if not rids:
            continue
        if not (since <= day <= yesterday):
            continue
        missing = False
        for rid in rids:
            if rid in reviewed_rids:
                continue
            src = url_by_rid.get(rid)
            key = select_targets.url_key(src) if src else None
            if key is not None and key in reviewed_urls:
                continue
            missing = True
            break
        if missing:
            out.append(day)
    return out


def main() -> int:
    yesterday = jst_today() - datetime.timedelta(days=1)

    try:
        index = load_index()
    except LedgerBroken as exc:
        # 触らずに終える。`git status` に差分が出ないので、壊れた版が main へ行くこともない。
        print(f"LEDGER_ERROR: {INDEX} is unusable ({exc}). Not overwriting it.")
        print("LEDGER_ERROR: fix it by hand; tonight's run continues without it.")
        return 1

    by_day, url_by_rid = captures_by_day()
    for day, rids in by_day.items():
        index.setdefault(day, set()).update(rids)   # 一度観測した rid は消さない

    try:
        save_index(index)
        print(f"INDEX: {len(index)} day(s) on record -> {INDEX}")
    except OSError as exc:
        print(f"LEDGER_ERROR: could not write {INDEX}: {exc}")
        return 1

    # 公開できていない日の報告。**直しには行かない**（人が後から見るための記録）。
    unpublished = unpublished_days(index, url_by_rid, SINCE.isoformat(),
                                    yesterday.isoformat())
    if unpublished:
        print(f"UNPUBLISHED: {len(unpublished)} day(s) have saves not yet reflected "
              "in any review: " + ", ".join(unpublished))
        print("UNPUBLISHED: this is a record, not a queue. Nothing is retried automatically.")
    else:
        print("UNPUBLISHED: none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
