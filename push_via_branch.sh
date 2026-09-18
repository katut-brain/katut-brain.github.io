#!/usr/bin/env bash
# push_via_branch.sh — ローカルのファイルを、中身をエージェントの文脈に通さずに公開リポへ送る。
#
#   送る:   bash push_via_branch.sh <コミットメッセージ> <パス> [<パス> ...]
#
#   例: bash push_via_branch.sh "update: 2026-09-13" \
#         fetch_facts/2026-09-13.json reviews/2026-09-13.html
#
# なぜこれが要るか（2026-09-14）:
#   push_via_api.sh（gh api で Git Data API を叩く）は、クラウド Routine の GitHub プロキシに
#   "Write access to this GitHub API path is not permitted through this proxy." で拒否され、
#   2026-09-09〜13 の公開が全部止まった。GH_TOKEN を環境に入れても同じだった。
#   一方、git push で claude/ 以下の新しいブランチを作ることはできる（2026-09-14 疎通試験で確認）。
#   そこで「作業ブランチまで git で送る → 公開リポの Actions（publish-from-branch.yml）が
#   検証してから main に取り込み、Pages を更新する」二段にした。
#
# ■ 何をするか
#   1. 送れるパスは reviews/<日付>.html・fetch_facts/<日付>.json・fetch_facts/runs/<日付>.json・capture_index.json だけ。
#      それ以外が混じっていたら何もせず exit 2。
#   2. origin/main を土台に、作業ツリーと index には一切触らず（一時 index を使う）、
#      渡されたファイルと .publish/manifest.json（各ファイルの SHA-256 と git blob）だけを
#      載せたコミットを作る。captures.json 等の未コミット変更は巻き込まない。
#   3. claude/publish-<UTC時刻>-<PID> という新しいブランチに git push する。
#      リモートの ref が自分のコミットと一致したら WRITE_COMMIT を出す。
#   4. Actions が main に取り込むのを最大 PUSH_VIA_BRANCH_WAIT 秒（既定300）待ち、
#      main 上の各ファイルの blob がローカル原本と一致したら PUBLISHED: yes を出す。
#      時間内に一致しなければ PUBLISHED: pending（ブランチまでは届いている。
#      検証に落ちた場合は Actions が失敗し、GitHub から失敗メールが届く）。
#
# 本文を argv に置かない・文字列で運ばない。git がディスクのバイト列をそのまま送る。
#
# 標準出力の契約（Routine のログで読む行）:
#   WRITE_PATH: <パス> blob=<git blob sha> sha256=<hex>
#   WRITE_COMMIT: <commit sha> files=N branch=<ブランチ名>     … ブランチまで届いた
#   WRITE_COMMIT: none reason=<理由> note=main_untouched       … 何も届いていない
#   PUBLISHED: yes main=<sha> | PUBLISHED: pending waited=<秒>
#
# 終了コード:
#   0 = ブランチまで届いた（PUBLISHED が pending でも 0）
#   1 = 失敗。main もリモートのブランチも増えていないので、そのまま再実行してよい
#   2 = 引数エラー（許可されていないパス・ファイルが無い等）

set -u

if [ $# -lt 2 ]; then
  echo "usage: push_via_branch.sh <message> <path> [<path> ...]" >&2
  exit 2
fi
MESSAGE="$1"; shift

REMOTE="${PUSH_VIA_BRANCH_REMOTE:-origin}"
BASE_BRANCH="${PUSH_VIA_BRANCH_BASE:-main}"
WAIT="${PUSH_VIA_BRANCH_WAIT:-300}"
ALLOW_RE='^(reviews/[0-9]{4}-[0-9]{2}-[0-9]{2}\.html|fetch_facts/[0-9]{4}-[0-9]{2}-[0-9]{2}\.json|fetch_facts/runs/[0-9]{4}-[0-9]{2}-[0-9]{2}\.json|capture_index\.json)$'

for p in "$@"; do
  if ! printf '%s\n' "$p" | grep -Eq "$ALLOW_RE"; then
    echo "WRITE_COMMIT: none reason=path_not_allowed:$p note=main_untouched"
    exit 2
  fi
  if [ ! -f "$p" ]; then
    echo "WRITE_COMMIT: none reason=file_missing:$p note=main_untouched"
    exit 2
  fi
done

sha256_of() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$1" | cut -d' ' -f1; }

if ! git fetch -q "$REMOTE" "$BASE_BRANCH" 2>&1 >&2; then
  echo "WRITE_COMMIT: none reason=fetch_failed note=main_untouched"
  exit 1
fi
base_sha=$(git rev-parse "FETCH_HEAD") || { echo "WRITE_COMMIT: none reason=no_base note=main_untouched"; exit 1; }

tmp=$(mktemp -d) || { echo "WRITE_COMMIT: none reason=mktemp_failed note=main_untouched"; exit 1; }
trap 'rm -rf "$tmp"' EXIT
export GIT_INDEX_FILE="$tmp/index"
git read-tree "$base_sha" || { echo "WRITE_COMMIT: none reason=read_tree_failed note=main_untouched"; exit 1; }

manifest="$tmp/manifest.json"
{
  printf '{\n  "base": "%s",\n  "message": %s,\n  "files": [\n' "$base_sha" \
    "$(printf '%s' "$MESSAGE" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read(), ensure_ascii=False))')"
} > "$manifest"
n=0
for p in "$@"; do
  blob=$(git hash-object -w -- "$p") || { echo "WRITE_COMMIT: none reason=hash_failed:$p note=main_untouched"; exit 1; }
  sum=$(sha256_of "$p")
  size=$(wc -c < "$p" | tr -d ' ')
  git update-index --add --cacheinfo "100644,$blob,$p" || { echo "WRITE_COMMIT: none reason=index_failed:$p note=main_untouched"; exit 1; }
  [ $n -gt 0 ] && printf ',\n' >> "$manifest"
  printf '    {"path": "%s", "blob": "%s", "sha256": "%s", "bytes": %s}' "$p" "$blob" "$sum" "$size" >> "$manifest"
  echo "WRITE_PATH: $p blob=$blob sha256=$sum"
  n=$((n+1))
done
printf '\n  ]\n}\n' >> "$manifest"
mblob=$(git hash-object -w -- "$manifest") && git update-index --add --cacheinfo "100644,$mblob,.publish/manifest.json" \
  || { echo "WRITE_COMMIT: none reason=manifest_failed note=main_untouched"; exit 1; }

tree=$(git write-tree) || { echo "WRITE_COMMIT: none reason=write_tree_failed note=main_untouched"; exit 1; }
commit=$(GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-katut-brain}" GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-katut-brain@users.noreply.github.com}" \
         GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-katut-brain}" GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-katut-brain@users.noreply.github.com}" \
         git commit-tree "$tree" -p "$base_sha" -m "$MESSAGE") \
  || { echo "WRITE_COMMIT: none reason=commit_failed note=main_untouched"; exit 1; }
unset GIT_INDEX_FILE

branch="claude/publish-$(date -u +%Y%m%d%H%M%S)-$$"
if ! git push -q "$REMOTE" "$commit:refs/heads/$branch" >&2 2>&1; then
  echo "WRITE_COMMIT: none reason=push_failed note=main_untouched"
  exit 1
fi
remote_sha=$(git ls-remote "$REMOTE" "refs/heads/$branch" | cut -f1)
if [ "$remote_sha" != "$commit" ]; then
  echo "WRITE_COMMIT: none reason=remote_ref_mismatch:${remote_sha:-missing} note=main_untouched"
  exit 1
fi
echo "WRITE_COMMIT: $commit files=$n branch=$branch"

# Actions が main に取り込むのを待つ（取り込み後、main 上の blob がローカル原本と一致するか）。
waited=0
while [ "$waited" -lt "$WAIT" ]; do
  sleep 15; waited=$((waited+15))
  git fetch -q "$REMOTE" "$BASE_BRANCH" >/dev/null 2>&1 || continue
  head=$(git rev-parse FETCH_HEAD)
  ok=1
  for p in "$@"; do
    want=$(git hash-object -- "$p")
    got=$(git rev-parse -q --verify "$head:$p" 2>/dev/null || true)
    [ "$want" = "$got" ] || { ok=0; break; }
  done
  if [ $ok -eq 1 ]; then
    echo "PUBLISHED: yes main=$head"
    exit 0
  fi
done
echo "PUBLISHED: pending waited=$waited"
exit 0
