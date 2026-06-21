#!/usr/bin/env python3
# fetch_content.py — 保存URL → 中身（タイトル/本文/投稿者/日付）を取る統一モジュール
#
# 設計（capture-pipeline 機能A）: ドメインで振り分ける1モジュール。
#   x.com / twitter.com → syndication endpoint（無料・無認証）
#   instagram.com       → og:meta を facebookexternalhit UA で取得（無料・無認証）
#   threads.com/net     → oEmbed（1次）→ og:meta/facebookexternalhit（2次）
#   それ以外（ニュース等）→ 汎用Webリーダー（og/meta + 本文テキスト抽出）
# 依存は標準ライブラリのみ（urllib）。クラウドでも追加インストール不要。
# 各取得は失敗しても例外で落とさず ok=False を返す（完走優先）。
#
# 使い方:
#   python3 fetch_content.py <url> [<url> ...]   # 指定URLを取得してJSON表示
#   python3 fetch_content.py                     # 内蔵テストURLで動作確認

import sys, re, json, html, string, urllib.request, urllib.error, urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"


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
    for attr in ("property", "name"):
        m = re.search(r'<meta\s+%s=["\']%s["\'\]\s+content=["\'](.*?)["\'\]\s*/?>' % (attr, re.escape(prop)),
                      htmltext, re.I | re.S)
        if m:
            return html.unescape(m.group(1)).strip()
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
    media = [md.get("media_url_https", "") for md in d.get("mediaDetails", []) if md.get("media_url_https")]
    return {"ok": True, "type": "x", "url": url,
            "title": re.sub(r"\s+", " ", d.get("text", "") or "").strip()[:80],
            "text": d.get("text", ""),
            "author": u.get("name", ""), "handle": u.get("screen_name", ""),
            "date": (d.get("created_at", "") or "")[:10],
            "likes": d.get("favorite_count"), "media": media,
            "cover": media[0] if media else ""}


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
    mc = re.search(r':\s*"(.*)"\'\s*$', og_title)
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
            "note": "og:descriptionは長文辝断あり。フル要時はoEmbedへ昇格"}


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
                "author": author_name, "handle": handle, "cover": ""}

    # 2次: og:meta（facebookexternalhit UA）
    st, txt = _get(url, CRAWLER_UA)
    if txt:
        og_title = _meta(txt, "og:title")
        og_desc  = _meta(txt, "og:description")
        og_img   = _meta(txt, "og:image")
        if og_title or og_desc:
            return {"ok": True, "type": "threads", "url": url,
                    "title": (og_title or "")[:80],
                    "text": og_desc or og_title,
                    "author": author_name, "handle": handle,
                    "cover": og_img or "", "note": "og:meta fallback"}

    return {"ok": False, "type": "threads", "reason": "oEmbed & og:meta 失敗", "url": url}


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
            "snippet": body[:1500]}


# ---------- ディスパッチャ ----------
def fetch_content(url):
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if host.endswith("x.com") or host.endswith("twitter.com"):
        return fetch_x(url)
    if host.endswith("instagram.com"):
        return fetch_instagram(url)
    if host.endswith("threads.com") or host.endswith("threads.net"):
        return fetch_threads(url)
    return fetch_web(url)


if __name__ == "__main__":
    urls = sys.argv[1:] or [
        "https://x.com/obsidianotaku/status/2067951785298522305",
        "https://www.instagram.com/p/DZj9G-hEiZW/",
        "https://www.threads.com/@nft_web3_reo/post/DZzHeryknsF",
        "https://en.wikipedia.org/wiki/Curator",
    ]
    for u in urls:
        print("\n==== %s ====" % u)
        print(json.dumps(fetch_content(u), ensure_ascii=False, indent=2))
