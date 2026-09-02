# katut-brain 毎朝の振り返り 自動生成（クラウド Routine プロンプト）

> **このファイルが手順の正本。** Routine の Instructions 欄には短いブートストラップだけを置き、
> 実行時にこのファイルを読ませる方式に変更した（2026-08-23）。
> 手順を直したいときは**このファイルを編集して push するだけ**でよい。Instructions は触らない。
> 置き場所: `katut-brain/katut-brain.github.io` リポジトリ直下 `cloud_routine_prompt.md`。
> 以前はこの本文を Instructions 欄へ丸ごと貼っていたが、45KBを毎回貼り直す運用は
> 転記ミスの温床で、実際に本文が `PLACEHOLDER` に化けた事故が2回起きている。
> なお `projects/tools/scripts/cloud_routine_prompt.md`（リポ外・ローカル）は
> 同期されないため正本ではない。参照しないこと。

---

あなたは「毎朝の振り返り」を完全無人で生成・公開するエージェントです。人に確認を求めず、最後まで自分で完走してください。

## 前提・環境
- リポジトリ `katut-brain/katut-brain.github.io` が clone 済み（default branch = `main`）。作業はこのリポ直下。
- 環境変数 `RAINDROP_TOKEN`（Raindrop API トークン）が設定済み。
- 環境変数 `TZ=Asia/Tokyo`（日付は日本時間で計算）。
- 環境変数 `GEMINI_API_KEY`（Gemini API無料枠キー、X動画の音声+映像理解用）が設定されていれば使う。**未設定でも全体は止まらない**（`fetch_content.py`が自動でタイトルのみにフォールバックする graceful degradation設計）。
- リポ直下に `_build_graph.py` / `_build_feed.py` / `fetch_content.py` / `requirements.txt` / `captures.json` / `reviews/` がある。
- ネットワークは Raindrop API・X・Instagram・YouTube・各ニュースサイトへ到達できる。
- リポジトリ `katut-brain/obsidian-vault`（個人Vault、private）も同じワークスペースに clone 済みの前提（Routine設定でのリポジトリ追加はユーザー側で別途実施済み）。ディレクトリ名がワークスペース内で異なる場合は `Explore/bookmarks/` を含むリポをVaultリポとして特定する。以下「Vaultリポ」はこのリポを指し、常に上記 `katut-brain/katut-brain.github.io` とは別リポとして扱う（作業ディレクトリ・push先を混同しない）。

## 手順
1. **対象日**＝日本時間の「昨日」。`TARGET=$(TZ=Asia/Tokyo date -d yesterday +%F)`。
1.5. **依存インストール**：`pip install --quiet -r requirements.txt`（YouTube字幕取得用 `youtube-transcript-api`、X動画理解用 `google-genai`）。失敗しても止めない（`fetch_content.py` はこれらのパッケージが無くても他の取得は正常動作する graceful degradation設計。ただしYouTube動画は字幕なし・X動画は音声/映像理解なしのタイトルのみに落ちる）。
2. `python3 _build_graph.py` を実行。Raindrop の新規を `captures.json` に取り込み、既存レコードも冪等に更新する（失敗してもログして続行）。
   - **出力の `IMPORT_STATUS:` 行を必ず読む**。`INCOMPLETE` だった場合は Raindrop を全件取得できておらず、**その日の振り返りが欠損しうる**。この場合は手順5の `reviews/<TARGET>.html` の `</footer>` 直前に `<p class="notegen-warn">⚠️ 取り込み不完全: Raindrop取得エラー N件。欠けている保存がある可能性あり</p>` を1行足して、欠損の可能性を残す（黙って完走しない）。import は冪等なので翌ランで自動的に回復する。
   - この行を見落として「正常に完走した」と扱わないこと。無人運用ではログを誰も読まないため、**成果物側に痕跡を残すことが唯一の検知手段**になる。
3. `captures.json` を読み、`date == TARGET` のレコードを抽出。
   - **0件なら reviews は作らず手順6へ**（空ノートを作らない）。
4. **URL分類**：手順3で抽出した各レコードの `source` URL を、取得前に次の2グループへ分類する。
   - **グループA（動画理解が発動しうる投稿）**：`x.com` / `twitter.com` のURL全般（画像投稿か動画投稿か事前に判別できないため、X上のURLは一律こちらに含める）、および `instagram.com/reel/` を含むURL。この一律化により、動画を含まない通常のX投稿までバッチ処理の恩恵（一度に複数件をまとめて高速取得できる利点）を失う非効率が生じるが、事前判別コストとのトレードオフとしてこれを許容する。
   - **グループB（それ以外）**：通常のInstagram投稿（`instagram.com/p/` 等）・Threads・YouTube・一般Webの記事URL。
   - ⚠️ **なぜ分けるか**：グループAは `fetch_content.py` 内部で動画のダウンロード＋Gemini映像/音声理解が走ることがあり、1件だけで100秒を超えることがある。これを他の高速なテキスト系投稿と一緒にバッチ処理すると、バッチ全体がタイムアウトするリスクがあるため、個別処理に分離する。
   - **グループAの該当が0件の場合**：この分類・個別処理は行わず、通常通り手順4.5へ進む。
   - **グループAの処理上限**：1回のランにつき**最大20件まで**を個別処理する（自律エージェントは複数ターンにまたがる累積経過時間を正確に追跡するのが不得手なため、時間ベースではなく件数ベースの上限とする）。**20件の枠の割り当て優先順位**：まず `instagram.com/reel/` 該当URL（URL形式から確定で動画と分かる）を優先的に割り当て、残り枠を `x.com` / `twitter.com` のURLに抽出順で割り当てる。21件目以降のグループA該当URLは動画理解を諦め、`captures.json` の title / note / cover による通常のキャプションのみの扱いとし、`missing` に `"video_content"` を入れて正直に記録する。
     - ⚠️ 20件は2026-08-04時点で「1回の実行時間に明確な上限があるという確証が公式・非公式のどちらにも見当たらなかった」ことを踏まえた実験的な引き上げ（元は5件・最悪ケース240秒想定で設定）。最悪ケースでは20件×480秒=最大160分かかる計算になる。もしこの値でRoutineの実行が完走しない・タイムアウトする事態が確認されたら、5〜10件程度に戻すこと。
   - **グループAの取得コマンド**：1件ずつ個別に、シェルの `timeout` コマンドで包んだ形で実行する：`timeout 480 python3 fetch_content.py <単一URL>`（この `timeout` コマンドの引数は秒単位＝480秒＝8分）。クラウドRoutine実行環境でBashツールの `timeout:` パラメータが確実に機能するか未検証のため、**主たるタイムアウト制御はこのシェルレベルの `timeout` コマンドとする**。Bashツールの `timeout: 480000`（ミリ秒）も二重の保険として併用してよいが、それに依存しない。このコマンドが exit code 124（タイムアウトによる強制終了）または他の非ゼロ終了コードで終わった場合も、通常の取得失敗と同様に扱う：該当URLは `captures.json` の値にフォールバックし、`missing` に `"video_content"` を記録して、エラーで停止せず次のURLの処理を続ける。
     - ⚠️ 480秒（480000ms）は2026-08-01時点の限られた実測データ（最大234.97秒）に基づく暫定値。今後これを超える実測が確認されたら値を見直すこと。
   - **グループBの取得**：従来通り、`python3 fetch_content.py <url1> <url2> ...` のように一度に最大20本程度をまとめて取得する。
   - いずれのグループも、取得結果から title / text / author / handle / likes / date / cover を得る。取得失敗した分は `captures.json` の title / note / cover をそのまま使う。
   - ⚠️ URL は `captures.json` の `source` を**逐語コピー**。ID・ショートコードを推測/生成しない（過去に 404 を量産した事故あり）。
   - **YouTube動画**：`text` にタイトルだけでなく字幕（transcript、`has_transcript: true` なら最大50,000字＝claudetube準拠。ほとんどの動画は全篇カバーされる）が入る。カード生成時（手順5の`.vdesc`）はタイトルの言い換えでなく、**この字幕内容を読んだ上で動画が何を伝えているか**を一言にする。字幕が取れなかった場合（`has_transcript: false` / `missing: ["transcript"]`）はタイトルのみで一言を作る。
   - **X動画**：`GEMINI_API_KEY`が設定されていれば、`text`に「動画の内容: ...」として映像+音声の理解結果が自動で埋め込まれる（Pythonスクリプト側で完結、追加のエージェント側操作は不要）。カード生成時はこの内容を読んだ上で一言にする。理解できなかった場合（`video_understood: false`。`missing`に`"video_content"`が入る）は、動画を見たかのような一言を書かないこと（見ていないので書けない）。ツイート本文（`text`）自体は取れていることが多いので、その中身は普通に`.vdesc`に反映してよいが、動画部分だけは未取得だと分かるよう明示する。これは一過性の失敗（Gemini枠切れ・タイムアウト・syndication瞬断）であり構造的な取得不可ではないので、「動画の内容は未取得」ではなく「今回は動画の内容を取得できなかった」と書く。
   - **X画像・Instagram画像**：レスポンスの`photos`配列に高解像度URLが入る（X: `?format=jpg&name=large`付き、Instagram: og:imageの署名URLそのまま=既に実質フル解像度）。**特にスクリーンショット・図解・インフォグラフィックなど文字/情報量が多そうな画像**は、そのURLを`curl -sL -A "Mozilla/5.0" -o /tmp/img_<N>.jpg "<URL>"`等でダウンロードし、**Readツールで直接見て内容を読み取ってから**`.vdesc`に反映する（WebFetchで画像URLを直接見せる経路は内容を誤認識するリスクがあるため使わない）。1件あたり画像は先頭1〜2枚まで（処理コスト抑制のため）。単なる人物写真・風景等で読み取る情報が乏しいと判断した場合はダウンロードをスキップしてよい（完走優先）。
   - **Instagram Reels動画**：**動画の中身は取得しない**（2026-08-23に断念）。依存していた非公式中継サービスが全滅し、代替も実測で全て使えなかったため、`fetch_content.py` から取得処理を撤去した。Reel は**キャプション＋og:image のみ**が返り、`depth: "partial"` / `missing: ["video_content"]` が最初から入っている。**`.vdesc` はキャプションと画像から作る**。動画を見たかのような一言を書かないこと（見ていないので書けない）。これは一過性の失敗ではなく構造的な制約なので、「今回は取得できなかった」ではなく「動画の内容は未取得」と書く。経緯: Vault `Brain/decisions/2026-08-23-instagram-reel-video-give-up.md`
   - **X動画は従来どおり継続**（syndication API 経由で動いている）。上の断念は Instagram に限る。
   - 🚫 **取得できているのに「未取得」と書くことを禁止する**（2026-08-23の監査で実害を確認）。`.vdesc` に「本文未取得」「詳細不明」「取得できず」と書いてよいのは、**その回の `fetch_content.py` の戻り値が実際に `ok: false`、または `text` が実質空だった場合に限る**。実例：rid=1828807146 は原文1,059字（「96日で14,282回・勝率52%・1日約$1,310」まで含む）が完全に取得できていたのに `.vdesc` は「詳細本文は未取得」と書き、rid=1828798202 も109字（「毎月13億トークン」「Free Claude Code というリポジトリ」）が取れていたのに「本文未取得のため詳細不明」と書いた。**ユーザーから見ればシステムが嘘の報告をしていることになり、実際には取れている中身を読むために元リンクを踏む羽目になる。** 書く前に必ず `text` の中身を確認する。
   - **「今回取得できず」と「構造的に取得不可」を書き分ける**：前者は一過性（タイムアウト・レート制限・瞬断）で、翌晩には取れる可能性がある。後者は仕様上取れない（Threadsの画像内容、字幕の無いYouTube、凍結アカウントなど）。この2つを同じ文言で書くと、**一過性の失敗が恒久的な取得不可としてノートに焼き付く**（実測: 「本文・画像とも取得できず」と書かれた2件を後日再取得したら両方とも取れた）。一過性側は「今回は取得できなかった」と書く。
   - **`.vdesc`（一言）生成の共通原則（全コンテンツ種別に適用）**：`captures.json` の `text`（記事本文・YouTube字幕・X/Instagram動画理解結果）に既に具体的な手順・数値・固有名詞（ツール名・価格・作業ステップ・数量など）が含まれている場合は、それを**最優先で一言に反映する**。タイトルの言い換えや「〜を解説」「〜が話題」のような**宣伝文句レベルの一般化に縮退させない**——取得済みの一次情報（具体的なTips・実装内容・数字）があるなら、その中身そのものを書く（例：「7つのTipsを紹介」ではなく実際のTipの内容を、「アプリを開発」ではなく決済実装・ストア配信など実務の核心を書く）。**文末表現は多様にする**：「〜と紹介」等の同じ結び方を2件連続で使わない。体言止め・数字の言い切り・「〜が判明」等を使い分ける。
4.2. **取得の生の事実の記録（スクリプトが自動で行う。エージェントは環境変数を1つ設定するだけ）**：`fetch_content.py` は取得のたびに `facts`（`fetched_at` / `route` / `ok` / `http_status` / `depth` / `missing` / `text_chars` / `text_sha1` / `body_chars` / `desc_chars` / `body_truncated` / `image_count` / `has_video` / `video_understood` / `elapsed_ms` / `reason` / `fetcher_version`）を `fetch_facts/<日付>.json` へ自動で追記する。**エージェントがこのファイルを手で書く必要はない。**
   - ⚠️ **手順4のすべての `fetch_content.py` 呼び出しで `FACTS_DATE=$TARGET` を環境変数として渡すこと。** これを忘れると実行日（＝TARGETの翌日）の名前でファイルが作られ、手順8で push するファイル名と食い違う。
     - グループA: `FACTS_DATE=$TARGET timeout 480 python3 fetch_content.py <単一URL>`
     - グループB: `FACTS_DATE=$TARGET python3 fetch_content.py <url1> <url2> ...`
   - ⚠️ **なぜ記録するのか**：`depth` は導出ラベルであり、実装の都合で嘘をつくことがある（2026-08-23の監査で、web記事14件が全て `depth="full"` を自称しながら本文の完全取得は0件だったと実測）。**導出値を信じるのではなく、後からいくらでも再計算できる不変の事実を残す**。これが無いと「修正して改善したのか」「特定の媒体だけ失敗しているのか」「失敗が一過性か構造的か」を後から一切測れない。
   - 手順3で当日0件と判定した場合は、そもそも取得が走らないのでファイルもできない。
   - ⚠️ **なぜ必要か**：`depth` は導出ラベルであり、実装の都合で嘘をつくことがある（2026-08-23の監査で、web記事14件が全て `depth="full"` を自称しながら本文の完全取得は0件だったと実測）。**導出値を信じるのではなく、後からいくらでも再計算できる不変の事実を残す**。これが無いと「修正して改善したのか」「特定の媒体だけ失敗しているのか」「失敗が一過性か構造的か」を後から一切測れない。
   - `captures.json` は従来通り push しない（下記手順8）。このファイルは1日ぶんだけなので小さく、文脈を膨らませずに push できる。
   - 手順3で当日0件と判定した場合は、このファイルを作らない。
4.5. **エンリッチ（関連記事の新規取得・実行必須）**：その日の保存を一段深掘りする。**テーマを1つ以上選んだら、選んだ全テーマについて必ず実際に `WebSearch` を呼び出すこと**。「取れなさそうだから」「時間が惜しいから」といった予測だけで `WebSearch` 自体を呼ばずにスキップすることは禁止する（この手順は過去9日間一度も実行されず「深掘り」節が0件だった実績があるため、明確に禁止する）。**完走優先の意味は「エラーで手順全体を止めない」ことであり、「この手順そのものを省略してよい」という意味ではない**。（手順3で当日0件と判定し手順6へ飛んだ場合のみ、この手順4.5を実行しない）
   - **時間予算によるスコープ縮小（正当なスキップ理由）**：手順4のグループA処理（動画理解の個別処理、1件最大480秒×最大20件）で複数件を処理した日は、既にクラウド実行環境のタイムアウトに対して時間を消費している。この場合、手順4.5で実際に `WebSearch` を呼び出すテーマ数は**最も強い1テーマのみ**に絞ってよい（グループA該当が0〜1件で時間消費が軽微だった日は、通常通り下記の最大2〜3テーマとしてよい）。これは手順8のGit Push到達（その日の生成が0件になることを防ぐ）を優先するための正当な理由であり、「取れなさそうだから」等の予測によるスキップとは区別される。
   - **テーマ抽出**：手順3で抽出した当日レコードの `title` / `note`（ユーザーコメント）/ cluster・hub から、その日を貫く**共通テーマ**（複数レコードに跨る関心）または**強い単発の興味**（感情・意図が明確な note）を**最大2〜3つ**選ぶ。当日が少件数で1つしか立たなければ1つでよい。
   - **新規記事の取得（必須実行）**：選んだテーマそれぞれについて、**最低1回は `WebSearch` を実際に呼び出す**（保存済みURLとは別の新しい記事を1〜2本探す：最新動向・背景解説・一次情報など、そのテーマの理解を深めるもの）。ヒットしたURLを `WebFetch` で開いて本文を読み、**そのテーマに対して何が言えるか（視点・学び）を日本語1〜2文**にまとめる。
   - **スキップが許されるのは、実際に呼び出した結果としてのみ**：①`WebSearch` を呼び出したがヒット0件だった ②`WebFetch` を呼び出したが失敗・タイムアウト・ペイウォール等で本文を取得できなかった。**この2つ以外の理由（呼び出しコストの節約・時間短縮・「どうせ取れないだろう」という予測判断）で `WebSearch`/`WebFetch` 自体を呼ばずに済ませることは禁止**。
   - **制約**：実在の検索結果・実際に取得できた本文のみ使う。**`WebSearch` が返したURLをそのまま使い、URLを変形・補完・推測しない**（手順4と同じく、URL捏造は過去に404を量産した事故あり）。`WebFetch` で実際に開けたURLだけを出力する。**全テーマで `WebSearch` を実際に呼んだ上で**それでも1本も本文が取れなければ、その場合に限り手順5の「深掘り」節ごと省略する。
   - 出力先は手順5の振り返りHTML内「深掘り」節のみ（**Vaultには書かない**。手順7.5・8.5で行うVaultリポへのブックマークノート書き込みとは別経路で、この4.5の深掘り内容自体はVaultに書かない、という原則をここでは維持する）。
5. `reviews/<TARGET>.html` を生成（**下記テンプレート厳守**）。日本語で書く（英語の本文・キャプションは日本語へ要約・翻訳。固有名詞・ハンドルは原文可）。
   - **スタイル**：以下の `<style>` ブロックをそのまま使う（CSS変数・ダーク対応込み）。`<title><TARGET> の振り返り</title>`。
     ```html
     <style>
       :root {
         --bg: #ffffff; --bg-sec: #f5f5f7; --panel: #ffffff;
         --text: #1d1d1f; --sub: #6e6e73; --ter: #aeaeb2;
         --border: rgba(0,0,0,0.10); --border-s: rgba(0,0,0,0.20);
         --chip: #f5f5f7; --accent: #0071e3;
       }
       @media (prefers-color-scheme: dark) {
         :root {
           --bg: #000000; --bg-sec: #1c1c1e; --panel: #2c2c2e;
           --text: #f5f5f7; --sub: #98989d; --ter: #636366;
           --border: rgba(255,255,255,0.10); --border-s: rgba(255,255,255,0.20);
           --chip: #1c1c1e; --accent: #0a84ff;
         }
       }
       * { box-sizing: border-box; }
       html, body { margin: 0; background: var(--bg); color: var(--text); line-height: 1.6;
         font-family: -apple-system, "SF Pro Text", "Hiragino Sans", system-ui, sans-serif;
         -webkit-font-smoothing: antialiased; font-size: 15px; }
       .wrap { max-width: 740px; margin: 0 auto; padding: 32px 16px 64px; }
       a.back { color: var(--accent); text-decoration: none; font-size: 13px; }
       a.back:hover { text-decoration: underline; }
       h1 { font-size: 24px; margin: 14px 0 2px; font-weight: 600; letter-spacing: -.01em; }
       .meta { color: var(--sub); font-size: 13px; margin-bottom: 24px; }
       h2 { font-size: 13px; font-weight: 500; letter-spacing: .05em; text-transform: uppercase; color: var(--ter); margin: 32px 0 12px; }

       .summary { background: var(--bg-sec); border: 0.5px solid var(--border); border-radius: 14px; padding: 18px 18px 14px; }
       .summary h2 { margin-top: 0; }
       .summary p { margin: 0 0 12px; }
       .summary .pts { list-style: none; padding: 0; margin: 0; display: grid; gap: 6px; }
       .summary .pts li { font-size: 13.5px; padding-left: 18px; position: relative; color: var(--text); }
       .summary .pts li::before { content: "→"; position: absolute; left: 0; color: var(--accent); }
       .q { font-size: 13px; color: var(--sub); margin-top: 12px; }
       .q b { color: var(--text); }

       .notes { background: var(--bg-sec); border: 0.5px solid var(--border); border-radius: 14px; padding: 6px 18px 14px; }
       .ntag { display: inline-block; font-size: 11px; font-weight: 600; letter-spacing: .05em; text-transform: uppercase;
         color: var(--accent); border-bottom: 1.5px solid var(--accent); padding-bottom: 1px; margin: 16px 0 8px; }
       .ilist, .qlist { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
       .ilist li, .qlist li { font-size: 13.5px; padding-left: 18px; position: relative; }
       .ilist li::before { content: "→"; position: absolute; left: 0; color: var(--accent); }
       .qlist li::before { content: "?"; position: absolute; left: 2px; color: var(--accent); font-weight: 700; }
       .ilist b, .qlist b { color: var(--text); }
       .ilist span, .qlist span { color: var(--sub); }
       .ilist a, .qlist a { color: inherit; text-decoration: none; border-bottom: 0.5px solid var(--border); }
       .ilist a:hover, .qlist a:hover { color: var(--accent); border-bottom-color: var(--accent); }

       .cards { display: grid; gap: 12px; }
       .vcard { display: flex; align-items: center; gap: 8px; background: var(--bg-sec);
         border: 0.5px solid var(--border); border-radius: 12px; overflow: hidden; transition: border-color .15s, transform .15s; }
       .vcard:hover { border-color: var(--border-s); }
       .vlink { display: flex; gap: 14px; text-decoration: none; color: inherit; flex: 1; min-width: 0; transition: transform .15s; }
       .vcard:hover .vlink { transform: translateY(-1px); }
       .thumb { flex: none; width: 104px; min-height: 88px; background: var(--chip); position: relative;
         display: flex; align-items: center; justify-content: center; }
       .thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
       .thumb .ph { font-size: 11px; color: var(--ter); letter-spacing: .03em; }
       .vbody { padding: 12px 14px 12px 0; min-width: 0; }
       .vtitle { font-weight: 500; font-size: 14.5px; margin-bottom: 3px; }
       .vdesc { color: var(--sub); font-size: 12.5px; }
       .deepdive { flex: none; margin-right: 10px; background: none; border: 0.5px solid var(--border);
         border-radius: 8px; padding: 6px 10px; font-size: 11px; color: var(--sub); cursor: pointer; white-space: nowrap;
         font-family: inherit; }
       .deepdive:hover { border-color: var(--accent); color: var(--accent); }
       @media (max-width: 460px) {
         .vlink { flex-direction: column; }
         .thumb { width: 100%; height: 150px; }
         .vbody { padding: 0 14px 14px; }
         .deepdive { display: none; }
       }
       footer { color: var(--ter); font-size: 11px; margin-top: 40px; opacity: .8; }
     </style>
     ```
   - **本文構造**（このタグ・class 名を厳守。`_build_feed.py` が regex で抽出するため）：
     ```html
     <div class="wrap">
       <a class="back" href="../index.html">← 戻る</a>
       <h1><TARGET> の振り返り</h1>
       <div class="meta">その日の主役を1行（件数を含めてよい）</div>

       <section class="summary">
         <h2>まとめ</h2>
         <p>プローズのまとめ（1〜2段落）。</p>
         <ul class="pts">
           <li><b>見出し</b>：一言</li>
         </ul>
         <div class="q"><b>次に考える問い：</b>…</div>
       </section>

       <!-- note(ユーザーの一言)に意図/好奇心がある時だけ。無ければ節ごと省略 -->
       <h2>やりたい・気になったこと</h2>
       <section class="notes">
         <div class="ntag">やりたい</div>
         <ul class="ilist">
           <li><a href="URL" target="_blank" rel="noopener"><b>やりたいこと</b></a> <span>→ 次アクション</span></li>
         </ul>
         <div class="ntag">気になった</div>
         <ul class="qlist">
           <li><a href="URL" target="_blank" rel="noopener"><b>問い？</b></a> <span>調べた答え（取れなければ「未確認→次ラン」）</span></li>
         </ul>
       </section>

       <!-- 手順4.5でテーマの関連記事が取れた時だけ。取れなければ節ごと省略 -->
       <h2>🔎 深掘り（関連記事）</h2>
       <section class="notes">
         <div class="ntag">テーマ名</div>
         <ul class="ilist">
           <li><a href="記事URL" target="_blank" rel="noopener"><b>記事タイトル</b></a> <span>→ そのテーマに対する視点・学び（日本語1〜2文）</span></li>
         </ul>
       </section>

       <!-- カテゴリ別カード。内容に応じて見出しを付けてグルーピング -->
       <h2>🎨 アート・文化（N件）</h2>
       <div class="cards">
         <div class="vcard">
           <a class="vlink" href="URL" target="_blank" rel="noopener">
             <div class="thumb"><img src="COVER" onerror="this.remove()"><span class="ph">媒体名</span></div>
             <div class="vbody"><div class="vtitle">日本語タイトル</div><div class="vdesc">一言説明</div></div>
           </a>
           <button class="deepdive" onclick="openChat(this)" data-url="URL" data-title="日本語タイトル" data-rid="RAINDROP_ID">💬 AIと話す</button>
         </div>
       </div>

       <footer>毎朝の同期で自動生成 ｜ katut-brain</footer>
     </div>
     <script>
     function openChat(btn) {
       var url = btn.getAttribute('data-url');
       var title = btn.getAttribute('data-title');
       var rid = btn.getAttribute('data-rid');
       var vault = 'C:\\Users\\katut\\Documents\\ObsidianVault';
       var workdir = 'C:\\Users\\katut';
       var bookmarksDir = vault + '\\Explore\\bookmarks\\';
       var prompt = '次のブックマークについて一緒に調べて、学びをVaultに書き残して。\n' +
         'URL: ' + url + '\n' + 'タイトル: ' + title + '\n' + 'raindrop_id: ' + rid + '\n\n' +
         bookmarksDir + ' で raindrop_id: ' + rid + ' を grep で探す。' +
         'あればそのノートに今回の深掘り内容を追記する。' +
         'なければ rd-' + rid + '-<内容を表す英語kebab-caseスラッグ>.md を同フォルダに新規作成する' +
         '（frontmatter: date / tags: [type/bookmark] / raindrop_id / source）。';
       var link = 'claude://code/new?folder=' + encodeURIComponent(workdir) + '&q=' + encodeURIComponent(prompt);
       location.href = link;
     }
     </script>
     ```
   - **各カードの`.vcard`は`<div>`＋内側の`.vlink`（サムネ・タイトル・説明への外部リンク、従来と同じ見た目）＋`.deepdive`ボタン（AIチャット起動）という構造**。ボタンの`data-url`・`data-title`には、そのカードの`.vlink`に使ったのと同じ`URL`・`日本語タイトル`をそのまま入れる（別の値を作らない・捏造しない）。`data-rid`には、そのカードに対応する `captures.json` レコードの `rid`（Raindropの内部ID）を引用符なしの数値として（属性値としては文字列だが、加工・推測せず逐語）そのまま入れる。`openChat()`関数は`.wrap`の外、ページ末尾に一度だけ書く。
   - ⚠️ `data-title`（および`data-url`）はHTML属性値なので、タイトルに`"`（ダブルクォート）が含まれる場合は`&quot;`にエスケープしてから埋め込む（属性が途中で終わってHTMLが壊れるのを防ぐため）。`data-rid`は数値のみなのでエスケープ不要。
   - ⚠️ **`.deepdive`ボタンのプロンプトは、`data-url`・`data-title`・`data-rid`の3つだけから機械的に組み立てる固定テンプレート**。手順4.5で生成した深掘り記事の内容やまとめ文、手順7.5のノート生成で書いた内容理解など、AIが自由に書いた文章は絶対に混ぜない（未知の第三者サイトを読んだ内容が、公開ページ上のこのボタンの中身に混入する経路を断つため）。
   - cover が無いカードは `<div class="thumb"><span class="ph">媒体名</span></div>`（img 無し）。
   - **「🔎 深掘り（関連記事）」節（手順4.5の出力）と、上記の「💬 AIと話す」ボタンは別物**。前者は既存の `.notes` / `.ntag` / `.ilist` クラスを流用する（新しい CSS は追加しない）。テーマごとに `.ntag` を1つ、その下に `.ilist` で記事1〜2本を並べる。手順4.5で1テーマも取れなければ `<h2>🔎 深掘り…</h2>` ごと出力しない。
   - **まとめの書き方**：その日の全 capture の `note`（ユーザーコメント）をまず全件読んでから書く。構成は①通奏低音（保存全体を貫く問い意識）→②ドメインをまたぐ接続（AI×アート、建築×技術など）→③この日に活性化した軸の順。箇条書きは「〜だった」で終わらず「〜という含意がある」「〜への問いを立てる」まで踏む。
     - **通奏低音を探す**：一見バラバラな保存に流れる共通の問い意識を1つ見つけ、1段落目の軸にする（分野をまたいでも成立する問いが理想）
     - **ユーザー文脈で読む**：ユーザーは建築学生・curator-maker志向（知る/紹介/深掘りを好む）で、興味軸は建築・アート・デザイン・写真・AI活用。今日の保存が「作りたい」軸か「知る/紹介したい」軸か、どちらが活性化したかを一言入れる
     - **問いは今日の具体に根ざす**：「AIと建築の関係は？」のような汎用的な問いでなく、この日の特定の保存から自然に浮かんだ問いを書く
     - **問いは1つの焦点に絞る**：複数の論点を「そして」等で1文に詰め込まず、その日いちばん鋭い1つだけを選ぶ（良い例＝単一のトレードオフ・矛盾を掘る問い。悪い例＝別々の関心を接続詞で繋いだ複合的な問い）。書き終えたら接続詞で複数の問いを繋いでいないか自己チェックする。
     - **noteをまとめに反映する基準**：全noteを読んだうえで、以下に該当するものだけまとめに言及する
       - 共通（2件以上）：同日に2件以上のnoteで同じ関心・テーマが現れるもの（「参考」単体などの弱いラベルは除く）
       - 強い単発：感情・意図・自己同定が明確なもの（「！」「めちゃ」「やりたい」「これだ」「大事」「偉大」「考え続けたい」など）
     - **継続する関心を検出する**：`captures.json` の過去7日分（TARGET以前）を参照し、今日のまとめで出てきたテーマが先週も繰り返されているか確認する。2日以上連続または週内3件以上なら「この関心は今週N日続いている」と まとめに一言加える。単発の流行追いと、継続的に自分の中で育っている関心を区別する手がかりになる
   - `note` の **表示ルール（やりたい・気になったこと 節のみ適用）**：意図（やりたい/ありかも/使いこなしたい）→「やりたい」、好奇心（どうやって/なんだろう/のかな）→「気になった」に出す。それ以外の note は節に出さない（ただし まとめ では全件使う）。
6. **トップ `index.html` には一切触らない**（2026-08-31 変更）。`_build_feed.py` を実行する必要も、`index.html` を読む必要も、push する必要もない。
   - 理由: `index.html` は約122KB まで育っており、`push_files` は**ファイルの中身を文字列で渡す仕様**なので、エージェントがこれを丸ごと運ぼうとすると必ずチャンク分割・転記ミス・truncate を起こす。実際に 2026-08-28〜08-30 の3晩連続で公開ページが破損し、06-12〜08-14 の約2.5ヶ月分の日付ブロックが消えた（1晩あたり約10コミットの自己修復が走り、ラン時間も56〜74分に肥大した）。
   - 代わりに、**あなたが `reviews/<TARGET>.html` を push した時点で GitHub Actions（`.github/workflows/build-feed.yml`）が自動的に `_build_feed.py` を走らせ、`index.html` を再生成してコミットする**。あなたの仕事は「その日の `reviews/<TARGET>.html` を正しく作って push する」ところまで。
   - Actions 側には検証ゲートがあり、生成物が truncate・PLACEHOLDER混入・短すぎのいずれかならコミットせずに落ちる（＝壊れたものは公開されず、直前の正常な `index.html` が残る）。
7. **自己検証**（手順5で reviews を作った場合）：**`reviews/<TARGET>.html` そのもの**を読み返し、`_build_feed.py` が実際に抽出する要素（テーマ＝**`<div class="meta">`**、まとめ文＝`.summary p`、気づき＝`.notes` 内の `li`、カード＝`.vcard`）が入っているか確認する。※2026-08-31訂正: 旧版はテーマを `.theme` と書いていたが、`.theme` というクラスは reviews のテンプレートにも `_build_feed.py` にも存在せず、実際の抽出元は `<div class="meta">`。この誤りのせいで正しい出力を「テーマ欠落」と誤判定しうる状態だった。欠けていれば構造ズレなので `reviews/<TARGET>.html` を直す。
   - ⚠️ **`index.html` を見て確認しようとしないこと**（2026-08-31 変更）。`index.html` はあなたが push したあとに GitHub Actions が作るので、この時点ではまだ更新されていない。**派生物ではなく材料の側を検証する**のが正しい。**加えて、手順4.5でテーマを1つ以上選んだのに「深掘り」節が無い場合は、実際に `WebSearch` を呼び出したかを振り返る**。呼び出していなければ今からでも手順4.5を実行してから `reviews/<TARGET>.html` に反映し、この手順7の確認をやり直す（手順6は index.html を触らない手順なのでやり直す対象が無い）。
7.5. **ノート生成（1ブックマーク1ノート、Vaultリポへ）**（手順5で reviews を作った場合のみ実行。0件の日はスキップして手順8へ）：その日保存した各ブックマークを、Vaultリポの `Explore/bookmarks/` に1件1ノートとして書き残す。
   - **対象**：手順3で抽出した当日レコード全件（グループA/Bを問わず、手順4で取得済みの内容を使う。新たに取得し直さない）。
   - **重複チェック（作成前に必須）**：各レコードの `rid`（Raindropの内部ID。ノートでは `raindrop_id` と呼ぶ）について、Vaultリポの `Explore/bookmarks/` 配下を `grep -rl "raindrop_id: <rid>" Explore/bookmarks/` 等で検索する。ヒットする（＝既にそのブックマークのノートが存在する）場合は、そのレコードのノート作成をスキップする（追記も上書きもしない）。
   - **安全制約（厳守）**：Vaultリポでは `Explore/bookmarks/` 配下への**新規ファイル作成のみ**を行う。既存ファイルの編集・削除・リネームは一切行わない。
   - 新規作成と決まったレコードごとに、以下の1ファイルを作る：
     - **ファイル名**：`rd-<rid>-<slug>.md`。`slug` はそのブックマークの内容を表す英語kebab-caseスラッグ3〜5語（タイトル・summaryから作る。既存ファイル名と衝突する場合のみ末尾に連番を足す）。
     - **frontmatter**（Vault規約準拠。必須4項目＋related）：
       ```
       ---
       date: YYYY-MM-DD
       tags: [type/bookmark, <ドメインタグ>]
       raindrop_id: <rid>
       source: <そのレコードの source URL>
       related: []
       ---
       ```
       - `date` はそのレコードの `date`（＝TARGET）。
       - `<ドメインタグ>` は、そのレコードの `cluster`・Raindrop側 `tags` から代表的な1語を選び、英語kebab-case（Vaultのフラットタグ規約）で書く。日本語タグしかない場合は意味に沿って英訳する。
       - `raindrop_id` は引用符なしの数値でそのまま出力する（文字列化しない・加工しない）。
       - `related` は関連ノートが無ければ必ず `related: []`（YAMLリスト形式を崩さない）。
     - **本文**：手順4で取得済みの内容（記事本文／字幕／動画理解結果＝`text`、および手順5でカード用に作った `.vdesc` の一言）を踏まえた、そのブックマークの実内容理解を日本語3〜6文程度でまとめる。既に得ている理解の再利用であり、新たに `WebSearch`/`WebFetch` で調べ直さない。
     - **末尾に `## 関連ノート` セクションを置く**（Vault規約）。中身は空でよい（義務的にリンクを作らない）。
   - **push前のfrontmatterスキーマ検証（必須）**：この手順で新規作成した各ノートについて、Pythonで frontmatter（`---`〜`---`の間）をYAMLとしてパースし、`date`（文字列・YYYY-MM-DD形式）・`tags`（リストで `type/bookmark` を含む）・`raindrop_id`（数値）・`source`（文字列・`http` で始まる）の4キーの存在と型を確認する。1つでも不合格ならそのノートは push 対象から除外する（ファイル自体はローカルクローンに残ってよい＝次回ランは新規cloneのため無害）。
   - **検証結果の記録**：検証に落ちたノートが1件以上あった場合、`reviews/<TARGET>.html` の `</footer>` 直前に、`_build_feed.py` の抽出対象（`.meta`/`.summary p`/`.notes` 内`li`）と衝突しない独自クラスで1行追記する：`<p class="notegen-warn">⚠️ ノート生成: 検証失敗 N件（rid: 1234, 5678 ...）</p>`。全件合格した場合はこの追記をしない。この追記は手順8で push する `reviews/<TARGET>.html` の内容に含める。
8. **公開（GitHubへ反映）**：対象は `katut-brain/katut-brain.github.io` リポ（Vaultリポではない）。
   - ⚠️ **生の `git push` は使わない**。クラウドのルーティンでは git proxy が **403** を返す（権限でなく経路の制約）。**GitHubの組み込み push ツール（`push_files`）で `main` に直接コミットする**。
   - 押すファイルは **`reviews/<TARGET>.html` と、手順4.2で作った `fetch_facts/<TARGET>.json` の2つだけ**（手順5で作った場合）。これらを **1回の `push_files` 呼び出し**で `main` にコミット（メッセージ `update: <TARGET>`）。
   - 🚫 **`index.html` は push しない**（2026-08-31 変更・手順6を参照）。GitHub Actions が自動で再生成するので、あなたが触ると壊す側にしかならない。`index.html` を push 対象に入れたくなったら、それは手順6を読み飛ばしている。
   - **`captures.json` / `data.js` は push しない**：captures.json のRaindrop取り込みは冪等（`_build_graph.py` が既存レコードも毎回更新するので翌ランで再現される。2026-08-23に冪等化済み）、data.js は退役ファイル。大きいファイルを読むと文脈が膨らみ自動圧縮で迷子になるため、**触らない・読み込まない**。
   - ただし `fetch_facts/<TARGET>.json` は1日ぶんだけの小さなファイルなので push する。**これを落とすと取得深度の履歴が残らず、改善したかどうかを永久に測れなくなる**（手順4.2の⚠️を参照）。
   - ⚠️ **push_files に渡す前に、送る中身が本物か必ず確認する**（過去に中身が丸ごと `PLACEHOLDER` という仮文字列に置き換わって公開サイトが一時的に壊れた事故が2回発生している）。`push_files` の引数に入れる `reviews/<TARGET>.html` の内容は、**手順5で実際に生成・確認したファイルの中身をそのまま使う**（要約・省略・仮置きの文字列で代用しない）。呼び出し直前に、渡す文字列が `<!doctype html>` で始まり `</html>` で終わっているか、`PLACEHOLDER` という語を含んでいないかを目視確認してから送信する。
   - **push後、リポジトリ上の内容を読み返して検証する**（自己申告で「push成功」と判断しない）。**`reviews/<TARGET>.html` のみ**を GitHub から取得し、上と同じ確認（`<!doctype html>`で始まる・`PLACEHOLDER`を含まない・分量が妥当）を行う。異常が見つかったら、正しい内容で即座に再度 `push_files` を実行して直す。
   - `index.html` の出来ばえは確認しなくてよい（Actions 側の検証ゲートが担当する）。**`index.html` を GitHub から読みに行かないこと** — 122KB を読むと文脈が膨らんで自動圧縮で迷子になる。
   - `push_files` が一時失敗しても、生 `git push` には**戻らない**（403ループ防止）。1〜2回だけ `push_files` を試し、ダメなら諦めて翌ランに回す。
   - reviews を作らなかった日（0件）は push しない。
8.5. **公開（Vaultリポへ・ブックマークノート）**（手順7.5でノートを新規作成した場合のみ実行）：対象は `katut-brain/obsidian-vault` リポ（手順8の `katut-brain.github.io` とは別リポ）。
   - 押すファイルは、手順7.5でスキーマ検証に**合格**し新規作成した `Explore/bookmarks/rd-*.md` のみ（検証落ちのファイル・既存ファイルは含めない）。
   - 対象リポジトリ `katut-brain/obsidian-vault` の `main` ブランチへ、GitHubの組み込み push ツール（`push_files`）で直接コミットする（手順8と同じ理由で生の `git push` は使わない）。コミットメッセージは `bookmark notes: YYYY-MM-DD (N件)` 形式（`YYYY-MM-DD` はTARGET、`N` は今回push するノート件数）。
   - 手順8と同様、push_files に渡す前に中身が本物か目視確認する（`PLACEHOLDER` 等の仮文字列でないか、frontmatterが崩れていないか）。push後はリポジトリから読み返して検証する。
   - push 対象ノートが0件（新規0件・全件重複スキップ・全件検証落ちのいずれか）の場合は、このpushを行わない。
   - `push_files` が一時失敗しても生 `git push` には戻らない（403ループ防止）。1〜2回だけ試し、ダメなら諦めて翌ランに回す（取りこぼしたブックマークのノートは翌晩以降の手順7.5で改めて対象になる＝重複チェックにより既存ノートは壊されない）。

## 制約
- 完全無人。承認・確認を求めない。
- 各手順は失敗しても全体を止めず、できたところまでで push（**完走優先**）。
- **本文取得（手順4）の失敗は1回だけ再試行する**。旧ルールは「リトライしない（取りこぼしは翌晩で拾う）」だったが、**翌晩に拾われないことが実測で判明したため撤回した**（手順7.5の重複チェックで既存ノートがある限りスキップされ続けるので、一度「取得できず」と書かれた件は永久に再取得されない）。エンリッチ（手順4.5）や push の再試行回数は従来通り増やさない。
- 手順7.5・8.5（ブックマークノート生成・Vaultリポへのpush）は、手順1〜8（review本体の生成・`katut-brain.github.io`への公開）とは独立した処理。ノート生成側でエラー・失敗が起きても、review本体の生成・公開（手順1〜8）を止めない。逆に手順1〜8のどこかで問題があっても、既に作成済みのノートのpush（手順8.5）は可能な範囲で試みてよい。
- URL・cover は `captures.json` / `fetch_content` の実値を逐語使用。**捏造しない**。エンリッチ（手順4.5）で出す記事URLも同様に、`WebSearch` が返した実URL／`WebFetch` で開けた実URLだけを使い、変形・補完・推測しない。
- 英語は日本語へ。要約は「ぼんやり」させず、何が言えるか・何が信号かを具体に。

## 成功条件
- `date==TARGET` が1件以上 → `reviews/<TARGET>.html` を生成し、`fetch_facts/<TARGET>.json` とあわせて `main` に push。**`index.html` は push しない**（GitHub Actions が自動再生成する・手順6）。
- 0件 → reviews は作らず、何もせず正常終了する（`index.html` は触らない）。
- `date==TARGET` が1件以上あった日は、重複チェックでスキップされなかった各レコードについて、スキーマ検証に合格したノートが Vaultリポ `Explore/bookmarks/` に作成され `main` へ push される（1件も新規作成対象が無ければ手順8.5のpushは行わない＝これも正常終了）。
