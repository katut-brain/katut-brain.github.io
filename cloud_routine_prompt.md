# katut-brain 毎朝の振り返り 自動生成（クラウド Routine プロンプト）

> **このファイルが手順の正本。** Routine の Instructions 欄には短いブートストラップだけを置き、
> 実行時にこのファイルを読ませる方式に変更した（2026-08-23）。
> 手順を直したいときは**このファイルを編集して push するだけ**でよい。Instructions は触らない。
> 置き場所: `katut-brain/katut-brain.github.io` リポジトリ直下 `cloud_routine_prompt.md`。
> 以前はこの本文を Instructions 欄へ丸ごと貼っていたが、45KBを毎回貼り直す運用は
> 転記ミスの温床で、実際に本文が `PLACEHOLDER` に化けた事故が2回起きている。
> なお `projects/tools/scripts/cloud_routine_prompt.md`（リポ外・ローカル）は
> 同期されないため正本ではない。参照しないこと。
>
> **変更履歴**: 2026-09-22: 振り返りの対象を「`captures.json` の `date == TARGET`」から
> 「まだどの `reviews/*.html` にも載っていない保存すべて（`date >= 2026-06-14`、過去日も含む・
> 上限なし）」へ変更した。理由: 深夜0〜4時の保存が1日遅れて翌々日扱いになる／ランが落ちた夜の
> 保存が永久に載らない、という取りこぼしが実測されていたため。選定は `select_targets.py` が行う
> （手順3参照）。

---

あなたは「毎朝の振り返り」を完全無人で生成・公開するエージェントです。人に確認を求めず、最後まで自分で完走してください。

## 前提・環境
- リポジトリ `katut-brain/katut-brain.github.io` が clone 済み（default branch = `main`）。作業はこのリポ直下。
- 環境変数 `RAINDROP_TOKEN`（Raindrop API トークン）が設定済み。
- 環境変数 `TZ=Asia/Tokyo`（日付は日本時間で計算）。
- 環境変数 `GEMINI_API_KEY`（Gemini API無料枠キー、X動画・Instagram Reel動画の音声+映像理解用）が設定されていれば使う。**未設定でも全体は止まらない**（`fetch_content.py`が自動でタイトルのみにフォールバックする graceful degradation設計）。
- リポ直下に `_build_graph.py` / `_build_feed.py` / `fetch_content.py` / `requirements.txt` / `captures.json` / `reviews/` がある。
- ネットワークは Raindrop API・X・Instagram・YouTube・各ニュースサイトへ到達できる。
- リポジトリ `katut-brain/obsidian-vault`（個人Vault、private）も同じワークスペースに clone 済みの前提（Routine設定でのリポジトリ追加はユーザー側で別途実施済み）。ディレクトリ名がワークスペース内で異なる場合は `Explore/bookmarks/` を含むリポをVaultリポとして特定する。以下「Vaultリポ」はこのリポを指し、常に上記 `katut-brain/katut-brain.github.io` とは別リポとして扱う（作業ディレクトリ・push先を混同しない）。

## 手順
1. **対象日**＝日本時間の「昨日」。`TARGET=${TARGET_OVERRIDE:-$(TZ=Asia/Tokyo date -d yesterday +%F)}`。
   - 環境変数 `TARGET_OVERRIDE`（`YYYY-MM-DD`）が設定されていればその日を対象にする。公開できなかった日を後から作り直す一回きりのランで使う（2026-09-14 追加）。**形式が `YYYY-MM-DD` でなければ無視して「昨日」を使う**。通常の毎晩のランでは設定しない。
   - あわせて**所要時間の計測を開始する**：`python3 run_timing.py start --target $TARGET`。
     出力の **`RUN_TIMING_RUN_ID: <8桁の16進>` を控えておき、以降の計測コマンド全てに `--run-id <8桁>` で渡す**
     （この値は各コマンドの引数として毎回書く。控え損ねた・取り違えた場合は計測が `status=incomplete` になるだけで、
     振り返り本体には影響しない＝安全側に倒れる）。
     以降、**手順2 / 2.5 / 3 / 4 / 4.5 / 5 / 7 の開始時**に `python3 run_timing.py mark <step名> --run-id <8桁>` を
     1行ずつ実行する（`step2` `step2_5` `step3` `step4` `step4_5` `step5` `step7`）。手順7でこの記録を reviews に焼き込む。
     **区間の所要時間は隣り合う mark の差**なので、1つでも打ち忘れるとその区間は測れない（`status=incomplete` になる）。
   - ⚠️ 状態ファイル `.run_timing.json` は作業用で、push しない（`.gitignore` 済み）。
     開始時刻と各手順の時刻は、シェル変数ではなくこのファイルに置く（手順をまたぐたびに
     別のシェル呼び出しになるため、変数で持ち回るより確実）。`$TARGET` のように
     コマンド行へ毎回書き下すものは従来どおりでよい。
   - **計測が失敗しても手順は止めない**。`run_timing.py` はどんな例外でも exit 0 で終わる設計なので、
     このコマンドがエラーを出しても次へ進んでよい（振り返り本体には影響しない）。
1.5. **依存インストール**：`pip install --quiet -r requirements.txt`（YouTube字幕取得用 `youtube-transcript-api`、X動画・Instagram Reel動画理解用 `google-genai`）。失敗しても止めない（`fetch_content.py` はこれらのパッケージが無くても他の取得は正常動作する graceful degradation設計。ただしYouTube動画は字幕なし・X動画/Instagram Reel動画は音声/映像理解なしのタイトルのみに落ちる）。
2. （先に `python3 run_timing.py mark step2 --run-id <8桁>`）`python3 _build_graph.py` を実行。Raindrop の新規を `captures.json` に取り込み、既存レコードも冪等に更新する（失敗してもログして続行）。
   - **出力の `IMPORT_STATUS:` 行を必ず読む**。`INCOMPLETE` だった場合は Raindrop を全件取得できておらず、**その日の振り返りが欠損しうる**。この場合は手順5の `reviews/<TARGET>.html` の `</footer>` 直前に `<p class="notegen-warn">⚠️ 取り込み不完全: Raindrop取得エラー N件。欠けている保存がある可能性あり</p>` を1行足して、欠損の可能性を残す（黙って完走しない）。import は冪等なので翌ランで自動的に回復する。
   - この行を見落として「正常に完走した」と扱わないこと。無人運用ではログを誰も読まないため、**成果物側に痕跡を残すことが唯一の検知手段**になる。
2.1. **日別台帳の更新**：
   ```bash
   python3 build_capture_index.py
   ```
   - `capture_index.json` を「既存の台帳 ∪ 今夜の `captures.json`」で更新する。日付と rid だけの小さなファイル（実測43日分で7.3KB）。**この場で単独で push する**（下記）。
   - **何のためにあるか**: 手順8は照合が通らなければ押さない。押さなかった日を**後から人が見つけられるようにするための記録**。`captures.json` は毎晩 Raindrop から作り直されるので、押せなかった日の保存が消えたり取り込みが不完全だったりすると証拠ごと消える。台帳は一度観測した rid を消さない。
   - **⚠️ これはキューではない。自動回収はしない**（2026-09-09 の裁定）。`UNPUBLISHED:` の行が出ても、その日を作り直しに行かないこと。対象日は常に「昨日」のまま。
   - 出力の `UNPUBLISHED:` と `INDEX:` の行はログにそのまま残す。
   - **作ったら、その場で台帳だけを押す**（差分があるときだけ）：
     ```bash
     if git status --porcelain -- capture_index.json | grep -q .; then PUSH_VIA_BRANCH_WAIT=0 bash push_via_branch.sh "index: <TARGET>" capture_index.json; fi
     ```
     - 台帳は main への取り込みを待たない（`PUSH_VIA_BRANCH_WAIT=0`）。`WRITE_COMMIT: <sha> ... branch=...` が出ればブランチまで届いており、取り込みは Actions がやる。
     - ⚠️ **手順8にまとめてはいけない**。台帳を reviews と同じコミットに入れると、**その夜の push が失敗したとき台帳も載らない** —— つまり「押せなかった日を後から調べる」という台帳の存在理由が、まさにその失敗時に効かなくなる。翌晩 Raindrop から保存が消えていれば、その日の rid はもうどこにも残らない。
     - 失敗しても手順を止めない（2回まで試して諦める）。`WRITE_COMMIT:` の行はログに残す。
     - 押す夜と押さない夜があるので、Actions は夜あたり1〜2回発火する。
   - 非ゼロで終わっても**手順を止めない**（台帳が壊れている等。その場合は台帳を書き換えないので `git status` に差分も出ず、手順8の push 対象にも入らない）。

2.5. **バックフィル（過去に取得できなかった rid 持ちレコードの再取得）**：
   （先に `python3 run_timing.py mark step2_5 --run-id <8桁>`）
   `FETCH_FACTS_DIR="$PWD/fetch_facts" FACTS_DATE=$TARGET timeout 1200 python3 backfill.py --target $TARGET --limit 5 --timeout 480 --max-total 1140` を実行する。
   - ⚠️ **`FETCH_FACTS_DIR="$PWD/fetch_facts"` を必ず前置する**。書き先をリポ直下の `fetch_facts/`（＝手順8(b)で
     `git status` を見る対象と同じディレクトリ）に固定するため。前置しないと `backfill.py`/`fetch_content.py` の
     既定値に依存することになり、実行環境しだいで書き先がリポ追跡対象からずれるリスクがある。
   - ⚠️ **この枠（`--limit 5` / `--max-total 1140` / 外側 `timeout 1200`）は 2026-09-23 に
     実測（`step2_5_s` 18〜22秒・`fetch_facts/runs/*.json` の `candidates`/`attempted`/`limit`/`timeout`）
     から決めた。**勝手に変えないこと。旧・棄却済みの「`--limit 5`（外側 `timeout 2500`）」との違いは、
     外側の時間上限が `1200` で止まる点——`--max-total 1140` により、480秒フルにかかる候補は最大2件までしか
     起動されず（起動条件「残り ≥ `--timeout`」）、`2500` 案のように単独で長時間を占有しない。
   - ⚠️ **Gemini 無料枠を先に使い切るリスク**: 手順2.5 のバックフィルは手順4より前に走るため、X動画・
     Instagram Reel 動画理解で Gemini API 枠を先に消費し、当日の通常取得（手順4）側の動画理解がタイトルのみへ
     フォールバックする可能性がある（`fetch_content.py` の graceful degradation で止まりはしないが、質は落ちる）。
     **枠が切れた夜は、`backfill.py` が1件だけ試して残りの候補を打ち切る**（下の `quota_stopped` を参照。
     2026-09-23 追加。無料枠は同一キーをプロセス内の全候補が共有するため、1件で `gemini_quota` が
     観測された時点で残りも同じ理由で失敗する可能性が高く、試すだけ無駄なうえ `--max-attempts` を
     早く消費して恒久的に候補から外れる副作用がある）。
   - ⚠️ **`budget_stopped` の見方**: `BACKFILL_STATUS:` 行の `budget_stopped` は「残り時間が `--timeout` 未満で
     起動を見送った候補数」。0より大きければ、その夜は `--max-total 1140` の枠を使い切って一部候補が翌晩へ
     繰り越されたことを示す（失敗ではない。試行回数は消費していないので候補のまま残る）。
   - ⚠️ **`quota_stopped` の見方**（2026-09-23 追加）: `BACKFILL_STATUS:` 行の `quota_stopped` は「直前の候補が
     `video_reason=gemini_quota` を返したため、起動せずに見送った候補数」。`budget_stopped` とは別のカウンタ
     （時間切れではなく Gemini 無料枠切れが理由）。0より大きい夜は「Gemini 無料枠が尽きたので1件で打ち切った」
     ことを示し、これも失敗ではない（試行回数は消費していないので候補のまま残る）。
   - 枠を決め直すときの算術（変えない前提）: 1件あたり最大 `--timeout` 秒・上限 `--limit` 件だが、
     **総時間の上限は `--max-total` で決まる**（既定 `limit × timeout + 30`。`backfill.py` は各候補の起動前に
     「残り < `--timeout`」なら起動せず打ち切り、残件数を `budget_stopped` に記録する）。外側のシェル `timeout` は
     `--max-total` に60秒の余裕を足した値を下回らないこと（親が外側より先に自分で終わらないと、子が孤児化して
     排他ロックの無い `fetch_facts/<日付>.json` に競合書込みする）。**この60秒ルールを守れば通常の計算では
     親が先に終わるが、厳密な保証ではない**（`backfill.py` 自身のコメントに明記のとおり、「残り ≥ `--timeout`」
     判定から実際の子起動までの微小な遅延や、`subprocess.run` の timeout 後の子プロセス回収時間までは
     縛れない。ベストエフォート）。
   - **`backfill.py` は候補抽出より前に `fetch_facts/runs/<TARGET>.json` へ証跡を書きにいく**（2026-09-18 追加。「起動すれば必ず書かれる」ではない——到達前に止まれば残らない。下の注記を読むこと）。
     起動直後に `status:"started"`、正常終了で `status:"completed"` ＋ `BACKFILL_STATUS` と同じ数字、
     例外終了で `status:"crashed"` に置き換わる（原子的置換）。**あなたがこのファイルを作ったり書き換えたりしない。**
     これは「このステップを実行したか」を成果物側から確かめるための材料（2026-09-15〜17 の3夜、
     候補が9件あるのに痕跡が1件も無く、未実行と実行後の無記録を区別できなかったため入れた）。
     ⚠️ **読み方を間違えないこと。証跡が示すのは「`backfill.py` が最初の証跡書き込みまで到達した」ことだけ**
     （2026-09-18 Codex 7周目 P1）。ファイルが無い理由は次の3つがありうる:
       1. コマンドを実行していない
       2. 実行したが、モジュールの import・引数解析など**最初の書き込みに到達する前**に
          外側 `timeout` や kill で止まった（この場合は `BACKFILL_EVIDENCE:` の行も出ない）
       3. 実行して到達したが書き込み自体に失敗した（この場合だけ
          `BACKFILL_EVIDENCE: write_failed` がログに出る）
     **ファイルが無いことだけで「実行していない」と断定しない。** 判定にはランのログを併せて見ること。
     逆向きの断定も**そのままでは成立しない**。新規cloneでも、同じ `<TARGET>` の証跡が
     過去のランで push されていれば main から降ってくるので、**今夜実行しなくてもファイルは在りうる**
     （2026-09-18 Codex 8周目 P1）。ファイルの存在が示すのは「**過去のどこかで**その TARGET の
     非 dry-run が最初の書き込みまで到達した」ことだけ。**今夜の実行と結び付けたいときは、
     `runs` 配列に**実行前には無かった `run` トークンが在るか**を見る**（上限20件に達している夜は古いエントリが落ちるので、配列の**件数**は増えない。件数ではなくトークンで見ること。2026-09-18 Codex 9周目 P2）
     （ランのログの `BACKFILL_STATUS:` と併せて判断する）。
     `--dry-run` のときは書かれない。
   - バックフィル結果の内訳は `BACKFILL_STATUS:` 行で確認する（candidates/attempted/improved/failed/stub_written等）。
     push の分岐は手順8(b)の `git status` だけで判定する（`BACKFILL_STATUS:` の内訳では判定しない）。
   - このコマンドは失敗しても（非ゼロ終了・timeoutによる強制終了含め）手順を止めない。`backfill.py` は内部で
     例外を握りつぶし exit 0 で終わる設計だが、シェルの `timeout` コマンド自体がプロセスを強制終了した場合は
     非ゼロで返ることがある。いずれの場合も次の手順3へそのまま進む。
   - このステップは `fetch_facts/<TARGET>.json` に直接追記する（手順4.2と同じ書き先・同じ形式）。
     手順4.2で同じURLを通常取得した場合、後勝ちでその日のレコードが上書きされる（内容は最新化され、
     `attempt_seq` が加算される。データが失われるわけではない）。
   - レコードを残せなかった候補（timeout・no_record・例外）には、backfill 自身が失敗レコード
     （`route: "backfill"`, `ok: false`）を書く。これが無いと同じURLが毎晩 `--limit` の枠を消費し続けるため。
   - スタブ書き込み・`backfill_rid` タグ付けも `fetch_facts/<TARGET>.json` へのファイル差分になるので、
     `attempted=0` の夜でも手順8(b)の push 判定（`git status --porcelain` で差分あり）に該当し push される。
   - 対象が0件（`candidates=0`）でも正常終了として扱う。
   - ⚠️ このコマンド形（`timeout` コマンド・`$TARGET` の `date -d` 計算）は Routine の Linux 実行環境専用。
     Windows ローカルで動作確認したいときは `python3 backfill.py --dry-run`（対象抽出のみ確認、実取得はしない）
     を直接叩く。
   - ⚠️ **既知の限界**: 対象抽出は `ledger.py` の `reason_kind` 判定に依存する。`fail_reason`
     の導入（2026-09-03）で `x_article_direct_link` / `x_tweet_id_unparsable` / `ssrf_rejected` /
     `unsupported_scheme` / `bad_host` / HTTP 404・410 は permanent に分類されるようになった
     （列挙の正本は `ledger.py` の `_FAIL_REASON_KIND`）。2026-09-05 に一般Web・Threads・
     YouTube でも HTTP ステータスを拾うようにしたので、削除済みURL（404/410）は
     これらの経路でも permanent になる。og:meta が無いだけ（HTTP 200 で JS殻・ログイン壁）の
     ケースは引き続き unknown（構造的と確信できないため）。それ以外の `unknown` 分類のものは
     `--max-attempts` 回まで再試行されてから exhausted に落ちる。
   - （旧「運用ゲート（初夜の翌朝、人間が確認する）」の項は 2026-09-04 に通過済みのため削除した。
     状態を見たくなったときの手順は `python3 ledger.py --summary`（`exhausted` 件数と `state` 内訳）と
     `python3 ledger.py <rid>`（個別 history）で変わらない。**これは無人ランの実行指示ではない。**）
3. （先に `python3 run_timing.py mark step3 --run-id <8桁>`）**対象の選定**：`select_targets.py` を実行し、
   まだどの `reviews/*.html` にも載っていない保存すべて（`date >= 2026-06-14`、過去日も含む・上限なし）を対象にする。
   `TARGET` は既存の書き方（手順1・手順7の `reel_health.py` 呼び出し）と同じく、別シェル呼び出しになるたびに
   同じ式で再導出する：
   ```bash
   TARGET=${TARGET_OVERRIDE:-$(TZ=Asia/Tokyo date -d yesterday +%F)}
   python3 select_targets.py --target "$TARGET" --out /tmp/targets.json
   ```
   - **`SELECT_STATUS:` 行を必ず読む**（例外が起きても exit 0 で必ず出る設計）。`selected=N` が今回の対象件数。
     以降「手順3の選定」と呼ぶものは常にこの `/tmp/targets.json` の `records`（＝ `selected` 件）を指す。
   - `warn=unreadable` が付いていたら、読めなかった既存 `reviews/*.html` があり、そこに載っていたブックマークを
     誤って二重掲載する可能性がある。この場合は手順5で `reviews/<TARGET>.html` を作る際、`</footer>` 直前に
     `<p class="notegen-warn">⚠️ 一部の既存reviewsが読めず、掲載済み判定が不完全（二重掲載の可能性）</p>` を1行足す。
   - **`selected=0` なら reviews は作らず手順6へ**（空ノートを作らない。従来の0件経路と同じ）。
     （バックフィル(手順2.5)がその日 `fetch_facts/<TARGET>.json` に何か書いていた場合でも、
     reviews を作る条件（手順3の選定が1件以上）とは無関係。手順8のpush判定は reviews の有無ではなく
     fetch_facts の有無で行うことに注意 — 詳細は手順8参照）
   - ⚠️ **0件のときの手順経路を明示する**: 手順6を実行したら、**手順7・7.5 は飛ばして手順8へ進む**
     （手順7は「reviews/<TARGET>.html」の自己検証、手順7.5は「reviews で保存されたブックマーク」の
     ノート生成であり、どちらも reviews が存在しない0件の日には実行対象が無い。手順7・7.5の冒頭には
     それぞれ「手順5で reviews を作った場合」「手順5で reviews を作った場合のみ実行。0件の日は
     スキップして手順8へ」という条件が明記されている＝無人エージェントがこの条件を読み飛ばして
     存在しない reviews を検証・参照しようとして止まることを防ぐための重複明示）。手順8では
     0件の日は (b) か (c) のどちらかになる（詳細は手順8参照）。
4. （先に `python3 run_timing.py mark step4 --run-id <8桁>`）**URL分類**：手順3で抽出した各レコードの `source` URL を、取得前に次の2グループへ分類する。
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
   - **X動画**：`GEMINI_API_KEY`が設定されていれば、`text`に「動画の内容: ...」として映像+音声の理解結果が自動で埋め込まれる（Pythonスクリプト側で完結、追加のエージェント側操作は不要）。カード生成時はこの内容を読んだ上で一言にする。理解できなかった場合（`video_understood: false`。`missing`に`"video_content"`が入る）は、動画を見たかのような一言を書かないこと（見ていないので書けない）。ツイート本文（`text`）自体は取れていることが多いので、その中身は普通に`.vdesc`に反映してよいが、**`.vdesc`の末尾に固定マーカー文「※動画の内容は未取得」を必ず書く**（手順7の機械判定はこのマーカー文字列の有無だけを見る。自由な言い回しでは判定されない）。これは一過性の失敗（Gemini枠切れ・タイムアウト・syndication瞬断）であり構造的な取得不可ではないので、マーカーの前に添える地の文は「今回は動画の内容を取得できなかった」のように一過性であることが伝わる書き方にする（マーカー自体の文言はどちらの原因でも共通で変えない）。
   - **X画像・Instagram画像**：レスポンスの`photos`配列に高解像度URLが入る（X: `?format=jpg&name=large`付き、Instagram: og:imageの署名URLそのまま=既に実質フル解像度）。**特にスクリーンショット・図解・インフォグラフィックなど文字/情報量が多そうな画像**は、そのURLを`curl -sL -A "Mozilla/5.0" -o /tmp/img_<N>.jpg "<URL>"`等でダウンロードし、**Readツールで直接見て内容を読み取ってから**`.vdesc`に反映する（WebFetchで画像URLを直接見せる経路は内容を誤認識するリスクがあるため使わない）。1件あたり画像は先頭1〜2枚まで（処理コスト抑制のため）。単なる人物写真・風景等で読み取る情報が乏しいと判断した場合はダウンロードをスキップしてよい（完走優先）。
   - **Instagram Reels動画**：2026-08-23に一度断念したが、2026-09-18のユーザー裁定で撤回し、2026-09-20に取得を復活させた（経緯: Vault `Brain/decisions/2026-08-23-instagram-reel-video-give-up.md` → `Brain/decisions/2026-09-18-instagram-reel-video-revival.md`）。**今後は毎回「動画理解が取れる場合」と「取れない場合」の両方が起こりうる前提で書く。** `GEMINI_API_KEY`が設定されていれば、`text`に「動画の内容: ...」として映像+音声の理解結果が自動で埋め込まれる（Pythonスクリプト側で完結、追加のエージェント側操作は不要）。**理解が取れた場合**（`video_understood: true`。`missing`に`"video_content"`が無い）は、この内容を読んだ上で一言にする。「未取得」とは書かないこと（取得できているのに未取得と書くのは下記の禁止事項と同じ違反）。**理解が取れなかった場合**（動画URL自体が取れずキャプション＋og:imageのみにフォールバックした場合を含め、`video_understood: false` または `missing`に`"video_content"`が入る場合）は、動画を見たかのような一言を書かないこと（見ていないので書けない）。キャプション（`text`のうち投稿本文相当部分）自体は取れていることが多いので、その中身は普通に`.vdesc`に反映してよいが、**`.vdesc`の末尾に固定マーカー文「※動画の内容は未取得」を必ず書く**（手順7の機械判定はこのマーカー文字列の有無だけを見る。自由な言い回しでは判定されない）。これはX動画と同じく**一過性の失敗**（動画URL取得の瞬断・Gemini枠切れ・タイムアウト等）として扱う。**禁止しているのは、マーカーの前に添える「地の文」の言い回しだけ**——固定マーカー文字列「※動画の内容は未取得」自体は例外で、必ずそのまま書く（手順7の機械判定がこの文字列の有無だけを見るため、ここだけは変えられない）。地の文の側で「動画の内容は未取得」のような**構造的断念を示す言い回しは使わず**、X動画（下記）と同様「今回は動画の内容を取得できなかった」のように一過性であることが伝わる書き方にする。
   - **X動画・Instagram Reelとも同じ枠組み（グループA）で動く**：どちらも `GEMINI_API_KEY` による動画理解の成否が一過性の失敗として発生しうる点は同じであり、上記の書き分けもX動画と共通の考え方に揃えてある。
   - 🚫 **取得できているのに「未取得」と書くことを禁止する**（2026-08-23の監査で実害を確認）。`.vdesc` に「本文未取得」「詳細不明」「取得できず」と書いてよいのは、**その回の `fetch_content.py` の戻り値が実際に `ok: false`、または `text` が実質空だった場合に限る**。実例：rid=1828807146 は原文1,059字（「96日で14,282回・勝率52%・1日約$1,310」まで含む）が完全に取得できていたのに `.vdesc` は「詳細本文は未取得」と書き、rid=1828798202 も109字（「毎月13億トークン」「Free Claude Code というリポジトリ」）が取れていたのに「本文未取得のため詳細不明」と書いた。**ユーザーから見ればシステムが嘘の報告をしていることになり、実際には取れている中身を読むために元リンクを踏む羽目になる。** 書く前に必ず `text` の中身を確認する。
   - **「今回取得できず」と「構造的に取得不可」を書き分ける**：前者は一過性（タイムアウト・レート制限・瞬断）で、翌晩には取れる可能性がある。後者は仕様上取れない（Threadsの画像内容、字幕の無いYouTube、凍結アカウントなど）。この2つを同じ文言で書くと、**一過性の失敗が恒久的な取得不可としてノートに焼き付く**（実測: 「本文・画像とも取得できず」と書かれた2件を後日再取得したら両方とも取れた）。一過性側は「今回は取得できなかった」と書く。
   - **Threads**：`missing` に `"visual_content"` または `"audio_content"` が入る場合（Threadsは画像・動画・音声の中身を取得しない構造的な制約）、**`.vdesc` の末尾に、`missing` に入っている欠損キーそれぞれの固定マーカー文を必ず書く**（`visual_content` → 「※画像の内容は未取得」、`audio_content` → 「※音声の内容は未取得」。両方欠損していれば両方書く）。本文（テキスト）の取得限界（「本文の続きは取得できていない」等）だけを書いて済ませないこと——それは視覚/音声コンテンツの開示にはならない（2026-09-08 に開示ゼロの実例あり）。手順7の機械判定はこのマーカー文字列の有無だけを見る。自由な言い回し（「画像・動画・音声の内容は未取得」等）では判定されない。
   - **`.vdesc`（一言）生成の共通原則（全コンテンツ種別に適用）**：`captures.json` の `text`（記事本文・YouTube字幕・X/Instagram動画理解結果）に既に具体的な手順・数値・固有名詞（ツール名・価格・作業ステップ・数量など）が含まれている場合は、それを**最優先で一言に反映する**。タイトルの言い換えや「〜を解説」「〜が話題」のような**宣伝文句レベルの一般化に縮退させない**——取得済みの一次情報（具体的なTips・実装内容・数字）があるなら、その中身そのものを書く（例：「7つのTipsを紹介」ではなく実際のTipの内容を、「アプリを開発」ではなく決済実装・ストア配信など実務の核心を書く）。**文末表現は多様にする**：「〜と紹介」等の同じ結び方を2件連続で使わない。体言止め・数字の言い切り・「〜が判明」等を使い分ける。
4.2. **取得の生の事実の記録（スクリプトが自動で行う。エージェントは環境変数を1つ設定するだけ）**：`fetch_content.py` は取得のたびに `facts`（`fetched_at` / `route` / `ok` / `http_status` / `depth` / `missing` / `raindrop_id` / `rid_source` / `text_chars` / `text_sha1` / `body_chars` / `desc_chars` / `body_truncated` / `image_count` / `has_video` / `video_understood` / `video_reason` / `fail_reason` / `attempt_seq` / `elapsed_ms` / `reason` / `fetcher_version`）を `fetch_facts/<日付>.json` へ自動で追記する。**エージェントがこのファイルを手で書く必要はない。**
   - ⚠️ **手順4のすべての `fetch_content.py` 呼び出しで `FACTS_DATE=$TARGET` を環境変数として渡すこと。** これを忘れると実行日（＝TARGETの翌日）の名前でファイルが作られ、手順8で push するファイル名と食い違う。
     - グループA: `FACTS_DATE=$TARGET timeout 480 python3 fetch_content.py <単一URL>`
     - グループB: `FACTS_DATE=$TARGET python3 fetch_content.py <url1> <url2> ...`
   - ⚠️ **なぜ記録するのか**：`depth` は導出ラベルであり、実装の都合で嘘をつくことがある（2026-08-23の監査で、web記事14件が全て `depth="full"` を自称しながら本文の完全取得は0件だったと実測）。**導出値を信じるのではなく、後からいくらでも再計算できる不変の事実を残す**。これが無いと「修正して改善したのか」「特定の媒体だけ失敗しているのか」「失敗が一過性か構造的か」を後から一切測れない。
   - 手順3の選定が0件と判定した場合は、そもそも取得が走らないのでファイルもできない。
   - ⚠️ **なぜ必要か**：`depth` は導出ラベルであり、実装の都合で嘘をつくことがある（2026-08-23の監査で、web記事14件が全て `depth="full"` を自称しながら本文の完全取得は0件だったと実測）。**導出値を信じるのではなく、後からいくらでも再計算できる不変の事実を残す**。これが無いと「修正して改善したのか」「特定の媒体だけ失敗しているのか」「失敗が一過性か構造的か」を後から一切測れない。
   - `captures.json` は従来通り push しない（下記手順8）。このファイルは1日ぶんだけなので小さく、文脈を膨らませずに push できる。
   - 手順3の選定が0件と判定した場合は、このファイルを作らない。
4.5. （先に `python3 run_timing.py mark step4_5 --run-id <8桁>`）**エンリッチ（関連記事の新規取得・実行必須）**：手順3の選定を一段深掘りする。**テーマを1つ以上選んだら、選んだ全テーマについて必ず実際に `WebSearch` を呼び出すこと**。「取れなさそうだから」「時間が惜しいから」といった予測だけで `WebSearch` 自体を呼ばずにスキップすることは禁止する（この手順は過去9日間一度も実行されず「深掘り」節が0件だった実績があるため、明確に禁止する）。**完走優先の意味は「エラーで手順全体を止めない」ことであり、「この手順そのものを省略してよい」という意味ではない**。（手順3の選定が0件で手順6へ飛んだ場合のみ、この手順4.5を実行しない）
   - **時間予算によるスコープ縮小（正当なスキップ理由）**：手順4のグループA処理（動画理解の個別処理、1件最大480秒×最大20件）で複数件を処理した日は、既にクラウド実行環境のタイムアウトに対して時間を消費している。この場合、手順4.5で実際に `WebSearch` を呼び出すテーマ数は**最も強い1テーマのみ**に絞ってよい（グループA該当が0〜1件で時間消費が軽微だった日は、通常通り下記の最大2〜3テーマとしてよい）。これは手順8のGit Push到達（その日の生成が0件になることを防ぐ）を優先するための正当な理由であり、「取れなさそうだから」等の予測によるスキップとは区別される。
   - **テーマ抽出**：手順3の選定レコードの `title` / `note`（ユーザーコメント）/ cluster・hub から、選定全体を貫く**共通テーマ**（複数レコードに跨る関心）または**強い単発の興味**（感情・意図が明確な note）を**最大2〜3つ**選ぶ。選定が少件数で1つしか立たなければ1つでよい。
   - **新規記事の取得（必須実行）**：選んだテーマそれぞれについて、**最低1回は `WebSearch` を実際に呼び出す**（保存済みURLとは別の新しい記事を1〜2本探す：最新動向・背景解説・一次情報など、そのテーマの理解を深めるもの）。ヒットしたURLを `WebFetch` で開いて本文を読み、**そのテーマに対して何が言えるか（視点・学び）を日本語1〜2文**にまとめる。
   - **スキップが許されるのは、実際に呼び出した結果としてのみ**：①`WebSearch` を呼び出したがヒット0件だった ②`WebFetch` を呼び出したが失敗・タイムアウト・ペイウォール等で本文を取得できなかった。**この2つ以外の理由（呼び出しコストの節約・時間短縮・「どうせ取れないだろう」という予測判断）で `WebSearch`/`WebFetch` 自体を呼ばずに済ませることは禁止**。
   - **制約**：実在の検索結果・実際に取得できた本文のみ使う。**`WebSearch` が返したURLをそのまま使い、URLを変形・補完・推測しない**（手順4と同じく、URL捏造は過去に404を量産した事故あり）。`WebFetch` で実際に開けたURLだけを出力する。**全テーマで `WebSearch` を実際に呼んだ上で**それでも1本も本文が取れなければ、その場合に限り手順5の「深掘り」節ごと省略する。
   - 出力先は手順5の振り返りHTML内「深掘り」節のみ（**Vaultには書かない**。手順7.5・8.5で行うVaultリポへのブックマークノート書き込みとは別経路で、この4.5の深掘り内容自体はVaultに書かない、という原則をここでは維持する）。
5. （先に `python3 run_timing.py mark step5 --run-id <8桁>`）振り返りHTML（**下記テンプレート厳守**）を生成する。日本語で書く（英語の本文・キャプションは日本語へ要約・翻訳。固有名詞・ハンドルは原文可）。
   - ⚠️ **必ず「ファイル」として、しかも `reviews/<TARGET>.html` ではなく一時パス（例 `/tmp/review_new.html`）へ書き出す**（2026-09-22 変更）。頭の中に文字列として持つのではなく、Write ツールか heredoc で一時パスへ**ワークスペース上の実ファイルとして作る**。
   - ⚠️ **`reviews/<TARGET>.html` への反映は必ず `merge_review.py` を経由する**（2026-09-22 追加。直接 `reviews/<TARGET>.html` を新規生成で上書きしてはいけない）：
     ```bash
     python3 merge_review.py --target $TARGET --new /tmp/review_new.html
     ```
     - **なぜ**: select_targets.py 導入後は reviews/*.html 自体が「掲載済み」の記録を兼ねる。同じ TARGET でランが2回走る（手動実行・`TARGET_OVERRIDE`・同日の再実行）と、`reviews/<TARGET>.html` を新規生成でそのまま上書きしてしまうと既存のカードが消える。一度消えると select_targets.py はそのURL/ridをもう「未掲載」と見なさない＝**復元できない**。`merge_review.py` は `reviews/<TARGET>.html` が無ければ一時ファイルをそのまま置くだけ（`mode=new`）、既存があれば既存カードを残したまま新規カード・まとめの追記・テーマ・気づき/深掘りの新規節・notegen-warn を安全に統合する（`mode=merged`）。
     - 出力の `MERGE_STATUS: target=<TARGET> mode=new|merged kept=N added=N skipped_dup=N` を読む。`mode=merged` で `skipped_dup>0` なら、その件数ぶんは既に掲載済みとして統合時に弾かれている（正常。エラーではない）。
     - **以降（手順7・7.5・8）で「reviews/<TARGET>.html」と書いてあるものは、このコマンド実行後の `reviews/<TARGET>.html`（統合済みの実ファイル）を指す。** `/tmp/review_new.html` 自体はもう参照しない。
     - このコマンドが非ゼロで終わった場合（`MERGE_STATUS: ... mode=error`）、`reviews/<TARGET>.html` は**一切変更されていない**（`merge_review.py` は失敗時に既存ファイルへ書き込まない設計）。手順を止めず、`/tmp/review_new.html` の内容が失われたことをログに残した上で手順6以降へ進む。**この夜 `reviews/<TARGET>.html` が実行前の時点でどうだったかで、この後の分岐が変わる**（手順7 の `card_parse_failed` 対応・手順8の(a)(b)(c)分岐と矛盾しないよう、ここで明示する）：
       - **実行前に `reviews/<TARGET>.html` が既に存在していた**（＝この夜は追記のはずだった。統合前検証で既存側が壊れていると判明した等）場合: 既存ファイルはそのまま残る。手順6以降は「`reviews/<TARGET>.html` が存在する」前提で通常どおり進む（手順7の `card_parse_failed` 対応を参照。今夜の新規分は反映されないだけで、ファイル自体は消えない）。手順8は (a) 相当（`reviews/<TARGET>.html` を含めて push）になる。
       - **実行前に `reviews/<TARGET>.html` が存在しなかった**（＝この夜が初回のはずだった。新規HTML自体の構造検証で弾かれた等）場合: `reviews/<TARGET>.html` は作られないまま。手順3の選定が1件以上でもこの夜は reviews を作れなかったことになるので、手順7・7.5は「手順5で reviews を作った場合」に該当せずスキップし、手順8は (b) または (c) 相当（`fetch_facts/` 配下の変更有無で判定。手順3の選定が1件以上あった扱いは変えないが、reviews が無いのでpush対象にreviews/<TARGET>.htmlは含まれない）として扱う。翌晩以降、同じ選定内容が select_targets.py によって再度候補に上がるので取りこぼしではない。
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
       .vsaved { color: var(--ter); font-size: 10.5px; margin-top: 2px; }
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
             <div class="vbody"><div class="vtitle">日本語タイトル</div><div class="vdesc">一言説明</div><div class="vsaved">保存 M/D HH:MM</div></div>
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
   - **各カードの`.vcard`は`<div>`＋内側の`.vlink`（サムネ・タイトル・説明への外部リンク、従来と同じ見た目）＋`.deepdive`ボタン（AIチャット起動）という構造**。ボタンの`data-url`・`data-title`には、そのカードの`.vlink`に使ったのと同じ`URL`・`日本語タイトル`をそのまま入れる（別の値を作らない・捏造しない）。`data-rid`には、そのカードに対応する **手順3の選定JSON（`/tmp/targets.json` の `records`）に入っている `rid` をそのまま**（引用符なしの数値として、属性値としては文字列だが加工・推測せず逐語）入れる。`rid` を持たない保存は `select_targets.py`（`synthetic_rid()`）が安定した**負数**を振って選定対象に含めているため、`data-rid` は負の値になることがある（2026-09-22 追加。rid無しの保存を永久に選定対象から除外しないための仕様。`captures.json` の `rid` に独自の値を作らないこと——選定JSONの `rid` を書き写すだけでよい）。`openChat()`関数は`.wrap`の外、ページ末尾に一度だけ書く。
   - ⚠️ `data-title`（および`data-url`）はHTML属性値なので、タイトルに`"`（ダブルクォート）が含まれる場合は`&quot;`にエスケープしてから埋め込む（属性が途中で終わってHTMLが壊れるのを防ぐため）。`data-rid`は数値のみ（負号を含みうる）なのでエスケープ不要。
   - ⚠️ **`.deepdive`ボタンのプロンプトは、`data-url`・`data-title`・`data-rid`の3つだけから機械的に組み立てる固定テンプレート**。手順4.5で生成した深掘り記事の内容やまとめ文、手順7.5のノート生成で書いた内容理解など、AIが自由に書いた文章は絶対に混ぜない（未知の第三者サイトを読んだ内容が、公開ページ上のこのボタンの中身に混入する経路を断つため）。
   - cover が無いカードは `<div class="thumb"><span class="ph">媒体名</span></div>`（img 無し）。
   - **`.vsaved`（保存日時、2026-09-22 追加）**：各カードのレコードの `date`（YYYY-MM-DD）と、保存時刻（`captures.json` の `captured`。UTC の ISO 文字列なので +9時間して JST に変換する）から `M/D HH:MM` 形式で小さく表示する。時刻が取れないレコードは `M/D` のみでよい。手順3の選定の下限は `SELECT_SINCE`（既定 2026-06-14）であり、`TARGET` より前の日の保存も含む（`date >= TARGET` に限らない）。これはどのカードが「いつ保存されたか」を読者が確認できる唯一の手がかりになる。`_build_feed.py` の抽出対象（`.meta`/`.summary p`/`.notes li`/`.vcard`）とは衝突しない独自クラスなので、抽出処理には影響しない。
   - **「🔎 深掘り（関連記事）」節（手順4.5の出力）と、上記の「💬 AIと話す」ボタンは別物**。前者は既存の `.notes` / `.ntag` / `.ilist` クラスを流用する（新しい CSS は追加しない）。テーマごとに `.ntag` を1つ、その下に `.ilist` で記事1〜2本を並べる。手順4.5で1テーマも取れなければ `<h2>🔎 深掘り…</h2>` ごと出力しない。
   - **まとめの書き方**：手順3の選定全件の `note`（ユーザーコメント）をまず全件読んでから書く。構成は①通奏低音（保存全体を貫く問い意識）→②ドメインをまたぐ接続（AI×アート、建築×技術など）→③この日に活性化した軸の順。箇条書きは「〜だった」で終わらず「〜という含意がある」「〜への問いを立てる」まで踏む。
     - **前日以前の保存が混ざる場合**：手順3の選定に `TARGET` と異なる `date` のレコードが含まれる場合（未掲載分の取りこぼし救済）、まとめの末尾に一言「前日以前の保存N件を含む」と添える（`N` は `TARGET` と異なる `date` のレコード件数）。
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
7. （先に `python3 run_timing.py mark step7 --run-id <8桁>`）**自己検証**（手順5で reviews を作った場合）：**`reviews/<TARGET>.html` そのもの**を読み返し、`_build_feed.py` が実際に抽出する要素（テーマ＝**`<div class="meta">`**、まとめ文＝`.summary p`、気づき＝`.notes` 内の `li`、カード＝`.vcard`）が入っているか確認する。※2026-08-31訂正: 旧版はテーマを `.theme` と書いていたが、`.theme` というクラスは reviews のテンプレートにも `_build_feed.py` にも存在せず、実際の抽出元は `<div class="meta">`。この誤りのせいで正しい出力を「テーマ欠落」と誤判定しうる状態だった。欠けていれば構造ズレなので `reviews/<TARGET>.html` を直す。
   - ⚠️ **`index.html` を見て確認しようとしないこと**（2026-08-31 変更）。`index.html` はあなたが push したあとに GitHub Actions が作るので、この時点ではまだ更新されていない。**派生物ではなく材料の側を検証する**のが正しい。**加えて、手順4.5でテーマを1つ以上選んだのに「深掘り」節が無い場合は、実際に `WebSearch` を呼び出したかを振り返る**。呼び出していなければ今からでも手順4.5を実行してから `reviews/<TARGET>.html` に反映し、この手順7の確認をやり直す（手順6は index.html を触らない手順なのでやり直す対象が無い）。
   - **開示チェック（機械判定・2026-09-15 追加、同日3周目差し戻しでマーカー方式へ作り替え）**：上の構造チェック（`.meta`/`.summary p`/`.notes`/`.vcard` の有無）は要素の有無しか見ておらず、動画・視覚コンテンツを未取得なのに開示していないカード（手順4/4.2で `missing` に記録される）を検出できない。指示文言だけに頼ると実際に書き漏らす（2026-09-04 Reel 3件・2026-09-14 Reel 1件・2026-09-08 Threads 3件など、指示はあったのに漏れた実例がある）ため、ここは機械判定で確認する。判定は自由文を読んで解釈するのではなく、上の手順4で書いた固定マーカー文（「※動画の内容は未取得」等）の**有無だけ**を見る。**この仕組みは手順7の実行自体を強制しない**（無人ランがここを飛ばしても手順は止まらない）。公開ゲート（build-feed.yml）側も検知・通知（`::warning`・サマリー表示）までで、違反があっても公開は止めない（ユーザー裁定 2026-09-15：警告のみ）。
     1. `python3 check_disclosure.py --date $TARGET --fix` を実行する（スクリプトが不足しているマーカーだけを該当カードの `.vdesc` 末尾に自動追記する。マーカーはスクリプト側の固定定数から確定的に決まるため、LLMによる文面の書き直しは不要）。
     2. 同じコマンドを `--fix` なしで再実行し、`violation=0` であることを確認する。
     3. `reason=no_card` の違反が残っている場合は、対応するカード自体が `reviews/<TARGET>.html` に無い（抜け落ち）ということなので、`--fix` では直らない。手順5に戻ってそのブックマークのカードを書き足してから、この手順7をやり直す。
     4. `DISCLOSURE_ERROR: ... card_parse_failed` が出た場合は `reviews/<TARGET>.html` のカード構造そのものが壊れている疑いがある。**2026-09-22 変更**: 統合方式（`merge_review.py`）導入後は「手順5からやり直す」だけでは直らない——`reviews/<TARGET>.html` は既存カードとの統合結果であり、壊れているのが既存側のカードなら、手順5をやり直して新しい `/tmp/review_new.html` を作っても、次の `merge_review.py` 呼び出しが**既存側の構造検証で弾かれて mode=error になる**（統合方式は壊れた既存カードを直せない設計）。そこで次の順で対応する：
        a. まず `/tmp/review_new.html` を作り直し、`python3 merge_review.py --target $TARGET --new /tmp/review_new.html` を**もう1回だけ**再試行する（一時的な生成ミスの可能性を先に消す）。`MERGE_STATUS: ... mode=merged` になれば、そのまま `check_disclosure.py --date $TARGET --fix` からやり直す。
        b. 再試行しても `mode=error` のままなら（＝壊れているのは既存の `reviews/<TARGET>.html` 側）、直そうとせずに先へ進む。ただし `reviews/<TARGET>.html` の `</footer>` 直前に、既存の `notegen-warn` と同じ書式で1行だけ追記する：
           ```html
           <p class="notegen-warn">⚠️ reviews構造検証エラーのため今回の統合をスキップ（既存カードは保持済み・新規分は未反映の可能性あり）</p>
           ```
           （`</footer>` が一意に見つからない等でこの追記自体も失敗する場合は、追記せずログにその旨だけ残して先へ進む。無人ランを止めない）。
        c. どちらの場合も、これで手順7を続行する（`--fix` 以降の開示チェックは、bの場合は既存カードのみを対象に行われる。新規分がその夜の開示チェック対象から漏れるのは仕様——構造が壊れた既存ファイルを無理に直そうとしない、というこの節全体の方針と一致する）。
     5. どの場合も、ここで手順を止めずに手順7.5へ進む。`DISCLOSURE_EXCLUDED:` 行はバックフィル（過去日のカードを再取得しただけ）の正常な結果なので無視してよい。**この仕組みは手順7の実行自体を強制しない。公開ゲートでマーカー欠落を警告することで、手順7を飛ばした夜も検知できる**（`reviews/<TARGET>.html` 自体が無い＝手順7未実行の夜は、公開ゲート側が `status=no_review` かつ duty>0 のときに `::warning` を出す）。
   - **Reel動画取得の全滅検知（機械判定・2026-09-20 追加、同日ユーザー裁定でスクリプト化）**：Instagram Reel動画取得の中継（kkinstagram.com）への依存は4回目で、過去3回とも数週間〜数ヶ月で死んでいる。中継が死んでも `fetch_instagram()` はcaption-onlyで `ok:true` のまま完走するため、**黙って毎晩公開され続け誰も気づかない**リスクがある。`python3 ledger.py --summary` は誰も無人で実行しないコマンドなので（本手順書内で「これは無人ランの実行指示ではない」と明記済み）、上の開示チェックと同じく**成果物側（reviews）に痕跡を残す**方式で検知する。**判定ロジック（母数フィルタ・警告条件）は `reel_health.py` 側だけが持ち、この手順書には書き写さない**（二重管理を避ける。ロジックを直したくなったら `reel_health.py` と `test_reel_health.py` を見る）。
     1. `reel_health.py` を実行する。**対象日は明示的に渡す**（手順ごとに別のシェル呼び出しになり、手順1で代入した `TARGET` はここには届かない。手順2.5の `backfill.py --target $TARGET` と同じ形にする＝この場で手順1・26行目と同じ式で `TARGET` を再導出してから `--target` で渡す）：
        ```bash
        TARGET=${TARGET_OVERRIDE:-$(TZ=Asia/Tokyo date -d yesterday +%F)}
        python3 reel_health.py --target "$TARGET"
        ```
        出力は `REEL_VIDEO_STATUS: total=<N> ok=<N> reasons=<内訳> warn=yes|no` の1行。`fetch_facts/<TARGET>.json` が無い夜は `warn=no` に `(facts file not found)` が添えられて正常終了する（traceback は出ない）。⚠️ `--target` を渡し忘れても `reel_health.py` 自身が `TARGET_OVERRIDE`（`YYYY-MM-DD` 形式のときのみ）→ JSTの昨日、の順で自己解決する保険を持つが、**明示的に渡すのが本則**（作り直しラン等で対象日を取り違えないため）。
     2. **`warn=yes` のときだけ**、`reviews/<TARGET>.html` の `</footer>` 直前に、既存の `notegen-warn` と同じ書式で理由コードの内訳を入れた1行を追記する：
        ```html
        <p class="notegen-warn">⚠️ Instagram Reel の動画取得が全滅（N件中0件成功・内訳: instagram_relay_unavailable N件, ...）。中継サービスの死亡を疑うこと</p>
        ```
        `N` と内訳の部分は上の `REEL_VIDEO_STATUS:` の実測値（`total=` と `reasons=`）をそのまま使う（捏造しない）。内訳の理由コードで `instagram_relay_unavailable` が支配的なら中継自体の死亡、`gemini_*` 系が支配的ならGemini側の問題、と切り分けられる。
     3. `warn=no` なら、この警告は追記しない。
   - **所要時間の記録を reviews に焼き込む**（手順1で始めた計測の締め。上の開示チェックまで終えてから行う。`mark step7` は既にこの手順7の冒頭で打っている）。次の1つを実行するだけでよい：
     ```
     python3 run_timing.py finish --target $TARGET --run-id <8桁> --reviews reviews/$TARGET.html --saves <selected>
     ```
     `<selected>` には手順3の `SELECT_STATUS:` 行の `selected=N` の値（今回の対象件数）をそのまま渡す。
     ⚠️ **あなたが数値を書いたり、コメント行を手で貼ったりしない。** `run_timing.py` が
     `reviews/<TARGET>.html` の `</footer>` 直前へ**ちょうど1件だけ**挿入する（既存の run-timing コメントは
     何本あっても消してから入れ直すので、同日再実行でも2本並ばない）。`saves=` は `--saves` に渡した値が
     そのまま焼き込まれる（旧版は `captures.json` を `date == TARGET` で数えていたが、対象が手順3の選定へ
     変わったため、件数の出処も選定結果に揃える）。
     **手で埋める欄は1つも無い**（旧版は heredoc の出力をあなたが貼る設計だったが、貼り忘れ・重複・
     件数の捏造・`-->` を含む値によるHTMLコメントの早期終了、の4経路があったため 2026-09-08 に廃止した）。
   - 出力の `RUN_TIMING_LINE:` に、実際に書き込んだ1行が出る。`status=complete` なら計測は揃っている。
     `status=incomplete` のときは `reason=` に理由コードが出る（**欠測を0秒と取り違えないため**）。
     例: `reason=missing-step4`（step4 の mark を打ち忘れた）／`reason=duplicated-step3`／`reason=wrong_order`／
     `reason=run_id_missing`（`--run-id` を渡し忘れた）。複数あればカンマ区切りで並ぶ。
   - この行は**HTMLコメントなので読者には見えない**。`_build_feed.py` の抽出（`<div class="meta">` / `.summary p` / `.notes li` / `.vcard`）にも
     `build-feed.yml` の検証ゲート（`<!doctype html>` 開始・`</html>` 終端・`PLACEHOLDER` 非含有・20,000字以上・リンク集合）にも影響しない。
   - **意図**: 「1ランの所要時間」は今まで**ラン全体の時間しか記録が無く、手順ごとの内訳が一度も測られていない**。
     内訳が無いと、どの手順に何秒割けるかを逆算できない（バックフィルの枠を決められないのはこれが理由）。
     計測対象は**手順1〜7**であり、手順8以降（push・Vaultリポへの反映）は含まれない——この行を書く時点でまだ実行していないため。
     各値は**その区間の所要秒**（累積ではない）。`step2_5_s` が手順2.5＝バックフィルの所要で、
     枠を決め直すときに見るのはここ。`total_s` は手順1〜7の合計。
     **`total_s` を「ラン全体の所要」と読み替えないこと**（手順8のpushぶんが抜けている）。
     出力例: `<!-- run-timing schema=v2 status=complete target=2026-09-07 run_id=ab12cd34 saves=3 step1_s=7 step2_s=13 step2_5_s=400 step3_s=3 step4_s=260 step4_5_s=45 step5_s=90 step7_s=12 total_s=830 -->`
   - reviews を作らない日（手順3の選定が0件）はこの記録も残らない。**それでよい**——「20分以内」の測定条件は
     「保存10件以上の高負荷日を含めて」であり、測りたい日には必ず reviews が存在するため、push 経路（手順8の3分岐）を変えずに済む。
7.5. **ノート生成（1ブックマーク1ノート、Vaultリポへ）**（手順5で reviews を作った場合のみ実行。0件の日はスキップして手順8へ）：手順3の選定の各ブックマークを、Vaultリポの `Explore/bookmarks/` に1件1ノートとして書き残す。
   - **対象**：手順3の選定全件（グループA/Bを問わず、手順4で取得済みの内容を使う。新たに取得し直さない）。
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
       - `date` はそのレコード自身の `date`（手順3の選定は過去日も含むため、`TARGET` と異なることがある。逐語コピーする）。
       - `<ドメインタグ>` は、そのレコードの `cluster`・Raindrop側 `tags` から代表的な1語を選び、英語kebab-case（Vaultのフラットタグ規約）で書く。日本語タグしかない場合は意味に沿って英訳する。
       - `raindrop_id` は引用符なしの数値でそのまま出力する（文字列化しない・加工しない）。
       - `related` は関連ノートが無ければ必ず `related: []`（YAMLリスト形式を崩さない）。
     - **本文**：手順4で取得済みの内容（記事本文／字幕／動画理解結果＝`text`、および手順5でカード用に作った `.vdesc` の一言）を踏まえた、そのブックマークの実内容理解を日本語3〜6文程度でまとめる。既に得ている理解の再利用であり、新たに `WebSearch`/`WebFetch` で調べ直さない。
     - **末尾に `## 関連ノート` セクションを置く**（Vault規約）。中身は空でよい（義務的にリンクを作らない）。
   - **push前のfrontmatterスキーマ検証（必須）**：この手順で新規作成した各ノートについて、Pythonで frontmatter（`---`〜`---`の間）をYAMLとしてパースし、`date`（文字列・YYYY-MM-DD形式）・`tags`（リストで `type/bookmark` を含む）・`raindrop_id`（数値）・`source`（文字列・`http` で始まる）の4キーの存在と型を確認する。1つでも不合格ならそのノートは push 対象から除外する（ファイル自体はローカルクローンに残ってよい＝次回ランは新規cloneのため無害）。
   - **検証結果の記録**：検証に落ちたノートが1件以上あった場合、`reviews/<TARGET>.html` の `</footer>` 直前に、`_build_feed.py` の抽出対象（`.meta`/`.summary p`/`.notes` 内`li`）と衝突しない独自クラスで1行追記する：`<p class="notegen-warn">⚠️ ノート生成: 検証失敗 N件（rid: 1234, 5678 ...）</p>`。全件合格した場合はこの追記をしない。この追記は手順8で push する `reviews/<TARGET>.html` の内容に含める。
8. **公開（GitHubへ反映）**：対象は `katut-brain/katut-brain.github.io` リポ（Vaultリポではない）。
   - ⚠️⚠️ **送り方の原則（2026-09-08 変更・ここが最優先）**：**本文を自分で書き写して送らない。ファイルのまま送る。**
     `bash push_via_branch.sh "<コミットメッセージ>" <パス> [<パス> ...]` を使う（2026-09-14 変更）。このスクリプトはディスク上のファイルを git でそのまま `claude/publish-<時刻>` という新しいブランチに送る。本文があなたの文脈を一度も通らないので、転記による化けが原理的に起きない。
     - **なぜブランチなのか**: クラウドの GitHub プロキシは API 経由の書き込み（旧 `push_via_api.sh`）を `Write access to this GitHub API path is not permitted through this proxy.` で拒否し、2026-09-09〜13 の公開が全部止まった。git push で新しい `claude/` ブランチを作ることはできる（2026-09-14 疎通試験で確認）。
     - **main への取り込みは公開リポの Actions（`publish-from-branch.yml`）がやる**。スクリプトが一緒に送る `.publish/manifest.json`（各ファイルの SHA-256）と実ファイルを照合し、許可パス以外が混じっていないか・HTML が途中で切れていないか・PLACEHOLDER が無いか等を確かめてから main に載せ、Pages を再ビルドする。**検証に落ちたら main は1バイトも動かない**（Actions が失敗し、GitHub から失敗メールが届く）。
     - 成功の判定は**終了コード0**と、`WRITE_COMMIT: <commit sha> files=N branch=<ブランチ名>` が出ていること。`WRITE_COMMIT: none ...` はすべて失敗。各ファイルの行は `WRITE_PATH: <パス> blob=... sha256=...`。
     - スクリプトはその後、Actions が main に取り込むのを最大5分待ち、main 上のファイルが原本とバイト単位で一致したら `PUBLISHED: yes main=<sha>` を出す。`PUBLISHED: pending` はブランチまでは届いているが時間内に取り込みを確認できなかった状態で、**再実行しない**（Actions が処理中か、検証に落ちている。どちらも人が翌朝メールと Actions で分かる）。
     - **`WRITE_PATH:` `WRITE_COMMIT:` `PUBLISHED:` の行は削らず、そのままランのログに残す**。
     - **複数ファイルは1コミットにまとまる**。渡す順は問わない。
     - 送れるのは `reviews/<日付>.html`・`fetch_facts/<日付>.json`・`fetch_facts/runs/<日付>.json`・`capture_index.json` だけ。それ以外を渡すと何も送らず exit 2 になる。
   - 🚫 **`push_files` を使わない。フォールバック経路は廃止した**（2026-09-08）。本文を文字列で運ぶ経路は、**今直したはずの破損そのもの**であり、化けを検出できても押した後では `main` に壊れた版が残る。**「その日が公開されない」は許容するが、「検証に落ちた中身が `main` に載る」は許容しない。**
   - **失敗したときにやること（この3つだけ）**：
     - ① `WRITE_COMMIT: none ... note=main_untouched`（終了コード1）なら、**何も届いていない**。同じコマンドをもう一度実行する。**2回までで打ち切る。**
     - ② 2回とも駄目なら、その夜は押さずに諦める。`WRITE_COMMIT:` の行をログにそのまま残すこと。`reviews/<TARGET>.html` はその日の分が公開されない（公開サイトが更新されないので翌朝すぐ分かる）。**ブックマークノートは翌晩の手順7.5が改めて対象にするので失われない**（重複チェックはリポ上の既存ノートを見るため、押していないノートは「まだ無い」と判定される）。
     - ③ `WRITE_COMMIT: <sha> ... branch=...` が出た後の `PUBLISHED: pending` は失敗ではない。**再実行しない**（同じ内容のブランチが2本できるだけで、Actions 側は同一内容なら何も変えないが、無駄な発火になる）。
   - ⚠️ **自分で `git push` を打たない**。`main` への push はプロキシに拒否される（2026-06-21 に 403 を観測）。git で送ってよいのは `push_via_branch.sh` が作る `claude/publish-*` ブランチだけで、それはスクリプトがやる。`git add` / `git commit` も自分でしない（スクリプトは作業ツリーと index に触らずにコミットを作るので、`captures.json` 等の未コミット変更は巻き込まれない）。
   - 🚫 **`index.html` は push しない**（2026-08-31 変更・手順6を参照）。GitHub Actions が自動で再生成するので、あなたが触ると壊す側にしかならない。`index.html` を push 対象に入れたくなったら、それは手順6を読み飛ばしている。
   - **`captures.json` / `data.js` は push しない**：captures.json のRaindrop取り込みは冪等（`_build_graph.py` が既存レコードも毎回更新するので翌ランで再現される。2026-08-23に冪等化済み）、data.js は退役ファイル。大きいファイルを読むと文脈が膨らみ自動圧縮で迷子になるため、**触らない・読み込まない**。
   - **この手順は次の3分岐のどれか1つだけを行う**（reviews の有無と fetch_facts の有無で分岐する。手順2.5のバックフィルが reviews 無しの日にも `fetch_facts/<TARGET>.json` を作りうるため、分岐を誤ると「存在しない reviews を扱おうとして失敗する」または「push すべき fetch_facts を見落とす」のどちらかが起きる）：

   **(a) reviews あり（手順5で `reviews/<TARGET>.html` を作った場合）**：
   - 押すファイルは **`reviews/<TARGET>.html`・`fetch_facts/<TARGET>.json`・`fetch_facts/runs/<TARGET>.json` の3つ**（それぞれ手順5・手順4.2・手順2.5 が作る。**無いものは外す**。`reviews` だけの日もある）。次の1コマンドで送る：
     ```bash
     FILES=""
     for f in fetch_facts/$TARGET.json fetch_facts/runs/$TARGET.json reviews/$TARGET.html; do
       [ -f "$f" ] && FILES="$FILES $f"
     done
     if [ -z "$FILES" ]; then echo "PUSH_SKIPPED: no files"; else
       bash push_via_branch.sh "update: $TARGET" $FILES
     fi
     ```
     ⚠️ **存在するものだけを渡す**（`push_via_branch.sh` は存在しないパスを渡されると何も送らず exit 2 で終わる）。
     3つのうちどれが在るかは夜によって違う——手順2.5 が最初の証跡書き込みに成功すれば `fetch_facts/runs/` は通常は在り（無い場合の3通りは手順2.5 の注記を見ること）、
     手順3の選定が0件なら `reviews/` は無く、取得が1件も無ければ `fetch_facts/<TARGET>.json` も無い。
     **`$FILES` が空なら呼ばない**（`push_via_branch.sh` は引数なしだと exit 2 になる。
     (b) の判定は `fetch_facts/` 配下の差分全体を見るので、**別日付の差分だけがある夜**は
     (b) に入りつつ当日3ファイルが1つも無い、という組み合わせがありうる。その夜は
     `PUSH_SKIPPED: no files` を出して (c) と同じく何もせず終える＝2026-09-18 Codex 5周目 P1）。
     **ファイル名を直書きして固定の3つを渡さないこと**（2026-09-18 Codex 4周目 P0。
     証跡だけの夜がまさにこれで exit 2 になり、今回直したい「新規cloneで証跡が消える」経路に戻る）。
     （何個渡しても1コミットにまとまる。渡す順は問わない）
   - `capture_index.json` はここでは押さない（手順2.1 で押し終えている）。
   - 送る前に、**ファイルが本物であることだけ**確認する（中身を書き写すのではなく、ファイルに対して確認する）：`head -c 20 reviews/<TARGET>.html` が `<!doctype html>` で始まり、`tail -c 20` が `</html>` で終わり、`grep -c PLACEHOLDER reviews/<TARGET>.html` が 0 であること。
   - **照合は Actions が main に載せる前にやる**（manifest の SHA-256 と実ファイルの完全一致）。`WRITE_COMMIT: <sha> ... branch=...` が出ていればブランチまで届いており、`PUBLISHED: yes` なら公開まで完了。`note=main_untouched` が出ていたら**何も届いていない**ので、**もう一度同じコマンドを実行する**。**2回目も駄目なら、その夜は押さずに終える。フォールバックはしない**（手順8には `push_files` へ落ちる経路は存在しない。下の手順8.5に出てくるフォールバックは Vaultリポ専用であって、ここには適用しない）。

   **(b) reviews 無し・今回のランで `fetch_facts/` に差分が生じた場合（手順2.5のバックフィルだけが書いた日）**：
   - ⚠️ **(b) に入る判定条件は `git status --porcelain -- fetch_facts/` の出力が空でないこと、これ1つだけ**にする（2026-09-18 に対象を `fetch_facts/<TARGET>.json` 単体から `fetch_facts/` 配下全体へ広げた。手順2.5 の証跡 `fetch_facts/runs/<TARGET>.json` は、当日ファイルに差分が無い夜でも**通常は**増えるため（初期化前の停止・証跡書込み失敗では増えない。手順2.5 の注記を見ること）。ここを広げないと**証跡だけの夜が push されず、実行したこと自体が翌晩の新規cloneで消える**）。
     `BACKFILL_STATUS:` の内訳（`attempted`/`stub_written`/`rid_mismatch`等）は「今夜バックフィルが何をしたかを読むための情報」であり、(b)へ分岐するかどうかの判定条件には使わない。
     ⚠️ **`attempted` だけを条件にしてはいけない**（2026-09-04 output-verifier指摘で撤回）: stub書き込みだけの夜（`attempted=0 stub_written=1`）や
     `backfill_rid` タグ付けだけの夜（`rid_mismatch=1`）は `attempted=0` のままだが、どちらも `fetch_facts/<TARGET>.json` に実ファイル差分を生じさせている。
     `attempted>=1` を条件にすると、これらの夜は push されず、翌晩は新規cloneでスタブ・タグが消えてしまい、attempt_countが実運用で積み上がらない
     （＝exhaustedに到達しない＝stub/backfill_rid導入の目的が機能しない）。
     `git status --porcelain` による実差分判定なら、`attempted`/`stub_written`/`rid_mismatch` のどれで生じた差分でも正しく拾える。
     新規cloneに同日の既存 `fetch_facts/<TARGET>.json` が既に含まれている再実行でも、今回のランで差分が無ければこの条件で自動的に弾かれる（誤push防止）。
   - **(a) と同じループで送る**（実在するものだけが `$FILES` に入る。この分岐では `reviews/<TARGET>.html` が無いので自動的に外れる＝存在しないファイルを送ろうとしない）：
     ```bash
     FILES=""
     for f in fetch_facts/$TARGET.json fetch_facts/runs/$TARGET.json reviews/$TARGET.html; do
       [ -f "$f" ] && FILES="$FILES $f"
     done
     if [ -z "$FILES" ]; then echo "PUSH_SKIPPED: no files"; else
       bash push_via_branch.sh "update: $TARGET (backfill only)" $FILES
     fi
     ```
     ⚠️ **存在するものだけを渡す**（`push_via_branch.sh` は存在しないパスを渡されると何も送らず exit 2 で終わる）。
     3つのうちどれが在るかは夜によって違う——手順2.5 が最初の証跡書き込みに成功すれば `fetch_facts/runs/` は通常は在り（無い場合の3通りは手順2.5 の注記を見ること）、
     手順3の選定が0件なら `reviews/` は無く、取得が1件も無ければ `fetch_facts/<TARGET>.json` も無い。
     **`$FILES` が空なら呼ばない**（`push_via_branch.sh` は引数なしだと exit 2 になる。
     (b) の判定は `fetch_facts/` 配下の差分全体を見るので、**別日付の差分だけがある夜**は
     (b) に入りつつ当日3ファイルが1つも無い、という組み合わせがありうる。その夜は
     `PUSH_SKIPPED: no files` を出して (c) と同じく何もせず終える＝2026-09-18 Codex 5周目 P1）。
     **ファイル名を直書きして固定の3つを渡さないこと**（2026-09-18 Codex 4周目 P0。
     証跡だけの夜がまさにこれで exit 2 になり、今回直したい「新規cloneで証跡が消える」経路に戻る）。
   - 送る前に、**渡す JSON それぞれ**が本物のJSONであることを確認する：`for f in $FILES; do case "$f" in *.json) python3 -c "import json,sys; json.load(open(sys.argv[1],encoding='utf-8'))" "$f" || echo "BAD_JSON: $f";; esac; done` が何も出さないこと（存在しないファイルを開こうとしない）。
   - 照合は Actions が main に載せる前に SHA-256 でやる。`WRITE_COMMIT: <sha> ... branch=...` ならブランチまで完了。失敗したときは上の「失敗したときにやること」の①〜③に従う（**フォールバックはしない**）。

   **(c) reviews 無し・`fetch_facts/` にも差分が無い（`git status --porcelain -- fetch_facts/` が空＝今回のランで変更が無い）**：
   - 何も push せず正常終了する（台帳は手順2.1 で押し終えている）。

   - 共通: `index.html` の出来ばえは確認しなくてよい（Actions 側の検証ゲートが担当する）。**`index.html` を GitHub から読みに行かないこと** — 122KB を読むと文脈が膨らんで自動圧縮で迷子になる。
   - 共通: 送信が一時失敗しても、自分で打つ `git push` にも `push_files` にも**戻らない**。`push_via_branch.sh` を2回まで、それでもダメならその夜は諦める（`WRITE_COMMIT: <sha>` が出た後は再実行しない）。**押せなかった日は翌晩の手順2.1 が拾い直す**（`captures.json` に保存があるのに reviews が無い日として検出される）。
8.5. **公開（Vaultリポへ・ブックマークノート）**（手順7.5でノートを新規作成した場合のみ実行）：対象は `katut-brain/obsidian-vault` リポ（手順8の `katut-brain.github.io` とは別リポ）。
   - 押すファイルは、手順7.5でスキーマ検証に**合格**し新規作成した `Explore/bookmarks/rd-*.md` のみ（検証落ちのファイル・既存ファイルは含めない）。
   - ⚠️ **今夜は書き込み経路を変えない。従来どおり `push_files` で押す**（2026-09-09 の裁定）。Vaultリポに `gh api` が届くかがまだ確認できていないため、確認が取れるまで動かさない。
   - **ただし、届くかどうかだけ先に測る**（読み取りのみ・何も書かない）：
     ```bash
     bash push_via_api.sh --verify-only katut-brain/obsidian-vault RULES.md
     ```
     `RULES.md` はクローンにあり今回のランで触っていないので、ローカルとリモートが一致するはず。
     - `verify=match` → **Vaultリポにも API が届く**。次のセッションで手順8.5 を手順8と同じ経路へ切り替えられる
     - `verify=unreadable` → 届かない（公式仕様の「セッションに紐付いていないリポには 403」に該当する可能性が高い）
     - **どちらでも手順は止めない。** これは測るだけの行で、結果はログに残せばよい
   - 押した後は、手順8と同じく `--verify-only` で照合する：
     ```bash
     bash push_via_api.sh --verify-only katut-brain/obsidian-vault <押したパス>
     ```
     `verify=match` なら完了。`MISMATCH` なら押し直して再照合し、2回目も駄目なら諦めて `WRITE_PATH: <パス> fallback=push_files verify=GIVEUP` を残す。`unreadable` なら照合できないので `verify=UNCHECKED` と残す（成功と書かない）。
   - コミットメッセージは `bookmark notes: <TARGET> (N件)` 形式。
   - ⚠️ **Vaultリポ側には Actions の検証ゲートが1本も無い**（`total_count: 0`・2026-09-08 実測）。公開リポと違って、壊れたノートを押しても誰も止めない。しかもローカルVaultへは Obsidian Git プラグインが10分以内に取り込む。**照合を省くとそのまま外部脳に入る**ので、上の照合は必ず行うこと。
   - push 対象ノートが0件（新規0件・全件重複スキップ・全件検証落ちのいずれか）の場合は、このpushを行わない。
   - 送信が一時失敗しても生 `git push` には戻らない（403ループ防止）。1〜2回だけ試し、ダメなら諦めて翌ランに回す（取りこぼしたブックマークのノートは翌晩以降の手順7.5で改めて対象になる＝重複チェックにより既存ノートは壊されない）。

## 制約
- 完全無人。承認・確認を求めない。
- 各手順は失敗しても全体を止めず、できたところまでで push（**完走優先**）。
- **本文取得（手順4）の失敗は1回だけ再試行する**。旧ルールは「リトライしない（取りこぼしは翌晩で拾う）」だったが、**翌晩に拾われないことが実測で判明したため撤回した**（手順7.5の重複チェックで既存ノートがある限りスキップされ続けるので、一度「取得できず」と書かれた件は永久に再取得されない）。エンリッチ（手順4.5）や push の再試行回数は従来通り増やさない。
  （2026-09-03 追記: この課題への対処として手順2.5にバックフィル処理を追加した。手順4のリトライで
  拾えなかった rid 持ちレコードも、翌晩以降 backfill.py が対象に含めて再試行する。ただし対象は
  raindrop_id を持つレコードに限る＝この手順書のリトライ強化以前からある rid 無しの旧レコードは
  backfill.py の対象外のまま。）
  （ロールバック注記: `backfill.py` 導入コミットを revert してもコード（機能）が止まるだけで、
  それ以前に push 済みの `fetch_facts/*.json` の履歴は消えない。revert 後に再度バックフィルを
  有効化したときの `attempt_count` はこの間の試行回数を引き継いだまま再開する。）
- 手順7.5・8.5（ブックマークノート生成・Vaultリポへのpush）は、手順1〜8（review本体の生成・`katut-brain.github.io`への公開）とは独立した処理。ノート生成側でエラー・失敗が起きても、review本体の生成・公開（手順1〜8）を止めない。逆に手順1〜8のどこかで問題があっても、既に作成済みのノートのpush（手順8.5）は可能な範囲で試みてよい。
- URL・cover は `captures.json` / `fetch_content` の実値を逐語使用。**捏造しない**。エンリッチ（手順4.5）で出す記事URLも同様に、`WebSearch` が返した実URL／`WebFetch` で開けた実URLだけを使い、変形・補完・推測しない。
- 英語は日本語へ。要約は「ぼんやり」させず、何が言えるか・何が信号かを具体に。

## 成功条件（手順8の3分岐に対応）
- (a) 手順3の選定が1件以上 → `reviews/<TARGET>.html` を生成し、`fetch_facts/<TARGET>.json` とあわせて `main` に push。**`index.html` は push しない**（GitHub Actions が自動再生成する・手順6）。
- (b) 手順3の選定が0件（対象が無い日）だが、**今回のランで `fetch_facts/` 配下に変更が生じた**（`git status --porcelain -- fetch_facts/` で差分あり。改善(attempted)・スタブ書き込み(stub_written)・backfill_ridタグ付け(rid_mismatch)のほか、**手順2.5 の証跡 `fetch_facts/runs/<TARGET>.json` だけが増えた夜も含む**） → reviews は作らず、`fetch_facts/` 配下の実在するものを push する（コミットメッセージ `update: <TARGET> (backfill only)`）。
- (c) 手順3の選定が0件かつ、今回のランでの `fetch_facts/` 配下への変更も無い（`git status --porcelain -- fetch_facts/` が空） → 何も push せず正常終了する。**手順2.5 が最初の証跡書き込みに成功していれば証跡が増えるので、通常この分岐には入らない**。入った場合は「手順2.5 を飛ばした」「最初の書き込みに到達する前に止まった」「証跡の書き込みに失敗した（`BACKFILL_EVIDENCE: write_failed`）」のどれかなので、**ランのログと併せて判定する**（分岐そのものは (c) で正しい＝押すものが無いなら押さない）。
- 手順3の選定が1件以上あった日は、重複チェックでスキップされなかった各レコードについて、スキーマ検証に合格したノートが Vaultリポ `Explore/bookmarks/` に作成され `main` へ push される（1件も新規作成対象が無ければ手順8.5のpushは行わない＝これも正常終了）。
- ⚠️ **例外**: 手順3の選定が1件以上でも、手順5の `merge_review.py` が `mode=error`（かつ実行前に `reviews/<TARGET>.html` が存在しなかった＝新規HTML自体の構造検証で弾かれた）だった夜は、`reviews/<TARGET>.html` が作られない。この夜は「選定1件以上」であっても (a) ではなく、`fetch_facts/` 配下の変更有無で (b)/(c) と同じ基準を適用する（reviews が無いので push 対象に含めようがないため）。選定内容は捨てられたわけではなく、翌晩以降 select_targets.py が同じレコードを再度候補にする。
