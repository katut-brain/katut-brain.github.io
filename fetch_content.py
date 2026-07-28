#!/usr/bin/env python3
# fetch_content.py — 保存URL → 中身（タイトル/本文/投稿者/日付）を取る統一モジュール
#
# 設計（capture-pipeline 機能A）: ドメインで振り分ける1モジュール。
#   x.com / twitter.com → syndication endpoint（無料・無認証）
#   instagram.com       → og:meta を facebookexternalhit UA で取得（無料・無認証）
#   それ以外（ニュース等）→ 汎用Webリーダー（og/meta + 本文テキスト抽出）
# 依存は標準ライブラリのみ（urllib）＋オプション: youtube-transcript-api, google-genai
# （いずれもrequirements.txtに記載・クラウドRoutineではcloud_routine_prompt.mdの手順内で明示pip installする）。
# 未インストール/APIキー未設定でも他の取得は正常動作する（graceful degradation）。
# 各取得は失敗しても例外で落とさず ok=False を返す（完走優先）。
#
# 使い方:
#   python3 fetch_content.py <url> [<url> ...]   # 指定URLを取得してJSON表示
#   python3 fetch_content.py                     # 内蔵テストURLで動作確認

import sys, os, re, json, html, string, tempfile, time, urllib.request, urllib.error, urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# youtube-transcript-api（オプション依存）
try:
    from youtube_transcript_api import YouTubeTranscriptApi as _YTApi
    _YT_TRANSCRIPT_AVAILABLE = True
except ImportError:
    _YT_TRANSCRIPT_AVAILABLE = False

# google-genai（オプション依存。X動画の音声+映像理解に使う。GEMINI_API_KEY環境変数も必要）
try:
    from google import genai as _genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False
_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

# t.co 展開でスキップするドメイン（同種SNSへのリンクは記事本文ではない）
_TCO_SKIP_HOSTS = ("x.com", "twitter.com", "instagram.com", "youtube.com", "youtu.be")


def _get(url, ua, timeout=12, max_bytes=600_000):
    """HTTP GET。(status, text) を返す。失敗は (None, "") 。"""
    # Accept-Language は en 固定（IG/X のog/ラッパー文を英語に揃えてパースを安定させる。
    # 投稿本文・キャプションは投稿者の言語のまま返るので日本語は失われない）。
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(max_bytes)
            return r.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return None, ""


def _meta(htmltext, prop):
    """<meta property|name="prop" content="..."> を拾って unescape。"""
    m = re.search(r'<meta\s+(?:property|name)=["\']%s["\']\s+content=["\'](.*?)["\']' % re.escape(prop),
                  htmltext, re.I | re.S)
    if m:
        return html.unescape(m.group(1)).strip()
    return ""


def _expand_tco(url):
    """t.co URL を展開して最終 URL を返す。失敗は '' を返す。
    t.co はJSリダイレクトを使うため、HTMLからURLを抽出する。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read(2000).decode("utf-8", "replace")
            # JS: location.replace("https://...")
            m = re.search(r'location\.replace\("(https?://[^"]+)"', body)
            if m:
                return m.group(1)
            # noscript: <META http-equiv="refresh" content="0;URL=https://...">
            m2 = re.search(r'content=["\']0;URL=(https?://[^"\']+)["\']', body, re.I)
            if m2:
                return m2.group(1)
            # HTTPリダイレクトで追えた場合
            if r.url != url:
                return r.url
        return ""
    except Exception:
        return ""


# ---------- X (Twitter) ----------
def _tweet_token(tid):
    digs = string.digits + string.ascii_lowercase
    x = (int(tid) / 1e15) * 3.141592653589793
    def b36(n):
        n = int(n)
        if n == 0: return "0"
        s = ""
        while n:
            n, r = divmod(n, 36); s = digs[r] + s
        return s
    frac, fs = x - int(x), ""
    for _ in range(12):
        frac *= 36; d = int(frac); fs += digs[d]; frac -= d
    return (b36(int(x)) + fs).replace("0", "").replace(".", "")


# X Articleへのリンクは `/i/article/<id>`(内部ID形式)と `/<username>/article/<id>`(著者名形式)の
# 両方が実在する(実測: 後者も200を返す)。プレフィックス問わず "/article/<数字>" にマッチさせるが、
# これはx.com/twitter.comドメイン限定で判定すること(ドメイン無制約だと一般ニュースサイトの
# よくあるURL構造 "/article/<数字>" まで誤ってX Article扱いしてしまう回帰があったため=要注意)。
_ARTICLE_PATH_RE = re.compile(r"/article/\d+")
_X_ARTICLE_HOSTS = ("x.com", "twitter.com")


def _is_x_article_url(url):
    """展開後のURLがX Article(x.com系ドメイン配下の/article/<id>)かどうかを判定する。"""
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if not any(host.endswith(h) for h in _X_ARTICLE_HOSTS):
        return False
    return bool(_ARTICLE_PATH_RE.search(url))


def _fetch_x_article(tid):
    """X「Article」(長文記事)の本文を FxEmbed(旧FixTweet)の公開API経由で取得。無料・無認証。
    tid には Article内部ID(x.com/i/article/<ID>)ではなく、その記事を貼った**告知ツイート自体のstatus ID**
    を渡すこと(実測で両者は別の数値。Article内部IDをそのまま渡すと404になる)。
    api.fxtwitter.com はレート制限が緩く(1000req/min/IP)無認証で使えるが、非公式の埋め込み修正サービスの
    副産物APIであり、X側の仕様変更で予告なく壊れる可能性がある点は運用上留意する。
    """
    st, txt = _get("https://api.fxtwitter.com/i/status/%s" % tid, BROWSER_UA)
    if st != 200 or not txt.strip():
        return None
    try:
        d = json.loads(txt)
    except Exception:
        return None
    tweet = d.get("tweet") or d.get("status")
    article = (tweet or {}).get("article")
    if not article:
        return None
    lines = []
    for b in article.get("content", {}).get("blocks", []):
        t = (b.get("text") or "").strip()
        if not t:
            continue
        lines.append(("## " + t) if str(b.get("type", "")).startswith("header") else t)
    body = "\n".join(lines)
    if not body:
        return None
    return {"title": article.get("title", ""), "body": body[:20000]}


_X_VIDEO_MAX_BYTES = 100_000_000  # 100MB上限。Gemini Files APIは2GBまで対応しているが、
# 毎朝の無人ジョブの処理時間を抑えるための実務的な上限(実測: 51MBの動画が存在するため
# 従来の30MB上限は不十分だった)。上限超過・ダウンロード途中切断は理解を諦めて""を返す
# (不完全な動画をGeminiに渡すと、切れた部分だけを根拠にした要約が生成されうるため=禁止)。


def _fetch_x_video_understanding(video_url, max_chars=4000):
    """X投稿に添付された動画(mp4)をダウンロードし、Gemini API(無料枠)で
    映像+音声の内容を理解して日本語要約を返す。
    GEMINI_API_KEY未設定/google-genai未インストール/失敗時は "" を返す(graceful degradation。
    その場合カード生成はタイトル・キャプションのみで続行する＝完走優先)。
    """
    if not _GEMINI_AVAILABLE or not _GEMINI_API_KEY:
        return ""
    tmp_path = None
    try:
        req = urllib.request.Request(video_url, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            content_length = r.headers.get("Content-Length")
            data = r.read(_X_VIDEO_MAX_BYTES + 1)
        if len(data) > _X_VIDEO_MAX_BYTES:
            return ""  # 上限超過。不完全データをGeminiに渡さない
        if content_length is not None:
            try:
                if int(content_length) != len(data):
                    return ""  # 途中で切断された(接続断等)。不完全データをGeminiに渡さない
            except ValueError:
                pass
        fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
        with os.fdopen(fd, "wb") as f:
            f.write(data)

        client = _genai.Client(api_key=_GEMINI_API_KEY)
        uploaded = client.files.upload(file=tmp_path)
        waited = 0
        while getattr(uploaded.state, "name", "") == "PROCESSING" and waited < 100:
            time.sleep(4)
            waited += 4
            uploaded = client.files.get(name=uploaded.name)
        if getattr(uploaded.state, "name", "") != "ACTIVE":
            # 長尺動画はGemini側の処理が数分かかる場合がある。無人ジョブの時間予算を優先し、
            # 100秒待って終わらなければ理解を諦める(missing: video_contentで正直に記録)。
            return ""
        resp = client.models.generate_content(
            model="gemini-flash-latest",
            contents=[uploaded, "この動画に映っている内容と、話されている・聞こえる音声の内容を、日本語で具体的に要約して。"],
        )
        return (resp.text or "").strip()[:max_chars]
    except Exception:
        return ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def fetch_x(url):
    m = re.search(r"/status/(\d+)", url)
    if not m:
        return {"ok": False, "type": "x", "reason": "tweet id をURLから抽出できない", "url": url}
    tid = m.group(1)
    api = "https://cdn.syndication.twimg.com/tweet-result?id=%s&token=%s&lang=en" % (tid, _tweet_token(tid))
    st, txt = _get(api, BROWSER_UA)
    if st != 200 or not txt.strip():
        return {"ok": False, "type": "x", "reason": "syndication 空/失敗 (status=%s)" % st,
                "url": url, "fallback": "twitterapi.io"}
    try:
        d = json.loads(txt)
    except Exception:
        return {"ok": False, "type": "x", "reason": "JSON parse 失敗", "url": url}
    u = d.get("user", {})
    media_details = d.get("mediaDetails", [])
    media = [md.get("media_url_https", "") for md in media_details if md.get("media_url_https")]
    tweet_text = d.get("text", "")

    # 画像: 高解像度バリアントを明示指定(クエリなしだと画像により縮小版が返ることがある実測済み)。
    # 既存クエリに format/name が既にあれば置換する(単純追記だと重複パラメータになりCDN側の
    # 採用がどちらか不定になるため)。
    photos = []
    for md in media_details:
        base = md.get("media_url_https", "")
        if md.get("type") == "photo" and base:
            parts = urllib.parse.urlsplit(base)
            q = dict(urllib.parse.parse_qsl(parts.query))
            q["format"] = "jpg"
            q["name"] = "large"
            photos.append(urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q))))

    # 動画: 最初の1本のみ対象(複数動画・複数ツイート跨ぎは今回対象外)。mp4バリアントのうち
    # 最小bitrateを選ぶ(Whisper/Gemini等の後段処理には解像度は不要・ダウンロード量を最小化するため)
    video_understanding = ""
    has_video = False
    for md in media_details:
        if md.get("type") != "video":
            continue
        has_video = True
        variants = md.get("video_info", {}).get("variants", [])
        mp4s = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
        if mp4s:
            best = min(mp4s, key=lambda v: v.get("bitrate", 0))
            video_understanding = _fetch_x_video_understanding(best["url"])
        break

    # t.co 展開: 本文テキストから最初の t.co リンクを抽出して記事本文(またはX Article本文)を取得
    article_text = ""
    article_title = ""
    depth = "full"
    missing = []
    tco_matches = re.findall(r"https://t\.co/\S+", tweet_text)
    if tco_matches:
        try:
            # 全リンクを展開し、X Articleへのリンクが(先頭以外にあっても)無いか優先的に探す
            expanded_list = [_expand_tco(u) for u in tco_matches]
            article_expanded = next((e for e in expanded_list if e and _is_x_article_url(e)), None)
            if article_expanded:
                # X Article: 元ツイートのtid(Article内部IDではない)でFxEmbed経由取得
                art = _fetch_x_article(tid)
                if art:
                    article_title = art["title"]
                    article_text = art["body"]
            else:
                # X Articleでなければ、従来通り先頭リンクの外部記事本文を試みる
                expanded = expanded_list[0]
                if expanded:
                    exp_host = re.sub(r"^https?://", "", expanded).split("/")[0].lower()
                    is_skip = any(exp_host.endswith(skip) for skip in _TCO_SKIP_HOSTS)
                    if not is_skip:
                        web_result = fetch_web(expanded)
                        if web_result.get("ok") and web_result.get("text", ""):
                            article_text = web_result["text"]
        except Exception:
            pass

    if tco_matches and not article_text:
        depth = "partial"
        missing = ["article_body"]

    text = tweet_text
    if article_text:
        label = ("X Article: " + article_title) if article_title else "記事本文"
        text = tweet_text + "\n\n" + label + ":\n" + article_text

    if video_understanding:
        text = text + "\n\n動画の内容: " + video_understanding
    elif has_video:
        # 動画はあるが理解できなかった(APIキー未設定/ダウンロード失敗/Gemini側エラー等)。
        # タイトル・キャプションのみで完走させる(完走優先)が、欠損は正直に記録する。
        if depth == "full":
            depth = "partial"
        missing = missing + ["video_content"]

    return {"ok": True, "type": "x", "url": url,
            "title": (article_title or re.sub(r"\s+", " ", tweet_text or "").strip())[:80],
            "text": text,
            "author": u.get("name", ""), "handle": u.get("screen_name", ""),
            "date": (d.get("created_at", "") or "")[:10],
            "likes": d.get("favorite_count"), "media": media,
            "photos": photos,
            "cover": media[0] if media else "",
            "depth": depth, "missing": missing}


# ---------- Instagram ----------
def fetch_instagram(url):
    st, txt = _get(url, CRAWLER_UA)
    if not txt:
        return {"ok": False, "type": "instagram", "reason": "取得失敗 (status=%s)" % st,
                "url": url, "fallback": "oEmbed(Metaアプリ) or Apify"}
    og_title = _meta(txt, "og:title")
    og_desc = _meta(txt, "og:description")
    og_img = _meta(txt, "og:image")
    if not og_title and not og_desc:
        return {"ok": False, "type": "instagram", "reason": "og:meta無し（JS殻/ログイン壁の可能性）", "url": url}
    # og_title 例: 'Kalypso on Instagram: "Sites for designers"'
    cap = ""
    mc = re.search(r':\s*"(.*)"\s*$', og_title)
    if mc: cap = mc.group(1)
    # 表示名 = og_title の "… on/- /• Instagram" より前（区切りはロケールで揺れる）
    name = re.split(r"\s+(?:on|[-•|·])\s*Instagram", og_title)[0].strip() if og_title else ""
    # og_desc 例: '1,745 likes, 6 comments - kalypsodesigns on June 14, 2026: "Sites for designers". '
    handle = ""
    mh = re.search(r"-\s*([A-Za-z0-9_.]+)\s+on\s", og_desc)
    if mh: handle = mh.group(1)
    likes = comments = None
    ml = re.search(r"([\d,]+)\s+likes?,\s*([\d,]+)\s+comments?", og_desc)
    if ml:
        likes = int(ml.group(1).replace(",", "")); comments = int(ml.group(2).replace(",", ""))
    date = ""
    md = re.search(r"\son\s+([A-Z][a-z]+\s+\d+,\s+\d{4})", og_desc)
    if md: date = md.group(1)
    if not cap:  # フォールバック: og_desc のコロン以降
        mc2 = re.search(r':\s*"(.*)"', og_desc)
        cap = mc2.group(1) if mc2 else og_desc
    return {"ok": True, "type": "instagram", "url": url,
            "title": (cap or og_title)[:80], "text": cap,
            "author": name, "handle": handle, "date": date,
            "likes": likes, "comments": comments, "cover": og_img,
            "note": "og:descriptionは長文截断あり。フル要時はoEmbedへ昇格",
            "depth": "shallow", "missing": ["visual_content", "audio_content"]}


# ---------- Threads ----------
def fetch_threads(url):
    """1次: Threads oEmbed（無認証・無料）→ 2次: og:meta（facebookexternalhit）"""
    # 1次: oEmbed
    oe_url = "https://www.threads.net/oembed/?url=" + urllib.parse.quote(url, safe="")
    st, txt = _get(oe_url, BROWSER_UA)
    post_text = ""
    author_name = ""
    if st == 200 and txt.strip():
        try:
            d = json.loads(txt)
            author_name = d.get("author_name", "")
            post_html = d.get("html", "")
            bq = re.search(r"<blockquote[^>]*>(.*?)</blockquote>", post_html, re.I | re.S)
            if bq:
                raw = bq.group(1)
                # 末尾の attribution 行（"— @handle"）を除去
                raw = re.sub(r"<p[^>]*>[^<]*—[^<]*</p>", "", raw, flags=re.I | re.S)
                raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
                raw = re.sub(r"<[^>]+>", "", raw)
                post_text = html.unescape(re.sub(r"[ \t]+", " ", raw)).strip()
        except Exception:
            pass

    mh = re.search(r"threads\.(?:com|net)/@([^/?&#]+)", url, re.I)
    handle = mh.group(1) if mh else ""

    if post_text:
        return {"ok": True, "type": "threads", "url": url,
                "title": post_text[:80], "text": post_text,
                "author": author_name, "handle": handle, "cover": "",
                "depth": "shallow", "missing": ["visual_content", "audio_content"]}

    # 2次: og:meta（facebookexternalhit UA）
    st, txt = _get(url, CRAWLER_UA)
    if txt:
        og_title = _meta(txt, "og:title")
        og_desc  = _meta(txt, "og:description")
        og_img   = _meta(txt, "og:image")
        if og_title or og_desc:
            # og_title 例: "レオ｜AI使って賢く時短| (@nft_web3_reo) on Threads"
            author_from_title = re.sub(r'\s*\(@[^)]+\)\s*(?:on\s+Threads)?\s*$', '', og_title or "").strip()
            post_text = og_desc or og_title
            return {"ok": True, "type": "threads", "url": url,
                    "title": post_text[:80],
                    "text": post_text,
                    "author": author_from_title or author_name, "handle": handle,
                    "cover": og_img or "", "note": "og:meta fallback",
                    "depth": "shallow", "missing": ["visual_content", "audio_content"]}

    return {"ok": False, "type": "threads", "reason": "oEmbed & og:meta 失敗", "url": url}


# ---------- YouTube ----------
def _yt_video_id(url):
    """YouTube URL から 11文字の video ID を抽出。失敗時は ""。
    watch?v=/youtu.be/shorts/ に加え、live/embed 形式のURLにも対応する。"""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else ""


def _yt_transcript(vid_id, max_chars=50000):
    """字幕(優先 ja→en、無ければ取得できた最初の言語)を取得し先頭max_chars字を返す。失敗時は ""。
    注意: youtube-transcript-api 1.x では `YouTubeTranscriptApi.get_transcript()` クラスメソッドは
    廃止済み。インスタンスの `.fetch()` / `.list()` を使う（旧API呼び出しは黙って例外→空文字化するため要注意）。
    """
    if not _YT_TRANSCRIPT_AVAILABLE or not vid_id:
        return ""
    try:
        api = _YTApi()
        try:
            fetched = api.fetch(vid_id, languages=["ja", "en"])
        except Exception:
            # ja/en 字幕が無い動画: 取得できる最初の言語にフォールバック
            fetched = next(iter(api.list(vid_id))).fetch()
        full = " ".join(seg.text for seg in fetched).replace("\n", " ")
        return full[:max_chars]
    except Exception:
        return ""


def fetch_youtube(url):
    """YouTube oEmbed（無認証・無料）: タイトル・チャンネル名・サムネ取得 + transcript"""
    vid_id = _yt_video_id(url)
    oe_url = "https://www.youtube.com/oembed?url=" + urllib.parse.quote(url, safe="") + "&format=json"
    st, txt = _get(oe_url, BROWSER_UA)
    if st == 200 and txt.strip():
        try:
            d = json.loads(txt)
            title = d.get("title", "")
            transcript = _yt_transcript(vid_id)
            text = (title + "\n\n" + transcript).strip() if transcript else title
            has_transcript = bool(transcript)
            return {"ok": True, "type": "youtube", "url": url,
                    "title": title[:80], "text": text,
                    "author": d.get("author_name", ""), "handle": "",
                    "cover": d.get("thumbnail_url", ""),
                    "has_transcript": has_transcript,
                    "depth": "full" if has_transcript else "shallow",
                    "missing": [] if has_transcript else ["transcript"]}
        except Exception:
            pass
    # フォールバック: og:meta
    st, txt = _get(url, BROWSER_UA)
    if txt:
        og_title = _meta(txt, "og:title")
        og_desc  = _meta(txt, "og:description")
        og_img   = _meta(txt, "og:image")
        if og_title:
            transcript = _yt_transcript(vid_id)
            text_base = og_desc or og_title
            text = (og_title + "\n\n" + transcript).strip() if transcript else text_base
            has_transcript = bool(transcript)
            return {"ok": True, "type": "youtube", "url": url,
                    "title": og_title[:80], "text": text,
                    "author": "", "handle": "", "cover": og_img or "",
                    "has_transcript": has_transcript,
                    "note": "og:meta fallback",
                    "depth": "full" if has_transcript else "shallow",
                    "missing": [] if has_transcript else ["transcript"]}
    return {"ok": False, "type": "youtube", "reason": "oEmbed & og:meta 失敗", "url": url}


# ---------- 汎用Web ----------
def fetch_web(url):
    st, txt = _get(url, BROWSER_UA)
    if not txt:
        return {"ok": False, "type": "web", "reason": "取得失敗 (status=%s)" % st, "url": url}
    title = _meta(txt, "og:title")
    if not title:
        mt = re.search(r"<title[^>]*>(.*?)</title>", txt, re.I | re.S)
        title = html.unescape(mt.group(1)).strip() if mt else ""
    desc = _meta(txt, "og:description") or _meta(txt, "description")
    # 本文テキスト抽出（script/style除去→タグ除去→空白圧縮→先頭1500字）
    body = re.sub(r"(?is)<(script|style|noscript|head|nav|footer|header)[^>]*>.*?</\1>", " ", txt)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(re.sub(r"\s+", " ", body)).strip()
    return {"ok": True, "type": "web", "url": url,
            "title": title, "text": desc or body[:1500],
            "cover": _meta(txt, "og:image"),
            "snippet": body[:1500],
            "depth": "full", "missing": []}


# ---------- ディスパッチャ ----------
def fetch_content(url):
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if host.endswith("x.com") or host.endswith("twitter.com"):
        return fetch_x(url)
    if host.endswith("instagram.com"):
        return fetch_instagram(url)
    if host.endswith("threads.com") or host.endswith("threads.net"):
        return fetch_threads(url)
    if host.endswith("youtube.com") or host.endswith("youtu.be"):
        return fetch_youtube(url)
    return fetch_web(url)


if __name__ == "__main__":
    urls = sys.argv[1:] or [
        "https://x.com/obsidianotaku/status/2067951785298522305",
        "https://www.instagram.com/p/DZj9G-hEiZW/",
        "https://www.threads.com/@nft_web3_reo/post/DZzHeryknsF",
        "https://youtube.com/shorts/U0wWH9GQ7ng",
        "https://en.wikipedia.org/wiki/Curator",
    ]
    for u in urls:
        print("\n==== %s ====" % u)
        print(json.dumps(fetch_content(u), ensure_ascii=False, indent=2))
