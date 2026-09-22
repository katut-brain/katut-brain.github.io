#!/usr/bin/env python3
"""reviews/<TARGET>.html を安全に更新する。既存が無ければ新規ファイルを
そのまま置くだけ、既存があれば**統合する**（上書きで既存カードを消さない）。

## 背景（2026-09-22）

select_targets.py 導入後、reviews/*.html 自体が「掲載済み」の記録を兼ねる。
手順5は reviews/<TARGET>.html を新規に書くため、同じ TARGET でランが2回
走る（手動実行・TARGET_OVERRIDE・同日の再実行）と、既存のカードが上書きで
消える。一度消えると select_targets.py はそのURL/ridをもう「未掲載」と
見なさない＝復元できない。この場合に限り統合する。

## 使い方

  python3 merge_review.py --target 2026-09-21 --new /tmp/review_new.html

## 統合の方針（既存の構造・並びは変えず、末尾へ追記する）

  - `.vcard`: 新規側のうち、既存側に無いものだけを、新しい見出し
    「➕ 追記（M/D HH:MM JST）」＋新しい `.cards` ブロックとして footer の
    直前に追加する。重複判定は「rid一致」「強いIDキー(post_id)一致」
    「generic種別URLキー一致」の**いずれか1つでも成立すれば重複**とする
    （select_targets.classify_reviewed の判定と同じ優先順位ではなく、
    3種類それぞれ独立にOR判定する——別rid同士でも強いIDキーが一致すれば
    「同じ投稿」なので、rid一致だけを見て見逃してはいけない。
    2026-09-22 ユーザー裁定「同じ投稿の再保存は重複とみなす」と揃える）。
  - `.summary p`: 新規側の各 `<p>` を、既存の `<section class="summary">`
    の末尾（閉じタグ直前）に「追記 M/D HH:MM JST: 」を先頭に付けて追加する。
  - `.meta`（テーマ行）: 既存のテキストを残し、新規側にあって既存に無い
    トークン（"・"/","/"、" 区切り）だけを "・" で連結して追記する。
  - `<h2>` + `<section class="notes">` の組（「やりたい・気になったこと」
    節・「🔎 深掘り（関連記事）」節など、どちらも構造は同じなので同じ
    ロジックで扱う）: 直前の `<h2>` のテキストで既存・新規を対応付ける。
    同名の節が既存にあれば、`<ul class="ilist">`/`<ul class="qlist">` の
    `<li>`（正規化した文字列で重複排除）を各リストの末尾に追加する
    （既存にそのリスト種別が無ければ新しく作る）。同名の節が既存に無ければ、
    新規側の `<h2>` + `<section>` をまるごと footer 直前に追加する。
  - footer 直前の `<p class="notegen-warn">`: 既存を残し、新規側にあって
    既存に無いもの（正規化して重複排除）を追加する。
  - run-timing コメント（`<!-- run-timing ... -->`）: **既存のものだけを
    残し、新規側は常に捨てる。** `run_timing.py` の `insert_comment()` は
    「`</footer>` の直前にちょうど1件」だけを前提に、直前にある正規形
    コメントを1つだけ剥がしてから新しい1件を置く（run_timing.py の
    `insert_comment()` を参照）。手順の実行順序上（手順5でこのスクリプトを
    呼び、run-timing の書き込みは手順7）、このスクリプトが呼ばれる時点で
    新規側に run-timing コメントが存在することは通常無いはずだが、仮に
    あっても2件併存させると「ちょうど1件」の前提が崩れ、手順7の
    `run_timing.py` が古いコメントを剥がせずに重複が蓄積する事故になる。
    よって新規側の run-timing コメントは統合時に捨て、既存側だけを残す
    （既存側が無ければ何も置かない＝手順7が新しく1件書く）。
  - 上記の追加物はすべて、既存の run-timing コメントより**前**（footer
    直前で run-timing コメントが最後に来るように）挿入する。そうしないと
    `run_timing.py` 側の「直前にちょうど1件」判定が崩れる。

## 安全性

どんな例外でも既存ファイルを壊さない: 一時ファイル+os.replace で原子的に
書き、失敗時（既存の `</footer>` が一意に特定できない等）は exit 1 を返して
既存ファイルには一切書き込まない（読むだけ）。
"""

import argparse
import datetime
import io
import os
import re
import sys
import traceback

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import select_targets  # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))

# run_timing.py の COMMENT_RE と同じ正規形（正本は run_timing.py。
# ここでは「既存側にこれが直前にあるか」を判定するためだけに複製する
# ——run_timing.py 自体をimportして使うと、状態ファイル読み込み等
# 無関係な副作用の経路に依存することになるため、正規表現だけを揃える）。
_RUN_TIMING_COMMENT_RE = re.compile(
    r"[ \t]*<!-- run-timing schema=v\d+ [0-9A-Za-z_=,\- ]*-->[ \t]*\r?\n?"
)
_NOTEGEN_WARN_RE = re.compile(r'<p class="notegen-warn">.*?</p>', re.S)
_H2_RE = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.S)
_P_RE = re.compile(r"<p\b[^>]*>.*?</p>", re.S)
_LI_RE = re.compile(r"<li\b[^>]*>.*?</li>", re.S)


# --- 任意タグの深さカウント切り出し（select_targets.extract_vcards と同じ
# アルゴリズムを div 以外にも使えるよう一般化したもの。select_targets の
# 切り出しヘルパー(_blank_comments/_get_attr/_has_class_token)をそのまま
# 再利用し、手書きの正規表現を増やしすぎないようにする） -------------------

def _open_tag_re(tag):
    return re.compile(r"<%s\b[^>]*>" % re.escape(tag))


def _find_next_open(html_str, tag, pos):
    n = len(html_str)
    while True:
        i = html_str.find("<%s" % tag, pos)
        if i == -1:
            return -1
        after = i + 1 + len(tag)
        if after < n and (html_str[after].isspace() or html_str[after] in ">/"):
            return i
        pos = i + 1


def extract_blocks(html_text, tag, cls=None):
    """class トークンに cls を含む `<tag>...</tag>` ブロックを深さカウントで
    切り出す（cls=None ならクラス属性を問わず、そのタグの出現ごとに切り出す）。
    返り値は (blocks, malformed) のタプル（select_targets.extract_vcards と
    同じ形）。blocks は [{"start", "end", "raw"}, ...]。malformed は、
    閉じタグが足りずに最後まで閉じられなかったブロックが1件でもあれば True
    （2026-09-22 Codexレビュー3周目 指摘対応で追加。merge_review.py の
    validate_structure() がこれを見て、壊れた入力での統合を拒否する）。
    """
    blocks = []
    malformed = False
    idx = 0
    n = len(html_text)
    structural = select_targets._blank_comments(html_text)
    open_re = _open_tag_re(tag)
    close_tag = "</%s>" % tag
    for m in open_re.finditer(structural):
        start = m.start()
        if start < idx:
            continue
        if cls is not None:
            c = select_targets._get_attr(m.group(0), "class")
            if not select_targets._has_class_token(c, cls):
                continue
        pos = m.end()
        depth = 1
        while depth > 0:
            next_open = _find_next_open(structural, tag, pos)
            next_close = structural.find(close_tag, pos)
            if next_close == -1:
                pos = n
                malformed = True
                break
            if next_open != -1 and next_open < next_close:
                depth += 1
                pos = next_open + 1 + len(tag)
            else:
                depth -= 1
                pos = next_close + len(close_tag)
        end = pos
        blocks.append({"start": start, "end": end, "raw": html_text[start:end]})
        idx = end
    return blocks, malformed


def _strip_tag(raw, tag):
    """`<tag ...>content</tag>` から content を取り出す。マッチしなければ
    raw をそのまま返す（安全側）。
    """
    m = re.match(r"^<%s\b[^>]*>(.*)</%s>\s*$" % (re.escape(tag), re.escape(tag)),
                 raw, re.S)
    return m.group(1) if m else raw


def _normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def _dedup_keep_order(items, seen_norm):
    """items のうち、seen_norm（正規化済み文字列の集合。呼び出し側が持つ
    ものをそのまま渡す＝副作用で更新される）に無いものだけを順序保持で返す。
    """
    out = []
    for it in items:
        norm = _normalize(it)
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        out.append(it)
    return out


def _h2_text_before(html_text, block_start):
    """block_start より前にある最後の `<h2>...</h2>` の中身を返す
    （見つからなければ None）。厳密なHTML解釈はしない——比較キーとして
    使うだけなので、タグの中身をそのまま正規化して使う。
    """
    matches = list(_H2_RE.finditer(html_text[:block_start]))
    if not matches:
        return None
    return _normalize(matches[-1].group(1))


def _footer_insertion_point(html):
    """`</footer>` の絶対位置と、追加物を挿入すべき位置を返す
    (footer_idx, insert_at)。

    insert_at は「既存の run-timing コメントより前」（run_timing.py 側の
    『直前にちょうど1件』前提を壊さないため）。run-timing コメントが
    直前に無ければ `</footer>` の位置そのもの。

    `</footer>` がちょうど1つでない場合は (None, None) を返す（安全側:
    位置を一意に特定できないときは footer 周りを一切いじらない。
    呼び出し側はこれを例外として扱う）。
    """
    if html.count("</footer>") != 1:
        return None, None
    footer_idx = html.find("</footer>")
    head = html[:footer_idx]
    stripped = head.rstrip(" \t\r\n")
    last_match = None
    for cand in _RUN_TIMING_COMMENT_RE.finditer(stripped):
        if cand.end() == len(stripped):
            last_match = cand
    if last_match is not None:
        return footer_idx, last_match.start()
    return footer_idx, footer_idx


def _notes_list_patches(existing_block, new_block_raw):
    """既存の1つの `.notes` セクション（同名 h2 に対応付け済み）に対して、
    新規側の同名セクションの `<li>` 差分を反映するための (abs_pos, text)
    パッチのリストを返す。既存に該当リスト種別（ilist/qlist）が無ければ
    新しく `<ul>` ごと footer 直前ではなくその section 内へ作る。
    """
    patches = []
    for list_cls in ("ilist", "qlist"):
        new_uls, _m_new_ul = extract_blocks(new_block_raw, "ul", list_cls)
        if not new_uls:
            continue
        new_lis = _LI_RE.findall(new_uls[0]["raw"])
        if not new_lis:
            continue
        existing_uls, _m_existing_ul = extract_blocks(existing_block["raw"], "ul", list_cls)
        if existing_uls:
            existing_lis_norm = {
                _normalize(li) for li in _LI_RE.findall(existing_uls[0]["raw"])
            }
            add_lis = _dedup_keep_order(new_lis, existing_lis_norm)
            if not add_lis:
                continue
            rel_close = existing_uls[0]["raw"].rfind("</ul>")
            if rel_close == -1:
                continue
            abs_pos = (existing_block["start"] + existing_uls[0]["start"]
                       + rel_close)
            patches.append((abs_pos, "\n".join(add_lis) + "\n"))
        else:
            rel_close = existing_block["raw"].rfind("</section>")
            if rel_close == -1:
                continue
            abs_pos = existing_block["start"] + rel_close
            patches.append((abs_pos, new_uls[0]["raw"] + "\n"))
    return patches


def validate_structure(html_text):
    """統合前に新規/既存いずれのHTMLも通す構造検証（2026-09-22 Codexレビュー
    3周目 指摘対応）。統合はテキストの切り貼りで行うため、入力そのものが
    壊れていると統合結果も壊れる。ここで壊れていることを検出し、呼び出し側
    （merge()）に例外を投げさせて mode=error にする——統合方式では壊れた
    既存カードを直せないので、壊れたまま統合を続けない。

    戻り値は問題点の説明文リスト（空リストなら問題なし）。確認する項目:
      - `<!doctype html>` で始まる
      - `</html>` で終わる
      - `.vcard`（select_targets.extract_vcards）が閉じている
        （malformed=True でない）
      - `.summary`・`.notes` セクション（extract_blocks）が閉じている
      - 上記いずれのブロックも、終端が文書の `</html>` 終端をまたいでいない
        （親コンテナや文書末尾を越えて切り出されていないか）
    """
    errors = []
    stripped_head = html_text.lstrip()
    if not stripped_head.lower().startswith("<!doctype html>"):
        errors.append("<!doctype html> で始まっていない")
    stripped_tail = html_text.rstrip()
    if not stripped_tail.lower().endswith("</html>"):
        errors.append("</html> で終わっていない")
    doc_end = len(stripped_tail)

    cards, cards_malformed = select_targets.extract_vcards(html_text)
    if cards_malformed:
        errors.append(".vcard に閉じていないブロックがある")
    for c in cards:
        if c["end"] > doc_end:
            errors.append(".vcard がドキュメント終端(</html>)をまたいでいる")
            break

    for tag, cls in (("section", "summary"), ("section", "notes")):
        blocks, block_malformed = extract_blocks(html_text, tag, cls)
        if block_malformed:
            errors.append('<%s class="%s"> に閉じていないブロックがある'
                          % (tag, cls))
        for b in blocks:
            if b["end"] > doc_end:
                errors.append(
                    '<%s class="%s"> がドキュメント終端(</html>)をまたいでいる'
                    % (tag, cls)
                )
                break

    return errors


def merge(existing_html, new_html, now_jst):
    """統合後のHTML文字列と統計dict（kept/added/skipped_dup）を返す。

    例外はここでは握りつぶさない（呼び出し側 main() が捕捉して exit 1 に
    する。この関数が例外を投げた場合、呼び出し側は既存ファイルへの書き込み
    を一切行わない）。
    """
    for label, html_text in (("既存", existing_html), ("新規", new_html)):
        errs = validate_structure(html_text)
        if errs:
            raise ValueError(
                "%sファイルの構造検証に失敗したため統合を中止する: %s"
                % (label, "; ".join(errs))
            )

    stats = {"kept": 0, "added": 0, "skipped_dup": 0}
    ts = now_jst.strftime("%m/%d %H:%M")

    footer_idx, insert_at = _footer_insertion_point(existing_html)
    if insert_at is None:
        raise ValueError(
            "既存reviewsの</footer>が一意に特定できない（0件または複数件）"
            "ため、安全のため統合を中止する"
        )

    to_insert_chunks = []  # insert_at へまとめて挿入する断片（順序どおり連結）
    patches = []  # (abs_pos, text) の直接挿入パッチ（既存ブロックの中身へ）

    # --- 1. .vcard ---
    # 重複判定は select_targets.card_matches_any() を正本にする（2026-09-22
    # Codexレビュー3周目 指摘対応: 以前はここに同じロジックをローカル関数で
    # 複製していたが、check_review_preserved.py 側の保持判定と食い違う
    # リスクがあるため select_targets.py に1つだけ置いて両方から使う）。
    # rid・強いIDキー(post_id)・generic種別URLキーの3種類をそれぞれ独立な
    # 集合として持ち、いずれか1つでも一致すれば重複とする——別rid同士でも
    # 強いIDキーが一致すれば「同じ投稿」なので、rid不一致だけを理由に
    # 見逃してはいけない。
    existing_cards, _m1 = select_targets.extract_vcards(existing_html)
    new_cards, _m2 = select_targets.extract_vcards(new_html)
    existing_rids, existing_post_ids, existing_generic = \
        select_targets.card_key_sets(c["raw"] for c in existing_cards)
    stats["kept"] = len(existing_cards)

    add_cards = []
    for c in new_cards:
        if select_targets.card_matches_any(
                c["raw"], existing_rids, existing_post_ids, existing_generic):
            stats["skipped_dup"] += 1
            continue
        add_cards.append(c["raw"])
        # 新規側内部の重複も後続で弾くために、追加した分も取り込んでおく。
        for kind, val in select_targets.card_identity_keys(c["raw"]):
            if kind == "rid":
                existing_rids.add(val)
            elif kind == "post_id":
                existing_post_ids.add(val)
            else:
                existing_generic.add(val)
    stats["added"] = len(add_cards)
    if add_cards:
        to_insert_chunks.append(
            '\n<h2>➕ 追記（%s JST）</h2>\n<div class="cards">\n%s\n</div>\n'
            % (ts, "\n".join(add_cards))
        )

    # --- 2. .summary p ---
    new_summary_blocks, _m_new_summary = extract_blocks(new_html, "section", "summary")
    existing_summary_blocks, _m_existing_summary = extract_blocks(existing_html, "section", "summary")
    if new_summary_blocks:
        new_ps = _P_RE.findall(new_summary_blocks[0]["raw"])
        if new_ps:
            addition = "".join(
                "<p>追記 %s JST: %s</p>\n" % (ts, _strip_tag(p, "p"))
                for p in new_ps
            )
            if existing_summary_blocks:
                block = existing_summary_blocks[0]
                rel_close = block["raw"].rfind("</section>")
                if rel_close != -1:
                    patches.append((block["start"] + rel_close, addition))
            else:
                to_insert_chunks.append(new_summary_blocks[0]["raw"])

    # --- 3. .meta（テーマ行） ---
    new_meta_blocks, _m_new_meta = extract_blocks(new_html, "div", "meta")
    existing_meta_blocks, _m_existing_meta = extract_blocks(existing_html, "div", "meta")
    if new_meta_blocks and existing_meta_blocks:
        block = existing_meta_blocks[0]
        existing_text = _strip_tag(block["raw"], "div")
        new_text = _strip_tag(new_meta_blocks[0]["raw"], "div")
        existing_norm = {
            _normalize(t) for t in re.split(r"[・,、]", existing_text) if t.strip()
        }
        new_tokens = [t for t in re.split(r"[・,、]", new_text) if t.strip()]
        add_tokens = _dedup_keep_order(new_tokens, existing_norm)
        if add_tokens:
            rel_close = block["raw"].rfind("</div>")
            if rel_close != -1:
                addition = "・" + "・".join(t.strip() for t in add_tokens)
                patches.append((block["start"] + rel_close, addition))
    elif new_meta_blocks and not existing_meta_blocks:
        to_insert_chunks.append(new_meta_blocks[0]["raw"])

    # --- 4. <h2> + <section class="notes"> の組（やりたい/気になった・深掘り） ---
    existing_notes, _m_existing_notes = extract_blocks(existing_html, "section", "notes")
    new_notes, _m_new_notes = extract_blocks(new_html, "section", "notes")
    existing_by_h2 = {}
    for b in existing_notes:
        h2 = _h2_text_before(existing_html, b["start"])
        if h2 is not None and h2 not in existing_by_h2:
            existing_by_h2[h2] = b
    for b in new_notes:
        h2 = _h2_text_before(new_html, b["start"])
        if h2 is None:
            continue
        if h2 in existing_by_h2:
            patches.extend(_notes_list_patches(existing_by_h2[h2], b["raw"]))
        else:
            heading = ""
            head_matches = list(_H2_RE.finditer(new_html[:b["start"]]))
            if head_matches:
                heading = head_matches[-1].group(0)
            to_insert_chunks.append("\n" + heading + "\n" + b["raw"] + "\n")

    # --- 5. footer直前の notegen-warn ---
    existing_warns = _NOTEGEN_WARN_RE.findall(existing_html)
    new_warns = _NOTEGEN_WARN_RE.findall(new_html)
    existing_warn_norm = {_normalize(w) for w in existing_warns}
    add_warns = _dedup_keep_order(new_warns, existing_warn_norm)
    if add_warns:
        to_insert_chunks.append("\n" + "\n".join(add_warns) + "\n")

    # --- 6. run-timing コメント: 新規側は常に捨てる（モジュール docstring 参照） ---
    # （to_insert_chunks / patches のどちらにも新規側の run-timing コメントを
    #  一切含めない。既存側のコメントはそもそも触っていないのでそのまま残る）

    if to_insert_chunks:
        patches.append((insert_at, "\n".join(to_insert_chunks) + "\n"))

    # 位置降順で適用する（後ろから挿入すればオフセットがずれない）。
    patches.sort(key=lambda t: t[0], reverse=True)
    html = existing_html
    for pos, text in patches:
        html = html[:pos] + text + html[pos:]

    return html, stats


def _atomic_write(path, content):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.replace(tmp, path)


def main(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument(
        "--reviews-dir",
        default=os.path.join(REPO_DIR, "reviews"),
    )
    args = parser.parse_args(argv)

    dest = os.path.join(args.reviews_dir, "%s.html" % args.target)

    try:
        with io.open(args.new, encoding="utf-8", newline="") as f:
            new_html = f.read()
    except Exception as e:
        print("MERGE_STATUS: target=%s mode=error error=%s (cannot read --new)"
              % (args.target, type(e).__name__))
        return 1

    try:
        if not os.path.exists(dest):
            errs = validate_structure(new_html)
            if errs:
                print("MERGE_STATUS: target=%s mode=error error=invalid_structure (%s)"
                      % (args.target, "; ".join(errs)))
                return 1
            _atomic_write(dest, new_html)
            cards, _malformed = select_targets.extract_vcards(new_html)
            print("MERGE_STATUS: target=%s mode=new kept=0 added=%d skipped_dup=0"
                  % (args.target, len(cards)))
            return 0

        with io.open(dest, encoding="utf-8", newline="") as f:
            existing_html = f.read()

        merged_html, stats = merge(existing_html, new_html,
                                    datetime.datetime.now(JST))
        _atomic_write(dest, merged_html)
        print("MERGE_STATUS: target=%s mode=merged kept=%d added=%d skipped_dup=%d"
              % (args.target, stats["kept"], stats["added"], stats["skipped_dup"]))
        return 0
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print("MERGE_STATUS: target=%s mode=error error=%s"
              % (args.target, type(e).__name__))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
