#!/usr/bin/env python3
"""深掘り済みブックマークの rid 一覧 `deepdive_state.json` を Vault から作る。

    {"done": [1756418244, ...]}

Vault（private）の Explore/bookmarks/rd-*.md のうち、`## ...深掘り` の見出しを持つ
ノートの frontmatter `raindrop_id` を集める。公開ページの deepdive-mark.js がこれを読み、
`data-rid` が一致するカードを薄くして、ボタンを「続きを話す」にする（2026-10-27 本人決定:
バッジ・アイコンは付けない）。公開されるのは rid だけ（rid は各ページに既に載っている）。

毎晩の Routine が手順2.2 で実行し、変わったときだけ単独で push する。

## 守ること

- Vault が見つからない・ノートが少なすぎる（部分的な clone）ときは何も書かない。
  空の一覧で上書きすると、全カードの「済み」が一晩で消える。
- 前回の一覧から MAX_DROP 件を超えて減るときも書かない（読み取り失敗の疑い）。
  深掘り節を本人が消した正当な減少は、ごく少数なら通る。
- 「事前リサーチ」節は深掘りではない（見出しに「深掘り」を含まない）。
"""

import argparse
import glob
import json
import os
import re
import sys

STATE = "deepdive_state.json"
MIN_NOTES = 100   # 実測 567件（2026-10-27）。これ未満は部分的な clone とみなす
MAX_DROP = 5

HEAD_RE = re.compile(r"^## .*深掘り", re.M)
RID_RE = re.compile(r"^raindrop_id:\s*[\"']?(-?\d+)[\"']?\s*$", re.M)
FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


def find_vault():
    """作業ディレクトリの隣にある、Explore/bookmarks を持つリポ。候補が一意でなければ None
    （別の clone を Vault と取り違えて別の rid を公開しない）。"""
    base = os.path.dirname(os.getcwd())
    hits = glob.glob(os.path.join(base, "*", "Explore", "bookmarks"))
    return os.path.dirname(os.path.dirname(hits[0])) if len(hits) == 1 else None


def validate(obj):
    """公開してよい形か。問題があれば理由の文字列、なければ None。
    publish-from-branch.yml も同じ関数で検査する（rid の整数配列だけを公開する境界）。"""
    if not isinstance(obj, dict) or set(obj) != {"done"}:
        return 'top level must be exactly {"done": [...]}'
    done = obj["done"]
    if not isinstance(done, list) or any(type(x) is not int for x in done):
        return "'done' must be a list of integers"
    if len(set(done)) != len(done):
        return "'done' has duplicates"
    return None


def scan(vault):
    """(ノート数, 深掘り済み rid の集合) を返す。"""
    notes, done = 0, set()
    for path in glob.glob(os.path.join(vault, "Explore", "bookmarks", "rd-*.md")):
        try:
            with open(path, encoding="utf-8-sig") as fh:  # BOM 付きも読む
                text = fh.read()  # 改行は読み込み時に \n へ揃う（CRLF のノートも同じ）
        except OSError:
            continue
        notes += 1
        m = FRONT_RE.match(text)
        if not m:
            continue
        rid = RID_RE.search(m.group(1))
        if rid and HEAD_RE.search(m.group(2)):
            done.add(int(rid.group(1)))
    return notes, done


def load_old(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return set(json.load(fh).get("done", []))
    except (OSError, ValueError, AttributeError):
        return set()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", help="Vault リポのパス（省略時は隣のディレクトリから探す）")
    ap.add_argument("--out", default=STATE)
    args = ap.parse_args(argv)

    vault = args.vault or find_vault()
    if not vault or not os.path.isdir(os.path.join(vault, "Explore", "bookmarks")):
        print("DEEPDIVE_STATE: status=skipped reason=vault_not_found_or_ambiguous")
        return 1
    notes, done = scan(vault)
    if notes < MIN_NOTES:
        print(f"DEEPDIVE_STATE: status=skipped reason=too_few_notes notes={notes}")
        return 1
    old = load_old(args.out)
    if len(old - done) > MAX_DROP:
        print(f"DEEPDIVE_STATE: status=skipped reason=would_drop dropped={len(old - done)}")
        return 1

    body = json.dumps({"done": sorted(done)}, ensure_ascii=False) + "\n"
    try:
        with open(args.out, encoding="utf-8") as fh:
            changed = fh.read() != body
    except OSError:
        changed = True
    if changed:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
    print(f"DEEPDIVE_STATE: status=ok notes={notes} done={len(done)} changed={'yes' if changed else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
