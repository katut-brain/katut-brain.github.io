# -*- coding: utf-8 -*-
"""手順1.5: 依存パッケージ(google-genai / youtube-transcript-api)が、今のインタプリタから
import できるかを1行で報告する。常に exit 0（無人運用を止めない）。

出力: DEPS_STATUS: python=<版> genai=ok|fail(<例外型>: <先頭200字>) yt_transcript=ok|fail(...) pip_exit=<数値|unknown>
pip の終了コードは環境変数 PIP_EXIT か第1引数で受け取る。
"""
import sys
import os


def _try(stmt):
    try:
        exec(stmt, {})
        return "ok"
    except BaseException as e:  # noqa: BLE001 - どんな失敗も報告に変える
        msg = " ".join(str(e).split())[:200]
        return "fail(%s: %s)" % (type(e).__name__, msg)


def status_line(pip_exit=None):
    pe = str(pip_exit).strip() if pip_exit not in (None, "") else "unknown"
    if not pe.lstrip("-").isdigit():
        pe = "unknown"
    v = sys.version_info
    return "DEPS_STATUS: python=%d.%d.%d genai=%s yt_transcript=%s pip_exit=%s" % (
        v[0], v[1], v[2], _try("from google import genai"),
        _try("from youtube_transcript_api import YouTubeTranscriptApi"), pe)


if __name__ == "__main__":
    try:
        arg = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PIP_EXIT")
        print(status_line(arg))
    except BaseException as e:  # noqa: BLE001
        print("DEPS_STATUS: check_failed(%s)" % type(e).__name__)
    sys.exit(0)
