# -*- coding: utf-8 -*-
"""reel_health.py（Instagram Reel動画取得の全滅検知）の回帰テスト。

Codex敵対的レビュー最終ラウンドの指摘（2026-09-20）: test_reel_video.py の25件は
`fetch_instagram()` の戻り値までしか検証しておらず、`fetch_content._facts()` に
よる永続化（fetch_facts/<日付>.json への書き出し）を経由した経路を一度も通って
いなかった。`_facts()` 側で `video_reason` または `has_video` の保存を落としても
その25件は緑のままになる、という欠陥をこのファイルで潰す。

そのため、ここでは fetch_instagram() の戻り値を手で作るのではなく、
`fetch_content._facts()` → `fetch_content.record_facts()` という**本物の永続化経路**
を通して一時ディレクトリへ実際にJSONを書き出し、`reel_health.py` にそのファイルを
読ませる（本番の fetch_facts/ は一切汚さない。FACTS_DIR/FETCH_FACTS_DIR を
一時ディレクトリへ差し替える）。ネットワークには出ない。
"""

import contextlib
import importlib
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fetch_content  # noqa: E402
import reel_health  # noqa: E402


_TARGET_ENV_KEYS = ("FACTS_DATE", "TARGET_OVERRIDE")


@contextlib.contextmanager
def _clean_target_env(**kv):
    """FACTS_DATE/TARGET_OVERRIDEを一旦確実に外してから指定した値だけを設定する
    コンテキストマネージャ（テスト間の環境変数汚染防止。テスト実行順に依存させない）。"""
    saved = {k: os.environ.get(k) for k in _TARGET_ENV_KEYS}
    for k in _TARGET_ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in kv.items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k in _TARGET_ENV_KEYS:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


def _reel_result(video_understood, video_reason):
    """fetch_instagram() がReelに対して返す戻り値のうち、_facts()が見る
    フィールドだけを持つ最小限のdict（本物のfetch_instagram()の戻り値の部分集合）。"""
    return {
        "ok": True,
        "type": "instagram",
        "has_video": True,
        "video_understood": video_understood,
        "video_reason": video_reason,
        "depth": "full" if video_understood else "partial",
        "missing": [] if video_understood else ["video_content"],
    }


class ReelHealthPersistencePipelineTest(unittest.TestCase):
    """fetch_content._facts()→record_facts()という本物の永続化経路を通してから
    reel_health.compute_status()に読ませる（Codexの指摘の核心を潰すテスト群）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reel-health-")
        self._patches = [
            patch.object(fetch_content, "FACTS_DIR", self.tmp),
            patch.object(reel_health, "FACTS_DIR", self.tmp),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _persist(self, url, result, day):
        """本物の _facts()/record_facts() を通して一時ディレクトリへ書き出す。"""
        facts = fetch_content._facts(url, time.time(), result)
        written = fetch_content.record_facts(facts, day=day)
        self.assertIsNotNone(written, "record_facts() が書き込みに失敗した")
        return facts

    # ---- 4ケース ----

    def test_all_new_code_failures_trigger_warning(self):
        """全滅（新コードのみ）→ 警告あり。"""
        day = "2030-05-01"
        self._persist("https://www.instagram.com/reel/A1/",
                       _reel_result(False, "instagram_relay_unavailable"), day)
        self._persist("https://www.instagram.com/reel/A2/",
                       _reel_result(False, "instagram_relay_not_video"), day)
        status = reel_health.compute_status(day)
        self.assertEqual(status["total"], 2)
        self.assertEqual(status["ok"], 0)
        self.assertEqual(status["reasons"],
                          {"instagram_relay_unavailable": 1, "instagram_relay_not_video": 1})
        self.assertTrue(reel_health.should_warn(status))

    def test_legacy_only_does_not_warn(self):
        """レガシーのみ（instagram_reel_abandoned / video_reason空）→ 警告なし。"""
        day = "2030-05-02"
        self._persist("https://www.instagram.com/reel/B1/",
                       _reel_result(False, "instagram_reel_abandoned"), day)
        self._persist("https://www.instagram.com/reel/B2/",
                       _reel_result(False, ""), day)
        status = reel_health.compute_status(day)
        self.assertEqual(status["total"], 0)
        self.assertEqual(status["ok"], 0)
        self.assertFalse(reel_health.should_warn(status))

    def test_mixed_legacy_and_new_excludes_legacy_from_denominator(self):
        """混在 → 母数からレガシーが落ちる。1件成功があれば警告なし。"""
        day = "2030-05-03"
        self._persist("https://www.instagram.com/reel/C1/",
                       _reel_result(False, "instagram_reel_abandoned"), day)
        self._persist("https://www.instagram.com/reel/C2/",
                       _reel_result(False, ""), day)
        self._persist("https://www.instagram.com/reel/C3/",
                       _reel_result(False, "instagram_relay_unavailable"), day)
        self._persist("https://www.instagram.com/reel/C4/",
                       _reel_result(True, ""), day)
        status = reel_health.compute_status(day)
        self.assertEqual(status["total"], 2)  # C3(失敗) + C4(成功)のみ。C1/C2は除外
        self.assertEqual(status["ok"], 1)
        self.assertEqual(status["reasons"], {"instagram_relay_unavailable": 1})
        self.assertFalse(reel_health.should_warn(status))

    def test_missing_facts_file_returns_gracefully(self):
        """ファイル不存在 → 正常終了（totalなど0で note に不存在の旨）。"""
        status = reel_health.compute_status("2030-05-04")  # 何も書いていない日
        self.assertEqual(status["total"], 0)
        self.assertEqual(status["ok"], 0)
        self.assertEqual(status["reasons"], {})
        self.assertEqual(status["note"], "facts file not found")
        self.assertFalse(reel_health.should_warn(status))

    # ---- main()経由（実際のスクリプト実行と同じ入口） ----

    def test_main_prints_expected_line_after_real_persistence(self):
        day = "2030-05-05"
        self._persist("https://www.instagram.com/reel/D1/",
                       _reel_result(False, "instagram_relay_unavailable"), day)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = reel_health.main(["--target", day])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("REEL_VIDEO_STATUS: total=1 ok=0", out)
        self.assertIn("warn=yes", out)

    def test_target_resolution_from_facts_date_env(self):
        """--target省略時はFACTS_DATE環境変数を使う（手順書がTARGETを渡さない
        運用に対応する。resolve_target()の優先順位の回帰）。"""
        day = "2030-05-06"
        self._persist("https://www.instagram.com/reel/E1/",
                       _reel_result(True, ""), day)
        with patch.dict(os.environ, {"FACTS_DATE": day}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = reel_health.main([])
        self.assertEqual(rc, 0)
        self.assertIn("REEL_VIDEO_STATUS: total=1 ok=1", buf.getvalue())
        self.assertIn("warn=no", buf.getvalue())


class TargetResolutionTest(unittest.TestCase):
    """修正I/修正K(2026-09-20 Codex敵対的レビュー最終ラウンド指摘): resolve_target() の
    優先順位（--target > FACTS_DATE > TARGET_OVERRIDE > JSTの昨日）と、**出自を
    問わない**書式検証を固定する。修正Iの初版は --target だけを無検証で信じており、
    手順書のbash再導出（`${TARGET_OVERRIDE:-...}`。キー未設定/空文字のときだけ
    フォールバックし書式は検証しない）経由で不正なTARGET_OVERRIDEが--targetとして
    そのまま渡ると検証を素通りする非対称があった（CEO実測で再現）。

    resolve_target() は (target, ignored) のタプルを返す。"""

    def test_default_uses_jst_yesterday(self):
        """通常（何も指定しない）→ JSTの昨日。"""
        with patch.object(reel_health, "_default_target", return_value="2020-01-01"), \
             _clean_target_env():
            target, ignored = reel_health.resolve_target(None)
            self.assertEqual(target, "2020-01-01")
            self.assertEqual(ignored, [])

    def test_target_override_valid_format_is_used(self):
        """TARGET_OVERRIDE=2026-09-16 → その日を読む。"""
        with _clean_target_env(TARGET_OVERRIDE="2026-09-16"):
            target, ignored = reel_health.resolve_target(None)
            self.assertEqual(target, "2026-09-16")
            self.assertEqual(ignored, [])

    def test_target_override_invalid_format_is_ignored(self):
        """TARGET_OVERRIDE が不正形式（例 2026/09/16・yesterday）→ 無視して昨日
        （手順1・27行目と同じ無視ルール）。無視した事実がignoredに残る。"""
        with patch.object(reel_health, "_default_target", return_value="2020-01-01"):
            for bad in ("2026/09/16", "yesterday", "2026-9-16", "20260916"):
                with _clean_target_env(TARGET_OVERRIDE=bad):
                    target, ignored = reel_health.resolve_target(None)
                    self.assertEqual(target, "2020-01-01",
                                      "TARGET_OVERRIDE=%r が無視されなかった" % bad)
                    self.assertEqual(ignored, [("TARGET_OVERRIDE", bad)])

    def test_explicit_target_invalid_format_is_ignored_too(self):
        """修正Kの核心: --target(explicit)も出自を問わず同じ検証にかかる。
        --targetを無条件に最優先で信じていた旧実装では、ここが素通りしていた。"""
        with patch.object(reel_health, "_default_target", return_value="2020-01-01"), \
             _clean_target_env():
            for bad in ("yesterday", "2026/09/16", "20260916", "2026-9-16"):
                target, ignored = reel_health.resolve_target(bad)
                self.assertEqual(target, "2020-01-01",
                                  "--target=%r が無視されなかった" % bad)
                self.assertEqual(ignored, [("--target", bad)])

    def test_explicit_target_invalid_falls_through_to_next_valid_source(self):
        """--targetが不正でも、次の優先順位（FACTS_DATE）に有効な値があれば使う。"""
        with _clean_target_env(FACTS_DATE="2026-05-05"):
            target, ignored = reel_health.resolve_target("yesterday")
            self.assertEqual(target, "2026-05-05")
            self.assertEqual(ignored, [("--target", "yesterday")])

    def test_priority_explicit_beats_facts_date_beats_override(self):
        """--target と FACTS_DATE と TARGET_OVERRIDE が同時にある → 優先順位どおり
        （いずれも有効な書式の場合）。"""
        with _clean_target_env(FACTS_DATE="2026-05-05", TARGET_OVERRIDE="2026-09-16"):
            # --target が最優先
            target, ignored = reel_health.resolve_target("2099-01-01")
            self.assertEqual(target, "2099-01-01")
            self.assertEqual(ignored, [])
            # --target無し: FACTS_DATE が TARGET_OVERRIDE より優先
            target, ignored = reel_health.resolve_target(None)
            self.assertEqual(target, "2026-05-05")
            self.assertEqual(ignored, [])
        with _clean_target_env(TARGET_OVERRIDE="2026-09-16"):
            # FACTS_DATE無し: TARGET_OVERRIDE(有効書式)が使われる
            target, ignored = reel_health.resolve_target(None)
            self.assertEqual(target, "2026-09-16")
            self.assertEqual(ignored, [])


class MainCliInvalidTargetTest(unittest.TestCase):
    """修正K: resolve_target()を直接叩くだけでは、main()経由(コマンドライン経由)の
    実経路にある見逃しを検出できない（Codex名指し指摘）。手順書が再導出した不正値が
    `--target`としてそのまま渡る実運用経路を、main()を通して確認する。"""

    def _run_main(self, argv, env=None):
        buf = io.StringIO()
        with _clean_target_env(**(env or {})), contextlib.redirect_stdout(buf):
            rc = reel_health.main(argv)
        return rc, buf.getvalue()

    def test_target_yesterday_is_ignored_and_uses_jst_yesterday(self):
        """`--target yesterday` → 無視され、JSTの昨日が採用される
        （CEO実測の再現ケースそのもの）。"""
        with patch.object(reel_health, "_default_target", return_value="2020-01-01"):
            rc, out = self._run_main(["--target", "yesterday"])
        self.assertEqual(rc, 0)
        self.assertIn("ignored invalid target(s) --target='yesterday'; using 2020-01-01", out)

    def test_target_slash_format_is_ignored(self):
        """`--target 2026/09/16` → 無視される。"""
        with patch.object(reel_health, "_default_target", return_value="2020-01-01"):
            rc, out = self._run_main(["--target", "2026/09/16"])
        self.assertEqual(rc, 0)
        self.assertIn("ignored invalid target(s) --target='2026/09/16'; using 2020-01-01", out)

    def test_target_no_separator_format_is_ignored(self):
        """`--target 20260916` → 無視される。"""
        with patch.object(reel_health, "_default_target", return_value="2020-01-01"):
            rc, out = self._run_main(["--target", "20260916"])
        self.assertEqual(rc, 0)
        self.assertIn("ignored invalid target(s) --target='20260916'; using 2020-01-01", out)

    def test_target_single_digit_month_format_is_ignored(self):
        """`--target 2026-9-16` → 無視される。"""
        with patch.object(reel_health, "_default_target", return_value="2020-01-01"):
            rc, out = self._run_main(["--target", "2026-9-16"])
        self.assertEqual(rc, 0)
        self.assertIn("ignored invalid target(s) --target='2026-9-16'; using 2020-01-01", out)

    def test_valid_target_is_used_unchanged(self):
        """正常な --target は無視されずそのまま採用される（既存挙動を壊さない）。"""
        rc, out = self._run_main(["--target", "2026-09-16"])
        self.assertEqual(rc, 0)
        self.assertNotIn("ignored invalid target", out)
        self.assertIn("REEL_VIDEO_STATUS: total=0 ok=0", out)

    def test_target_override_env_yesterday_is_also_ignored_symmetrically(self):
        """比較対照: --target無し・TARGET_OVERRIDE=yesterday(env経由)でも同じく
        無視される（--target経由とenv経由で非対称にならないことの確認）。"""
        with patch.object(reel_health, "_default_target", return_value="2020-01-01"):
            rc, out = self._run_main([], env={"TARGET_OVERRIDE": "yesterday"})
        self.assertEqual(rc, 0)
        self.assertIn("ignored invalid target(s) TARGET_OVERRIDE='yesterday'; using 2020-01-01", out)


class FactsDirEnvResolutionTest(unittest.TestCase):
    """修正J(2026-09-20 Codex敵対的レビュー最終ラウンド指摘): FACTS_DIR の解決規則
    そのものを、環境変数を経由してモジュールを読み込み直す形で検証する
    （モジュール定数を直接差し替える方式だと解決規則自体を検証できないという指摘への
    対応）。fetch_content.py:1326 と一字一句同じ規則
    （`os.environ.get("FETCH_FACTS_DIR", <既定>)` = キー自体が無いときだけ既定値、
    空文字はそのまま空文字）になっているかを確認する。"""

    def setUp(self):
        self._had_env = "FETCH_FACTS_DIR" in os.environ
        self._orig_env = os.environ.get("FETCH_FACTS_DIR")

    def tearDown(self):
        if self._had_env:
            os.environ["FETCH_FACTS_DIR"] = self._orig_env
        else:
            os.environ.pop("FETCH_FACTS_DIR", None)
        # 後続テストに影響しないよう、素の状態へ読み込み直しておく。
        importlib.reload(fetch_content)
        importlib.reload(reel_health)

    def test_unset_env_uses_default_directory_next_to_module(self):
        os.environ.pop("FETCH_FACTS_DIR", None)
        importlib.reload(fetch_content)
        importlib.reload(reel_health)
        expected = os.path.join(
            os.path.dirname(os.path.abspath(reel_health.__file__)), "fetch_facts")
        self.assertEqual(reel_health.FACTS_DIR, expected)
        self.assertEqual(reel_health.FACTS_DIR, fetch_content.FACTS_DIR,
                          "未設定時はfetch_content.pyと同じ既定ディレクトリになるはず")

    def test_explicit_dir_env_is_honored(self):
        tmp = tempfile.mkdtemp(prefix="reel-health-dir-")
        try:
            os.environ["FETCH_FACTS_DIR"] = tmp
            importlib.reload(fetch_content)
            importlib.reload(reel_health)
            self.assertEqual(reel_health.FACTS_DIR, tmp)
            self.assertEqual(reel_health.FACTS_DIR, fetch_content.FACTS_DIR)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_string_env_matches_fetch_content_disabled_semantics(self):
        """FETCH_FACTS_DIR="" は fetch_content.py 側で「factsを書かない」の意味
        （record_facts() の `if not FACTS_DIR: return None`）。reel_health.py は
        これを無視して既定ディレクトリへフォールバックしてはいけない
        （フォールバックすると、今回は書いていないのに別実行の古い同日ファイルを
        読んでしまう食い違いが起きる＝Codex指摘の実害そのもの）。"""
        os.environ["FETCH_FACTS_DIR"] = ""
        importlib.reload(fetch_content)
        importlib.reload(reel_health)
        self.assertEqual(fetch_content.FACTS_DIR, "",
                          "前提: fetch_content.py側も空文字のままのはず")
        self.assertEqual(reel_health.FACTS_DIR, "",
                          "reel_health.FACTS_DIR は fetch_content.py と同じ空文字であるべき"
                          "（ledger.pyのように既定ディレクトリへフォールバックしてはいけない）")
        status = reel_health.compute_status("2020-01-01")
        self.assertEqual(status["total"], 0)
        self.assertEqual(status["note"], 'facts recording disabled (FETCH_FACTS_DIR="")')
        self.assertFalse(reel_health.should_warn(status))


if __name__ == "__main__":
    unittest.main()
