# fetch-chain（v0.2・2026-10-30）

SNS（X・Instagram・Threads・YouTube）と Web の中身を取る「取り方の一覧」と、壊れたら次の取り方へ回す仕組み。標準ライブラリだけ（Python 3.11+）。
AI は使わない。会話では `sns-fetcher`（`~/.claude/agents/`）がこれを呼び、夜間のキャプチャパイプラインは直接呼ぶ。

```
python fetchchain.py fetch <url>   # 優先順に試す。使った手段・失敗した手段・本文・生データ（data.raw）を JSON で返す
python fetchchain.py canary        # 全手段を見本で実測 → status.json（終了コード 0=全OK 1=一部だめ 2=丸ごとだめ）
python fetchchain.py status        # 直近の実測結果
python -m unittest test_fetchchain test_vet
```

## 取り方の一覧（`sources.json`）

| 系統 | 手段（優先順） | 取れるもの |
|---|---|---|
| x_post | x_fxtwitter → x_syndication → x_vxtwitter | 本文・日時・投稿者・生データ（fxtwitter は画像・引用・X Article の本文も） |
| youtube | yt_dlp_subs → yt_oembed | 字幕（元の言語を優先）・題名・投稿者・日付／題名と投稿者だけ |
| instagram | ig_oembed → og_meta | キャプション・投稿者・投稿日時（media_id から）／og:description |
| threads | og_meta | og:description（本文の頭） |
| web | web_direct → web_jina | 本文 |

- **名乗りは偽らない**。User-Agent はいつも `fetch-chain/0.2 (+リポジトリの URL)`。ブラウザやクローラー（facebookexternalhit・Discordbot 等）を名乗る取り方は、検知の回避になるので足さない（2026-10-30。クラウドの安全装置に止められたのがきっかけ。正直な名乗りで PC・クラウドとも全部通ることを実測済み）
- `where`: 手段ごとに効く場所を `{"手段": ["pc"]}` のように書ける（pc / cloud）。書いていない手段はどこでも使う。効かない場所では取得にも健康診断にも使わず、壊れたとも数えない
- `expect_by_method`: 見本で期待する文字を手段ごとに変える（自動字幕は歌詞を聞き違えるので題名の文が出ない、など）
- 直近3回続けて失敗した手段は最後尾へ回る。数えるのは**同じ場所（pc / cloud）の記録だけ**。実測で復活すれば戻る
- 道具が入っていない手段（yt-dlp が無い等）は「この場所では使えない」として飛ばす。**yt-dlp は自動では入れない**（`vet.py` が reject にする。ブラウザの保存データを読む機能があるため）
- 手段は `plugins/*.json` の宣言でも足せる（コードを含まない窓口型のみ・https のみ・標準の手段は上書き不可・UA は指定できない）。試用中は最後尾

## 記録の置き場

- `FETCHCHAIN_STATE_DIR`（既定は `~/.fetch-chain/`）に `health.jsonl`（追記のみ）と `status.json`。**リポジトリの中には置かない**（PC で何を読んだかを公開リポに混ぜない。記録には URL も書かない）
- 実行場所は自動で判定する（`GITHUB_ACTIONS` → gha、`CLAUDE_CODE_REMOTE` → claude-cloud、ほか pc）。`FETCHCHAIN_ENV` で上書きできる
- 毎日の健康診断は `.github/workflows/fetch-chain-canary.yml`（GitHub Actions・JST 06:37）。記録は記録専用ブランチ `fetch-chain-health` に積み、main には入れない。壊れていたら Secrets の `FETCHCHAIN_SLACK_WEBHOOK` へ通知する

## vet.py（他人のプログラムを入れる前の安全確認）
```
python vet.py <パッケージ.whl か展開済みフォルダ>   # 終了コード 0=clean 1=needs_review 2=reject
```
- wheel は展開せずメモリで読む。sdist は対象外（入れた瞬間に setup.py が動くため）
- reject: `.pth` の実行行／復号部品＋動的実行／資格情報・ブラウザ保存データの場所／送り先に使われがちな宛先／常駐登録／組み立てたシェル実行／環境変数の丸読み＋通信／同梱の実行ファイル
- needs_review: subprocess／ctypes／pickle／生ソケット／バイナリ／.netrc を読む／IP 直指定 URL ほか
- **安全の保証ではない**: 実行時に組み立てるコード・後から落とすコード・バイナリの中身・依存パッケージは見えない

## 既知の限界
- 見本の投稿が消えると、その系統は全滅の誤報になる（見本を差し替える）
- ログインが要る取得（X の検索・Reddit・Instagram のカルーセルの2枚目以降）は対象外。PC の会話では、全部だめなときに会話の本体が本人の Chrome で「読むだけ」で開く（sns-fetcher の指示文）
- Instagram の年齢制限アカウントは本文が取れない（日時だけ）
