#!/usr/bin/env python3
# test_backfill_recovery.py — capture-pipeline Task G の結合テスト。
#
# 目的: 「無人回復機構（候補抽出→再取得→改善判定→次回は候補から外れる）が
# 動く」ことの証拠を残す。backfill.py / ledger.py / fetch_content.py は一切
# 変更しない（読むだけ）。標準ライブラリの unittest のみを使い、pytest は
# 使わない・requirements.txt も変えない。
#
# 隔離方針（この方式を崩さないこと）:
#   - unittest.mock.patch.dict(os.environ, {"FETCH_FACTS_DIR": ..., "FACTS_DATE": ...})
#     backfill._facts_dir() / fetch_content.record_facts() は呼び出し時に env を読むので
#     これで足りる。子プロセスにも dict(os.environ) 経由で自動継承される。
#   - 加えて unittest.mock.patch.object(ledger, "FACTS_DIR", tmpdir)。
#     ledger.FACTS_DIR は import 時に env を読むモジュール定数なので env だけでは
#     効かない。importlib.reload は使わない（reload は `from ledger import FACTS_DIR`
#     形式の参照元を更新できず、実行順序次第で本番ディレクトリを読み書きしうるため）。
#   - backfill.FETCH_SCRIPT を patch.object で差し替える。この seam は backfill.py の
#     「integration テストがこれを差し替えて本物の subprocess.run 経路を通す」という
#     設計コメントに沿ったもの。
#
# テストケース:
#   test_a_recovery_path            … 回復の本経路（Task G の完了根拠そのもの）。
#                                       facts は本物の fetch_content._facts() に
#                                       導出させ、隔離 captures.json 経由の
#                                       _resolve_rid() が実際に rid を引けたことを
#                                       rid_source=="exact" / rid_mismatch==0 で検査する。
#   test_b_child_process_contract   … 本物の fetch_content.py を通す子プロセス呼び出し規約
#   test_c_production_ledger_untouched … 本番 fetch_facts/ の非汚染
#   test_d_broken_captures_causes_rid_mismatch_not_recovery … captures.json の
#                                       対応付けが壊れた（seed URLが無い）場合、
#                                       rid が unresolved になり attempted=0 /
#                                       rid_mismatch=1 で回収が全滅することの検知力証明
#                                       （2026-09-07 Codexの敵対的レビュー対応の本体）

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import backfill  # noqa: E402
import ledger  # noqa: E402

PROD_FACTS_DIR = os.path.join(REPO_DIR, "fetch_facts")


def _snapshot_dir(path):
    """ディレクトリ配下のファイル一覧と mtime/size を辞書で返す。
    本番 fetch_facts/ が汚染されていないことをテスト前後で比較するために使う。"""
    snap = {}
    if not os.path.isdir(path):
        return snap
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if os.path.isfile(full):
            st = os.stat(full)
            snap[name] = (st.st_mtime_ns, st.st_size)
    return snap


def _write_json(path, data):
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def _make_dummy_child_script(dest_path, captures_path):
    """backfill.FETCH_SCRIPT を差し替えるテストダブル。
    subprocess.run 経路（_default_run_one）をそのまま通しつつ、実ネットワークに
    触らず、fetch_content.record_facts() を使って本番と同じ書き込み経路で
    <TARGET>.json にレコードを書く（backfill.py:201 が同じことをしているのに
    合わせる）。

    2026-09-07 Codexの敵対的レビュー対応: facts の dict（raindrop_id/rid_source/
    depth/missing 含む）を手書きしていた旧版は、実運用で最も壊れやすい
    「子プロセス起動時の captures.json による URL→rid 再解決」
    （fetch_content._resolve_rid / _facts:1132,1180）を丸ごと迂回していた。
    これだと captures 側の対応付けが壊れて実運用の回復が全滅していても、この
    ダミーは常に正しい rid を書けてしまいCIが緑になる（backfill.py:435,455の
    rid_mismatch判定を素通りする）。
    そこで facts は本物の fetch_content._facts(url, started, result) に組み立て
    させ、raindrop_id/rid_source/depth/missing はここでは一切書かない。
    fetch_content.CAPTURES_PATH（fetch_content.py:1021、モジュール定数なので env
    では差し替えられない）を隔離用 captures_path に差し替え、かつ
    fetch_content._RID_INDEX_CACHE（fetch_content.py:1022,1132）を None にリセット
    してから _resolve_rid() を初めて呼ばせる（キャッシュが温まっていると差し替えが
    効かないため）。
    URL は sys.argv[1]、captures.json のパスは環境変数 TEST_CAPTURES_PATH から
    受け取る。"""
    script = (
        "import sys, os, time\n"
        "sys.path.insert(0, %r)\n"
        "import fetch_content\n"
        "from fetch_content import record_facts\n"
        "fetch_content.CAPTURES_PATH = os.environ[\"TEST_CAPTURES_PATH\"]\n"
        "fetch_content._RID_INDEX_CACHE = None\n"
        "url = sys.argv[1]\n"
        "started = time.time()\n"
        "result = {\n"
        "    \"type\": \"web\",\n"
        "    \"ok\": True,\n"
        "    \"http_status\": 200,\n"
        "    \"text\": \"recovered article body\" * 10,\n"
        "    \"photos\": [],\n"
        "    \"cover\": \"https://example.com/cover.jpg\",\n"
        "    \"depth\": \"full\",\n"
        "    \"missing\": [],\n"
        "}\n"
        "facts = fetch_content._facts(url, started, result)\n"
        "record_facts(facts)\n"
    ) % (REPO_DIR,)
    with io.open(dest_path, "w", encoding="utf-8") as f:
        f.write(script)


def _write_captures(path, entries):
    """隔離用 captures.json を書く。実 captures.json のスキーマ
    （fetch_content._build_rid_index: list、各要素は rid(int)・source(str)を見る、
    fetch_content.py:1096-1128）に合わせる。"""
    _write_json(path, entries)


def _parse_status_line(output):
    m = re.search(r"BACKFILL_STATUS:.*$", output, re.M)
    assert m, "BACKFILL_STATUS 行が出力に見つからない: %r" % output
    line = m.group(0)
    fields = {}
    for k, v in re.findall(r"(\w+)=(\S+)", line):
        if k == "target":
            fields[k] = v
            continue
        try:
            fields[k] = int(v)
        except ValueError:
            fields[k] = v
    return fields


class TestBackfillRecovery(unittest.TestCase):
    """setUpClass/tearDownClass は「本番 fetch_facts/ が実行前後で不変」の安全網。
    test_a/test_b が実際に本番ディレクトリへ書き込んでいれば、実行順序に関係なく
    tearDownClass で検知して失敗する（テストケースC自体は可視性のための独立テスト）。"""

    @classmethod
    def setUpClass(cls):
        cls._prod_snapshot_before = _snapshot_dir(PROD_FACTS_DIR)

    @classmethod
    def tearDownClass(cls):
        after = _snapshot_dir(PROD_FACTS_DIR)
        if after != cls._prod_snapshot_before:
            raise AssertionError(
                "本番 fetch_facts/ がテスト実行中に変化した: before=%r after=%r"
                % (cls._prod_snapshot_before, after)
            )

    # ---------- テストケースA: 回復の本経路 ----------
    def test_a_recovery_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            facts_dir = os.path.join(tmp, "fetch_facts")
            os.makedirs(facts_dir, exist_ok=True)

            seed_day = "2026-01-10"   # target とは別の日（ledger.py:416 の除外を避ける）
            target = "2026-01-15"
            target_plus1 = "2026-01-16"
            cand_rid = 999001
            url = "https://example.com/recoverable-article"

            seed_record = {
                "fetched_at": seed_day + "T00:00:00Z",
                "fetcher_version": "test",
                "url": url,
                "route": "web",
                "ok": False,
                "depth": "none",
                "missing": ["fetch_failed"],
                "fail_reason": "timeout",
                "reason": "接続タイムアウト",
                "raindrop_id": cand_rid,
                "rid_source": "exact",
                "attempt_seq": 1,
            }
            _write_json(os.path.join(facts_dir, "%s.json" % seed_day), {url: seed_record})

            # 隔離 captures.json（本物の fetch_content._resolve_rid() が引く対象）。
            # source が seed URL と一致していないと exact 解決に落ちるため、テストの
            # 検知力が失われる。実 captures.json のスキーマ（rid:int, source:str）に
            # 合わせる。
            captures_path = os.path.join(tmp, "captures.json")
            _write_captures(captures_path, [
                {"rid": cand_rid, "source": url, "cluster": "test"},
            ])

            dummy_child = os.path.join(tmp, "dummy_child.py")
            _make_dummy_child_script(dummy_child, captures_path)

            env_patch = {
                "FETCH_FACTS_DIR": facts_dir,
                "FACTS_DATE": target,
                "TEST_CAPTURES_PATH": captures_path,
            }
            with patch.dict(os.environ, env_patch), \
                 patch.object(ledger, "FACTS_DIR", facts_dir), \
                 patch.object(backfill, "FETCH_SCRIPT", dummy_child):

                # 候補0件で緑になっていないことをまず確認する
                pre_candidates = ledger.backfill_candidates(target)
                self.assertEqual(len(pre_candidates), 1,
                                  "シードが候補として抽出されていない（テスト自体が無意味になる）")

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = backfill.run(target=target, limit=1, max_attempts=3,
                                       dry_run=False, timeout=30)
                self.assertEqual(rc, 0)
                status = _parse_status_line(buf.getvalue())

                # 4点個別アサート（候補0件で緑にしない）
                self.assertEqual(status["candidates"], 1)
                self.assertEqual(status["attempted"], 1)
                self.assertEqual(status["improved"], 1)
                # 2026-09-07 Codexの敵対的レビュー対応: rid が本物の解決器
                # （captures.json 経由の _resolve_rid）で実際に引けたこと・
                # backfill が rid_mismatch に落としていないことを検査する。
                self.assertEqual(status["rid_mismatch"], 0)
                # ⚠️ T ではなく T+1 で確認する。T で確認すると「当日試行済みだから除外
                # された」だけでも0件になり、成功して閉じたことの証拠にならない。
                post_candidates = ledger.backfill_candidates(target_plus1)
                self.assertEqual(len(post_candidates), 0)

                # ディスク実体もアサートする（カウンタだけでは書き込み破損を見逃す）
                with io.open(os.path.join(facts_dir, "%s.json" % target),
                              encoding="utf-8") as f:
                    store = json.load(f)
                rec = store.get(url)
                self.assertIsNotNone(rec, "対象URLのレコードがTARGET日のファイルに無い")
                self.assertTrue(rec["ok"])
                self.assertEqual(rec["depth"], "full")
                self.assertEqual(rec["raindrop_id"], cand_rid)
                # 本物の _resolve_rid() が隔離 captures.json を実際に引いて exact 一致
                # したことの証拠（rid_source を自分で書いていない＝本物の導出結果）。
                self.assertEqual(rec["rid_source"], "exact")

                # グループ状態もアサート（回収完了の意味を数値でなく状態で示す）
                records = ledger._load_all_records()
                groups = ledger._build_groups(records)
                g = groups[("rid", cand_rid)]
                self.assertNotEqual(g["state"], "open")
                self.assertEqual(g["state"], "resolved")

    # ---------- テストケースB: 子プロセスの呼び出し規約（本物の fetch_content.py） ----------
    def test_b_child_process_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            facts_dir = os.path.join(tmp, "fetch_facts")
            os.makedirs(facts_dir, exist_ok=True)

            seed_day = "2026-02-10"
            target = "2026-02-15"
            cand_rid = 999002
            url = "http://127.0.0.1/whatever"   # SSRF ガードで確実に弾かれる(実ネットワーク不要)

            seed_record = {
                "fetched_at": seed_day + "T00:00:00Z",
                "fetcher_version": "test",
                "url": url,
                "route": "web",
                "ok": False,
                "depth": "none",
                "missing": ["fetch_failed"],
                "fail_reason": "timeout",
                "reason": "接続タイムアウト",
                "raindrop_id": cand_rid,
                "rid_source": "exact",
                "attempt_seq": 1,
            }
            _write_json(os.path.join(facts_dir, "%s.json" % seed_day), {url: seed_record})

            env_patch = {
                "FETCH_FACTS_DIR": facts_dir,
                "FACTS_DATE": target,
                "GEMINI_API_KEY": "",   # 無くても落ちない経路であることを明示する
            }
            # FETCH_SCRIPT は差し替えない（既定=本物の fetch_content.py）
            with patch.dict(os.environ, env_patch), \
                 patch.object(ledger, "FACTS_DIR", facts_dir):

                self.assertIsNone(backfill.RUN_ONE)  # RUN_ONE は丸ごと差し替えていない

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = backfill.run(target=target, limit=1, max_attempts=3,
                                       dry_run=False, timeout=30)
                self.assertEqual(rc, 0)

                # backfill が依存している規約「URLを1つ渡すと FETCH_FACTS_DIR/FACTS_DATE
                # に沿ってレコードを書く」が生きていることを検査する
                target_path = os.path.join(facts_dir, "%s.json" % target)
                self.assertTrue(os.path.exists(target_path),
                                 "本物の fetch_content.py がTARGET日のファイルを書いていない")
                with io.open(target_path, encoding="utf-8") as f:
                    store = json.load(f)
                rec = store.get(url)
                self.assertIsNotNone(rec, "対象URLのレコードが書かれていない")
                self.assertFalse(rec["ok"])
                self.assertEqual(rec.get("fail_reason"), "ssrf_rejected")

    # ---------- テストケースC: 本番台帳の非汚染 ----------
    def test_c_production_ledger_untouched(self):
        current = _snapshot_dir(PROD_FACTS_DIR)
        self.assertEqual(current, self._prod_snapshot_before,
                          "本番 fetch_facts/ の内容がテスト中に変化した")
        # FETCH_SCRIPT の既定値が実在する fetch_content.py を指すこと
        # （このseamが将来壊れたらここで落ちる）
        expected = os.path.join(REPO_DIR, "fetch_content.py")
        self.assertEqual(backfill.FETCH_SCRIPT, expected)
        self.assertTrue(os.path.isfile(backfill.FETCH_SCRIPT))

    # ---------- テストケースD: captures.json 対応付け破損の検知力 ----------
    def test_d_broken_captures_causes_rid_mismatch_not_recovery(self):
        """2026-09-07 Codexの敵対的レビュー対応の本体。
        candidate と同じシナリオを、隔離 captures.json に seed URL が存在しない
        （＝実運用で captures 側の対応付けが壊れた）状態で流す。本物の
        _resolve_rid() は unresolved（rid=None）を返すので、backfill.py:435,455
        の rid_ok チェックに落ちて rid_mismatch になり、attempted は増えない
        （＝回収が全滅する）ことを検査する。これが「テストが実際に検知できる」
        ことの証拠。test_a と対になっており、逆向きにしないと test_a の緑だけでは
        この失敗モードを見逃す。"""
        with tempfile.TemporaryDirectory() as tmp:
            facts_dir = os.path.join(tmp, "fetch_facts")
            os.makedirs(facts_dir, exist_ok=True)

            seed_day = "2026-03-10"
            target = "2026-03-15"
            cand_rid = 999003
            url = "https://example.com/broken-mapping-article"

            seed_record = {
                "fetched_at": seed_day + "T00:00:00Z",
                "fetcher_version": "test",
                "url": url,
                "route": "web",
                "ok": False,
                "depth": "none",
                "missing": ["fetch_failed"],
                "fail_reason": "timeout",
                "reason": "接続タイムアウト",
                "raindrop_id": cand_rid,
                "rid_source": "exact",
                "attempt_seq": 1,
            }
            _write_json(os.path.join(facts_dir, "%s.json" % seed_day), {url: seed_record})

            # 壊れた captures.json: seed URL を含まない（空リスト）。実運用で
            # captures 側の対応付けが壊れた状況を再現する。
            captures_path = os.path.join(tmp, "captures.json")
            _write_captures(captures_path, [])

            dummy_child = os.path.join(tmp, "dummy_child.py")
            _make_dummy_child_script(dummy_child, captures_path)

            env_patch = {
                "FETCH_FACTS_DIR": facts_dir,
                "FACTS_DATE": target,
                "TEST_CAPTURES_PATH": captures_path,
            }
            with patch.dict(os.environ, env_patch), \
                 patch.object(ledger, "FACTS_DIR", facts_dir), \
                 patch.object(backfill, "FETCH_SCRIPT", dummy_child):

                pre_candidates = ledger.backfill_candidates(target)
                self.assertEqual(len(pre_candidates), 1,
                                  "シードが候補として抽出されていない（テスト自体が無意味になる）")

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = backfill.run(target=target, limit=1, max_attempts=3,
                                       dry_run=False, timeout=30)
                self.assertEqual(rc, 0)
                status = _parse_status_line(buf.getvalue())

                self.assertEqual(status["candidates"], 1)
                # captures 対応付けが壊れているので rid が解決できず、回収は
                # 一切カウントされない（実運用の全滅を再現）。
                self.assertEqual(status["attempted"], 0)
                self.assertEqual(status["rid_mismatch"], 1)


if __name__ == "__main__":
    unittest.main()
