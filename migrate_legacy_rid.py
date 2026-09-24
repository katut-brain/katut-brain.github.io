#!/usr/bin/env python3
# migrate_legacy_rid.py — fetch_facts/*.json の旧レコード（raindrop_id を持たない）に
# rid を付ける一度きりの移行スクリプト。
#
# 設計（ユーザー裁定済み方式）:
#   - 正本は Raindrop REST API（GET のみ）。captures.json は使わない
#     （captures.json は毎晩再生成される二次キャッシュで、削除済みの旧保存を
#     引けない可能性がある。API が唯一の一次情報源）。
#   - URL→rid の照合は fetch_content._normalize_url_for_rid（generic正規化）と
#     select_targets.url_key（X/Instagram/Threads の強いID）を **そのまま流用**する。
#     新しい正規化ロジックはここに書かない（url-identity-normalization.md の教訓:
#     同じ正規化を2箇所に書くと片方が腐る）。
#   - 候補が1件に定まる時だけ確定。0件=raindrop_missing、複数=raindrop_ambiguous。
#   - reviews/*.html のカード data-rid とも突き合わせる。カードの data-rid がある
#     場合は API 一致時だけ確定、不一致は card_rid_mismatch。
#   - 既定は dry-run。--apply を付けたときだけ fetch_facts/*.json を書き換える。
#   - 冪等: 既に raindrop_id または legacy_rid_unresolved を持つレコードは
#     「移行済み」として一切触らない（再実行しても差分が出ない）。
#
# 使い方:
#   python3 migrate_legacy_rid.py                  # dry-run（既定）
#   python3 migrate_legacy_rid.py --apply           # 実際に書き込む
#   python3 migrate_legacy_rid.py --since 2026-08-23 --until 2026-09-01

import sys, os, io, json, glob, time, argparse, datetime
import urllib.request, urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import fetch_content as fc   # _normalize_url_for_rid を流用（正本は fetch_content.py）
import select_targets as st  # url_key/extract_vcards/card_rid/card_href を流用

FACTS_DIR = os.environ.get("FETCH_FACTS_DIR") or os.path.join(HERE, "fetch_facts")
REVIEWS_DIR = os.environ.get("MIGRATE_REVIEWS_DIR") or os.path.join(HERE, "reviews")

RAINDROP_TOKEN_ENV = "RAINDROP_TOKEN"
# _build_graph.py と同じ解決規則（トップレベルでAPIを叩く _build_graph.py 自体は
# import しない。import すると即座にネットワークアクセスが走ってしまうため）。
LOCAL_TOKEN_PATH = r"C:\Users\katut\Documents\ObsidianVault\.obsidian\plugins\make-it-rain\data.json"

MAX_PAGES = 50          # perpage=50 * 50page = 2500件の安全弁（raindrop-api-pagination-50limit.md）
PAGE_RETRIES = 3
PAGE_RETRY_WAIT = 2.0

REASON_CODES = ("raindrop_missing", "raindrop_ambiguous", "card_rid_mismatch")


# ---------------- トークン解決 ----------------

def get_raindrop_token():
    tok = os.environ.get(RAINDROP_TOKEN_ENV)
    if tok:
        return tok.strip()
    try:
        return json.load(io.open(LOCAL_TOKEN_PATH, encoding="utf-8")).get("apiToken")
    except Exception:
        return None


# ---------------- Raindrop API 取得（GETのみ・ページング対応） ----------------

def _http_get_json(url, token, timeout=30):
    """実際のHTTP GET。テストではこの関数を差し替える。"""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_raindrops(search_query, token, http_get=_http_get_json, page_retries=PAGE_RETRIES,
                     retry_wait=PAGE_RETRY_WAIT, max_pages=MAX_PAGES):
    """search クエリで raindrops を全ページ取得する。
    戻り値: (items, complete, errors)。complete は API count と取得件数が一致したか。"""
    items = []
    errors = []
    expected_count = None
    page = 0
    q = urllib.parse.quote(search_query)
    while page < max_pages:
        page_items = None
        for attempt in range(page_retries):
            try:
                url = ("https://api.raindrop.io/rest/v1/raindrops/0"
                       "?perpage=50&sort=-created&page=%d&search=%s") % (page, q)
                data = http_get(url, token)
                if expected_count is None:
                    expected_count = data.get("count")
                page_items = data.get("items", [])
                break
            except Exception as e:
                if attempt + 1 < page_retries:
                    time.sleep(retry_wait)
                else:
                    errors.append("page=%d: %s" % (page, e))
        if page_items is None:
            page += 1
            continue
        if not page_items:
            break
        items.extend(page_items)
        page += 1
    if page >= max_pages:
        errors.append("MAX_PAGES=%d に到達（取りこぼしの可能性あり）" % max_pages)
    complete = expected_count is not None and expected_count == len(items)
    if expected_count is not None and not complete:
        errors.append("count不一致(api count=%s 取得=%d)" % (expected_count, len(items)))
    return items, complete, errors


# ---------------- URL照合（既存正規化を流用。新しい正規化は作らない） ----------------

def identity_key(url):
    """(kind, key) を返す。kind は "strong"（X/Instagram/Threads の強いID）
    または "norm"（fetch_content の generic 正規化）。
    同じURLは常に同じkeyになるので、target側・candidate側どちらに使っても
    タプル同士の比較でそのまま照合できる。"""
    if not isinstance(url, str) or not url:
        return ("norm", "")
    key = st.url_key(url)
    if key is not None and key[0] != "generic":
        return ("strong", key)
    return ("norm", fc._normalize_url_for_rid(url))


def find_api_matches(url, candidates):
    """candidates（Raindrop APIのitemsそのもの。'link'キーを持つdict想定）のうち、
    identity_key が一致するものを返す。"""
    target = identity_key(url)
    out = []
    for c in candidates:
        link = c.get("link") or ""
        if identity_key(link) == target:
            out.append(c)
    return out


def find_card_rid(url, day, reviews_dir=REVIEWS_DIR):
    """reviews/<day>.html 内で、target URL と identity_key が一致する href を持つ
    カードを探す。戻り値: (rid_or_None, matched)。
    matched=True かつ rid=None は「一致するカードはあるが data-rid が1つに
    定まらない（無い、または複数の矛盾する値がある）」ことを表す。"""
    path = os.path.join(reviews_dir, "%s.html" % day)
    if not os.path.isfile(path):
        return None, False
    try:
        with io.open(path, encoding="utf-8", newline="") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return None, False
    target = identity_key(url)
    cards, _malformed = st.extract_vcards(raw)
    matched_any = False
    rid_values = set()
    for c in cards:
        href = st.card_href(c["raw"])
        if not href:
            continue
        if identity_key(href) != target:
            continue
        matched_any = True
        rid_str = st.card_rid(c["raw"])
        if rid_str is not None:
            try:
                rid_values.add(int(rid_str))
            except ValueError:
                pass
    if not matched_any:
        return None, False
    if len(rid_values) == 1:
        return next(iter(rid_values)), True
    return None, True   # カードはあるが data-rid 無し、または複数の矛盾する値


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def resolve_record(url, day, candidates, reviews_dir=REVIEWS_DIR, now=None):
    """1レコードぶんの解決を行う。戻り値は dict:
      確定時: {"status": "resolved", "rid": int, "method": "strong"|"norm",
               "checked_at": iso, "card_review_date": day-or-None}
      未確定時: {"status": "unresolved", "reason": <REASON_CODES>, "checked_at": iso,
                 その他デバッグ情報}
    """
    checked_at = now or _now_iso()
    key = identity_key(url)
    method = key[0]
    matches = find_api_matches(url, candidates)
    if len(matches) == 0:
        return {"status": "unresolved", "reason": "raindrop_missing",
                "checked_at": checked_at, "method": method}
    if len(matches) > 1:
        return {"status": "unresolved", "reason": "raindrop_ambiguous",
                "checked_at": checked_at, "method": method,
                "candidate_rids": sorted(c.get("_id") for c in matches
                                          if isinstance(c.get("_id"), int))}
    api_rid = matches[0].get("_id")
    if not isinstance(api_rid, int):
        return {"status": "unresolved", "reason": "raindrop_missing",
                "checked_at": checked_at, "method": method}
    card_rid, card_matched = find_card_rid(url, day, reviews_dir=reviews_dir)
    if card_matched and card_rid is not None and card_rid != api_rid:
        return {"status": "unresolved", "reason": "card_rid_mismatch",
                "checked_at": checked_at, "method": method,
                "api_rid": api_rid, "card_rid": card_rid}
    card_review_date = day if (card_matched and card_rid == api_rid) else None
    return {"status": "resolved", "rid": api_rid, "method": method,
            "checked_at": checked_at, "card_review_date": card_review_date}


# ---------------- fetch_facts/*.json の走査 ----------------

DATE_RE_FILE = os.path.join(FACTS_DIR, "*.json")


def _is_legacy(rec):
    """記録時に raindrop_id を持たず、かつ未処理（raindrop_id/legacy_rid_unresolved
    のどちらも無い）レコードか。"""
    if isinstance(rec.get("raindrop_id"), int):
        return False
    if "legacy_rid_unresolved" in rec:
        return False
    return True


def collect_legacy_targets(facts_dir=FACTS_DIR, since=None, until=None):
    """(day, url, rec, path) のリストを返す。since/until は 'YYYY-MM-DD' 文字列
    （両端含む）。None なら制限なし。"""
    out = []
    for path in sorted(glob.glob(os.path.join(facts_dir, "*.json"))):
        day = os.path.splitext(os.path.basename(path))[0]
        if since and day < since:
            continue
        if until and day > until:
            continue
        try:
            with io.open(path, encoding="utf-8") as f:
                store = json.load(f)
        except Exception as e:
            print("WARN: 読み込み失敗のためスキップ: %s (%s)" % (path, e), file=sys.stderr)
            continue
        if not isinstance(store, dict):
            continue
        for url, rec in store.items():
            if not isinstance(rec, dict):
                continue
            if _is_legacy(rec):
                out.append((day, url, rec, path))
    return out


def _buffered_range(since, until, buffer_days=1):
    """since/until の日付文字列から、JST/UTCの日跨ぎ(±1日)を吸収するための
    バッファ付き検索範囲を作る（raindrop-utc-jst-date-mismatch.md）。"""
    s = datetime.date.fromisoformat(since) - datetime.timedelta(days=buffer_days)
    u = datetime.date.fromisoformat(until) + datetime.timedelta(days=buffer_days)
    return s.isoformat(), u.isoformat()


def build_search_query(since, until):
    lo, hi = _buffered_range(since, until)
    return "created:>%s created:<%s" % (lo, hi)


# ---------------- 書き込み（--apply 時のみ。fetch_content.record_facts と同じ
#                   原子的更新の流儀: tmp書き込み→os.replace） ----------------

def _atomic_write_json(path, store):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(store, ensure_ascii=False, indent=1) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def apply_resolution(path, url, resolution):
    """1件ぶんの解決結果を fetch_facts/<day>.json に書き戻す。
    既存キーは一切変えず、新しいキーだけ足す（呼び出し側で冪等性を保証済みの
    前提だが、念のためここでも「既に処理済みなら何もしない」を守る）。"""
    with io.open(path, encoding="utf-8") as f:
        store = json.load(f)
    rec = store.get(url)
    if not isinstance(rec, dict):
        return False
    if not _is_legacy(rec):
        return False   # 既に処理済み。冪等性を守るため何もしない
    if resolution["status"] == "resolved":
        rec["raindrop_id"] = resolution["rid"]
        rec["rid_source"] = "legacy_migrated"
        rec["rid_evidence"] = {
            "method": resolution["method"],
            "api_checked_at": resolution["checked_at"],
            "card_review_date": resolution.get("card_review_date"),
        }
    else:
        rec["legacy_rid_unresolved"] = resolution["reason"]
        rec["checked_at"] = resolution["checked_at"]
    _atomic_write_json(path, store)
    return True


# ---------------- 表示（dry-run / apply 共通） ----------------

def _fmt_resolution(resolution):
    if resolution["status"] == "resolved":
        card = resolution.get("card_review_date")
        card_str = ("card確認済み(%s)" % card) if card else "card確認なし"
        return "確定 rid=%s method=%s %s" % (resolution["rid"], resolution["method"], card_str)
    reason = resolution["reason"]
    extra = ""
    if reason == "raindrop_ambiguous":
        extra = " candidates=%s" % resolution.get("candidate_rids")
    elif reason == "card_rid_mismatch":
        extra = " api_rid=%s card_rid=%s" % (resolution.get("api_rid"), resolution.get("card_rid"))
    return "%s%s" % (reason, extra)


def run(since=None, until=None, apply=False, facts_dir=FACTS_DIR, reviews_dir=REVIEWS_DIR,
        token=None, http_get=_http_get_json, now=None):
    targets = collect_legacy_targets(facts_dir=facts_dir, since=since, until=until)
    if not targets:
        print("対象レコード無し（旧レコードは既に無い、または --since/--until の範囲外）")
        return 0

    days = sorted(set(t[0] for t in targets))
    search_since, search_until = days[0], days[-1]
    query = build_search_query(search_since, search_until)

    token = token or get_raindrop_token()
    if not token:
        print("エラー: Raindrop トークンが取得できません"
              "（RAINDROP_TOKEN 未設定・ローカル設定も読めません）")
        return 1

    print("=== migrate_legacy_rid: 対象 %d 件（%s 〜 %s） ===" % (len(targets), days[0], days[-1]))
    print("Raindrop API 検索クエリ: %s" % query)
    candidates, complete, errors = fetch_raindrops(query, token, http_get=http_get)
    print("API取得件数: %d 件（complete=%s）" % (len(candidates), complete))
    for e in errors:
        print("  WARN: %s" % e)
    print()

    counts = {"resolved": 0, "raindrop_missing": 0, "raindrop_ambiguous": 0,
              "card_rid_mismatch": 0}
    card_checked = 0
    card_confirmed = 0
    apply_changed_files = {}

    for day, url, rec, path in targets:
        resolution = resolve_record(url, day, candidates, reviews_dir=reviews_dir, now=now)
        print("[%s] %s" % (day, url))
        print("  -> %s" % _fmt_resolution(resolution))
        if resolution["status"] == "resolved":
            counts["resolved"] += 1
            if resolution.get("card_review_date"):
                card_checked += 1
                card_confirmed += 1
        else:
            counts[resolution["reason"]] += 1
            if resolution["reason"] == "card_rid_mismatch":
                card_checked += 1
        if apply:
            changed = apply_resolution(path, url, resolution)
            if changed:
                apply_changed_files[path] = apply_changed_files.get(path, 0) + 1

    print()
    print("=== 内訳 ===")
    print("  確定(resolved):          %d" % counts["resolved"])
    print("  raindrop_missing:        %d" % counts["raindrop_missing"])
    print("  raindrop_ambiguous:      %d" % counts["raindrop_ambiguous"])
    print("  card_rid_mismatch:       %d" % counts["card_rid_mismatch"])
    print("  data-rid照合を行った件数: %d（うち一致: %d）" % (card_checked, card_confirmed))

    if apply:
        print()
        print("=== 書き込み結果 ===")
        if apply_changed_files:
            for p, n in sorted(apply_changed_files.items()):
                print("  %s: %d件" % (p, n))
        else:
            print("  変更なし")
    else:
        print()
        total_files = len(set(t[3] for t in targets))
        print("--apply すると %d ファイル・%d 件が変わります"
              "（dry-run のため今回は書き込んでいません）" % (total_files, len(targets)))
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                         help="実際に fetch_facts/*.json へ書き込む（既定はdry-run）")
    parser.add_argument("--since", default=None, help="対象の下限日(YYYY-MM-DD、両端含む)")
    parser.add_argument("--until", default=None, help="対象の上限日(YYYY-MM-DD、両端含む)")
    args = parser.parse_args(argv)
    return run(since=args.since, until=args.until, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
