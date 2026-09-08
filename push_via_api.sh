#!/usr/bin/env bash
# push_via_api.sh — ローカルのファイルを、中身をエージェントの文脈に通さずに GitHub へ送る。
#
#   送る:   bash push_via_api.sh <owner/repo> <コミットメッセージ> <パス> [<パス> ...]
#   照合:   bash push_via_api.sh --verify-only <owner/repo> <パス> [<パス> ...]
#
#   例: bash push_via_api.sh katut-brain/katut-brain.github.io "update: 2026-09-08" \
#         fetch_facts/2026-09-08.json reviews/2026-09-08.html
#
# なぜこれが要るか（2026-09-08）:
#   従来は GitHub MCP の push_files に本文を「文字列引数」で渡していた。本文がエージェントの
#   文脈を通るので、通るたびに化ける。実害2件 —— index.html 122KB の truncate（2026-08-30・
#   公開履歴2.5ヶ月分が消失）と、reviews/2026-09-03.html の全角括弧11箇所の化け（79cfd12）。
#   このスクリプトは本文をディスクから直接読んで送るので、本文は一度も文脈を通らない。
#
# 経路が成立する根拠（code.claude.com/docs/en/cloud-environments を原文照合・2026-09-08）:
#   - api.github.com は Trusted の既定 allowlist に含まれる
#   - "requests from ... `gh` under the `proxy-injected` placeholder, go out with your real
#     credentials substituted." ＝ gh はトークンを自分で持たなくても認証済みで送れる
#   - "a script that reads `GITHUB_TOKEN` directly gets the placeholder, not a usable token."
#     ＝ 素の curl では送れない。必ず gh 経由にすること
#
# ■ 順番の設計（2026-09-08 の敵対的レビュー3周目・ここが本体）
#   「押してから確かめる」ではなく「**確かめてから載せる**」。Contents API は PUT した瞬間に
#   main へコミットが載るので、その後の照合 GET が失敗しても未検証の中身が main に残る。
#   そこで Git Data API を使い、次の順で進める:
#
#     1. main の先端 commit sha を取る
#     2. 各ファイルを blob として作る          ← main からは見えない
#     3. main の tree を土台に新しい tree を作る ← main からは見えない
#     4. commit を作る（親＝1で取った先端）      ← main からは見えない・宙に浮いている
#     5. **作った blob を読み返して SHA-256 を全件照合する**
#     6. 全件一致したときだけ、main の ref をその commit へ進める（force=false ＝
#        早送りのみ。途中で main が動いていたら失敗する）
#
#   5で1件でも落ちたら6を行わない。**main は一切動かない**（宙に浮いたオブジェクトは
#   GitHub 側の GC で消える）。副産物として複数ファイルが1コミットにまとまるので、
#   Actions の二重発火も無くなる。
#
# ■ 本文を argv に置かない
#   Linux の execve は単一引数が約128KiB までで、base64 は約4/3倍に膨らむ。argv に置くと
#   元ファイル約98KB で `Argument list too long` になり、122KB の index.html が壊れたのと
#   同じサイズの崖を作り直すことになる。リクエストボディは常に一時ファイルに書いて
#   `gh api --input` で渡す。
#
# ■ --verify-only
#   main に載っている実体が、ローカル原本とバイト単位で一致しているかだけを確かめる
#   （読み取りのみ・何も書かない）。手順書の常用経路ではなく、点検用:
#     - あるリポジトリに読み取りで到達できるかを確かめる（Vaultリポの疎通確認など）
#     - 過去に押したファイルが化けていないかを後から調べる
#   かつて `push_files` へフォールバックした夜の照合に使う設計だったが、
#   フォールバック自体を廃止したので（2026-09-08 の敵対的レビュー4周目）、
#   今は常用されない。
#
# 終了コード: 0=成功 / 1=失敗（main は動いていない） / 2=引数エラー
# 標準出力: 1ファイル1行の `WRITE_PATH:` と、最後に `WRITE_COMMIT:` を出す
#           （無人ランのログで経路を追うため）。

set -uo pipefail

MODE="push"
if [ "${1:-}" = "--verify-only" ]; then
  MODE="verify"
  shift
fi

if [ "$MODE" = "push" ] && [ "$#" -lt 3 ]; then
  echo "usage: bash push_via_api.sh <owner/repo> <message> <path> [<path> ...]" >&2
  exit 2
fi
if [ "$MODE" = "verify" ] && [ "$#" -lt 2 ]; then
  echo "usage: bash push_via_api.sh --verify-only <owner/repo> <path> [<path> ...]" >&2
  exit 2
fi

REPO="$1"; shift
MESSAGE=""
if [ "$MODE" = "push" ]; then
  MESSAGE="$1"; shift
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "WRITE_PATH: - api=unavailable reason=gh_not_found"
  exit 1
fi

BRANCH="${PUSH_VIA_API_BRANCH:-main}"
PY="${PUSH_VIA_API_PYTHON:-python3}"

TMPDIR_SELF=$(mktemp -d) || { echo "WRITE_PATH: - api=failed reason=mktemp_failed"; exit 1; }
cleanup() { rm -rf "$TMPDIR_SELF"; }
trap cleanup EXIT

# 失敗理由を1行に潰す（トークンの値は出さない）。
squash() { printf '%s' "$1" | tr '\n' ' ' | sed 's/[^ -~]//g' | cut -c1-160; }

# ---------------------------------------------------------------- verify-only
if [ "$MODE" = "verify" ]; then
  failed=0
  for path in "$@"; do
    if [ ! -f "$path" ]; then
      echo "WRITE_PATH: $path verify=skipped reason=local_file_missing"
      failed=1
      continue
    fi
    local_sha=$(sha256sum "$path" | cut -d' ' -f1)
    tmp="$TMPDIR_SELF/remote"
    if ! gh api "repos/$REPO/contents/$path?ref=$BRANCH" \
          -H "Accept: application/vnd.github.raw+json" > "$tmp" 2>/dev/null; then
      echo "WRITE_PATH: $path verify=unreadable"
      failed=1
      continue
    fi
    if [ "$(sha256sum "$tmp" | cut -d' ' -f1)" = "$local_sha" ]; then
      echo "WRITE_PATH: $path verify=match bytes=$(wc -c < "$path" | tr -d ' ')"
    else
      echo "WRITE_PATH: $path verify=MISMATCH local_bytes=$(wc -c < "$path" | tr -d ' ') remote_bytes=$(wc -c < "$tmp" | tr -d ' ')"
      failed=1
    fi
  done
  exit "$failed"
fi

# --------------------------------------------------------------------- push
for path in "$@"; do
  if [ ! -f "$path" ]; then
    echo "WRITE_PATH: $path api=skipped reason=local_file_missing"
    echo "WRITE_COMMIT: none reason=local_file_missing"
    exit 1
  fi
done

# 1. main の先端を取る
base_sha=$(gh api "repos/$REPO/git/ref/heads/$BRANCH" --jq '.object.sha' 2>&1)
if [ "$?" -ne 0 ] || [ -z "$base_sha" ]; then
  echo "WRITE_PATH: - api=failed reason=$(squash "$base_sha")"
  echo "WRITE_COMMIT: none reason=cannot_read_ref"
  exit 1
fi
base_tree=$(gh api "repos/$REPO/git/commits/$base_sha" --jq '.tree.sha' 2>&1)
if [ "$?" -ne 0 ] || [ -z "$base_tree" ]; then
  echo "WRITE_PATH: - api=failed reason=$(squash "$base_tree")"
  echo "WRITE_COMMIT: none reason=cannot_read_base_tree"
  exit 1
fi

# 2. blob を作る（main からは見えない）
blob_list="$TMPDIR_SELF/blobs.tsv"
: > "$blob_list"
for path in "$@"; do
  body="$TMPDIR_SELF/blob-body.json"
  if ! "$PY" -c '
import base64, json, sys
path, out = sys.argv[1:3]
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"content": base64.b64encode(open(path, "rb").read()).decode("ascii"),
               "encoding": "base64"}, fh)
' "$path" "$body" 2>/dev/null; then
    echo "WRITE_PATH: $path api=failed reason=payload_build_failed"
    echo "WRITE_COMMIT: none reason=payload_build_failed"
    exit 1
  fi
  blob_sha=$(gh api -X POST "repos/$REPO/git/blobs" --input "$body" --jq '.sha' 2>&1)
  if [ "$?" -ne 0 ] || [ -z "$blob_sha" ]; then
    echo "WRITE_PATH: $path api=failed reason=$(squash "$blob_sha")"
    echo "WRITE_COMMIT: none reason=blob_failed"
    exit 1
  fi
  printf '%s\t%s\n' "$blob_sha" "$path" >> "$blob_list"
done

# 3. tree を作る（main からは見えない）
tree_body="$TMPDIR_SELF/tree.json"
if ! "$PY" -c '
import json, sys
base, listing, out = sys.argv[1:4]
entries = []
with open(listing, encoding="utf-8") as fh:
    for line in fh:
        line = line.rstrip("\n")
        if not line:
            continue
        sha, path = line.split("\t", 1)
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"base_tree": base, "tree": entries}, fh)
' "$base_tree" "$blob_list" "$tree_body" 2>/dev/null; then
  echo "WRITE_COMMIT: none reason=tree_build_failed"
  exit 1
fi
tree_sha=$(gh api -X POST "repos/$REPO/git/trees" --input "$tree_body" --jq '.sha' 2>&1)
if [ "$?" -ne 0 ] || [ -z "$tree_sha" ]; then
  echo "WRITE_COMMIT: none reason=$(squash "$tree_sha")"
  exit 1
fi

# 4. commit を作る（まだ宙に浮いている＝main からは見えない）
commit_body="$TMPDIR_SELF/commit.json"
if ! "$PY" -c '
import json, sys
message, tree, parent, out = sys.argv[1:5]
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"message": message, "tree": tree, "parents": [parent]}, fh)
' "$MESSAGE" "$tree_sha" "$base_sha" "$commit_body" 2>/dev/null; then
  echo "WRITE_COMMIT: none reason=commit_build_failed"
  exit 1
fi
commit_sha=$(gh api -X POST "repos/$REPO/git/commits" --input "$commit_body" --jq '.sha' 2>&1)
if [ "$?" -ne 0 ] || [ -z "$commit_sha" ]; then
  echo "WRITE_COMMIT: none reason=$(squash "$commit_sha")"
  exit 1
fi

# 5. 作った blob を読み返して全件照合する（ここで落ちても main は動いていない）
verified=1
while IFS=$'\t' read -r blob_sha path; do
  [ -z "$blob_sha" ] && continue
  local_sha=$(sha256sum "$path" | cut -d' ' -f1)
  tmp="$TMPDIR_SELF/readback"
  if ! gh api "repos/$REPO/git/blobs/$blob_sha" \
        -H "Accept: application/vnd.github.raw+json" > "$tmp" 2>/dev/null; then
    echo "WRITE_PATH: $path api=staged verify=unreadable"
    verified=0
    continue
  fi
  if [ "$(sha256sum "$tmp" | cut -d' ' -f1)" = "$local_sha" ]; then
    echo "WRITE_PATH: $path api=staged verify=match bytes=$(wc -c < "$path" | tr -d ' ')"
  else
    echo "WRITE_PATH: $path api=staged verify=MISMATCH local_bytes=$(wc -c < "$path" | tr -d ' ') remote_bytes=$(wc -c < "$tmp" | tr -d ' ')"
    verified=0
  fi
done < "$blob_list"

if [ "$verified" -ne 1 ]; then
  echo "WRITE_COMMIT: none reason=verify_failed note=main_untouched"
  exit 1
fi

# 6. 全件一致したときだけ main を進める（force=false ＝ 早送りのみ）
ref_body="$TMPDIR_SELF/ref.json"
if ! "$PY" -c '
import json, sys
sha, out = sys.argv[1:3]
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"sha": sha, "force": False}, fh)
' "$commit_sha" "$ref_body" 2>/dev/null; then
  echo "WRITE_COMMIT: none reason=ref_build_failed note=main_untouched"
  exit 1
fi
ref_out=$(gh api -X PATCH "repos/$REPO/git/refs/heads/$BRANCH" --input "$ref_body" 2>&1)
if [ "$?" -ne 0 ]; then
  # ⚠️ 失敗＝未更新、と断定してはいけない（2026-09-08 の敵対的レビュー4周目の指摘）。
  # サーバー側が ref を進めた後で応答だけ失われることがある。`force=false` は
  # 上書きを防ぐが、HTTP の「結果不明」問題は解決しない。**必ず読み直して確かめる。**
  actual=$(gh api "repos/$REPO/git/ref/heads/$BRANCH" --jq '.object.sha' 2>/dev/null)
  reread_rc=$?
  if [ "$reread_rc" -eq 0 ] && [ "$actual" = "$commit_sha" ]; then
    # 応答は失われたが、更新自体は通っていた。
    echo "WRITE_COMMIT: $commit_sha files=$# branch=$BRANCH note=confirmed_after_lost_response"
    exit 0
  fi
  if [ "$reread_rc" -ne 0 ] || [ -z "$actual" ]; then
    # 読み直せない＝載ったかどうか分からない。この状態で再送すると二重コミットに、
    # 別経路で押すと未検証の上書きになる。**何もしないのが正しい。**
    echo "WRITE_COMMIT: unknown reason=$(squash "$ref_out") note=state_unknown_do_not_retry"
    exit 3
  fi
  # ⚠️ 先端が違う＝未更新、とも断定できない（2026-09-08 の敵対的レビュー5周目の指摘）。
  # 「PATCH は通ったが応答が消え、その直後に別の書き手が main を進めた」場合、
  # 先端は我々の commit の *子孫* になる。ここで未更新と誤認して再送すると、
  # その別の書き手の変更を古いローカル内容で上書きしてしまう。
  # そこで、我々の commit が現在の先端の祖先かどうかを確かめる。
  cmp_status=$(gh api "repos/$REPO/compare/$commit_sha...$actual" --jq '.status' 2>/dev/null)
  cmp_rc=$?
  if [ "$cmp_rc" -ne 0 ]; then
    echo "WRITE_COMMIT: unknown reason=cannot_compare note=state_unknown_do_not_retry"
    exit 3
  fi
  case "$cmp_status" in
    identical|ahead)
      # 我々の commit は祖先＝ PATCH は成功していた。再送してはいけない。
      echo "WRITE_COMMIT: $commit_sha files=$# branch=$BRANCH note=confirmed_as_ancestor"
      exit 0
      ;;
  esac
  # 祖先ではない＝本当に載っていない（早送りできなかった等）。
  echo "WRITE_COMMIT: none reason=$(squash "$ref_out") note=main_untouched"
  exit 1
fi

echo "WRITE_COMMIT: $commit_sha files=$# branch=$BRANCH"
exit 0
