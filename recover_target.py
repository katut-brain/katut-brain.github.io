#!/usr/bin/env python3
"""その夜に処理すべき日付（TARGET）を決め、日別の台帳を更新する。

既定は「日本時間の昨日」。ただし直近 WINDOW_DAYS 日のうちに
**保存があったのに公開できていない日**があれば、その最も古い日を返す。

なぜ要るか（2026-09-08）:
  書き込み経路を push_via_api.sh へ切り替えたとき、「照合が通らなければ押さない」
  という設計にした（壊れた本文を main に載せないため）。その代わり、押せなかった夜の
  ぶんは公開されない。手順書には以前から「翌ランに回す」と書いてあったが、翌ランは
  別の日を対象にするだけで、前日ぶんを拾う仕組みはどこにも無かった——つまり
  **静かに1日欠ける経路**が残っていた。ここで拾う。

■ 判定の根拠を Raindrop でなくリポジトリ側に置く（敵対的レビュー8周目の指摘）
  当初は `captures.json`（毎晩 Raindrop から作り直される）だけを見ていた。しかし
  それだと、押せなかった日の保存が Raindrop 側で消された／取り込みが INCOMPLETE
  だった場合に、**証拠ごと消えて回収されない**。そこで `capture_index.json` を置く:

      {"days": {"2026-09-07": {"rids": [123, 456]}, ...}}

  これは毎晩「既存の台帳 ∪ 今夜の captures.json」で更新し、手順8で push する。
  **一度でも観測した保存は、あとから Raindrop で消えても台帳に残る。**
  日付と rid だけの小さなファイルなので、毎晩押しても負担にならない
  （`captures.json` 本体は 560KB あるので押さない）。

■ 「ファイルがあれば公開済み」とは見なさない（同レビュー P1）
  reviews に埋め込まれた `review-meta` コメント（手順5が書く）を読み、
  その日の rid を台帳と突き合わせる。台帳にあって review に無い rid があれば、
  **不完全な公開**として回収対象にする。`review-meta` が無い古いファイルは、
  遡って作り直しても仕方がないので公開済みとして扱う。

■ 1晩に1日ずつ
  溜まっていても古い順に消化する。追いつけば自然に「昨日」へ戻る。
  過去に張り付いて最新が止まった場合は `stale-check.yml` が拾う。
"""

import datetime
import json
import os
import re
import sys

WINDOW_DAYS = 14
CAPTURES = "captures.json"
INDEX = "capture_index.json"
REVIEWS_DIR = "reviews"

META_RE = re.compile(r"<!--\s*review-meta:\s*(\{.*?\})\s*-->", re.S)


def jst_today() -> datetime.date:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=9)).date()


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def captures_by_day() -> dict:
    """captures.json から {日付文字列: set(rid)} を作る。"""
    data = _load_json(CAPTURES)
    records = data if isinstance(data, list) else (data or {}).get("captures", [])
    out = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        value = rec.get("date")
        rid = rec.get("rid")
        if not isinstance(value, str) or len(value) < 10:
            continue
        try:
            datetime.date.fromisoformat(value[:10])
        except ValueError:
            continue
        out.setdefault(value[:10], set())
        if rid is not None:
            out[value[:10]].add(rid)
    return out


def load_index() -> dict:
    data = _load_json(INDEX)
    days = (data or {}).get("days")
    if not isinstance(days, dict):
        return {}
    out = {}
    for day, entry in days.items():
        rids = entry.get("rids") if isinstance(entry, dict) else None
        out[day] = set(rids or [])
    return out


def merge_index(index: dict, seen: dict) -> dict:
    """台帳と今夜の観測を合併する。**一度観測した rid は消さない。**"""
    merged = {day: set(rids) for day, rids in index.items()}
    for day, rids in seen.items():
        merged.setdefault(day, set()).update(rids)
    return merged


def save_index(index: dict) -> None:
    payload = {"days": {day: {"rids": sorted(rids)}
                        for day, rids in sorted(index.items())}}
    with open(INDEX, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


def review_meta(day: str):
    """reviews/<day>.html に埋まっている review-meta を読む。

    戻り値: None=ファイルが無い / {}=meta が無い（古い形式）/ dict=meta
    """
    path = os.path.join(REVIEWS_DIR, day + ".html")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    m = META_RE.search(text)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {}


def is_done(day: str, expected_rids: set):
    """その日が「ちゃんと公開できている」か。(判定, 理由) を返す。"""
    meta = review_meta(day)
    if meta is None:
        return False, "no review file"
    if not meta:
        # review-meta を書く前に作られた古いファイル。遡って作り直さない。
        return True, "legacy review (no meta)"
    if meta.get("import") == "INCOMPLETE":
        # 取り込みが不完全だった日は、新しい保存が見えるようになったときだけやり直す。
        covered = set(meta.get("rids") or [])
        if expected_rids - covered:
            return False, "import was incomplete and new saves appeared"
        return True, "import was incomplete but nothing new to add"
    covered = set(meta.get("rids") or [])
    missing = expected_rids - covered
    if missing:
        return False, f"{len(missing)} save(s) not in the published review"
    return True, "complete"


def main() -> int:
    today = jst_today()
    yesterday = today - datetime.timedelta(days=1)

    seen = captures_by_day()
    index = merge_index(load_index(), seen)
    try:
        save_index(index)
        print(f"INDEX: {len(index)} day(s) on record -> {INDEX}")
    except OSError as exc:
        print(f"INDEX: could not write {INDEX}: {exc}")

    window = sorted(yesterday - datetime.timedelta(days=i) for i in range(WINDOW_DAYS))

    pending = []
    for day in window:
        key = day.isoformat()
        rids = index.get(key)
        if not rids:
            continue  # その日は保存0件＝review を作らないのが正しい
        done, why = is_done(key, rids)
        if not done:
            pending.append((key, why))

    if pending:
        target = pending[0][0]
        print(f"RECOVER: {len(pending)} day(s) pending: "
              + ", ".join(f"{d} ({w})" for d, w in pending))
        print("RECOVER: picking the oldest one; the rest follow on later nights")
    else:
        target = yesterday.isoformat()
        print("RECOVER: nothing to recover")

    print(f"TARGET={target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
