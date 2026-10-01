#!/usr/bin/env python3
# note_gaps.py — 手順7.5 の「公開済みなのに Vault にノートが無いブックマーク」を選ぶ。
#
# なぜ要るか（2026-09-30 capture-pipeline P-0）:
#   手順3の選定（select_targets.py）は「どの reviews/*.html にも載っていない保存」
#   だけを選ぶ。だから手順8で reviews/<TARGET>.html が公開され、手順8.5（Vaultリポ
#   へのノート送信）が失敗した夜や、手順7.5 のスキーマ検証に落ちた夜のブックマークは、
#   翌晩以降の選定に二度と入らない＝ノートが永久に作られない。手順書の
#   「翌晩以降の手順7.5で改めて対象になる」は、2026-09-22 に選定方式を変えた時点で
#   成り立たなくなっていた。
#
# やること:
#   「reviews/*.html のカード（data-rid）として公開済み」かつ「Vaultリポの
#   Explore/bookmarks/ にその rid のノートが無い」ものを、毎晩その場で数え直す。
#   台帳もキューも持たない（2026-09-09 の「回収装置は作らない」で問題になった
#   押せなかった日の列を作らない。状態から毎回導出するだけ）。
#   「掲載済みか」の判定は select_targets.card_index() をそのまま使う（正本は1か所）。
#
# 安全側の約束:
#   - 今夜の reviews/<TARGET>.html にしか載っていないカードは数えない。手順7.5 の
#     時点で今夜の reviews は書かれているが、まだ公開されておらず、そのノートは
#     今夜の通常経路（手順3の選定）が作る。ここで拾うと二重に作る。
#     --targets（手順3の /tmp/targets.json）に載っている rid も同じ理由で除く。
#   - Vault が読めない／rd-*.md が MIN_VAULT_NOTES 件未満／rid をファイル名から取れない
#     ノートが読めなかった／フォルダの走査に失敗した、のどれかなら何も選ばない
#     （読み取りの失敗を「全部ノートが無い」と取り違えて大量に作らない）。理由は
#     NOTE_GAPS 行の reason= に出る（no_dir / too_few:N / read_failed:N）。
#   - ノートの有無はファイル名の rd-<rid> と frontmatter の raindrop_id の両方で
#     見る（frontmatter が壊れたノートを「無い」と数えて作り直さない）。見る範囲は
#     Explore/bookmarks/ の**サブフォルダも含む**（手順7.5 の重複チェックが grep -r で
#     サブフォルダも見るのに揃える。揃えないと、サブフォルダにあるノートを毎晩 gap と
#     数えては重複チェックで飛ばし、古い順の5枠を永久に占有して新しい gap が回収されない）。
#   - GAP_SINCE より前に公開されたカードは数えない。1ブックマーク1ノート制
#     （2026-08-04 決定）より前に公開された67件（最新は 08-03）は制度の外で、
#     取りこぼしではない（2026-09-30 ユーザー裁定「08-05 以降」）。
#   - 1晩の上限 --limit（既定5）。公開日の古い順→rid 順に選ぶ。
#   - 例外・不正な引数・--out の書き込み失敗のどれでも exit 0 で NOTE_GAPS 行を出す
#     （無人Routineを止めない）。例外のときは --out に records が空の JSON を書く。
#     --out に書けなかったときは古い出力が使われないよう先に消してあり、行は
#     status=error になる。--out の親フォルダが無い等で行そのものを出せない場合に備えて、
#     手順書は「行が出ない・exit が0以外」も status=error と同じ扱いにする。
#
# 既知の限界:
#   - Vault で意図的にノートを消すと、翌晩ここで作り直される（2026-09-30 時点で
#     Explore/bookmarks の削除は git 履歴上0件）。消す運用を始めるなら除外の
#     仕組みが要る。
#   - 材料は captures.json のレコードと公開済みカードの一言（.vdesc）だけで、
#     手順4の本文は無い（その夜に取り直さない）。captures.json から消えた
#     レコードはカードの情報だけになる（date は公開日で代用し date_from で示す）。

import sys
import os
import io
import re
import json
import html as html_module
import argparse
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import select_targets as st  # noqa: E402

GAP_SINCE = "2026-08-05"
DEFAULT_LIMIT = 5
MIN_VAULT_NOTES = 100

_NAME_RID_RE = re.compile(r"^rd-(-?\d+)(?:-|\.md$)")
_FM_RID_RE = re.compile(r"^raindrop_id:\s*['\"]?(-?\d+)['\"]?[ \t]*(?:#[^\r\n]*)?\s*$", re.M)
_DIV_CLASS_RE = r"""<div\b[^>]*\bclass\s*=\s*["'][^"']*\b%s\b[^"']*["'][^>]*>(.*?)</div>"""
_TAG_RE = re.compile(r"<[^>]+>")
_BUTTON_OPEN_RE = re.compile(r"<button\b[^>]*>")


def vault_rids(vault_dir):
    """Vaultリポの Explore/bookmarks/（サブフォルダも含む）から rid 集合を作る。

    戻り値: (rids: set[int], note_count: int, problem: str | None)
    problem が None 以外は「読めない／信用できない」＝呼び出し側は何も選ばない。
      no_dir        vault_dir が無い、または直下に Explore/bookmarks/ が無い
      read_failed:N rid をファイル名から取れず、中身も読めなかった .md が N 個、
                    またはフォルダの走査に失敗した（その中に rid が載っているかもしれない）
      too_few:N     rd-*.md が MIN_VAULT_NOTES 件未満（N は実数）
    note_count は rd-*.md の件数。ファイル名から rid が取れるノートは、中身が読めなくても
    ファイル名だけで数える（読み取り失敗にはしない）。
    """
    if not vault_dir:
        return set(), 0, "no_dir"
    bm = os.path.join(vault_dir, "Explore", "bookmarks")
    if not os.path.isdir(bm):
        return set(), 0, "no_dir"
    rids = set()
    note_count = 0
    walk_errors = []
    read_failed = 0
    for root, _dirs, files in os.walk(bm, onerror=walk_errors.append):
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            if name.startswith("rd-"):
                note_count += 1
            m = _NAME_RID_RE.match(name)
            if m:
                rids.add(int(m.group(1)))
            try:
                with io.open(os.path.join(root, name), encoding="utf-8-sig") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError, ValueError):
                if not m:
                    read_failed += 1
                continue  # ファイル名の rid だけで数える
            if text.startswith("---"):
                end = text.find("\n---", 3)
                fm = text[3:end] if end != -1 else ""
                for fm_m in _FM_RID_RE.finditer(fm):
                    rids.add(int(fm_m.group(1)))
    read_failed += len(walk_errors)
    if read_failed:
        return rids, note_count, "read_failed:%d" % read_failed
    if note_count < MIN_VAULT_NOTES:
        return rids, note_count, "too_few:%d" % note_count
    return rids, note_count, None


def _card_text(card_raw, cls):
    m = re.search(_DIV_CLASS_RE % re.escape(cls), card_raw, re.S)
    if not m:
        return ""
    return " ".join(html_module.unescape(_TAG_RE.sub("", m.group(1))).split())


def card_material(reviews_dir, review_date, rid):
    """review_date の reviews から rid のカードを探し、題名・一言・URLを返す。"""
    path = os.path.join(reviews_dir, review_date + ".html")
    try:
        with io.open(path, encoding="utf-8", newline="") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    cards, _malformed = st.extract_vcards(raw)
    for c in cards:
        rid_str = st.card_rid(c["raw"])
        try:
            if rid_str is None or int(rid_str) != rid:
                continue
        except ValueError:
            continue
        data_title = ""
        for tag in _BUTTON_OPEN_RE.findall(c["raw"]):
            val = st._get_attr(tag, "data-title")
            if val:
                data_title = html_module.unescape(val)
                break
        return {
            "vtitle": _card_text(c["raw"], "vtitle"),
            "vdesc": _card_text(c["raw"], "vdesc"),
            "href": st.card_href(c["raw"]) or "",
            "data_title": data_title,
        }
    return {}


def captures_by_rid(captures_path):
    """captures.json を rid→レコードに。rid が数字の文字列でも int として引く。
    rid の無いレコードは select_targets.synthetic_rid() で引けるようにする
    （カードの data-rid に合成 rid が書かれている場合に対応）。"""
    data = st._load_json(captures_path)
    records = data if isinstance(data, list) else (data or {}).get("captures", [])
    out = {}
    if not isinstance(records, list):
        return out
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rid = rec.get("rid")
        if isinstance(rid, bool):
            rid = None
        if isinstance(rid, str) and re.fullmatch(r"-?\d+", rid.strip()):
            rid = int(rid.strip())
        if not isinstance(rid, int):
            date = rec.get("date")
            if not isinstance(date, str) or len(date) < 10:
                continue
            rid = st.synthetic_rid(rec)
        out.setdefault(rid, rec)
    return out


def _load_target_rids(path):
    if not path:
        return set()
    data = st._load_json(path)
    rids = (data or {}).get("rids", []) if isinstance(data, dict) else []
    return {r for r in rids if isinstance(r, int) and not isinstance(r, bool)}


def find_gaps(target, vault_dir, reviews_dir=st.REVIEWS_DIR,
              captures_path=st.CAPTURES, targets_path=None,
              since=GAP_SINCE, limit=DEFAULT_LIMIT):
    """回収対象を選ぶ本体。戻り値は dict（main が JSON化・STATUS整形する）。"""
    rids_in_vault, note_count, problem = vault_rids(vault_dir)
    base = {"target": target, "since": since, "limit": limit,
            "vault_notes": note_count, "gaps": 0, "records": []}
    if problem:
        base["status"] = "vault_unreadable"
        base["reason"] = problem
        return base

    rid_dates, _url, _pid, unreadable = st.card_index(reviews_dir)
    exclude = _load_target_rids(targets_path)
    gaps = []
    for rid, dates in rid_dates.items():
        published = sorted(d for d in dates
                           if st.DATE_RE.match(d) and d != target and d >= since)
        if not published:
            continue
        if rid in rids_in_vault or rid in exclude:
            continue
        gaps.append((published[0], rid))
    gaps.sort()

    caps = captures_by_rid(captures_path)
    records = []
    for review_date, rid in gaps[:max(0, limit)]:
        rec = caps.get(rid) or {}
        card = card_material(reviews_dir, review_date, rid)
        rec_date = rec.get("date") if isinstance(rec.get("date"), str) else ""
        from_caps = bool(rec_date) and st.DATE_RE.match(rec_date[:10]) is not None
        records.append({
            "rid": rid,
            "date": rec_date[:10] if from_caps else review_date,
            "date_from": "captures" if from_caps else "review",
            "source": rec.get("source") or card.get("href", ""),
            "title": rec.get("title") or card.get("data_title") or card.get("vtitle", ""),
            "note": rec.get("note") or "",
            "cluster": rec.get("cluster") or "",
            "tags": rec.get("tags") or [],
            "review_date": review_date,
            "vtitle": card.get("vtitle", ""),
            "vdesc": card.get("vdesc", ""),
        })
    base.update(status="ok", gaps=len(gaps), records=records,
                unreadable_reviews=unreadable)
    return base


def _write_out(path, payload):
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")
    os.replace(tmp, path)


def _status_line(r, error=None):
    line = ("NOTE_GAPS: target=%s status=%s gaps=%d selected=%d vault_notes=%d "
            "since=%s limit=%d" % (r.get("target"), r.get("status"), r.get("gaps", 0),
                                   len(r.get("records", [])), r.get("vault_notes", 0),
                                   r.get("since"), r.get("limit", 0)))
    if r.get("reason"):
        line += " reason=%s" % r["reason"]
    if r.get("unreadable_reviews"):
        line += " warn=unreadable_reviews:%d" % len(r["unreadable_reviews"])
    if error:
        line += " error=%s" % error
    return line


def main(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--target", default=None)
    parser.add_argument("--vault-dir", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--targets", default=None)
    parser.add_argument("--reviews-dir", default=st.REVIEWS_DIR)
    parser.add_argument("--captures", default=st.CAPTURES)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--since", default=GAP_SINCE)
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        if e.code in (0, None):  # --help
            raise
        # 不正な引数（argparse は SystemExit(2) で終わる）。行を出して無人Routineを止めない。
        print("NOTE_GAPS: target=unknown status=error gaps=0 selected=0 vault_notes=0 "
              "since=%s limit=0 error=bad_args" % GAP_SINCE)
        return 0
    # 形式の違う --target（TARGET_OVERRIDE の打ち間違い等）では、今夜の reviews を
    # 除外できなくなる。手順1と同じく無視して昨日にする。
    target = args.target if st.DATE_RE.match(args.target or "") else st._default_target()
    since = args.since if st.DATE_RE.match(args.since or "") else GAP_SINCE
    if args.out:  # 書けなかったときに前回の出力が使われないよう、先に消す
        try:
            os.remove(args.out)
        except OSError:
            pass
    try:
        result = find_gaps(target, args.vault_dir, reviews_dir=args.reviews_dir,
                           captures_path=args.captures, targets_path=args.targets,
                           since=since, limit=args.limit)
        error = None
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        result = {"target": target, "since": since, "limit": args.limit,
                  "status": "error", "gaps": 0, "vault_notes": 0, "records": []}
        error = type(e).__name__
    if args.out:
        try:
            _write_out(args.out, {k: result.get(k) for k in
                                  ("target", "status", "since", "limit", "gaps", "records")})
        except Exception as e:  # 書けなくても STATUS 行は出す。ただし ok とは言わない
            traceback.print_exc(file=sys.stderr)
            error = error or ("out_" + type(e).__name__)
            result = dict(result, status="error", records=[])
    elif result.get("records"):
        print(json.dumps(result["records"], ensure_ascii=False, indent=1))
    print(_status_line(result, error))
    return 0


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print("NOTE_GAPS: target=unknown status=error gaps=0 selected=0 vault_notes=0 "
              "since=%s limit=0 error=%s" % (GAP_SINCE, type(e).__name__))
    sys.stdout.flush()
    sys.exit(0)
