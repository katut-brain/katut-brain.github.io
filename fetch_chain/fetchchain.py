#!/usr/bin/env python3
"""fetchchain — 取得手段を優先順に試し、壊れたら次へ回す（v0.2・標準ライブラリのみ）。

  python fetchchain.py fetch <url>   # チェーンで取得。使った手段・失敗した手段を返す
  python fetchchain.py canary        # 全手段を見本で実測。status.json を更新（終了コード 0=全部OK 1=一部だめ 2=丸ごとだめ）
  python fetchchain.py status        # 直近の canary 結果を表示

設計の要点:
  - 手段は sources.json に優先順で並べる。コードは触らず一覧だけ直せば手段を足せる
  - canary は「入っているか」でなく「見本を実際に取って、期待する文字が出るか」を見る
  - 直近3回続けて失敗した手段は、取得時に自動で最後尾へ回す（health.jsonl の記録から）
  - 手段は plugins/*.json の「宣言」でも足せる（コードを含まない窓口型のみ。試用中は最後尾・標準手段は上書き不可）
  - 他人のプログラムを入れる場合は、先に vet.py の検査を通す（自動で探して足す部分は未実装）
"""
import sys, os, re, json, time, html, string, ipaddress, socket, pathlib, datetime, shutil, subprocess, tempfile
import urllib.request, urllib.error, urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
REGISTRY = HERE / "sources.json"
PLUGINS = HERE / "plugins"       # 自動で足された手段の仕様（1手段1ファイル）
# 実測の記録はリポジトリの外に置く（PC で読んだ投稿の記録を公開リポに混ぜない）。クラウドの健康診断は記録用ブランチを指す
STATE = pathlib.Path(os.environ.get("FETCHCHAIN_STATE_DIR") or pathlib.Path.home() / ".fetch-chain")
HEALTH = STATE / "health.jsonl"  # 追記のみ
STATUS = STATE / "status.json"

# 名乗りは偽らない。ブラウザやクローラーを名乗るのは検知の回避になるので使わない（2026-10-30）
UA = "fetch-chain/0.2 (+https://github.com/katut-brain/katut-brain.github.io)"
DEMOTE_AFTER = 3  # 直近この回数すべて失敗した手段は最後尾へ


def where_am_i():
    """いまの実行場所（pc / gha / claude-cloud）。手段の効く場所と健康記録の仕分けに使う。"""
    env = os.environ.get("FETCHCHAIN_ENV")
    if env:
        return env
    if os.environ.get("GITHUB_ACTIONS"):
        return "gha"
    if os.environ.get("CLAUDE_CODE_REMOTE"):
        return "claude-cloud"
    return "pc"


def place_class(env):
    """sources.json の where は pc / cloud の2択（gha と claude-cloud はどちらも cloud）。"""
    return "pc" if env == "pc" else "cloud"


class FetchError(Exception):
    pass


class Unavailable(FetchError):
    """この場所では使えない手段（道具が入っていない等）。壊れたとは数えない。"""


# ---------- HTTP（公開ホストだけ・リダイレクト先も検査） ----------
def _check_public(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchError("http/https 以外は取らない: %s" % url)
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror as e:
        raise FetchError("名前解決に失敗: %s" % e)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FetchError("公開ホストでない（%s → %s）" % (p.hostname, ip))


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url, timeout=15, max_bytes=2_000_000):
    """(status, text) を返す。通信失敗は (None, "")。"""
    _check_public(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    opener = urllib.request.build_opener(_Redirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(max_bytes).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except FetchError:
        raise
    except Exception:
        return None, ""


def _json_get(url):
    st, txt = http_get(url)
    if st != 200 or not txt.strip():
        raise FetchError("HTTP %s・本文が空" % st)
    try:
        return json.loads(txt)
    except ValueError:
        raise FetchError("JSON として読めない")


def _tweet_id(url):
    m = re.search(r"/status/(\d+)", url)
    if not m:
        raise FetchError("URL から投稿IDを取れない")
    return m.group(1)


def _strip_html(raw):
    raw = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


# ---------- 取得手段（どれも {text,title,author,date} を返す） ----------
def x_fxtwitter(url):
    raw = _json_get("https://api.fxtwitter.com/i/status/%s" % _tweet_id(url))
    d = raw.get("tweet")
    if not d:
        raise FetchError("tweet が無い")
    text = d.get("text", "") or ""
    blocks = ((d.get("article") or {}).get("content") or {}).get("blocks") or []
    if blocks:  # X Article（長文記事）は本文を足す
        text = (text + "\n\n" + "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))).strip()
    return {"text": text, "title": text[:80], "author": (d.get("author") or {}).get("screen_name", ""),
            "date": d.get("created_at", ""), "raw": raw}


def x_vxtwitter(url):
    m = re.search(r"(?:x|twitter)\.com/([^/]+)/status/(\d+)", url)
    if not m:
        raise FetchError("URL から投稿者とIDを取れない")
    d = _json_get("https://api.vxtwitter.com/%s/status/%s" % (m.group(1), m.group(2)))
    return {"text": d.get("text", ""), "title": (d.get("text", "") or "")[:80],
            "author": d.get("user_screen_name", ""), "date": d.get("date", ""), "raw": d}


def _syndication_token(tid):
    digs = string.digits + string.ascii_lowercase
    x = (int(tid) / 1e15) * 3.141592653589793

    def b36(n):
        n, s = int(n), ""
        if n == 0:
            return "0"
        while n:
            n, r = divmod(n, 36)
            s = digs[r] + s
        return s
    frac, fs = x - int(x), ""
    for _ in range(12):
        frac *= 36
        d = int(frac)
        fs += digs[d]
        frac -= d
    return (b36(int(x)) + fs).replace("0", "").replace(".", "")


def x_syndication(url):
    tid = _tweet_id(url)
    d = _json_get("https://cdn.syndication.twimg.com/tweet-result?id=%s&token=%s&lang=en"
                  % (tid, _syndication_token(tid)))
    return {"text": d.get("text", ""), "title": (d.get("text", "") or "")[:80],
            "author": (d.get("user") or {}).get("screen_name", ""), "date": d.get("created_at", ""), "raw": d}


def yt_oembed(url):
    d = _json_get("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe=""))
    return {"text": d.get("title", ""), "title": d.get("title", ""),
            "author": d.get("author_name", ""), "date": "", "raw": d}


def web_direct(url):
    st, txt = http_get(url)
    if st != 200 or not txt:
        raise FetchError("HTTP %s" % st)
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", txt)
    return {"text": _strip_html(txt), "title": html.unescape(m.group(1)).strip() if m else "",
            "author": "", "date": ""}


def web_jina(url):
    st, txt = http_get("https://r.jina.ai/" + url)
    if st != 200 or not txt.strip():
        raise FetchError("HTTP %s" % st)
    m = re.search(r"(?m)^Title:\s*(.+)$", txt)
    return {"text": txt.strip(), "title": m.group(1).strip() if m else "", "author": "", "date": ""}


IG_EPOCH_MS = 1314220021721  # Instagram の ID の上位ビットは、この時刻からのミリ秒


def _ig_date(media_id):
    try:
        ms = (int(str(media_id).split("_")[0]) >> 23) + IG_EPOCH_MS
    except ValueError:
        return ""
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec="seconds")


def ig_oembed(url):
    # 公式の oEmbed。title にキャプション、media_id から投稿日時。年齢制限の投稿はエラー JSON（media_igid だけ・本文なし）
    d = _json_get("https://www.instagram.com/api/v1/oembed/?url=" + urllib.parse.quote(url, safe=""))
    mid = d.get("media_id") or d.get("media_igid") or ""
    return {"text": d.get("title", "") or "", "title": (d.get("title", "") or "")[:80],
            "author": d.get("author_name", ""), "date": _ig_date(mid) if mid else "", "raw": d}


def _og(page, prop):
    m = re.search(r'<meta[^>]+property="og:%s"[^>]+content="([^"]*)"' % prop, page)
    return html.unescape(m.group(1)) if m else ""


def og_meta(url):
    # 公開ページの og:title / og:description（Instagram・Threads は未ログインでもここに本文の頭が入る）
    st, txt = http_get(url)
    if st != 200 or not txt:
        raise FetchError("HTTP %s" % st)
    desc, title = _og(txt, "description"), _og(txt, "title")
    return {"text": desc, "title": title, "author": "", "date": "",
            "raw": {"og:title": title, "og:description": desc, "og:image": _og(txt, "image")}}


def _ytdlp():
    return shutil.which("yt-dlp")


def yt_dlp_subs(url):
    """YouTube の字幕（自動字幕を含む）を本文として返す。yt-dlp が既に入っている場所でだけ動く（ここでは入れない）。"""
    exe = _ytdlp()
    if not exe:
        raise Unavailable("yt-dlp が無い（自動では入れない。入れるなら vet.py を通す）")
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [exe, "--skip-download", "--no-simulate", "--no-warnings", "--write-subs", "--write-auto-subs",
               "--sub-langs", ".*-orig,ja,en", "--sub-format", "json3", "--user-agent", UA,
               "-o", os.path.join(tmp, "s.%(ext)s"), "--print", "%(title)s\t%(uploader)s\t%(upload_date)s", url]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        except subprocess.TimeoutExpired:
            raise FetchError("yt-dlp が120秒で終わらない")
        if p.returncode != 0:
            raise FetchError("yt-dlp 失敗: %s" % ((p.stderr.strip().splitlines() or ["?"])[-1][:200]))
        meta = ((p.stdout.strip().splitlines() or [""])[0].split("\t") + ["", "", ""])[:3]
        # 元の言語の字幕を先に（ja-orig・en-orig など）。無ければ ja、次に en
        files = sorted(pathlib.Path(tmp).glob("s.*.json3"),
                       key=lambda f: ("-orig" not in f.name, ".ja" not in f.name, f.name))
        if not files:
            raise FetchError("字幕が無い")
        ev = json.loads(files[0].read_text(encoding="utf-8")).get("events") or []
        # 字幕の行送りの改行は、文の途中に入るので空白にまとめる
        text = re.sub(r"\s+", " ", "".join(sg.get("utf8", "") for e in ev for sg in (e.get("segs") or []))).strip()
        return {"text": text, "title": meta[0], "author": meta[1], "date": meta[2],
                "raw": {"subtitle_file": files[0].name}}


METHODS = {f.__name__: f for f in (x_fxtwitter, x_vxtwitter, x_syndication, yt_oembed, yt_dlp_subs,
                                    ig_oembed, og_meta, web_direct, web_jina)}
CORE_METHODS = frozenset(METHODS)  # プラグインはこの名前を上書きできない
MIN_CHARS = {"web_direct": 200, "web_jina": 200}  # 中身がほぼ空なら「取れた」と数えない
DEFAULT_MIN_CHARS = 1


# ---------- プラグイン手段（コードを含まない宣言だけ。AI が書いた仕様でも PC 上で他人のコードは動かない） ----------
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
_VAR_RE = re.compile(r"^[a-z][a-z0-9_]{0,20}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_.]+$")
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_MAP_KEYS = ("text", "title", "author", "date")


def validate_spec(spec, channels):
    """プラグイン仕様の誤りを文字列のリストで返す（空なら有効）。"""
    if not isinstance(spec, dict):
        return ["仕様が JSON の連想配列でない"]
    errs = []
    name = spec.get("name", "")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        errs.append("name が不正（小文字英数と _ の3〜41字）")
    elif name in CORE_METHODS:
        errs.append("標準の手段と同じ名前は使えない: %s" % name)
    if spec.get("channel") not in channels:
        errs.append("channel が sources.json に無い: %s" % spec.get("channel"))
    if spec.get("kind") != "http":
        errs.append('kind は "http" のみ')
    if spec.get("format") not in ("json", "text"):
        errs.append('format は "json" か "text"')
    if spec.get("status") not in ("trial", "active"):
        errs.append('status は "trial" か "active"')
    url = spec.get("url")
    vars_ = spec.get("vars_from_url", {})
    if not isinstance(vars_, dict):
        errs.append("vars_from_url は連想配列")
        vars_ = {}
    if not isinstance(url, str) or not url.startswith("https://") or re.search(r"\s", url):
        errs.append("url は空白なしの https:// で始まる文字列")
    else:
        netloc = url[len("https://"):].split("/", 1)[0]
        if "{" in netloc or not netloc:
            errs.append("接続先のホスト名にプレースホルダは使えない")
        allowed = set(vars_) | {"url"}
        for ph in _PLACEHOLDER_RE.findall(url):
            if ph not in allowed:
                errs.append("url のプレースホルダ {%s} の取り出し方が vars_from_url に無い" % ph)
        if re.search(r"\{[^}]*[^\w}][^}]*\}", url):
            errs.append("url の {} の中は英数字と _ だけ")
    for k, pat in vars_.items():
        if not isinstance(k, str) or not _VAR_RE.match(k) or k == "url":
            errs.append("vars_from_url のキーが不正: %r" % (k,))
            continue
        if not isinstance(pat, str) or len(pat) > 200:
            errs.append("vars_from_url[%s] の正規表現が不正か長すぎる" % k)
            continue
        try:
            if re.compile(pat).groups != 1:
                errs.append("vars_from_url[%s] はグループがちょうど1つ必要" % k)
        except re.error:
            errs.append("vars_from_url[%s] が正規表現として不正" % k)
    mp = spec.get("map", {})
    if spec.get("format") == "json":
        if not isinstance(mp, dict) or "text" not in mp or set(mp) - set(_MAP_KEYS):
            errs.append("map は text を含み、キーは %s のみ" % "/".join(_MAP_KEYS))
        else:
            for k, p in mp.items():
                if not isinstance(p, str) or not _PATH_RE.match(p):
                    errs.append("map[%s] の取り出しパスが不正（英数字と . と _ のみ）" % k)
    if "ua" in spec:
        errs.append("ua は指定できない（名乗りは fetch-chain の UA に固定）")
    mc = spec.get("min_chars", DEFAULT_MIN_CHARS)
    if not isinstance(mc, int) or isinstance(mc, bool) or not 1 <= mc <= 5000:
        errs.append("min_chars は 1〜5000 の整数")
    return errs


def _dig(d, path):
    for part in path.split("."):
        if isinstance(d, list):
            try:
                d = d[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(d, dict):
            d = d.get(part)
        else:
            return None
    return d


def make_http_method(spec):
    """検証済みの仕様から取得関数を作る。url の置換は str.format を使わない（書式式の悪用を避ける）。"""
    def _method(url):
        vals = {"url": urllib.parse.quote(url, safe="")}
        for k, pat in (spec.get("vars_from_url") or {}).items():
            m = re.search(pat, url[:2000])
            if not m:
                raise FetchError("URL から %s を取れない" % k)
            vals[k] = urllib.parse.quote(m.group(1), safe="")
        target = _PLACEHOLDER_RE.sub(lambda m: vals[m.group(1)], spec["url"])
        if spec["format"] == "json":
            d = _json_get(target)
            out = {k: ("" if _dig(d, p) is None else str(_dig(d, p))) for k, p in spec["map"].items()}
            for k in _MAP_KEYS:
                out.setdefault(k, "")
            if not out["title"]:
                out["title"] = out["text"][:80]
            out["raw"] = d
            return out
        st, txt = http_get(target)
        if st != 200 or not txt.strip():
            raise FetchError("HTTP %s" % st)
        m = re.search(r"(?m)^Title:\s*(.+)$", txt)
        return {"text": txt.strip(), "title": m.group(1).strip() if m else "", "author": "", "date": ""}
    _method.__name__ = spec["name"]
    return _method


PLUGIN_STATUS = {}  # 手段名 → "trial" | "active"（プラグインのみ）


def load_plugins(channels):
    """plugins/*.json を読み、有効なものを METHODS と channels に足す。壊れた仕様は黙って無視せず skipped に残す。"""
    skipped = []
    for k in [k for k in METHODS if k not in CORE_METHODS]:  # 前回の読み込み分を消してから入れ直す
        METHODS.pop(k, None)
        MIN_CHARS.pop(k, None)
        PLUGIN_STATUS.pop(k, None)
    if not PLUGINS.is_dir():
        return skipped
    for p in sorted(PLUGINS.glob("*.json")):
        try:
            spec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            skipped.append((p.name, ["読めない: %s" % e]))
            continue
        errs = validate_spec(spec, channels)
        if not errs and spec["name"] in METHODS:
            errs.append("同じ名前のプラグインが既にある: %s" % spec["name"])
        if errs:
            skipped.append((p.name, errs))
            continue
        METHODS[spec["name"]] = make_http_method(spec)
        MIN_CHARS[spec["name"]] = spec.get("min_chars", DEFAULT_MIN_CHARS)
        PLUGIN_STATUS[spec["name"]] = spec["status"]
        channels[spec["channel"]]["methods"].append(spec["name"])
    return skipped


def load_registry():
    channels = json.loads(REGISTRY.read_text(encoding="utf-8"))["channels"]
    for name, errs in load_plugins(channels):
        sys.stderr.write("プラグイン %s を読み飛ばした: %s\n" % (name, "; ".join(errs)))
    return channels


def classify(url, channels):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for name, ch in channels.items():
        if re.search(ch["match"], host):
            return name
    raise FetchError("どのチャンネルにも当たらない: %s" % url)


def run_method(name, url):
    """手段を1つ実行し、中身の検査まで通ったものだけ返す。失敗は FetchError。"""
    data = METHODS[name](url)
    if len((data.get("text") or "").strip()) < MIN_CHARS.get(name, DEFAULT_MIN_CHARS):
        raise FetchError("本文が短すぎる／空")
    return data


# ---------- 健康記録と並べ替え ----------
def _now():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def log_health(kind, channel, method, ok, ms, error=""):
    # URL は記録しない（PC で何を読んだかを残さない）
    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    with HEALTH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now(), "env": where_am_i(), "kind": kind, "channel": channel, "method": method,
                            "ok": ok, "ms": ms, "error": error}, ensure_ascii=False) + "\n")


def _recent(method):
    if not HEALTH.exists():
        return []
    here = place_class(where_am_i())
    rows = []
    for l in HEALTH.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(l)
        except ValueError:
            continue  # 途中で切れた行は読み飛ばす
        # 場所の無い古い行は PC の記録（2026-10-30 までの記録は PC だけ）
        if r.get("method") == method and place_class(r.get("env", "pc")) == here:
            rows.append(r["ok"])
    return rows[-DEMOTE_AFTER:]


def usable_here(channel_cfg, method):
    """sources.json の where（手段 → ["pc"] / ["cloud"]）。書いていない手段はどこでも使う。"""
    where = channel_cfg.get("where", {}).get(method)
    return where is None or place_class(where_am_i()) in where


def ordered_methods(channel_cfg):
    """この場所で使う手段だけを登録順に並べ、直近 DEMOTE_AFTER 回すべて失敗した手段だけ最後尾へ。"""
    ok, demoted = [], []
    for m in channel_cfg["methods"]:
        if not usable_here(channel_cfg, m):
            continue
        r = _recent(m)
        (demoted if len(r) == DEMOTE_AFTER and not any(r) else ok).append(m)
    return ok + demoted


# ---------- コマンド ----------
def fetch(url):
    channels = load_registry()
    try:
        ch = classify(url, channels)
    except FetchError as e:
        return {"ok": False, "url": url, "error": str(e), "tried": []}
    tried = []
    for m in ordered_methods(channels[ch]):
        t0 = time.time()
        try:
            data = run_method(m, url)
        except Unavailable as e:
            tried.append({"method": m, "ok": False, "error": str(e), "unavailable": True})
            continue
        except FetchError as e:
            ms = int((time.time() - t0) * 1000)
            log_health("fetch", ch, m, False, ms, str(e))
            tried.append({"method": m, "ok": False, "error": str(e)})
            continue
        ms = int((time.time() - t0) * 1000)
        log_health("fetch", ch, m, True, ms)
        tried.append({"method": m, "ok": True})
        return {"ok": True, "url": url, "channel": ch, "method": m, "tried": tried, "data": data}
    return {"ok": False, "url": url, "channel": ch, "tried": tried,
            "error": "このチャンネルの手段がすべて失敗。sources.json に別の手段を足すか、人が調べる"}


def canary():
    channels = load_registry()
    report = {"checked_at": _now(), "env": where_am_i(), "channels": {}}
    for ch, cfg in channels.items():
        res = []
        for m in cfg["methods"]:  # 並べ替えず登録順に全部測る（壊れた手段も毎回測り続け、復活を拾う）
            if not usable_here(cfg, m):
                res.append({"method": m, "ok": None, "ms": 0, "error": "この場所では使わない（where）"})
                continue
            t0 = time.time()
            try:
                data = run_method(m, cfg["canary"]["url"])
                expect = cfg["canary"].get("expect_by_method", {}).get(m, cfg["canary"]["expect"])
                if expect.lower() not in (data["text"] or "").lower():
                    raise FetchError("取れたが期待する文字列が無い（%s）" % expect)
                ok, err = True, ""
            except Unavailable as e:
                res.append({"method": m, "ok": None, "ms": 0, "error": str(e)})
                continue
            except FetchError as e:
                ok, err = False, str(e)
            ms = int((time.time() - t0) * 1000)
            log_health("canary", ch, m, ok, ms, err)
            res.append({"method": m, "ok": ok, "ms": ms, "error": err})
        measured = [r for r in res if r["ok"] is not None]  # 使わない・使えない手段は数えない
        n_ok = sum(r["ok"] for r in measured)
        level = "ok" if n_ok == len(measured) else ("degraded" if n_ok else "down")
        report["channels"][ch] = {"level": level, "methods": res}
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def render(report):
    mark = {"ok": "OK      ", "degraded": "一部だめ", "down": "全滅    "}
    lines = ["実測日時: %s（場所: %s）" % (report["checked_at"], report.get("env", "pc"))]
    for ch, c in report["channels"].items():
        lines.append("[%s] %s" % (mark[c["level"]], ch))
        for r in c["methods"]:
            mark_ok = {True: "○", False: "×", None: "－"}[r["ok"]]
            lines.append("    %s %-14s %5dms %s%s" % (mark_ok, r["method"], r["ms"], r["error"],
                                                     "（試用中）" if PLUGIN_STATUS.get(r["method"]) == "trial" else ""))
    return "\n".join(lines)


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if len(argv) >= 3 and argv[1] == "fetch":
        out = fetch(argv[2])
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out["ok"] else 1
    if len(argv) >= 2 and argv[1] == "canary":
        rep = canary()
        print(render(rep))
        levels = {c["level"] for c in rep["channels"].values()}
        return 2 if "down" in levels else (1 if "degraded" in levels else 0)
    if len(argv) >= 2 and argv[1] == "status":
        if not STATUS.exists():
            print("まだ canary を実行していない")
            return 1
        print(render(json.loads(STATUS.read_text(encoding="utf-8"))))
        return 0
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
