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

import sys, os, re, json, html, string, tempfile, time, threading, ipaddress, socket, http.client
import urllib.request, urllib.error, urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# youtube-transcript-api（オプション依存。requestsはその推移依存として同時にインストールされる）
try:
    import requests as _requests
    from youtube_transcript_api import YouTubeTranscriptApi as _YTApi
    _YT_TRANSCRIPT_AVAILABLE = True

    class _TimeoutSession(_requests.Session):
        """youtube-transcript-apiの内部HTTP通信に既定タイムアウトを強制するSession。
        素のrequests.Sessionはタイムアウト未指定だと応答がない限り無期限にハングしうり、
        無人ルーティーンが1件のリンク取得で丸ごと止まってしまう(Codex敵対的レビューで指摘)。"""
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", 15)
            return super().request(*args, **kwargs)
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


def _host_of(url):
    """URLからホスト名だけを取り出す(クエリ・パス・ポート番号・userinfoは除く、小文字化)。
    urllib.parse.urlsplit().hostname を使うこと。素朴な文字列分割(':'→'@'の順で区切る等)だと
    "https://good.com:80@evil.com/" のようなuserinfo構文で実際の接続先ホスト(evil.com)を
    誤判定し、SSRF対策のホスト許可リストを回避できてしまう(Codex敵対的レビューで指摘)。
    """
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return ""
    return (host or "").lower()


def _host_matches(host, domain):
    """hostがdomain自身、またはそのサブドメインかを正しく判定する。
    単純な str.endswith(domain) だと "netflix.com".endswith("x.com") が True になる等、
    ドメイン境界を無視した誤判定(Codex敵対的レビューで指摘)が起きるため使わないこと。"""
    return host == domain or host.endswith("." + domain)


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
    host = _host_of(url)
    if not any(_host_matches(host, h) for h in _X_ARTICLE_HOSTS):
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


_VIDEO_MAX_BYTES = 100_000_000  # 100MB上限。Gemini Files APIは2GBまで対応しているが、
# 毎朝の無人ジョブの処理時間を抑えるための実務的な上限(実測: 51MBの動画が存在するため
# 従来の30MB上限は不十分だった)。上限超過・ダウンロード途中切断は理解を諦めて""を返す
# (不完全な動画をGeminiに渡すと、切れた部分だけを根拠にした要約が生成されうるため=禁止)。


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """リダイレクト先ホストを許可リストに限定するHTTPRedirectHandler(SSRF対策)。
    信頼できない第三者(vxinstagram.com等)が返したURLを無条件にリダイレクト追従すると、
    任意ホストへの意図しないアクセス・データ持ち出しに悪用されうる(Codex敵対的レビューで指摘)。
    """
    def __init__(self, allowed_hosts):
        self._allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = _host_of(newurl)
        if not any(_host_matches(host, h) for h in self._allowed_hosts):
            raise urllib.error.URLError("redirect to disallowed host: %s" % host)
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


def _gemini_video_understanding(video_url, max_chars=4000, allowed_hosts=None):
    """動画(mp4)をダウンロードし、Gemini API(無料枠)で映像+音声の内容を理解して
    日本語要約を返す。X動画・Instagram Reels等、動画の実体URLが手に入るケースで共用する。
    GEMINI_API_KEY未設定/google-genai未インストール/失敗時は "" を返す(graceful degradation。
    その場合カード生成はタイトル・キャプションのみで続行する＝完走優先)。

    allowed_hosts: 指定した場合、開始URL・リダイレクト先ともにこのホスト(サブドメイン含む)
    以外は拒否する(SSRF対策)。信頼できない第三者サービス経由で得たURL(Instagram Reels等)を
    渡す場合は必ず指定すること。Xのように取得元が最初から信頼できるドメインの場合は省略可。
    """
    if not _GEMINI_AVAILABLE or not _GEMINI_API_KEY:
        return ""
    if allowed_hosts is not None and not any(
        _host_matches(_host_of(video_url), h) for h in allowed_hosts
    ):
        return ""  # 開始URL自体が想定外のホスト。安全側に倒して理解を諦める
    tmp_path = None
    try:
        req = urllib.request.Request(video_url, headers={"User-Agent": BROWSER_UA})
        if allowed_hosts is not None:
            opener = urllib.request.build_opener(_AllowlistRedirectHandler(allowed_hosts))
            cm = opener.open(req, timeout=60)
        else:
            cm = urllib.request.urlopen(req, timeout=60)
        with cm as r:
            content_type = r.headers.get("Content-Type", "")
            if not content_type.startswith("video/"):
                return ""  # 想定外のContent-Type。動画以外のデータをGeminiに渡さない
            content_length = r.headers.get("Content-Length")
            data = r.read(_VIDEO_MAX_BYTES + 1)
        if len(data) > _VIDEO_MAX_BYTES:
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


def _fetch_x_embedded_link_inner(expanded):
    """_fetch_x_embedded_link() の実処理本体(タイムアウトガードの内側で動く)。"""
    host = _host_of(expanded)
    try:
        if _host_matches(host, "youtube.com") or _host_matches(host, "youtu.be"):
            r = fetch_youtube(expanded)
            label = "リンク先動画の内容"
        elif _host_matches(host, "instagram.com"):
            r = fetch_instagram(expanded)
            label = "リンク先Instagram投稿"
        elif _host_matches(host, "threads.com") or _host_matches(host, "threads.net"):
            r = fetch_threads(expanded)
            label = "リンク先Threads投稿"
        elif _host_matches(host, "x.com") or _host_matches(host, "twitter.com"):
            # 引用ツイート等。無限再帰防止のためfollow_links=Falseで1階層のみ辿る。
            # understand_video=False: 目的はテキスト本文の補完であり、動画Gemini理解
            # (最大100秒超)まで行うと外側45秒タイムアウトでテキストごと失われるため省略する。
            r = fetch_x(expanded, follow_links=False, understand_video=False)
            label = "引用元投稿の内容"
        else:
            r = fetch_web(expanded)
            label = "記事本文"
    except Exception:
        return None
    if r and r.get("ok") and r.get("text"):
        return {"label": label, "title": r.get("title", ""), "text": r["text"]}
    return None


def _fetch_x_embedded_link(expanded, timeout=45):
    """t.co展開後のURL(X Articleではない)を、ホストに応じた専用フェッチャーに振り分ける。
    YouTube/Instagram/Threads/X(引用ツイート等)はそれぞれの専用取得関数に委譲し、
    それ以外は従来通り汎用Webリーダー(fetch_web)を使う。
    戻り値: {"label":str, "title":str, "text":str} または None(取得失敗/中身が空/timeout)。

    デーモンスレッド+timeout付きjoinで一連の処理(oEmbed取得・字幕取得・OGメタ取得等の
    複数ステップ全て)を1つの壁時計上限で打ち切る(個々の_get()呼び出しのソケットタイムアウト
    だけでは、応答がちびちび届き続けるサーバー相手に総所要時間を保証できないため。
    Codex敵対的レビューで指摘)。
    タイムアウトしたスレッドは終了させず放置する(join(timeout=)はキャンセルではなく
    待機の打ち切りのため)。daemon=Trueなのでプロセス終了はブロックしないが、リンク先が
    悪意的に応答を止め続けた場合は該当プロセスの生存中はスレッドが残る。1回のRoutine実行は
    最大20件程度のURLをバッチ処理して都度プロセスが終了する運用のため、蓄積は当該バッチの
    件数分に限定される(サーバープロセスとして常駐し続ける使い方はしていない)。
    """
    result = {}

    def _run():
        r = _fetch_x_embedded_link_inner(expanded)
        if r:
            result["value"] = r

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return result.get("value")


def fetch_x(url, follow_links=True, understand_video=True):
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
    # 最小bitrateを選ぶ(Whisper/Gemini等の後段処理には解像度は不要・ダウンロード量を最小化するため)。
    # understand_video=False(埋め込みリンク経由の引用ツイート取得等)の場合はGemini理解を
    # スキップする: 動画理解(ダウンロード+Gemini処理で最大100秒超)は、埋め込みリンク全体に
    # 掛かっている45秒の壁時計タイムアウト(_fetch_x_embedded_link)を軽く超え、本来数秒で
    # 済むはずの引用元テキスト本文まで巻き添えで失われるバグを実機で確認したため
    # (output-verifierが発見)。
    video_understanding = ""
    has_video = False
    for md in media_details:
        if md.get("type") != "video":
            continue
        has_video = True
        if not understand_video:
            break
        variants = md.get("video_info", {}).get("variants", [])
        mp4s = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
        if mp4s:
            best = min(mp4s, key=lambda v: v.get("bitrate", 0))
            video_understanding = _gemini_video_understanding(best["url"])
        break

    # t.co 展開: 本文テキストのリンク先(X Article/YouTube/Instagram/Threads/引用ツイート/一般記事)を取得
    article_text = ""
    article_title = ""
    article_label = "記事本文"
    depth = "full"
    missing = []
    tco_matches = re.findall(r"https://t\.co/\S+", tweet_text) if follow_links else []
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
                    article_label = "X Article: " + article_title if article_title else "X Article"
            else:
                # X Articleでなければ、先頭リンクをホストに応じた専用フェッチャーで取得。
                # (リンク先のtitleはtext本文には使うが、投稿自体のtitleはツイート本文のまま
                # 保つ＝ツイート本文が空でリンクだけの投稿以外はツイート側の声を優先する)
                expanded = expanded_list[0]
                if expanded:
                    linked = _fetch_x_embedded_link(expanded)
                    if linked:
                        article_text = linked["text"]
                        article_label = (linked["label"] + ": " + linked["title"]) if linked["title"] else linked["label"]
        except Exception:
            pass

    if tco_matches and not article_text:
        depth = "partial"
        missing = ["article_body"]

    text = tweet_text
    if article_text:
        text = tweet_text + "\n\n" + article_label + ":\n" + article_text

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


# vxinstagram.com経由で得たURLがリダイレクトしてよい先(SSRF対策の許可リスト)。
# 2026-08-01: vxinstagram.com側の仕様変更を確認。新URL(d.vxinstagram.com/offload/<id>/0.mp4)は
# 実機確認の結果、直接配信ではなくcdninstagram.comへ302リダイレクトしている(既存の
# cdninstagram.com許可により安全性への影響はない)。d.vxinstagram.com自体もリダイレクト元
# ホストとして許可リストに含めておく。旧仕様(d.rapidcdn.app経由)のホストも、
# 再度その方式に戻った場合に備えて残す。
_INSTAGRAM_VIDEO_ALLOWED_HOSTS = ("d.vxinstagram.com", "d.rapidcdn.app", "cdninstagram.com", "fbcdn.net")


def _fetch_instagram_reel_video_url(url):
    """Instagram Reelsの動画ファイル実体URLを、vxinstagram.com(Discord/Telegram埋め込み修正用の
    非公式サードパーティサービス、github.com/Lainmode/InstagramEmbed-vxinstagram)経由で取得する。
    無料・無認証。戻り値: 動画URL(str) または None(Reelでない/取得失敗)。

    公式のog:meta・GraphQL・モバイル内部APIはいずれも無認証では動画データを返さないことを
    確認済み(2026-07-28調査)。個人運営の非公式サービスでありXのFxEmbed同様、仕様変更・
    サービス終了のリスクがある点に留意(運用上のリスクとして受容)。

    2026-08-01: vxinstagram.com側の仕様変更を確認・追従。
    - ベースドメイン`vxinstagram.com/reel/<id>/`は404化。`d.vxinstagram.com/reel/<id>/`
      (サブドメイン必須)に変更されていた（実測確認）。
    - 動画URLの取得元も`d.rapidcdn.app/v2?token=...`のトークン付きリダイレクトから、
      レスポンスHTMLの`og:video`/`og:video:secure_url`メタタグへの記載に変更（実機確認では
      `d.vxinstagram.com/offload/<id>/0.mp4`からcdninstagram.comへ302リダイレクトされる）。
      抽出は既存の`_meta()`ヘルパー（クオート種別・空白の揺れに頑健）を再利用する
      （output-verifierの指摘により、当初の直書き正規表現から修正・2026-08-01）。
    """
    m = re.search(r"/reel/([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    shortcode = m.group(1)
    st, txt = _get("https://d.vxinstagram.com/reel/%s/" % shortcode, BROWSER_UA)
    if st != 200 or not txt:
        return None
    video_url = _meta(txt, "og:video:secure_url") or _meta(txt, "og:video")
    if not video_url:
        return None
    # 末尾に dl=1 等の添付ダウンロード指定が付くと、CDNが
    # Content-Type: application/octet-stream を返し _gemini_video_understanding() の
    # video/*判定(SSRF対策)に弾かれることがある（旧仕様での実測事例）。念のため除去しておく。
    parts = urllib.parse.urlsplit(video_url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "dl"]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


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

    # Reels動画理解(vxinstagram.com経由でmp4 URL取得→Gemini API)。失敗/対象外ならmissingに正直に記録。
    text = cap
    is_reel = "/reel/" in url
    if is_reel:
        # Reelと分かっているのに動画URL取得やGemini理解が失敗した場合は、通常投稿(画像等)と
        # 区別できるようdepth: partial + missing: video_content で記録する(単なる shallow だと
        # 「動画として一度も試みていない」場合と見分けが付かず、後からの再取得対象を絞り込めない。
        # Codex敵対的レビューで指摘)。
        depth = "partial"
        missing = ["video_content"]
    else:
        depth = "shallow"
        missing = ["visual_content", "audio_content"]
    video_url = _fetch_instagram_reel_video_url(url)
    if video_url:
        video_understanding = _gemini_video_understanding(
            video_url, allowed_hosts=_INSTAGRAM_VIDEO_ALLOWED_HOSTS
        )
        if video_understanding:
            text = (cap + "\n\n動画の内容: " + video_understanding) if cap else video_understanding
            depth = "full"
            missing = []
        # 理解できなかった場合は is_reel 分岐で既に partial/video_content 設定済み

    return {"ok": True, "type": "instagram", "url": url,
            "title": (cap or og_title)[:80], "text": text,
            "author": name, "handle": handle, "date": date,
            "likes": likes, "comments": comments, "cover": og_img,
            # photos: og:image はIG側の署名付きURLで既に実質フル解像度(実測2160x2880px)。
            # X実装のようなクエリ改変での高解像度化は署名検証に引っかかり403になるため行わない
            # (2026-07-28調査)。Xとのフィールド名を揃え、Routine側の「画像はphotosを見てダウンロード
            # →Readで読む」という指示を使い回せるようにするだけの目的でそのまま入れる。
            "photos": [og_img] if og_img else [],
            "note": "og:descriptionは長文截断あり。フル要時はoEmbedへ昇格",
            "depth": depth, "missing": missing}


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


def _yt_transcript(vid_id, max_chars=50000, timeout=30):
    """字幕(優先 ja→en、無ければ取得できた最初の言語)を取得し先頭max_chars字を返す。失敗時は ""。
    注意: youtube-transcript-api 1.x では `YouTubeTranscriptApi.get_transcript()` クラスメソッドは
    廃止済み。インスタンスの `.fetch()` / `.list()` を使う（旧API呼び出しは黙って例外→空文字化するため要注意）。

    実行はデーモンスレッド+timeout付きjoinで壁時計の総実行時間を打ち切る。requestsの
    timeoutパラメータはソケット単位(接続/読み取りの無通信時間)であり、応答がちびちび
    届き続ける・.fetch()失敗後の.list()フォールバックが積み上がる等のケースでは全体の
    所要時間を保証しない(Codex敵対的レビューで指摘)。無人ルーティーンが1件のリンクで
    無期限にハングしないよう、ここで確実に上限を掛ける。
    """
    if not _YT_TRANSCRIPT_AVAILABLE or not vid_id:
        return ""
    result = {}

    def _do_fetch():
        try:
            api = _YTApi(http_client=_TimeoutSession())
            try:
                fetched = api.fetch(vid_id, languages=["ja", "en"])
            except Exception:
                # ja/en 字幕が無い動画: 取得できる最初の言語にフォールバック
                fetched = next(iter(api.list(vid_id))).fetch()
            result["text"] = " ".join(seg.text for seg in fetched).replace("\n", " ")
        except Exception:
            pass

    t = threading.Thread(target=_do_fetch, daemon=True)
    t.start()
    t.join(timeout=timeout)  # 超過してもここで戻る。取り残されたスレッドはdaemonなので
    # プロセス終了をブロックしない(ThreadPoolExecutorの非daemonワーカーだと終了時に
    # ハングしたままの回収待ちでプロセスごと止まりうるため、生のThreadを使っている)。
    return result.get("text", "")[:max_chars]


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


# ---------- 汎用Web(fetch_web専用のSSRF対策: IPピン留め方式) ----------
# fetch_web()は任意の外部URL(未知ドメイン)を受け付けるため、fetch_x/fetch_instagram/
# fetch_threads/fetch_youtube等が共有する_get()(ドメイン許可リスト方式、上の_AllowlistRedirectHandler)
# は使えない。ドメインを問わず「接続先IPアドレスがプライベート/ループバック/リンクローカル/予約/
# マルチキャスト/未指定の範囲に該当したら拒否」という別方式で対策する
# (Vault Brain/knowledge/ssrf-safe-url-host-validation.md の「未対応のスコープ」参照)。
#
# 方式(DNSリバインディング対策=IPピン留め):
#   1. ホスト名を解決し、解決された全IPを ipaddress モジュールの範囲判定で検証する
#      (個々のIPを拒否リストに直書きしない。10進/8進/16進等のIPv4偽装表記もsocket.inet_atonで
#      事前に検出し、DNS解決を経由せず正しく拒否できるようにする)。
#   2. 検証を1度でも通ったIPに接続を「固定」し、検証後に再度ホスト名で名前解決させない
#      (検証と接続の間に別IPへ差し替えられるDNSリバインディング攻撃を塞ぐ)。
#   3. HTTPS接続時は、証明書検証(ホスト名照合)を壊さないよう、SSLハンドシェイクの
#      server_hostname には元のホスト名を明示的に渡す(接続先はIPだが、証明書はホスト名で照合する)。
#   4. リダイレクトは urllib.request の自動追従を使わず手動で辿り、各ホップ(初回URL含む)で
#      1〜3を毎回やり直す(初回URLだけ検証してリダイレクト先を素通りさせない)。
#
# 既存の_get()・_AllowlistRedirectHandler(上記186行目付近)は一切変更しない。このブロックは
# fetch_web()専用の新規追加であり、他の取得経路(_get()を共有する関数群)への副作用はない。

_WEB_MAX_REDIRECTS = 5


def _is_unsafe_ip(ip_str):
    """ipaddressモジュールの範囲判定で、プライベート/ループバック/リンクローカル/予約/
    マルチキャスト/未指定のいずれかに該当するIPかどうかを判定する。特定IPを拒否リストに
    直書きせず汎用的な範囲判定を使うこと(Vault Brain/mistakesで過去に指摘済みの失敗パターン)。
    ::ffff:127.0.0.1 のようなIPv4-mapped IPv6も、Pythonのipaddressモジュールが
    IPv6Address.is_private/is_loopback側で正しくTrueを返すため追加変換なしで判定できる
    (実機確認済み)。
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # パース不能なら安全側に倒して拒否
    return (ip.is_private or ip.is_loopback or ip.is_link_local or
            ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _literal_ip_from_host(host):
    """hostが(標準/非標準表記いずれかの)IPアドレスリテラルなら ipaddress オブジェクトを返す。
    通常のホスト名(DNS解決が必要)なら None を返す。

    socket.inet_aton() は "2130706433"(127.0.0.1の10進整数表記)や "017700000001"(8進)、
    "0x7f.0.0.1"(16進混在)、"127.1"(短縮形)のような非標準IPv4表記も受理する一方、
    "example.com" 等の実ホスト名は例外を投げて弾く(実機確認済み)。ipaddress.ip_address()
    単体では上記の非標準表記を文字列としては受理しない("2130706433"は文字列だとValueError)ため、
    inet_atonでの追加チェックが無いとこの偽装表記でSSRF対策(DNSベースの解決→検証)を素通りされる
    (getaddrinfoが偽装表記を解決できずDNS失敗になるか、OSによっては解決してしまう可能性があり
    プラットフォーム依存で対策が効かないケースが生まれるため、DNS解決の前でIP偽装を確定的に検出する)。
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _resolve_pinned_ip(host):
    """hostを検証し、接続に使ってよい単一のIPアドレス文字列を返す。
    戻り値: (ip_str, reason)。reasonが非空なら拒否/失敗(ip_strはNone)。

    名前解決で複数IPが返る場合、そのうち1つでもプライベート/予約範囲に該当すれば
    フェイルクローズで全体を拒否する(一部の解決結果だけが不正でも、そのホスト経由での
    接続自体を信頼しない=安全側に倒す)。
    """
    literal = _literal_ip_from_host(host)
    if literal is not None:
        if _is_unsafe_ip(str(literal)):
            return None, "private/reserved IPへの接続を拒否"
        return str(literal), ""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as e:
        return None, "DNS解決失敗: %s" % e
    candidates = []
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        if ip_str not in candidates:
            candidates.append(ip_str)
    if not candidates:
        return None, "DNS解決結果が空"
    for ip_str in candidates:
        if _is_unsafe_ip(ip_str):
            return None, "private/reserved IPへの接続を拒否"
    return candidates[0], ""


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """検証済みIPへ直接接続するHTTPConnection(IPピン留め=DNSリバインディング対策)。
    self.host(Hostヘッダに使われる)は元のホスト名のまま保持し、TCP接続先だけを
    事前に検証済みのIPアドレスに差し替える(接続時に再度ホスト名で名前解決させない)。
    """
    def __init__(self, host, port, pinned_ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS版IPピン留めConnection。TCP接続先は検証済みIPだが、SSLハンドシェイクの
    server_hostname には元のホスト名を明示的に渡すことで証明書のホスト名照合(cert検証)を
    壊さない(IPアドレスに対する証明書照合になってしまうと大半のサイトで検証エラーになるため)。
    """
    def __init__(self, host, port, pinned_ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _safe_get_web(url, ua, timeout=12, max_bytes=600_000):
    """fetch_web()専用の安全GET。(status, text, reason) を返す。
    プライベート/ループバック/リンクローカル/予約/マルチキャスト/未指定のIPアドレスへの接続を
    拒否する(SSRF対策・詳細は本ブロック冒頭のコメント参照)。失敗/拒否時は status=None または
    エラーstatus、text=""。reasonは拒否/失敗理由の説明で、SSRF対策によるブロック時は
    "private/reserved IPへの接続を拒否" のように専用の文言を返す(タイムアウト等の単純な通信失敗
    "タイムアウト"/"接続失敗: ..." とは文言上明確に区別できる)。
    """
    current_url = url
    for _ in range(_WEB_MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(current_url)
        if parts.scheme not in ("http", "https"):
            return None, "", "非対応スキーム: %s" % (parts.scheme or "(なし)")
        host = _host_of(current_url)
        if not host:
            return None, "", "ホスト名を抽出できない"
        ip, reason = _resolve_pinned_ip(host)
        if reason:
            return None, "", reason
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn = None
        try:
            if parts.scheme == "https":
                conn = _PinnedHTTPSConnection(host, port, ip, timeout)
            else:
                conn = _PinnedHTTPConnection(host, port, ip, timeout)
            conn.request("GET", path, headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"})
            resp = conn.getresponse()
            status = resp.status
            if status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                resp.read(max_bytes)  # ボディを読み切ってから次ホップへ(接続を正しく終端する)
                if not location:
                    return status, "", "リダイレクト先Locationヘッダが無い"
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            raw = resp.read(max_bytes)
            return status, raw.decode("utf-8", "replace"), ""
        except (socket.timeout, TimeoutError):
            return None, "", "タイムアウト"
        except Exception as e:
            return None, "", "接続失敗: %s" % e
        finally:
            if conn is not None:
                conn.close()
    return None, "", "リダイレクトが多すぎる"


# ---------- 汎用Web ----------
def fetch_web(url):
    st, txt, reason = _safe_get_web(url, BROWSER_UA)
    if not txt:
        return {"ok": False, "type": "web", "reason": reason or ("取得失敗 (status=%s)" % st), "url": url}
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
    host = _host_of(url)
    if _host_matches(host, "x.com") or _host_matches(host, "twitter.com"):
        return fetch_x(url)
    if _host_matches(host, "instagram.com"):
        return fetch_instagram(url)
    if _host_matches(host, "threads.com") or _host_matches(host, "threads.net"):
        return fetch_threads(url)
    if _host_matches(host, "youtube.com") or _host_matches(host, "youtu.be"):
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
