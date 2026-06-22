import json, io, re, os, sys, datetime
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# fetch_content.py が同じディレクトリにある前提でインポート
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
  from fetch_content import fetch_content as _fetch_content
  _FETCH_AVAILABLE = True
except ImportError:
  _FETCH_AVAILABLE = False
  print("WARN: fetch_content.py が見つかりません。summary フェーズをスキップします。")

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

# TODAY = 振り返り対象日（=実行日の前日）。当日取り込み（new チップ）に使う。
# 環境変数 BUILD_TODAY=YYYY-MM-DD で上書き可（特定日を再生成したいとき）。
TODAY = os.environ.get("BUILD_TODAY") or (datetime.date.today() - datetime.timedelta(days=1)).isoformat()


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
items = []
try:
  TOKEN = get_raindrop_token()
  if not TOKEN:
    raise RuntimeError("Raindrop トークンが取得できません（RAINDROP_TOKEN 未設定）")
  req = urllib.request.Request("https://api.raindrop.io/rest/v1/raindrops/0?perpage=50&sort=-created",
                               headers={"Authorization": "Bearer " + TOKEN})
  items = json.load(urllib.request.urlopen(req)).get("items", [])
except Exception as e:
  print("WARN: Raindrop 取得をスキップ（captures.json のみでグラフを構築）:", e)

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


# --- import フェーズ：Raindrop の新規だけ captures.json に追記 ---
recs = load_captures()
existing = {r["rid"] for r in recs}
hub_set = {h for v in clusters.values() for h,_ in v["hubs"]}
created = 0
for it in items:
  rid = it["_id"]
  if rid in existing:
    continue
  title = it.get("title","") or "(無題)"
  exc = it.get("excerpt","") or ""
  link = it.get("link","") or ""
  note = it.get("note","") or ""
  if rid in overrides:
    cl, hub, _lbl = overrides[rid]
  else:
    cl, hub = classify(title, exc, link, note)
  recs.append({
    "rid": rid,
    "cluster": cl,
    "hub": hub,
    "source": link,
    "date": (it.get("created","") or "")[:10] or TODAY,
    "cover": it.get("cover","") or "",
    "type": it.get("type","") or "link",
    "captured": it.get("created","") or "",
    "title": title,
    "note": note,
    "summary": "",   # 未要約。後段（Haiku要約・本文取得=fetch_content）で埋める
  })
  existing.add(rid)
  created += 1

if created:
  recs = sorted(recs, key=lambda r: (r.get("captured") or r.get("date") or ""), reverse=True)
  save_captures(recs)

# --- fetch フェーズ：新規アイテム（summary が空のもの）のコンテンツ取得 ---
# 対象: 今回 Raindrop から新規取り込みした分のうち summary が空のもの
# fetch_content.py を使い、取得結果の text を summary に書く。
# 失敗時は空のまま記録してスキップ（エラーで止まらない）。
if created and _FETCH_AVAILABLE:
  # 今回新規に追加されたアイテムだけ対象（全件 backfill は行わない）
  # "captured" が今回の Raindrop items の rid に含まれるものを特定
  new_rids = {it["_id"] for it in items}
  fetch_updated = 0
  for r in recs:
    if r["rid"] not in new_rids:
      continue
    if r.get("summary", "") != "":
      continue
    url = r.get("source", "")
    if not url:
      continue
    try:
      result = _fetch_content(url)
      if result.get("ok"):
        # text フィールドを summary として使用（title は触らない）
        r["summary"] = (result.get("text") or "").strip()[:500]
        fetch_updated += 1
      else:
        # 失敗時は空のまま（reason をログに出す）
        print("WARN: fetch 失敗 rid=%s reason=%s" % (r["rid"], result.get("reason", "unknown")))
    except Exception as e:
      print("WARN: fetch 例外 rid=%s %s" % (r["rid"], e))
  if fetch_updated:
    save_captures(recs)
    print("fetch_content: %d 件の summary を更新しました" % fetch_updated)
  else:
    print("fetch_content: summary 更新対象なし（新規 %d 件はすべて取得済みか失敗）" % created)

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
