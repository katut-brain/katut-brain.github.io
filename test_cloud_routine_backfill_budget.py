"""cloud_routine_prompt.md 手順2.5（バックフィル）のコマンド行を検査する。

2026-09-23 ユーザー裁定で確定した値そのもの（limit=5・timeout=480・
max-total=1140・外側timeout=1200）を定数として固定し、一致することを assert する
（2026-09-23 Codex 1周目レビュー P2 指摘: 不等式だけの検査だと `--limit 1
--max-total 480` や外側 `540` のような、確定値とは違う別の妥当な組み合わせに
差し戻っても気づけない）。

あわせて守るべき一般則3点も検査する（将来この確定値自体を変えるときのための
安全網。確定値と一般則のどちらも壊れていないかを見る）:
  1. 外側シェル `timeout` は `--max-total` に60秒以上の余裕を足した値であること
     （親が外側 timeout より先に自分で終わらないと、子が孤児化して排他ロックの無い
     `fetch_facts/<日付>.json` に競合書込みする。cloud_routine_prompt.md 手順2.5 の既存規約）。
  2. `--max-total` は `--timeout`（1件あたりの上限）以上であること
     （そうでないと1件もフルに起動できない）。
  3. `--limit` は1以上であること（0や負では候補を1件も処理しない）。

解析範囲は**手順2.5 の節だけ**に限定する（見出し `2.5.` から次の見出し `3.` の
直前まで）。範囲を手順書全体にすると、他所に残る例示的な記述（歴史的な値の
言及等）を実行コマンドと誤認する／実行行そのものが消えていても他所の例示に
一致して見逃す、という2方向の事故がありうる。
"""

import re
import unittest
from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parent / "cloud_routine_prompt.md"

# 2026-09-23 ユーザー裁定で確定した値。手順書側がこれと違えば、確定値からの
# 無断変更（またはその逆の書き戻し忘れ）を意味する。
EXPECTED_LIMIT = 5
EXPECTED_TIMEOUT = 480
EXPECTED_MAX_TOTAL = 1140
EXPECTED_OUTER_TIMEOUT = 1200

# 手順2.5 の節の境界（見出し行そのもの。次見出しの手前までを節本文とみなす）。
_SECTION_START_RE = re.compile(r"^2\.5\.\s", re.MULTILINE)
_SECTION_END_RE = re.compile(r"^3\.\s", re.MULTILINE)

# 手順2.5 のコマンド行を1本だけ特定する。
# 例: `FETCH_FACTS_DIR="$PWD/fetch_facts" FACTS_DATE=$TARGET timeout 1200 python3 backfill.py --target $TARGET --limit 5 --timeout 480 --max-total 1140`
_COMMAND_LINE_RE = re.compile(
    r"^\s*`.*\btimeout\s+(?P<outer_timeout>\d+)\s+python3\s+backfill\.py\b"
    r".*?--limit\s+(?P<limit>\d+)"
    r".*?--timeout\s+(?P<timeout>\d+)"
    r".*?--max-total\s+(?P<max_total>\d+)"
    r".*`",
    re.MULTILINE,
)


def _extract_step2_5_section(text):
    """手順書全文から手順2.5 の節本文だけを切り出す。"""
    start_m = _SECTION_START_RE.search(text)
    assert start_m, "cloud_routine_prompt.md に手順2.5 の見出し（`2.5. `で始まる行）が見つからない"
    end_m = _SECTION_END_RE.search(text, start_m.end())
    assert end_m, "cloud_routine_prompt.md に手順3 の見出し（`3. `で始まる行）が見つからない（節の終端を確定できない）"
    assert end_m.start() > start_m.start(), "手順3 の見出しが手順2.5 より前にある（見出しの並びが壊れている）"
    return text[start_m.start():end_m.start()]


def _extract_command(section_text):
    matches = list(_COMMAND_LINE_RE.finditer(section_text))
    assert matches, (
        "手順2.5 の節に backfill.py コマンド行が見つからない"
        "（`timeout N python3 backfill.py ... --limit N --timeout N --max-total N` の形を探した）"
    )
    assert len(matches) == 1, (
        f"手順2.5 の節でコマンド行が{len(matches)}箇所ヒットした（1箇所のはず）。"
        "正規表現が広すぎるか、コマンド行が複製されている"
    )
    m = matches[0]
    return {
        "outer_timeout": int(m.group("outer_timeout")),
        "limit": int(m.group("limit")),
        "timeout": int(m.group("timeout")),
        "max_total": int(m.group("max_total")),
    }


def _read_step2_5_command():
    """cloud_routine_prompt.md の手順2.5 節からコマンド行の数値4つを抽出する。

    見つからなければ AssertionError（手順書側の書式が変わった・行が消えた等、
    テスト対象そのものが壊れているサイン）。
    """
    text = PROMPT_PATH.read_text(encoding="utf-8")
    section = _extract_step2_5_section(text)
    return _extract_command(section)


class Step2_5BudgetTest(unittest.TestCase):
    """cloud_routine_prompt.md 手順2.5 の実際の数値に対する検査。"""

    def setUp(self):
        self.values = _read_step2_5_command()

    # ---- 2026-09-23 確定値そのものへの一致（別の妥当な値への差し戻りを検出） ----

    def test_matches_decided_limit(self):
        self.assertEqual(
            self.values["limit"], EXPECTED_LIMIT,
            f"--limit は2026-09-23確定値の{EXPECTED_LIMIT}のはず（実際={self.values['limit']}）。"
            "値を変えたなら、このテストの EXPECTED_LIMIT も更新すること",
        )

    def test_matches_decided_timeout(self):
        self.assertEqual(
            self.values["timeout"], EXPECTED_TIMEOUT,
            f"--timeout は2026-09-23確定値の{EXPECTED_TIMEOUT}のはず（実際={self.values['timeout']}）",
        )

    def test_matches_decided_max_total(self):
        self.assertEqual(
            self.values["max_total"], EXPECTED_MAX_TOTAL,
            f"--max-total は2026-09-23確定値の{EXPECTED_MAX_TOTAL}のはず"
            f"（実際={self.values['max_total']}）",
        )

    def test_matches_decided_outer_timeout(self):
        self.assertEqual(
            self.values["outer_timeout"], EXPECTED_OUTER_TIMEOUT,
            f"外側 timeout は2026-09-23確定値の{EXPECTED_OUTER_TIMEOUT}のはず"
            f"（実際={self.values['outer_timeout']}）",
        )

    # ---- 一般則（確定値を将来変えても守られるべき不等式） ----

    def test_outer_timeout_covers_max_total_plus_60(self):
        v = self.values
        self.assertGreaterEqual(
            v["outer_timeout"], v["max_total"] + 60,
            "外側 timeout は --max-total + 60 以上でなければならない"
            f"（outer_timeout={v['outer_timeout']}, max_total={v['max_total']}）",
        )

    def test_max_total_covers_single_timeout(self):
        v = self.values
        self.assertGreaterEqual(
            v["max_total"], v["timeout"],
            "--max-total は --timeout（1件あたりの上限）以上でなければならない"
            f"（max_total={v['max_total']}, timeout={v['timeout']}）",
        )

    def test_limit_is_at_least_one(self):
        v = self.values
        self.assertGreaterEqual(
            v["limit"], 1,
            f"--limit は1以上でなければならない（limit={v['limit']}）",
        )


class SectionScopeTest(unittest.TestCase):
    """節の切り出し自体の回帰テスト（手元の文字列に対して。手順書ファイルは読まない）。"""

    def test_command_outside_section_is_ignored(self):
        """手順2.5 の節の外にある例示は無視する（範囲を全体にしていないことの確認）。"""
        text = (
            "前置き\n"
            "2.5. バックフィル\n"
            '   `FETCH_FACTS_DIR="$PWD/fetch_facts" FACTS_DATE=$TARGET timeout 1200 '
            "python3 backfill.py --target $TARGET --limit 5 --timeout 480 "
            "--max-total 1140` を実行する。\n"
            "3. 次の手順\n"
            "   旧版の例: `timeout 2500 python3 backfill.py --target $TARGET --limit 5 "
            "--timeout 480 --max-total 2400`（節の外・無視されるべき）\n"
        )
        section = _extract_step2_5_section(text)
        result = _extract_command(section)
        self.assertEqual(result, {
            "outer_timeout": 1200, "limit": 5, "timeout": 480, "max_total": 1140,
        })

    def test_missing_command_line_in_section_fails(self):
        """節はあるがコマンド行そのものが無ければ検出できる（実行行が消えた事故）。"""
        text = (
            "2.5. バックフィル\n"
            "   説明だけでコマンド行が無い。\n"
            "3. 次の手順\n"
        )
        section = _extract_step2_5_section(text)
        with self.assertRaises(AssertionError):
            _extract_command(section)

    def test_extracts_current_values(self):
        line = (
            '   `FETCH_FACTS_DIR="$PWD/fetch_facts" FACTS_DATE=$TARGET timeout 1200 '
            "python3 backfill.py --target $TARGET --limit 5 --timeout 480 "
            "--max-total 1140` を実行する。"
        )
        text = "2.5. バックフィル\n" + line + "\n3. 次の手順\n"
        section = _extract_step2_5_section(text)
        self.assertEqual(
            _extract_command(section),
            {"outer_timeout": 1200, "limit": 5, "timeout": 480, "max_total": 1140},
        )


if __name__ == "__main__":
    unittest.main()
