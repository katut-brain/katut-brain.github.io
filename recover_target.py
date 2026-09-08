#!/usr/bin/env python3
"""その夜に処理すべき日付（TARGET）を決め、日別の台帳を更新する。

既定は「日本時間の昨日」。ただし台帳に「保存があったのに公開できていない日」が
あれば、その最も古い日を返す。

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

  これは毎晩「既存の台帳 ∪ 今夜の captures.json」で更新し、手順2.2 で先に push する。
  **一度でも観測した保存は、あとから Raindrop で消えても台帳に残る。**
  日付と rid だけの小さなファイルなので、毎晩押しても負担にならない
  （`captures.json` 本体は 560KB あるので押さない）。

■ rid だけでは足りない。本文も残す（敵対的レビュー10周目の指摘）
  台帳があれば「この日はまだ公開できていない」と分かるが、rid しか無いので
  **作り直せない**。押せなかった日の保存が Raindrop から消えていると、その日を
  TARGET に選んでも入力が0件で何も作れず、最古の pending に永久に張り付いて
  新しい日も処理できなくなる——直そうとした永久欠落が、永久停止に化ける。
  そこで `capture_days/<日付>.json` にレコード本文をそのまま残し、台帳と一緒に
  先に push する。**既存があれば rid で併合する**（上書きにすると、翌晩の取り込みが
  INCOMPLETE だったときにスナップショットが縮んで本文が失われる＝11周目の指摘）。
  手順3はこれがあれば入力の正本にする。
  それでも材料が無い日（本文がどこにも無い）は `unrecoverable` を台帳に焼き付けて
  飛ばす。飛ばしたことは記録に残るので、黙って消えるのとは違う。

■ 「ファイルがあれば公開済み」とは見なさない（同レビュー P1）
  reviews に埋め込まれた `review-meta` コメント（手順5が書く）を読み、
  その日の rid を台帳と突き合わせる。台帳にあって review に無い rid があれば、
  **不完全な公開**として回収対象にする。**meta が無い・壊れている・日付が違う・
  count が合わない場合も未公開として扱う**（9周目の指摘。無人LLMが書き忘れるのは
  いちばん起きやすい逸脱で、それを公開済みと判定すると欠けた review が永久に残る）。
  MIGRATION_DATE より前のファイルだけは、meta を書く前に作られたものなので
  公開済みとして扱う（遡って作り直さない）。

■ 探索に窓を掛けない（9周目の指摘）
  窓を掛けると、回収しようとした晩にまた押せなかった日が翌晩には窓の外へ落ち、
  台帳に残っていても二度と拾われない。移行日より前は legacy として done になるので、
  全期間を見ても過去に張り付くことはない。

■ 1晩に1日ずつ
  溜まっていても古い順に消化する。追いつけば自然に「昨日」へ戻る。
  過去に張り付いて最新が止まった場合は `stale-check.yml` が拾う。
"""

import datetime
import json
import os
import re
import sys

# この日以降の reviews は `review-meta` を持っていることを必須にする。
# 2026-09-08 ＝ この装置を入れた夜が扱う最初の対象日。これより前は meta を書く前に
# 作られたファイルなので、欠けていても遡って作り直さない（実際 2026-06-15 など、
# 保存があるのに振り返りが無い日が6日ある。当時の取りこぼしであって今回の対象ではない）。
MIGRATION_DATE = datetime.date.fromisoformat(
    os.environ.get("RECOVER_MIGRATION_DATE", "2026-09-08"))

CAPTURES = "captures.json"
INDEX = "capture_index.json"
SNAPSHOT_DIR = "capture_days"
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
    """captures.json から {日付文字列: [レコード, ...]} を作る。"""
    data = _load_json(CAPTURES)
    records = data if isinstance(data, list) else (data or {}).get("captures", [])
    out = {}
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
        out.setdefault(value[:10], []).append(rec)
    return out


def rids_of(records) -> set:
    return {r["rid"] for r in records if isinstance(r.get("rid"), int)}


def snapshot_path(day: str) -> str:
    return os.path.join(SNAPSHOT_DIR, day + ".json")


def load_snapshot(day: str):
    """その日のレコード本文のスナップショット。無ければ None。"""
    data = _load_json(snapshot_path(day))
    return data if isinstance(data, list) else None


def merge_records(existing, incoming):
    """rid をキーに単調併合する。**一度残した本文は減らさない。**

    ⚠️ 上書きにしてはいけない（敵対的レビュー11周目の指摘）。初夜に A/B を観測して
    残したのに、翌晩の取り込みが INCOMPLETE で A しか見えないと、上書きだと
    スナップショットが [A] に縮む。そのあと Raindrop から B が消えると、B の本文は
    どこにも無くなるのに台帳には rid が残るので、その日は永久に pending のまま
    張り付く（材料が A だけ残るので unrecoverable にもならない）。
    同じ rid は今夜の観測で更新する（そちらが新しいため）。
    """
    by_rid, loose = {}, []
    for source in (existing or [], incoming or []):
        for rec in source:
            if not isinstance(rec, dict):
                continue
            rid = rec.get("rid")
            if isinstance(rid, int):
                by_rid[rid] = rec          # 後勝ち＝今夜の観測を優先
            elif rec not in loose:
                loose.append(rec)
    return [by_rid[k] for k in sorted(by_rid)] + loose


def save_snapshot(day: str, records) -> None:
    """レコード本文を日別に残す（既存があれば併合する）。

    ⚠️ rid だけでは足りない（敵対的レビュー10周目の指摘）。押せなかった日の保存が
    Raindrop から消えると、翌晩その日を TARGET に選べても**入力が0件で何も作れず、
    最古の pending に永久に張り付く**。本文ごと残しておけば作り直せる。
    """
    records = merge_records(load_snapshot(day), records)
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    tmp = snapshot_path(day) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, snapshot_path(day))


class LedgerBroken(Exception):
    """台帳が読めない・形が違う。**この場合は絶対に上書きしない。**

    壊れた台帳を空として受け入れて書き直すと、そこにしか無かった過去の rid が
    永久に消える（敵対的レビュー9周目の指摘）。読めないなら触らないほうが安全。
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
        unrecoverable = entry.get("unrecoverable", False)
        if not isinstance(unrecoverable, bool):
            raise LedgerBroken(f"{day}: unrecoverable must be true/false")
        extra = set(entry) - {"rids", "unrecoverable"}
        if extra:
            raise LedgerBroken(f"{day}: unexpected fields {sorted(extra)}")
        out[day] = {"rids": set(rids), "unrecoverable": unrecoverable}
    return out


def merge_index(index: dict, seen: dict) -> dict:
    """台帳と今夜の観測を合併する。**一度観測した rid は消さない。**"""
    merged = {day: {"rids": set(e["rids"]), "unrecoverable": e["unrecoverable"]}
              for day, e in index.items()}
    for day, records in seen.items():
        entry = merged.setdefault(day, {"rids": set(), "unrecoverable": False})
        entry["rids"].update(rids_of(records))
    return merged


def save_index(index: dict) -> None:
    """一時ファイルへ書いてから置き換える（途中で落ちても台帳を壊さない）。"""
    payload = {"days": {}}
    for day, entry in sorted(index.items()):
        rec = {"rids": sorted(entry["rids"])}
        if entry["unrecoverable"]:
            rec["unrecoverable"] = True
        payload["days"][day] = rec
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, INDEX)


def review_text(day: str):
    path = os.path.join(REVIEWS_DIR, day + ".html")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def parse_meta(text: str, day: str):
    """review-meta を厳格に読む。(meta または None, 理由) を返す。

    ⚠️ 壊れた meta を「古い形式」として通してはいけない（敵対的レビュー9周目の指摘）。
    無人LLMが書き忘れる・JSONを少し壊す、は最も起きやすい逸脱で、それを
    「公開済み」と判定すると、rid が欠けた review が永久に正常扱いになる。
    """
    m = META_RE.search(text)
    if not m:
        return None, "review-meta is missing"
    try:
        meta = json.loads(m.group(1))
    except ValueError:
        return None, "review-meta is not valid JSON"
    if not isinstance(meta, dict):
        return None, "review-meta is not an object"
    if meta.get("date") != day:
        return None, f"review-meta date is {meta.get('date')!r}, expected {day}"
    rids = meta.get("rids")
    if not isinstance(rids, list) or not all(isinstance(r, int) for r in rids):
        return None, "review-meta rids must be a list of integers"
    if len(set(rids)) != len(rids):
        return None, "review-meta rids contains duplicates"
    if meta.get("count") != len(rids):
        return None, f"review-meta count is {meta.get('count')!r}, rids has {len(rids)}"
    if meta.get("import") not in ("OK", "INCOMPLETE"):
        return None, f"review-meta import is {meta.get('import')!r}"
    return meta, "ok"


def is_done(day: str, expected_rids: set, migration: datetime.date):
    """その日が「ちゃんと公開できている」か。(判定, 理由) を返す。"""
    if datetime.date.fromisoformat(day) < migration:
        # この装置を入れる前の日。review が欠けていても遡って作り直さない
        # （実際 2026-06-15 など、保存があるのに振り返りが無い日が6日ある。
        #  当時の取りこぼしであって、今から作り直す対象ではない）。
        return True, "before the migration date"

    text = review_text(day)
    if text is None:
        return False, "no review file"

    meta, why = parse_meta(text, day)
    if meta is None:
        # 移行日以降は meta が必須。無い・壊れている＝未公開として扱う。
        return False, why

    covered = set(meta["rids"])
    missing = expected_rids - covered
    if meta["import"] == "INCOMPLETE":
        # 取り込みが不完全だった日は、新しい保存が見えるようになったときだけやり直す
        # （毎晩やり直しても足せるものが無いなら前へ進めない）。
        if missing:
            return False, "import was incomplete and new saves appeared"
        return True, "import was incomplete but nothing new to add"
    if missing:
        return False, f"{len(missing)} save(s) not in the published review"
    return True, "complete"


def main() -> int:
    today = jst_today()
    yesterday = today - datetime.timedelta(days=1)
    migration = MIGRATION_DATE

    seen = captures_by_day()

    try:
        index = merge_index(load_index(), seen)
    except LedgerBroken as exc:
        # 壊れた台帳を空として書き直すと、そこにしか無い過去の rid が永久に消える。
        # 触らずに昨日を返し、ログに大きく残す（`git status` に差分が出ないので
        # 手順8も台帳を push しない＝壊れた版が main へ行くこともない）。
        print(f"LEDGER_ERROR: {INDEX} is unusable ({exc}). Not overwriting it.")
        print("LEDGER_ERROR: recovery is disabled tonight. Fix the ledger by hand.")
        print(f"TARGET={yesterday.isoformat()}")
        return 0

    # ⚠️ 探索に窓を掛けない（敵対的レビュー9周目の指摘）。窓を掛けると、回収しようと
    # した晩にまた押せなかった日が翌晩には窓の外へ落ち、台帳に残っていても二度と
    # 拾われない。移行日より前の日は legacy として done になるので、全期間を見ても
    # 過去に張り付くことはない。
    pending = []
    for key in sorted(index):
        try:
            day = datetime.date.fromisoformat(key)
        except ValueError:
            continue
        if day > yesterday:
            continue  # 当日と未来は対象外（まだ確定していない）
        entry = index[key]
        if not entry["rids"]:
            continue  # 保存0件の日＝review を作らないのが正しい
        if entry["unrecoverable"]:
            continue  # 作り直す材料が無いと確定済み（下で1度だけ判定する）
        done, why = is_done(key, entry["rids"], migration)
        if not done:
            pending.append((key, why))

    # ⚠️ 材料の無い日で止まらないこと（敵対的レビュー10周目の指摘）。
    # 「押せなかった日の保存が Raindrop から消えた」場合、その日を TARGET に選んでも
    # 入力が0件なので何も作れない。放っておくと最古の pending に永久に張り付き、
    # 新しい日も処理できなくなる——直そうとした永久欠落が、永久停止に化ける。
    # スナップショットも captures.json も無い日は、その旨を台帳に焼き付けて飛ばす。
    while pending:
        key = pending[0][0]
        if seen.get(key) or load_snapshot(key) is not None:
            break
        index[key]["unrecoverable"] = True
        print(f"RECOVER: {key} has no material left (not in captures.json, "
              f"no snapshot). Marking it unrecoverable and moving on.")
        pending.pop(0)

    try:
        save_index(index)
        print(f"INDEX: {len(index)} day(s) on record -> {INDEX}")
    except OSError as exc:
        print(f"LEDGER_ERROR: could not write {INDEX}: {exc}")

    if pending:
        target = pending[0][0]
        print(f"RECOVER: {len(pending)} day(s) pending: "
              + ", ".join(f"{d} ({w})" for d, w in pending))
        print("RECOVER: picking the oldest one; the rest follow on later nights")
    else:
        target = yesterday.isoformat()
        print("RECOVER: nothing to recover")

    # 今夜これから作る日と、まだ片付いていない日は、レコード本文を残しておく。
    # これが無いと、押せなかった日の保存が Raindrop から消えたときに作り直せない。
    for key in sorted({target} | {d for d, _ in pending}):
        records = seen.get(key)
        if not records and load_snapshot(key) is None:
            continue
        try:
            save_snapshot(key, records or [])
            kept = load_snapshot(key) or []
            print(f"SNAPSHOT: {key} ({len(kept)} record(s) on file)")
        except OSError as exc:
            print(f"SNAPSHOT_ERROR: {key}: {exc}")

    print(f"TARGET={target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
