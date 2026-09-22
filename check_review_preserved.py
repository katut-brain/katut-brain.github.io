#!/usr/bin/env python3
"""reviews/<日付>.html のカード（掲載記録）が消えていないかを確認する
公開ゲート。

## 何のためにあるか

select_targets.py 導入後、reviews/*.html 自体が「掲載済み」の記録を兼ねる
（capture_index.json の日付台帳ベースの判定から、reviews の実掲載を直接見る
方式へ2026-09-22に変更済み）。そのため、reviews/<日付>.html が**同じファイル名
のまま上書きされてカードが一部消える**と、そのURL/rid は二度と「掲載済み」と
確認できなくなり、かつ select_targets.py はもう選ばない（一度掲載済みと
見なされたら、それを取り消す仕組みが無い）＝実質的にそのカードの記録が
永久に失われる。

build-feed.yml の既存の「Check reviews shrink」ステップは**ファイル単位**
（存在の有無・バイト数の縮小）でしか見ておらず、同じファイル名のまま一部の
カードだけが消える上書きを検知できない。このスクリプトは**カード単位**
（data-rid / 強いIDキー / generic種別URLキー）で見る。

## 2つのモード

**`--history` モード（build-feed.yml が使う。正本）**: `--old-ref` の
ような**2点間の差分**は使わない（2026-09-08 のユーザー裁定と同じ理由。
「複数コミットのpushで、途中のコミットのカード消失を差分が一度も見ない」
という穴があるため——例えば削除コミットAで49→48件になり、Aは単独で見れば
検知できても、翌晩の無害なコミットBは before=A で before(A)=current(B)=48
のため diff(A,B) には現れず、縮小済みの状態がそのまま通る）。
`--history` は**そのファイルの git 履歴そのもの**を基準にする: 一度でも
そのファイルの過去のどこかの版に現れたカードの識別キーが、**現在の版**の
どれかのカードに残っているかを確認する。消えているキーについては、
「そのキーを最後に保持していたコミットの次のコミット（＝最後に消した
コミット）」のメッセージに `[allow-review-shrink]` があれば個別に許可する
（既存の「Check reviews shrink」と同じマーカーの流儀）。

**`--old-ref` モード（publish-from-branch.yml が使う）**: origin/main の
既存版 1点と、取り込み対象ブランチの新しい版 1点を比較する単純な2点比較。
publish-from-branch.yml は「main に取り込む前」の検証であり、対象は
「そのブランチの1コミットぶんの差分だけ」なので、複数コミットにまたがる
push を見逃す問題がそもそも起きない（1ブランチ=1コミットという構造上の
制約が publish-from-branch.yml 側にある）。

## 保持判定（両モード共通・2026-09-22 Codexレビュー3周目 指摘対応）

カードの保持は「rid・強いIDキー(post_id)・generic種別URLキーのいずれか
1つでも現在版のどれかのカードと一致すれば保持」という OR 判定にする
（select_targets.card_matches_any() を正本にする。merge_review.py の
重複判定と同じ関数を共有する——別rid・同じ強いIDキーのカードを、別物と
誤認して「消えた」と過検知しない）。

## カード枚数の単調性チェック（2026-09-22 Codexレビュー5周目 指摘対応）

上記のキー単位のOR判定だけでは、次の2種類の消失を見逃す:
  ① 1枚の新カードが、旧カード2枚ぶんの識別キーを同時に満たす場合
     （例: 誤統合で href は旧カードAのもの・data-rid は旧カードBのものに
     なった1枚のカードができると、キー単位ではAもBも「保持されている」
     ように見えるが、実際には2枚あったカードが1枚に潰れている）。
  ② 識別子を持たない `.vcard`（rid無し・href無し/キー抽出不能）は
     `card_identity_keys()` が空集合を返すため、そもそもキー単位の比較
     対象に入らない。そのカードが消えても検知できない。
どちらも「**カードの総枚数**が減っていないか」を見れば検知できるので、
キー単位のOR判定に**加えて**、`.vcard` の総枚数が減っていないかを
別途チェックする（両方の判定の**論理和**で shrunk を決める。枚数が
足りていてもキーが消えていれば shrunk、キーが揃っていても枚数が
減っていれば shrunk）。`--old-ref` は旧版と新版の枚数を比較し、
`--history` はそのファイルの履歴上の最大枚数と現在版の枚数を比較する
（枚数を減らしたコミットのメッセージに `[allow-review-shrink]` が
あれば、キー単位の判定と同じ流儀で許可する）。

## 使い方

  # history モード（build-feed.yml）
  python3 check_review_preserved.py --history --path reviews/2026-09-21.html [--path ...]
  python3 check_review_preserved.py --history --all [--reviews-dir reviews]

  # old-ref モード（publish-from-branch.yml）
  python3 check_review_preserved.py --old-ref origin/main --path reviews/2026-09-21.html [--allow]

終了コード: カードの消失があり許可されていなければ 1、それ以外は常に 0。
"""

import argparse
import glob
import os
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import select_targets  # noqa: E402

ALLOW_MARKER = "[allow-review-shrink]"


def _extract_card_raws(html_text):
    cards, _malformed = select_targets.extract_vcards(html_text)
    return [c["raw"] for c in cards]


def card_ids(html_text):
    """HTML文字列から (rid集合, post_id集合, url集合) を返す
    （select_targets.card_key_sets のラッパー）。
    """
    return select_targets.card_key_sets(_extract_card_raws(html_text))


def missing_cards(old_html, new_html):
    """old_html の各カードが new_html に保持されているかを
    select_targets.card_matches_any()（rid・post_id・generic の**いずれか
    1つでも**一致すれば保持、というOR判定）で確認し、保持されていない
    カードの識別キー集合を返す（2026-09-22 Codexレビュー3周目 指摘対応:
    単一優先順位の完全一致ではなく、別rid・同じ強いIDキーのカードは
    「保持されている」とみなす）。
    識別できないカード（rid無し・href無し等）は比較対象にしない
    （select_targets.card_identity_keys() が空集合を返す場合はスキップ）。
    """
    old_cards = _extract_card_raws(old_html)
    new_cards = _extract_card_raws(new_html)
    new_rids, new_post_ids, new_urls = select_targets.card_key_sets(new_cards)
    missing = set()
    for raw in old_cards:
        keys = select_targets.card_identity_keys(raw)
        if not keys:
            continue
        if select_targets.card_matches_any(raw, new_rids, new_post_ids, new_urls):
            continue
        missing.update(keys)
    return missing


def _git_show(ref, path):
    """git show <ref>:<path> の標準出力を返す。存在しない/読めない場合は
    None（「新規ファイルで比較対象が無い」と「読めない」を区別しない——
    どちらの場合も安全側は「チェックをスキップ」で、失敗させる必要はない。
    誤って古い版が読めないことを『縮小』と誤検知しない）。
    """
    try:
        r = subprocess.run(
            ["git", "show", "%s:%s" % (ref, path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _git_log_commits(path):
    """path の変更履歴コミット一覧を、古い→新しい順で返す（追加・変更・
    削除、どの種類のコミットも含める。削除コミットを含めないと『誰が
    最後に消したか』を正しく特定できない）。
    """
    r = subprocess.run(
        ["git", "log", "--format=%H", "--reverse", "--", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]


def _commit_message(commit):
    r = subprocess.run(
        ["git", "log", "-1", "--format=%B", commit],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.stdout if r.returncode == 0 else ""


class _UnionFind(object):
    """キー（("rid", 1) のようなタプル）を要素とする単純な素集合データ構造。
    同じカードに同時に現れたキー同士（例: rid と href の強いIDキー）を
    同じクラスタに束ねるために使う（下記 check_path_history 参照）。
    """

    def __init__(self):
        self._parent = {}

    def find(self, x):
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self._parent[rx] = ry


def check_path_history(path, allow_marker=ALLOW_MARKER):
    """path の全履歴版に一度でも現れたカードが、現在版のどれかのカードに
    保持されているかを確認する（git履歴そのものを基準にする。差分ベース
    にしない——2026-09-08裁定と同じ理由: 複数コミットのpushで、途中の
    コミットが落としたカードを差分は一度も見ない）。

    保持判定は「カードの持つキー（rid・強いIDキー・genericキー）の
    いずれか1つでも現在版のどれかのカードと一致すれば保持」という
    OR判定（select_targets.card_matches_any() と同じ考え方。2026-09-22
    Codexレビュー3周目 指摘対応）。

    実装上の注意: 1枚の歴史上のカードが複数キー（例: rid + href由来の
    強いIDキー）を持つとき、そのうち**片方だけ**が後のコミットで変わって
    （例: rid が訂正された）ももう片方（href）が生きていれば「同じカードは
    保持されている」とみなしたい。単純に「キーごとに独立して、そのキーの
    値がどこかの版に一度でも現れたか」だけを見ると、rid が変わった時点で
    旧rid値そのものは二度と現れないため誤って missing 扱いになってしまう
    （実機のバグで発見）。そこで Union-Find で「同じカードに同時に現れた
    ことのあるキー同士」を1つのクラスタにまとめ、**クラスタ単位**で
    「現在版のどれかのキーと重なるか」を判定する。

    枚数チェック（2026-09-22 Codexレビュー5周目 指摘対応）: 上記のキー単位
    のOR判定だけでは、①1枚の新カードが旧カード2枚の識別子を同時に満たす
    ケース、②識別子を持たないカードの消失、のどちらも検知できない。
    そこで**現在版の `.vcard` 総枚数**を、そのファイルの履歴上の**最大
    枚数**と比較する。現在が最大枚数を下回っていれば、そのぶんは
    「最後に最大枚数だったコミットの次のコミット」が減らしたとみなし、
    そのコミットのメッセージに `[allow-review-shrink]` があれば許可する
    （キー単位の判定と同じ流儀）。この枚数チェックとキー単位のOR判定は
    **論理和**で shrunk を決める（どちらか一方でも引っかかれば shrunk）。

    戻り値: {"path", "status": "ok"|"shrunk"|"no_history",
             "missing": [str,...], "allowed_missing": [str,...],
             "count_max": int, "count_current": int,
             "count_shrunk": bool, "count_allowed": bool}
    """
    commits = _git_log_commits(path)
    if not commits:
        return {"path": path, "status": "no_history", "missing": [],
                "allowed_missing": [], "count_max": 0, "count_current": 0,
                "count_shrunk": False, "count_allowed": False}

    uf = _UnionFind()
    per_commit_card_keysets = []  # [(commit, [frozenset(keys), ...])]
    per_commit_counts = []  # [int, ...]（commits と同じ並び）
    for commit in commits:
        text = _git_show(commit, path)
        card_keysets = []
        count = 0
        if text is not None:
            raws = _extract_card_raws(text)
            count = len(raws)
            for raw in raws:
                keys = select_targets.card_identity_keys(raw)
                if not keys:
                    continue
                keys_list = list(keys)
                for k in keys_list[1:]:
                    uf.union(keys_list[0], k)
                card_keysets.append(frozenset(keys))
        per_commit_card_keysets.append((commit, card_keysets))
        per_commit_counts.append(count)

    try:
        with open(path, encoding="utf-8") as fh:
            current_text = fh.read()
    except OSError:
        current_text = None
    current_cards = _extract_card_raws(current_text) if current_text else []
    current_all_keys = set()
    for raw in current_cards:
        current_all_keys |= select_targets.card_identity_keys(raw)

    # クラスタ（=同一カードとみなす歴史上のキー群）ごとに、現在版へ
    # 保持されているかを判定する。
    clusters = {}  # root -> set(keys)
    for commit, card_keysets in per_commit_card_keysets:
        for keyset in card_keysets:
            root = uf.find(next(iter(keyset)))
            clusters.setdefault(root, set()).update(keyset)

    missing_clusters = [
        keys for keys in clusters.values() if not (keys & current_all_keys)
    ]

    missing_report = []
    allowed = []
    for keys in missing_clusters:
        label = "|".join(sorted("%s:%s" % (k, v) for k, v in keys))

        # このクラスタのキーを最後に保持していたコミットの「次」を
        # 消したコミットとみなす。
        last_had_idx = None
        for i, (_commit, card_keysets) in enumerate(per_commit_card_keysets):
            if any(ks & keys for ks in card_keysets):
                last_had_idx = i

        removing_commit = None
        if last_had_idx is not None and last_had_idx + 1 < len(per_commit_card_keysets):
            removing_commit = per_commit_card_keysets[last_had_idx + 1][0]
        if removing_commit is not None and allow_marker in _commit_message(removing_commit):
            allowed.append(label)
            continue
        missing_report.append(label)

    # --- 枚数チェック ---
    count_max = max(per_commit_counts)
    count_current = len(current_cards)
    count_shrunk = count_current < count_max
    count_allowed = False
    if count_shrunk:
        last_max_idx = None
        for i, c in enumerate(per_commit_counts):
            if c == count_max:
                last_max_idx = i
        count_removing_commit = None
        if last_max_idx is not None and last_max_idx + 1 < len(commits):
            count_removing_commit = commits[last_max_idx + 1]
        if count_removing_commit is not None and allow_marker in _commit_message(count_removing_commit):
            count_allowed = True

    result = {
        "path": path,
        "missing": sorted(missing_report),
        "allowed_missing": sorted(allowed),
        "count_max": count_max,
        "count_current": count_current,
        "count_shrunk": count_shrunk,
        "count_allowed": count_allowed,
    }
    if missing_report or (count_shrunk and not count_allowed):
        result["status"] = "shrunk"
    else:
        result["status"] = "ok"
    return result


def check_path(old_ref, path):
    """1ファイル分のチェック結果を dict で返す（--old-ref モード）。
    {"path", "status": "new"|"ok"|"shrunk"|"missing_now", "missing": [str,...],
     "count_old", "count_new"}

    枚数チェック（2026-09-22 Codexレビュー5周目 指摘対応）: 新版の `.vcard`
    枚数が旧版より**少なければ**、キー単位のOR判定が missing=0 でも
    shrunk とする（① 1枚の新カードが旧カード2枚の識別子を同時に満たす
    ケース、② 識別子を持たないカードの消失、のどちらもキー単位の判定
    だけでは検知できないため）。
    """
    old_text = _git_show(old_ref, path)
    if old_text is None:
        return {"path": path, "status": "new", "missing": [],
                "count_old": None, "count_new": None}
    try:
        with open(path, encoding="utf-8") as fh:
            new_text = fh.read()
    except OSError:
        return {"path": path, "status": "missing_now", "missing": [],
                "count_old": None, "count_new": None}
    missing = missing_cards(old_text, new_text)
    count_old = len(_extract_card_raws(old_text))
    count_new = len(_extract_card_raws(new_text))
    if missing or count_new < count_old:
        return {"path": path, "status": "shrunk",
                "missing": sorted("%s:%s" % (k, v) for k, v in missing),
                "count_old": count_old, "count_new": count_new}
    return {"path": path, "status": "ok", "missing": [],
            "count_old": count_old, "count_new": count_new}


def main(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--old-ref",
                         help="2点比較モード。比較対象の古い版を読む git ref"
                              "（例: origin/main）")
    parser.add_argument("--history", action="store_true",
                         help="git履歴基準モード。--old-refは使わず、各"
                              "パスの全履歴版と現在版を比較する")
    parser.add_argument("--path", action="append", default=[], dest="paths")
    parser.add_argument("--all", action="store_true",
                         help="--reviews-dir 配下の *.html すべてを対象にする"
                              "（--history と併用する想定）")
    parser.add_argument("--reviews-dir", default="reviews")
    parser.add_argument("--allow", action="store_true",
                         help="消失を検知しても exit 0 にする（呼び出し側が"
                              " [allow-review-shrink] を全体として確認済みの"
                              "ときに使う。--history モードではコミット単位の"
                              "許可判定が既定で効くので、通常は不要）")
    args = parser.parse_args(argv)

    if args.history and args.old_ref:
        print("CHECK_REVIEW_PRESERVED_ERROR: --history と --old-ref は同時指定できない")
        return 1
    if not args.history and not args.old_ref:
        print("CHECK_REVIEW_PRESERVED_ERROR: --history か --old-ref のどちらかが必要")
        return 1

    paths = list(args.paths)
    if args.all:
        paths.extend(sorted(glob.glob(os.path.join(args.reviews_dir, "*.html"))))
    # git のパススペックは常にスラッシュ区切り。Windows で glob.glob() や
    # os.path.join() がバックスラッシュ区切りのパスを返すと `git log`/
    # `git show` がそのパスを履歴上のファイルと一致させられず、実際には
    # 履歴があるのに no_history 扱いになってしまう（2026-09-22 実機で発見。
    # --all 経由でのみ再現し、フォワードスラッシュを直接 --path に渡す
    # 経路では起きなかった）。ここで一括して正規化する。
    paths = [p.replace(os.sep, "/") for p in paths]
    # 重複除去（順序は保つ）。
    seen = set()
    uniq_paths = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            uniq_paths.append(p)
    paths = uniq_paths

    if not paths:
        print("CHECK_REVIEW_PRESERVED: no --path given, nothing to check")
        return 0

    any_missing = False
    if args.history:
        for path in paths:
            result = check_path_history(path)
            count_note = " count_max=%d count_current=%d" % (
                result["count_max"], result["count_current"])
            if result["status"] == "shrunk":
                any_missing = True
                allow_note = (" count_allowed=1" if result["count_allowed"] else "")
                print("CHECK_REVIEW_PRESERVED: path=%s status=shrunk missing=%d "
                      "ids=%s allowed=%d%s%s"
                      % (result["path"], len(result["missing"]),
                         result["missing"], len(result["allowed_missing"]),
                         count_note, allow_note))
            elif result["status"] == "no_history":
                print("CHECK_REVIEW_PRESERVED: path=%s status=no_history"
                      % result["path"])
            else:
                allowed_note = (" allowed=%d" % len(result["allowed_missing"])
                                 if result["allowed_missing"] else "")
                print("CHECK_REVIEW_PRESERVED: path=%s status=ok%s%s"
                      % (result["path"], allowed_note, count_note))
    else:
        for path in paths:
            result = check_path(args.old_ref, path)
            count_note = (" count_old=%s count_new=%s"
                          % (result["count_old"], result["count_new"]))
            if result["status"] == "shrunk":
                any_missing = True
                print("CHECK_REVIEW_PRESERVED: path=%s status=shrunk missing=%d ids=%s%s"
                      % (result["path"], len(result["missing"]), result["missing"],
                         count_note))
            elif result["status"] == "missing_now":
                any_missing = True
                print("CHECK_REVIEW_PRESERVED: path=%s status=missing_now "
                      "(existed before, file not found now)" % result["path"])
            else:
                print("CHECK_REVIEW_PRESERVED: path=%s status=%s%s"
                      % (result["path"], result["status"], count_note))

    if any_missing:
        if args.allow:
            print("CHECK_REVIEW_PRESERVED: shrink detected but allowed (--allow)")
            return 0
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
