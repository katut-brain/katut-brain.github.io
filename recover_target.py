#!/usr/bin/env python3
"""その夜に処理すべき日付（TARGET）を決める。

既定は「日本時間の昨日」。ただし直近 WINDOW_DAYS 日のうちに
**保存があったのに reviews が無い日**があれば、その最も古い日を返す。

なぜ要るか（2026-09-08）:
  書き込み経路を push_via_api.sh へ切り替えたとき、「照合が通らなければ押さない」
  という設計にした（壊れた本文を main に載せないため）。その代わり、押せなかった夜の
  ぶんは公開されない。手順書には以前から「翌ランに回す」と書いてあったが、翌ランは
  別の日を対象にするだけで、前日ぶんを拾う仕組みはどこにも無かった——つまり
  **静かに1日欠ける経路**が残っていた。ここで拾う。

判定の根拠:
  reviews は「その日の保存が1件以上あるとき」だけ作る仕様（手順3）。したがって
  「captures.json にその日の保存があるのに reviews/<日付>.html が無い」＝
  作られなかったか、作られたが押せなかった、のどちらか。どちらも作り直しが要る。
  保存0件の日は reviews を作らないのが正しいので、対象にしない。

1晩に1日しか戻さない:
  溜まっていても順に消化する。追いつけば自然に「昨日」へ戻る。まとめて処理すると
  1ランの所要が読めなくなるため。
"""

import datetime
import json
import os
import sys

WINDOW_DAYS = 7
CAPTURES = "captures.json"
REVIEWS_DIR = "reviews"


def jst_today() -> datetime.date:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=9)).date()


def saved_dates() -> set:
    """captures.json に保存日として現れる日付の集合。"""
    try:
        with open(CAPTURES, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()

    records = data if isinstance(data, list) else data.get("captures", [])
    out = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        value = rec.get("date")
        if isinstance(value, str) and len(value) >= 10:
            try:
                out.add(datetime.date.fromisoformat(value[:10]))
            except ValueError:
                pass
    return out


def published_dates() -> set:
    out = set()
    try:
        names = os.listdir(REVIEWS_DIR)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".html"):
            continue
        try:
            out.add(datetime.date.fromisoformat(name[:-5]))
        except ValueError:
            pass
    return out


def main() -> int:
    today = jst_today()
    yesterday = today - datetime.timedelta(days=1)

    saved = saved_dates()
    published = published_dates()

    # 直近 WINDOW_DAYS 日ぶん（昨日を含み、当日は含まない）を古い順に見る。
    window = [yesterday - datetime.timedelta(days=i) for i in range(WINDOW_DAYS)]
    window.sort()

    missing = [d for d in window if d in saved and d not in published]

    if missing:
        target = missing[0]
        print(f"RECOVER: {len(missing)} day(s) saved but not published: "
              + ", ".join(d.isoformat() for d in missing))
        print(f"RECOVER: picking the oldest one; the rest follow on later nights")
    else:
        target = yesterday
        print("RECOVER: nothing to recover")

    print(f"TARGET={target.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
