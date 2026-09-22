"""check_disclosure.py の受け入れテスト。

2026-09-15 3周目差し戻し（固定マーカー方式への作り替え）に伴い、開示判定は
「自由文を読んで判定」から「固定マーカー文（cd.MARKERS）の有無判定」に
変わった。このテストファイルも自由文判定用のデータ（FALSE_POSITIVE_TEXTS・
real_cases 30件Trueなど）をマーカー方式のデータに置き換えている。

fixture は実データ（fetch_facts/*.json・reviews/*.html の実レコード・実カード）
から書き写している箇所はその旨を各テストのdocstringに明記する。マーカー方式の
境界条件テストはこのタスクで新規作成したデータ駆動テストである。
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

V_MARKER = cd.MARKERS["video_content"]
I_MARKER = cd.MARKERS["visual_content"]
A_MARKER = cd.MARKERS["audio_content"]


class DisclosureCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.facts_dir = os.path.join(self.tmp, "fetch_facts")
        self.reviews_dir = os.path.join(self.tmp, "reviews")
        os.mkdir(self.facts_dir)
        os.mkdir(self.reviews_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- 素材 -------------------------------------------------------------

    def _write_facts(self, date, records):
        path = os.path.join(self.facts_dir, "%s.json" % date)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=1)

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
               "--reviews-dir", self.reviews_dir] + args
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
        見たかのような記述でマーカーが無い（実際の事故）。
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

    # --- 実データ: 2026-09-11 X の3件。マーカーが無いので全件違反 -----------

    def test_20260911_x_varied_phrasing_without_marker_is_a_violation(self):
        """実データ: fetch_facts/2026-09-11.json の3件。route=x・
        missing=["video_content"]。方式変更前は言い回しの揺れ
        （追えていない／未確認／見られていない）を自由文判定で開示扱いに
        していたが、マーカー方式ではこれらにマーカーが無いため全件違反に
        なる（＝方式変更の意図の固定）。
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
            "DISCLOSURE_CHECK: date=2026-09-11 duty=3 disclosed=0 "
            "violation=3 excluded=0 out_of_scope=0", out)

    # --- 実データ: 2026-09-14 facts のバックフィルrid が除外される -----------

    def test_backfill_rid_from_other_date_is_excluded_not_violation(self):
        """実データ: fetch_facts/2026-09-14.json には09-11のrid(1850403031)が
        再取得で追記されている。そのカードは reviews/2026-09-11.html にあり
        reviews/2026-09-14.html には無い。2026-09-22 CEO裁定後は
        capture_index.json を見ない：対象日(09-14)以外の既存reviews
        （09-11）に実際にそのカード(data-rid一致)が掲載されているので、
        バックフィルとして除外され、違反にしてはいけない
        （＝別reviewsに掲載済みの再取得分は除外される、の確認）。
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
            "別の当日カード", V_MARKER + "。",
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

    # --- reviews実掲載ベースの違反/除外境界（2026-09-22 CEO裁定） ------------

    def test_no_card_anywhere_is_a_violation_not_backfill(self):
        """対象日のreviewsにカードが無く、他のどのreviewsにも
        （data-rid・URLどちらでも）そのカードが実在しない場合は、過去日に
        保存されたものであっても違反(no_card)にする（バックフィル扱いしない）。
        ＝「過去日保存で初掲載のカードが開示漏れなら違反になる」の確認。
        """
        self._write_facts("2026-09-14", {
            "https://x.com/example/status/999": {
                "route": "x", "missing": ["video_content"], "raindrop_id": 999,
            },
        })
        # 別日の reviews はあるが、まったく無関係なカードしか載っていない
        # （このrid/URLはどこにも掲載されていない）。
        self._write_review("2026-09-10", self._vcard(
            "https://x.com/unrelated/status/1", "無関係な過去カード",
            V_MARKER + "。", "1",
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
        URL照合も含めて掲載が確認できないので違反(no_card)にする。
        """
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

    def test_backfill_excluded_via_url_key_match_without_matching_data_rid(self):
        """data-rid が付いていない（2026-08-04の retrofit漏れのような）別日の
        カードでも、URL(select_targets.url_key)が一致すれば掲載済みとして
        バックフィル除外される。
        """
        self._write_facts("2026-09-14", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        # 09-11 の review には同じURLのカードがあるが data-rid が空（後付け漏れ）。
        self._write_review("2026-09-11", self._vcard(
            "https://www.instagram.com/reel/Dcy4SIYiD7U/",
            "写真1枚をRhinoモデルに変換するAIエージェント「STF Agent」",
            V_MARKER + "。", "",
        ))
        self._write_review("2026-09-14", self._vcard(
            "https://x.com/gencoin8/status/2099302097909158238?s=12",
            "別の当日カード", V_MARKER + "。",
            "1853167142",
        ))
        out = self._run(["--date", "2026-09-14"])
        self.assertIn(
            "DISCLOSURE_EXCLUDED: date=2026-09-14 rid=1842608050 "
            "reason=backfill(2026-09-11,card=yes)", out)
        self.assertNotIn("DISCLOSURE_VIOLATION: date=2026-09-14", out)

    def test_url_shared_with_a_different_data_rid_card_is_not_excluded(self):
        """別の data-rid を持つカードが、たまたま同じ URL を href に持つ
        だけでは掲載済みにならない（2026-09-22 Codexレビュー P1-a 対応）。
        data-rid を持つカードの href は URL照合に混ぜてはいけない。
        """
        self._write_facts("2026-09-14", {
            "https://x.com/example/status/999": {
                "route": "x", "missing": ["video_content"], "raindrop_id": 999,
            },
        })
        # 09-11 の review には別rid(111)を持つカードがあり、href だけが
        # たまたま対象URLと同じ（実運用では起きないはずの取り違えだが、
        # 誤ってURL照合が効かないことを確認するため意図的に作る）。
        self._write_review("2026-09-11", self._vcard(
            "https://x.com/example/status/999",
            "別の投稿（rid違い）", V_MARKER + "。", "111",
        ))
        self._write_review("2026-09-14", self._vcard(
            "https://x.com/other/status/1", "無関係カード", "本文のみ。", "1",
        ))
        out = self._run(["--date", "2026-09-14"])
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-14 kind=x_video rid=999 "
            "url=https://x.com/example/status/999 reason=no_card", out)
        self.assertNotIn("DISCLOSURE_EXCLUDED:", out)

    # --- 実データ: 2026-09-08 Threads 開示ゼロ ------------------------------

    def test_20260908_threads_no_disclosure_is_violation(self):
        """実データ: fetch_facts/2026-09-08.json rid=1847745101
        （https://www.threads.com/share/BAY7Zm6kIj/、route=threads、
        missing=["visual_content","audio_content"]）。カードにマーカー無し。
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
        """実データ: fetch_facts/2026-09-12.json rid=1851132721。文中に
        マーカーが無い（テキスト取得限界の話のみ）。threadsの画像・動画・
        音声欠損への開示にはならないので違反。
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

    # --- X (image+video欠損、動画マーカーのみ) は開示あり ---------------------

    def test_20260913_x_video_only_disclosure_counts_when_image_also_missing(self):
        """missing=["image_content","video_content"]。x_videoの判定対象は
        video_contentのみなので、動画マーカーがあれば足りる。
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
            + V_MARKER + "。",
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
        self.assertIn(V_MARKER + "。", after)

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
        inserted = after[prefix_len:len(after) - suffix_len]
        self.assertIn(V_MARKER + "。", inserted)

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

    def test_fix_does_not_double_period_when_vdesc_ends_with_inline_tag(self):
        """.vdesc の中身が <span>本文。</span> のようにインラインタグで
        終わっている場合、タグを除去した後のテキスト末尾（句点）で句点判定
        するべきで、生HTML末尾（閉じタグ）を見て「句点なし」と誤判定し
        「。。」を作ってはいけない。2回目の --fix はバイト一致（冪等）。
        """
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        desc = "<span>進捗を見せながら生成する。</span>"
        self._write_review("2026-09-04", self._vcard(
            "https://www.instagram.com/reel/Dcy4SIYiD7U/",
            "写真1枚をRhinoモデルに変換するAIエージェント「STF Agent」",
            desc, "1842608050",
        ))

        out = self._run(["--date", "2026-09-04", "--fix"])
        self.assertIn("DISCLOSURE_FIXED: date=2026-09-04 rid=1842608050", out)
        after = self._read_review("2026-09-04")
        self.assertNotIn("。。", after)
        self.assertIn(V_MARKER + "。", after)

        out2 = self._run(["--date", "2026-09-04"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-04 duty=1 disclosed=1 "
            "violation=0 excluded=0 out_of_scope=0", out2)

        # 2回目の --fix はファイルをバイト単位で変化させない（冪等）。
        out3 = self._run(["--date", "2026-09-04", "--fix"])
        self.assertNotIn("DISCLOSURE_FIXED:", out3)
        after2 = self._read_review("2026-09-04")
        self.assertEqual(after, after2)

    def test_fix_threads_only_adds_the_missing_marker(self):
        """threads で片方（visual_content）だけ既にマーカー済みの場合、
        --fix は足りない方（audio_content）のマーカーだけを追記する。
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
            "広告画像・SNS画像・ショート動画素材の接続例。" + I_MARKER + "。",
            "1847745101",
        ))
        out = self._run(["--date", "2026-09-08", "--fix"])
        self.assertIn("DISCLOSURE_FIXED: date=2026-09-08 rid=1847745101", out)
        after = self._read_review("2026-09-08")
        self.assertEqual(after.count(I_MARKER), 1)
        self.assertIn(A_MARKER + "。", after)

        out2 = self._run(["--date", "2026-09-08"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-08 duty=1 disclosed=1 "
            "violation=0 excluded=0 out_of_scope=0", out2)

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
                "同じ投稿が誤って2枚生成されたケース。" + V_MARKER + "。",
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
        # 両方のカードにマーカーが入り、元々開示済みだった2枚目も変化しない
        # （冪等）はずなので、未開示だった1枚目だけに文言が追加される。
        self.assertEqual(after.count(V_MARKER + "。"), 2)

        out3 = self._run(["--date", "2026-09-04"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-04 duty=1 disclosed=1 "
            "violation=0 excluded=0 out_of_scope=0", out3)

    def _vcard_without_vdesc(self, url, title, rid):
        """.vdesc 自体が無いカード（_vdesc_insert_point が None になる）。"""
        return (
            '    <div class="vcard">\n'
            '      <a class="vlink" href="%s" target="_blank" rel="noopener">\n'
            '        <div class="thumb"><span class="ph">X</span></div>\n'
            '        <div class="vbody"><div class="vtitle">%s</div></div>\n'
            "      </a>\n"
            '      <button class="deepdive" onclick="openChat(this)" '
            'data-url="%s" data-title="%s" data-rid="%s">💬 AIと話す</button>\n'
            "    </div>\n"
        ) % (url, title, url, title, rid)

    def test_fix_stays_violation_when_one_of_the_duplicate_cards_has_no_vdesc(self):
        """同じ rid の候補カードが2枚あり、片方は .vdesc 自体が無く挿入不能。
        --fix は挿入できるカードには文言を追記するが、undisclosed 全部には
        挿入できていないので disclosed/fixed に入れず違反として記録する
        （挿入できたカードだけ直って成功申告になってしまうのを防ぐ）。
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
            + self._vcard_without_vdesc(
                "https://www.instagram.com/reel/Dcy4SIYiD7U/",
                "同じ投稿の.vdesc欠損カード（検証用）",
                "1842608050")
        )
        self._write_review("2026-09-04", cards)

        out = self._run(["--date", "2026-09-04", "--fix"])
        self.assertNotIn("DISCLOSURE_FIXED:", out)
        self.assertIn(
            "DISCLOSURE_VIOLATION: date=2026-09-04 kind=reel "
            "rid=1842608050", out)
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-04 duty=1 disclosed=0 "
            "violation=1 excluded=0 out_of_scope=0", out)

        after = self._read_review("2026-09-04")
        # 挿入できた1枚目には積まれた edit が反映されている。
        self.assertIn(V_MARKER + "。", after)

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
               "--reviews-dir", self.reviews_dir, "--all",
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
            "<div class='vdesc'>進捗を見せながら生成する。" + V_MARKER + "。"
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

    # --- D: HTMLコメントを構造カウントから無視（2周目指示3） ----------------

    def test_html_comment_inside_vdesc_does_not_break_card_extraction(self):
        """.vdesc の中に <!-- <div> --> のようなHTMLコメントがあっても、
        div開閉カウントを乱さず1枚のカードとして正しく抽出でき、開示判定も
        正しく行える（マーカーが無いので違反のまま）。
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
        """コメントの後に本物のマーカーが続く場合はちゃんと開示ありになる
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
            + V_MARKER + "。"
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

    def test_marker_only_inside_html_comment_is_not_disclosed(self):
        """マーカーが HTML コメントの中にだけある場合は開示扱いにならない
        （vdesc_text はコメントの中身も含めてタグとして除去するため）。
        """
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        desc = "進捗を見せながら生成する<!-- " + V_MARKER + "。 -->。本文のみ。"
        self._write_review("2026-09-04", self._vcard(
            "https://www.instagram.com/reel/Dcy4SIYiD7U/",
            "写真1枚をRhinoモデルに変換するAIエージェント「STF Agent」",
            desc, "1842608050",
        ))
        out = self._run(["--date", "2026-09-04"])
        self.assertIn(
            "DISCLOSURE_CHECK: date=2026-09-04 duty=1 disclosed=0 "
            "violation=1 excluded=0 out_of_scope=0", out)

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
        self.assertIn(V_MARKER + "。", after)

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
        self.assertIn(V_MARKER + "。", inserted)
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
            after.replace(V_MARKER + "。", "").count("\r\n"),
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
               "--date", "2026-09-16", "--since", "2026-09-15", "--github"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DISCLOSURE_ERROR: date=2026-09-16 card_parse_failed",
                       r.stdout)
        self.assertIn("::warning title=disclosure::", r.stdout)

    # --- G: no_review の ::warning（このタスクで追加した仕様） --------------

    def test_no_review_warns_when_duty_positive_and_within_since(self):
        """duty>0 の日に reviews/<日付>.html が無く、--since 以降なら
        ::warning を出す（手順7を飛ばした夜を公開ゲートで検知するため）。
        """
        self._write_facts("2026-09-16", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        summary = os.path.join(self.tmp, "summary.md")
        env = dict(os.environ)
        env["GITHUB_STEP_SUMMARY"] = summary
        cmd = [sys.executable, str(SCRIPT), "--facts-dir", self.facts_dir,
               "--reviews-dir", self.reviews_dir,
               "--date", "2026-09-16", "--since", "2026-09-15", "--github"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DISCLOSURE_CHECK: date=2026-09-16 status=no_review",
                       r.stdout)
        self.assertIn("::warning title=disclosure::", r.stdout)
        self.assertIn("status=no_review", r.stdout.split("::warning", 1)[1])

    def test_no_review_does_not_warn_before_since(self):
        """--since より前の日付の no_review は ::warning を出さない。"""
        self._write_facts("2026-09-04", {
            "https://www.instagram.com/reel/Dcy4SIYiD7U/": {
                "route": "instagram", "missing": ["video_content"],
                "raindrop_id": 1842608050,
            },
        })
        summary = os.path.join(self.tmp, "summary.md")
        env = dict(os.environ)
        env["GITHUB_STEP_SUMMARY"] = summary
        cmd = [sys.executable, str(SCRIPT), "--facts-dir", self.facts_dir,
               "--reviews-dir", self.reviews_dir,
               "--date", "2026-09-04", "--since", "2026-09-15", "--github"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DISCLOSURE_CHECK: date=2026-09-04 status=no_review",
                       r.stdout)
        self.assertNotIn("::warning", r.stdout)

    def test_no_review_does_not_warn_when_duty_is_zero(self):
        """duty=0（対象となる欠損レコードが無い）日は、reviewsが無くても
        ::warning を出さない。
        """
        self._write_facts("2026-09-16", {
            "https://www.instagram.com/p/whatever/": {
                "route": "instagram",
                "missing": ["visual_content", "audio_content"],
                "raindrop_id": None,
            },
        })
        summary = os.path.join(self.tmp, "summary.md")
        env = dict(os.environ)
        env["GITHUB_STEP_SUMMARY"] = summary
        cmd = [sys.executable, str(SCRIPT), "--facts-dir", self.facts_dir,
               "--reviews-dir", self.reviews_dir,
               "--date", "2026-09-16", "--since", "2026-09-15", "--github"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DISCLOSURE_CHECK: date=2026-09-16 status=no_review",
                       r.stdout)
        self.assertNotIn("::warning", r.stdout)


class DisclosureMarkerMatchingTest(unittest.TestCase):
    """開示判定コア（cd.text_disclosed）のマーカー方式データ駆動テスト。

    HTMLを組み立てず、.vdesc相当のプレーンテキストに対して直接判定する。
    マーカー文字列は cd.MARKERS を正本として参照し、このテストファイルに
    複製しない。
    """

    KIND_MISSING_COMBOS = [
        ("x_video", ["video_content"]),
        ("reel", ["video_content"]),
        ("threads", ["visual_content"]),
        ("threads", ["audio_content"]),
        ("threads", ["visual_content", "audio_content"]),
    ]

    # --- True: マーカーが境界条件を満たして現れる -----------------------

    def test_true_marker_at_end_of_text(self):
        self.assertTrue(cd.text_disclosed(V_MARKER, "x_video", ["video_content"]))
        self.assertTrue(cd.text_disclosed(V_MARKER, "reel", ["video_content"]))

    def test_true_marker_followed_by_period(self):
        self.assertTrue(
            cd.text_disclosed(V_MARKER + "。", "x_video", ["video_content"]))

    def test_true_marker_followed_by_space(self):
        self.assertTrue(
            cd.text_disclosed(V_MARKER + " 続き", "reel", ["video_content"]))

    def test_true_marker_followed_by_newline(self):
        self.assertTrue(
            cd.text_disclosed(V_MARKER + "\n続き", "reel", ["video_content"]))

    def test_true_marker_after_free_text(self):
        text = "キッチン什器の写真を投げて生成する。" + V_MARKER + "。"
        self.assertTrue(cd.text_disclosed(text, "reel", ["video_content"]))

    def test_true_marker_before_free_text_with_boundary(self):
        # マーカー直後が「。」なので、その後にどんな文が続いても構わない。
        text = V_MARKER + "。詳細は後日追記予定。"
        self.assertTrue(cd.text_disclosed(text, "x_video", ["video_content"]))

    def test_true_threads_both_markers_present(self):
        text = I_MARKER + "。" + A_MARKER + "。"
        self.assertTrue(
            cd.text_disclosed(text, "threads",
                               ["visual_content", "audio_content"]))

    def test_true_fix_phrases_are_self_consistent(self):
        """--fixが実際に挿入する定型文自体も自己整合していることを確認。"""
        self.assertTrue(
            cd.text_disclosed(cd.FIX_PHRASES["x_video"], "x_video",
                               ["video_content"]))
        self.assertTrue(
            cd.text_disclosed(cd.FIX_PHRASES["reel"], "reel",
                               ["video_content"]))
        self.assertTrue(
            cd.text_disclosed(cd.FIX_PHRASES["threads"], "threads",
                               ["visual_content", "audio_content"]))

    # --- False: マーカーが無い自由文（旧real_cases30件・方式変更の意図の固定）-

    # 2026-09-15 3周目差し戻し前の自由文判定では True 扱いだった実文
    # （旧 test_check_disclosure.py の real_cases 30件から逐語コピー）。
    # マーカー文字列が無いため、方式変更後はどれも False にならなければ
    # ならない。
    OLD_REAL_CASE_TEXTS_WITHOUT_MARKERS = [
        "白背景の即答カードで支払い状況・与信利用率を示しつつ、"
        "グラデーションのクレジットカード表示でローンごとの識別性を"
        "確保。動画の内容は未取得（Instagram Reelの動画理解は構造的"
        "に撤去済みのため、キャプションと画像から判断）",
        "撥水加工・丈夫さを生かし、複数の巾着袋をナップサック代わりに"
        "使うアイデアを紹介(動画内容は未取得、キャプションのみ)",
        "Tord Björklund設計、2002年発売。各トレイにCD51枚収納可能・"
        "壁掛けにも対応するヴィンテージIKEA品を4台集めたと紹介"
        "(動画内容は未取得、キャプションのみ)",
        "「バズったやつ」とのキャプションで、横断歩道を渡るPOV映像が"
        "話題に。動画の中身は未取得（Instagram Reelは動画取得を構造的"
        "に断念済みのため）で、キャプションと画像から推測。",
        "FRAGG・VINK・GRUBBE×2・JUTISと廃盤プロダクトを並べた投稿。"
        "動画の中身は未取得（同上の構造的制約）で、キャプションと画像"
        "から推測。",
        "水彩調テクスチャを継ぎ目なくタイル化する塗装法をKrita/"
        "Blender/UnrealEngineで実演（詳しい手順動画は後日公開予定と"
        "のこと）。動画の内容は未取得。",
        "「Claude Codeを毎月16億トークン無料で回すOmniRoute設定書」を"
        "プロフィールから配布中と告知しつつ、AI×SNS運用のオンライン"
        "勉強会（無料・約2時間）へも誘導する投稿。動画の内容は未取得。",
        "カメラワーク・照明・構図・ジャンル/スタイル別に、映画の撮影・"
        "演出技法150個をAI動画生成用プロンプトとして整理。サンプル"
        "映像を選ぶと技法解説とプロンプトがそのまま出てきてコピー"
        "できる（今回は埋め込み動画自体の内容は取得できなかった）",
        "RevitモデルをThree.js環境に変換しASTRAが空間構成を読んで"
        "カメラパスを自動提案、同じジオメトリのカラーマップ動画を"
        "Higgsfieldのマテリアル指定にも使う二段構え。動画の内容は"
        "未取得",
        "WebMCPでページのツールを自動検出し、YouTube再生と同期して"
        "一時停止→解説→再生継続。対応はGPT-5.6 SolとTerraのみでLuna"
        "は非対応、セットアップは10秒と紹介。今回は動画の中身までは"
        "追えていない",
        "Anthropicの最高責任者本人が、自分のClaude Code環境を丸ごと"
        "公開したと投稿（実態は要検証、上の深掘り参照）。動画部分は"
        "今回未確認",
        "投資歴15年の投稿者が「AI社員を使った株式投資はマジで最強」と"
        "断言するのみで、具体的な運用実績や再現性の記載は本文には"
        "無い。動画は今回見られていない",
        "レイヤー216・オブジェクト30,595・完全重複線13,523という"
        "DWGを丸ごと読み込み、MIDASの構造モデルから部材1,119点を"
        "REVITへ自動モデリング。まだパイロット段階と明記。動画の内容"
        "は未取得",
        "開発2ヶ月・元手50万ウォンから、初週で200万ウォンの収益を"
        "達成したという投稿。動画付きだが今回は動画の内容を取得でき"
        "なかった",
        "CADは最初の学習曲線が急だが、練習を重ねて機能に慣れ、実務"
        "でも毎日使ううちに身についたという経験を紹介し、コメント欄"
        "で学習リンクを配布。動画の内容は未取得。",
        "Appleが導入するとみられる新ステータスアイコンのアニメーショ"
        "ンを1時間半かけて模写し、ゼロから発想するより模写の方が易し"
        "いと述懐。動画の内容は未取得。",
        "段ボールなどを使った建築模型制作の裏ワザを紹介する投稿（本文"
        "はハッシュタグのみ）。動画の内容は未取得。",
        "同じく建築模型づくりの裏ワザ動画で、何のスプレーを吹きかけて"
        "いるのか気になるというユーザーコメントが付いた一本。動画の"
        "内容は未取得。",
        "プロジェクトの知性は単一ツールに閉じるべきではないとし、複数"
        "フォーマットをまたいでAIと協働できるエージェント型デスクトッ"
        "プワークスペースをローンチ。動画の内容は未取得。",
        "現在価格47円のジャパンディスプレイが化けるかもしれないと予告"
        "した投稿。今回は動画の内容を取得できなかった。",
        "現在価格47円のジャパンディスプレイがフジクラ・村田製作所・"
        "太陽誘電・キオクシア以上に来るかもしれないとし、空気が変わっ"
        "た瞬間に一気に飛ぶと予告。今回は動画の内容を取得できなかっ"
        "た。",
        "現在26円のCRAVIAが125円に到達すれば投資額が10倍超になり得る"
        "としつつ、結果は誰にも分からないと留保を添えて煽る一本。今回"
        "は動画の内容を取得できなかった。",
        "JT・三菱重工・トヨタ自動車・ソフトバンク・NTTなど、300株保有"
        "を前提にした高配当・国策銘柄7選を順位付きで紹介。今回は動画"
        "の内容を取得できなかった。",
        "声を学習させて動画の音声を646言語に差し替えたり、書き起こし"
        "・朗読までこなすOSSツール「VoiceStudio」がGitHubで2万star近"
        "くに到達、搭載エンジンは本家ElevenLabsの32種を上回る14種類だ"
        "と紹介。今回は動画の内容を取得できなかった。",
        "厚みは壁の50〜60%、抜き勾配は射出成形0.5〜3°・3Dプリントは"
        "不要、高さは厚みの約3倍、根元フィレット0.5〜1mm、意匠面裏へ"
        "のリブ配置はヒケ発生リスクがあるため回避、3Dプリントでは積層"
        "方向も要考慮というTipsをまとめた投稿（タミル語）。動画の内容"
        "は未取得。",
        "Codex 6 Astraをメインモデルにしているなら、OpenAI公式案内ど"
        "おりAGENTS.mdやSkillsも合わせて更新しないと本来の性能を半減"
        "させかねないと注意喚起。今回は動画の内容を取得できなかっ"
        "た。",
        "AIボイスレコーダーが2〜3万円で手が出しにくいことから、"
        "iPhoneだけで完結する「Recory」を自作。録音・リアルタイム文字"
        "起こし・議事録・タスク化に加え、MCP経由でGPTやClaudeが議事録"
        "の文脈を呼び出せる設計。今回は動画の内容を取得できなかっ"
        "た。",
        "ChatGPTとノーコードを組み合わせ、Zoom終了と同時に文字起こし"
        "→要約→ドキュメント作成→Slack通知まで自動で走るワークフロー"
        "を構築、非エンジニアでも30分・無料で作れると述べる。今回は"
        "動画の内容を取得できなかった。",
        "ローカルCPUだけで動く多言語リアルタイム文字起こしエンジン"
        "「早耳 hayamimi」をOSS公開。実放送の日本語でCER 5.8%、発話"
        "終了から約0.1秒で字幕確定、日英中韓含む約1600言語対応、GPU"
        "不要・RAM 2GBで動作という仕様をMITライセンスで公開。今回は"
        "動画の内容を取得できなかった。",
        "19歳の日本人学生がClaude Codeで2日で組んだトレードbotが元手"
        "$68（約1万円）から初日の夜だけで+$6,732（約100万円）稼いだと"
        "紹介する投稿。今回は動画の内容を取得できなかった。",
    ]

    def test_false_old_real_case_texts_without_markers_are_all_false(self):
        self.assertEqual(len(self.OLD_REAL_CASE_TEXTS_WITHOUT_MARKERS), 30)
        for text in self.OLD_REAL_CASE_TEXTS_WITHOUT_MARKERS:
            for kind, missing in self.KIND_MISSING_COMBOS:
                self.assertFalse(
                    cd.text_disclosed(text, kind, missing),
                    "マーカーが無いので開示なしと判定されるべき"
                    "（kind=%s missing=%s）: %s" % (kind, missing, text))

    # 前周までの自由文判定の反例（全角コロン・否定・Threads代行・Reel内
    # 画像など）。マーカーが無いのでどれも False。
    PREVIOUS_ROUND_COUNTEREXAMPLE_TEXTS = [
        "動画：画像は未取得",
        "未取得ではない",
        "動画は音声も含めて未取得",  # Threadsでaudio_contentを動画語が代行
        "このReel内の画像は未取得",
    ]

    def test_false_previous_round_counterexample_texts(self):
        for text in self.PREVIOUS_ROUND_COUNTEREXAMPLE_TEXTS:
            for kind, missing in self.KIND_MISSING_COMBOS:
                self.assertFalse(
                    cd.text_disclosed(text, kind, missing),
                    "マーカーが無いので開示なしと判定されるべき"
                    "（kind=%s missing=%s）: %s" % (kind, missing, text))

    # --- False: 別媒体のマーカーのみ ------------------------------------

    def test_false_reel_with_only_image_marker(self):
        """reel（video_content欠損）に画像マーカーだけでは開示にならない。"""
        self.assertFalse(
            cd.text_disclosed(I_MARKER + "。", "reel", ["video_content"]))
        self.assertFalse(
            cd.text_disclosed(I_MARKER + "。", "x_video", ["video_content"]))

    def test_false_threads_audio_missing_with_only_video_marker(self):
        """threads で audio_content のみ欠損のとき、動画マーカーだけでは
        音声の開示にならない（動画マーカーは audio_content 用ではない）。
        """
        self.assertFalse(
            cd.text_disclosed(V_MARKER + "。", "threads", ["audio_content"]))

    def test_false_threads_both_missing_only_one_marker(self):
        """threads で画像・音声両方欠損のとき、画像マーカーだけでは不十分。"""
        missing = ["visual_content", "audio_content"]
        self.assertFalse(cd.text_disclosed(I_MARKER + "。", "threads", missing))
        self.assertFalse(cd.text_disclosed(A_MARKER + "。", "threads", missing))
        self.assertTrue(
            cd.text_disclosed(I_MARKER + "。" + A_MARKER + "。",
                               "threads", missing))

    # --- False: マーカー直後に文字が続く（否定・別の文の一部） --------------

    def test_false_marker_followed_by_negation(self):
        self.assertFalse(
            cd.text_disclosed(V_MARKER + "ではない", "reel", ["video_content"]))
        self.assertFalse(
            cd.text_disclosed(V_MARKER + "ではない。", "x_video",
                               ["video_content"]))

    def test_false_marker_followed_by_past_tense_continuation(self):
        self.assertFalse(
            cd.text_disclosed(V_MARKER + "だった", "reel", ["video_content"]))

    def test_false_marker_is_a_prefix_of_a_longer_word(self):
        """マーカー文字列がたまたま別の語の一部として現れているだけの場合
        （直後に境界文字が来ない）は開示にならない。
        """
        self.assertFalse(
            cd.text_disclosed(V_MARKER + "情報として社内共有", "x_video",
                               ["video_content"]))

    # --- False: マーカーがHTMLコメント内にだけある（card_disclosed経由）----

    def test_false_marker_only_inside_html_comment_via_card_disclosed(self):
        card_raw = (
            '<div class="vcard"><div class="vdesc">'
            "本文のみ。<!-- " + V_MARKER + "。 -->"
            "</div></div>"
        )
        self.assertFalse(
            cd.card_disclosed(card_raw, "reel", ["video_content"]))

    # --- missing_marker_keys の直接テスト（--fix が使う足りないキー抽出）---

    def test_missing_marker_keys_threads_partial(self):
        text = I_MARKER + "。"
        self.assertEqual(
            cd.missing_marker_keys(text, "threads",
                                    ["visual_content", "audio_content"]),
            ["audio_content"],
        )

    def test_missing_marker_keys_empty_when_all_present(self):
        text = I_MARKER + "。" + A_MARKER + "。"
        self.assertEqual(
            cd.missing_marker_keys(text, "threads",
                                    ["visual_content", "audio_content"]),
            [],
        )


if __name__ == "__main__":
    unittest.main()
