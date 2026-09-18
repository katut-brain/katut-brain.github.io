#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_timing.py の回帰テスト（標準ライブラリ unittest のみ）。

pytest を入れない理由は test_backfill_recovery.py と同じ——クラウド手順1.5 の
`pip install -r requirements.txt` に波及させないため。

ここで固定しているのは、2026-09-08 の Codex 敵対的レビュー**5周**が挙げた失敗モード。
1周目: ①同日再実行でコメントが2本並ぶ ②saves 手入力による捏造・`-->` 注入
       ③状態ファイルが壊れていると例外で手順が止まる ④欠測と0秒の取り違え
2周目: ⑤start やり直しで旧ランの記録が混ざる ⑥finish が置換前に落ちて前回の
       complete が残る ⑦壊れたコメントから後続HTMLまで正規表現が飲み込む
       ⑧順序違反を complete が通す ⑨不正な target で complete を名乗る
       ⑩書き込みが非原子的
3周目: ⑪A/B交互実行で混在 complete ⑫CRLF が LF に潰れる ⑬暦不正な日付・bool・
       t0より前の時刻 ⑭未閉鎖コメントの内側に書き込む ⑮書き込み失敗の握りつぶし
4周目: ⑯--run-id 省略で照合が素通り ⑰本文の <script> 内の同形文字列まで削除
       ⑱書き込み失敗テストが旧ファイルを消していて「旧値が残る」を再現していない
5周目: ⑲**累積秒を stepN= として出しており「手順ごとの所要時間」になっていない**
       ⑳**ロック無しの read-modify-write 競合で、別ランの開始を消して complete を作れる**
6周目: ㉑stale lock の回収が、生きている所有者の lock を破壊しうる（無条件 unlink）
       ㉒step1 が t0 であることを検証しておらず、開始後の欠落でも complete になる
       ㉓並行性テストがロック除去を3回に1回しか検出しない（回帰検知として弱い）
自分で踏んだもの: ㉔待機側が50msごとにロックを open するため、Windows で所有者の
       unlink が共有違反で失敗しロックが孤児化する（実測3回中2回）

実データ（本番の reviews HTML）も使う。合成HTMLだけで緑にしない。
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_timing  # noqa: E402

FOOTER_HTML = (
    "<!doctype html>\n<html><body>\n"
    "<div class=\"meta\">テーマ</div>\n"
    "<footer>ふっだ</footer>\n</html>\n"
)
RID = "abc12345"


def state(steps, t0=1000, run_id=RID, target="2026-09-07", schema="v2"):
    """steps は [(name, t0からの秒)]。run_id は各markに付く（本番と同じ形）。"""
    return {"schema": schema, "run_id": run_id, "target": target, "t0": t0,
            "steps": [{"name": n, "at": t0 + d, "run_id": run_id} for n, d in steps]}


def marks(gap=10):
    """全 mark を gap 秒間隔で。end は finish が打つのでここには含めない。"""
    return [(s, i * gap) for i, s in enumerate(run_timing.MARKS)]


def bc(state_dict, target, saves, rid=RID, end_at=None):
    """本番の finish と同じく expected_run_id と end_at を渡して組み立てる。"""
    if end_at is None:
        t0 = state_dict.get("t0") if isinstance(state_dict, dict) else 0
        end_at = (t0 or 0) + 10 * len(run_timing.MARKS)
    return run_timing.build_comment(state_dict, target, saves, rid, end_at)


def _raise_oserror(*args, **kwargs):
    raise OSError("injected write failure")


class DurationTest(unittest.TestCase):
    """⑲ 出すのは累積秒ではなく**各区間の所要時間**であること。時刻を注入して検算する。"""

    def test_durations_are_differences_not_cumulative(self):
        gaps = [7, 13, 400, 3, 260, 45, 90, 12]   # step1..step7 の8区間
        self.assertEqual(len(gaps), len(run_timing.MARKS))
        at, steps = 1000, []
        for name, g in zip(run_timing.MARKS, gaps):
            steps.append((name, at - 1000))
            at += g
        c = bc(state(steps), "2026-09-07", 3, end_at=at)
        self.assertIn("status=complete", c)
        for name, g in zip(run_timing.MARKS, gaps):
            self.assertIn("%s_s=%d" % (name, g), c)
        self.assertIn("total_s=%d" % sum(gaps), c)
        # 累積値が混ざっていないこと（step2_5_s は 400 であって 420 ではない）
        self.assertNotIn("step2_5_s=420", c)

    def test_backfill_interval_is_isolated(self):
        """枠を決めたいのは手順2.5。その区間だけが長い日を正しく切り出せること。"""
        at, steps = 1000, []
        for name in run_timing.MARKS:
            steps.append((name, at - 1000))
            at += 480 if name == "step2_5" else 2
        c = bc(state(steps), "2026-09-07", 3, end_at=at)
        self.assertIn("step2_5_s=480", c)
        self.assertIn("step2_s=2", c)

    def test_total_excludes_push(self):
        """total_s は手順1〜7。手順8以降を含まないことを名前と値で固定する。"""
        c = bc(state(marks()), "2026-09-07", 1, end_at=1000 + 10 * len(run_timing.MARKS))
        self.assertIn("total_s=%d" % (10 * len(run_timing.MARKS)), c)
        self.assertNotIn("step8", c)


class PartialDurationTest(unittest.TestCase):
    """欠測1つで実測値が全滅しないこと（2026-09-18 追加）。

    きっかけは 2026-09-17 の初回実測。`missing-step4_5` だけで全区間が捨てられ、
    枠（`--limit`）を決めるための `step2_5_s` まで消えた。
    """

    def _steps_missing(self, drop, gap=10):
        at, steps = 0, []
        for name in run_timing.MARKS:
            if name != drop:
                steps.append((name, at))
            at += gap
        return steps, at

    def test_step2_5_survives_a_missing_later_mark(self):
        """09-17 の再現。step4_5 を落としても step2_5_s は出る。"""
        steps, end = self._steps_missing("step4_5")
        c = bc(state(steps), "2026-09-07", 8, end_at=1000 + end)
        self.assertIn("status=incomplete", c)
        self.assertIn("missing-step4_5", c)
        self.assertIn("step2_5_s=10", c)

    def test_no_synthetic_interval_across_the_gap(self):
        """欠測を跨いだ合成値は出さない（step4→step5 の差を step4_s にしない）。"""
        steps, end = self._steps_missing("step4_5")
        c = bc(state(steps), "2026-09-07", 8, end_at=1000 + end)
        self.assertNotIn("step4_s=", c)
        self.assertNotIn("step4_5_s=", c)

    def test_no_total_when_incomplete(self):
        """total_s は「手順1〜7の合計」。欠測があるときは出さない。"""
        steps, end = self._steps_missing("step4_5")
        c = bc(state(steps), "2026-09-07", 8, end_at=1000 + end)
        self.assertNotIn("total_s=", c)

    def test_duplicated_name_yields_no_interval_for_it(self):
        """重複があると「どの出現の差か」が決まらないので、その区間は出さない。"""
        steps = [(s, i * 10) for i, s in enumerate(run_timing.MARKS)]
        steps.insert(3, ("step2_5", 25))   # step2_5 が2回
        c = bc(state(steps), "2026-09-07", 3, end_at=1000 + 10 * len(run_timing.MARKS))
        self.assertIn("status=incomplete", c)
        self.assertNotIn("step2_5_s=", c)
        self.assertNotIn("step2_s=", c)     # step2→step2_5 も一意に決まらない

    def test_missing_end_still_reports_measured_intervals(self):
        """finish が end を打てなくても、手前の区間は測れている。"""
        c = run_timing.build_comment(state(marks()), "2026-09-07", 1, RID, "nope")
        self.assertIn("status=incomplete", c)
        self.assertIn("missing-end", c)
        self.assertIn("step2_5_s=10", c)
        self.assertNotIn("step7_s=", c)     # end が無いので手順7の区間は測れない


class BuildCommentTest(unittest.TestCase):
    def test_complete_when_ordered_and_monotonic(self):
        c = bc(state(marks()), "2026-09-07", 3)
        self.assertIn("status=complete", c)
        self.assertIn("saves=3", c)
        self.assertNotIn("reason=", c)

    def test_missing_mark_is_incomplete_and_named(self):
        """④ 欠測は 0秒 と区別できる形で出る。キーが消えるだけにしない。"""
        steps = [(s, i * 10) for i, s in enumerate(run_timing.MARKS) if s != "step4"]
        c = bc(state(steps), "2026-09-07", 3)
        self.assertIn("status=incomplete", c)
        self.assertIn("missing-step4", c)
        self.assertNotIn("step4_s=", c)

    def test_missing_end_is_incomplete(self):
        c = run_timing.build_comment(state(marks()), "2026-09-07", 1, RID, "nope")
        self.assertIn("status=incomplete", c)
        self.assertIn("missing-end", c)

    def test_zero_length_interval_is_not_confused_with_missing(self):
        steps = [(s, 0) for s in run_timing.MARKS[:-1]] + [("step7", 5)]
        c = bc(state(steps), "2026-09-07", 0, end_at=1010)
        self.assertIn("status=complete", c)
        self.assertIn("step2_s=0", c)
        self.assertIn("saves=0", c)

    def test_wrong_order_is_rejected(self):
        """⑧ 名前が揃っていても順序が違えば complete にしない。"""
        steps = marks()
        steps[1], steps[2] = ("step2_5", 10), ("step2", 20)
        c = bc(state(steps), "2026-09-07", 1)
        self.assertIn("status=incomplete", c)
        self.assertIn("wrong_order", c)

    def test_duplicate_mark_is_rejected(self):
        c = bc(state(marks() + [("step3", 90)]), "2026-09-07", 1)
        self.assertIn("status=incomplete", c)
        self.assertIn("duplicated-step3", c)

    def test_marks_from_a_previous_run_are_discarded(self):
        """⑤ start やり直し後に残った旧ランの記録を混ぜない。"""
        s = state(marks())
        s["steps"] = ([{"name": "step1", "at": 500, "run_id": "deadbeef"},
                       {"name": "step2", "at": 501, "run_id": "deadbeef"}] + s["steps"])
        c = bc(s, "2026-09-07", 1)
        self.assertIn("status=complete", c)
        self.assertIn("run_id=" + RID, c)

    def test_only_previous_run_marks_means_incomplete(self):
        s = state(marks())
        for st in s["steps"]:
            st["run_id"] = "deadbeef"
        c = bc(s, "2026-09-07", 1)
        self.assertIn("status=incomplete", c)

    def test_calendar_invalid_date_is_incomplete(self):
        """⑬ 形は合っていても暦として存在しない日付を通さない。"""
        for bad in ["2026-02-30", "2026-99-99", "2026-13-01"]:
            c = bc(state(marks(), target=bad), bad, 1)
            self.assertIn("status=incomplete", c, bad)
            self.assertIn("bad_target", c, bad)

    def test_bool_timestamps_are_rejected(self):
        """⑬ bool は int のサブクラス。True を時刻として通さない。"""
        s = state(marks())
        s["steps"][2]["at"] = True
        c = bc(s, "2026-09-07", 1)
        self.assertIn("status=incomplete", c)

    def test_mark_before_t0_is_rejected(self):
        s = state(marks())
        s["steps"][3]["at"] = s["t0"] - 500
        c = bc(s, "2026-09-07", 1)
        self.assertIn("status=incomplete", c)
        self.assertIn("step_before_t0", c)

    def test_step1_must_equal_t0(self):
        """㉒ step1 は start が t0 で置く基準点。ずれていたら complete にしない
        （total_s が『開始からの実測』でなくなり、未計測の区間を黙って捨てた値になる）。"""
        s = state(marks())
        s["steps"][0]["at"] = s["t0"] + 1000
        c = run_timing.build_comment(s, "2026-09-07", 1, RID, s["t0"] + 2000)
        self.assertIn("status=incomplete", c)
        self.assertIn("step1_not_t0", c)

    def test_time_after_finish_is_rejected(self):
        s = state(marks())
        c = run_timing.build_comment(s, "2026-09-07", 1, RID, s["t0"] + 5)
        self.assertIn("status=incomplete", c)
        self.assertIn("time_in_future", c)

    def test_implausible_span_is_rejected(self):
        """並びが整っていても、1ランとしてあり得ない長さなら complete にしない
        （`t0=1`＝1970年起点のような state で巨大な total_s が公開されるのを防ぐ）。"""
        steps = [(s, i) for i, s in enumerate(run_timing.MARKS)]
        c = run_timing.build_comment(state(steps, t0=1), "2026-09-07", 1, RID,
                                     1 + run_timing.MAX_RUN_SEC + 60)
        self.assertIn("status=incomplete", c)
        self.assertIn("implausible_span", c)

    def test_negative_saves_is_unknown(self):
        c = bc(state(marks()), "2026-09-07", -1)
        self.assertIn("saves=unknown", c)
        self.assertIn("status=incomplete", c)

    def test_run_id_missing_and_mismatch(self):
        """⑯ --run-id を省略／取り違えたら complete にしない。"""
        c = run_timing.build_comment(state(marks()), "2026-09-07", 1, None, 1080)
        self.assertIn("run_id_missing", c)
        c = run_timing.build_comment(state(marks()), "2026-09-07", 1, "ffffffff", 1080)
        self.assertIn("run_id_mismatch", c)

    def test_target_mismatch_is_incomplete(self):
        c = bc(state(marks(), target="2026-09-01"), "2026-09-07", 1)
        self.assertIn("status=incomplete", c)
        self.assertIn("target_mismatch", c)

    def test_broken_state_shapes_do_not_raise(self):
        """③ 壊れた状態でも例外を出さず incomplete を返す。"""
        for s in [{}, {"schema": "v2"}, {"schema": "v2", "steps": "nope"},
                  {"schema": "v2", "run_id": "zz", "t0": "x", "steps": [1, 2]},
                  {"schema": "broken"}, "not a dict"]:
            c = run_timing.build_comment(s, "2026-09-07", 1, RID, 2000)
            self.assertIn("status=incomplete", c)

    def test_all_zero_intervals_is_flagged(self):
        c = bc(state([(s, 0) for s in run_timing.MARKS]), "2026-09-07", 1, end_at=1000)
        self.assertIn("suspicious_zero", c)
        self.assertIn("status=incomplete", c)

    def test_saves_unknown_when_count_failed(self):
        c = bc(state(marks()), "2026-09-07", None)
        self.assertIn("saves=unknown", c)
        self.assertIn("status=incomplete", c)

    def test_comment_contains_no_html_breaking_sequence(self):
        """② 任意文字列がHTMLへ通る経路が無い。`-->` はコメント終端にしか出ない。"""
        c = bc(state(marks(), run_id="--><script>"), "2026-09-07", 3)
        self.assertTrue(c.startswith("<!-- run-timing "))
        self.assertTrue(c.endswith(" -->"))
        self.assertEqual(c.count("-->"), 1)
        self.assertNotIn("<", c[4:-3])
        self.assertIn("status=incomplete", c)


class InsertCommentTest(unittest.TestCase):
    C1 = "<!-- run-timing schema=v2 a=1 -->"
    C2 = "<!-- run-timing schema=v2 a=2 -->"

    def test_inserts_immediately_before_footer(self):
        out, existing = run_timing.insert_comment(FOOTER_HTML, self.C1)
        self.assertEqual(existing, 0)
        # 既存の notegen-warn 警告（手順2の取り込み不完全マーク）と同じ差し込み位置
        self.assertIn(self.C1 + "\n</footer>", out)

    def test_rerun_replaces_instead_of_appending(self):
        once, _ = run_timing.insert_comment(FOOTER_HTML, self.C1)
        twice, existing = run_timing.insert_comment(once, self.C2)
        self.assertEqual(existing, 1)
        self.assertEqual(twice.count("run-timing"), 1)
        self.assertIn("a=2", twice)

    def test_old_schema_comment_is_also_replaced(self):
        """形式を変えたときに前版が残らないこと。"""
        dirty = FOOTER_HTML.replace(
            "</footer>", "<!-- run-timing schema=v1 saves=3 step1=0 -->\n</footer>")
        out, existing = run_timing.insert_comment(dirty, self.C1)
        self.assertEqual(existing, 1)
        self.assertNotIn("schema=v1", out)

    def test_multiple_pre_existing_comments_collapse_to_one(self):
        dirty = FOOTER_HTML.replace(
            "</footer>", self.C1 + "\n" + self.C2 + "\n</footer>")
        out, existing = run_timing.insert_comment(dirty, "<!-- run-timing schema=v2 a=3 -->")
        self.assertEqual(existing, 2)
        self.assertEqual(out.count("run-timing"), 1)

    def test_unclosed_comment_before_footer_blocks_the_write(self):
        """⑦⑭ 壊れたコメントは消さない。かつ footer より前にあるなら書かない。"""
        dirty = FOOTER_HTML.replace(
            "<footer>", "<!-- run-timing schema=v2 a=1\n<p>大事な本文</p>\n<footer>")
        out, existing = run_timing.insert_comment(dirty, self.C1)
        self.assertEqual(existing, 0)
        self.assertIsNone(out)

    def test_same_looking_string_inside_script_is_not_touched(self):
        """⑰ 本文の <script> にある同形の文字列を消さない（消すとJSが壊れる）。"""
        js = ('<script>\nconst example = "<!-- run-timing schema=v2 '
              'status=complete -->";\n</script>\n')
        dirty = FOOTER_HTML.replace("<footer>", js + "<footer>")
        out, existing = run_timing.insert_comment(dirty, self.C1)
        self.assertEqual(existing, 0)
        self.assertIn('const example = "<!-- run-timing schema=v2 status=complete -->";', out)

    def test_crlf_line_endings_are_preserved(self):
        """⑫ CRLF の reviews を LF に書き換えない。"""
        crlf = FOOTER_HTML.replace("\n", "\r\n")
        out, _ = run_timing.insert_comment(crlf, self.C1)
        self.assertNotIn("\n", out.replace("\r\n", ""))
        self.assertEqual(out.replace(self.C1 + "\r\n", ""), crlf)

    def test_no_footer_means_no_write(self):
        out, _ = run_timing.insert_comment("<html><body>x</body></html>", self.C1)
        self.assertIsNone(out)

    def test_two_footers_means_no_write(self):
        """`</footer>` が文書中に2個あると、`rfind` では場所を一意に特定できない
        ので書かずに諦める（誤挿入防止。手順書の <script> の openChat() 文字列に
        `</footer>` は含まれないが、他の理由で複数化した場合の安全側フォールバック）。"""
        dirty = FOOTER_HTML.replace(
            "</html>", "<script>var s = \"</footer>\";</script>\n</html>")
        out, existing = run_timing.insert_comment(dirty, self.C1)
        self.assertEqual(existing, 0)
        self.assertIsNone(out)

    def test_blank_lines_between_existing_comments_and_footer_still_collapse(self):
        """コメント同士・コメントとfooterの間に空行が挟まっていても、
        再実行で正規形コメントを剥がして1本に差し替えられること（本文は消さない）。"""
        dirty = FOOTER_HTML.replace(
            "</footer>",
            self.C1 + "\n\n" + self.C2 + "\n   \n</footer>",
        )
        out, existing = run_timing.insert_comment(dirty, "<!-- run-timing schema=v2 a=3 -->")
        self.assertEqual(existing, 2)
        self.assertEqual(out.count("run-timing"), 1)
        self.assertIn("a=3", out)
        # 本文（テーマの div）は空行剥がしの影響を受けず残っていること。
        self.assertIn('<div class="meta">テーマ</div>', out)

    def test_real_review_html_is_preserved(self):
        """合成HTMLだけで緑にしない。本番の reviews 全件で往復させる。"""
        reviews_dir = os.path.join(HERE, "reviews")
        names = sorted(n for n in os.listdir(reviews_dir) if n.endswith(".html"))
        self.assertTrue(names, "reviews/*.html が無い")
        checked = 0
        for name in names:
            with open(os.path.join(reviews_dir, name), encoding="utf-8", newline="") as f:
                original = f.read()
            if "</footer>" not in original:
                continue
            out, existing = run_timing.insert_comment(original, self.C1)
            self.assertIsNotNone(out, name)
            eol = "\r\n" if "\r\n" in original else "\n"
            if existing == 0:
                # まだ計測を通っていない過去ぶん。本文は1バイトも変わらない。
                self.assertEqual(out.replace(self.C1 + eol, ""), original, name)
            else:
                # 本番ランで既に計測コメントが入ったファイル（2026-09-17 以降）。
                # 旧版はここを existing == 0 と決め打ちしていたので、装置が動き出した
                # 時点で main の CI が落ちた（2026-09-18 に発見）。コメント行を両側から
                # 同じ規則で取り除いて、本文が変わっていないことを見る。
                self.assertEqual(run_timing.COMMENT_RE.sub("", out),
                                 run_timing.COMMENT_RE.sub("", original), name)
            self.assertEqual(out.count("<!-- run-timing"), 1, name)
            base = out.replace(self.C1 + eol, "")
            again, existing2 = run_timing.insert_comment(out, self.C2)
            self.assertEqual(existing2, 1, name)
            self.assertEqual(again.count("run-timing"), 1, name)
            self.assertEqual(again.replace(self.C2 + eol, ""), base, name)
            checked += 1
        self.assertGreater(checked, 0)


class CliTest(unittest.TestCase):
    """実際にサブプロセスで叩く。どの経路でも exit 0 で、手順を止めない。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(HERE, "run_timing.py"), self.dir)
        with open(os.path.join(self.dir, "captures.json"), "w", encoding="utf-8") as f:
            json.dump([{"date": "2026-09-07"}, {"date": "2026-09-07"},
                       {"date": "2026-09-06"}], f)
        self.reviews = os.path.join(self.dir, "r.html")
        self._write_reviews(FOOTER_HTML)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write_reviews(self, text):
        with open(self.reviews, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def _read_reviews(self):
        with open(self.reviews, encoding="utf-8", newline="") as f:
            return f.read()

    def _run_id(self, out):
        for line in out.splitlines():
            if line.startswith("RUN_TIMING_RUN_ID:"):
                return line.split(":", 1)[1].strip()
        return None

    def _run(self, *args):
        p = subprocess.run([sys.executable, "run_timing.py"] + list(args),
                           cwd=self.dir, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def _full_run(self, target="2026-09-07"):
        """正常系。**最後の mark の前に1秒空ける**のは、サブプロセスが同一秒に
        収まると全区間0秒になり `suspicious_zero` が立つため。本番は手順5だけで
        分単位かかるのでこの形にはならない。"""
        rid = self._run_id(self._run("start", "--target", target))
        for s in run_timing.MARKABLE[:-1]:
            self._run("mark", s, "--run-id", rid)
        time.sleep(1.05)
        self._run("mark", run_timing.MARKABLE[-1], "--run-id", rid)
        return rid

    def test_happy_path_counts_saves_itself(self):
        rid = self._full_run()
        out = self._run("finish", "--target", "2026-09-07", "--run-id", rid,
                        "--reviews", "r.html")
        self.assertIn("status=complete", out)
        self.assertIn("saves=2", out)   # 手で埋めていない。captures.json から数えた
        self.assertIn("total_s=", out)
        self.assertEqual(self._read_reviews().count("run-timing"), 1)

    def test_run_id_is_required_for_mark_and_finish(self):
        """⑯ --run-id を省略したら記録もせず complete にもしない。"""
        self._run("start", "--target", "2026-09-07")
        for s in run_timing.MARKABLE:
            out = self._run("mark", s)          # --run-id を渡さない
            self.assertIn("--run-id required", out)
        out = self._run("finish", "--target", "2026-09-07", "--reviews", "r.html")
        self.assertIn("status=incomplete", out)
        self.assertIn("run_id_missing", out)

    def test_restart_discards_previous_run_marks(self):
        self._run("start", "--target", "2026-09-07")
        rid = self._full_run()
        out = self._run("finish", "--target", "2026-09-07", "--run-id", rid,
                        "--reviews", "r.html")
        self.assertIn("status=complete", out)

    def test_interleaved_runs_do_not_produce_a_mixed_complete(self):
        """⑪ A start → B start → A mark、で偽の complete を作らせない。"""
        rid_a = self._run_id(self._run("start", "--target", "2026-09-07"))
        rid_b = self._run_id(self._run("start", "--target", "2026-09-07"))
        self.assertNotEqual(rid_a, rid_b)
        out = self._run("mark", "step2", "--run-id", rid_a)
        self.assertIn("run_id mismatch", out)
        out = self._run("finish", "--target", "2026-09-07", "--run-id", rid_a,
                        "--reviews", "r.html")
        self.assertIn("run_id_mismatch", out)

    def test_concurrent_marks_do_not_lose_updates(self):
        """⑳ 同時 mark で更新が消えないこと（ロックの実効性）。

        逐次実行では踏めないので、実際に並列でサブプロセスを起動する。
        ロックが無いと read-modify-write が競合して mark が取りこぼされる。"""
        rid = self._run_id(self._run("start", "--target", "2026-09-07"))
        procs = [subprocess.Popen(
            [sys.executable, "run_timing.py", "mark", s, "--run-id", rid],
            cwd=self.dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for s in run_timing.MARKABLE]
        for p in procs:
            p.communicate()          # パイプを閉じる（ResourceWarning 回避）
            self.assertEqual(p.returncode, 0)
        with open(os.path.join(self.dir, run_timing.STATE_FILE), encoding="utf-8") as f:
            recorded = [s["name"] for s in json.load(f)["steps"]]
        # 順序は並列なので不定でよいが、**1つも失われていない**こと
        self.assertEqual(sorted(recorded), sorted(["step1"] + run_timing.MARKABLE))

    def test_concurrent_start_and_mark_never_yields_complete(self):
        """⑳ A の mark と B の start が並走しても、A が complete を作れないこと。"""
        rid_a = self._run_id(self._run("start", "--target", "2026-09-07"))
        procs = [subprocess.Popen(
            [sys.executable, "run_timing.py", "mark", s, "--run-id", rid_a],
            cwd=self.dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for s in run_timing.MARKABLE]
        b = subprocess.Popen(
            [sys.executable, "run_timing.py", "start", "--target", "2026-09-07"],
            cwd=self.dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for p in procs + [b]:
            p.communicate()          # パイプを閉じる（ResourceWarning 回避）
        out = self._run("finish", "--target", "2026-09-07", "--run-id", rid_a,
                        "--reviews", "r.html")
        # B が start を通した以上、A は自分の記録だけで complete にはなれない
        self.assertIn("status=incomplete", out)

    def test_stale_comment_is_replaced_when_state_is_broken(self):
        """⑥ finish が失敗しても、前回の complete を残さない。"""
        rid = self._full_run()
        self._run("finish", "--target", "2026-09-07", "--run-id", rid,
                  "--reviews", "r.html")
        self.assertIn("status=complete", self._read_reviews())
        with open(os.path.join(self.dir, run_timing.STATE_FILE), "w") as f:
            f.write("{not json")
        out = self._run("finish", "--target", "2026-09-07", "--run-id", rid,
                        "--reviews", "r.html")
        html = self._read_reviews()
        self.assertIn("status=incomplete", out)
        self.assertNotIn("status=complete", html)
        self.assertEqual(html.count("run-timing"), 1)

    def test_write_failure_keeps_the_old_file_and_says_so(self):
        """⑮⑱ 書き込み失敗時、**旧ファイルが保持されたまま** WRITE_FAILED を出す。
        os.replace の失敗はOSごとに起こし方が違うので _atomic_write を差し替えて注入する。"""
        rid = self._full_run()
        self._run("finish", "--target", "2026-09-07", "--run-id", rid,
                  "--reviews", "r.html")
        before = self._read_reviews()
        self.assertIn("status=complete", before)

        cwd, original, out = os.getcwd(), run_timing._atomic_write, io.StringIO()
        try:
            os.chdir(self.dir)
            run_timing._atomic_write = _raise_oserror
            with contextlib.redirect_stdout(out):
                run_timing.cmd_finish("2026-09-07", "r.html", rid)
        finally:
            run_timing._atomic_write = original
            os.chdir(cwd)
        self.assertIn("WRITE_FAILED", out.getvalue())
        self.assertIn("stale comment may remain", out.getvalue())
        self.assertEqual(self._read_reviews(), before)   # 1バイトも変わっていない

    def test_finish_never_rolls_back_the_review_body(self):
        """㉕ **7周目の最重要指摘。** finish が reviews を読んでから書くまでの間に
        別ランが本文を差し替えた場合、古い本文で丸ごと上書きして相手の成果物を
        消してはならない（計測の遅れが振り返り本体へ波及する経路）。

        `insert_comment` を差し替えて「読んだあとに B が書いた」状況を決定的に作る。"""
        rid = self._full_run()
        newer = FOOTER_HTML.replace("ふっだ", "あとから B が書いた新しい本文")
        original = run_timing.insert_comment

        def insert_and_swap(html, comment):
            self._write_reviews(newer)          # ← 読み直しの直前に B が書く
            return original(html, comment)

        cwd, out = os.getcwd(), io.StringIO()
        try:
            os.chdir(self.dir)
            run_timing.insert_comment = insert_and_swap
            with contextlib.redirect_stdout(out):
                run_timing.cmd_finish("2026-09-07", "r.html", rid)
        finally:
            run_timing.insert_comment = original
            os.chdir(cwd)
        self.assertIn("changed since read", out.getvalue())
        self.assertEqual(self._read_reviews(), newer)   # B の本文が1バイトも欠けていない

    def test_missing_state_file_does_not_stop_the_routine(self):
        out = self._run("finish", "--target", "2026-09-07", "--reviews", "r.html")
        self.assertIn("status=incomplete", out)

    def test_state_from_another_target_is_not_adopted(self):
        rid = self._full_run(target="2026-09-01")
        out = self._run("finish", "--target", "2026-09-07", "--run-id", rid,
                        "--reviews", "r.html")
        self.assertIn("target_mismatch", out)

    def test_missing_captures_json_yields_saves_unknown(self):
        rid = self._full_run()
        os.remove(os.path.join(self.dir, "captures.json"))
        out = self._run("finish", "--target", "2026-09-07", "--run-id", rid,
                        "--reviews", "r.html")
        self.assertIn("saves=unknown", out)

    def test_missing_reviews_file_does_not_stop_the_routine(self):
        rid = self._full_run()
        os.remove(self.reviews)
        out = self._run("finish", "--target", "2026-09-07", "--run-id", rid,
                        "--reviews", "r.html")
        self.assertIn("RUN_TIMING_LINE:", out)
        self.assertIn("WRITE_FAILED", out)

    def test_no_footer_leaves_file_untouched(self):
        rid = self._full_run()
        self._write_reviews("<html><body>x</body></html>")
        self._run("finish", "--target", "2026-09-07", "--run-id", rid,
                  "--reviews", "r.html")
        self.assertEqual(self._read_reviews(), "<html><body>x</body></html>")

    def test_no_temp_files_left_behind(self):
        """⑩ 原子的書き込みの一時ファイルが残らない（ロックファイルは残ってよい）。"""
        rid = self._full_run()
        self._run("finish", "--target", "2026-09-07", "--run-id", rid,
                  "--reviews", "r.html")
        leftovers = [n for n in os.listdir(self.dir) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_lock_is_actually_exclusive_across_processes(self):
        """㉓ ロックの実効性を**決定的に**確かめる。並列 mark のテストはロックを外しても
        3回に1回しか落ちない（回帰検知として弱い）と出口検証に指摘されたので、
        別プロセスにロックを保持させたまま取得を試みる。"""
        if run_timing._lock_op is None:
            self.skipTest("この環境にはロック機構が無い")
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys,time; sys.path.insert(0,'.'); import run_timing as r;"
             "l=r._acquire_lock(); print('HELD' if l else 'NOPE', flush=True);"
             "time.sleep(6); r._release_lock(l)"],
            cwd=self.dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "HELD")
            cwd, wait = os.getcwd(), run_timing.LOCK_WAIT_SEC
            try:
                os.chdir(self.dir)
                run_timing.LOCK_WAIT_SEC = 0.3      # 待たされる側をすぐ諦めさせる
                blocked = run_timing._acquire_lock()
            finally:
                run_timing.LOCK_WAIT_SEC = wait
                os.chdir(cwd)
            self.assertFalse(blocked, "保持中なのにロックが取れてしまった")
        finally:
            holder.kill()
            holder.communicate()
        # 保持者が死ねば OS が解放する（stale の回収処理が要らない理由）
        cwd = os.getcwd()
        try:
            os.chdir(self.dir)
            free = run_timing._acquire_lock()
            self.assertTrue(free)
            run_timing._release_lock(free)
        finally:
            os.chdir(cwd)

    def test_waiting_does_not_orphan_the_owners_lock(self):
        """待機側がロックファイルを掴んで所有者の解放を妨げないこと。

        Windows では他プロセスが open 中のファイルの unlink が失敗する。待機の常路で
        ロックを読みに行く実装だと、所有者が自分のロックを外せず孤児化し、以後
        全員が lock busy になった（実測で3回中2回再現）。"""
        rid = self._run_id(self._run("start", "--target", "2026-09-07"))
        procs = [subprocess.Popen(
            [sys.executable, "run_timing.py", "mark", s, "--run-id", rid],
            cwd=self.dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for s in run_timing.MARKABLE]
        outs = [p.communicate()[0] for p in procs]
        self.assertEqual([o for o in outs if "lock busy" in o], [])
        with open(os.path.join(self.dir, run_timing.STATE_FILE), encoding="utf-8") as f:
            recorded = [x["name"] for x in json.load(f)["steps"]]
        self.assertEqual(sorted(recorded), sorted(["step1"] + run_timing.MARKABLE))

    def test_lock_file_is_never_deleted(self):
        """㉑ ロックファイルは作るだけで消さない（削除で所有権を管理しない）。
        消さないので「他ランのロックを消す」「Windowsで共有違反により孤児化する」が
        原理的に起きない。残っていても次のランは普通にロックを取れる。"""
        rid = self._full_run()
        self._run("finish", "--target", "2026-09-07", "--run-id", rid,
                  "--reviews", "r.html")
        if run_timing._lock_op is not None:
            self.assertTrue(os.path.exists(os.path.join(self.dir, run_timing.LOCK_FILE)))
        out = self._run("start", "--target", "2026-09-07")
        self.assertIn("RUN_TIMING_RUN_ID:", out)

    def test_unknown_subcommand_and_missing_args_are_survivable(self):
        for args in [("bogus",), ("mark",), ("mark", "step99"),
                     ("finish",), ("start",)]:
            self._run(*args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
