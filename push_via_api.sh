#!/usr/bin/env bash
# push_via_api.sh — ローカルのファイルを、中身をエージェントの文脈に通さずに GitHub へ送る。
#
#   使い方: bash push_via_api.sh <owner/repo> <コミットメッセージ> <パス> [<パス> ...]
#   例:     bash push_via_api.sh katut-brain/katut-brain.github.io "update: 2026-09-08" \
#             fetch_facts/2026-09-08.json reviews/2026-09-08.html
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
# 設計上の約束:
#   - 送ったあと必ず GitHub から読み返し、SHA-256 が一致したときだけ成功と見なす。
#     「push できた」の自己申告を成功条件にしない。
#   - 1ファイル1コミット（Contents API の仕様）。複数渡すと引数の順にコミットが分かれる。
#     Actions を最後に正しい状態で発火させるため、reviews は最後に渡すこと。
#   - 途中で失敗しても、そこまでに成功したファイルは戻さない（無人運用で巻き戻しを試みるほうが危険）。
#     呼び出し側は WRITE_PATH 行を見て、失敗したパスだけフォールバックすること。
#
# 終了コード: 0=全ファイル成功 / 1=1つ以上失敗 / 2=引数エラー
# 標準出力に1ファイル1行の `WRITE_PATH:` 行を出す（無人ランのログで経路を追うため）。

set -uo pipefail

if [ "$#" -lt 3 ]; then
  echo "usage: bash push_via_api.sh <owner/repo> <message> <path> [<path> ...]" >&2
  exit 2
fi

REPO="$1"; shift
MESSAGE="$1"; shift

if ! command -v gh >/dev/null 2>&1; then
  echo "WRITE_PATH: - api=unavailable reason=gh_not_found"
  exit 1
fi

BRANCH="${PUSH_VIA_API_BRANCH:-main}"
failed=0

for path in "$@"; do
  if [ ! -f "$path" ]; then
    echo "WRITE_PATH: $path api=skipped reason=local_file_missing"
    failed=1
    continue
  fi

  local_bytes=$(wc -c < "$path" | tr -d ' ')
  local_sha=$(sha256sum "$path" | cut -d' ' -f1)

  if ! b64=$(base64 -w0 "$path" 2>/dev/null); then
    echo "WRITE_PATH: $path api=failed reason=base64_failed"
    failed=1
    continue
  fi

  # 既存ファイルの更新には blob sha が要る。存在しなければ空のまま（新規作成）。
  remote_sha=$(gh api "repos/$REPO/contents/$path?ref=$BRANCH" --jq '.sha' 2>/dev/null || true)
  case "$remote_sha" in
    *[!0-9a-f]* | "") remote_sha="" ;;
  esac

  if [ -n "$remote_sha" ]; then
    put_out=$(gh api -X PUT "repos/$REPO/contents/$path" \
      -f message="$MESSAGE" -f branch="$BRANCH" -f content="$b64" -f sha="$remote_sha" 2>&1)
  else
    put_out=$(gh api -X PUT "repos/$REPO/contents/$path" \
      -f message="$MESSAGE" -f branch="$BRANCH" -f content="$b64" 2>&1)
  fi
  put_rc=$?

  if [ "$put_rc" -ne 0 ]; then
    # 認証・スコープ・権限のどれで落ちたかを1行に残す（トークンの値は出さない）。
    reason=$(printf '%s' "$put_out" | tr '\n' ' ' | sed 's/[^ -~]//g' | cut -c1-160)
    echo "WRITE_PATH: $path api=failed rc=$put_rc reason=${reason:-unknown}"
    failed=1
    continue
  fi

  # 送った内容が本当にその通り載ったかを、読み返して照合する。
  tmp=$(mktemp)
  if ! gh api "repos/$REPO/contents/$path?ref=$BRANCH" \
        -H "Accept: application/vnd.github.raw" > "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    echo "WRITE_PATH: $path api=ok verify=unreadable"
    failed=1
    continue
  fi

  remote_bytes=$(wc -c < "$tmp" | tr -d ' ')
  remote_content_sha=$(sha256sum "$tmp" | cut -d' ' -f1)
  rm -f "$tmp"

  if [ "$remote_content_sha" = "$local_sha" ]; then
    echo "WRITE_PATH: $path api=ok verify=match bytes=$local_bytes"
  else
    echo "WRITE_PATH: $path api=ok verify=MISMATCH local_bytes=$local_bytes remote_bytes=$remote_bytes"
    failed=1
  fi
done

exit "$failed"
