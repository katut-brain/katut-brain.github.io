import json, io, re, os, sys, time, datetime
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ============================================================================
# Vault非依存版（A-1, 2026-06-20）
#   真実源 = 公開リポ内の captures.json（このスクリプトと同じディレクトリ）。
#   Vault（Obsidian）は一切読まない。新規キャプチャは Raindrop API から取り込み、
#   captures.json に追記してから data.js を生成する。
#   → PC/Vault に依存せず、リポ + Raindrop だけで公開グラフを作れる（クラウドcron可）。
# ============================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURES_JSON = os.path.join(HERE, "captures.json")
DATA_JS = os.path.join(HERE, "data.js")

# 運用日はすべて日本時間で数える。Routine 側の対象日が
#   TARGET=$(TZ=Asia/Tokyo date -d yesterday +%F)
# なので、ここで UTC 基準の日付を作ると 9 時間ぶんズレて JST 早朝の保存が
# どちらの窓にも入らず永久に落ちる（実測 6/195 件）。
# 詳細: Vault Brain/knowledge/raindrop-utc-jst-date-mismatch.md
JST = datetime.timezone(datetime.timedelta(hours=9))


def jst_date(created):
  """Raindrop の created（UTC の ISO8601）を JST の日付文字列にする。
  パースできない値は空文字を返す（呼び出し側でフォールバックする）。"""
  s = (created or "").strip()
  if not s:
    return ""
  try:
    # "2026-08-14T23:16:18.018Z" 形式。Python 3.10 系は 'Z' を直接は読めない
    dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
  except ValueError:
    return s[:10]  # 最低限、先頭10文字にフォールバック（従来挙動）
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=datetime.timezone.utc)
  return dt.astimezone(JST).date().isoformat()


# TODAY = 振り返り対象日（=実行日の前日・JST）。当日取り込み（new チップ）に使う。
# 環境変数 BUILD_TODAY=YYYY-MM-DD で上書き可（特定日を再生成したいとき）。
TODAY = os.environ.get("BUILD_TODAY") or (
  datetime.datetime.now(JST).date() - datetime.timedelta(days=1)).isoformat()


def get_raindrop_token():
  """Raindrop トークン取得。環境変数を優先（クラウド/secret向け）、
  無ければローカルの Make-It-Rain プラグイン設定にフォールバック。"""
  tok = os.environ.get("RAINDROP_TOKEN")
  if tok:
    return tok.strip()
  local = r"C:\Users\katut\Documents\ObsidianVault\.obsidian\plugins\make-it-rain\data.json"
  try:
    return json.load(io.open(local, encoding="utf-8"))["apiToken"]
  except Exception:
    return None


# Raindrop 取得は「新規キャプチャの取り込み（import）」のためだけ。
# 取得失敗（トークン無し・オフライン・クラウドIP制限等）でも captures.json から
# グラフは描けるよう try で包む。
# ページネーション対応（perpage=50 の1リクエスト天井による取りこぼしバグの修正。
# 詳細: Vault Brain/knowledge/raindrop-api-pagination-50limit.md）。
MAX_PAGES = 50  # 50ページ * 50件/page = 2500件の安全弁（無限ループ防止）
PAGE_RETRIES = 3       # 1ページあたりの再試行回数（瞬断対策）
PAGE_RETRY_WAIT = 2.0  # 再試行の待ち（秒）

# 取得の健全性。1ページでも取りこぼしたら False にし、以降の
# 「既存レコードの棚卸し」で削除判定を行わない根拠にする。
import_complete = False
import_errors = []
items = []
TOKEN = get_raindrop_token()
if not TOKEN:
  import_errors.append("Raindrop トークンが取得できません（RAINDROP_TOKEN 未設定）")
  print("WARN: Raindrop 取得をスキップ（captures.json のみでグラフを構築）:", import_errors[-1])
else:
  expected_count = None
  page = 0
  # ページ単位で try する。全体を1つの try で包むと、1ページの瞬断で
  # その回の新規保存が丸ごと消え、しかも警告だけ出して完走してしまう
  # （無人運用ではこれが一番痛い。実測の指摘: Gemini 敵対的レビュー 2026-08-23）。
  while page < MAX_PAGES:
    page_items = None
    for attempt in range(PAGE_RETRIES):
      try:
        req = urllib.request.Request(
          "https://api.raindrop.io/rest/v1/raindrops/0?perpage=50&sort=-created&page=%d" % page,
          headers={"Authorization": "Bearer " + TOKEN})
        data = json.load(urllib.request.urlopen(req, timeout=30))
        if expected_count is None:
          expected_count = data.get("count")
        page_items = data.get("items", [])
        break
      except Exception as e:
        msg = "page=%d attempt=%d/%d: %s" % (page, attempt + 1, PAGE_RETRIES, e)
        if attempt + 1 < PAGE_RETRIES:
          print("WARN: Raindrop 取得を再試行 —", msg)
          time.sleep(PAGE_RETRY_WAIT)
        else:
          import_errors.append(msg)
          print("ERROR: Raindrop 取得に失敗（このページは諦めて次ページへ）—", msg)
    if page_items is None:
      # このページは3回とも失敗。ここで break すると以降のページも取らずに
      # 「取れた分だけ」で完走してしまい、しかも後続に何件あったか分からない。
      # 次ページへ進めば末尾の count 照合で不一致として必ず検出できる。
      page += 1
      continue
    if not page_items:       # 正常終了（これ以上ない）
      import_complete = True
      break
    items.extend(page_items)
    page += 1
  if page >= MAX_PAGES:
    import_errors.append("MAX_PAGES=%d に到達（取りこぼしの可能性あり）" % MAX_PAGES)
    print("WARN: Raindrop 取得が MAX_PAGES=%d に到達（強制終了。取りこぼしの可能性あり）" % MAX_PAGES)
  if expected_count is not None and expected_count != len(items):
    import_complete = False
    import_errors.append("count 不一致（API count=%s, 取得件数=%d）" % (expected_count, len(items)))
    print("WARN: Raindrop count 不一致（API count=%s, 取得件数=%d）。部分的な結果で続行します" %
          (expected_count, len(items)))
  elif expected_count is not None:
    import_complete = True
    print("Raindrop 取得件数: %d 件（count と一致）" % len(items))

clusters = {
  "arch":    {"name": "建築の言説・展覧会",         "rgb": "232,85,45",  "hubs": [("h_arch","建築"),("h_exh","展覧会"),("h_crit","言説・批評")]},
  "culture": {"name": "アート / デザイン・キュレーション", "rgb": "41,182,246", "hubs": [("h_cur","キュレーション"),("h_arc","アーカイブ"),("h_aiart","AI×表現"),("h_fash","ファッション")]},
  "ai":      {"name": "AI活用・SNS収益化",          "rgb": "120,175,70", "hubs": [("h_aiuse","AI活用"),("h_money","収益化"),("h_threads","Threads運用"),("h_insta","Instagram運用"),("h_claude","Claude Code")]},
  "d3d":     {"name": "3D・デザインツール",          "rgb": "171,71,188", "hubs": [("h_3d","3D・ツール"),("h_ui","UIデザイン")]},
  "misc":    {"name": "雑学・バズ",                 "rgb": "240,170,40", "hubs": [("h_misc","雑学・バズ")]},
}

# overrides = ラベルまで固定したい少数の例外だけ。通常の分類はレコードの
# cluster/hub（取り込み時に付与）で行うので、ここには基本足さなくてよい。
overrides = {
  1756418244: ("arch","h_exh","Under35 建築家展"),
  1756487204: ("arch","h_crit","布施・磯崎新論"),
  1756484712: ("culture","h_aiart","nova / divine simulation"),
  1756483154: ("culture","h_arc","マルジェラ アーカイブ展"),
}


def classify(title, excerpt, link, note=""):
  # note（ユーザーの一言コメント）は「なぜ刺さったか」のシグナルなので分類に含める
  t = (title + " " + excerpt + " " + link + " " + note).lower()
  if "雑学" in t or "バズ" in t or "タピオカ" in t or "郵便ポスト" in t or "郵政" in t: return ("misc","h_misc")
  if any(k in t for k in ["under 35","磯崎","建築家","建築の展覧"]): return ("arch","h_exh")
  if any(k in t for k in ["nova","hypebeast","margiela","マルジェラ"]): return ("culture","h_arc")
  if "twinmotion" in t or "flashforge" in t or "diseño" in t or "qupe" in t or "プリンタ" in t or " 3d" in t or "3d " in t: return ("d3d","h_3d")
  if "プロダクトのui" in t or "uiトレンド" in t or " ui " in t: return ("d3d","h_ui")
  if "claudecode" in t or "claude code" in t or "コード社長" in t or "おサボり" in t or "claude" in t: return ("ai","h_claude")
  if "on threads" in t or "threads" in t: return ("ai","h_threads")
  if "収益" in t or "副業" in t or "月収" in t or "月7桁" in t or "月50万" in t or "稼" in t or "note" in t or "patreon" in t or "income" in t: return ("ai","h_money")
  if "instagram" in t or "インスタ" in t: return ("ai","h_insta")
  return ("ai","h_aiuse")


def short(title):
  s = re.split(r"\s*[（(]@", title)[0]
  s = re.split(r"\s+•\s+|\s+on Threads", s)[0]
  s = s.strip().strip("｜|")
  return s[:18] if s else "(無題)"


def load_captures():
  """captures.json（真実源）を読む。無ければ空。"""
  try:
    data = json.load(io.open(CAPTURES_JSON, encoding="utf-8"))
    return data if isinstance(data, list) else []
  except FileNotFoundError:
    return []
  except Exception as e:
    print("WARN: captures.json 読み込み失敗（空で続行）:", e)
    return []


def save_captures(recs):
  io.open(CAPTURES_JSON, "w", encoding="utf-8").write(
    json.dumps(recs, ensure_ascii=False, indent=2) + "\n")


# --- import フェーズ：Raindrop の新規を追記し、既存も冪等に更新する ---
#
# 旧実装は `if rid in existing: continue` で既存を素通ししていた。その結果
#   ・初回取り込み時の誤り（UTC日付・古いタイトル・古いURL）が永久に固定される
#   ・取りこぼしや一過性の失敗を、後から再実行しても回復できない
# という状態だった。ここを冪等にしないと、この後の日付修正も過去には効かない
# （Codex 敵対的レビュー 2026-08-23 の指摘）。
#
# フィールドの所有者を分ける。全部を Raindrop で上書きすると、ローカルで
# 育てた値が毎晩壊れる（2026-08-23 の実装時に実測: title 80件・type 4件が劣化した。
# 例「ヘルツォーグ＆ド・ムーロン: SF湾岸の発電所改修複合施設」→「【アーカイブ】」、
# type "x-post" → "link"）。
#
#   AUTHORITATIVE = Raindrop が正。常に上書きする
#     source/captured … 一次データ。ローカルで書き換えてはいけない（過去にURL捏造事故あり）
#     date            … captured から導出する運用日（JST）
#     note            … ユーザーが Raindrop に書く一言。本人が直したら追従する
#     tags            … 同上
#   FILL_ONLY = ローカルが空のときだけ Raindrop で埋める
#     title/type      … ローカルで人が読める形に整えている
#     cover           … 取得できた画像を保持する。Raindrop 側が空でも消さない
RAINDROP_AUTHORITATIVE = ("source", "date", "captured", "note", "tags")
RAINDROP_FILL_ONLY = ("title", "type", "cover")

recs = load_captures()
by_rid = {r["rid"]: r for r in recs}
hub_set = {h for v in clusters.values() for h,_ in v["hubs"]}
created = 0
updated = 0
changed_fields = {}


def raindrop_fields(it):
  """Raindrop のレコードから、captures.json 側で保持するフィールドを作る。"""
  created_at = it.get("created", "") or ""
  return {
    "source": it.get("link", "") or "",
    "date": jst_date(created_at) or TODAY,
    "cover": it.get("cover", "") or "",
    "type": it.get("type", "") or "link",
    "captured": created_at,
    "title": it.get("title", "") or "(無題)",
    "note": it.get("note", "") or "",
    "tags": it.get("tags", []) or [],
  }


for it in items:
  rid = it["_id"]
  fields = raindrop_fields(it)
  cur = by_rid.get(rid)
  if cur is None:
    if rid in overrides:
      cl, hub, _lbl = overrides[rid]
    else:
      cl, hub = classify(fields["title"], it.get("excerpt","") or "",
                         fields["source"], fields["note"])
    rec = {"rid": rid, "cluster": cl, "hub": hub}
    rec.update(fields)
    # 更新パスと同じ形にしておく（ここで入れないと次回ランで必ず差分が出て、
    # 冪等でなくなる）
    rec["title_raindrop"] = fields["title"]
    rec["summary"] = ""   # 未要約。後段（本文取得=fetch_content）で埋める
    recs.append(rec)
    by_rid[rid] = rec
    created += 1
    continue
  # 既存レコード：所有者に応じて差分更新する
  diff = []
  for k in RAINDROP_AUTHORITATIVE:
    if cur.get(k) != fields[k]:
      cur[k] = fields[k]
      diff.append(k)
  for k in RAINDROP_FILL_ONLY:
    if not cur.get(k) and fields[k]:
      cur[k] = fields[k]
      diff.append(k + "(補填)")
  # Raindrop 側のタイトルは表示用 title を壊さずに別フィールドで持つ。
  # 「上流で何と呼ばれているか」を後から照合できるようにするため。
  if fields["title"] and cur.get("title_raindrop") != fields["title"]:
    cur["title_raindrop"] = fields["title"]
    diff.append("title_raindrop")
  # overrides は新規取り込み時にしか効いていなかった。グラフ構築側は毎回
  # overrides を優先するので、後から override を足すと captures.json と
  # グラフで分類が食い違う。既存レコードにも反映して一本化する。
  if rid in overrides:
    ocl, ohub, _lbl = overrides[rid]
    if (cur.get("cluster"), cur.get("hub")) != (ocl, ohub):
      cur["cluster"], cur["hub"] = ocl, ohub
      diff.append("cluster/hub(override)")
  if diff:
    for k in diff:
      changed_fields[k] = changed_fields.get(k, 0) + 1
    updated += 1

# --- バックフィル：Raindrop に無い（＝もう取得できない）既存レコードの日付も JST に正規化 ---
# 旧実装が UTC 日付で書いた分が残っているため、captured から作り直す。
# 取得が不完全だった回でも安全（date の再計算はレコード内で完結する）。
backfilled = []
for r in recs:
  d = jst_date(r.get("captured", ""))
  if d and r.get("date") != d:
    backfilled.append({"rid": r.get("rid"), "old": r.get("date"), "new": d,
                       "captured": r.get("captured", "")})
    r["date"] = d

# 日付の付け替えは、既に公開済みの reviews/YYYY-MM-DD.html との対応を静かに
# ずらす（公開済みHTMLは当時の内容のまま残る）。何をいつ動かしたかを必ず残す。
if backfilled:
  log_path = os.path.join(HERE, "date_backfill_log.json")
  try:
    prev = json.load(io.open(log_path, encoding="utf-8")) if os.path.exists(log_path) else []
    if not isinstance(prev, list):
      prev = []
  except Exception:
    prev = []
  prev.append({
    "run_at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
    "reason": "UTC日付→JST運用日への正規化",
    "count": len(backfilled),
    "entries": backfilled,
  })
  io.open(log_path, "w", encoding="utf-8").write(
    json.dumps(prev, ensure_ascii=False, indent=1) + "\n")
  print("  日付バックフィルの記録:", log_path)

print("import: 新規 %d 件 / 既存更新 %d 件 / 日付バックフィル %d 件" %
      (created, updated, len(backfilled)))
if changed_fields:
  print("  更新されたフィールド:", ", ".join("%s=%d" % kv for kv in sorted(changed_fields.items())))
if import_errors:
  print("  取得エラー:", " / ".join(import_errors))

# 取得完全性は「ログに出して終わり」にしない。無人運用ではログを誰も読まない。
# 機械可読な1行を出し、後続（Routine手順・監査）が拾えるようにする。
print("IMPORT_STATUS: %s items=%d errors=%d" %
      ("complete" if import_complete else "INCOMPLETE", len(items), len(import_errors)))
if not import_complete:
  print("WARN: Raindrop を全件取得できていない。この回の振り返りは欠損しうる。"
        "reviews のフッタに欠損を明記し、翌ランで再取得すること（import は冪等）。")

if created or updated or len(backfilled):
  recs = sorted(recs, key=lambda r: (r.get("captured") or r.get("date") or ""), reverse=True)
  save_captures(recs)

# --- グラフ構築フェーズ：captures.json の全件から作る ---
nodes = []
edges = []
cluster_ids = {k: [hid for hid,_ in v["hubs"]] for k,v in clusters.items()}

# hub nodes（各クラスタ先頭=親概念=大／以降=子概念=中）
for k,v in clusters.items():
  for idx,(hid,lab) in enumerate(v["hubs"]):
    nodes.append({"id":hid,"label":lab,"kind":"concept","cluster":k,"value":(26 if idx==0 else 15)})
# 階層: 親概念 → 子概念（スター状）
for k,v in clusters.items():
  hs=[hid for hid,_ in v["hubs"]]
  lead=hs[0]
  for h in hs[1:]:
    edges.append({"from":lead,"to":h})
# light cross bridges
edges.append({"from":"h_aiuse","to":"h_aiart","label":"AI","dashes":True})
edges.append({"from":"h_3d","to":"h_arch","label":"つくる/見る","dashes":True})

for r in recs:
  rid = r["rid"]
  title = r.get("title","(無題)")
  link = r.get("source","")
  note = r.get("note","")
  if rid in overrides:
    cl, hub, lbl = overrides[rid]
  elif r.get("cluster") in clusters and r.get("hub") in hub_set:
    cl, hub = r["cluster"], r["hub"]; lbl = short(title)   # レコードの cluster/hub を優先
  else:
    cl, hub = classify(title, "", link, note)              # ヒント無しは keyword 分類にフォールバック
    lbl = short(title)
  nid = "c%d" % rid
  is_new = (r.get("date") == TODAY)
  cap_node = {"id":nid,"label":lbl,"kind":"capture","cluster":cl,"new":is_new,"url":link}
  if note: cap_node["note"] = note   # ホバーのツールチップに出すユーザーコメント
  nodes.append(cap_node)
  edges.append({"from":nid,"to":hub})
  cluster_ids[cl].append(nid)

cap_count = sum(1 for n in nodes if n["kind"]=="capture")
out_clusters = {k:{"name":v["name"],"rgb":v["rgb"],"ids":cluster_ids[k]} for k,v in clusters.items()}
graph = {"updated": TODAY, "count": cap_count, "clusters": out_clusters, "nodes": nodes, "edges": edges}
js = "window.GRAPH = " + json.dumps(graph, ensure_ascii=False) + ";\n"
io.open(DATA_JS,"w",encoding="utf-8").write(js)

# summary
from collections import Counter
cap_by = Counter(n["cluster"] for n in nodes if n["kind"]=="capture")
print("raindrop items:", len(items), "| notes imported:", created, "| captures total:", len(recs), "| total nodes:", len(nodes), "edges:", len(edges))
print("captures per cluster:", dict(cap_by))
