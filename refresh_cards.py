#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公開済みカードの「※動画の内容は未取得」を、取り直しに成功した動画理解の内容で書き直す。

背景: 2026-10-06〜10-13 の gemini_unavailable は backfill（手順2.5）で取り直されるが、
select_targets は掲載済み rid を選び直さず、merge_review は追記だけ、check_disclosure --fix は
マーカーを足すだけなので、取れても公開済みカードが書き換わらない。この穴を塞ぐ。

検出（既定・読み取り専用）: ledger の rid グループの最新レコードが video_understood=true・
missing に video_content なし・video_summary あり、かつ reviews の data-rid 一致カードの .vdesc に
未取得マーカーが残っている rid（check_disclosure.recovered_rids を共用）。
書き直し（--apply）: そのカードの .vdesc の中身だけを置換する。「今回は…取得できなかった。」の文と
マーカーを消し、「動画の内容：<要約>」を末尾に入れる。要約は外部由来なので HTML エスケープし、
長さは fetch_content.VIDEO_SUMMARY_MAX で切る。カード外のバイトは1つも変えない（newline="" で読み書き）。
書き直せる形のカードだけを先に選び、その rid を --limit 件（既定 5・rid 単位）まで処理する。
書き直せない形は skipped として数え、上限を食わせない。
書き直したファイルの一覧は --files-out（既定 /tmp/card_refresh_files.txt）に書く（手順8が読む）。
LLM は通さない。終了コードは常に0。
"""
import argparse
import glob
import html
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_disclosure as cd  # noqa: E402
import fetch_content  # noqa: E402

MARKER = cd.MARKERS["video_content"]
DEFAULT_LIMIT = 5
DEFAULT_FILES_OUT = "/tmp/card_refresh_files.txt"


def new_inner(inner, summary):
    """.vdesc の中身を書き直した文字列。書き直せない形なら None。"""
    if "<" in inner:
        return None
    idx = inner.find(MARKER)
    if idx < 0 or inner[idx + len(MARKER):].strip("。 \t\r\n") != "":
        return None
    head = inner[:idx].rstrip()
    core = head[:-1] if head.endswith("。") else head
    cut = core.rfind("。")
    sentence = core[cut + 1:]
    if not (sentence.startswith("今回は") and "できなかった" in sentence):
        return None
    base = core[:cut + 1] if cut >= 0 else ""
    s = fetch_content.clean_video_summary(summary)
    if not s:
        return None
    s = html.escape(s, quote=True)
    out = base + cd.REFRESHED_PREFIX + s + ("" if s.endswith(("。", "…")) else "。")
    return None if MARKER in out else out


def plan(reviews_dir, facts_dir):
    """({rid: summary}, [(path, rid, card, inner_span, new_inner or None)]) を返す。
    対象は未取得マーカーが残っている回収済み rid のカード全部。"""
    ok = {rid: r["video_summary"] for rid, r in cd.recovered_rids(facts_dir).items()
          if (r.get("video_summary") or "").strip()}
    items = []
    for path in sorted(glob.glob(os.path.join(reviews_dir, "*.html"))):
        try:
            _raw, cards = cd.load_review_cards(path)[:2]
        except Exception as e:
            print("CARD_REFRESH_WARN: %s: %s" % (os.path.basename(path), e))
            continue
        for c in cards:
            rid = c.get("rid")
            if rid not in ok or not cd._marker_disclosed(cd.vdesc_text(c["raw"]), MARKER):
                continue
            span = cd._vdesc_span(c["raw"])
            inner = c["raw"][span[0]:span[1]] if span else None
            new = new_inner(inner, ok[rid]) if inner is not None else None
            items.append((path, rid, c["start"], span, new))
    return items


def rewrite_file(path, edits):
    """edits=[(card_start, span, new_inner)] を path に適用する。書いたカード数を返す。"""
    raw, cards, _a, _b, malformed = cd.load_review_cards(path)
    if malformed:
        return 0
    pos_edits = sorted((cs + sp[0], cs + sp[1], new) for cs, sp, new in edits)
    out, pos = [], 0
    for a, b, new in pos_edits:
        out.append(raw[pos:a])
        out.append(new)
        pos = b
    out.append(raw[pos:])
    text = "".join(out)
    cards2, bad2 = cd.extract_vcards(text)
    if bad2 or len(cards2) != len(cards):
        return 0
    tmp = path + ".refresh.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return len(pos_edits)


def _write_files_out(path, files):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(f + "\n" for f in files))
    except OSError as e:
        print("CARD_REFRESH_WARN: files-out を書けない: %s" % e)


def _truncate(path):
    """前の夜の一覧が残って誤って送られないよう、最初に空にする（書けなくても止めない）。"""
    try:
        with open(path, "w", encoding="utf-8"):
            pass
    except OSError:
        pass


def main(argv=None):
    _truncate(DEFAULT_FILES_OUT)
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="書き直す（既定は検出のみ）")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="rid 単位の上限")
    p.add_argument("--reviews-dir", default="reviews")
    p.add_argument("--facts-dir", default=cd.ledger.FACTS_DIR)
    p.add_argument("--files-out", default=DEFAULT_FILES_OUT)
    try:
        a = p.parse_args(argv)
    except SystemExit:
        print("CARD_REFRESH_ERROR: 引数が不正")
        return 0
    _truncate(a.files_out)
    limit = max(a.limit, 0)
    candidates = skipped = rewritten_rids = cards_written = errors = 0
    files = []
    try:
        items = plan(a.reviews_dir, a.facts_dir)
        rids = []
        for _p, rid, _c, _s, _n in items:
            if rid not in rids:
                rids.append(rid)
        can = [r for r in rids if any(i[1] == r and i[4] is not None for i in items)]
        candidates, skipped = len(rids), len(rids) - len(can)
        chosen = can[:limit]
        if a.apply:
            by_file = {}
            for path, rid, cs, span, new in items:
                if rid in chosen and new is not None:
                    by_file.setdefault(path, []).append((cs, span, new, rid))
            done_rids = set()
            for path, edits in sorted(by_file.items()):
                try:
                    n = rewrite_file(path, [e[:3] for e in edits])
                except Exception as e:
                    errors += 1
                    print("CARD_REFRESH_WARN: %s: %s: %s"
                          % (os.path.basename(path), type(e).__name__, e))
                    continue
                if n:
                    cards_written += n
                    done_rids.update(e[3] for e in edits)
                    files.append(path.replace(os.sep, "/"))
            rewritten_rids = len(done_rids)
            chosen = [r for r in chosen if r in done_rids]
    except Exception as e:
        errors += 1
        chosen = []
        print("CARD_REFRESH_WARN: %s: %s" % (type(e).__name__, e))
    if a.apply:
        _write_files_out(a.files_out, files)
    print("CARD_REFRESH: mode=%s candidates=%d skipped=%d limit=%d rewritten=%d cards=%d "
          "errors=%d rids=%s files=%s"
          % ("apply" if a.apply else "detect", candidates, skipped, limit, rewritten_rids,
             cards_written, errors, ",".join(chosen) or "-", ",".join(files) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
