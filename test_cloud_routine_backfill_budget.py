"""cloud_routine_prompt.md 手順2.5（バックフィル）のコマンド行の予算関係を検査する。

守るのは3点だけ（2026-09-23 ユーザー裁定）:
  1. 外側シェル `timeout` は `--max-total` に60秒以上の余裕を足した値であること
     （親が外側 timeout より先に自分で終わらないと、子が孤児化して排他ロックの無い
     `fetch_facts/<日付>.json` に競合書込みする。cloud_routine_prompt.md 手順2.5 の既存規約）。
  2. `--max-total` は `--timeout`（1件あたりの上限）以上であること
     （そうでないと1件もフルに起動できない）。
  3. `--limit` は1以上であること（0や負では候補を1件も処理しない）。

この手順書は Routine が実行時に読む正本であって、ここに書かれた数値がそのまま
無人ランへ渡る。数値がずれても import エラー等では検出できないため、
手順書のテキストを直接解析して壊れた値を機械的に検出する。
"""

import re
import unittest
from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parent / "cloud_routine_prompt.md"

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


def _read_step2_5_command():
    """cloud_routine_prompt.md の手順2.5 コマンド行から数値4つを抽出する。

    見つからなければ AssertionError（手順書側の書式が変わった・行が消えた等、
    テスト対象そのものが壊れているサイン）。
    """
    text = PROMPT_PATH.read_text(encoding="utf-8")
    matches = list(_COMMAND_LINE_RE.finditer(text))
    assert matches, (
        "cloud_routine_prompt.md に手順2.5 の backfill.py コマンド行が見つからない"
        "（`timeout N python3 backfill.py ... --limit N --timeout N --max-total N` の形を探した）"
    )
    assert len(matches) == 1, (
        f"手順2.5 のコマンド行が{len(matches)}箇所ヒットした（1箇所のはず）。"
        "正規表現が広すぎるか、コマンド行が複製されている"
    )
    m = matches[0]
    return {
        "outer_timeout": int(m.group("outer_timeout")),
        "limit": int(m.group("limit")),
        "timeout": int(m.group("timeout")),
        "max_total": int(m.group("max_total")),
    }


class Step2_5BudgetTest(unittest.TestCase):
    """cloud_routine_prompt.md 手順2.5 の実際の数値に対する検査（ミューテーションなし）。"""

    def setUp(self):
        self.values = _read_step2_5_command()

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


class ExtractorRegressionTest(unittest.TestCase):
    """抽出ロジック自体の回帰テスト（手元の文字列に対して。手順書ファイルは読まない）。"""

    def _extract(self, line):
        text = "前置き\n" + line + "\n後書き\n"
        matches = list(_COMMAND_LINE_RE.finditer(text))
        self.assertEqual(len(matches), 1)
        m = matches[0]
        return {
            "outer_timeout": int(m.group("outer_timeout")),
            "limit": int(m.group("limit")),
            "timeout": int(m.group("timeout")),
            "max_total": int(m.group("max_total")),
        }

    def test_extracts_current_values(self):
        line = (
            '   `FETCH_FACTS_DIR="$PWD/fetch_facts" FACTS_DATE=$TARGET timeout 1200 '
            "python3 backfill.py --target $TARGET --limit 5 --timeout 480 "
            "--max-total 1140` を実行する。"
        )
        self.assertEqual(
            self._extract(line),
            {"outer_timeout": 1200, "limit": 5, "timeout": 480, "max_total": 1140},
        )


if __name__ == "__main__":
    unittest.main()
