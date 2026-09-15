#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""動画・視覚コンテンツ未取得の開示を機械判定するチェッカー。

無人LLM（クラウドRoutine）の指示遵守だけに頼ると、手順書に文言指示があっても
実際に書き漏らす（2026-09-04 Reel 3件・2026-09-14 Reel 1件・2026-09-08
Threads 3件など、見たかのような記述や開示ゼロが実例として発生した）。
このスクリプトは `reviews/<日付>.html` の各カードを機械的に読み、
`fetch_facts/<日付>.json` の欠損記録と突き合わせて、開示すべきなのに
していないカードを検出する。判定語（マーカー文）はこのファイル先頭の定数
（MARKERS）にのみ置き、他所へ複製しない。

開示判定（2026-09-15 3周目差し戻しで固定マーカー方式に作り替え）は、自由文を
読んで判定するのをやめ、欠損キーごとに決まった1つのマーカー文
（「※動画の内容は未取得」等）の**有無**だけで判定する。マーカー文字列の
直後が「文末（テキスト終端）」「。」「空白」「改行」のいずれかであれば
開示ありとみなし、それ以外（マーカーが無い・マーカーの直後に文字が続く
＝「※動画の内容は未取得ではない」等）は開示なしとする。自由文の内容を
解釈する処理（語彙の近接判定・否定の先読み等）は一切行わない。3周にわたる
外部レビューで自由文判定に新しい誤合格反例（全角コロン・否定・Threadsでの
語の代行・別媒体のマーカーの流用など）が出続けたため、いたちごっこを
終わらせる目的でこの方式に切り替えた（ユーザー裁定 2026-09-15）。

判定対象（duty）は3種類のみ:
  - x_video   : route=x かつ missing に video_content
  - reel      : route=instagram かつ URLパスが /reel/ かつ missing に video_content
  - threads   : route=threads かつ missing に visual_content または audio_content
それ以外で missing に video_content/visual_content/audio_content を含むもの
（Instagram /p/ の画像欠損など）は違反対象にせず out_of_scope として件数のみ数える。

対象日の reviews に対応カードが無い場合、capture_index.json（手順1.5の台帳。
`{"days": {"YYYY-MM-DD": {"rids": [int,...]}}}`）を見て、その rid が当日の
台帳に無く別日の台帳にあるときだけ「バックフィル（再取得）」として除外する。
それ以外（rid が無い・当日の台帳にある・台帳のどこにも無い・台帳自体が
読めない）は正直に違反として扱う（reason=no_card を付けて通常の
DISCLOSURE_VIOLATION として出す。挿入先のカードが無いので --fix の対象には
しない）。

終了コードは常に0（無人Routineの gate を止めないため）。入力ファイルの欠如や
例外は DISCLOSURE_ERROR: / status=no_facts / status=no_review 等の行で示す。

この仕組みは手順7の実行自体を強制しない。公開ゲート（build-feed.yml）側は
検知・通知（::warning・GITHUB_STEP_SUMMARY）までで、違反があっても公開は
止めない（ユーザー裁定 2026-09-15：警告のみ）。
"""

import argparse
import glob
import html as html_module
import json
import os
import re
import sys
import tempfile
from urllib.parse import urlsplit

# --- 判定語（正本はここ一箇所） -------------------------------------------

# missing の欠損キーごとの固定マーカー文。自由文ではなく、この文字列の
# 有無だけで開示を判定する。threads は visual_content/audio_content
# それぞれに専用のマーカーを持ち、欠損キーごとに個別の開示が要る
# （「※画像の内容は未取得」だけでは audio_content の開示にはならない）。
MARKERS = {
    "video_content": "※動画の内容は未取得",
    "visual_content": "※画像の内容は未取得",
    "audio_content": "※音声の内容は未取得",
}

# マーカー文字列の直後がこれらのいずれかであれば「開示あり」とみなす境界文字
# （文末は別途 index が末尾に達したかで判定する）。
_BOUNDARY_PUNCT = "。"


def _marker_disclosed(text, marker):
    """text 内に marker が現れ、かつその直後が文末／「。」／空白／改行の
    いずれかである箇所が1つでもあれば True。直後に他の文字が続く場合
    （「※動画の内容は未取得ではない」等）はその出現を合格させず、
    後続に同じマーカーが別途あれば改めて探す。
    """
    start = 0
    n = len(text)
    while True:
        idx = text.find(marker, start)
        if idx == -1:
            return False
        after = idx + len(marker)
        if after >= n:
            return True
        ch = text[after]
        if ch == _BOUNDARY_PUNCT or ch.isspace():
            return True
        start = idx + 1


# 種別ごとの --fix 定型文（欠損キー全部が missing だった場合の参考値。
# 実際の --fix 挿入は process_date 側で missing の実キーだけを個別に
# MARKERS から組み立てる。テスト等の参照用にここにも用意する）。
FIX_PHRASES = {
    "x_video": MARKERS["video_content"] + "。",
    "reel": MARKERS["video_content"] + "。",
    "threads": MARKERS["visual_content"] + "。" + MARKERS["audio_content"] + "。",
}

TAG_RE = re.compile(r"<[^>]+>")
DATE_RE = re.compile(r"^(\d{4}-\d\d-\d\d)$")
DIV_OPEN_RE = re.compile(r"<div\b[^>]*>")
ANCHOR_OPEN_RE = re.compile(r"<a\b[^>]*>")
ATTR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)
RID_ATTR_RE = re.compile(r"""data-rid\s*=\s*["'](\d+)["']""")


# --- 属性・classトークンの揺れに強いHTMLパーツ抽出 --------------------------

def _get_attr(tag, name):
    """<div class="a b" href='...'> のようなタグ文字列から属性値を取る。
    属性の並び順・引用符の種類（"/'）の揺れに対応する。
    """
    for m in ATTR_RE.finditer(tag):
        attr_name = m.group(1)
        if attr_name.lower() != name:
            continue
        return m.group(2) if m.group(2) is not None else m.group(3)
    return None


def _has_class_token(class_value, token):
    if not class_value:
        return False
    return token in class_value.split()


# --- URL 正規化 --------------------------------------------------------

def normalize_url(url):
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = parts.path
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    return "%s://%s%s" % (scheme, netloc, path)


# --- facts 読み込みと duty 判定 --------------------------------------------

def classify_record(url, rec):
    """kind を返す。x_video/reel/threads/out_of_scope/None のいずれか。"""
    route = rec.get("route")
    missing = rec.get("missing") or []
    if route == "x" and "video_content" in missing:
        return "x_video"
    if route == "instagram":
        path = urlsplit(url).path
        if "/reel/" in path and "video_content" in missing:
            return "reel"
    if route == "threads" and (
        "visual_content" in missing or "audio_content" in missing
    ):
        return "threads"
    if any(m in missing for m in ("video_content", "visual_content", "audio_content")):
        return "out_of_scope"
    return None


def load_facts(facts_path):
    with open(facts_path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("facts はdictでなければならない: %s" % facts_path)
    return data


# --- capture_index.json（当日の保存台帳。バックフィル判定に使う） ----------

def load_capture_index(path):
    """{"YYYY-MM-DD": {int rid, ...}} を返す。読めない/壊れていれば None
    （None のときは呼び出し側がバックフィル除外を一切行わない＝安全側）。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        days = data["days"]
        if not isinstance(days, dict):
            raise ValueError("days は dict でなければならない")
        result = {}
        for day, v in days.items():
            if not DATE_RE.match(day):
                raise ValueError("不正な日付キー: %s" % day)
            rids = v.get("rids")
            if not isinstance(rids, list):
                raise ValueError("day=%s の rids はリストでなければならない" % day)
            result[day] = set(int(r) for r in rids)
        return result
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def build_rid_origin_index(capture_index):
    """{rid(int): 最初に見つかった保存日} を capture_index 全体から作る。"""
    origin = {}
    if not capture_index:
        return origin
    for day in sorted(capture_index.keys()):
        for rid in capture_index[day]:
            if rid not in origin:
                origin[rid] = day
    return origin


# --- reviews 読み込みとカード抽出 ------------------------------------------

COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _blank_comments(html_str):
    """`<!-- ... -->` の中身をオフセット長を保ったまま無害化したコピーを
    返す（改行はそのまま残し、他は空白に置換）。div の開閉カウントなど
    「構造」を数えるときだけこれを使い、実際に取り出す文字列
    （card_raw・vdesc の中身など）は元の html_str から切り出す。
    """
    def repl(m):
        s = m.group(0)
        return "".join(ch if ch == "\n" else " " for ch in s)
    return COMMENT_RE.sub(repl, html_str)


def _find_next_div_open(html_str, pos):
    """`<div` の直後が空白・`>`・`/` のときだけ開きタグとみなして探す
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
    """class トークンに "vcard" を含む <div> ブロックを、開閉の深さを数えて
    切り出す。属性順・追加クラス・引用符の揺れに対応する（正規表現の
    完全一致 `<div class="vcard">` には依存しない）。HTMLコメントの中に
    書かれた `<div>` 等は構造カウントから除外する。
    返り値は (cards, malformed) のタプル。
    cards は [{"start", "end", "raw"}, ...]。malformed は、閉じタグが
    足りずに最後まで閉じられなかったカードが1件でもあれば True。
    """
    cards = []
    malformed = False
    idx = 0
    n = len(review_html)
    structural = _blank_comments(review_html)
    for m in DIV_OPEN_RE.finditer(structural):
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
    m = RID_ATTR_RE.search(card_raw)
    return m.group(1) if m else None


def card_href(card_raw):
    """class トークンに "vlink" を含む最初の <a> タグの href を返す。"""
    for m in ANCHOR_OPEN_RE.finditer(card_raw):
        tag = m.group(0)
        cls = _get_attr(tag, "class")
        if _has_class_token(cls, "vlink"):
            href = _get_attr(tag, "href")
            return html_module.unescape(href) if href is not None else None
    return None


def _vdesc_span(card_raw, start=0):
    """card_raw 内の class トークン "vdesc" を持つ最初の <div> の中身の
    (content_start, close) を card_raw 相対オフセットで返す。無ければ None。
    テンプレート上 vdesc の中に別の div は入らない想定だが、万一入れ子に
    なっていても深さカウントで正しい閉じタグを見つける。HTMLコメントの
    中の `<div>` は構造カウントから除外する。
    """
    structural = _blank_comments(card_raw)
    m = None
    for cand in DIV_OPEN_RE.finditer(structural, start):
        tag = cand.group(0)
        cls = _get_attr(tag, "class")
        if _has_class_token(cls, "vdesc"):
            m = cand
            break
    if m is None:
        return None
    pos = m.end()
    depth = 1
    while depth > 0:
        next_open = _find_next_div_open(structural, pos)
        next_close = structural.find("</div>", pos)
        if next_close == -1:
            return None
        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open + 4
        else:
            depth -= 1
            pos = next_close + len("</div>")
    close = pos - len("</div>")
    return m.end(), close


def vdesc_text(card_raw):
    """.vdesc の中身だけを、タグ除去・HTMLエンティティ解決した状態で返す
    （vtitle・ボタンのdata-title等は見ない）。無ければ空文字列。
    """
    span = _vdesc_span(card_raw)
    if span is None:
        return ""
    content_start, close = span
    inner = card_raw[content_start:close]
    stripped = TAG_RE.sub(" ", inner)
    return html_module.unescape(stripped)


def required_missing_keys(kind, missing):
    """kind と missing から、開示が要る欠損キーのリストを返す。
    x_video/reel は video_content 固定1つ。threads は missing に実際に
    入っているキーだけ（visual_content と audio_content は独立に開示が要る）。
    """
    missing = missing or []
    if kind in ("x_video", "reel"):
        return ["video_content"]
    if kind == "threads":
        keys = []
        if "visual_content" in missing:
            keys.append("visual_content")
        if "audio_content" in missing:
            keys.append("audio_content")
        return keys
    return []


def missing_marker_keys(text, kind, missing):
    """text の中に、kind/missing から要る各欠損キーのマーカーが
    （境界条件を満たす形で）まだ無いものだけをキーのリストで返す
    （空リスト＝全部開示済み）。required_missing_keys が返す順序を保つ。
    """
    keys = required_missing_keys(kind, missing)
    return [k for k in keys if not _marker_disclosed(text, MARKERS[k])]


def text_disclosed(text, kind, missing):
    """.vdesc相当のプレーンテキストを直接判定するコア関数（テストからも
    HTMLを組み立てずに直接呼べるようにここを正本にする）。要る欠損キーの
    マーカーが全部そろっていれば True（要るキーが無ければ False）。
    """
    keys = required_missing_keys(kind, missing)
    if not keys:
        return False
    return not missing_marker_keys(text, kind, missing)


def card_disclosed(card_raw, kind, missing):
    return text_disclosed(vdesc_text(card_raw), kind, missing)


def load_review_cards(review_path):
    # newline="" で開き、改行コード（CRLF等）を変換せずそのまま保持する。
    # --fix はここで読んだ raw に挿入するだけなので、変換してしまうと
    # 挿入位置以外のバイトまで書き換わってしまう。
    with open(review_path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    cards, malformed = extract_vcards(raw)
    by_rid = {}
    by_url = {}
    for c in cards:
        rid = card_rid(c["raw"])
        href = card_href(c["raw"])
        c["rid"] = rid
        c["url_norm"] = normalize_url(href) if href else None
        if rid:
            by_rid.setdefault(rid, []).append(c)
        if c["url_norm"]:
            by_url.setdefault(c["url_norm"], []).append(c)
    return raw, cards, by_rid, by_url, malformed


# --- 全 reviews にまたがる rid/url インデックス（補助情報のみに使う） ------

def build_global_index(reviews_dir):
    """{rid: {date, ...}} と {url_norm: {date, ...}} を全 reviews/*.html から
    作る。バックフィル判定そのものには使わない（それは capture_index.json の
    役目）。ここは除外理由に添える補助情報「バックフィル元の日に実際カードが
    あるか（card=yes/no）」を出すためだけに使う。
    """
    rid_to_dates = {}
    url_to_dates = {}
    if not os.path.isdir(reviews_dir):
        return rid_to_dates, url_to_dates
    for path in sorted(glob.glob(os.path.join(reviews_dir, "*.html"))):
        stem = os.path.basename(path)[:-5]
        if not DATE_RE.match(stem):
            continue
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                raw = fh.read()
        except OSError:
            continue
        cards, _malformed = extract_vcards(raw)
        for c in cards:
            rid = card_rid(c["raw"])
            href = card_href(c["raw"])
            if rid:
                rid_to_dates.setdefault(rid, set()).add(stem)
            if href:
                u = normalize_url(href)
                if u:
                    url_to_dates.setdefault(u, set()).add(stem)
    return rid_to_dates, url_to_dates


# --- 1日分の処理 ------------------------------------------------------------

class DateResult(object):
    def __init__(self, date):
        self.date = date
        self.status = "ok"
        self.error = None
        self.duty = 0
        self.disclosed = 0
        self.violation = 0
        self.excluded = 0
        self.out_of_scope = 0
        self.violations = []       # [{kind, rid, url, reason}]
        self.excluded_records = [] # [{rid, url, reason}]
        self.fixed = []            # [rid, ...]

    def to_json(self):
        return {
            "date": self.date,
            "status": self.status,
            "error": self.error,
            "duty": self.duty,
            "disclosed": self.disclosed,
            "violation": self.violation,
            "excluded": self.excluded,
            "out_of_scope": self.out_of_scope,
            "violations": self.violations,
            "excluded_records": self.excluded_records,
            "fixed": self.fixed,
        }


def _write_review_atomic(review_path, content):
    """同じディレクトリに一時ファイルを作ってから os.replace する。
    無人Routineは1体が逐次実行する前提のためロックは入れない。
    """
    d = os.path.dirname(review_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".check_disclosure_", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp_path, review_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def process_date(date, facts_dir, reviews_dir, capture_index, rid_origin,
                  aux_rid_dates, aux_url_dates, fix=False):
    result = DateResult(date)
    facts_path = os.path.join(facts_dir, "%s.json" % date)
    if not os.path.isfile(facts_path):
        result.status = "no_facts"
        return result

    try:
        facts = load_facts(facts_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result.status = "error"
        result.error = str(exc)
        return result

    duty_records = [
        (url, rec, classify_record(url, rec)) for url, rec in facts.items()
    ]
    duty_count = sum(
        1 for _u, _r, k in duty_records if k not in (None, "out_of_scope")
    )

    review_path = os.path.join(reviews_dir, "%s.html" % date)
    if not os.path.isfile(review_path):
        result.status = "no_review"
        result.duty = duty_count
        return result

    raw, cards, by_rid, by_url, malformed = load_review_cards(review_path)

    # カード抽出そのものが失敗している疑いがあるときは、個々のレコードを
    # 誤って「違反」や「除外」と判定してしまう前に別枠のエラーとして止める
    # （除外経路と混ぜない）。
    if duty_count > 0 and (not cards or malformed):
        result.status = "card_parse_failed"
        return result

    edits = []  # [(close_pos, rid_str, keys_to_add, card, content_start)]

    for url, rec, kind in duty_records:
        if kind is None:
            continue
        if kind == "out_of_scope":
            result.out_of_scope += 1
            continue

        result.duty += 1
        rid = rec.get("raindrop_id")
        rid_str = str(rid) if rid is not None else None

        # 同じ rid/URL に対応するカードが対象日の reviews に複数あることが
        # あり得るので、候補は1枚に絞らず全部見る。
        candidates = []
        if rid_str and rid_str in by_rid:
            candidates = by_rid[rid_str]
        if not candidates:
            u = normalize_url(url)
            if u in by_url:
                candidates = by_url[u]

        if not candidates:
            reason, is_backfill = _classify_missing_card(
                date, rid_str, capture_index, rid_origin,
                aux_rid_dates, aux_url_dates,
            )
            if is_backfill:
                result.excluded += 1
                result.excluded_records.append(
                    {"rid": rid_str, "url": url, "reason": reason}
                )
            else:
                result.violation += 1
                result.violations.append(
                    {"kind": kind, "rid": rid_str, "url": url, "reason": reason}
                )
            continue

        # 候補カードのうち1枚でも開示なしなら違反（全部開示していて初めて合格）。
        missing = rec.get("missing")
        undisclosed = [
            c for c in candidates if not card_disclosed(c["raw"], kind, missing)
        ]
        if not undisclosed:
            result.disclosed += 1
            continue

        if fix:
            record_edits = []
            for c in undisclosed:
                vp = _vdesc_insert_point(raw, c)
                if vp is not None:
                    content_start, close_pos = vp
                    keys_to_add = missing_marker_keys(
                        vdesc_text(c["raw"]), kind, missing
                    )
                    record_edits.append(
                        (close_pos, rid_str, keys_to_add, c, content_start)
                    )
            if record_edits:
                edits.extend(record_edits)
                result.disclosed += 1
                result.fixed.append(rid_str)
                continue
            # 挿入位置が見つからなかった場合のみ違反として記録する。

        result.violation += 1
        result.violations.append(
            {"kind": kind, "rid": rid_str, "url": url, "reason": "undisclosed"}
        )

    if fix and edits:
        raw = _apply_fixes(raw, edits)
        _write_review_atomic(review_path, raw)

    return result


def _classify_missing_card(date, rid_str, capture_index, rid_origin,
                            aux_rid_dates, aux_url_dates):
    """対象日にカードが見つからなかったレコードの扱いを決める。
    戻り値は (reason, is_backfill)。
    is_backfill=True のときだけ「除外」とし、それ以外は違反(reason=no_card)。
    """
    if capture_index is None or rid_str is None:
        return "no_card", False
    try:
        rid_int = int(rid_str)
    except ValueError:
        return "no_card", False

    if date not in capture_index:
        # 台帳ファイルはあるが当日分のキー自体が無い（その夜の手順1.5が
        # 走らなかった等）。バックフィルかどうか判定しようがないので、
        # 安全側で除外は一切許可せず違反にする。
        return "no_card", False

    day_rids = capture_index[date]
    if rid_int in day_rids:
        # 当日の台帳に載っているのにカードが無い＝正直に違反。
        return "no_card", False

    origin_date = rid_origin.get(rid_int)
    if not origin_date or origin_date == date:
        return "no_card", False

    card_dates = aux_rid_dates.get(rid_str, set())
    card_flag = "yes" if origin_date in card_dates else "no"
    return "backfill(%s,card=%s)" % (origin_date, card_flag), True


def _vdesc_insert_point(raw, card):
    """card（raw文字列全体内の絶対start/endを持つ）の .vdesc 中身の開始位置と
    閉じタグ直前の絶対オフセットを (content_start, close_pos) で返す。
    見つからなければ None。
    """
    span = _vdesc_span(card["raw"])
    if span is None:
        return None
    rel_content_start, rel_close = span
    return card["start"] + rel_content_start, card["start"] + rel_close


def _apply_fixes(raw, edits):
    # 同じファイル内で複数カードを直す場合、後ろの挿入から先に適用して
    # 前方のオフセットがずれないようにする。
    edits_sorted = sorted(edits, key=lambda e: e[0], reverse=True)
    for close_pos, rid_str, keys_to_add, card, content_start in edits_sorted:
        # 足りないマーカーだけを組み立てる（threads で片方だけ既にある
        # 場合は足りない方のマーカーだけを追記する）。
        phrase = "".join(MARKERS[k] + "。" for k in keys_to_add)
        # 句点等で終わっているかは vdesc の開始位置からの中身で判定する
        # （インラインタグが挟まっていても末尾テキストの位置がずれないように）。
        desc_tail = raw[content_start:close_pos]
        stripped_tail = desc_tail.rstrip()
        needs_period = not stripped_tail.endswith(("。", "！", "？", "」", "』"))
        insertion = ("。" if needs_period and stripped_tail else "") + phrase
        raw = raw[:close_pos] + insertion + raw[close_pos:]
    return raw


# --- 出力 --------------------------------------------------------------

def print_check_line(result, github=False, warn=True):
    """標準出力の DISCLOSURE_CHECK/ERROR 行は常に出す。::warning は
    warn=False（--since より前の日付）なら抑止する。
    """
    if result.status == "no_facts":
        print("DISCLOSURE_CHECK: date=%s status=no_facts" % result.date)
        return
    if result.status == "no_review":
        print("DISCLOSURE_CHECK: date=%s status=no_review" % result.date)
        if github and warn and result.duty > 0:
            print(
                "::warning title=disclosure::DISCLOSURE_CHECK: date=%s "
                "status=no_review duty=%d (reviews file missing)"
                % (result.date, result.duty)
            )
        return
    if result.status == "card_parse_failed":
        line = "DISCLOSURE_ERROR: date=%s card_parse_failed" % result.date
        print(line)
        if github and warn:
            print("::warning title=disclosure::%s" % line)
        return
    if result.status == "error":
        line = "DISCLOSURE_ERROR: date=%s %s" % (result.date, result.error)
        print(line)
        if github and warn:
            print("::warning title=disclosure::%s" % line)
        return
    print(
        "DISCLOSURE_CHECK: date=%s duty=%d disclosed=%d violation=%d "
        "excluded=%d out_of_scope=%d"
        % (
            result.date,
            result.duty,
            result.disclosed,
            result.violation,
            result.excluded,
            result.out_of_scope,
        )
    )


def print_detail_lines(result, github=False):
    for v in result.violations:
        line = "DISCLOSURE_VIOLATION: date=%s kind=%s rid=%s url=%s reason=%s" % (
            result.date, v["kind"], v["rid"], v["url"], v.get("reason", "undisclosed"),
        )
        print(line)
        if github:
            print("::warning title=disclosure::%s" % line)
    for e in result.excluded_records:
        print(
            "DISCLOSURE_EXCLUDED: date=%s rid=%s reason=%s"
            % (result.date, e["rid"], e["reason"])
        )
    for rid_str in result.fixed:
        print("DISCLOSURE_FIXED: date=%s rid=%s" % (result.date, rid_str))


def write_github_summary(results):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## disclosure check",
        "",
        "| date | status | duty | disclosed | violation | excluded | out_of_scope |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.status != "ok":
            lines.append("| %s | %s | - | - | - | - | - |" % (r.date, r.status))
            continue
        lines.append(
            "| %s | ok | %d | %d | %d | %d | %d |"
            % (r.date, r.duty, r.disclosed, r.violation, r.excluded, r.out_of_scope)
        )
    try:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


# --- CLI -----------------------------------------------------------------

def collect_dates(args, facts_dir):
    dates = []
    if args.date:
        dates.extend(args.date)
    if args.all:
        for path in sorted(glob.glob(os.path.join(facts_dir, "*.json"))):
            stem = os.path.basename(path)[:-5]
            if DATE_RE.match(stem):
                dates.append(stem)
    # 重複除去（順序維持）
    seen = set()
    ordered = []
    for d in dates:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


def build_arg_parser():
    p = argparse.ArgumentParser(description="開示義務チェッカー")
    p.add_argument("--date", action="append", help="対象日（複数可）")
    p.add_argument("--all", action="store_true", help="fetch_facts 全日を対象")
    p.add_argument("--since", help="この日付以降のみ違反として扱う（--all併用）")
    p.add_argument("--fix", action="store_true", help="違反カードに定型文を追記")
    p.add_argument("--github", action="store_true",
                    help="::warning 出力とGITHUB_STEP_SUMMARYへの追記")
    p.add_argument("--json", action="store_true", help="JSON出力")
    p.add_argument("--facts-dir", default="fetch_facts")
    p.add_argument("--reviews-dir", default="reviews")
    p.add_argument("--capture-index", default="capture_index.json",
                    help="バックフィル判定に使う当日保存の台帳")
    return p


def main(argv=None):
    try:
        return _main(argv)
    except SystemExit as exc:
        # argparse がエラー時に sys.exit(2) するのを含め、常に0で返す
        # （「終了コードは常に0」の仕様。ゲートを止めない）。
        print("DISCLOSURE_ERROR: argument parsing failed (exit=%s)" % exc.code)
        return 0
    except Exception as exc:  # noqa: BLE001 - gateを止めないため必ず0で返す
        print("DISCLOSURE_ERROR: %s: %s" % (type(exc).__name__, exc))
        return 0


def _main(argv):
    args = build_arg_parser().parse_args(argv)
    facts_dir = args.facts_dir
    reviews_dir = args.reviews_dir

    if args.fix and args.all:
        print(
            "DISCLOSURE_ERROR: --fix は --all と併用できない"
            "（過去の reviews を誤って書き換えないため。--date で対象日を"
            "明示してから --fix を使う）"
        )
        return 0
    if args.fix and not args.date:
        print("DISCLOSURE_ERROR: --fix には --date の指定が必要")
        return 0

    dates = collect_dates(args, facts_dir)
    if not dates:
        print("DISCLOSURE_CHECK: status=no_dates")
        return 0

    capture_index = load_capture_index(args.capture_index)
    rid_origin = build_rid_origin_index(capture_index)
    aux_rid_dates, aux_url_dates = build_global_index(reviews_dir)

    since = args.since
    results = []
    for date in sorted(dates):
        try:
            r = process_date(
                date, facts_dir, reviews_dir, capture_index, rid_origin,
                aux_rid_dates, aux_url_dates, fix=args.fix,
            )
        except Exception as exc:  # noqa: BLE001
            r = DateResult(date)
            r.status = "error"
            r.error = "%s: %s" % (type(exc).__name__, exc)
        results.append(r)

    for r in results:
        enforce = (since is None) or (r.date >= since)
        print_check_line(r, github=args.github, warn=enforce)
        if r.status == "ok" and enforce:
            print_detail_lines(r, github=args.github)
        elif r.status == "ok" and r.fixed:
            # --fix はsince以前でも適用結果自体は報告する
            for rid_str in r.fixed:
                print("DISCLOSURE_FIXED: date=%s rid=%s" % (r.date, rid_str))

    if args.github:
        write_github_summary(results)

    if args.json:
        print(json.dumps([r.to_json() for r in results], ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
