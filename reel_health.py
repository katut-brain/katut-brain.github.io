#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Instagram Reel 動画取得の**全滅検知**（手順7から呼ばれる読み取り専用チェッカー）。

なぜ要るか:
  Reel動画取得の中継（kkinstagram.com）への依存はこれで4回目（vxinstagram
  2026-07-28採用→08-01追従→08-23撤去→2026-09-20 kkinstagramで復活）で、
  過去3回とも数週間〜数ヶ月で死んでいる。中継が死んでも `fetch_instagram()` は
  caption-onlyで `ok:true` のまま完走するため、**黙って毎晩公開され続け誰も
  気づかない**リスクがある。`python3 ledger.py --summary` は無人ランでは誰も
  実行しないコマンドなので、`check_disclosure.py` と同じく**成果物側
  （reviews）に痕跡を残す**方式で検知する。

  2026-09-20 に手順書 `cloud_routine_prompt.md` へ検知ロジックを `python3 -c "..."`
  として直接埋め込んだが、Codex 敵対的レビュー最終ラウンドで「このリポで唯一
  テストに守られていない実行コード」と指摘された（`fetch_instagram()` の戻り値
  までは25件のテストで守られているが、`_facts()` による永続化を経由した経路と、
  手順書のスクリプトそのものは一度もテストを通っていなかった）。ユーザー裁定で
  このファイルへ切り出し、`test_reel_health.py` で回帰テストを持たせる。

スコープ（意図的な線引き。ユーザー裁定 2026-09-20）:
  - **これは全滅検知であって健全性監視ではない。部分劣化（例: 10件中9件失敗・
    1件成功）は意図的に検知しない。** 1件でも成功していれば中継そのものは
    生きていると判断できるため、警告はしない。閾値監視が要るとCEOが判断したら
    別タスクとして追加する（実装漏れではない）。
  - Instagram本体の取得失敗（`missing` に `"fetch_failed"` が入るケース）は
    今回のスコープ外（既存の経路で今回の変更とは無関係）。

使い方（手順書側から呼ぶ）:
  python3 reel_health.py [--target YYYY-MM-DD]

  対象日は --target > 環境変数 `FACTS_DATE` > 環境変数 `TARGET_OVERRIDE` >
  日本時間の「昨日」（手順1・26行目の `$TARGET` と同じ計算式）の優先順位で
  決める。**出自を問わず、`YYYY-MM-DD` 形式でない値は採用しない**（不正値は
  黙って捨てず、無視した事実と実際に採用した日を出力に残す。修正K）。
  手順書側は手順1と同じ式で `$TARGET` を再導出し `--target "$TARGET"` を
  明示的に渡す（手順ごとに別のシェル呼び出しになり、手順1の `$TARGET` は
  ここには届かないため）。この bash 再導出（`${TARGET_OVERRIDE:-...}`）は
  キー未設定/空文字のときだけフォールバックし値の書式は検証しないため、
  不正な `TARGET_OVERRIDE` が `--target` としてそのまま渡ってくることがある。
  このスクリプト側で出自を問わず書式検証するのはそのため。

出力（標準出力に1行。終了コードは常に0＝無人ランのゲートを止めない。
check_disclosure.py / backfill.py / run_timing.py と同じ流儀）:
  REEL_VIDEO_STATUS: total=<N> ok=<N> reasons=<dict> warn=yes|no

  `warn=yes` は「`reviews/<TARGET>.html` の `</footer>` 直前に警告を追記せよ」
  の合図。判定条件（`total>=1 かつ ok==0`）はこのスクリプト側だけが持ち、
  手順書には書き写さない（二重管理を避ける）。
  ファイルが存在しない／読めない夜は `total=0 ok=0 reasons={} warn=no` に
  末尾へ `(facts file not found)` / `(facts file unreadable: <理由>)` を添えて
  traceback を出さず正常終了する（無人エージェントがtracebackを見てどう
  振る舞うかは信用できないため）。

読み取り専用。何も書き出さない（ledger.py と同じ性質）。
"""

import argparse
import collections
import datetime
import io
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JST = datetime.timezone(datetime.timedelta(hours=9))

# ⚠️ 読み先は fetch_content.py の**書き先**と一字一句同じ解決規則にする（2026-09-20
# Codex敵対的レビュー最終ラウンド指摘・修正J）。fetch_content.py:1326は
# `os.environ.get("FETCH_FACTS_DIR", <既定>)` であり、dict.get(key, default) は
# **キー自体が無いときだけ**既定値を返す。FETCH_FACTS_DIR="" (空文字, キーは在る)は
# そのまま空文字が返る。
#
# ledger.py:27 は `os.environ.get("FETCH_FACTS_DIR") or <既定>` で、空文字も
# 未設定と同じ扱いにして既定ディレクトリへフォールバックする。**これは
# fetch_content.py と食い違う**（3者を並べて確認した事実。ここでは直さない
# ＝ledger.py側の変更は今回のスコープ外。気づいた点として記録だけ残す）。
#
# fetch_content.py の record_facts() は `if not FACTS_DIR: return None` で
# 空文字を「factsを書かない」の意味に使っている（モジュールコメント「FACTS_DIR を
# 空文字にすると書き出しを止められる」参照）。このスクリプトが空文字を無視して
# 既定ディレクトリへフォールバックすると、今回の実行では書き込みが無効化されて
# 何も書かれていないのに、既定ディレクトリに残る**別の実行の古い同日ファイル**を
# 読んでしまい、実態と食い違う。そのためここは書き手と一致させ、空文字は
# 空文字のまま保持する（下の compute_status() で「無効化」として明示的に扱う）。
FACTS_DIR = os.environ.get("FETCH_FACTS_DIR", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fetch_facts"))

# YYYY-MM-DD 形式のみを日付として受理する（TARGET_OVERRIDE の書式検証に使う。
# 手順1・27行目の「形式が YYYY-MM-DD でなければ無視して『昨日』を使う」と同じ規則）。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 母数から除外する「未試行」の video_reason。
#   - "instagram_reel_abandoned": 2026-08-23の構造的断念による歴史的コード。
#     2026-09-20以降は新規発行されない（正本は ledger.py の _VIDEO_REASON_KIND の
#     コメント＝ここと二重管理しない。除外対象を増減したくなったらまず
#     そちらのコメントで「新規発行されない歴史的コードか」を確認してから直す）。
#     「その晩に試みて失敗した」のではなく「そもそも中継を叩いていない」ことを示す。
#   - "" (空): 理由コード未記録の2026-09-02以前のレガシーレコード。同じ理由で未試行。
# どちらも母数に入れると、中継が完全に健全な夜でも「全滅」と誤検知する
# （2026-09-20 Codex敵対的レビュー4周目で発見・修正）。
_NOT_ATTEMPTED_REASONS = ("", "instagram_reel_abandoned")


def _default_target():
    """他のスクリプト（backfill.py の _default_target）・Routine の $TARGET と
    同じ計算式（JSTの「昨日」）。"""
    now = datetime.datetime.now(JST)
    yesterday = now.date() - datetime.timedelta(days=1)
    return yesterday.isoformat()


def resolve_target(explicit):
    """--target > FACTS_DATE環境変数 > TARGET_OVERRIDE環境変数 > JSTの昨日、の
    優先順位で対象日を決める（2026-09-20 Codex敵対的レビュー最終ラウンド指摘・
    修正I/修正K）。

    ⚠️ **値がどの経路（--target / FACTS_DATE / TARGET_OVERRIDE）から来たかに
    関わらず、YYYY-MM-DD 形式でなければ採用しない**（修正K）。手順書は
    `TARGET=${TARGET_OVERRIDE:-$(TZ=Asia/Tokyo date -d yesterday +%F)}` という
    bash構文でTARGETを再導出してから `--target "$TARGET"` として明示的に渡す。
    この bash構文は**キーが未設定/空文字のときだけフォールバックし、値の書式は
    検証しない**。修正Iで --target を「無条件に最優先で信じる」実装にしたところ、
    不正な TARGET_OVERRIDE（例: "yesterday"）が bash 経由で --target に流れ込むと
    検証を素通りしてしまう実害が起きた（CEO実測:
    `reel_health.py --target yesterday` が `fetch_facts/yesterday.json` を
    読みにいき、`TARGET_OVERRIDE=yesterday python reel_health.py`（--target無し・
    TARGET_OVERRIDE経由）の方は正しく無視されて昨日になる、という非対称）。
    ここでは出自を問わず同じ _DATE_RE 検証を通し、最初に見つかった有効な値を
    使う。全て無効なら JSTの昨日。

    戻り値: (target, ignored)。ignored は無視した (出自, 元の値) のタプルのリスト
    （空なら全て正常）。呼び出し側はこれを出力へ残す（無人運用で「なぜ違う日を
    見たのか」が後から分からなくならないようにするため）。
    """
    candidates = [
        ("--target", explicit),
        ("FACTS_DATE", os.environ.get("FACTS_DATE")),
        ("TARGET_OVERRIDE", os.environ.get("TARGET_OVERRIDE")),
    ]
    ignored = []
    for source, value in candidates:
        if not value:
            continue
        if _DATE_RE.match(value):
            return value, ignored
        # YYYY-MM-DD形式でない。黙って捨てず、無視した事実を残してから
        # 次の優先順位へ落とす（手順1・27行目と同じ無視ルール）。
        ignored.append((source, value))
    return _default_target(), ignored


def compute_status(target):
    """対象日の Reel 動画取得状況を集計する。戻り値は dict:
    {"total": int, "ok": int, "reasons": {code: count}, "note": str или None}
    note が非None のときは note の文言をログへ添えるだけの注記
    （facts file not found / unreadable / recording disabled）で、
    total/ok/reasons は 0/0/{} のまま。
    """
    if not FACTS_DIR:
        # fetch_content.py と同じ「空文字＝facts記録を無効化」の解釈（上のFACTS_DIR
        # 定義のコメント参照）。空文字を無視して既定ディレクトリへフォールバックすると
        # 「今回は書いていないのに別実行の古い同日ファイルを読む」食い違いが起きるため、
        # ここでは既定ディレクトリを一切見ない。
        return {"total": 0, "ok": 0, "reasons": {},
                "note": "facts recording disabled (FETCH_FACTS_DIR=\"\")"}
    path = os.path.join(FACTS_DIR, "%s.json" % target)
    if not os.path.isfile(path):
        return {"total": 0, "ok": 0, "reasons": {}, "note": "facts file not found"}
    try:
        with io.open(path, encoding="utf-8") as f:
            store = json.load(f)
    except Exception as e:
        return {"total": 0, "ok": 0, "reasons": {},
                "note": "facts file unreadable: %s" % type(e).__name__}
    if not isinstance(store, dict):
        return {"total": 0, "ok": 0, "reasons": {},
                "note": "facts file unreadable: top-level not a dict"}

    # ⚠️ fetch_facts/<TARGET>.json のトップレベルは配列ではなく、URLをキーにした
    # dict（ledger.py の _load_all_records() と同じ読み方＝ .values() で取り出す）。
    recs = [r for r in store.values()
            if isinstance(r, dict) and r.get("route") == "instagram" and r.get("has_video")]
    tried = [r for r in recs
             if r.get("video_understood") or (r.get("video_reason") or "") not in _NOT_ATTEMPTED_REASONS]
    total = len(tried)
    ok = sum(1 for r in tried if r.get("video_understood"))
    reasons = collections.Counter(
        r.get("video_reason") or "(empty)" for r in tried if not r.get("video_understood"))
    return {"total": total, "ok": ok, "reasons": dict(reasons), "note": None}


def should_warn(status):
    """全滅検知の判定そのもの（この関数だけが正本。手順書には書き写さない）。
    誤警告のコストは振り返りHTMLに1行増えるだけだが、中継の死亡を取り逃がす
    コストは数週間の静かな欠損なので、非対称性を踏まえ total==1 でも警告する
    側に倒す（ユーザー裁定 2026-09-20）。部分劣化（ok>=1）は警告しない
    （スコープ外・上のモジュールdocstring参照）。"""
    return status["total"] >= 1 and status["ok"] == 0


def _ignored_target_note(target, ignored):
    """resolve_target()が捨てた不正値を、採用した日と一緒に1文にする
    （修正K: 黙って捨てない）。ignoredが空ならNone。"""
    if not ignored:
        return None
    parts = ["%s=%r" % (source, value) for source, value in ignored]
    return "ignored invalid target(s) %s; using %s" % (", ".join(parts), target)


def print_status_line(target, status, ignored=None):
    line = "REEL_VIDEO_STATUS: total=%d ok=%d reasons=%s warn=%s" % (
        status["total"], status["ok"], status["reasons"],
        "yes" if should_warn(status) else "no")
    notes = []
    target_note = _ignored_target_note(target, ignored or [])
    if target_note:
        notes.append(target_note)
    if status["note"]:
        notes.append(status["note"])
    if notes:
        line += " (%s)" % "; ".join(notes)
    print(line)
    return line


def build_arg_parser():
    p = argparse.ArgumentParser(description="Instagram Reel 動画取得の全滅検知")
    p.add_argument("--target", default=None,
                    help="対象日 YYYY-MM-DD（不正な書式は無視される。省略時: "
                         "FACTS_DATE > TARGET_OVERRIDE > JSTの昨日。いずれも同じ"
                         "書式検証を通る）")
    return p


def main(argv=None):
    try:
        args = build_arg_parser().parse_args(argv)
        target, ignored = resolve_target(args.target)
        status = compute_status(target)
        print_status_line(target, status, ignored)
    except SystemExit as exc:
        # argparse がエラー時に sys.exit(2) するのを含め、常に0で返す
        # （check_disclosure.py / backfill.py と同じ「終了コードは常に0」の流儀。
        # 無人Routineのゲートをこのチェッカーで止めない）。
        print("REEL_VIDEO_STATUS: total=0 ok=0 reasons={} warn=no "
              "(argument parsing failed: exit=%s)" % exc.code)
        return 0
    except Exception as exc:  # noqa: BLE001 - 完走優先。ここで例外を上げない
        print("REEL_VIDEO_STATUS: total=0 ok=0 reasons={} warn=no "
              "(unexpected error: %s: %s)" % (type(exc).__name__, exc))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
