#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""夜間Routine（cloud_routine_prompt.md）の手順ごとの所要時間を計測する。

なぜ要るか:
  成功条件「1ランの所要時間が20分以内」を評価しようにも、これまで記録が
  あったのは**ラン全体の時間だけ**（474〜758秒）で、手順ごとの内訳が一度も
  測られていなかった。内訳が無いと「手順2.5のバックフィルに何秒割けるか」を
  逆算できず、枠拡大（--limit）の値を決められない。まず内訳を集める。

出すもの（schema=v2）:
  各手順の**所要時間そのもの**（`<step>_s`）と `total_s`。累積秒ではない。
  `status=incomplete` のときも、測れている区間の `<step>_s` は出る（`total_s` は出ない）。
  v1 は `t0` からの累積を `stepN=` として出していたが、それは「各手順の所要時間」
  ではなく、差を取る規約も成果物に書かれていなかった（2026-09-08 Codex 5周目 P0）。
  区間の対応は下記のとおりで、**mark を打っていない手順は隣の区間に含まれる**:
    step1_s   = 手順1 + 1.5（対象日決定・依存インストール）
    step2_s   = 手順2（Raindrop取り込み）
    step2_5_s = 手順2.5（バックフィル）  ← 枠を決めたいのはここ
    step3_s   = 手順3（当日レコード抽出）
    step4_s   = 手順4（URL分類＋取得。動画理解が走る重い区間）
    step4_5_s = 手順4.5（エンリッチ）
    step5_s   = 手順5 + 6（reviews生成。手順6はindex.htmlに触らない無処理）
    step7_s   = 手順7（自己検証。finish 実行時点までで打ち切る）
    total_s   = 手順1〜7の合計
  **手順8以降（push・Vaultリポ反映）は含まれない**（コメントを書く時点で未実行）。
  `total_s` を「ラン全体の所要」と読み替えないこと。

設計の原則（Codex 敵対的レビュー5周で削り出した）:
  1. **値は全て自分で作る。** LLMに数値を書かせない・コメント行を貼らせない。
     手で埋める欄があると、捏造・記入漏れ・`-->` を含む値によるHTMLコメントの
     早期終了（→ `<div class="meta">` 注入で _build_feed.py の抽出を汚せる）が起きる。
  2. **黙って古い値を残さない。** finish がどこで失敗しても、必ず
     `status=incomplete reason=...` のコメントへ置換する。
  3. **疑わしきは incomplete。** 順序不一致・重複・欠測・不正なtarget・別ランの記録は、
     もっともらしい数字を出さずに incomplete と理由を残す。
     ただし **両端が実在する mark で挟まれた区間だけは incomplete でも出す**
     （2026-09-18 追加。条件は `_validate` の該当コメント）。`status` は incomplete のまま、
     `reason=` も消さない。欠測を跨いだ合成値と `total_s` は出さない。
     きっかけ: 2026-09-17 の初回実測が `missing-step4_5` の1件で全区間を失い、
     枠を決めたい `step2_5_s` まで消えた（全か無かでは目的を達成できない）。
  4. **手順を止めない。** どんな例外でも exit 0。計測は振り返り本体より優先度が低い。
  5. **成果物を壊さない。** reviews への書き込みは一時ファイル＋os.replace で原子的に。
     既存コメントの削除は「最後の `</footer>` の直前に連なる正規形」だけに限る。
  6. **状態の読み書きはロックで直列化する。** read-modify-write を素で行うと、
     「A が読む → B が start → A が古い state を書き戻して B の開始を消す」で
     偽の complete が作れた（2026-09-08 Codex 5周目 P0）。

使い方（手順書側から呼ぶ）:
  python3 run_timing.py start  --target YYYY-MM-DD
      → `RUN_TIMING_RUN_ID: <8桁>` を返す。以降の全コマンドに --run-id で渡す
  python3 run_timing.py mark   <step名> --run-id <8桁>
      ※ --run-id は mark / finish の**必須引数**。省略すると記録せず complete にもしない
  python3 run_timing.py finish --target YYYY-MM-DD --run-id <8桁> \
                               --reviews reviews/YYYY-MM-DD.html [--saves N]

  状態ファイル .run_timing.json・ロック .run_timing.lock は作業用（.gitignore 済み）。

  ⚠️ `saves=`（2026-09-22 CEO裁定で意味が変わった。schema バージョンは
  v2 のまま据え置き、フィールドの意味だけが変わる点に注意）:
    旧: captures.json の「date == TARGET」件数（手順3の旧仕様と同じ数え方）。
    新: select_targets.py の選定件数（その夜 reviews へ書くべき、まだどの
    reviews にも載っていない保存の件数）。`--saves N` を明示すれば手順書側の
    数値をそのまま使い、省略時だけ finish が自分で select_targets.select() を
    呼んで数える（二重に選定を走らせたくない場合や select_targets 側の失敗を
    切り分けたい場合は明示指定を使う）。過去に書かれた schema=v2 のコメント
    にある `saves=` は旧い数え方の値であり、このスクリプトを更新した日以降の
    ランだけが新しい意味を持つ（コメント自体にその区別は残らないので、過去
    ログを読むときは日付で判断すること）。

既知の限界（直せないので明記する）:
  - `mark` を打つのは無人LLMなので、実作業のあとにまとめて打たれた場合は
    「全区間が0秒」という形になる。`suspicious_zero` で incomplete にはするが、
    **LLMが後追いで打たないことをこの設計だけで保証はできない**。
  - `finish` が reviews を書けなかった場合、**古いコメントを消せない**。
    「計測失敗でも手順を止めない」と「古い complete を絶対に残さない」は同時に
    満たせないので前者を優先し、`RUN_TIMING: WRITE_FAILED` を出力する。
    なお残るのは**同じ対象日の別ラン（run_id が違う）が実際に測った実測値**であって
    捏造ではない。どのランのものかは `run_id=` で判別できる。
"""

import datetime
import json
import os
import re
import sys
import time
import uuid

STATE_FILE = ".run_timing.json"
LOCK_FILE = ".run_timing.lock"

LOCK_WAIT_SEC = 10
# 1ランの所要がこれを超えたら計測として成立していないとみなす（実測の最長は112分）。
MAX_RUN_SEC = 6 * 60 * 60

# 手順書が mark する順序。finish はこの**順序どおりに各1回ずつ**揃ったときだけ
# status=complete を出す。ここを変えるときは cloud_routine_prompt.md の mark 行も同時に変える。
# 末尾の "end" は finish 自身が打つ（手順7の終わり）。
MARKS = ["step1", "step2", "step2_5", "step3", "step4", "step4_5", "step5", "step7"]
REQUIRED_STEPS = MARKS + ["end"]
# 手順書側から `mark` で打つのはこれだけ（step1 は start が、end は finish が打つ）。
MARKABLE = MARKS[1:]

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RUN_ID = re.compile(r"^[0-9a-f]{8}$")
# 出力する値の最終防壁。値は全てこちらで作るので通常は素通りする。
_SAFE_TOKEN = re.compile(r"^[0-9A-Za-z_,\-]+$")

# 既存コメントの削除対象は「1行に収まった正規形」だけ。改行を跨がせない（re.S を使わない）。
# schema は v1/v2… どれでも剥がせるようにする（形式を変えたときに前版が残らないように）。
COMMENT_RE = re.compile(r"[ \t]*<!-- run-timing schema=v\d+ [0-9A-Za-z_=,\- ]*-->[ \t]*\r?\n?")
# footer より前に**閉じていない** run-timing コメントがあると、新しく入れる行も
# `</footer>` もHTMLパーサー上その内側に入って無効化される。書かずに失敗させる。
UNCLOSED_RE = re.compile(r"<!-- run-timing(?![0-9A-Za-z_=,\- ]*-->)")


def _now():
    return int(time.time())


def _atomic_write(path, text):
    """一時ファイル＋os.replace。途中停止で中身が空・途中までになるのを防ぐ。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ロックは**OSの advisory lock**（Linux: fcntl.flock / Windows: msvcrt.locking）で取る。
# ロックファイルの作成・削除で所有権を管理する自前実装はやめた（2026-09-08 Codex 7周目 P0）:
#   - トークンの compare-and-delete は原子的でない。「読んだ時点の所有者」を確認するだけで、
#     unlink する対象が同じ世代である保証がなく、他ランのロックを消せた
#   - **削除しない**ので、Windows で待機側がファイルを開いていると所有者の unlink が
#     共有違反で失敗する、という自分で踏んだ孤児化も原理的に起きない
#   - プロセスが異常終了しても OS が解放するので、stale の回収（一定時間で奪う）が要らない。
#     「生きている所有者を勝手に締め出す」経路も同時に消える
# 解放は fd を閉じるだけ。ロックファイルは残るが .gitignore 済みで成果物ではない。
try:
    import fcntl as _fcntl

    def _lock_op(fd, acquire):
        if acquire:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        else:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
except ImportError:
    try:
        import msvcrt as _msvcrt

        def _lock_op(fd, acquire):
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK if acquire else _msvcrt.LK_UNLCK, 1)
    except ImportError:
        _lock_op = None


class _Lock(object):
    """取れたら真、取れなければ偽。解放は fd を閉じるだけ。"""

    def __init__(self):
        self.fd = None

    def __bool__(self):
        return self.fd is not None

    __nonzero__ = __bool__


def _acquire_lock():
    """OS の advisory lock を取る。取れなければ空の _Lock を返す。"""
    lock = _Lock()
    if _lock_op is None:
        # ロック機構が無い環境。無人ランは単発起動なので直列前提で続行する。
        return lock
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return lock
    deadline = time.monotonic() + LOCK_WAIT_SEC
    while True:
        try:
            _lock_op(fd, True)
            lock.fd = fd
            return lock
        except OSError:
            if time.monotonic() > deadline:
                os.close(fd)
                return lock
            time.sleep(0.05)


def _release_lock(lock):
    if not lock or lock.fd is None:
        return
    try:
        _lock_op(lock.fd, False)
    except OSError:
        pass
    try:
        os.close(lock.fd)
    except OSError:
        pass
    lock.fd = None


def _read_state():
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state, dict):
        raise ValueError("state is not an object")
    return state


def _write_state(state):
    _atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False))


def cmd_start(target):
    """計測開始。**必ず作り直す**（前ラン・同日再実行の記録を引き継がない）。

    発行した run_id を `RUN_TIMING_RUN_ID:` で返す。以降の `mark`/`finish` は
    これを `--run-id` で受け取り、state の run_id と一致しなければ記録しない／
    complete にしない。
    """
    lock = _acquire_lock()
    if not lock and _lock_op is not None:
        print("RUN_TIMING: lock busy; measurement not started")
        return
    try:
        run_id = uuid.uuid4().hex[:8]
        t0 = _now()
        state = {"schema": "v2", "run_id": run_id, "target": target, "t0": t0,
                 "steps": [{"name": "step1", "at": t0, "run_id": run_id}]}
        _write_state(state)
    finally:
        _release_lock(lock)
    print("RUN_TIMING: started target=%s" % target)
    print("RUN_TIMING_RUN_ID: %s" % run_id)


def cmd_mark(name, run_id):
    """`--run-id` は**必須**。省略・不一致なら記録しない。

    読み取り→追記→書き戻しはロックの中で行う。素でやると
    「A が読む → B が start → A が古い state を書き戻して B の開始を消す」経路が残る。
    """
    if name not in MARKABLE:
        print("RUN_TIMING: ignored unknown step %r" % name)
        return
    if not (isinstance(run_id, str) and _RUN_ID.match(run_id or "")):
        print("RUN_TIMING: --run-id required; %s not recorded" % name)
        return
    lock = _acquire_lock()
    if not lock and _lock_op is not None:
        print("RUN_TIMING: lock busy; %s not recorded" % name)
        return
    try:
        state = _read_state()
        if state.get("run_id") != run_id:
            # 自分の start 以降に別ランが start をやり直している。相手の記録を汚さない。
            print("RUN_TIMING: run_id mismatch; %s not recorded" % name)
            return
        state.setdefault("steps", []).append(
            {"name": name, "at": _now(), "run_id": run_id})
        _write_state(state)
    finally:
        _release_lock(lock)
    print("RUN_TIMING: mark %s" % name)


def _count_saves_via_select_targets(target):
    """`--saves` が明示されなかったときの既定の数え方。

    2026-09-22 CEO裁定で、手順3の振り返り対象が「captures.json の
    date == TARGET」から select_targets.py の選定（まだどの reviews にも
    載っていない保存全件）へ変わったため、`saves=` も同じ選定件数
    （select_targets.select() の `_selected_count`）で数える
    （旧 date==target 方式は削除。同じ数え方を2箇所に書かない）。
    人手で埋めさせない方針は変わらない：値は必ずここで機械的に作る。
    select_targets 側は自身の失敗を例外にせず `selected=0` 等に握りつぶす
    設計（無人ランのgateを止めない流儀）なので、ここで拾えない失敗は
    import 自体の失敗などに限られる。それも含めて例外はすべて呼び出し側
    （cmd_finish）で捕捉し、拾えなければ saves=unknown にする。
    """
    import select_targets
    result = select_targets.select(target)
    return result["_selected_count"]


def _is_int(v):
    """bool は int のサブクラスなので isinstance では弾けない。True が時刻として通る。"""
    return type(v) is int


def _is_real_date(s):
    """形だけでなく暦としても正しいか（`2026-02-30` / `2026-99-99` を弾く）。"""
    if not (isinstance(s, str) and _ISO_DATE.match(s)):
        return False
    try:
        datetime.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _validate(state, target, expected_run_id, end_at):
    """(区間名→所要秒, 理由コードのリスト) を返す。例外は投げない。"""
    reasons = []
    if not _is_real_date(target):
        reasons.append("bad_target")
    if state.get("schema") != "v2":
        reasons.append("bad_schema")
    run_id = state.get("run_id")
    if not (isinstance(run_id, str) and _RUN_ID.match(run_id or "")):
        reasons.append("bad_run_id")
    if not (isinstance(expected_run_id, str) and _RUN_ID.match(expected_run_id or "")):
        reasons.append("run_id_missing")
    elif run_id != expected_run_id:
        reasons.append("run_id_mismatch")
    if state.get("target") != target:
        # start した対象日と finish の対象日が違う＝別ランの状態を拾っている。
        reasons.append("target_mismatch")
    t0 = state.get("t0")
    if not _is_int(t0) or t0 <= 0:
        reasons.append("bad_t0")

    # この run_id の mark だけを、記録順のまま拾う。
    seq = []
    for s in state.get("steps") or []:
        if not isinstance(s, dict):
            reasons.append("bad_step_record")
            continue
        if s.get("run_id") != run_id:
            continue  # 別ランの記録は黙って捨てる（混ぜない）
        if not _is_int(s.get("at")):
            reasons.append("bad_step_time")
            continue
        if _is_int(t0) and s["at"] < t0:
            reasons.append("step_before_t0")
            continue
        seq.append((s.get("name"), s["at"]))
    if _is_int(end_at):
        seq.append(("end", end_at))

    names = [n for n, _ in seq]
    if names == REQUIRED_STEPS and _is_int(t0) and seq[0][1] != t0:
        # step1 は start が t0 で置く基準点。ここがずれていると total_s が
        # 「開始からの実測」でなくなる（未計測の区間を黙って捨てた値になる）。
        reasons.append("step1_not_t0")
    if _is_int(end_at) and any(t > end_at for _, t in seq):
        reasons.append("time_in_future")
    if _is_int(end_at) and _is_int(t0) and not (0 <= end_at - t0 <= MAX_RUN_SEC):
        # 並びが整っていても `t0=1`（1970年）のような state は作れてしまい、
        # 巨大な total_s が complete として公開されうる（Codex 7周目 P1）。
        # 1ランがこの幅を超えることは運用上あり得ないので、計測不成立とみなす。
        reasons.append("implausible_span")
    if names != REQUIRED_STEPS:
        missing = [x for x in REQUIRED_STEPS if x not in names]
        dup = sorted({x for x in names if names.count(x) > 1})
        if missing:
            reasons.append("missing:" + ",".join(missing))
        if dup:
            reasons.append("duplicated:" + ",".join(dup))
        if not missing and not dup:
            reasons.append("wrong_order")
    times = [t for _, t in seq]
    if any(a > b for a, b in zip(times, times[1:])):
        reasons.append("nonmonotonic")

    # 所要時間＝隣接する mark の差。累積ではない。
    #
    # 揃っていないラン（`status=incomplete`）でも、**両端が実在する mark で挟まれた区間
    # だけ**は出す（2026-09-18 追加）。理由: 2026-09-17 の初回実測は `step4_5` の mark が
    # 1つ欠けただけで全区間が捨てられ、枠（`--limit`）を決めたい `step2_5_s` まで消えた。
    # 欠測1つで実測値が全滅する形だと、目的（手順2.5 に何秒割けるかを決める）が達成できない。
    # 出す条件は次の全てで、1つでも欠ければその区間は黙って出さない（推定はしない）:
    #   - 記録順で隣り合っている（間に他の mark が挟まっていない）
    #   - その2つが REQUIRED_STEPS でも隣り合っている（欠測を跨いで区間を合成しない。
    #     例: step4_5 が無いとき step4→step5 の差を `step4_s` として出さない）
    #   - どちらの名前もこのランに1回しか現れない（重複があると、どの出現の差か決まらない）
    #   - 差が負でない
    # `total_s` は従来どおり**全部揃ったときだけ**出す（欠測を含む合計は「1〜7の所要」ではない）。
    durations = {}
    local_only = all(r.startswith("missing:") or r.startswith("duplicated:")
                     for r in reasons)
    if names == REQUIRED_STEPS and not reasons:
        for (name, a), (_, b) in zip(seq, seq[1:]):
            durations[name] = b - a
        durations["total"] = seq[-1][1] - seq[0][1]
    elif local_only:
        # 部分値を出してよいのは**欠測・重複という局所的な理由だけ**のとき。
        # run_id_mismatch / target_mismatch / bad_t0 / step_before_t0 / nonmonotonic /
        # time_in_future / implausible_span / bad_step_record / bad_schema などは
        # 「その値がこのランのものである」ことすら怪しい＝全区間を出さない
        # （2026-09-18 Codex レビュー P1。局所欠測だけを許容する）。
        order = {n: i for i, n in enumerate(REQUIRED_STEPS)}
        for (name, a), (nxt, b) in zip(seq, seq[1:]):
            if names.count(name) != 1 or names.count(nxt) != 1:
                continue
            if name not in order or nxt not in order:
                continue
            if order[nxt] - order[name] != 1:
                continue
            if b < a:
                continue
            durations[name] = b - a
    return durations, reasons


def build_comment(state, target, saves, expected_run_id=None, end_at=None):
    """run-timing コメントを1行組み立てる。値は全てここで作る。

    status=complete は「必須の mark が順序どおり各1回ずつ・時刻が非減少・
    target と state が妥当・saves が整数」のときだけ。それ以外は incomplete と reason=。
    **欠測を 0秒 と区別できる形にすることが目的**（キーが無いだけだと 0 で補完されうる）。
    """
    if not isinstance(state, dict):
        state = {}
    if end_at is None:
        end_at = _now()
    durations, reasons = _validate(state, target, expected_run_id, end_at)
    if not _is_int(saves) or saves < 0:
        reasons.append("saves_unknown")
    if durations and all(v == 0 for v in durations.values()):
        # 全区間0秒＝実作業のあとに mark をまとめて打った疑い。
        reasons.append("suspicious_zero")

    # ⚠️ **秒数を出すかどうかは、reason が出揃ってから最終判定する。**
    # `_validate()` の中だけで決めていたときは、あとから build_comment 側で足される
    # `saves_unknown` / `suspicious_zero` が durations を落とせず、`suspicious_zero`
    # （全区間0秒＝後追い mark の疑い）が付いた状態で全区間値と total_s が公開された
    # （2026-09-18 Codex 2周目 P1）。
    # 秒数を落とさない reason は `saves_unknown` だけ——これは保存件数が数えられなかった
    # という意味で、時刻そのものの信頼性には関係しないため。
    if any(r != "saves_unknown" and not r.startswith("missing:")
           and not r.startswith("duplicated:") for r in reasons):
        durations = {}

    ok = not reasons
    fields = [("schema", "v2"), ("status", "complete" if ok else "incomplete"),
              ("target", target if isinstance(target, str) and target else "unknown"),
              ("run_id", state.get("run_id") or "unknown"),
              ("saves", str(saves) if _is_int(saves) and saves >= 0 else "unknown")]
    for s in MARKS:
        if s in durations:
            fields.append(("%s_s" % s, str(durations[s])))
    if "total" in durations:
        fields.append(("total_s", str(durations["total"])))
    if reasons:
        # `:` は _SAFE_TOKEN に無いので `-` へ。区切りは `,`（例: missing-step5,wrong_order）。
        fields.append(("reason", ",".join(r.replace(":", "-") for r in reasons)))

    # 最終防壁。1つでも弾かれたら status を complete のままにしない。
    sanitized = []
    tainted = False
    for key, val in fields:
        val = str(val)
        if not _SAFE_TOKEN.match(val):
            val = "invalid"
            tainted = True
        sanitized.append((key, val))
    if tainted:
        sanitized = [(k, "incomplete" if k == "status" else v) for k, v in sanitized]
    return "<!-- run-timing %s -->" % " ".join("%s=%s" % kv for kv in sanitized)


# 末尾の空白のみの行（コメントとコメントの間・コメントとfooterの間の見た目上の
# 区切り）を剥がすための正規表現。本文（非空白を含む行）には決してマッチしない。
_TRAILING_BLANK_LINES_RE = re.compile(r"(?:[ \t]*\r?\n)+\Z")


def _strip_trailing_blank_lines(s):
    """末尾の空白のみの行を取り除いた文字列を返す。マッチが無ければ s をそのまま返す。"""
    m = _TRAILING_BLANK_LINES_RE.search(s)
    if not m:
        return s
    return s[:m.start()]


def insert_comment(html, comment):
    """最後の `</footer>` 直前に run-timing コメントをちょうど1件だけ置く。

    削除するのは**最後の `</footer>` の直前に連なっている正規形**だけ。文書全体を
    置換すると、本文の `<script>`/`<pre>` にたまたま同形の文字列があるとき、それを
    消してJSを壊せる。壊れた（閉じていない）コメントが footer より前にあるときは
    **書き込み自体を諦める**（新しい行も `</footer>` もその内側に入って無効化されるため）。
    `</footer>` が文書中にちょうど1個でない場合も同様に諦める（`rfind` は最後の1個しか
    見ないため、2個以上あると `</footer>` の場所を一意に特定できず誤挿入しうる）。
    改行コードは元のHTMLに合わせる（CRLF の reviews を LF に書き換えない）。
    戻り値は (新HTML or None, 消した既存件数)。
    """
    if html.count("</footer>") != 1:
        return None, 0
    idx = html.find("</footer>")
    if UNCLOSED_RE.search(html[:idx]):
        return None, 0
    head, tail = html[:idx], html[idx:]
    existing = 0
    while True:
        # コメント間・コメントとfooterの間に空白のみの行（空行）が挟まっていても
        # 剥がせるように、末尾の空白のみの行をいったん取り除いてから判定する。
        # 非空白の本文行はこの関数では取り除かれないので、正規形コメントが直前に
        # 無ければ何も変わらずループを抜ける（本文を誤って消さない）。
        candidate = _strip_trailing_blank_lines(head)
        matches = list(COMMENT_RE.finditer(candidate))
        # search() だと本文の <script> 等にある同形の文字列を先に拾ってしまうので、
        # 常に**最後の**マッチを見て、それが末尾に接しているときだけ剥がす。
        if not matches or matches[-1].end() != len(candidate):
            break
        head = candidate[:matches[-1].start()]
        existing += 1
    eol = "\r\n" if "\r\n" in html else "\n"
    return head + comment + eol + tail, existing


def cmd_finish(target, reviews_path, expected_run_id=None, saves=None):
    """**必ずコメントを1件書く**（失敗しても古い値を残さない）。

    ただし reviews 自体が読めない・書けない場合だけは古い値を消せない。
    その事実を `RUN_TIMING: WRITE_FAILED` として標準出力に出す。

    saves: 呼び出し側（手順書の `--saves N`）が明示した保存件数。None
    （未指定）なら select_targets.select() の選定件数を自分で数える
    （2026-09-22 CEO裁定。--saves を明示で渡せるのは、選定を二重に
    走らせたくない場合や select_targets 側の失敗を切り分けたい場合のため）。
    """
    end_at = _now()
    try:
        state = _read_state()
    except Exception as e:
        print("RUN_TIMING: state unreadable (%s)" % type(e).__name__)
        state = {"schema": "broken"}
    if saves is None:
        try:
            saves = _count_saves_via_select_targets(target)
        except Exception as e:
            print("RUN_TIMING: saves count failed (%s)" % type(e).__name__)
            saves = None

    try:
        comment = build_comment(state, target, saves, expected_run_id, end_at)
    except Exception as e:
        comment = ("<!-- run-timing schema=v2 status=incomplete target=unknown "
                   "run_id=unknown saves=unknown reason=build_failed -->")
        print("RUN_TIMING: build_comment failed (%s)" % type(e).__name__)
    print("RUN_TIMING_LINE: %s" % comment)

    if not reviews_path:
        print("RUN_TIMING: WRITE_FAILED no --reviews given")
        return
    try:
        # newline="" で読む＝改行コードを正規化させない。既定の読み方だと CRLF が
        # LF に潰れ、書き戻しでファイル全体の改行が変わってしまう。
        with open(reviews_path, "r", encoding="utf-8", newline="") as f:
            html = f.read()
    except Exception as e:
        print("RUN_TIMING: WRITE_FAILED cannot read %s (%s)"
              % (reviews_path, type(e).__name__))
        return
    new_html, existing = insert_comment(html, comment)
    if new_html is None:
        print("RUN_TIMING: WRITE_FAILED no unique </footer> or unclosed run-timing "
              "comment in %s" % reviews_path)
        return
    # ⚠️ **書く直前に読み直し、読んだときから変わっていないことを確かめる。**
    # これが無いと、A が古い本文を読んで止まっている間に B が新しい reviews を書き、
    # A が「古い本文＋計測コメント」で丸ごと置換して**B の成果物を消せる**
    # （2026-09-08 Codex 7周目 P0）。計測の遅れが振り返り本体へ波及する経路なので、
    # 変化を見つけたら書かずに諦める。厳密な原子性ではないが、競合窓は
    # 「読んでからコメントを組み立てるまで」から「読み直してから replace まで」へ縮む。
    # 残存窓: 読み直し〜os.replace の間に別の書き手が割り込む余地は、依然として
    # 厳密には排除できていない（Codex 敵対的レビュー指摘）。厳密な排他には reviews
    # への全書き手（check_disclosure.py 等）で共有するロックが要るが、それは本変更の
    # スコープ外とする。夜間Routineは単一プロセスが逐次実行する前提であり、
    # 同一 reviews ファイルへの並行書き込みは実運用では想定していない。
    try:
        with open(reviews_path, "r", encoding="utf-8", newline="") as f:
            if f.read() != html:
                print("RUN_TIMING: WRITE_FAILED %s changed since read; not written"
                      % reviews_path)
                return
    except Exception as e:
        print("RUN_TIMING: WRITE_FAILED cannot re-read %s (%s)"
              % (reviews_path, type(e).__name__))
        return
    try:
        _atomic_write(reviews_path, new_html)
    except Exception as e:
        # 書けなかった＝古いコメントが残る可能性がある。必ず言う。
        print("RUN_TIMING: WRITE_FAILED %s (%s); stale comment may remain"
              % (reviews_path, type(e).__name__))
        return
    print("RUN_TIMING: wrote 1 comment (removed %d existing) -> %s"
          % (existing, reviews_path))


def _arg(argv, flag):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv):
    if not argv:
        print("RUN_TIMING: no subcommand")
        return
    sub = argv[0]
    if sub == "start":
        cmd_start(_arg(argv, "--target"))
    elif sub == "mark":
        cmd_mark(argv[1] if len(argv) > 1 else "", _arg(argv, "--run-id"))
    elif sub == "finish":
        saves_arg = _arg(argv, "--saves")
        saves = None
        if saves_arg is not None:
            try:
                saves = int(saves_arg)
            except ValueError:
                print("RUN_TIMING: ignored invalid --saves %r" % saves_arg)
        cmd_finish(_arg(argv, "--target"), _arg(argv, "--reviews"),
                   _arg(argv, "--run-id"), saves)
    else:
        print("RUN_TIMING: unknown subcommand %r" % sub)


if __name__ == "__main__":
    # 計測は振り返り本体より優先度が低い。**どんな例外でも手順を止めない**
    # （「計測が失敗しても手順は止めない」という手順書の方針をコード側で保証する）。
    try:
        main(sys.argv[1:])
    except Exception as e:
        print("RUN_TIMING: failed (%s: %s)" % (type(e).__name__, e))
    sys.exit(0)
