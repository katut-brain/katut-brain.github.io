#!/usr/bin/env python3
# backfill.py — 過去に取得できなかった rid 持ちレコードを再取得する（Task G-2）。
#
# 対象母集団は rid（raindrop_id）を持つ新規レコードのみ。rid 無しの旧レコードには
# 一切触らない。抽出ロジックは ledger.py に一本化し（ledger.backfill_candidates）、
# ここでは呼ぶだけにする（同じ抽出ロジックを二重に書かない）。
#
# 実行方式: 1URLごとに `fetch_content.py <url>` を **別プロセスで・完全に逐次**実行する
# （2026-09-03 Codexの敵対的レビューで NO-GO: ThreadPoolExecutor 案は
#   (1) future.result(timeout=...) はワーカースレッドの実行を止めない上、
#       shutdown がワーカーの完了を待つため、タイムアウトが事実上効かない
#   (2) 同一プロセス内の複数スレッドが同時に fetch_content.record_facts() を呼ぶと、
#       tmp ファイル名が os.getpid() 固定のため衝突し、記録消失・os.replace 失敗を招く
#       （fetch_content.py の record_facts 実装がシングルライター前提のため）
#   という2つの実害があった。サブプロセス化すればどちらも構造的に起きない）。
#
# attempted/improved/unchanged の判定は「サブプロセスの終了コード」では信用しない。
# fetch_content() は dict を返したときだけ record_facts() を呼び、record_facts() は
# 書き込み失敗を握りつぶして None を返す実装のため、正常終了しても記録が残っていない
# ケースがありうる。子プロセス終了後に fetch_facts/<TARGET>.json を実際に読み直し、
# 次の3条件をすべて満たしたときだけ attempted（improved/unchanged）として数える
# （2026-09-03 Codex 2周目レビュー指摘: URL一致だけでは、fetch_content._facts() が
#  実行時の captures.json から rid を引き直す都合上、URL→rid 対応が変わった／消えた
#  場合に「別rid・rid無し」のレコードを誤って attempted に計上し、かつ元の候補rid
#  自身は当日試行済みと誤認されないまま実際には翌晩も候補に残り続ける、という食い違い
#  が起きうる）:
#   (1) URL一致（_read_record_for(target, url) で保証）
#   (2) 新レコードの raindrop_id が候補の rid と一致（int比較）
#   (3) 子起動前のレコードと異なる（attempt_seq増加 or レコード全体が変化）
# いずれか1つでも満たさなければ failed。(2)を満たさなかった件数は STATUS 行の
# rid_mismatch に別枠で数える。
#
# 子がレコードを一切残せなかった候補(timeout/no_record/例外)には、backfill 自身が
# 最小限の失敗レコード(route="backfill", ok=false, depth="none", raindrop_id=候補rid,
# rid_source="backfill")を record_facts() 経由で書く（2026-09-03 CEO決定・4周目
# レビュー対応）。これをしないと attempt_count が伸びず、毎晩タイムアウトするURLが
# --limit の枠を永久に消費し exhausted に到達しない。書いた件数は STATUS 行の
# stub_written に数える。
#
# rid 無し(unresolved)で子がレコードを書いてしまった場合は、そのレコードに
# backfill_rid キーを1つだけ追記する（record_facts() は使わず attempt_seq を
# 二重加算しない専用の小さな書き込み関数を使う）。rid_source は子が記録した値
# (unresolved)のまま変えない。ledger.py の _effective_rid() はこの backfill_rid も
# 見るため、次回以降このレコードは候補ridのグループに合流し attempt_count が伸びる
# （2026-09-03 CEO決定・4周目レビュー対応）。この場合の STATUS は attempted では
# なく rid_mismatch のまま（改善判定はしない）。
#
# `fail_reason` 導入（2026-09-03）後は、表（ledger.py の _FAIL_REASON_KIND）にあるコードは
# permanent として候補から除外される。自由文 `reason` しか無い旧レコードと og:meta 無し系は
# 依然 unknown のまま max_attempts 回まで再試行されてから exhausted に落ちる。
#
# 使い方:
#   python3 backfill.py --target YYYY-MM-DD [--limit N] [--max-attempts N] [--dry-run] [--timeout SEC]
#   （--max-total SEC で総時間予算。既定 limit×timeout＋30秒・0で無効化。外側シェルの
#     `timeout` が親だけを殺して子を孤児化させる前に、自分で止まるための内部予算）

import sys, os, argparse, datetime, json, io, subprocess, traceback, time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JST = datetime.timezone(datetime.timedelta(hours=9))

DEFAULT_LIMIT = 5
DEFAULT_TIMEOUT = 480

# 内部デッドラインと外側シェル `timeout` の差（秒）。子の終了後にかかる後処理
# （当日JSONの読み直し・stub書き込み・fsync・改善判定）と、subprocess.run が
# timeout 後に子を kill して回収するまでの時間をここで賄う。
# ⚠️ これは「次の子を起動してよいか」の判定には足さない。足すと既定予算
# （limit×timeout＋30）に対して開始条件が limit×timeout＋60 相当になり、候補抽出に
# 掛かったわずかな時間だけで1件目すら起動されず、バックフィルが丸ごと無効化される
# （2026-09-05 に回帰テストで実際に踏んだ）。
CLEANUP_RESERVE_SEC = 30

# --max-total（総時間予算・秒）の既定は limit × timeout ＋ CLEANUP_RESERVE_SEC。
# これは「最後の子がここまでに終わっていてほしい時刻」であって、外側 `timeout`
# （手順書の規約で「limit × timeout ＋60秒以上」）より必ず手前に来る。
# 既定 --timeout 480 なら limit=1 → 予算510 / 外側600、limit=5 → 予算2430 / 外側2500。
# 子を起動する条件は「残り ≥ timeout」だけ。
#
# ⚠️ これは**ベストエフォートであって厳密な保証ではない**（2026-09-05 Codex 4周目
# レビュー指摘を採用し、断定的な表現を撤回した）。理由:
#   ①「残り ≥ timeout」を判定してから subprocess.run が実際に子を起動するまでに
#     必ず微小な経過時間があり、残りがちょうど timeout のときは内部デッドラインを
#     その分だけ越える
#   ② subprocess.run の timeout は、超過後に子を kill して回収し終えるまでの時間を
#     厳密には縛らない
# 実運用では CLEANUP_RESERVE_SEC（30秒）＋外側との差分がこのズレを吸収する想定。
# 厳密に保証したいなら親子を同一プロセスグループ／Job Object で起動し、外側の
# timeout もグループ単位で終了させる必要がある（今回は採らない・既知の限界）。
#
# 副作用として、候補抽出（ledger の全走査）が CLEANUP_RESERVE_SEC を超えて長引くと
# 1件目すら起動されず budget_stopped=<limit> になる。黙って0件になるのではなく
# BACKFILL_STATUS に残るので、無人運用でも成果物側から検知できる。
#
# ⚠️ 子の timeout を残り時間で「頭打ちにする」設計は採らない（2026-09-05 Codex
# 3周目レビュー指摘で撤回）。残り31秒で 480秒かかる候補を起動すると、予算都合の
# 人工的な timeout で殺されて stub が書かれ、attempt_seq が加算される。これを
# 数晩繰り返すと、恒久失敗ではない回収可能なURLが「予算不足だけ」を理由に
# exhausted に落ちて永久に諦められる＝無人運用では回収不能なデータ欠落になる。
# 入る時間が無いなら **起動しない**（試行回数を消費しないので翌晩そのまま候補に残る）。
#
# なぜ内部予算が要るか（2026-09-05 Codex 2周目レビュー指摘）:
# 外側のシェル `timeout` は backfill.py（親）だけを殺し、実行中の
# fetch_content.py（子）はプロセスグループごと殺されないので孤児として生き残る。
# 孤児の子は排他ロックの無い fetch_facts/<日付>.json に書き続けるため、手順を進めた
# 先の手順4の通常取得と同じファイルを同時に read-modify-write し、片方の更新が
# 黙って消える。親が外側 timeout より先に自分で終われば、この経路自体が発生しない。
def _default_max_total(limit, timeout):
    return int(limit) * int(timeout) + CLEANUP_RESERVE_SEC

# depth の順序。none < shallow < partial < full。未知/None はキーに無い＝比較不能。
# CEO確定: depth:"none" は fetch_content.py の _facts() で ok:false のとき実際に
# 代入される実在の値。
_DEPTH_ORDER = {"none": 0, "shallow": 1, "partial": 2, "full": 3}


def _depth_rank(depth):
    """既知の4段階のみランクを返す。未知/None は None（比較不能）。"""
    return _DEPTH_ORDER.get(depth)


# テストが差し替える実行口。None のときだけ本番のサブプロセス実行を使う。
# 本番パスとテストパスの分岐はここ1点だけ（CEO指定）。
# シグネチャ: run_one(url, timeout, env) -> None（成功/失敗は呼び出し側が
# fetch_facts/<TARGET>.json を読み直して判定する。例外/subprocess.TimeoutExpired は
# 呼び出し側が捕捉する）。
RUN_ONE = None

# 本番サブプロセスが起動する子スクリプトのパス。既定は同ディレクトリの fetch_content.py。
# integration テストがこれを差し替えて、本物の subprocess.run 経路（_default_run_one）を
# そのまま通しつつ、実ネットワークに触らないダミー子スクリプトを起動できるようにする
# （2026-09-03 Codex 2周目レビュー指摘: RUN_ONE を丸ごと差し替えるテストは
#  subprocess.run 呼び出しそのもの＝env継承・cwd非依存・timeout後の子プロセス終了を
#  検証できていなかった）。
FETCH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_content.py")


def _default_run_one(url, timeout, env):
    # 子は本文JSONを全量 print することがあり、capture_output=True で親メモリに
    # 溜める意味が無い（診断は fetch_facts のレコードで足りる）。
    # 2026-09-03 Codex 2周目レビュー指摘で DEVNULL に変更。
    subprocess.run([sys.executable, FETCH_SCRIPT, url], env=env, timeout=timeout,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _get_run_one():
    return RUN_ONE if RUN_ONE is not None else _default_run_one


def _facts_dir():
    """ledger.py / fetch_content.py と同じ解決規則（環境変数名も同一）。"""
    return os.environ.get("FETCH_FACTS_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fetch_facts")


def _read_record_for(day, url):
    """fetch_facts/<day>.json を読み直し、指定URLのレコードを三値で返す。
    ⚠️ 「対象URLキーが無い」（ファイルは正常に読めたが、そのURLのレコードがまだ
    存在しない＝stubを書いてよい）と「当日JSONが読めない／dictでない」（ファイルが
    壊れている・レース等＝stubを書くと fetch_content.record_facts() が既存ファイルを
    .broken へ退避してstubだけの新JSONを作ってしまい、同日の他URLレコードがその日の
    ファイルから消える）を同じ None に潰していたのは事故（2026-09-03 5周目レビュー
    指摘）。三値で区別する:
      - ("ok", rec)   … ファイルは正常。該当URLのレコードが存在する
      - ("ok", None)  … ファイルは正常（存在しない＝初回、または対象URLキーが無い）。
                         stub書き込み・backfill_ridタグ付けを行ってよい
      - ("unreadable", None) … ファイルが壊れている/dictでない。stubを書かず
                         backfill_ridタグも付けず、呼び出し側は failed のみ加算する
    ファイル自体が存在しない（初回）場合は「正常に読めて空」と同じ扱い＝("ok", None)。
    このプロセスはシングルライター前提でロックを持ち込まない
    （累積台帳を作らない/ロックで無人ジョブを止めないという Task G-1 の決定を踏襲）。"""
    path = os.path.join(_facts_dir(), "%s.json" % day)
    if not os.path.exists(path):
        return "ok", None
    try:
        with io.open(path, encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        return "unreadable", None
    if not isinstance(store, dict):
        return "unreadable", None
    rec = store.get(url)
    if not isinstance(rec, dict):
        return "ok", None
    return "ok", rec


def _write_stub_record(day, url, rid, reason):
    """子がレコードを一切残せなかった候補(timeout/no_record/例外)に、backfill自身が
    最小限の失敗レコードを書く（2026-09-03 CEO決定・4周目レビュー対応）。
    子は既に終了済みのため並行書込みは起きない。record_facts() 経由で書くので
    attempt_seq は既存レコードから引き継がれる（record_facts の既存ロジックそのまま）。
    ⚠️ _facts() が出す他のキー(elapsed_ms/http_status/text_chars等)は捏造しない。
    ここで書くのは仕様どおりの最小フィールドだけ。
    シングルライター前提・ロックなし（累積台帳を作らない/ロックで無人ジョブを
    止めないという Task G-1 の決定を踏襲。5周目レビューで指摘されたロック導入は
    採らない）。
    戻り値: 書き込めたら True。"""
    try:
        from fetch_content import record_facts, FETCHER_VERSION
    except Exception:
        return False
    facts = {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc)
                              .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "fetcher_version": FETCHER_VERSION,
        "url": url,
        "route": "backfill",
        "ok": False,
        "depth": "none",
        "missing": ["fetch_failed"],
        "reason": reason,
        "raindrop_id": rid,
        "rid_source": "backfill",
    }
    try:
        path_written = record_facts(facts, day=day)
        return path_written is not None
    except Exception:
        return False


def _tag_record_with_backfill_rid(day, url, cand_rid):
    """子が rid 無し(unresolved)でレコードを書いてしまった場合に、backfill_rid
    キーを1つだけ追記する（2026-09-03 CEO決定・4周目レビュー対応）。
    record_facts() は使わない（attempt_seq を二重加算しないため）。
    tmp+fsync+os.replace で原子的に書く（fetch_content.record_facts() と同じ安全策）。
    rid_source は書き換えない（子が記録した rid 解決の事実を偽らない）。
    シングルライター前提・ロックなし（Task G-1 の決定を踏襲。5周目レビューで
    指摘されたロック導入は採らない）。
    戻り値: 書き込めたら True。"""
    facts_dir = _facts_dir()
    path = os.path.join(facts_dir, "%s.json" % day)
    try:
        with io.open(path, encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        return False
    if not isinstance(store, dict):
        return False
    rec = store.get(url)
    if not isinstance(rec, dict):
        return False
    rec = dict(rec)
    rec["backfill_rid"] = cand_rid
    store[url] = rec
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(store, ensure_ascii=False, indent=1) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _default_target():
    # Routine の $TARGET と同じ計算式（JSTの「昨日」）
    now = datetime.datetime.now(JST)
    yesterday = now.date() - datetime.timedelta(days=1)
    return yesterday.isoformat()


def _improved(prev_rec, new_rec):
    """直前(TARGET以前で最新)のレコードと、今回子プロセスが書いた新レコードを比較して
    「改善」か判定する。改善条件: depthが既知の4段階で向上した、または ok が
    False→True に変わった。depth が未知/None で比較不能な場合は improved に数えない
    （unchanged扱い。2026-09-03 Codexレビュー指摘）。"""
    prev_ok = bool(prev_rec.get("ok")) if prev_rec else False
    new_ok = bool(new_rec.get("ok"))
    if (not prev_ok) and new_ok:
        return True
    prev_rank = _depth_rank(prev_rec.get("depth")) if prev_rec else None
    new_rank = _depth_rank(new_rec.get("depth"))
    if prev_rank is not None and new_rank is not None and new_rank > prev_rank:
        return True
    return False


def run(target, limit, max_attempts, dry_run, timeout, max_total=None):
    import ledger

    if max_total is None:
        max_total = _default_max_total(limit, timeout)
    deadline = time.monotonic() + max_total if max_total > 0 else None

    candidates = ledger.backfill_candidates(target, max_attempts=max_attempts)
    total_candidates = len(candidates)
    picked = candidates[:limit]

    # exhausted_skipped（max_attemptsで除外した件数）と rid_changed_skipped（URLが
    # 後日別rid/無ridのレコードに奪われて候補から終端した件数。2026-09-03 Codex 3周目
    # レビュー指摘）は候補抽出とは別に数える。
    exhausted_skipped = 0
    rid_changed_skipped = 0
    try:
        all_records = ledger._load_all_records()
        all_groups = ledger._build_groups(all_records, max_attempts=max_attempts)
        url_index = ledger._url_index(all_records)
        for key, g in all_groups.items():
            if key[0] != "rid":
                continue
            if not ledger._has_solid_rid(g["latest"]):
                continue
            if g["state"] != "open":
                continue
            if g["reason_kind"] == "permanent":
                continue
            if any(r.get("_day") == target for r in g["records"]):
                continue
            if g["exhausted"]:
                exhausted_skipped += 1
                continue
            latest = g["latest"]
            if ledger._rid_changed_after(url_index, latest.get("url"), key[1],
                                          latest.get("fetched_at"), latest.get("_day")):
                rid_changed_skipped += 1
    except Exception:
        exhausted_skipped = 0
        rid_changed_skipped = 0

    if dry_run:
        print("=== backfill --dry-run ===")
        print("target=%s candidates=%d" % (target, total_candidates))
        for g in picked:
            key = g["key"]
            latest = g["latest"]
            print("  rid=%s url=%s reason_kind=%s attempt_count=%s last_attempt_at=%s"
                  % (key[1], latest.get("url"), g["reason_kind"], g["attempt_count"],
                     g["last_attempt_at"]))
        print("BACKFILL_STATUS: target=%s candidates=%d attempted=0 improved=0 unchanged=0 "
              "failed=0 exhausted_skipped=%d rid_mismatch=0 rid_changed_skipped=%d stub_written=0 "
              "unreadable_day_file=0 budget_stopped=0"
              % (target, total_candidates, exhausted_skipped, rid_changed_skipped))
        return 0

    run_one_fn = _get_run_one()

    attempted = 0
    improved = 0
    unchanged = 0
    failed = 0
    rid_mismatch = 0
    stub_written = 0
    unreadable_day_file = 0
    budget_stopped = 0

    for idx, g in enumerate(picked):
        # 総時間予算のチェック（2026-09-05 追加）。この候補を短縮せずに1回分
        # （timeout 秒）走らせる時間が残っていなければ、起動せずに打ち切る。
        # 残件数を budget_stopped として成果物側の
        # BACKFILL_STATUS に残す（無人運用ではログを誰も読まないため、打ち切りが
        # 起きたこと自体が観測できないと「何件目で止まったか」が失われる）。
        # 起動しなかった候補は試行回数を消費しないので、翌晩そのまま候補に残る。
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining < timeout:
                budget_stopped = len(picked) - idx
                break

        key = g["key"]
        rid = key[1]
        latest = g["latest"]
        url = latest.get("url")

        child_env = dict(os.environ)
        child_env["FACTS_DATE"] = target
        # FETCH_FACTS_DIR は os.environ に既にあれば dict(os.environ) コピーで
        # 自動的に子へ引き継がれる。未設定（本番）ならこのキー自体が無いので
        # fetch_content.py 側の既定値（リポ直下 fetch_facts/）がそのまま使われる。

        # 子起動**前**に TARGET ファイル内の対象URLレコードを退避する。
        # fetch_content._facts() は実行時の captures.json から rid を引き直すため、
        # URL→rid 対応が変わった／消えた場合に「別rid・rid無し」のレコードが
        # 同じURLキーで書かれることがある。それを見逃すと、別rid・rid無しのレコードを
        # 誤って attempted に計上してしまい、かつ元の候補rid自身は「当日試行済み」と
        # 誤認されないまま実際には今日試行されていないのに翌晩も候補に残り続ける
        # （2026-09-03 Codex 2周目レビュー指摘）。
        before_status, before_rec = _read_record_for(target, url)
        if before_status == "unreadable":
            # 起動前の時点で当日JSONが読めない/dictでない場合、子プロセスを
            # 起動しない（2026-09-04 Codex 6周目レビュー・出荷停止級対応）。
            # ここで子を起動すると、子(fetch_content.record_facts())が壊れた
            # 当日JSONを .broken へ退避して空JSONを書き直してしまい、post-run側の
            # unreadable チェック（stubを書かない対策）で防いだはずの事故が
            # 子の通常保存経路で再発する。
            unreadable_day_file += 1
            failed += 1
            continue

        # 子が例外/timeoutで終わっても、直後の「レコードが残っているか」チェックに
        # そのまま進む（2026-09-03 CEO決定・4周目レビュー対応: 子がレコードを一切
        # 残せなかった候補には backfill 自身が失敗レコードを書く。そのためには
        # 例外発生時も stub_reason だけ記録して、以降の共通処理へ合流させる必要がある）。
        stub_reason = None
        try:
            run_one_fn(url, timeout, child_env)
        except subprocess.TimeoutExpired:
            stub_reason = "backfill_timeout"
        except Exception as e:
            stub_reason = "backfill_exception:%s" % type(e).__name__

        new_status, new_rec = _read_record_for(target, url)
        if new_status == "unreadable":
            # 当日JSONが読めない/dictでない（壊れている・レース等）。ここで
            # stubを書くと fetch_content.record_facts() が既存ファイルを .broken へ
            # 退避してstubだけの新JSONを作ってしまい、同日の他URLレコードがその日の
            # ファイルから消える実害がある。stub書き込み・backfill_ridタグ付けの
            # どちらも行わず、failedのみ加算して次のURLへ進む
            # （2026-09-03 5周目レビュー指摘・出荷停止級）。
            unreadable_day_file += 1
            failed += 1
            continue
        if new_rec is None:
            # ファイルは正常に読めたが、対象URLのレコードがまだ存在しない
            # （子が例外/timeoutだった、またはサブプロセスは異常終了しなかったが
            # record_facts が書き込みに失敗した等）ケース。
            # レコードが1件も残っていないと、このURLは翌晩以降も attempt_count が
            # 増えないまま毎晩 --limit の枠を消費し続けてしまうため、backfill 自身が
            # 最小限の失敗レコード(route=backfill)を書く（2026-09-03 CEO決定）。
            if stub_reason is None:
                stub_reason = "backfill_no_record"
            if _write_stub_record(target, url, rid, stub_reason):
                stub_written += 1
            failed += 1
            continue

        # attempted の3条件（すべて満たさなければ failed / rid_mismatch）:
        # (1) URL一致 … _read_record_for(target, url) で既にURLキー一致は保証済み
        # (2) raindrop_id が候補の rid と一致（int比較）
        # (3) 起動前のレコードと異なる（attempt_seq が増えている、またはレコード全体が変化）
        new_rid = new_rec.get("raindrop_id")
        rid_ok = isinstance(new_rid, int) and new_rid == rid
        if not rid_ok:
            rid_mismatch += 1
            if not isinstance(new_rid, int):
                # rid 無し(unresolved)で子が書いてしまったケース。2026-09-03 CEO決定・
                # 4周目レビュー対応: backfill_rid タグを追記し、次回以降このレコードが
                # 候補ridのグループに合流して attempt_count が伸びるようにする
                # （rid_source は変えない＝rid解決の事実を偽らない）。
                _tag_record_with_backfill_rid(target, url, rid)
            failed += 1
            continue

        changed = (before_rec is None) or (before_rec != new_rec)
        if not changed:
            # 子が起動前と全く同じレコードを書いた（何も試行していない）とみなし
            # attempted に数えない。
            failed += 1
            continue

        attempted += 1
        try:
            if _improved(latest, new_rec):
                improved += 1
            else:
                unchanged += 1
        except Exception:
            unchanged += 1

    print("BACKFILL_STATUS: target=%s candidates=%d attempted=%d improved=%d unchanged=%d "
          "failed=%d exhausted_skipped=%d rid_mismatch=%d rid_changed_skipped=%d stub_written=%d "
          "unreadable_day_file=%d budget_stopped=%d"
          % (target, total_candidates, attempted, improved, unchanged, failed,
             exhausted_skipped, rid_mismatch, rid_changed_skipped, stub_written,
             unreadable_day_file, budget_stopped))
    return 0


def main(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--target", default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-attempts", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-total", type=int, default=None,
                        help="総時間予算(秒)。既定は limit×timeout。0で無効化。")
    args = parser.parse_args(argv)

    target = args.target or _default_target()
    os.environ["FACTS_DATE"] = target

    try:
        import ledger
        max_attempts = args.max_attempts if args.max_attempts is not None else ledger.DEFAULT_MAX_ATTEMPTS
    except Exception:
        max_attempts = args.max_attempts if args.max_attempts is not None else 3

    try:
        return run(target, args.limit, max_attempts, args.dry_run, args.timeout,
                   max_total=args.max_total)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print("BACKFILL_STATUS: target=%s candidates=0 attempted=0 improved=0 unchanged=0 "
              "failed=0 exhausted_skipped=0 rid_mismatch=0 rid_changed_skipped=0 stub_written=0 "
              "unreadable_day_file=0 budget_stopped=0 error=%s" % (target, type(e).__name__))
        return 0


if __name__ == "__main__":
    rc = 0
    try:
        rc = main(sys.argv[1:])
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print("BACKFILL_STATUS: target=unknown candidates=0 attempted=0 improved=0 unchanged=0 "
              "failed=0 exhausted_skipped=0 rid_mismatch=0 rid_changed_skipped=0 stub_written=0 "
              "unreadable_day_file=0 budget_stopped=0 error=%s" % type(e).__name__)
        rc = 0
    sys.stdout.flush()
    sys.exit(rc if isinstance(rc, int) else 0)
