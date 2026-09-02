#!/usr/bin/env python3
# ledger.py — fetch_facts/*.json を照会時に毎回その場で集計する読み取り専用ツール。
#
# 設計（capture-pipeline Task G-1）: 累積台帳(_ledger.json)は作らない。GeminiとCodexの
# 敵対的レビューが独立にNO-GOを出した(「同じ事実を2箇所に書いて片方が腐る」という
# このプロジェクトの既知の失敗型そのもの／新設台帳は既存の滞留を含まない／
# 全データ20件程度で状態を永続化する技術的理由がゼロ)。
# 日別ログ fetch_facts/<日付>.json を唯一の真実とし、必要な集計は照会のたびに
# 全ファイルを読んでその場で導出する。このスクリプトは何も書き出さない。
#
# 使い方:
#   python3 ledger.py <rid>        # そのridの状態を人が読める形で出す
#   python3 ledger.py --summary    # 全体集計
#   python3 ledger.py --url <url>  # URLで引く(ridが未解決の件を調べる用)

import sys, os, glob, json, io

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 読み先は fetch_content.py の書き先と必ず同じ解決規則にする（環境変数名も同一）。
# ここを揃えないと、FETCH_FACTS_DIR を指して取得した分を照会側が見に行かず、
# 「取ったのに引けない」という観測装置として最悪の食い違いが起きる。
FACTS_DIR = os.environ.get("FETCH_FACTS_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fetch_facts")


def _load_all_records():
    """fetch_facts/*.json を全ファイル読み、各レコードに day を付けて1本のリストにする。
    壊れたファイルがあっても他のファイルの集計を止めない(完走優先)。"""
    records = []
    for path in sorted(glob.glob(os.path.join(FACTS_DIR, "*.json"))):
        day = os.path.splitext(os.path.basename(path))[0]
        try:
            with io.open(path, encoding="utf-8") as f:
                store = json.load(f)
        except Exception as e:
            print("WARN: 読み込み失敗のためスキップ: %s (%s)" % (path, e), file=sys.stderr)
            continue
        if not isinstance(store, dict):
            print("WARN: トップレベルがdictでないためスキップ: %s" % path, file=sys.stderr)
            continue
        for url, rec in store.items():
            if not isinstance(rec, dict):
                print("WARN: レコードがdictでないためスキップ: %s / %s" % (path, url),
                      file=sys.stderr)
                continue
            rec = dict(rec)
            rec["_day"] = day
            rec.setdefault("url", url)
            records.append(rec)
    return records


# 旧レコード(raindrop_id キー自体が無い)を照会時に救済するための逆引き。
# これが無いと、いま最も回収したい既存の滞留分が rid で1件も引けない
# （2026-09-02 Codexの敵対的レビューで発見）。
# 逆引きロジックは fetch_content.py 側を唯一の正本とし、ここでは import して使う。
# 同じ正規化規則を2箇所に書くと必ず片方が腐る。
try:
    from fetch_content import _resolve_rid as _fc_resolve_rid
except Exception as _e:      # 依存が無い環境でも照会自体は動かす(完走優先)
    _fc_resolve_rid = None
    print("WARN: fetch_content から rid 逆引きを読み込めないため、旧レコードの"
          "rid 救済は行わない: %s" % _e, file=sys.stderr)


# 旧レコードの救済は**既定オフ**。
# 救済結果は captures.json の最新状態しだいで変わるので、記録時の事実ではない。
# 既定で混ぜると「rid=X の履歴」という照会に、後日変わりうる（＝誤りうる）答えを
# 返すことになる。既定では再現可能な事実だけを答え、救済は --include-legacy で
# 明示的に求められたときだけ行う（2026-09-02 Codex 1周目と3周目の指摘の両立）。
INCLUDE_LEGACY = False


def _effective_rid(rec):
    """レコードの raindrop_id を返す。無ければ（--include-legacy 時のみ）captures.json で
    救済を試みる。戻り値: (rid or None, source文字列)。救済時は 'legacy_<元のsource>'。"""
    rid = rec.get("raindrop_id")
    if isinstance(rid, int):
        return rid, _rid_source_of(rec)
    if not INCLUDE_LEGACY or _fc_resolve_rid is None:
        return None, _rid_source_of(rec)
    try:
        resolved, src = _fc_resolve_rid(rec.get("url", "") or "")
    except Exception:
        return None, _rid_source_of(rec)
    if isinstance(resolved, int):
        return resolved, "legacy_" + src
    return None, _rid_source_of(rec)


def _group_key(rec):
    """レコードのグルーピングキーを決める。
    raindrop_id が解決できていれば ("rid", <int>)、できていなければ ("unresolved", <url>)。
    旧レコードは照会時に captures.json で救済を試み、それでもダメなら unresolved。"""
    rid, _ = _effective_rid(rec)
    if isinstance(rid, int):
        return ("rid", rid)
    return ("unresolved", rec.get("url", ""))


def _rid_source_of(rec):
    """rid_source を返す。キー自体が無い旧レコードは '(missing)' として扱う
    (サマリで「旧レコードで欠落」件数として明示するため)。"""
    if "rid_source" not in rec:
        return "(missing)"
    return rec.get("rid_source") or "(missing)"


# ---------- reason_kind 導出 ----------
# コードで機械的に確定できるものだけ permanent と言い切り、できないものは正直に unknown と出す。
def _reason_kind(rec):
    route = rec.get("route", "")
    url = rec.get("url", "") or ""
    missing = rec.get("missing") or []

    if route == "instagram" and "/reel/" in url and "video_content" in missing:
        # 2026-08-23の構造的断念による設計値。Brain/decisions/2026-08-23-instagram-reel-video-give-up.md
        return "permanent"
    if route == "instagram" and missing and set(missing) <= {"visual_content", "audio_content"}:
        # 画像・音声読解の実装が存在しない
        return "permanent"
    if route == "threads":
        # 同上(画像・音声読解の実装が存在しない)
        return "permanent"
    if "image_content" in missing:
        # fetch側に画像読解の実装が無く、読むのはRoutine側の仕事
        return "permanent"
    if "article_body_tail" in missing:
        # 本文上限による設計上の切り詰め。再取得しても同じ
        return "permanent"
    if not missing and rec.get("depth") == "full":
        return "none"
    # 上記以外(X動画のvideo_content、transcript、article_body、fetch_failed等)は
    # 恒久側と一過性側が現状のコードでは区別できない。推測で埋めない。
    return "unknown"


def _is_unknown_video_content(rec):
    """reason_kindがunknownの中でも「X動画のvideo_content」に該当するか。
    _gemini_video_understanding()の7つの早期returnが全部同じ空文字に潰れており、
    恒久側(APIキー未設定・許可外ホスト・サイズ超過)と一過性側(タイムアウト・例外・切断)が
    区別できない。この区別不能であること自体が観測結果なので、サマリで明示する。"""
    return rec.get("route") == "x" and "video_content" in (rec.get("missing") or [])


def _attempt_seq_of(rec):
    """そのレコードが「その日に何回目の取得だったか」。旧レコードは1回とみなす。"""
    try:
        n = int(rec.get("attempt_seq", 1))
    except (TypeError, ValueError):
        return 1
    return n if n >= 1 else 1


def _build_groups(records):
    groups = {}
    for rec in records:
        key = _group_key(rec)
        groups.setdefault(key, []).append(rec)

    summary = {}
    for key, recs in groups.items():
        recs_sorted = sorted(recs, key=lambda r: r.get("fetched_at") or "")
        latest = recs_sorted[-1]
        fetched_ats = [r.get("fetched_at") for r in recs_sorted if r.get("fetched_at")]
        reason_kind = _reason_kind(latest)
        depth = latest.get("depth")
        if depth == "full":
            state = "resolved"
        elif reason_kind == "permanent":
            state = "permanent"
        else:
            state = "open"
        summary[key] = {
            "key": key,
            # 日別ログはURLキーのdictなので、同日2回目は1回目のレコードを上書きする。
            # 残るのは最後の1レコードだけなので len() では回数を取りこぼす。
            # fetch_content 側が引き継いでいる attempt_seq を足して実試行回数にする
            # （attempt_seq を持たない旧レコードは1回とみなす）。
            "attempt_count": sum(_attempt_seq_of(r) for r in recs_sorted),
            "records_kept": len(recs_sorted),
            "first_attempt_at": min(fetched_ats) if fetched_ats else None,
            "last_attempt_at": max(fetched_ats) if fetched_ats else None,
            "latest": latest,
            "reason_kind": reason_kind,
            "state": state,
            "history": [(r.get("fetched_at"), r.get("depth"), r.get("ok")) for r in recs_sorted],
            "records": recs_sorted,
        }
    return summary


def _fmt_key(key):
    kind, val = key
    if kind == "rid":
        return "rid=%s" % val
    return "unresolved:%s" % val


def _print_group(g):
    key = g["key"]
    latest = g["latest"]
    print("キー: %s" % _fmt_key(key))
    print("state: %s" % g["state"])
    print("reason_kind: %s" % g["reason_kind"])
    print("attempt_count: %s (日別ログに残っているレコード数: %s)"
          % (g["attempt_count"], g["records_kept"]))
    print("first_attempt_at: %s" % g["first_attempt_at"])
    print("last_attempt_at: %s" % g["last_attempt_at"])
    print("最新レコード:")
    print("  url: %s" % latest.get("url"))
    print("  route: %s" % latest.get("route"))
    print("  ok: %s" % latest.get("ok"))
    print("  depth: %s" % latest.get("depth"))
    print("  missing: %s" % latest.get("missing"))
    print("  reason: %s" % latest.get("reason"))
    print("history (fetched_at, depth, ok):")
    for h in g["history"]:
        print("  %s" % (h,))
    # 記録時にridが入っていないレコードが混ざっていたら、その旨を隠さない。
    legacy = [r for r in g["records"] if not isinstance(r.get("raindrop_id"), int)]
    if legacy:
        print("⚠️ このうち %d 件は記録時に raindrop_id を持たない旧レコードで、照会時に"
              % len(legacy))
        print("   captures.json で救済して結び付けたもの。captures.json は毎晩再生成される")
        print("   ため、後日の照会で結果が変わりうる（この結び付けは再現可能な事実ではない）。")


def cmd_rid(rid_str):
    try:
        rid = int(rid_str)
    except ValueError:
        print("エラー: rid は整数で指定してください: %r" % rid_str)
        return 1
    records = _load_all_records()
    groups = _build_groups(records)
    g = groups.get(("rid", rid))
    if not g:
        print("rid=%s の観測記録は fetch_facts/*.json 中に見つかりません。" % rid)
        return 1
    _print_group(g)
    return 0


def cmd_url(url):
    records = _load_all_records()
    groups = _build_groups(records)
    # そのurlを含むグループを探す(rid解決済みならrid側、未解決ならunresolved側)。
    # groups は latest レコードしか保持していないため、生レコードを再グルーピングして走査する。
    matched = []
    for key, recs in _regroup_for_url(records).items():
        for rec in recs:
            if rec.get("url") == url:
                matched.append(key)
                break
    if not matched:
        print("url=%s の観測記録は fetch_facts/*.json 中に見つかりません。" % url)
        return 1
    for key in matched:
        g = groups[key]
        _print_group(g)
        print()
    return 0


def _regroup_for_url(records):
    groups = {}
    for rec in records:
        key = _group_key(rec)
        groups.setdefault(key, []).append(rec)
    return groups


def cmd_summary():
    records = _load_all_records()
    groups = _build_groups(records)

    total_groups = len(groups)
    state_counts = {}
    reason_kind_counts = {}
    rid_source_counts = {}
    unresolved_count = 0
    unknown_video_content_count = 0

    for key, g in groups.items():
        state_counts[g["state"]] = state_counts.get(g["state"], 0) + 1
        reason_kind_counts[g["reason_kind"]] = reason_kind_counts.get(g["reason_kind"], 0) + 1
        if key[0] == "unresolved":
            unresolved_count += 1
        if g["reason_kind"] == "unknown" and _is_unknown_video_content(g["latest"]):
            unknown_video_content_count += 1

    # rid_source は「観測レコード単位」で集計する(グループ単位ではなく、日別ログの
    # レコードそれぞれがどう解決されたかを見たいため)。
    # 照会時に captures.json で救済できた旧レコードは legacy_<元source> として
    # 別枠で数える。「記録時に解決できた」と「後から救済した」は別の事実なので混ぜない。
    for rec in records:
        _, src = _effective_rid(rec)
        rid_source_counts[src] = rid_source_counts.get(src, 0) + 1

    print("=== ledger --summary ===")
    print("総レコード数(fetch_facts/*.json): %d" % len(records))
    print("総グループ数: %d" % total_groups)
    print("  うち rid未解決(unresolved擬似キー)グループ数: %d" % unresolved_count)
    print()
    print("-- state 別件数 --")
    for k in ("resolved", "open", "permanent"):
        print("  %s: %d" % (k, state_counts.get(k, 0)))
    for k, v in state_counts.items():
        if k not in ("resolved", "open", "permanent"):
            print("  %s: %d" % (k, v))
    print()
    print("-- reason_kind 別件数(グループの最新レコード基準) --")
    for k in ("none", "permanent", "unknown"):
        print("  %s: %d" % (k, reason_kind_counts.get(k, 0)))
    for k, v in reason_kind_counts.items():
        if k not in ("none", "permanent", "unknown"):
            print("  %s: %d" % (k, v))
    print("  うち reason_kind==unknown で『X動画video_content(恒久/一過性が区別不能)』: %d"
          % unknown_video_content_count)
    print()
    print("-- rid_source 別件数(レコード単位) --")
    for k in ("exact", "normalized", "ambiguous", "unresolved", "no_captures", "(missing)"):
        print("  %s: %d" % (k, rid_source_counts.get(k, 0)))
    for k, v in rid_source_counts.items():
        if k not in ("exact", "normalized", "ambiguous", "unresolved", "no_captures", "(missing)"):
            print("  %s: %d" % (k, v))
    if any(k.startswith("legacy_") for k in rid_source_counts):
        print()
        print("  ⚠️ legacy_* は『記録時の事実』ではなく『照会時に captures.json で救済できた』")
        print("     という意味。captures.json は毎晩再生成されるので、同じ履歴でも後日の照会で")
        print("     結果が変わりうる（再現性が無い）。恒久的な事実として扱わないこと。")
        print("     raindrop_id が記録時に入っているレコード(exact/normalized)だけが再現可能。")
    return 0


def _usage():
    print(__doc__ if __doc__ else "")
    print("使い方:")
    print("  python3 ledger.py <rid>        # そのridの状態を表示")
    print("  python3 ledger.py --summary    # 全体集計を表示")
    print("  python3 ledger.py --url <url>  # URLで引く(ridが未解決の件を調べる用)")
    print()
    print("  --include-legacy を足すと、記録時に raindrop_id を持たない旧レコードも")
    print("  captures.json で rid を引き直して集計に混ぜる。ただしこの結び付けは")
    print("  captures.json の最新状態に依存し、後日変わりうる（再現可能な事実ではない）。")
    print("  既定ではオフで、記録時に確定した rid だけを答える。")


def main(argv):
    global INCLUDE_LEGACY
    if "--include-legacy" in argv:
        INCLUDE_LEGACY = True
        argv = [a for a in argv if a != "--include-legacy"]
    if not argv:
        _usage()
        return 0
    if argv[0] == "--summary":
        return cmd_summary()
    if argv[0] == "--url":
        if len(argv) < 2:
            print("エラー: --url には URL を指定してください")
            return 1
        return cmd_url(argv[1])
    return cmd_rid(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
