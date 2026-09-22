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
# rid が無いレコードの扱い（2026-09-22 CEO裁定で変更）:
#   当初は rid の無いレコードを選定対象から除外していたが、それでは rid が
#   永久に無いままのレコードが未来永劫「取りこぼし」に留まる。
#   build_capture_index.py の synthetic_rid()（rid を持たないレコードに
#   date+source から作る安定した負整数を振る規則）をこのモジュールへ移し
#   （正本は1か所・import で共有。build_capture_index.py 側は
#   `select_targets.synthetic_rid` を使う）、rid の無いレコードも
#   synthetic_rid で選定対象に含める。`data-rid` は選定JSONの `rid` を
#   そのまま書けばよく（手順書 cloud_routine_prompt.md に明記）、負数の
#   data-rid も reviewed_index() のカード抽出（RID_ATTR_RE は符号付き数値に
#   対応済み）で正しく「掲載済み」判定できる。
#
# capture_index.json との関係: 参考情報として「captures.json には無いが
# capture_index.json にはある rid」を数える（index_only）。これは過去に captures.json
# から消えた（Raindrop側で削除等）が一度は観測された rid で、本文材料が無いので
# 選定はしない。
#
# 既知の限界（2026-09-22 Codexレビュー2周目 指摘。対応不要と判断し、コードは
# 変えずここに明記するに留める）:
#   - 永続作業ツリーでの再実行は考慮していない（クラウドRoutineは毎晩新規
#     clone で動く前提。ローカルで同じ作業ツリーを使い回して繰り返し実行する
#     運用には別途の考慮が要る）。
#   - synthetic_rid() は date+source のハッシュなので、理論上は衝突しうる
#     （実データはcaptures.json全件が実rid持ちで発生しないが、rid無し
#     レコードが増えた場合に確率的な衝突リスクが残る）。

import sys
import os
import io
import json
import re
import glob
import hashlib
import html as html_module
import argparse
import datetime
import traceback
from urllib.parse import urlsplit, parse_qsl, urlencode

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

# 下限日。これより前(date < SINCE)のレコードは選定対象にしない。
# 2026-06-13 はパイプライン初回稼働日で、それ以前の保存48件は初期グラフ/
# 思考マップ用に一括取り込みしたもの。日次振り返りの仕組み自体がまだ無かった
# 期間なので「取りこぼし」ではない（2026-09-22 ユーザー裁定）。
# SELECT_SINCE 環境変数で上書き可能（YYYY-MM-DD 形式以外は無視して既定値を使う）。
DEFAULT_SINCE = "2026-06-14"


def _resolve_since():
    val = os.environ.get("SELECT_SINCE")
    if isinstance(val, str) and DATE_RE.match(val):
        try:
            datetime.date.fromisoformat(val)
            return val
        except ValueError:
            pass
    return DEFAULT_SINCE


SINCE = _resolve_since()


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


def synthetic_rid(rec):
    """`rid` を持たないレコードに、安定した識別子を振る。

    build_capture_index.py と同じ規則（正本はここ1か所）。Raindrop の rid は
    常に正の整数なので、**負数**にして合成だと見て分かるようにする。
    date と source から決定的に作るので、同じレコードなら毎回同じ値になる
    （captures.json は毎晩作り直されるが、date/source が変わらなければ
    synthetic_rid も変わらない＝再実行しても同じレコードとして掲載済み判定が
    効く）。
    """
    parts = [str(rec.get("date") or "")[:10], str(rec.get("source") or "")]
    key = chr(31).join(parts)   # 日付にも URL にも現れない区切り
    return -int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:12], 16)


def load_captures(path=CAPTURES):
    """captures.json を読み、(records, synthetic_rid_count) を返す。

    トップレベルは list（実測）または {"captures": [...]} のどちらでも許容する
    （build_capture_index.py の captures_by_day() と同じ寛容さ）。

    rid が無いレコードは synthetic_rid() で安定した負整数を振って選定対象に
    含める（2026-09-22 CEO裁定。rid が付かないまま永久に取りこぼされるのを
    防ぐ）。synthetic_rid_count はそのうち何件が合成rid経由だったかの参考値。
    """
    data = _load_json(path)
    records = data if isinstance(data, list) else (data or {}).get("captures", [])
    if not isinstance(records, list):
        records = []
    out = []
    synth_count = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        # rid は captures.json の "rid" フィールドのみを見る（2026-09-22
        # Codexレビュー2周目 指摘: build_capture_index.py / stale-check.yml の
        # 台帳系は "rid" だけを正としており、ここだけ "raindrop_id" への
        # フォールバックを持つと規則が食い違う。"raindrop_id" しか無い
        # レコードは rid 無しとして扱い、synthetic_rid の経路に合流させる）。
        rid = rec.get("rid")
        if not isinstance(rid, int):
            date_for_synth = rec.get("date")
            if not isinstance(date_for_synth, str) or len(date_for_synth) < 10:
                # 合成キーの一部である date すら無い/不正なレコードは、
                # 日付でソート・SINCE判定もできないので選定対象にしない。
                continue
            rid = synthetic_rid(rec)
            synth_count += 1
        date = rec.get("date")
        if not isinstance(date, str) or len(date) < 10:
            continue
        try:
            datetime.date.fromisoformat(date[:10])
        except ValueError:
            continue
        out.append((rid, date[:10], rec))
    return out, synth_count


# --- カード単位の HTML 解析（掲載済み判定の土台。正本はここ1か所） -----------
#
# 2026-09-22 Codex敵対的レビュー P1-a 指摘への対応: URL照合を単純に
# 「reviews 全体の href 集合」対 「レコードの source」でやると、**別の rid を
# 持つカードのURLとたまたま一致**しただけで誤って掲載済み扱いにしてしまう
# （data-rid を持つカードは rid だけで既に掲載判定できているので、そのカードの
# href を URL照合の対象に混ぜる意味がない上に、誤爆の温床になる）。
# そこで reviews を **カード単位**（`.vcard` ブロック）で解析し、
#   - data-rid を持つカード → rid 集合にのみ算入（URLキー集合には入れない）
#   - data-rid を持たないカード → そのカードの href の url_key() を
#     URLキー集合に算入（rid が無いので URL照合でしか掲載済みと確認できない）
# という分離を行う。check_disclosure.py / build_capture_index.py /
# .github/workflows/stale-check.yml はすべてこの canonical な実装
# （card_index / reviewed_index）を import して使う（同じロジックを複数箇所に
# 書かない）。

_DIV_OPEN_RE = re.compile(r"<div\b[^>]*>")
_ANCHOR_OPEN_RE = re.compile(r"<a\b[^>]*>")
_ATTR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _get_attr(tag, name):
    """<div class="a b" href='...'> のようなタグ文字列から属性値を取る。
    属性の並び順・引用符の種類（"/'）の揺れに対応する。
    """
    for m in _ATTR_RE.finditer(tag):
        attr_name = m.group(1)
        if attr_name.lower() != name:
            continue
        return m.group(2) if m.group(2) is not None else m.group(3)
    return None


def _has_class_token(class_value, token):
    if not class_value:
        return False
    return token in class_value.split()


def _blank_comments(html_str):
    """`<!-- ... -->` の中身をオフセット長を保ったまま無害化したコピーを返す
    （改行はそのまま残し、他は空白に置換）。div の開閉カウントなど「構造」を
    数えるときだけこれを使う。
    """
    def repl(m):
        s = m.group(0)
        return "".join(ch if ch == "\n" else " " for ch in s)
    return _COMMENT_RE.sub(repl, html_str)


def _find_next_div_open(html_str, pos):
    """`<div` の直後が空白・`>`・`/` のときだけ開きタグとみなす
    （`<divider>` のような別要素を誤ってdivとして数えない）。
    """
    n = len(html_str)
    while True:
        i = html_str.find("<div", pos)
        if i == -1:
            return -1
        after = i + 4
        if after < n and (html_str[after].isspace() or html_str[after] in ">/"):
            return i
        pos = i + 4


def extract_vcards(review_html):
    """class トークンに "vcard" を含む `<div>` ブロックを、開閉の深さを数えて
    切り出す（check_disclosure.py の同名関数と同じアルゴリズム。正本はここ）。
    返り値は (cards, malformed) のタプル。
    cards は [{"start", "end", "raw"}, ...]。malformed は、閉じタグが
    足りずに最後まで閉じられなかったカードが1件でもあれば True。
    """
    cards = []
    malformed = False
    idx = 0
    n = len(review_html)
    structural = _blank_comments(review_html)
    for m in _DIV_OPEN_RE.finditer(structural):
        start = m.start()
        if start < idx:
            continue  # 既に前のカードの範囲に含まれている
        tag = m.group(0)
        cls = _get_attr(tag, "class")
        if not _has_class_token(cls, "vcard"):
            continue
        pos = m.end()
        depth = 1
        while depth > 0:
            next_open = _find_next_div_open(structural, pos)
            next_close = structural.find("</div>", pos)
            if next_close == -1:
                pos = n
                malformed = True
                break
            if next_open != -1 and next_open < next_close:
                depth += 1
                pos = next_open + 4
            else:
                depth -= 1
                pos = next_close + len("</div>")
        end = pos
        cards.append({"start": start, "end": end, "raw": review_html[start:end]})
        idx = end
    return cards, malformed


def card_rid(card_raw):
    """カード内の data-rid を文字列で返す（無ければ None）。"""
    m = RID_ATTR_RE.search(card_raw)
    return m.group(1) if m else None


def card_href(card_raw):
    """class トークンに "vlink" を含む最初の `<a>` タグの href を返す。"""
    for m in _ANCHOR_OPEN_RE.finditer(card_raw):
        tag = m.group(0)
        cls = _get_attr(tag, "class")
        if _has_class_token(cls, "vlink"):
            href = _get_attr(tag, "href")
            return html_module.unescape(href) if href is not None else None
    return None


# --- URL照合による掲載済み判定（data-rid 後付けの retrofit 漏れ対策） ---------
#
# data-rid が後付けで付与されなかったカード（2026-08-04 のretrofit漏れ等）は
# data-rid 集合だけでは掲載済みと判定できない。そのレコードの source が
# reviews の **data-rid を持たないカード**（`.vcard` の href。P1-a: data-rid
# を持つカードの href は対象にしない）のいずれかと一致すれば掲載済みとして扱う。
#
# ⚠️ 2026-09-22 Codexレビュー P1-a 対応で card_index()/reviewed_index() を
# カード単位（`.vcard` ブロック）の解析に作り替えた結果、当初「data-rid無しの
# retrofit漏れ3件」だと思っていた実データ（rid=1757864748/1757864938/
# 1758030878）は、実際には (a) `.vcard` の外（「🔎 深掘り」節の `<li><a>`）に
# あるだけで `.vcard` ですらない、または (b) `.vcard` ではあるが**別のrid**
# （1758030881。captures.json側は1758030878で3ずれている）を持つカードだった
# と判明した。どちらも「掲載済み」と断定してよい根拠にはならない
# （前者はそもそもカードとして掲載されていない、後者はまさにP1-aが防ごうと
# した「別ridのカードとURLが一致しただけ」の実例）ため、この3件は
# reviewed_by_url に数えなくなった（selected が9→12に増える）。
#
# 照合キーは媒体ごとに「不変で衝突しにくい識別子」を抜き出す方式にする
# （scheme+host+path の完全一致だと、X/Instagram/Threads はクエリの有無や
# 短縮パラメータの差で同じ投稿でも一致しなくなるケースが実際にある）。
# **別物に誤一致させない方を優先**する：キーが取れない・短すぎる場合は
# その URL を照合対象にしない（None を返す）。
_X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com"}
_INSTAGRAM_HOSTS = {"instagram.com"}
_THREADS_HOSTS = {"threads.net", "threads.com"}

_X_STATUS_RE = re.compile(r"/status(?:es)?/(\d+)")
_IG_SHORTCODE_RE = re.compile(r"/(?:p|reel|reels)/([A-Za-z0-9_-]+)")
_THREADS_POST_RE = re.compile(r"/post/([A-Za-z0-9_-]+)")

_MIN_KEY_LEN = 3

# generic（X/Instagram/Threads 以外）ホストのクエリから取り除く「追跡用」
# パラメータ名（2026-09-22 Codexレビュー2周目 指摘対応）。当初はクエリを
# 全部落としていたが、それだと `?id=1` と `?id=2` のような**本当に別物を指す
# クエリ**まで同一視してしまう。既知の追跡用パラメータだけを落とし、
# それ以外はクエリごと残す（後述のとおりソートして正規化する）。
# ここに挙げるのは「複数ホストにまたがって実際に観測される追跡用パラメータ」
# のみ。未知のパラメータを推測で追加しない。
_TRACKING_PARAM_NAMES = {
    "fbclid", "gclid", "igsh", "img_index", "s", "t", "ref", "xmt",
}


def _is_tracking_param(name):
    if name in _TRACKING_PARAM_NAMES:
        return True
    return name.startswith("utm_")


# URL照合フォールバックを許可する上限日（この日付**より前**の reviews ファイル
# の、data-rid を持たないカードだけが対象。2026-09-22 Codexレビュー2周目
# 指摘対応）。2026-08-04 に data-rid の後付け（retrofit）作業が行われ、
# それ以降に生成された reviews のカードには全て data-rid が付いている
# （＝以降の日付で data-rid が無いカードがあるとすれば、それはretrofit漏れ
# ではなく別の異常であり、URL照合で「掲載済み」と推測してよい根拠が無い）。
# 8/4より前の古いreviewsだけが、retrofit対象そのものとして本来 data-rid が
# 付くべきだったのに付いていない、という前提が成り立つ期間。
URL_FALLBACK_BEFORE = "2026-08-04"


def _strip_www(host):
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def url_key(url):
    """URLから媒体別の照合キーを作る。判別できない/短すぎる場合は None。

    戻り値は ("kind", key) のタプル、または None。
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = _strip_www(parts.netloc)
    path = parts.path or ""

    if host in _X_HOSTS:
        m = _X_STATUS_RE.search(path)
        if not m:
            return None
        digits = m.group(1)
        if len(digits) < _MIN_KEY_LEN:
            return None
        return ("x", digits)

    if host in _INSTAGRAM_HOSTS:
        m = _IG_SHORTCODE_RE.search(path)
        if not m:
            return None
        code = m.group(1)
        if len(code) < _MIN_KEY_LEN:
            return None
        return ("instagram", code)

    if host in _THREADS_HOSTS:
        m = _THREADS_POST_RE.search(path)
        if not m:
            return None
        pid = m.group(1)
        if len(pid) < _MIN_KEY_LEN:
            return None
        return ("threads", pid)

    # それ以外: scheme+host(小文字)+path。フラグメントは除去し、末尾スラッシュ
    # は正規化する。path が空/ルートのみは汎用すぎて誤一致のリスクが高いため
    # 照合しない。
    # クエリは**追跡用パラメータだけ**を落とし、それ以外は残す（2026-09-22
    # Codexレビュー2周目 指摘対応。クエリを全部消すと `?id=1` と `?id=2` の
    # ような別物まで同一視してしまう）。残ったパラメータはキーの並び順に
    # 依存しないよう名前でソートしてから連結する。
    if path in ("", "/"):
        return None
    norm_path = path.rstrip("/") if path != "/" else path
    scheme = (parts.scheme or "https").lower()
    kept = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    )
    base = "%s://%s%s" % (scheme, host, norm_path)
    if kept:
        base += "?" + urlencode(kept)
    return ("generic", base)


def card_index(reviews_dir=REVIEWS_DIR):
    """全 reviews/*.html をカード単位で解析し、掲載済み判定の土台を作る
    （正本はここ1か所。check_disclosure.py / build_capture_index.py /
    stale-check.yml はここを import して使う）。

    戻り値: (rid_dates, url_dates, unreadable)
      - rid_dates: {rid(int): set(date_str)} — data-rid を持つカードのみ。
        全期間の reviews が対象（日付の制限なし）。
      - url_dates: {url_key: set(date_str)} — **data-rid を持たないカードの
        うち、ファイル名の日付が URL_FALLBACK_BEFORE より前のものだけ**
        （P1-a: rid を持つカードの href を URL照合に混ぜると、別rid同士が
        たまたま同じURLを指すだけで誤って掲載済みにしてしまう。加えて
        2026-09-22 Codexレビュー2周目 指摘: 2026-08-04 の data-rid retrofit
        以降に生成された reviews は全カードに data-rid が付く前提なので、
        それ以降の日付で data-rid が無いカードをURL照合の材料にしてよい
        根拠が無い。ファイル名が YYYY-MM-DD 形式でない場合もURL照合には
        使わない＝安全側）。
      - unreadable: 読めなかったファイル名のリスト
    """
    rid_dates = {}
    url_dates = {}
    unreadable = []
    if not os.path.isdir(reviews_dir):
        return rid_dates, url_dates, unreadable
    for path in sorted(glob.glob(os.path.join(reviews_dir, "*.html"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            with io.open(path, encoding="utf-8", newline="") as fh:
                raw = fh.read()
        except (OSError, UnicodeDecodeError, ValueError):
            unreadable.append(os.path.basename(path))
            continue
        url_fallback_ok = bool(DATE_RE.match(stem)) and stem < URL_FALLBACK_BEFORE
        cards, _malformed = extract_vcards(raw)
        for c in cards:
            rid_str = card_rid(c["raw"])
            if rid_str is not None:
                try:
                    rid_int = int(rid_str)
                except ValueError:
                    rid_int = None
                if rid_int is not None:
                    rid_dates.setdefault(rid_int, set()).add(stem)
                # data-rid を持つカードの href は URL照合に使わない（P1-a）。
                continue
            if not url_fallback_ok:
                continue
            href = card_href(c["raw"])
            if href is None:
                continue
            key = url_key(href)
            if key is not None:
                url_dates.setdefault(key, set()).add(stem)
    return rid_dates, url_dates, unreadable


def reviewed_index(reviews_dir=REVIEWS_DIR):
    """card_index() を日付情報を捨てて集合へ平らにしたもの。

    戻り値: (rids: set[int], url_keys_of_ridless_cards: set[url_key], unreadable)
    「掲載済みかどうか」の判定だけが要る呼び出し元（select() 等）はこちらを使う。
    「どの日に掲載されているか」まで要る呼び出し元（check_disclosure.py の
    バックフィル理由表示など）は card_index() を直接使う。
    """
    rid_dates, url_dates, unreadable = card_index(reviews_dir)
    return set(rid_dates.keys()), set(url_dates.keys()), unreadable


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
           capture_index_path=CAPTURE_INDEX, since=None):
    """選定本体。戻り値は dict（呼び出し側が JSON化・STATUS整形する）。
    どんな例外も投げずに済むよう、呼び出し側 main() で最終的に捕捉する。

    since: 下限日（date < since は選定対象外）。None なら SELECT_SINCE 環境変数
    （不正なら DEFAULT_SINCE）を都度読み直す（テストが os.environ を差し替えた
    直後でも反映されるように、モジュール定数 SINCE をそのまま使わない）。
    """
    if since is None:
        since = _resolve_since()

    captures_records, synth_count = load_captures(captures_path)
    reviewed, reviewed_urls_set, unreadable = reviewed_index(reviews_dir)
    idx_only = index_only_rids(captures_records, capture_index_path)

    selected = []
    by_date = {}
    past = 0
    before_since = 0
    reviewed_by_url = 0
    for rid, date, rec in captures_records:
        if date < since:
            before_since += 1
            continue
        if rid in reviewed:
            continue
        key = url_key(rec.get("source"))
        if key is not None and key in reviewed_urls_set:
            reviewed_by_url += 1
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
        "_synthetic_rid_count": synth_count,
        "_index_only_count": idx_only,
        "_unreadable_reviews": unreadable,
        "_before_since_count": before_since,
        "_reviewed_by_url_count": reviewed_by_url,
        "_since": since,
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
              "captures=%d index_only=%d unreadable_reviews=%d before_since=%d "
              "reviewed_by_url=%d synthetic_rid=%d%s"
              % (target, result["_selected_count"], result["_past_count"],
                 result["_reviewed_count"], result["_captures_count"],
                 result["_index_only_count"], len(result["_unreadable_reviews"]),
                 result["_before_since_count"], result["_reviewed_by_url_count"],
                 result["_synthetic_rid_count"], warn))
        return 0
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print("SELECT_STATUS: target=%s selected=0 past=0 reviewed_rids=0 "
              "captures=0 index_only=0 unreadable_reviews=0 before_since=0 "
              "reviewed_by_url=0 synthetic_rid=0 error=%s"
              % (target, type(e).__name__))
        return 0


if __name__ == "__main__":
    rc = 0
    try:
        rc = main(sys.argv[1:])
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print("SELECT_STATUS: target=unknown selected=0 past=0 reviewed_rids=0 "
              "captures=0 index_only=0 unreadable_reviews=0 before_since=0 "
              "reviewed_by_url=0 synthetic_rid=0 error=%s" % type(e).__name__)
        rc = 0
    sys.stdout.flush()
    sys.exit(rc if isinstance(rc, int) else 0)
