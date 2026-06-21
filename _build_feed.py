#!/usr/bin/env python3
# _build_feed.py — 公開トップ index.html を「振り返りフィード」として生成（A-1b, 2026-06-20）
#
# 設計（単一情報源を守る）:
#   - まとめ文・テーマ・気づき = 各 reviews/*.html（唯一の編集済み成果物）から抽出
#   - cover サムネ            = captures.json を日付でグループ化
#   これらを束ねて静的な index.html を書き出す（GitHub Pages で fetch 不要・zero-touch）。
#   グラフ（旧 index.html / data.js）は廃止。
#
# 使い方:  python3 _build_feed.py
#   reviews/ にある全ての YYYY-MM-DD.html を新しい順に並べてフィード化する。

import json, io, os, re, sys, html, datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURES_JSON = os.path.join(HERE, "captures.json")
REVIEWS_DIR = os.path.join(HERE, "reviews")
INDEX_HTML = os.path.join(HERE, "index.html")

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.html$")
MAX_THUMBS = 5  # サムネ帯に並べる cover の最大数（超過分は +N）


def load_captures_by_date():
  """captures.json を日付→[record] に。各日付内は元の並び順を保つ。"""
  by_date = {}
  try:
    data = json.load(io.open(CAPTURES_JSON, encoding="utf-8"))
  except Exception as e:
    print("WARN: captures.json を読めません:", e)
    return by_date, 0
  for rec in data:
    by_date.setdefault(rec.get("date", ""), []).append(rec)
  return by_date, len(data)


def extract_review(path):
  """reviews/*.html から theme / summary / notes / cards を抽出。
  件数・cover サムネも review のカードから取る（その日の唯一の真実＝review に揃える。
  captures.json の date とは別グルーピングのためズレるので参照しない）。"""
  src = io.open(path, encoding="utf-8").read()
  out = {"theme": "", "summary": "", "notes": [], "cards": []}

  m = re.search(r'<div class="meta">(.*?)</div>', src, re.S)
  if m:
    theme = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    theme = re.sub(r"の\d+件(?=$|｜|\|)", "", theme).strip()  # 「…の8件」/「…の31件｜…」の件数を落とす
    out["theme"] = theme

  # カード（その日に取り上げた各アイテム）= cover img src ＋ プレースホルダ記号
  for thumb in re.findall(r'<div class="thumb">(.*?)</div>', src, re.S):
    img = re.search(r'<img[^>]+src="([^"]+)"', thumb)
    ph = re.search(r'<span class="ph">(.*?)</span>', thumb)
    out["cards"].append({
      "src": img.group(1) if img else "",
      "ph": (re.sub(r"<[^>]+>", "", ph.group(1)).strip() if ph else "記事"),
    })

  m = re.search(r'<section class="summary">.*?<p>(.*?)</p>', src, re.S)
  if m:
    out["summary"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()

  # 気づき = <section class="notes"> 内の .ilist / .qlist の各 li だけ。
  # （まとめの .pts li やカードを拾わないよう notes セクションに限定する）
  mnotes = re.search(r'<section class="notes">(.*?)</section>', src, re.S)
  notes_src = mnotes.group(1) if mnotes else ""
  for li in re.findall(r'<li>(.*?)</li>', notes_src, re.S):
    b = re.search(r"<b>(.*?)</b>", li, re.S)
    s = re.search(r"<span>(.*?)</span>", li, re.S)
    if not b:
      continue
    head = re.sub(r"<[^>]+>", "", b.group(1)).strip()
    body = re.sub(r"<[^>]+>", "", s.group(1)).strip() if s else ""
    out["notes"].append((head, body))
  return out


def esc(s):
  return html.escape(s or "", quote=True)


def thumb_html(card):
  src, ph = card.get("src", ""), card.get("ph", "記事")
  if src:
    return ('<a><img src="%s" alt="" loading="lazy" onerror="this.style.display=\'none\'">'
            '<span class="ph">%s</span></a>' % (esc(src), esc(ph)))
  return '<a><span class="ph">%s</span></a>' % esc(ph)


def day_block(date, theme, summary, notes, cards):
  count = len(cards)
  parts = ['<div class="day">']
  cnt = ('<span class="count">%d件</span>' % count) if count else ''
  parts.append('<div class="dayhead"><span class="date">%s</span>%s</div>' % (esc(date), cnt))
  if theme:
    parts.append('<div class="theme">%s</div>' % esc(theme))
  if summary:
    parts.append('<section class="summary"><p>%s</p></section>' % esc(summary))

  if cards:
    parts.append('<div class="strip">')
    for c in cards[:MAX_THUMBS]:
      parts.append(thumb_html(c))
    extra = count - MAX_THUMBS
    if extra > 0:
      parts.append('<a><span class="ph">+%d</span></a>' % extra)
    parts.append('</div>')

  if notes:
    parts.append('<div class="notes"><ul class="ilist">')
    for head, body in notes:
      span = (' <span>→ %s</span>' % esc(body)) if body else ''
      parts.append('<li><b>%s</b>%s</li>' % (esc(head), span))
    parts.append('</ul></div>')

  parts.append('<a class="more" href="reviews/%s.html">全文の振り返りを読む →</a>' % esc(date))
  parts.append('</div>')
  return "\n    ".join(parts)


def build():
  by_date, total = load_captures_by_date()

  dates = []
  if os.path.isdir(REVIEWS_DIR):
    for fn in os.listdir(REVIEWS_DIR):
      m = DATE_RE.match(fn)
      if m:
        dates.append(m.group(1))
  dates.sort(reverse=True)  # 新しい日が上

  updated = dates[0] if dates else datetime.date.today().isoformat()

  blocks = []
  for i, date in enumerate(dates):
    rv = extract_review(os.path.join(REVIEWS_DIR, date + ".html"))
    blocks.append(day_block(date, rv["theme"], rv["summary"], rv["notes"], rv["cards"]))
    if i < len(dates) - 1:
      blocks.append('<hr class="sep">')

  page = (TEMPLATE
          .replace("{{UPDATED}}", esc(updated))
          .replace("{{COUNT}}", str(total))
          .replace("{{BLOCKS}}", "\n\n  ".join(blocks)))
  io.open(INDEX_HTML, "w", encoding="utf-8").write(page)
  print("OK: index.html を生成（%d 日分・キャプチャ %d 件）" % (len(dates), total))


TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>毎朝の振り返り ｜ katut-brain</title>
<style>
  :root {
    --bg: #fafafa; --panel: #ffffff; --ink: #1c1c1e; --sub: #8a8a8e;
    --line: #e3e3e6; --chip: #f1f1f3; --accent: #1e88c7;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115; --panel: #161922; --ink: #e8e8ea; --sub: #8b91a0;
      --line: #2b3140; --chip: #1e2230; --accent: #5ec8f9;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--ink); line-height: 1.7;
    font-family: "Segoe UI", "Hiragino Sans", "Yu Gothic UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased; }
  .wrap { max-width: 720px; margin: 0 auto; padding: 22px 16px 64px; }
  h1.site { font-size: 22px; margin: 0; font-weight: 700; }
  .tagline { color: var(--sub); font-size: 13px; margin: 3px 0 4px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 8px; }
  .chip { background: var(--chip); color: var(--sub); border-radius: 999px; padding: 4px 10px; font-size: 12px; }

  .day { margin-top: 30px; }
  .dayhead { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
  .dayhead .date { font-size: 19px; font-weight: 700; }
  .dayhead .count { color: var(--sub); font-size: 12px; }
  .theme { color: var(--sub); font-size: 13px; margin-bottom: 12px; }

  .summary { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px 14px; }
  .summary p { margin: 0; font-size: 14px; }

  .strip { display: flex; gap: 8px; overflow-x: auto; margin: 12px 0 2px; padding-bottom: 2px; }
  .strip a { flex: none; width: 96px; height: 72px; border-radius: 10px; overflow: hidden; position: relative;
    background: var(--chip); border: 1px solid var(--line); text-decoration: none; }
  .strip img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; }
  .strip .ph { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    font-size: 10px; color: var(--sub); }

  .notes { margin-top: 10px; }
  .ilist { list-style: none; padding: 0; margin: 0; display: grid; gap: 6px; }
  .ilist li { font-size: 13px; padding-left: 18px; position: relative; }
  .ilist li::before { content: "\\1F4AC"; position: absolute; left: 0; font-size: 11px; }
  .ilist b { color: var(--ink); }
  .ilist span { color: var(--sub); }

  .more { display: inline-block; margin-top: 12px; color: var(--accent); text-decoration: none; font-size: 13px; font-weight: 700; }
  .more:hover { text-decoration: underline; }

  hr.sep { border: none; border-top: 1px solid var(--line); margin: 30px 0 0; }
  footer { color: var(--sub); font-size: 11px; margin-top: 40px; text-align: center; opacity: .8; }
</style>
</head>
<body>
<div class="wrap">
  <h1 class="site">毎朝の振り返り</h1>
  <div class="tagline">見たもの・読んだものから、その日の気づきを残す</div>
  <div class="chips">
    <span class="chip">最終更新 {{UPDATED}}</span>
    <span class="chip">キャプチャ {{COUNT}}件</span>
  </div>

  {{BLOCKS}}

  <footer>毎朝の同期で自動生成 ｜ katut-brain</footer>
</div>
</body>
</html>
"""


if __name__ == "__main__":
  build()
