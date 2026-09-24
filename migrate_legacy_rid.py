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
#   - API取得が不完全（ページ取得エラー・count不一致）なら、その回の対象は
#     一切確定させない（api_incomplete。dry-runでも resolved扱いにせず、
#     --applyでも一切書かない＝次回API正常時に再挑戦できる状態を保つ）
#     （2026-09-24 差し戻し P0-1）。
#   - reviews/*.html のカード data-rid とも突き合わせる。カードの data-rid がある
#     場合は API 一致時だけ確定、不一致は card_rid_mismatch。同一URLに対応する
#     カードが複数あり data-rid が矛盾する、または data-rid が数値でない場合は
#     card_rid_conflict とし、API単独でも確定させない（2026-09-24 差し戻し P0-2）。
#   - 空URL・正規化結果が空のキー同士を一致させない（2026-09-24 差し戻し P0-3）。
#   - 既定は dry-run。--apply を付けたときだけ fetch_facts/*.json を書き換える。
#   - 冪等: 既に raindrop_id または legacy_rid_unresolved を持つレコードは
#     「移行済み」として一切触らない（再実行しても差分が出ない）。
#   - 書き込み直前に対象ファイルを再読込し、収集時点(dry-run表示の材料を
#     読んだ時点)から内容が変わっていれば書かずにエラー（楽観ロック。
#     2026-09-24 差し戻し P1-2）。ただし、このスクリプト自体は
#     fetch_content.record_facts() と同じ**シングルライター前提**で動く
#     （無人ロック待ちで運用を止めない方針をここでも踏襲する）。この楽観ロックは
#     「このプロセス実行中に他プロセスが同じファイルを書き換えた」ような
#     想定外の競合を検知して安全側に倒すための防御であって、複数ライターの
#     並行実行を前提にした排他制御ではない。
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

REASON_CODES = ("raindrop_missing", "raindrop_ambiguous", "card_rid_mismatch",
                "card_rid_conflict", "api_incomplete")

# --applyでも一切書かない理由コード（一時的な取得不備。次回に再挑戦させるため
# legacy_rid_unresolvedタグを焼き付けない。2026-09-24 P0-1）。
_NO_WRITE_REASONS = ("api_incomplete",)


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
    戻り値: (items, complete, errors)。complete は「1ページもエラーにならず、かつ
    API count と取得件数が一致した」ことを表す（2026-09-24 P0-1: ページ取得エラーが
    1件でもあれば、たとえcountがたまたま一致していても complete=False とし、
    その回の全対象を確定させない安全側に倒す）。"""
    items = []
    errors = []
    expected_count = None
    page = 0
    page_had_error = False
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
            page_had_error = True
            page += 1
            continue
        if not page_items:
            break
        items.extend(page_items)
        page += 1
    if page >= max_pages:
        errors.append("MAX_PAGES=%d に到達（取りこぼしの可能性あり）" % max_pages)
        page_had_error = True
    count_ok = expected_count is not None and expected_count == len(items)
    if expected_count is not None and not count_ok:
        errors.append("count不一致(api count=%s 取得=%d)" % (expected_count, len(items)))
    complete = count_ok and not page_had_error
    return items, complete, errors


# ---------------- URL照合（既存正規化を流用。新しい正規化は作らない） ----------------

def identity_key(url):
    """(kind, key) を返す。kind は "strong"（X/Instagram/Threads の強いID）
    または "norm"（fetch_content の generic 正規化）。
    同じURLは常に同じkeyになるので、target側・candidate側どちらに使っても
    タプル同士の比較でそのまま照合できる。

    空URL・正規化結果が空になったURLは None を返す（2026-09-24 P0-3: 空キー同士を
    一致させない。呼び出し側は None を「絶対に何とも一致しないキー」として扱う）。
    """
    if not isinstance(url, str) or not url.strip():
        return None
    key = st.url_key(url)
    if key is not None and key[0] != "generic":
        return ("strong", key)
    norm = fc._normalize_url_for_rid(url)
    if not norm or not norm.strip() or norm in ("://",):
        return None
    return ("norm", norm)


def find_api_matches(url, candidates):
    """candidates（Raindrop APIのitemsそのもの。'link'キーを持つdict想定）のうち、
    identity_key が一致するものを返す。target/candidate どちらかのキーが None
    （空URL・正規化結果が空）のときは絶対に一致させない（P0-3）。"""
    target = identity_key(url)
    if target is None:
        return []
    out = []
    for c in candidates:
        link = c.get("link") or ""
        cand_key = identity_key(link)
        if cand_key is None:
            continue
        if cand_key == target:
            out.append(c)
    return out


def find_card_rid(url, day, reviews_dir=REVIEWS_DIR):
    """reviews/<day>.html 内で、target URL と identity_key が一致する href を持つ
    カードを探す。戻り値: (rid_or_None, matched, conflict)。
      - matched=False: 一致するカードが無い（reviewファイル欠落・空URL含む）
      - matched=True, conflict=False, rid=int: data-ridが1つに定まった
      - matched=True, conflict=False, rid=None: 一致カードはあるが誰もdata-ridを
        持たない（API単独で確定してよい）
      - matched=True, conflict=True, rid=None: 一致カードの中に
        (a) 複数の異なる数値data-ridがある、または
        (b) 数値でないdata-rid属性を持つカードがある
        （2026-09-24 P0-2: どちらもAPI単独では確定させない）
    """
    target = identity_key(url)
    if target is None:
        return None, False, False
    path = os.path.join(reviews_dir, "%s.html" % day)
    if not os.path.isfile(path):
        return None, False, False
    try:
        with io.open(path, encoding="utf-8", newline="") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return None, False, False
    cards, _malformed = st.extract_vcards(raw)
    matched_any = False
    rid_values = set()
    invalid_rid_seen = False
    for c in cards:
        href = st.card_href(c["raw"])
        if not href:
            continue
        if identity_key(href) != target:
            continue
        matched_any = True
        raw_attr = st._get_attr(c["raw"], "data-rid")
        if raw_attr is None:
            # data-rid属性自体が無いカード。select_targets.RID_ATTR_RE は
            # 数値(符号付き)しか拾わないため、_get_attr で属性値そのものを見て
            # 「非数値のdata-ridが付いている」ケースを取り逃さないようにする。
            continue
        rid_str = st.card_rid(c["raw"])
        if rid_str is None:
            # 属性はあるが数値でない(RID_ATTR_REにマッチしない)＝異常値。
            invalid_rid_seen = True
            continue
        try:
            rid_values.add(int(rid_str))
        except ValueError:
            invalid_rid_seen = True
    if not matched_any:
        return None, False, False
    if invalid_rid_seen or len(rid_values) > 1:
        return None, True, True
    if len(rid_values) == 1:
        return next(iter(rid_values)), True, False
    return None, True, False


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def resolve_record(url, day, candidates, reviews_dir=REVIEWS_DIR, now=None,
                    api_complete=True, api_query=None):
    """1レコードぶんの解決を行う。戻り値は dict:
      確定時: {"status": "resolved", "rid": int, "method": "strong"|"norm",
               "checked_at": iso, "card_review_date": day-or-None,
               "api_complete": True, "api_query": str, "api_link": str,
               "matched_key": (kind, key)}
      未確定時: {"status": "unresolved", "reason": <REASON_CODES>, "checked_at": iso,
                 その他デバッグ情報}

    api_complete が False のときは、matching を一切試みずに api_incomplete で
    即座に返す（2026-09-24 P0-1: 不完全なAPI結果を根拠に確定させない）。
    """
    checked_at = now or _now_iso()
    key = identity_key(url)
    method = key[0] if key is not None else "invalid"

    if not api_complete:
        return {"status": "unresolved", "reason": "api_incomplete",
                "checked_at": checked_at, "method": method, "api_query": api_query}

    matches = find_api_matches(url, candidates)
    if len(matches) == 0:
        return {"status": "unresolved", "reason": "raindrop_missing",
                "checked_at": checked_at, "method": method, "api_query": api_query}
    if len(matches) > 1:
        return {"status": "unresolved", "reason": "raindrop_ambiguous",
                "checked_at": checked_at, "method": method, "api_query": api_query,
                "candidate_rids": sorted(c.get("_id") for c in matches
                                          if isinstance(c.get("_id"), int))}
    api_item = matches[0]
    api_rid = api_item.get("_id")
    if not isinstance(api_rid, int):
        return {"status": "unresolved", "reason": "raindrop_missing",
                "checked_at": checked_at, "method": method, "api_query": api_query}

    card_rid, card_matched, card_conflict = find_card_rid(url, day, reviews_dir=reviews_dir)
    if card_conflict:
        return {"status": "unresolved", "reason": "card_rid_conflict",
                "checked_at": checked_at, "method": method, "api_query": api_query,
                "api_rid": api_rid}
    if card_matched and card_rid is not None and card_rid != api_rid:
        return {"status": "unresolved", "reason": "card_rid_mismatch",
                "checked_at": checked_at, "method": method, "api_query": api_query,
                "api_rid": api_rid, "card_rid": card_rid}
    card_review_date = day if (card_matched and card_rid == api_rid) else None
    return {"status": "resolved", "rid": api_rid, "method": method,
            "checked_at": checked_at, "card_review_date": card_review_date,
            "api_complete": True, "api_query": api_query,
            "api_link": api_item.get("link"), "matched_key": key}


# ---------------- fetch_facts/*.json の走査 ----------------

def _is_legacy(rec):
    """記録時に raindrop_id を持たず、かつ未処理（raindrop_id/legacy_rid_unresolved
    のどちらも無い）レコードか。"""
    if isinstance(rec.get("raindrop_id"), int):
        return False
    if "legacy_rid_unresolved" in rec:
        return False
    return True


def collect_legacy_targets(facts_dir=FACTS_DIR, since=None, until=None):
    """(day, url, rec, path, raw_text) のリストを返す。since/until は
    'YYYY-MM-DD' 文字列（両端含む）。None なら制限なし。raw_text はそのファイルを
    読んだ時点の生テキストで、apply時の楽観ロック（P1-2）の基準値として使う。"""
    out = []
    for path in sorted(glob.glob(os.path.join(facts_dir, "*.json"))):
        day = os.path.splitext(os.path.basename(path))[0]
        if since and day < since:
            continue
        if until and day > until:
            continue
        try:
            with io.open(path, encoding="utf-8") as f:
                raw_text = f.read()
            store = json.loads(raw_text)
        except Exception as e:
            print("WARN: 読み込み失敗のためスキップ: %s (%s)" % (path, e), file=sys.stderr)
            continue
        if not isinstance(store, dict):
            continue
        for url, rec in store.items():
            if not isinstance(rec, dict):
                continue
            if _is_legacy(rec):
                out.append((day, url, rec, path, raw_text))
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


def apply_resolution(path, url, resolution, expected_raw):
    """1件ぶんの解決結果を fetch_facts/<day>.json に書き戻す。
    既存キーは一切変えず、新しいキーだけ足す（呼び出し側で冪等性を保証済みの
    前提だが、念のためここでも「既に処理済みなら何もしない」を守る）。

    書き込み直前にファイルを再読込し、expected_raw（このプロセスが対象を収集
    した時点の生テキスト。同一ファイル内で複数件を続けて書く場合は、直前の
    自分自身の書き込み結果を呼び出し側が渡す）と一致しなければ、
    「収集後に何者かがこのファイルを書き換えた」とみなして一切書かずにエラーを
    返す（2026-09-24 P1-2 楽観ロック。本スクリプトはシングルライター前提で動く
    ため、これは想定外の競合を検知する防御であって、通常運用で頻発すべきもの
    ではない）。

    戻り値: (changed: bool, error: str または None)
    """
    if resolution["status"] == "unresolved" and resolution["reason"] in _NO_WRITE_REASONS:
        return False, None   # 一時的な取得不備。何も書かない(次回に再挑戦させる)

    try:
        with io.open(path, encoding="utf-8") as f:
            current_raw = f.read()
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return False, "読み込み失敗のため書き込み中止: %s (%s)" % (path, e)

    if current_raw != expected_raw:
        return False, ("楽観ロック競合: %s は収集後に内容が変わっていたため書き込みを"
                        "中止しました" % path)

    try:
        store = json.loads(current_raw)
    except ValueError as e:
        return False, "JSON parse失敗のため書き込み中止: %s (%s)" % (path, e)
    rec = store.get(url)
    if not isinstance(rec, dict):
        return False, None
    if not _is_legacy(rec):
        return False, None   # 既に処理済み。冪等性を守るため何もしない
    if resolution["status"] == "resolved":
        rec["raindrop_id"] = resolution["rid"]
        rec["rid_source"] = "legacy_migrated"
        rec["rid_evidence"] = {
            "method": resolution["method"],
            "api_checked_at": resolution["checked_at"],
            "card_review_date": resolution.get("card_review_date"),
            "api_complete": resolution.get("api_complete"),
            "api_query": resolution.get("api_query"),
            "api_link": resolution.get("api_link"),
            "matched_key": resolution.get("matched_key"),
        }
    else:
        rec["legacy_rid_unresolved"] = resolution["reason"]
        rec["checked_at"] = resolution["checked_at"]
    _atomic_write_json(path, store)
    return True, None


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
    elif reason == "card_rid_conflict":
        extra = " api_rid=%s（カード側のdata-ridが矛盾/非数値）" % resolution.get("api_rid")
    elif reason == "api_incomplete":
        extra = "（API取得が不完全なため今回は判定しません。次回再挑戦）"
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
    candidates, api_complete, errors = fetch_raindrops(query, token, http_get=http_get)
    print("API取得件数: %d 件（complete=%s）" % (len(candidates), api_complete))
    for e in errors:
        print("  WARN: %s" % e)
    if not api_complete:
        print("  ⚠️ API取得が不完全なため、今回の対象は全件 api_incomplete として"
              "確定させません（--apply でも書きません）。")
    print()

    counts = {"resolved": 0, "raindrop_missing": 0, "raindrop_ambiguous": 0,
              "card_rid_mismatch": 0, "card_rid_conflict": 0, "api_incomplete": 0}
    card_checked = 0
    card_confirmed = 0
    apply_changed_files = {}
    lock_conflicts = []
    # 同一ファイルに複数件書くとき、直前の自分の書き込み後の内容を次の
    # expected_raw として使う（P1-2。書いた直後の自分の変更を「競合」と
    # 誤検知しないため）。
    raw_by_path = {}

    for day, url, rec, path, raw_text in targets:
        if path not in raw_by_path:
            raw_by_path[path] = raw_text
        resolution = resolve_record(url, day, candidates, reviews_dir=reviews_dir, now=now,
                                     api_complete=api_complete, api_query=query)
        print("[%s] %s" % (day, url))
        print("  -> %s" % _fmt_resolution(resolution))
        if resolution["status"] == "resolved":
            counts["resolved"] += 1
            if resolution.get("card_review_date"):
                card_checked += 1
                card_confirmed += 1
        else:
            counts[resolution["reason"]] += 1
            if resolution["reason"] in ("card_rid_mismatch", "card_rid_conflict"):
                card_checked += 1
        if apply:
            changed, err = apply_resolution(path, url, resolution, raw_by_path[path])
            if changed:
                apply_changed_files[path] = apply_changed_files.get(path, 0) + 1
                with io.open(path, encoding="utf-8") as f:
                    raw_by_path[path] = f.read()
            if err:
                lock_conflicts.append(err)
                print("  ⚠️ %s" % err)

    print()
    print("=== 内訳 ===")
    print("  確定(resolved):          %d" % counts["resolved"])
    print("  raindrop_missing:        %d" % counts["raindrop_missing"])
    print("  raindrop_ambiguous:      %d" % counts["raindrop_ambiguous"])
    print("  card_rid_mismatch:       %d" % counts["card_rid_mismatch"])
    print("  card_rid_conflict:       %d" % counts["card_rid_conflict"])
    print("  api_incomplete:          %d" % counts["api_incomplete"])
    print("  data-rid照合を行った件数: %d（うち一致: %d）" % (card_checked, card_confirmed))

    if apply:
        print()
        print("=== 書き込み結果 ===")
        if apply_changed_files:
            for p, n in sorted(apply_changed_files.items()):
                print("  %s: %d件" % (p, n))
        else:
            print("  変更なし")
        if lock_conflicts:
            print("  ⚠️ 楽観ロック等のエラーが %d 件ありました（上記ログ参照）" % len(lock_conflicts))
    else:
        print()
        total_files = len(set(t[3] for t in targets))
        writable = len(targets) - counts["api_incomplete"]
        print("--apply すると最大 %d ファイル・%d 件が変わります"
              "（dry-run のため今回は書き込んでいません。api_incomplete=%d件は"
              "--apply でも書き込まれません）"
              % (total_files, writable, counts["api_incomplete"]))
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
