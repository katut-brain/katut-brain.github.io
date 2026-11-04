# -*- coding: utf-8 -*-
"""Groq Whisper API（whisper-large-v3）で音声を書き起こす。

方針（夜間の fetch_content から呼ぶ）:
  - 環境変数 GROQ_API_KEY が無いときは書き起こしをスキップし、::warning を1回出して戻る。例外は出さない。
  - ffprobe で音声ストリームを確認してから ffmpeg で Opus 約24kbps に圧縮する。
  - 無音、および音声帯域が極端に細い音楽だけのクリップは API を呼ばない。
  - 25MB を超えるファイルは分割し、チャンクの開始秒を足してタイムスタンプを繋ぐ。
  - 定型句・極端に低い文字/秒・compression_ratio > 2.4 の行は消さず〔要確認〕を付ける。
  - 1夜の音声秒数に上限がある。超えたら API を呼ばず reason=budget で戻る。
  - キーも書き起こし本文もログに出さない。一時ファイルは必ず消す。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

GROQ_MODEL = "whisper-large-v3"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
OPUS_BITRATE = "24k"
# Groq の1リクエスト上限は 25MB。マルチパートの余白を見て分割する。
MAX_PART_BYTES = 24 * 1024 * 1024
YT_MAX_DURATION_S = 15 * 60
DEFAULT_BUDGET_S = 1800  # 30分/夜。GROQ_BUDGET_SECONDS で上書きできる
TRANSCRIPT_OUT_MAX = 8000
FLAG = "〔要確認〕"

# 空白を除いた形で比較する。実在の文に混ざっていても消さず印を付けるだけ。
_STOCK = (
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございます",
    "ご視聴頂きありがとうございました",
    "ご清聴ありがとうございました",
    "ご清聴ありがとうございます",
    "チャンネル登録お願いします",
    "字幕は自動生成",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "thanksforwatching",
)

_warned_missing_key = False
_warned_budget = False


def _fail(reason, duration_s=0):
    return {"ok": False, "reason": reason, "text": "", "flagged": 0, "duration_s": int(duration_s or 0)}


def _ok(text, flagged, duration_s):
    if len(text) > TRANSCRIPT_OUT_MAX:
        text = text[:TRANSCRIPT_OUT_MAX] + "\n…(以下略)"
    return {"ok": True, "reason": "", "text": text, "flagged": int(flagged),
            "duration_s": int(duration_s or 0)}


def api_key():
    return os.environ.get("GROQ_API_KEY", "").strip()


def warn_missing_key():
    """キーが無いことを1プロセス1回だけ ::warning で出す。値は出さない。"""
    global _warned_missing_key
    if _warned_missing_key:
        return
    _warned_missing_key = True
    print("::warning::GROQ_API_KEY is unset; audio transcription skipped", file=sys.stderr)


def warn_budget():
    global _warned_budget
    if _warned_budget:
        return
    _warned_budget = True
    print("::warning::Groq nightly audio budget exceeded; transcription skipped for this item",
          file=sys.stderr)


def _budget_cap():
    raw = os.environ.get("GROQ_BUDGET_SECONDS", "").strip()
    try:
        n = int(raw) if raw else DEFAULT_BUDGET_S
    except ValueError:
        n = DEFAULT_BUDGET_S
    return max(0, n)


def _budget_file(path=None):
    if path:
        return path
    env = os.environ.get("GROQ_BUDGET_FILE", "").strip()
    if env:
        return env
    return os.path.join(tempfile.gettempdir(), "katut_groq_budget.json")


def _jst_day(today=None):
    if today:
        return today
    import datetime
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).date().isoformat()


def _read_budget(path, day):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("date") == day:
            return float(data.get("used_s") or 0)
    except Exception:
        pass
    return 0.0


def budget_allows(duration_s, today=None, budget_path=None):
    """この秒数を足しても今夜の上限以内なら True。"""
    cap = _budget_cap()
    used = _read_budget(_budget_file(budget_path), _jst_day(today))
    return used + max(0.0, float(duration_s or 0)) <= cap + 0.001


def budget_commit(duration_s, today=None, budget_path=None):
    path = _budget_file(budget_path)
    day = _jst_day(today)
    used = _read_budget(path, day) + max(0.0, float(duration_s or 0))
    try:
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": day, "used_s": round(used, 1)}, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _norm_text(text):
    return re.sub(r"[\s\W_]+", "", (text or "").lower(), flags=re.UNICODE)


_STOCK_NORM = tuple(_norm_text(s) for s in _STOCK if _norm_text(s))


def is_stock_phrase(text):
    n = _norm_text(text)
    if not n:
        return False
    return any(p and p in n for p in _STOCK_NORM)


def flag_segment(text, start, end, seg):
    """(表示文, 印を付けるか)。本文は消さない。"""
    raw = (text or "").strip()
    if not raw:
        return "", False
    flag = False
    if is_stock_phrase(raw):
        flag = True
    dur = float(end) - float(start)
    if dur >= 3 and (len(raw) / dur) < 0.8:
        flag = True
    cr = seg.get("compression_ratio") if isinstance(seg, dict) else None
    if isinstance(cr, (int, float)) and not isinstance(cr, bool) and cr > 2.4:
        flag = True
    nsp = seg.get("no_speech_prob") if isinstance(seg, dict) else None
    if isinstance(nsp, (int, float)) and not isinstance(nsp, bool) and nsp > 0.6:
        flag = True
    return raw, flag


def _fmt_ts(t):
    t = max(0, int(round(float(t))))
    m, s = divmod(t, 60)
    h, m = divmod(m, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


def stitch_segments(chunks):
    """chunks: [(offset_seconds, [segment, ...]), ...]。offset を各 start/end に足す。"""
    lines = []
    flagged = 0
    for offset, segs in chunks:
        off = float(offset or 0)
        for seg in segs or []:
            if not isinstance(seg, dict):
                continue
            start = float(seg.get("start") or 0) + off
            end = float(seg.get("end") or seg.get("start") or 0) + off
            shown, mark = flag_segment(seg.get("text") or "", start, end, seg)
            if not shown:
                continue
            if mark:
                flagged += 1
                if not shown.startswith(FLAG):
                    shown = FLAG + shown
            lines.append("[%s → %s] %s" % (_fmt_ts(start), _fmt_ts(end), shown))
    return "\n".join(lines), flagged


def split_offsets(size, duration, max_bytes=MAX_PART_BYTES):
    """ファイルを max_bytes 以下に切るときの開始秒。1個なら [0.0]。"""
    size = int(size or 0)
    duration = float(duration or 0)
    if size <= max_bytes or duration <= 0:
        return [0.0]
    chunk_s = duration * (max_bytes * 0.9) / float(size)
    chunk_s = max(30.0, chunk_s)
    pts = []
    t = 0.0
    while t < duration - 0.5:
        pts.append(round(t, 3))
        t += chunk_s
    return pts or [0.0]


_VOL_RE = re.compile(r"(mean_volume|max_volume):\s*(-?[\d.]+)\s*dB")


def parse_volumedetect(text):
    out = {}
    for m in _VOL_RE.finditer(text or ""):
        try:
            out[m.group(1)] = float(m.group(2))
        except ValueError:
            continue
    return out


def classify_audio_levels(full, speech):
    """'' | 'silent' | 'music_only'。判定できないときは ''（書き起こしに進む）。"""
    full = full or {}
    speech = speech or {}
    mx = full.get("max_volume")
    if mx is None:
        return ""
    if mx <= -45.0:
        return "silent"
    sm = speech.get("mean_volume")
    fm = full.get("mean_volume")
    if sm is not None and fm is not None and fm > -35 and sm <= -40 and (fm - sm) >= 12:
        return "music_only"
    return ""


def parse_ffprobe_has_audio(payload):
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    for s in data.get("streams") or []:
        if isinstance(s, dict) and s.get("codec_type") == "audio":
            return True
    return False


def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


def _ffprobe_json(path, timeout):
    if not shutil.which("ffprobe"):
        return None
    p = _run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path], timeout)
    return (p.stdout or b"").decode("utf-8", "replace")


def _duration_sec(path, timeout):
    if not shutil.which("ffprobe"):
        return None
    p = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", path], timeout)
    raw = (p.stdout or b"").decode("utf-8", "replace").strip()
    try:
        return float(raw)
    except ValueError:
        return None


def _volumedetect(path, timeout, band=False):
    if not shutil.which("ffmpeg"):
        return {}
    af = "highpass=f=300,lowpass=f=3400,volumedetect" if band else "volumedetect"
    p = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", af, "-f", "null", "-"], timeout)
    err = (p.stderr or b"").decode("utf-8", "replace")
    return parse_volumedetect(err)


def _encode_opus(src, dest, timeout, max_duration_s=None):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", src]
    if max_duration_s:
        cmd += ["-t", str(int(max_duration_s))]
    cmd += ["-vn", "-ac", "1", "-c:a", "libopus", "-b:a", OPUS_BITRATE, "-ar", "16000",
            "-application", "voip", dest]
    p = _run(cmd, timeout)
    if p.returncode != 0 or not os.path.isfile(dest) or os.path.getsize(dest) <= 0:
        return False
    return True


def _cut_segment(src, dest, start, length, timeout):
    p = _run(["ffmpeg", "-y", "-hide_banner", "-nostats", "-ss", "%.3f" % start, "-t", "%.3f" % length,
              "-i", src, "-c", "copy", dest], timeout)
    return p.returncode == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 0


def _multipart(fields, filename, content, mime="audio/ogg"):
    boundary = "----katutgroq%x" % (time.time_ns() & 0xFFFFFFFFFFFF)
    chunks = []
    for key, val in fields:
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                       % (boundary, key, val)).encode("utf-8"))
    head = ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
            "Content-Type: %s\r\n\r\n" % (boundary, filename, mime)).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    body = b"".join(chunks) + head + content + tail
    return body, boundary


def post_transcription(path, timeout):
    """verbose_json を返す。失敗は {'_error': reason}。キーはメッセージに含めない。"""
    key = api_key()
    if not key:
        return {"_error": "no_api_key"}
    try:
        with open(path, "rb") as f:
            content = f.read()
    except OSError:
        return {"_error": "read_failed"}
    if len(content) > 25 * 1024 * 1024:
        return {"_error": "part_too_large"}
    fields = [
        ("model", GROQ_MODEL),
        ("response_format", "verbose_json"),
        ("temperature", "0"),
        ("timestamp_granularities[]", "segment"),
    ]
    body, boundary = _multipart(fields, os.path.basename(path) or "audio.opus", content)
    req = urllib.request.Request(
        GROQ_URL, data=body, method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(2_000_000)
        return json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"_error": "http_%s" % e.code}
    except Exception as e:
        return {"_error": "exception:%s" % type(e).__name__}


def _segments_of(payload, duration):
    if not isinstance(payload, dict) or payload.get("_error"):
        return None
    segs = payload.get("segments")
    if isinstance(segs, list) and segs:
        return segs
    text = (payload.get("text") or "").strip()
    if not text:
        return []
    return [{"start": 0.0, "end": float(duration or 0), "text": text}]


def _remaining(deadline):
    if deadline is None:
        return 10 ** 9
    return deadline - time.monotonic()


def transcribe_file(path, deadline=None, today=None, budget_path=None, max_duration_s=None):
    """ローカルの音声/動画ファイルを書き起こす。例外は出さない。"""
    try:
        return _transcribe_file(path, deadline, today, budget_path, max_duration_s)
    except Exception as e:
        return _fail("exception:%s" % type(e).__name__)


def _transcribe_file(path, deadline, today, budget_path, max_duration_s):
    if not api_key():
        warn_missing_key()
        return _fail("no_api_key")
    if _remaining(deadline) < 5:
        return _fail("deadline_skipped")
    if not path or not os.path.isfile(path):
        return _fail("missing_file")
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        return _fail("ffmpeg_missing")
    timeout = max(5, min(60, int(_remaining(deadline))))
    probed = _ffprobe_json(path, timeout)
    if probed is None:
        return _fail("ffprobe_failed")
    if not parse_ffprobe_has_audio(probed):
        return _fail("no_audio_stream")
    dur = _duration_sec(path, timeout) or 0
    if max_duration_s and dur > float(max_duration_s) + 1:
        return _fail("duration_cap", dur)
    gate = classify_audio_levels(_volumedetect(path, timeout, band=False),
                                 _volumedetect(path, timeout, band=True))
    if gate:
        return _fail(gate, dur)
    if not budget_allows(dur or 1, today=today, budget_path=budget_path):
        warn_budget()
        return _fail("budget", dur)

    tmp = tempfile.mkdtemp(prefix="katut_groq_")
    try:
        opus = os.path.join(tmp, "audio.opus")
        enc_timeout = max(5, min(90, int(_remaining(deadline))))
        if not _encode_opus(path, opus, enc_timeout, max_duration_s=max_duration_s):
            return _fail("encode_failed", dur)
        opus_dur = _duration_sec(opus, timeout) or dur
        size = os.path.getsize(opus)
        offsets = split_offsets(size, opus_dur)
        chunks = []
        if len(offsets) == 1:
            parts = [(0.0, opus)]
        else:
            parts = []
            for i, start in enumerate(offsets):
                end = offsets[i + 1] if i + 1 < len(offsets) else opus_dur
                piece = os.path.join(tmp, "part%03d.opus" % i)
                if not _cut_segment(opus, piece, start, max(0.1, end - start), timeout):
                    return _fail("split_failed", dur)
                parts.append((start, piece))
        for offset, piece in parts:
            if _remaining(deadline) < 5:
                return _fail("deadline_skipped", dur)
            payload = post_transcription(piece, max(5, min(60, int(_remaining(deadline)))))
            if isinstance(payload, dict) and payload.get("_error"):
                return _fail(payload["_error"], dur)
            segs = _segments_of(payload, opus_dur)
            if segs is None:
                return _fail("bad_response", dur)
            chunks.append((offset, segs))
        text, flagged = stitch_segments(chunks)
        if not text.strip():
            return _fail("empty", dur)
        budget_commit(opus_dur or dur, today=today, budget_path=budget_path)
        return _ok(text, flagged, opus_dur or dur)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ytdlp_bin():
    return shutil.which("yt-dlp")


def youtube_host_ok(url):
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except Exception:
        return False
    host = host.lower()
    return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be" or host.endswith(".youtu.be")


def ytdlp_duration_argv(url):
    return [ytdlp_bin() or "yt-dlp", "--no-playlist", "--no-cookies", "--no-warnings",
            "--skip-download", "--print", "duration", url]


def ytdlp_audio_argv(url, outtmpl, max_duration_s):
    return [ytdlp_bin() or "yt-dlp", "--no-playlist", "--no-cookies", "--no-warnings", "--no-progress",
            "--socket-timeout", "20", "--retries", "1",
            "--match-filter", "duration <= %d" % int(max_duration_s),
            "--max-filesize", "30M",
            "-f", "ba/b",
            "-o", outtmpl, url]


def _argv_has_secret_flags(cmd):
    banned = {"--cookies", "--cookies-from-browser", "--username", "--password", "-u", "-p"}
    return any(a in banned or a.startswith("--cookies=") for a in cmd)


def transcribe_youtube(url, max_duration_s=YT_MAX_DURATION_S, deadline=None, today=None, budget_path=None):
    """字幕が無い YouTube の音声。15分を超えるものはダウンロードしない。例外は出さない。"""
    try:
        return _transcribe_youtube(url, max_duration_s, deadline, today, budget_path)
    except Exception as e:
        return _fail("exception:%s" % type(e).__name__)


def _transcribe_youtube(url, max_duration_s, deadline, today, budget_path):
    if not api_key():
        warn_missing_key()
        return _fail("no_api_key")
    if not youtube_host_ok(url):
        return _fail("host_not_allowed")
    if not ytdlp_bin():
        return _fail("ytdlp_missing")
    if _remaining(deadline) < 8:
        return _fail("deadline_skipped")
    dur_cmd = ytdlp_duration_argv(url)
    if _argv_has_secret_flags(dur_cmd):
        return _fail("refused_cookies")
    try:
        p = _run(dur_cmd, max(5, min(30, int(_remaining(deadline)))))
    except subprocess.TimeoutExpired:
        return _fail("ytdlp_timeout")
    except Exception:
        return _fail("ytdlp_failed")
    raw = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    dur = None
    if raw:
        try:
            dur = float(raw[-1])
        except ValueError:
            dur = None
    if dur is not None and dur > float(max_duration_s):
        return _fail("duration_cap", dur)
    tmp = tempfile.mkdtemp(prefix="katut_ytaudio_")
    try:
        outtmpl = os.path.join(tmp, "a.%(ext)s")
        cmd = ytdlp_audio_argv(url, outtmpl, max_duration_s)
        if _argv_has_secret_flags(cmd):
            return _fail("refused_cookies")
        try:
            _run(cmd, max(5, min(90, int(_remaining(deadline)))))
        except subprocess.TimeoutExpired:
            return _fail("ytdlp_timeout")
        files = [os.path.join(tmp, n) for n in os.listdir(tmp)
                 if os.path.isfile(os.path.join(tmp, n)) and os.path.getsize(os.path.join(tmp, n)) > 0]
        if not files:
            return _fail("ytdlp_failed", dur or 0)
        return transcribe_file(files[0], deadline=deadline, today=today, budget_path=budget_path,
                                max_duration_s=max_duration_s)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
