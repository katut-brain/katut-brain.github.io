#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""動画・視覚コンテンツ未取得の開示を機械判定するチェッカー。

無人LLM（クラウドRoutine）の指示遵守だけに頼ると、手順書に文言指示があっても
実際に書き漏らす（2026-09-04 Reel 3件・2026-09-14 Reel 1件・2026-09-08
Threads 3件など、見たかのような記述や開示ゼロが実例として発生した）。
このスクリプトは `reviews/<日付>.html` の各カードを機械的に読み、
`fetch_facts/<日付>.json` の欠損記録と突き合わせて、開示すべきなのに
していないカードを検出する。判定語リストはこのファイル先頭の定数
（MEDIA_WORDS / UNGOTTEN_WORDS）にのみ置き、他所へ複製しない。

判定対象（duty）は3種類のみ:
  - x_video   : route=x かつ missing に video_content
  - reel      : route=instagram かつ URLパスが /reel/ かつ missing に video_content
  - threads   : route=threads かつ missing に visual_content または audio_content
それ以外で missing に video_content/visual_content/audio_content を含むもの
（Instagram /p/ の画像欠損など）は違反対象にせず out_of_scope として件数のみ数える。

終了コードは常に0（無人Routineの gate を止めないため）。入力ファイルの欠如や
例外は DISCLOSURE_ERROR: / status=no_facts / status=no_review 等の行で示す。
"""

import argparse
import datetime
import glob
import html as html_module
import json
import os
import re
import sys
from urllib.parse import urlsplit

# --- 判定語（正本はここ一箇所） -------------------------------------------

MEDIA_WORDS = r"動画|映像|画像|音声|視覚|写真|リール|Reel"
UNGOTTEN_WORDS = (
    r"未取得|取得できていない|取得できな|取れていない|取れなかった|"
    r"見られていない|見ていない|視聴していない|未確認|確認できていない|"
    r"追えていない|未視聴"
)
MEDIA_RE = re.compile(MEDIA_WORDS)
UNGOTTEN_RE = re.compile(UNGOTTEN_WORDS)

# 種別ごとの --fix 定型文
FIX_PHRASES = {
    "x_video": "今回は動画の内容を取得できなかった。",
    "reel": "動画の内容は未取得。",
    "threads": "画像・動画・音声の内容は未取得。",
}

SENTENCE_SPLIT_RE = re.compile(r"[。\n]")
TAG_RE = re.compile(r"<[^>]+>")
DATE_RE = re.compile(r"^(\d{4}-\d\d-\d\d)$")


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
    """(kind, ) を返す。kind は x_video/reel/threads/out_of_scope/None のいずれか。"""
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


# --- reviews 読み込みとカード抽出 ------------------------------------------

def extract_vcards(review_html):
    """.vcard ブロックを div の開閉深さで数え上げて抽出する。
    返り値は [{"start": int, "end": int, "raw": str}, ...]。
    テンプレート内の入れ子 div（thumb/vbody/vtitle/vdesc）に対応するため、
    固定インデント前提の正規表現ではなく深さカウントで切り出す。
    """
    marker = '<div class="vcard">'
    cards = []
    idx = 0
    n = len(review_html)
    while True:
        start = review_html.find(marker, idx)
        if start == -1:
            break
        pos = start + len(marker)
        depth = 1
        while depth > 0:
            next_open = review_html.find("<div", pos)
            next_close = review_html.find("</div>", pos)
            if next_close == -1:
                # 壊れたHTML。残り全部をこのカードとして扱う。
                pos = n
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
    return cards


def card_rid(card_raw):
    m = re.search(r'data-rid="(\d+)"', card_raw)
    return m.group(1) if m else None


def card_href(card_raw):
    m = re.search(r'<a class="vlink" href="([^"]*)"', card_raw)
    if not m:
        return None
    return html_module.unescape(m.group(1))


def card_text(card_raw):
    """タグ除去・HTMLエンティティ解決後のカード内可視テキスト。"""
    stripped = TAG_RE.sub(" ", card_raw)
    return html_module.unescape(stripped)


def card_disclosed(card_raw):
    text = card_text(card_raw)
    for sentence in SENTENCE_SPLIT_RE.split(text):
        if MEDIA_RE.search(sentence) and UNGOTTEN_RE.search(sentence):
            return True
    return False


def load_review_cards(review_path):
    with open(review_path, encoding="utf-8") as fh:
        raw = fh.read()
    cards = extract_vcards(raw)
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
    return raw, cards, by_rid, by_url


# --- 全 reviews にまたがる rid/url インデックス（バックフィル除外用） ------

def build_global_index(reviews_dir):
    """{rid: date} と {url_norm: date} を全 reviews/*.html から作る。
    同じ rid/url が複数日にまたがる場合は最初に見つかった日付を採用する
    （バックフィルの「他の日のカードが存在する」ことが分かればよく、
    どの日が正かの優先順位は問わない）。
    """
    rid_to_date = {}
    url_to_date = {}
    if not os.path.isdir(reviews_dir):
        return rid_to_date, url_to_date
    for path in sorted(glob.glob(os.path.join(reviews_dir, "*.html"))):
        stem = os.path.basename(path)[:-5]
        if not DATE_RE.match(stem):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            continue
        for c in extract_vcards(raw):
            rid = card_rid(c["raw"])
            href = card_href(c["raw"])
            if rid and rid not in rid_to_date:
                rid_to_date[rid] = stem
            if href:
                u = normalize_url(href)
                if u and u not in url_to_date:
                    url_to_date[u] = stem
    return rid_to_date, url_to_date


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
        self.violations = []       # [{kind, rid, url}]
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


def process_date(date, facts_dir, reviews_dir, rid_to_date, url_to_date,
                  fix=False):
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

    review_path = os.path.join(reviews_dir, "%s.html" % date)
    if not os.path.isfile(review_path):
        result.status = "no_review"
        return result

    raw, cards, by_rid, by_url = load_review_cards(review_path)
    edits = []  # [(close_pos, rid_str, kind, card, content_start)]

    for url, rec in facts.items():
        kind = classify_record(url, rec)
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
            # このカードは今日の reviews に無い。バックフィルかどうか確認する。
            other_date = None
            if rid_str and rid_str in rid_to_date and rid_to_date[rid_str] != date:
                other_date = rid_to_date[rid_str]
            else:
                u = normalize_url(url)
                if u in url_to_date and url_to_date[u] != date:
                    other_date = url_to_date[u]
            result.excluded += 1
            if other_date:
                reason = "card_in_other_date(%s)" % other_date
            else:
                reason = "no_card"
            result.excluded_records.append(
                {"rid": rid_str, "url": url, "reason": reason}
            )
            continue

        # 候補カードのうち1枚でも開示なしなら違反（全部開示していて初めて合格）。
        undisclosed = [c for c in candidates if not card_disclosed(c["raw"])]
        if not undisclosed:
            result.disclosed += 1
            continue

        if fix:
            record_edits = []
            for c in undisclosed:
                vp = _vdesc_insert_point(raw, c)
                if vp is not None:
                    content_start, close_pos = vp
                    record_edits.append((close_pos, rid_str, kind, c, content_start))
            if record_edits:
                edits.extend(record_edits)
                result.disclosed += 1
                result.fixed.append(rid_str)
                continue
            # 挿入位置が見つからなかった場合のみ違反として記録する。

        result.violation += 1
        result.violations.append({"kind": kind, "rid": rid_str, "url": url})

    if fix and edits:
        raw = _apply_fixes(raw, edits)
        with open(review_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(raw)

    return result


def _vdesc_insert_point(raw, card):
    """card（raw文字列全体内の絶対start/endを持つ）の .vdesc 中身の開始位置と
    閉じタグ直前の絶対オフセットを (content_start, close_pos) で返す。
    見つからなければ None。
    """
    marker = '<div class="vdesc">'
    rel = card["raw"].find(marker)
    if rel == -1:
        return None
    abs_open_end = card["start"] + rel + len(marker)
    close = raw.find("</div>", abs_open_end)
    if close == -1:
        return None
    return abs_open_end, close


def _apply_fixes(raw, edits):
    # 同じファイル内で複数カードを直す場合、後ろの挿入から先に適用して
    # 前方のオフセットがずれないようにする。
    edits_sorted = sorted(edits, key=lambda e: e[0], reverse=True)
    for close_pos, rid_str, kind, card, content_start in edits_sorted:
        phrase = FIX_PHRASES[kind]
        # 句点等で終わっているかは vdesc の開始位置からの中身で判定する
        # （インラインタグが挟まっていても末尾テキストの位置がずれないように）。
        desc_tail = raw[content_start:close_pos]
        stripped_tail = desc_tail.rstrip()
        needs_period = not stripped_tail.endswith(("。", "！", "？", "」", "』"))
        insertion = ("。" if needs_period and stripped_tail else "") + phrase
        raw = raw[:close_pos] + insertion + raw[close_pos:]
    return raw


# --- 出力 --------------------------------------------------------------

def print_check_line(result):
    if result.status == "no_facts":
        print("DISCLOSURE_CHECK: date=%s status=no_facts" % result.date)
        return
    if result.status == "no_review":
        print("DISCLOSURE_CHECK: date=%s status=no_review" % result.date)
        return
    if result.status == "error":
        print("DISCLOSURE_ERROR: date=%s %s" % (result.date, result.error))
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
        line = "DISCLOSURE_VIOLATION: date=%s kind=%s rid=%s url=%s" % (
            result.date, v["kind"], v["rid"], v["url"],
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
        "| date | duty | disclosed | violation | excluded | out_of_scope |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.status != "ok":
            lines.append("| %s | - | - | - | - | status=%s |" % (r.date, r.status))
            continue
        lines.append(
            "| %s | %d | %d | %d | %d | %d |"
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

    rid_to_date, url_to_date = build_global_index(reviews_dir)

    since = args.since
    results = []
    for date in sorted(dates):
        try:
            r = process_date(
                date, facts_dir, reviews_dir, rid_to_date, url_to_date,
                fix=args.fix,
            )
        except Exception as exc:  # noqa: BLE001
            r = DateResult(date)
            r.status = "error"
            r.error = "%s: %s" % (type(exc).__name__, exc)
        results.append(r)

    for r in results:
        print_check_line(r)
        enforce = (since is None) or (r.date >= since)
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
