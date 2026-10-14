#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公開済みカードの「※動画の内容は未取得」を、取り直しに成功した動画理解の内容で書き直す。

背景: 2026-10-06〜10-13 の gemini_unavailable は backfill（手順2.5）で取り直されるが、
select_targets は掲載済み rid を選び直さず、merge_review は追記だけ、check_disclosure --fix は
マーカーを足すだけなので、取れても公開済みカードが書き換わらない。この穴を塞ぐ。

検出（既定・読み取り専用）: ledger の rid グループの最新レコードが video_understood=true・
missing に video_content なし・video_summary あり、かつ reviews の data-rid 一致カードの .vdesc に
未取得マーカーが残っている rid。
書き直し（--apply）: そのカードの .vdesc の中身だけを置換する。「今回は…取得できなかった。」の文と
マーカーを消し、「動画の内容：<要約冒頭>」を末尾に入れる。要約は外部由来なので HTML エスケープし、
長さを SUMMARY_MAX で切る。カード外のバイトは1つも変えない（newline="" で読み書き）。
LLM は通さない。1回の上限は --limit（既定 5）。終了コードは常に0。
"""
import argparse
import glob
import html
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_disclosure as cd  # noqa: E402
import ledger  # noqa: E402

MARKER = cd.MARKERS["video_content"]
SUMMARY_MAX = 100
DEFAULT_LIMIT = 5


def clean_summary(text):
    """Gemini の要約から、カードに入れる短い1〜2文を作る（エスケープ前）。"""
    s = re.sub(r"[*#`>_|]", "", text or "")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^この動画(では|には|は)、?", "", s)
    if len(s) <= SUMMARY_MAX:
        return s
    cut = s[:SUMMARY_MAX]
    i = cut.rfind("。")
    return cut[:i + 1] if i > 0 else cut + "…"


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
    s = clean_summary(summary)
    if not s:
        return None
    s = html.escape(s, quote=True)
    out = base + "動画の内容：" + s + ("" if s.endswith(("。", "…")) else "。")
    return None if MARKER in out else out


def detect(reviews_dir):
    """[(path, rid, summary)] を返す。ledger の最新レコードと reviews の実物を突き合わせるだけ。"""
    groups = ledger._build_groups(ledger._load_all_records())
    ok = {}
    for key, g in groups.items():
        r = g["latest"]
        if (key[0] == "rid" and r.get("video_understood") is True
                and "video_content" not in (r.get("missing") or [])
                and (r.get("video_summary") or "").strip()):
            ok[str(key[1])] = r["video_summary"]
    found = []
    for path in sorted(glob.glob(os.path.join(reviews_dir, "*.html"))):
        try:
            _raw, cards, _bad, = cd.load_review_cards(path)[:3]
        except Exception as e:
            print("CARD_REFRESH_WARN: %s: %s" % (os.path.basename(path), e))
            continue
        for c in cards:
            rid = c.get("rid")
            if rid in ok and cd._marker_disclosed(cd.vdesc_text(c["raw"]), MARKER):
                found.append((path, rid, ok[rid]))
    return found


def rewrite_file(path, items):
    """path 内の items=[(rid, summary)] のカードだけ書き直す。書いた rid のリストを返す。"""
    raw, cards, _a, _b, malformed = cd.load_review_cards(path)
    if malformed:
        return []
    want = dict(items)
    edits = []
    for c in cards:
        if c.get("rid") not in want:
            continue
        span = cd._vdesc_span(c["raw"])
        if span is None:
            continue
        a, b = span
        inner = c["raw"][a:b]
        new = new_inner(inner, want[c["rid"]])
        if new is not None:
            edits.append((c["start"] + a, c["start"] + b, new, c["rid"]))
    if not edits:
        return []
    out, pos = [], 0
    for a, b, new, _rid in edits:
        out.append(raw[pos:a])
        out.append(new)
        pos = b
    out.append(raw[pos:])
    text = "".join(out)
    cards2, bad2 = cd.extract_vcards(text)
    if bad2 or len(cards2) != len(cards):
        return []
    tmp = path + ".refresh.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return [e[3] for e in edits]


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="書き直す（既定は検出のみ）")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--reviews-dir", default="reviews")
    p.add_argument("--facts-dir")
    a = p.parse_args(argv)
    if a.facts_dir:
        ledger.FACTS_DIR = a.facts_dir
    try:
        found = detect(a.reviews_dir)
    except Exception as e:
        print("CARD_REFRESH_ERROR: %s" % e)
        return 0
    todo = found[:max(a.limit, 0)]
    rewritten, files = [], []
    if a.apply:
        for path in sorted({f[0] for f in todo}):
            done = rewrite_file(path, [(r, s) for pp, r, s in todo if pp == path])
            if done:
                rewritten += done
                files.append(path.replace(os.sep, "/"))
    print("CARD_REFRESH: mode=%s candidates=%d limit=%d rewritten=%d rids=%s files=%s"
          % ("apply" if a.apply else "detect", len(found), a.limit, len(rewritten),
             ",".join(found_rid for _p, found_rid, _s in (todo if not a.apply else [])) or
             ",".join(rewritten) or "-", ",".join(files) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
