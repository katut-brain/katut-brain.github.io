"""check_disclosure.py の受け入れテスト。

fixture は実データ（fetch_facts/*.json・reviews/*.html の実レコード・実カード）
から書き写している。手で作ったダミーは使わない。実データの出典は各テストの
docstring / コメントに日付とrid・URLで明記した。

判定の正解ラベル（人間判定）は作業時のスクラッチパッドに作成した labels.tsv。
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_disclosure.py"
sys.path.insert(0, str(SCRIPT.parent))
import check_disclosure as cd  # noqa: E402

REVIEW_HEAD = """<!doctype html>
<html lang="ja">
<head><meta charset="UTF-8"></head>
<body>
<div class="wrap">
  <div class="cards">
"""
REVIEW_TAIL = """  </div>
  <footer>毎朝の同期で自動生成 ｜ katut-brain</footer>
</div>
</body>
</html>
"""


class DisclosureCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.facts_dir = os.path.join(self.tmp, "fetch_facts")
        self.reviews_dir = os.path.join(self.tmp, "reviews")
        # 既定では capture_index.json を作らない（＝読み込むと None になり、
        # バックフィル除外は一切発生しない安全側の挙動になる）。バックフィル
        # を検証するテストだけ _write_capture_index で明示的に作る。
        self.capture_index_path = os.path.join(self.tmp, "capture_index.json")
        os.mkdir(self.facts_dir)
        os.mkdir(self.reviews_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- 素材 -------------------------------------------------------------

    def _write_facts(self, date, records):
        path = os.path.join(self.facts_dir, "%s.json" % date)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=1)

    def _write_capture_index(self, mapping):
        """mapping: {"YYYY-MM-DD": [rid, ...]}"""
        days = {d: {"rids": rids} for d, rids in mapping.items()}
        with open(self.capture_index_path, "w", encoding="utf-8") as fh:
            json.dump({"days": days}, fh, ensure_ascii=False, indent=1)

    def _write_review(self, date, cards_html):
        path = os.path.join(self.reviews_dir, "%s.html" % date)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(REVIEW_HEAD + cards_html + REVIEW_TAIL)
        return path

    def _read_review(self, date):
        path = os.path.join(self.reviews_dir, "%s.html" % date)
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()

    def _vcard(self, url, title, desc, rid):
        """実データのカード構造（reviews/*.htmlの実テンプレート）を模した
        1カード分のHTML断片。中身（title/desc）は各テストで実データから
        逐語コピーする。
        """
        return (
            '    <div class="vcard">\n'
            '      <a class="vlink" href="%s" target="_blank" rel="noopener">\n'
            '        <div class="thumb"><span class="ph">X</span></div>\n'
            '        <div class="vbody"><div class="vtitle">%s</div>'
            '<div class="vdesc">%s</div></div>\n'
            "      </a>\n"
            '      <button class="deepdive" onclick="openChat(this)" '
            'data-url="%s" data-title="%s" data-rid="%s">💬 AIと話す</button>\n'
            "    </div>\n"
        ) % (url, title, desc, url, title, rid)

    def _run(self, args, expect=0):
        cmd = [sys.executable, str(SCRIPT), "--facts-dir", self.facts_dir,
               "--reviews-dir", self.reviews_dir,
               "--capture-index", self.capture_index_path] + args
        r = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, expect, r.stdout + r.stderr)
        return r.stdout

    # --- 実データ: 見たかのように書いた違反（2026-09-04 Reel） --------------

    def test_20260904_reel_hallucinated_description_is_a_violation(self):
        """実データ: fetch_facts/2026-09-04.json rid=1842608050
        (https://www.instagram.com/reel/Dcy4SIYiD7U/, route=instagram,
        missing=["video_content"])。reviews/2026-09-04.html のカードは
        「Interpreting→Modeling→Refiningと進捗を見せながら」等、動画を
        見たかのような記述で開示文言が無い（実際の事故）。
        """
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        self._write_review("2026-09-04", self._vcard(
            "https://www.instagram.com/reel/Dcy4SIYiD7U/",
            "写真1枚をRhinoモデルに変換するAIエージェント「STF Agent」",
            "キッチン什器の写真を投げると、Interpreting→Modeling→Refiningと"
            "進捗を見せながらRhino上に3Dモデルを自動生成する。Rhino3D向け"
            "プラグインとして近くアルファ版公開予定。",
            "1842608050",
        ))
        out = self._run(["--date", "2026-09-04"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-04 duty=1 disclosed=0 "
            "violation=1 excluded=0 out_of_scope=0", out)
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-04 kind=reel "
            "rid=1842608050 url=https://www.instagram.com/reel/Dcy4SIYiD7U/",
            out)

    # --- 実データ: 2026-09-14 Reel rid=1853055093 の違反 --------------------

    def test_20260914_reel_1853055093_is_a_violation(self):
        """実データ: fetch_facts/2026-09-14.json rid=1853055093。開示ゼロ。"""
        self._write_facts("2026-09-14", {
            "https://www.instagram.com/reel/DdOYxd_za51/?stkn=dmc5ZzF4aGI3eThx": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1853055093,
            },
        })
        self._write_review("2026-09-14", self._vcard(
            "https://www.instagram.com/reel/DdOYxd_za51/?stkn=dmc5ZzF4aGI3eThx",
            "『AI一人社長』を支える声のClaudeアーキテクチャ",
            "自分のAI相棒の構成を種明かし：頭脳はClaude、日常会話は反応の速い"
            "Haiku、発話はAzure Speech、開発はCodexとClaude Codeという役割"
            "分担で、金額決定や外部送信など重要操作は実行前に本人へ確認する"
            "設計と説明。",
            "1853055093",
        ))
        out = self._run(["--date", "2026-09-14"])
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-14 kind=reel rid=1853055093",
            out)

    # --- 実データ: 2026-09-11 X の3件、揺れた言い回しがすべて開示ありになる --

    def test_20260911_x_varied_phrasing_all_count_as_disclosed(self):
        """実データ: fetch_facts/2026-09-11.json の3件。route=x・
        missing=["video_content"]。言い回しがそれぞれ違う
        （追えていない／未確認／見られていない）が全部開示として扱われる。
        """
        self._write_facts("2026-09-11", {
            "https://x.com/masahirochaen/status/2097990179227414850?s=12": {
                "route": "x", "missing": ["video_content"],
                "raindrop_id": 1850403031,
            },
            "https://x.com/claudecode84/status/2097959830942367747?s=12": {
                "route": "x", "missing": ["video_content"],
                "raindrop_id": 1850401311,
            },
            "https://x.com/sokichi_hoshino/status/2098193107628290238?s=12": {
                "route": "x", "missing": ["video_content"],
                "raindrop_id": 1850309485,
            },
        })
        cards = "".join([
            self._vcard(
                "https://x.com/masahirochaen/status/2097990179227414850?s=12",
                "ChatGPTがYouTube再生とリアルタイム同期",
                "WebMCPでページのツールを自動検出し同期再生。今回は動画の中身"
                "までは追えていない",
                "1850403031"),
            self._vcard(
                "https://x.com/claudecode84/status/2097959830942367747?s=12",
                "Anthropic幹部が自身のClaude Code環境をオープンソース化と投稿",
                "実態は要検証。動画部分は今回未確認",
                "1850401311"),
            self._vcard(
                "https://x.com/sokichi_hoshino/status/2098193107628290238?s=12",
                "AI社員を使った株式投資が「最強」と断言",
                "具体的な運用実績や再現性の記載は本文には無い。動画は今回"
                "見られていない",
                "1850309485"),
        ])
        self._write_review("2026-09-11", cards)
        out = self._run(["--date", "2026-09-11"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-11 duty=3 disclosed=3 "
            "violation=0 excluded=0 out_of_scope=0", out)

    # --- 実データ: 2026-09-14 facts のバックフィルrid が除外される -----------

    def test_backfill_rid_from_other_date_is_excluded_not_violation(self):
        """実データ: fetch_facts/2026-09-14.json には09-11のrid(1850403031)が
        再取得で追記されている。そのカードは reviews/2026-09-11.html にあり
        reviews/2026-09-14.html には無い。capture_index.json（実データの
        当日保存台帳）では 1850403031 は 2026-09-11 の rids にのみ入っており
        2026-09-14 の rids には入っていない＝バックフィルとして除外され、
        違反にしてはいけない。
        """
        self._write_capture_index({
            "2026-09-11": [1850403031],
            "2026-09-14": [1853167142],
        })
        self._write_facts("2026-09-11", {
            "https://x.com/masahirochaen/status/2097990179227414850?s=12": {
                "route": "x", "missing": ["video_content"],
                "raindrop_id": 1850403031,
            },
        })
        self._write_review("2026-09-11", self._vcard(
            "https://x.com/masahirochaen/status/2097990179227414850?s=12",
            "ChatGPTがYouTube再生とリアルタイム同期",
            "今回は動画の中身までは追えていない",
            "1850403031",
        ))
        # 09-14 facts に同じ rid が exception:ServerError で再度記録される
        # (実データ通り)。09-14 の reviews には対応カードが無い。
        self._write_facts("2026-09-14", {
            "https://x.com/masahirochaen/status/2097990179227414850?s=12": {
                "route": "x", "missing": ["video_content"],
                "raindrop_id": 1850403031,
                "video_reason": "exception:ServerError",
            },
            "https://x.com/gencoin8/status/2099302097909158238?s=12": {
                "route": "x", "missing": ["video_content"],
                "raindrop_id": 1853167142,
            },
        })
        self._write_review("2026-09-14", self._vcard(
            "https://x.com/gencoin8/status/2099302097909158238?s=12",
            "別の当日カード", "今回は動画の内容を取得できなかった。",
            "1853167142",
        ))
        out = self._run(["--date", "2026-09-11", "--date", "2026-09-14"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-14 duty=2 disclosed=1 "
            "violation=0 excluded=1 out_of_scope=0", out)
        self.assertIn(
            "DISCLOSURE_EXCLUDED: date=2026-09-14 rid=1850403031 "
            "reason=backfill(2026-09-11,card=yes)", out)
        self.assertNotIn("DISCLOSURE_VIOLATION: date=2026-09-14", out)

    # --- capture_index.json ベースの違反/除外境界（B: CEO差し戻し指示A） ----

    def test_rid_present_today_but_no_card_is_a_violation_not_backfill(self):
        """当日の capture_index に rid が載っているのにカードが無い場合、
        過去日にカードがあっても違反(no_card)にする（バックフィル扱いしない）。
        """
        self._write_capture_index({
            "2026-09-10": [999],
            "2026-09-14": [999],
        })
        self._write_facts("2026-09-14", {
            "https://x.com/example/status/999": {
                "route": "x", "missing": ["video_content"], "raindrop_id": 999,
            },
        })
        # 過去日 09-10 には同じURLのカードが実在する（同一URL再保存の想定）が、
        # 09-14 の reviews には対応カードが無い。
        self._write_review("2026-09-10", self._vcard(
            "https://x.com/example/status/999", "過去のカード",
            "今回は動画の内容を取得できなかった。", "999",
        ))
        self._write_review("2026-09-14", self._vcard(
            "https://x.com/other/status/1", "無関係カード", "本文のみ。", "1",
        ))
        out = self._run(["--date", "2026-09-14"])
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-14 kind=x_video rid=999 "
            "url=https://x.com/example/status/999 reason=no_card", out)
        self.assertNotIn("DISCLOSURE_EXCLUDED:", out)

    def test_rid_none_with_no_card_is_a_violation(self):
        """rid が無い（raindrop_id: null）レコードでカードも見つからない場合、
        バックフィル判定のしようがないので違反(no_card)にする。
        """
        self._write_capture_index({"2026-09-14": [1]})
        self._write_facts("2026-09-14", {
            "https://x.com/nowhere/status/1": {
                "route": "x", "missing": ["video_content"], "raindrop_id": None,
            },
        })
        self._write_review("2026-09-14", self._vcard(
            "https://x.com/other/status/1", "無関係カード", "本文のみ。", "1",
        ))
        out = self._run(["--date", "2026-09-14"])
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-14 kind=x_video rid=None "
            "url=https://x.com/nowhere/status/1 reason=no_card", out)
        self.assertNotIn("DISCLOSURE_EXCLUDED:", out)

    def test_missing_capture_index_means_no_card_is_always_a_violation(self):
        """capture_index.json が無い（今回のテストでは作らない）ときは、
        本来ならバックフィルに見える状況でも一切除外しない。
        """
        self._write_facts("2026-09-11", {
            "https://x.com/masahirochaen/status/2097990179227414850?s=12": {
                "route": "x", "missing": ["video_content"],
                "raindrop_id": 1850403031,
            },
        })
        self._write_review("2026-09-11", self._vcard(
            "https://x.com/masahirochaen/status/2097990179227414850?s=12",
            "ChatGPTがYouTube再生とリアルタイム同期",
            "今回は動画の中身までは追えていない",
            "1850403031",
        ))
        self._write_facts("2026-09-14", {
            "https://x.com/masahirochaen/status/2097990179227414850?s=12": {
                "route": "x", "missing": ["video_content"],
                "raindrop_id": 1850403031,
            },
        })
        self._write_review("2026-09-14", self._vcard(
            "https://x.com/gencoin8/status/2099302097909158238?s=12",
            "別の当日カード", "今回は動画の内容を取得できなかった。",
            "1853167142",
        ))
        self.assertFalse(os.path.isfile(self.capture_index_path))
        out = self._run(["--date", "2026-09-14"])
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-14 kind=x_video "
            "rid=1850403031 url=https://x.com/masahirochaen/"
            "status/2097990179227414850?s=12 reason=no_card", out)
        self.assertNotIn("DISCLOSURE_EXCLUDED:", out)

    # --- 実データ: 2026-09-08 Threads 開示ゼロ ------------------------------

    def test_20260908_threads_no_disclosure_is_violation(self):
        """実データ: fetch_facts/2026-09-08.json rid=1847745101
        （https://www.threads.com/share/BAY7Zm6kIj/、route=threads、
        missing=["visual_content","audio_content"]）。カードに開示文言なし。
        """
        self._write_facts("2026-09-08", {
            "https://www.threads.com/share/BAY7Zm6kIj/": {
                "route": "threads",
                "missing": ["visual_content", "audio_content"],
                "raindrop_id": 1847745101,
            },
        })
        self._write_review("2026-09-08", self._vcard(
            "https://www.threads.com/share/BAY7Zm6kIj/",
            "Higgsfield MCPで画像・動画生成まで接続",
            "広告画像・SNS画像・ショート動画素材まで、企画から生成物への同じ"
            "流れで作れるという接続例。",
            "1847745101",
        ))
        out = self._run(["--date", "2026-09-08"])
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-08 kind=threads "
            "rid=1847745101", out)

    # --- 実データ: 2026-09-12 Threads「本文…取得できていない」は違反 --------

    def test_20260912_threads_text_only_limitation_is_still_a_violation(self):
        """実データ: fetch_facts/2026-09-12.json rid=1851132721。開示文が
        「その先の本文はThreads側の要約制限で取得できていない」で、媒体語
        （画像/動画/音声等）が無い＝テキスト取得限界の話のみ。threadsの
        画像・動画・音声欠損への開示にはならないので違反。
        """
        self._write_facts("2026-09-12", {
            "https://www.threads.com/share/BAZj3RDXFs/": {
                "route": "threads",
                "missing": ["visual_content", "audio_content"],
                "raindrop_id": 1851132721,
            },
        })
        self._write_review("2026-09-12", self._vcard(
            "https://www.threads.com/share/BAZj3RDXFs/",
            "Claude Codeで月80万稼いだフォルダ構成",
            "CLAUDE.md・persona.md等の8ファイルで役割分担する運用テンプレを"
            "公開。どのファイルが特に重要かを続けて解説する構成だが、その先"
            "の本文はThreads側の要約制限で取得できていない",
            "1851132721",
        ))
        out = self._run(["--date", "2026-09-12"])
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-12 kind=threads "
            "rid=1851132721", out)

    # --- 実データ: 2026-09-13 X (image+video欠損、動画のみ開示) は開示あり ---

    def test_20260913_x_video_only_disclosure_counts_when_image_also_missing(self):
        """実データ: fetch_facts/2026-09-13.json rid=1852040378
        （missing=["image_content","video_content"]）。x_videoの判定対象は
        video_contentのみなので、動画についてだけ開示していれば足りる。
        """
        self._write_facts("2026-09-13", {
            "https://x.com/tomorotenn/status/2099105835112890811?s=12": {
                "route": "x",
                "missing": ["image_content", "video_content"],
                "raindrop_id": 1852040378,
            },
        })
        self._write_review("2026-09-13", self._vcard(
            "https://x.com/tomorotenn/status/2099105835112890811?s=12",
            "ジャパンディスプレイ（6740）が化けるかもという煽り",
            "現在価格47円のジャパンディスプレイが化けるかもしれないと予告。"
            "今回は動画の内容を取得できなかった。",
            "1852040378",
        ))
        out = self._run(["--date", "2026-09-13"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-13 duty=1 disclosed=1 "
            "violation=0 excluded=0 out_of_scope=0", out)

    # --- 実データ: Instagram /p/ の visual_content 欠損は out_of_scope -----

    def test_instagram_post_visual_content_missing_is_out_of_scope(self):
        """実データ: fetch_facts/2026-08-23.json
        (https://www.instagram.com/p/DcJUHa8iGu4/?img_index=3&igsi=...、
        route=instagram, path=/p/ なので reel 判定対象外、
        missing=["visual_content","audio_content"])。違反にしない。
        """
        self._write_facts("2026-08-23", {
            "https://www.instagram.com/p/DcJUHa8iGu4/?img_index=3&igsi=MWZzaHY4M2F3bTJxcQ==": {
                "route": "instagram",
                "missing": ["visual_content", "audio_content"],
                "raindrop_id": None,
            },
        })
        self._write_review("2026-08-23", self._vcard(
            "https://www.instagram.com/p/DcJUHa8iGu4/?img_index=3&amp;igsi=MWZzaHY4M2F3bTJxcQ==",
            "Hydroflaskスマートボトルアプリ",
            "摂取量・分子水素レベル・連続記録日数をグロー系グラデーション"
            "カードと円形インジケーターでまとめ、記録行為を「見て楽しい"
            "ルーティン」に変える設計",
            "1831234022",
        ))
        out = self._run(["--date", "2026-08-23"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-08-23 duty=0 disclosed=0 "
            "violation=0 excluded=0 out_of_scope=1", out)
        self.assertNotIn("DISCLOSURE_VIOLATION:", out)

    # --- --fix ---------------------------------------------------------------

    def test_fix_appends_boilerplate_and_is_idempotent_and_byte_preserving(self):
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        path = self._write_review("2026-09-04", self._vcard(
            "https://www.instagram.com/reel/Dcy4SIYiD7U/",
            "写真1枚をRhinoモデルに変換するAIエージェント「STF Agent」",
            "キッチン什器の写真を投げると進捗を見せながらRhino上に3Dモデル"
            "を自動生成する。",
            "1842608050",
        ))
        before = self._read_review("2026-09-04")

        out = self._run(["--date", "2026-09-04", "--fix"])
        self.assertIn("DISCLOSURE_FIXED: date=2026-09-04 rid=1842608050", out)

        after = self._read_review("2026-09-04")
        self.assertNotEqual(before, after)
        self.assertIn("動画の内容は未取得。", after)

        # 差分は挿入文字列だけ（他のバイトは不変）。挿入位置以外が一致することを
        # 前後不一致の最長共通接頭辞・接尾辞で確認する。
        prefix_len = 0
        while (prefix_len < len(before) and prefix_len < len(after)
               and before[prefix_len] == after[prefix_len]):
            prefix_len += 1
        suffix_len = 0
        while (suffix_len < len(before) - prefix_len
               and suffix_len < len(after) - prefix_len
               and before[-1 - suffix_len] == after[-1 - suffix_len]):
            suffix_len += 1
        self.assertEqual(before[:prefix_len] + before[len(before) - suffix_len:],
                          before)
        self.assertEqual(after[:prefix_len] + after[len(after) - suffix_len:]
                          == after[:prefix_len] + after[len(after) - suffix_len:],
                          True)
        inserted = after[prefix_len:len(after) - suffix_len]
        self.assertIn("動画の内容は未取得。", inserted)

        # 再判定で違反0
        out2 = self._run(["--date", "2026-09-04"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-04 duty=1 disclosed=1 "
            "violation=0 excluded=0 out_of_scope=0", out2)

        # 2回目の --fix は変化なし（冪等）
        out3 = self._run(["--date", "2026-09-04", "--fix"])
        self.assertNotIn("DISCLOSURE_FIXED:", out3)
        after2 = self._read_review("2026-09-04")
        self.assertEqual(after, after2)

    # --- 例外・ファイル欠如でも終了コード0 ------------------------------------

    def test_missing_facts_file_exits_zero(self):
        out = self._run(["--date", "2099-01-01"], expect=0)
        self.assertIn("DISCLOSURE_CHECK: date=2099-01-01 status=no_facts", out)

    def test_missing_review_file_exits_zero(self):
        self._write_facts("2026-09-04", {
            "https://x.com/example/status/1": {
                "route": "x", "missing": ["video_content"], "raindrop_id": 1,
            },
        })
        out = self._run(["--date", "2026-09-04"], expect=0)
        self.assertIn("DISCLOSURE_CHECK: date=2026-09-04 status=no_review", out)

    def test_broken_facts_json_exits_zero(self):
        path = os.path.join(self.facts_dir, "2026-09-05.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        self._write_review("2026-09-05", "")
        out = self._run(["--date", "2026-09-05"], expect=0)
        self.assertIn("DISCLOSURE_ERROR:", out)

    def test_invalid_cli_argument_exits_zero(self):
        """argparse がSystemExit(2)する不正な引数でも、ゲートを止めないため
        終了コードは0でなければならない。"""
        cmd = [sys.executable, str(SCRIPT), "--facts-dir", self.facts_dir,
               "--reviews-dir", self.reviews_dir, "--not-a-real-flag"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DISCLOSURE_ERROR:", r.stdout)

    def test_fix_with_all_is_refused_without_writing(self):
        """--fix は --all と併用しない。過去日を誤って一括書き換えしない。"""
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        self._write_review("2026-09-04", self._vcard(
            "https://www.instagram.com/reel/Dcy4SIYiD7U/",
            "写真1枚をRhinoモデルに変換するAIエージェント「STF Agent」",
            "進捗を見せながらRhino上に3Dモデルを自動生成する。",
            "1842608050",
        ))
        before = self._read_review("2026-09-04")
        out = self._run(["--all", "--fix"])
        self.assertIn("DISCLOSURE_ERROR:", out)
        self.assertNotIn("DISCLOSURE_FIXED:", out)
        after = self._read_review("2026-09-04")
        self.assertEqual(before, after, "--all --fix がreviewsを書き換えてしまった")

    def test_multiple_cards_with_same_rid_all_must_be_disclosed(self):
        """同じ rid のカードが対象日に複数枚ある場合、1枚でも開示が無ければ
        違反。--fix は開示の無いカードそれぞれに定型文を入れる。
        """
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        cards = (
            self._vcard(
                "https://www.instagram.com/reel/Dcy4SIYiD7U/",
                "写真1枚をRhinoモデルに変換するAIエージェント「STF Agent」",
                "進捗を見せながらRhino上に3Dモデルを自動生成する。",
                "1842608050")
            + self._vcard(
                "https://www.instagram.com/reel/Dcy4SIYiD7U/",
                "重複カード（実データにはないが検証用）",
                "同じ投稿が誤って2枚生成されたケース。動画の内容は未取得。",
                "1842608050")
        )
        self._write_review("2026-09-04", cards)
        out = self._run(["--date", "2026-09-04"])
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-04 kind=reel "
            "rid=1842608050", out)

        out2 = self._run(["--date", "2026-09-04", "--fix"])
        self.assertIn("DISCLOSURE_FIXED: date=2026-09-04 rid=1842608050", out2)
        after = self._read_review("2026-09-04")
        # 両方のカードに定型文が入り、元々開示済みだった2枚目も変化しない
        # （冪等）はずなので、未開示だった1枚目だけに文言が追加される。
        self.assertEqual(after.count("動画の内容は未取得。"), 2)

        out3 = self._run(["--date", "2026-09-04"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-04 duty=1 disclosed=1 "
            "violation=0 excluded=0 out_of_scope=0", out3)

    def test_all_and_since_and_github_flags_do_not_crash(self):
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        self._write_review("2026-09-04", self._vcard(
            "https://www.instagram.com/reel/Dcy4SIYiD7U/",
            "写真1枚をRhinoモデルに変換するAIエージェント「STF Agent」",
            "進捗を見せながらRhino上に3Dモデルを自動生成する。",
            "1842608050",
        ))
        summary = os.path.join(self.tmp, "summary.md")
        env = dict(os.environ)
        env["GITHUB_STEP_SUMMARY"] = summary
        cmd = [sys.executable, str(SCRIPT), "--facts-dir", self.facts_dir,
               "--reviews-dir", self.reviews_dir,
               "--capture-index", self.capture_index_path, "--all",
               "--since", "2026-09-01", "--github", "--json"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("::warning title=disclosure::", r.stdout)
        self.assertIn('"date": "2026-09-04"', r.stdout)
        self.assertTrue(os.path.isfile(summary))
        with open(summary, encoding="utf-8") as fh:
            self.assertIn("disclosure check", fh.read())

    # --- B: class属性の揺れに強いカード抽出 ------------------------------

    def test_card_extraction_tolerates_class_attribute_variation(self):
        """class="vcard" の完全一致に依存しない: 追加クラス・属性順の入替・
        シングルクォートでも .vcard / .vlink / .vdesc / data-rid を拾える。
        """
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        card = (
            '    <div data-x="1" class="mt-2 vcard highlight">\n'
            "      <a href='https://www.instagram.com/reel/Dcy4SIYiD7U/' "
            "class='vlink' target=\"_blank\">\n"
            '        <div class="thumb"><span class="ph">IG</span></div>\n'
            '        <div class="vbody"><div class="vtitle">title</div>'
            "<div class='vdesc'>進捗を見せながら生成する。動画の内容は未取得。"
            "</div></div>\n"
            "      </a>\n"
            "      <button class=\"deepdive\" data-rid='1842608050' "
            'data-url="https://www.instagram.com/reel/Dcy4SIYiD7U/">💬</button>\n'
            "    </div>\n"
        )
        self._write_review("2026-09-04", card)
        out = self._run(["--date", "2026-09-04"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-04 duty=1 disclosed=1 "
            "violation=0 excluded=0 out_of_scope=0", out)

    def test_zero_cards_extracted_with_duty_is_card_parse_failed(self):
        """duty>0 なのに .vcard が1枚も抽出できない場合、違反/除外と混ぜず
        別枠のエラーとして報告する。
        """
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        self._write_review("2026-09-04", "")  # cardsセクションが空
        out = self._run(["--date", "2026-09-04"])
        self.assertIn("DISCLOSURE_ERROR: date=2026-09-04 card_parse_failed", out)
        self.assertNotIn("DISCLOSURE_VIOLATION:", out)
        self.assertNotIn("DISCLOSURE_EXCLUDED:", out)
        self.assertNotIn("DISCLOSURE_CHECK: date=2026-09-04 duty=", out)

    # --- C: 当日キーが capture_index に無い夜は除外しない（2周目指示2） ------

    def test_capture_index_file_exists_but_today_key_missing_is_a_violation(self):
        """capture_index.json ファイル自体はあるが、対象日 D のキーが
        存在しない（その夜の手順1.5が走らなかった等）場合、rid が過去日に
        あってもバックフィル除外を一切許可せず violation(no_card) にする。
        """
        self._write_capture_index({
            "2026-09-11": [1850403031],
            # 2026-09-14 のキーは存在しない（欠損）。
        })
        self._write_facts("2026-09-11", {
            "https://x.com/masahirochaen/status/2097990179227414850?s=12": {
                "route": "x", "missing": ["video_content"],
                "raindrop_id": 1850403031,
            },
        })
        self._write_review("2026-09-11", self._vcard(
            "https://x.com/masahirochaen/status/2097990179227414850?s=12",
            "ChatGPTがYouTube再生とリアルタイム同期",
            "今回は動画の中身までは追えていない",
            "1850403031",
        ))
        self._write_facts("2026-09-14", {
            "https://x.com/masahirochaen/status/2097990179227414850?s=12": {
                "route": "x", "missing": ["video_content"],
                "raindrop_id": 1850403031,
            },
        })
        self._write_review("2026-09-14", self._vcard(
            "https://x.com/gencoin8/status/2099302097909158238?s=12",
            "別の当日カード", "今回は動画の内容を取得できなかった。",
            "1853167142",
        ))
        out = self._run(["--date", "2026-09-14"])
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-14 kind=x_video "
            "rid=1850403031 url=https://x.com/masahirochaen/"
            "status/2097990179227414850?s=12 reason=no_card", out)
        self.assertNotIn("DISCLOSURE_EXCLUDED:", out)

    # --- D: HTMLコメントを構造カウントから無視（2周目指示3） ----------------

    def test_html_comment_inside_vdesc_does_not_break_card_extraction(self):
        """.vdesc の中に <!-- <div> --> のようなHTMLコメントがあっても、
        div開閉カウントを乱さず1枚のカードとして正しく抽出でき、開示判定も
        正しく行える（未取得語が無いので違反のまま）。
        """
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        desc = (
            "進捗を見せながら生成する<!-- 構造メモ: <div>を後で挿入予定 -->。"
            "開示文言はまだ無い。"
        )
        self._write_review("2026-09-04", self._vcard(
            "https://www.instagram.com/reel/Dcy4SIYiD7U/",
            "写真1枚をRhinoモデルに変換するAIエージェント「STF Agent」",
            desc, "1842608050",
        ))
        out = self._run(["--date", "2026-09-04"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-04 duty=1 disclosed=0 "
            "violation=1 excluded=0 out_of_scope=0", out)
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-04 kind=reel "
            "rid=1842608050", out)
        self.assertNotIn("card_parse_failed", out)

    def test_html_comment_with_disclosure_phrase_after_it_is_disclosed(self):
        """コメントの後に本物の開示文言が続く場合はちゃんと開示ありになる
        （コメントに惑わされてカード境界がずれていないことの確認）。
        """
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        desc = (
            "<!-- TODO: <div class=\"note\">後で書く</div> -->"
            "動画の内容は未取得。"
        )
        self._write_review("2026-09-04", self._vcard(
            "https://www.instagram.com/reel/Dcy4SIYiD7U/",
            "写真1枚をRhinoモデルに変換するAIエージェント「STF Agent」",
            desc, "1842608050",
        ))
        out = self._run(["--date", "2026-09-04"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-04 duty=1 disclosed=1 "
            "violation=0 excluded=0 out_of_scope=0", out)

    # --- E: CRLF保持（2周目指示4） -------------------------------------------

    def test_fix_preserves_crlf_line_endings_outside_the_insertion(self):
        """reviews が CRLF の場合、--fix で挿入した文字列以外のバイトが
        LFに化けたりしないことを確認する（読み込みも newline="" にした）。
        """
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        card = self._vcard(
            "https://www.instagram.com/reel/Dcy4SIYiD7U/",
            "写真1枚をRhinoモデルに変換するAIエージェント「STF Agent」",
            "キッチン什器の写真を投げると進捗を見せながらRhino上に3Dモデル"
            "を自動生成する。",
            "1842608050",
        )
        content = (REVIEW_HEAD + card + REVIEW_TAIL).replace("\n", "\r\n")
        review_path = os.path.join(self.reviews_dir, "2026-09-04.html")
        with open(review_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        before = self._read_review("2026-09-04")
        self.assertIn("\r\n", before)

        out = self._run(["--date", "2026-09-04", "--fix"])
        self.assertIn("DISCLOSURE_FIXED: date=2026-09-04 rid=1842608050", out)

        after = self._read_review("2026-09-04")
        self.assertNotEqual(before, after)
        self.assertIn("動画の内容は未取得。", after)

        prefix_len = 0
        while (prefix_len < len(before) and prefix_len < len(after)
               and before[prefix_len] == after[prefix_len]):
            prefix_len += 1
        suffix_len = 0
        while (suffix_len < len(before) - prefix_len
               and suffix_len < len(after) - prefix_len
               and before[-1 - suffix_len] == after[-1 - suffix_len]):
            suffix_len += 1
        inserted = after[prefix_len:len(after) - suffix_len]
        self.assertIn("動画の内容は未取得。", inserted)
        # 挿入文字列自体には改行が無いはずなので、CRLFがLFに化けていれば
        # ここで prefix/suffix の外に取りこぼされたCRLFが現れて長さが
        # ずれる。挿入部を除いた残り全体が前後で一致することを確認する。
        self.assertEqual(
            before[:prefix_len] + before[len(before) - suffix_len:],
            after[:prefix_len] + after[len(after) - suffix_len:],
        )
        # 全体の \r\n の個数（挿入文言を除く）が変わっていないこと。
        self.assertEqual(
            before.count("\r\n"),
            after.replace("動画の内容は未取得。", "").count("\r\n"),
        )

    # --- F: --since と card_parse_failed/error の::warning抑止（2周目指示5）-

    def test_since_suppresses_warning_for_card_parse_failed_but_keeps_stdout_line(self):
        """--since より前の日付の card_parse_failed は ::warning を出さない
        が、DISCLOSURE_ERROR 自体は標準出力に残す。
        """
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        self._write_review("2026-09-04", "")  # cardsセクションが空
        summary = os.path.join(self.tmp, "summary.md")
        env = dict(os.environ)
        env["GITHUB_STEP_SUMMARY"] = summary
        cmd = [sys.executable, str(SCRIPT), "--facts-dir", self.facts_dir,
               "--reviews-dir", self.reviews_dir,
               "--capture-index", self.capture_index_path,
               "--date", "2026-09-04", "--since", "2026-09-15", "--github"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DISCLOSURE_ERROR: date=2026-09-04 card_parse_failed",
                       r.stdout)
        self.assertNotIn("::warning", r.stdout)

    def test_since_does_not_suppress_warning_on_or_after_since_date(self):
        """--since 以降の日付なら card_parse_failed でも ::warning を出す。"""
        self._write_facts("2026-09-16", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        self._write_review("2026-09-16", "")
        summary = os.path.join(self.tmp, "summary.md")
        env = dict(os.environ)
        env["GITHUB_STEP_SUMMARY"] = summary
        cmd = [sys.executable, str(SCRIPT), "--facts-dir", self.facts_dir,
               "--reviews-dir", self.reviews_dir,
               "--capture-index", self.capture_index_path,
               "--date", "2026-09-16", "--since", "2026-09-15", "--github"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DISCLOSURE_ERROR: date=2026-09-16 card_parse_failed",
                       r.stdout)
        self.assertIn("::warning title=disclosure::", r.stdout)


class DisclosureTextMatchingTest(unittest.TestCase):
    """開示判定コア（cd.text_disclosed）のデータ駆動テスト（2周目指示1）。
    HTMLを組み立てず、.vdesc相当のプレーンテキストに対して直接判定する。
    """

    # (a) 誤合格してはいけない文。x_video/reel/threads(視覚のみ)/
    # threads(音声のみ)/threads(両方)のどの kind/missing の組でも
    # Falseでなければならない（媒体を問わず誤合格しないことを見る文）。
    FALSE_POSITIVE_TEXTS = [
        # verifier 2周目 反例7文
        "この動画は展開が早くまだ誰も未確認のまま拡散している",
        "画像は鮮明で未確認ながら話題性は高いようだ",
        "写真集（未取得の新刊）が発表された",
        "Reelとして人気だが真偽は未確認のまま拡散",
        "写真（この記事とは別件だが）は未確認のままだ",
        "動画（前作の話）は今のところ未確認",
        "画像・文章とも簡潔だが真偽は未確認",
        # 追加の同型反例（このタスクで作成）
        "映像は事前に確認済みなのに、音声だけが未取得のままとされている",
        "写真は良い出来だが、動画のほうは今のところ未確認とのこと",
        "リールは伸びているらしいが実態は未確認。画像だけ後日追加予定",
        "音声トラックは収録済みで、動画は未取得情報として社内共有された",
    ]

    KIND_MISSING_COMBOS = [
        ("x_video", ["video_content"]),
        ("reel", ["video_content"]),
        ("threads", ["visual_content"]),
        ("threads", ["audio_content"]),
        ("threads", ["visual_content", "audio_content"]),
    ]

    def test_a_false_positive_sentences_are_false_for_every_kind(self):
        for text in self.FALSE_POSITIVE_TEXTS:
            for kind, missing in self.KIND_MISSING_COMBOS:
                self.assertFalse(
                    cd.text_disclosed(text, kind, missing),
                    "開示なしと判定されるべき（kind=%s missing=%s）: %s"
                    % (kind, missing, text))

    def test_a_codex_repro_video_swapped_for_image_is_false_for_video_kinds(self):
        """Codex 1周目P0（CEOが再現）: `動画は確認済みだが、画像は未取得。`
        が x_video/reel で開示ありと誤判定される問題。video_content が要る
        kind と、threads で画像語を持たない audio_content 単独では False。
        （なお threads の visual_content 単独では「画像は未取得」の部分が
        文字通り画像の開示に該当するため True が正しい。これは誤合格では
        なく、文中に無関係な話題が混じっていても該当媒体の開示自体は
        別途成立しているケース。x_videoの video_content がこの文の
        「画像は未取得」に釣られて誤って開示扱いになっていたのが元のバグ）。
        """
        text = "動画は確認済みだが、画像は未取得。"
        self.assertFalse(cd.text_disclosed(text, "x_video", ["video_content"]))
        self.assertFalse(cd.text_disclosed(text, "reel", ["video_content"]))
        self.assertFalse(cd.text_disclosed(text, "threads", ["audio_content"]))
        # 参考: threads の visual_content 単独では「画像は未取得」により
        # 正当にTrueになる（誤合格ではない。上のdocstring参照）。
        self.assertTrue(cd.text_disclosed(text, "threads", ["visual_content"]))

    def test_b_real_disclosed_vdesc_texts_are_true(self):
        """実データ: fetch_facts/*.json と reviews/*.html を突き合わせて
        「全候補カードが開示済み」と判定された実際のduty レコードから、
        .vdesc の中身をそのまま抽出した実文（kind/missing 付き）。
        手で書いた文例は含めない。30件（reel19件相当＋x_video11件相当、
        重複rid分含む）。
        """
        # (date, kind, missing, rid, text) — text は
        # check_disclosure.vdesc_text() で実カードから抽出した逐語。
        real_cases = [
            ("2026-08-23", "reel", ["video_content"], None,
             "白背景の即答カードで支払い状況・与信利用率を示しつつ、"
             "グラデーションのクレジットカード表示でローンごとの識別性を"
             "確保。動画の内容は未取得（Instagram Reelの動画理解は構造的"
             "に撤去済みのため、キャプションと画像から判断）"),
            ("2026-08-28", "reel", ["video_content"], None,
             "撥水加工・丈夫さを生かし、複数の巾着袋をナップサック代わりに"
             "使うアイデアを紹介(動画内容は未取得、キャプションのみ)"),
            ("2026-08-28", "reel", ["video_content"], None,
             "Tord Björklund設計、2002年発売。各トレイにCD51枚収納可能・"
             "壁掛けにも対応するヴィンテージIKEA品を4台集めたと紹介"
             "(動画内容は未取得、キャプションのみ)"),
            ("2026-08-30", "reel", ["video_content"], None,
             "「バズったやつ」とのキャプションで、横断歩道を渡るPOV映像が"
             "話題に。動画の中身は未取得（Instagram Reelは動画取得を構造的"
             "に断念済みのため）で、キャプションと画像から推測。"),
            ("2026-08-30", "reel", ["video_content"], None,
             "FRAGG・VINK・GRUBBE×2・JUTISと廃盤プロダクトを並べた投稿。"
             "動画の中身は未取得（同上の構造的制約）で、キャプションと画像"
             "から推測。"),
            ("2026-09-05", "reel", ["video_content"], "1844474651",
             "水彩調テクスチャを継ぎ目なくタイル化する塗装法をKrita/"
             "Blender/UnrealEngineで実演（詳しい手順動画は後日公開予定と"
             "のこと）。動画の内容は未取得。"),
            ("2026-09-06", "reel", ["video_content"], "1844650582",
             "「Claude Codeを毎月16億トークン無料で回すOmniRoute設定書」を"
             "プロフィールから配布中と告知しつつ、AI×SNS運用のオンライン"
             "勉強会（無料・約2時間）へも誘導する投稿。動画の内容は未取得。"),
            ("2026-09-09", "x_video", ["video_content"], "1848175166",
             "カメラワーク・照明・構図・ジャンル/スタイル別に、映画の撮影・"
             "演出技法150個をAI動画生成用プロンプトとして整理。サンプル"
             "映像を選ぶと技法解説とプロンプトがそのまま出てきてコピー"
             "できる（今回は埋め込み動画自体の内容は取得できなかった）"),
            ("2026-09-11", "reel", ["video_content"], "1850230050",
             "RevitモデルをThree.js環境に変換しASTRAが空間構成を読んで"
             "カメラパスを自動提案、同じジオメトリのカラーマップ動画を"
             "Higgsfieldのマテリアル指定にも使う二段構え。動画の内容は"
             "未取得"),
            ("2026-09-11", "x_video", ["video_content"], "1850403031",
             "WebMCPでページのツールを自動検出し、YouTube再生と同期して"
             "一時停止→解説→再生継続。対応はGPT-5.6 SolとTerraのみでLuna"
             "は非対応、セットアップは10秒と紹介。今回は動画の中身までは"
             "追えていない"),
            ("2026-09-11", "x_video", ["video_content"], "1850401311",
             "Anthropicの最高責任者本人が、自分のClaude Code環境を丸ごと"
             "公開したと投稿（実態は要検証、上の深掘り参照）。動画部分は"
             "今回未確認"),
            ("2026-09-11", "x_video", ["video_content"], "1850309485",
             "投資歴15年の投稿者が「AI社員を使った株式投資はマジで最強」と"
             "断言するのみで、具体的な運用実績や再現性の記載は本文には"
             "無い。動画は今回見られていない"),
            ("2026-09-12", "reel", ["video_content"], "1850945581",
             "レイヤー216・オブジェクト30,595・完全重複線13,523という"
             "DWGを丸ごと読み込み、MIDASの構造モデルから部材1,119点を"
             "REVITへ自動モデリング。まだパイロット段階と明記。動画の内容"
             "は未取得"),
            ("2026-09-12", "x_video", ["video_content"], "1851095582",
             "開発2ヶ月・元手50万ウォンから、初週で200万ウォンの収益を"
             "達成したという投稿。動画付きだが今回は動画の内容を取得でき"
             "なかった"),
            ("2026-09-13", "reel", ["video_content"], "1852143830",
             "CADは最初の学習曲線が急だが、練習を重ねて機能に慣れ、実務"
             "でも毎日使ううちに身についたという経験を紹介し、コメント欄"
             "で学習リンクを配布。動画の内容は未取得。"),
            ("2026-09-13", "reel", ["video_content"], "1852142500",
             "Appleが導入するとみられる新ステータスアイコンのアニメーショ"
             "ンを1時間半かけて模写し、ゼロから発想するより模写の方が易し"
             "いと述懐。動画の内容は未取得。"),
            ("2026-09-13", "reel", ["video_content"], "1852141997",
             "段ボールなどを使った建築模型制作の裏ワザを紹介する投稿（本文"
             "はハッシュタグのみ）。動画の内容は未取得。"),
            ("2026-09-13", "reel", ["video_content"], "1852086012",
             "同じく建築模型づくりの裏ワザ動画で、何のスプレーを吹きかけて"
             "いるのか気になるというユーザーコメントが付いた一本。動画の"
             "内容は未取得。"),
            ("2026-09-13", "reel", ["video_content"], "1852083950",
             "プロジェクトの知性は単一ツールに閉じるべきではないとし、複数"
             "フォーマットをまたいでAIと協働できるエージェント型デスクトッ"
             "プワークスペースをローンチ。動画の内容は未取得。"),
            ("2026-09-13", "x_video", ["image_content", "video_content"],
             "1852040378",
             "現在価格47円のジャパンディスプレイがフジクラ・村田製作所・"
             "太陽誘電・キオクシア以上に来るかもしれないとし、空気が変わっ"
             "た瞬間に一気に飛ぶと予告。今回は動画の内容を取得できなかっ"
             "た。"),
            ("2026-09-13", "x_video", ["video_content"], "1852030187",
             "現在26円のCRAVIAが125円に到達すれば投資額が10倍超になり得る"
             "としつつ、結果は誰にも分からないと留保を添えて煽る一本。今回"
             "は動画の内容を取得できなかった。"),
            ("2026-09-13", "x_video", ["video_content"], "1852030091",
             "JT・三菱重工・トヨタ自動車・ソフトバンク・NTTなど、300株保有"
             "を前提にした高配当・国策銘柄7選を順位付きで紹介。今回は動画"
             "の内容を取得できなかった。"),
            ("2026-09-13", "x_video", ["video_content"], "1852024519",
             "声を学習させて動画の音声を646言語に差し替えたり、書き起こし"
             "・朗読までこなすOSSツール「VoiceStudio」がGitHubで2万star近"
             "くに到達、搭載エンジンは本家ElevenLabsの32種を上回る14種類だ"
             "と紹介。今回は動画の内容を取得できなかった。"),
            ("2026-09-14", "reel", ["video_content"], "1852251457",
             "厚みは壁の50〜60%、抜き勾配は射出成形0.5〜3°・3Dプリントは"
             "不要、高さは厚みの約3倍、根元フィレット0.5〜1mm、意匠面裏へ"
             "のリブ配置はヒケ発生リスクがあるため回避、3Dプリントでは積層"
             "方向も要考慮というTipsをまとめた投稿（タミル語）。動画の内容"
             "は未取得。"),
            ("2026-09-14", "x_video", ["video_content"], "1853167142",
             "Codex 6 Astraをメインモデルにしているなら、OpenAI公式案内ど"
             "おりAGENTS.mdやSkillsも合わせて更新しないと本来の性能を半減"
             "させかねないと注意喚起。今回は動画の内容を取得できなかっ"
             "た。"),
            ("2026-09-14", "x_video", ["video_content"], "1853166438",
             "AIボイスレコーダーが2〜3万円で手が出しにくいことから、"
             "iPhoneだけで完結する「Recory」を自作。録音・リアルタイム文字"
             "起こし・議事録・タスク化に加え、MCP経由でGPTやClaudeが議事録"
             "の文脈を呼び出せる設計。今回は動画の内容を取得できなかっ"
             "た。"),
            ("2026-09-14", "x_video", ["video_content"], "1853166384",
             "ChatGPTとノーコードを組み合わせ、Zoom終了と同時に文字起こし"
             "→要約→ドキュメント作成→Slack通知まで自動で走るワークフロー"
             "を構築、非エンジニアでも30分・無料で作れると述べる。今回は"
             "動画の内容を取得できなかった。"),
            ("2026-09-14", "x_video", ["video_content"], "1853166010",
             "動画をAIとリアルタイムに同時視聴する「Watch with Codex」が"
             "登場、ChatGPT/Codex内蔵ブラウザとWebMCPで再生位置と同期し、"
             "任意の瞬間で一時停止して解説させられる。今回は動画の内容を"
             "取得できなかった。"),
            ("2026-09-14", "x_video", ["video_content"], "1853165924",
             "ローカルCPUだけで動く多言語リアルタイム文字起こしエンジン"
             "「早耳 hayamimi」をOSS公開。実放送の日本語でCER 5.8%、発話"
             "終了から約0.1秒で字幕確定、日英中韓含む約1600言語対応、GPU"
             "不要・RAM 2GBで動作という仕様をMITライセンスで公開。今回は"
             "動画の内容を取得できなかった。"),
            ("2026-09-14", "x_video", ["video_content"], "1852251612",
             "19歳の日本人学生がClaude Codeで2日で組んだトレードbotが元手"
             "$68（約1万円）から初日の夜だけで+$6,732（約100万円）稼いだと"
             "紹介する投稿。今回は動画の内容を取得できなかった。"),
        ]
        self.assertEqual(len(real_cases), 30, "実データ抽出件数が変わった")
        for date, kind, missing, rid, text in real_cases:
            self.assertTrue(
                cd.text_disclosed(text, kind, missing),
                "開示ありと判定されるべき（%s kind=%s rid=%s）: %s"
                % (date, kind, rid, text))

    def test_c_kind_mismatch_x_video_image_only_is_false(self):
        """x_video (video_content欠損) に「画像は未取得」だけでは開示にならない。"""
        self.assertFalse(
            cd.text_disclosed("画像は未取得。", "x_video", ["video_content"]))
        self.assertFalse(
            cd.text_disclosed("画像は未取得。", "reel", ["video_content"]))

    def test_c_kind_mismatch_threads_audio_only_image_word_is_false(self):
        """threads で audio_content のみ欠損のとき「画像は未取得」だけでは
        音声の開示にならない（画像語は audio_content の語彙に無い）。
        """
        self.assertFalse(
            cd.text_disclosed("画像は未取得。", "threads", ["audio_content"]))

    def test_c_threads_both_missing_requires_both_words(self):
        """threads で画像・音声両方欠損のとき、画像だけの開示では不十分。
        両方揃って初めて開示扱いになる。
        """
        missing = ["visual_content", "audio_content"]
        self.assertFalse(cd.text_disclosed("画像は未取得。", "threads", missing))
        self.assertFalse(cd.text_disclosed("音声は未取得。", "threads", missing))
        self.assertTrue(
            cd.text_disclosed("画像・音声は未取得。", "threads", missing))
        # --fixが実際に挿入する定型文自体も自己整合していることを確認。
        self.assertTrue(
            cd.text_disclosed(cd.FIX_PHRASES["threads"], "threads", missing))


if __name__ == "__main__":
    unittest.main()
