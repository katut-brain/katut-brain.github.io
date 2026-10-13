# -*- coding: utf-8 -*-
"""手順1.5: 依存導入(--install)と、google-genai / youtube-transcript-api が今のインタプリタから
import できるかの点検。常に exit 0（どう連結されても無人運用を止めない）。

  python3 check_deps.py --install    # pip install -> 失敗時のみ tail -> (PEP 668 なら1回だけ再試行) -> 点検
  PIP_EXIT=0 python3 check_deps.py   # 点検のみ（pip の終了コードは環境変数か第1引数）

出力: DEPS_STATUS: python=<版> genai=ok|fail(<型>: <先頭200字>) yt_transcript=ok|fail(...)
                   gemini_key=set|unset pip_exit=<数値|unknown> pip_retry=none|bsp
例外文のうち GEMINI_API_KEY の値・キー形の文字列は *** に伏せる（fetch_facts は公開される）。
"""
import os
import re
import subprocess
import sys

_KEY_SHAPE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


def redact(msg, limit=200):
    """秘密を伏せてから切り詰める（切り詰め後だと鍵が途中で切れて伏せ損なうため順序が重要）。"""
    msg = " ".join(str(msg).split())
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        msg = msg.replace(key, "***")
    return _KEY_SHAPE.sub("***", msg)[:limit]


def _try(stmt):
    try:
        exec(stmt, {})
        return "ok"
    except BaseException as e:  # noqa: BLE001 - どんな失敗も報告に変える
        return "fail(%s: %s)" % (type(e).__name__, redact(e))


def status_line(pip_exit=None, pip_retry="none"):
    pe = str(pip_exit).strip() if pip_exit not in (None, "") else "unknown"
    if not pe.lstrip("-").isdigit():
        pe = "unknown"
    v = sys.version_info
    return ("DEPS_STATUS: python=%d.%d.%d genai=%s yt_transcript=%s gemini_key=%s "
            "pip_exit=%s pip_retry=%s") % (
        v[0], v[1], v[2], _try("from google import genai"),
        _try("from youtube_transcript_api import YouTubeTranscriptApi"),
        "set" if os.environ.get("GEMINI_API_KEY") else "unset", pe, pip_retry)


def install():
    """(pip終了コード, pip_retry) を返す。pip 自体が起動できない場合も例外にしない。"""
    base = [sys.executable, "-m", "pip", "install", "--quiet", "-r", "requirements.txt"]

    def run(cmd):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except Exception as e:  # noqa: BLE001
            return 127, "%s: %s" % (type(e).__name__, e)

    rc, out = run(base)
    retry = "none"
    if rc != 0 and "externally-managed-environment" in out:
        retry = "bsp"   # PEP 668 の保険。1回だけ --break-system-packages で入れ直す
        rc, out = run(base + ["--break-system-packages"])
    if rc != 0:
        print("\n".join(out.strip().splitlines()[-5:]))
    return rc, retry


if __name__ == "__main__":
    try:
        if "--install" in sys.argv[1:]:
            rc, retry = install()
            print(status_line(rc, retry))
        else:
            arg = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PIP_EXIT")
            print(status_line(arg))
    except BaseException as e:  # noqa: BLE001
        print("DEPS_STATUS: check_failed(%s)" % type(e).__name__)
    sys.exit(0)
