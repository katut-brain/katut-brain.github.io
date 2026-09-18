# -*- coding: utf-8 -*-
"""手順2.5 が起動したこと自体の証跡（fetch_facts/runs/<日付>.json）のテスト。

なぜ要るか: 候補が9件ある夜が3晩続いても fetch_facts に痕跡が1件も増えず、
「手順2.5 を実行していない」と「実行したが何も書かなかった」を成果物から
区別できなかった（2026-09-15〜17）。無人LLMが打つ run_timing の mark は
実行証跡ではない（mark だけ打ってコマンドを飛ばせる）。
"""

import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import backfill  # noqa: E402


class RunEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bf-evidence-")
        self._old = os.environ.get("FETCH_FACTS_DIR")
        os.environ["FETCH_FACTS_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FETCH_FACTS_DIR", None)
        else:
            os.environ["FETCH_FACTS_DIR"] = self._old

    def _path(self, day="2026-09-18"):
        return os.path.join(self.tmp, "runs", "%s.json" % day)

    def _read(self, day="2026-09-18"):
        with io.open(self._path(day), encoding="utf-8") as f:
            return json.load(f)

    def test_writes_evidence_with_target_and_schema(self):
        backfill._write_run_evidence("2026-09-18", status="started", limit=1)
        d = self._read()
        self.assertEqual(d["schema"], "v2")
        self.assertEqual(d["target"], "2026-09-18")
        self.assertEqual(len(d["runs"]), 1)
        self.assertEqual(d["runs"][0]["status"], "started")
        self.assertEqual(d["runs"][0]["limit"], 1)
        self.assertEqual(d["runs"][0]["run"], backfill._RUN_TOKEN)

    def test_same_process_updates_its_own_entry(self):
        """同じプロセスの2回目は**自分のエントリを更新**する（行を増やさない）。"""
        backfill._write_run_evidence("2026-09-18", status="started")
        backfill._write_run_evidence("2026-09-18", status="completed", attempted=1)
        d = self._read()
        self.assertEqual(len(d["runs"]), 1)
        self.assertEqual(d["runs"][0]["status"], "completed")
        self.assertEqual(d["runs"][0]["attempted"], 1)

    def test_corrupt_file_does_not_lose_this_run(self):
        """壊れた証跡ファイルがあっても、今回の実行だけは必ず残す。"""
        os.makedirs(os.path.dirname(self._path()), exist_ok=True)
        with io.open(self._path(), "w", encoding="utf-8") as f:
            f.write("{ broken")
        backfill._write_run_evidence("2026-09-18", status="started")
        d = self._read()
        self.assertEqual(len(d["runs"]), 1)
        self.assertEqual(d["runs"][0]["run"], backfill._RUN_TOKEN)

    def test_runs_are_capped(self):
        """同日再実行を積んでもファイルは肥大しない（古いものから捨てる）。"""
        for i in range(backfill.MAX_RUNS_PER_DAY + 5):
            old = backfill._RUN_TOKEN
            try:
                backfill._RUN_TOKEN = "%08x" % i
                backfill._write_run_evidence("2026-09-18", status="completed")
            finally:
                backfill._RUN_TOKEN = old
        self.assertEqual(len(self._read()["runs"]), backfill.MAX_RUNS_PER_DAY)

    def test_no_tmp_file_left_behind(self):
        """一時ファイル＋os.replace の原子的置換。.tmp を1つも残さない。

        実装の一時名は `<path>.<RUN_TOKEN>.tmp` なので、`<path>.tmp` だけを見ても
        残骸検出にならない（2026-09-18 Codex 6周目 P2）。ディレクトリを走査する。
        """
        backfill._write_run_evidence("2026-09-18", status="started")
        leftovers = [n for n in os.listdir(os.path.dirname(self._path()))
                     if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_bad_target_writes_nothing(self):
        """日付として妥当でない target ではファイル名を作らない（パス汚染よけ）。"""
        for bad in ("../evil", "2026-9-8", "", None, "2026-09-18/x"):
            backfill._write_run_evidence(bad, status="started")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "runs")))

    def test_never_raises_when_dir_is_unwritable(self):
        """証跡が書けなくても手順を止めない（例外を投げない）。"""
        os.environ["FETCH_FACTS_DIR"] = os.path.join(self.tmp, "nope.txt")
        with io.open(os.environ["FETCH_FACTS_DIR"], "w", encoding="utf-8") as f:
            f.write("not a directory")
        backfill._write_run_evidence("2026-09-18", status="started")   # 例外が出ないこと

    def test_evidence_dir_is_not_read_by_ledger(self):
        """runs/ は fetch_facts/*.json の glob に掛からない＝台帳の集計を汚さない。"""
        import ledger
        backfill._write_run_evidence("2026-09-18", status="completed", candidates=9)
        old = ledger.FACTS_DIR
        try:
            ledger.FACTS_DIR = self.tmp
            self.assertEqual(ledger._load_all_records(), [])
        finally:
            ledger.FACTS_DIR = old


if __name__ == "__main__":
    unittest.main()


class CliEvidenceTest(unittest.TestCase):
    """CLI（main）経由で実際に証跡が started → completed / crashed になること。

    単体関数のテストだけでは「呼び出しそのものが抜けている」退行を捕まえられない
    （2026-09-18 Codex 4周目 P1）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bf-cli-")
        self._old = os.environ.get("FETCH_FACTS_DIR")
        os.environ["FETCH_FACTS_DIR"] = self.tmp
        # ⚠️ 環境変数だけでは足りない。ledger / fetch_content は FACTS_DIR を
        # **import 時に1回だけ**解決するので、既に import 済みなら環境変数を後から
        # 変えても効かず、テストが**リポジトリの fetch_facts/ に書いてしまう**
        # （実際に fetch_facts/2026-09-18.json を作ってしまった。2026-09-18）。
        import fetch_content
        import ledger
        self._patched = [(ledger, ledger.FACTS_DIR), (fetch_content, fetch_content.FACTS_DIR)]
        ledger.FACTS_DIR = self.tmp
        fetch_content.FACTS_DIR = self.tmp
        # 実ネットワークに出さない。
        self._old_run_one = backfill.RUN_ONE
        backfill.RUN_ONE = lambda url, timeout, env: None

    def tearDown(self):
        backfill.RUN_ONE = self._old_run_one
        for mod, old in self._patched:
            mod.FACTS_DIR = old
        if self._old is None:
            os.environ.pop("FETCH_FACTS_DIR", None)
        else:
            os.environ["FETCH_FACTS_DIR"] = self._old

    def _runs(self, day="2026-09-18"):
        with io.open(os.path.join(self.tmp, "runs", "%s.json" % day), encoding="utf-8") as f:
            return json.load(f)["runs"]

    def test_cli_records_completed(self):
        self.assertEqual(backfill.main(["--target", "2026-09-18", "--limit", "1"]), 0)
        runs = self._runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")
        self.assertIn("candidates", runs[0])
        self.assertIn("invoked_at", runs[0])
        self.assertIn("finished_at", runs[0])

    def test_cli_dry_run_writes_nothing(self):
        self.assertEqual(
            backfill.main(["--target", "2026-09-18", "--dry-run"]), 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "runs")))

    def test_cli_records_crashed_without_raising(self):
        """run() が落ちても exit 0 で、証跡は crashed になる（未実行と区別できる）。"""
        old = backfill.run
        try:
            def boom(*a, **k):
                raise RuntimeError("injected")
            backfill.run = boom
            self.assertEqual(backfill.main(["--target", "2026-09-18"]), 0)
        finally:
            backfill.run = old
        runs = self._runs()
        self.assertEqual(runs[-1]["status"], "crashed")
        self.assertEqual(runs[-1]["error"], "RuntimeError")

    def test_started_survives_when_the_run_never_finishes(self):
        """外側 timeout に殺された夜の姿。started のまま残り、未実行（ファイル無し）と違う。"""
        backfill._write_run_evidence("2026-09-18", status="started",
                                     invoked_at=backfill._utcnow())
        runs = self._runs()
        self.assertEqual(runs[-1]["status"], "started")
        self.assertNotIn("finished_at", runs[-1])

    def test_same_day_reruns_are_both_kept(self):
        """同日再実行で上書きしない。1回目completed・2回目中断が区別できること。"""
        self.assertEqual(backfill.main(["--target", "2026-09-18"]), 0)
        first = list(self._runs())
        old_token = backfill._RUN_TOKEN
        try:
            backfill._RUN_TOKEN = "ffffffff"      # 別プロセスを模す
            backfill._write_run_evidence("2026-09-18", status="started",
                                         invoked_at=backfill._utcnow())
        finally:
            backfill._RUN_TOKEN = old_token
        runs = self._runs()
        self.assertEqual(len(runs), len(first) + 1)
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(runs[-1]["status"], "started")
        self.assertNotEqual(runs[0]["run"], runs[-1]["run"])

    def test_write_failure_is_announced_on_stderr(self):
        """黙って消えない。手順書の「無い＝未実行」はこの行が無いことが前提。"""
        import contextlib
        blocker = os.path.join(self.tmp, "blocked")
        with io.open(blocker, "w", encoding="utf-8") as f:
            f.write("not a directory")
        os.environ["FETCH_FACTS_DIR"] = blocker
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            backfill._write_run_evidence("2026-09-18", status="started")
        self.assertIn("BACKFILL_EVIDENCE: write_failed", err.getvalue())


class TrimPolicyTest(unittest.TestCase):
    """上限で切り詰めるとき、未完了（started）の証跡を先に捨てないこと。

    外側 timeout に殺されて started のまま残った実行は、その夜に何が起きたかを
    示す唯一の材料。単純に古い順で切ると、その後の再実行20回で落ちる
    （2026-09-18 Codex 5周目 P2）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bf-trim-")
        self._old = os.environ.get("FETCH_FACTS_DIR")
        os.environ["FETCH_FACTS_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FETCH_FACTS_DIR", None)
        else:
            os.environ["FETCH_FACTS_DIR"] = self._old

    def _runs(self, day="2026-09-18"):
        with io.open(os.path.join(self.tmp, "runs", "%s.json" % day), encoding="utf-8") as f:
            return json.load(f)["runs"]

    def _write_as(self, token, **fields):
        old = backfill._RUN_TOKEN
        try:
            backfill._RUN_TOKEN = token
            backfill._write_run_evidence("2026-09-18", **fields)
        finally:
            backfill._RUN_TOKEN = old

    def test_unfinished_entry_survives_many_later_runs(self):
        self._write_as("aborted-one", status="started")
        for i in range(backfill.MAX_RUNS_PER_DAY + 5):
            self._write_as("done%02d" % i, status="completed")
        runs = self._runs()
        self.assertEqual(len(runs), backfill.MAX_RUNS_PER_DAY)
        self.assertIn("aborted-one", [r.get("run") for r in runs])
        # 直近の完了も残っている（古い完了だけが落ちる）
        self.assertEqual(runs[-1]["run"], "done%02d" % (backfill.MAX_RUNS_PER_DAY + 4))

    def test_current_run_always_survives(self):
        """未完了だけで上限を超えても、今回の実行は必ず残る。"""
        for i in range(backfill.MAX_RUNS_PER_DAY + 3):
            self._write_as("st%02d" % i, status="started")
        runs = self._runs()
        self.assertEqual(len(runs), backfill.MAX_RUNS_PER_DAY)
        self.assertEqual(runs[-1]["run"], "st%02d" % (backfill.MAX_RUNS_PER_DAY + 2))

    def test_tmp_file_name_is_process_specific(self):
        """固定の .tmp だと2プロセスが相互に壊す（Codex 5周目 P1）。"""
        self._write_as("tok-a", status="started")
        leftovers = [n for n in os.listdir(os.path.join(self.tmp, "runs"))
                     if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertEqual(len(backfill._RUN_TOKEN), 32)


    def test_current_run_survives_even_when_unfinished_fill_the_cap(self):
        """未完了で上限が埋まっていても、今回の実行（completed）は落ちない。

        起動時の証跡書込みに失敗し、完了時だけ成功した場合に踏む経路
        （2026-09-18 Codex 6周目 P2）。
        """
        for i in range(backfill.MAX_RUNS_PER_DAY + 2):
            self._write_as("old%02d" % i, status="started")
        self._write_as("mine", status="completed", attempted=1)
        runs = self._runs()
        self.assertEqual(len(runs), backfill.MAX_RUNS_PER_DAY)
        self.assertIn("mine", [r.get("run") for r in runs])
        self.assertEqual(runs[-1]["run"], "mine")



class EvidenceLockTest(unittest.TestCase):
    """証跡の read-modify-write が本当に直列化されること。

    「毎回あたらしい clone だから競合しない」という前提の確証が取れなかったので、
    前提を要らなくするためにロックを入れた（2026-09-18 Codex 6周目 P1）。
    **実際に別プロセスを並走させて**、片方の追記がもう片方に消されないことを見る。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bf-lock-")

    CHILD = """
import os, sys, time
sys.path.insert(0, sys.argv[2])
os.environ['FETCH_FACTS_DIR'] = sys.argv[3]
os.environ['BACKFILL_EVIDENCE_DELAY_SEC'] = '0.4'
import backfill
if sys.argv[5] == 'nolock':
    backfill._acquire_evidence_lock = lambda path: None
    backfill._release_evidence_lock = lambda fd: None
backfill._RUN_TOKEN = sys.argv[1]
# 開始バリア: 親が go ファイルを置くまで全員待つ（同時に読ませる）
while not os.path.exists(sys.argv[4]):
    time.sleep(0.01)
backfill._write_run_evidence('2026-09-18', status='completed')
"""

    def _race(self, mode, n=5):
        import subprocess
        import sys as _sys
        import time as _time
        d = tempfile.mkdtemp(prefix="bf-race-")
        go = os.path.join(d, "go")
        script = os.path.join(d, "child.py")
        with io.open(script, "w", encoding="utf-8") as f:
            f.write(self.CHILD)
        procs = [subprocess.Popen([_sys.executable, script, "tok%02d" % i,
                                   HERE, d, go, mode])
                 for i in range(n)]
        _time.sleep(1.0)          # 全員がバリアに到達するのを待つ
        with io.open(go, "w", encoding="utf-8") as f:
            f.write("go")
        for pr in procs:
            pr.wait(timeout=120)
        path = os.path.join(d, "runs", "2026-09-18.json")
        if not os.path.exists(path):
            return []
        with io.open(path, encoding="utf-8") as f:
            return sorted(r.get("run") for r in json.load(f)["runs"])

    def test_parallel_processes_do_not_lose_entries(self):
        """ロック有り: 並走した5実行のエントリが1つも失われない。"""
        self.assertEqual(self._race("lock"),
                         sorted("tok%02d" % i for i in range(5)))

    def test_without_the_lock_entries_are_actually_lost(self):
        """ロックを外すと**必ず**落ちること＝上のテストが本当に効いている証拠。

        開始バリアで全員を同時にスタートさせ、read と write の間を
        `BACKFILL_EVIDENCE_DELAY_SEC` で広げているので、ロックが無ければ全員が
        同じ状態を読んで書き戻す＝消失更新が確定的に起きる
        （2026-09-18 Codex 7周目 P2。低負荷CIで偶然通る余地を潰すため）。
        """
        survivors = self._race("nolock")
        self.assertLess(len(survivors), 5,
                        "ロック無しでも全件残った＝この並走テストは競合を検出できていない")
    def test_lock_file_is_not_a_published_artifact(self):
        """ロックファイルは .gitignore 済み＝成果物ではない（手順8(b) の判定を汚さない）。"""
        with io.open(os.path.join(HERE, ".gitignore"), encoding="utf-8") as f:
            self.assertIn("fetch_facts/runs/*.lock", f.read())


    def test_delay_seam_is_capped(self):
        """本番で誤設定されても影響を上限内に閉じ込める（Codex 8周目 P2）。"""
        import time as _time
        tmp = tempfile.mkdtemp(prefix="bf-cap-")
        old_env = os.environ.get("BACKFILL_EVIDENCE_DELAY_SEC")
        old_dir = os.environ.get("FETCH_FACTS_DIR")
        old_cap = backfill.MAX_EVIDENCE_DELAY_SEC
        try:
            os.environ["FETCH_FACTS_DIR"] = tmp
            os.environ["BACKFILL_EVIDENCE_DELAY_SEC"] = "600"
            backfill.MAX_EVIDENCE_DELAY_SEC = 0.05
            t0 = _time.monotonic()
            backfill._write_run_evidence("2026-09-18", status="started")
            self.assertLess(_time.monotonic() - t0, 5.0)
        finally:
            backfill.MAX_EVIDENCE_DELAY_SEC = old_cap
            for k, v in (("BACKFILL_EVIDENCE_DELAY_SEC", old_env),
                         ("FETCH_FACTS_DIR", old_dir)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
