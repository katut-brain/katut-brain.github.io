#!/usr/bin/env python3
# fill_summaries.py — captures.json の summary="" エントリを fetch_content で埋める
#
# 使い方:
#   python3 fill_summaries.py                     # 全件実行（summary="" または depth="partial"）
#   python3 fill_summaries.py --test              # 先頭5件のみ（動作確認用）
#   python3 fill_summaries.py --targets rids.txt   # rids.txt に列挙した rid のみ対象
#                                                  # （1行1rid。グローバルな summary/depth 条件は無視）

import sys, os, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_content import fetch_content

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CAPTURES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures.json")

# missing タイプとその対処戦略。
# 既知の戦略がある／ない／実装済みを問わず、ここで一元管理する。
# 将来 visual_content の戦略が実装されたら値を更新するだけでレポートに反映される。
KNOWN_STRATEGIES = {
    "article_body":       "t.co展開（実装済み・X内コンテンツはスキップ対象）",
    "article_body_tail":  "本文が上限で切れている。WEB_BODY_CAP を上げれば伸びる（実装済み）",
    "visual_content":     "未実装（Meta oEmbed トークン必要 or Claude vision）",
    "audio_content":      "なし",
    "transcript":         "youtube-transcript-api（実装済み・字幕なし動画は取得不可）",
    "video_content":      "Gemini動画理解（実装済み・Instagram Reelは中継サービス障害で現在ほぼ全滅）",
    "image_content":      "Routine側でダウンロードしてReadで読む（再取得では回復しない）",
}

# 再取得で回復し得る欠損。ここに載っている欠損を持つレコードは、
# 要約が埋まっていても再取得の対象にする（一過性の失敗を焼き付けないため）。
# visual_content / audio_content / image_content は、この経路で再取得しても
# 回復しないので入れない（毎晩の無駄打ちになる）。
RETRYABLE_MISSING = {"article_body", "article_body_tail", "transcript", "video_content"}

# 回路遮断器。同じレコードを何度取り直しても回復しないなら諦める。
# 無人ジョブなので、放っておくと恒久失敗を毎晩叩き続けて時間と外部APIを浪費する。
# 試行回数はレコードの `fetch_attempts` に貯める。
MAX_FETCH_ATTEMPTS = 5


def is_retry_target(e):
    """このレコードをもう一度取りに行く価値があるか。"""
    if e.get("fetch_attempts", 0) >= MAX_FETCH_ATTEMPTS:
        return False                      # 回路遮断
    if e.get("summary", "") == "":
        return True                       # そもそも未取得
    depth = e.get("depth")
    if depth is None:
        return True                       # 深度未記録（旧レコード）
    if depth in ("partial", "shallow"):
        # 回復手段のある欠損を持つものだけ。image_content のように
        # この経路では直らない欠損しか無いものは、何度やっても同じ。
        return any(m in RETRYABLE_MISSING for m in (e.get("missing") or []))
    return False
TEST_LIMIT = 5

def classify_skip(url, result):
    """skip 理由を分類する文字列を返す"""
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "YouTube字幕なし/取得失敗"
    if "instagram.com" in host:
        return "IG壁/og失敗"
    if "x.com" in host or "twitter.com" in host:
        return "X失敗"
    if "threads.com" in host or "threads.net" in host:
        return "Threads失敗"
    return "Web失敗"


def parse_targets_file(path):
    """--targets で指定されたファイルから rid のリストを読む。
    1行1rid（改行区切り）のプレーンテキスト。空行・#始まりのコメント行は無視。"""
    rids = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rids.append(int(line))
    return rids


def main():
    test_mode = "--test" in sys.argv

    targets_path = None
    if "--targets" in sys.argv:
        idx = sys.argv.index("--targets")
        try:
            targets_path = sys.argv[idx + 1]
        except IndexError:
            print("ERROR: --targets にはファイルパスを指定してください")
            sys.exit(1)

    # captures.json を読む
    with open(CAPTURES_PATH, encoding="utf-8") as f:
        data = json.load(f)

    total_all = len(data)

    if targets_path:
        # --targets 指定時: グローバルな summary/depth 条件は無視し、
        # 指定された rid のレコードのみを対象にする（機能Bへの影響ゼロ）。
        wanted_rids = set(parse_targets_file(targets_path))
        targets = [(i, e) for i, e in enumerate(data) if e.get("rid") in wanted_rids]
        found_rids = {e.get("rid") for _, e in targets}
        missing_rids = wanted_rids - found_rids
        if missing_rids:
            print(f"WARN: --targets に指定された rid のうち captures.json に見つからないもの: {sorted(missing_rids)}")
        print(f"[--targets モード] 指定rid: {len(wanted_rids)}件 / captures.json内で一致: {len(targets)}件 / 総件数: {total_all}件")
    else:
        # 既定動作: 「まだ良くなる余地があるもの」を収集する。
        #
        # 旧条件は summary=="" または depth=="partial" の2つだけだった。この網では
        # **shallow かつ要約が埋まっているもの（Instagram通常投稿・Threads・字幕なし
        # YouTube）が永久に再取得対象にならない**（2026-08-23の監査で判明）。
        # 一過性の失敗で shallow になった件も、要約さえ埋まっていれば二度と拾われない。
        # depth フィールドを持たない古いレコードも同様に漏れていた。
        # 旧条件は summary=="" または depth=="partial" の2つだけで、
        #   ・shallow かつ要約が埋まっているものが永久に対象外
        #   ・回復手段の無い欠損しか持たない partial を毎晩叩き続ける
        # の両方を抱えていた。判定は is_retry_target() に一本化する。
        targets = [(i, e) for i, e in enumerate(data) if is_retry_target(e)]
    total_targets = len(targets)

    # 内訳を表示
    empty_count = sum(1 for _, e in targets if e.get("summary", "") == "")
    partial_count = sum(1 for _, e in targets if e.get("depth") == "partial" and e.get("summary", "") != "")

    if test_mode:
        targets = targets[:TEST_LIMIT]
        print(f"[TEST モード] 先頭{TEST_LIMIT}件を対象（summary空: {empty_count}件, depth=partial: {partial_count}件 / 総件数: {total_all}件）")
    elif not targets_path:
        print(f"[開始] 対象: {total_targets}件（summary空: {empty_count}件, depth=partial: {partial_count}件）/ 総件数: {total_all}件")

    done = 0
    skip = 0
    upgraded = 0  # partial → full に昇格した件数
    skip_reasons = {}

    for seq, (idx, entry) in enumerate(targets, 1):
        url = entry.get("source", "")
        was_partial = entry.get("depth") == "partial"
        # 試行回数は成否に関わらず数える（回復しないものを毎晩叩き続けないため）
        data[idx]["fetch_attempts"] = data[idx].get("fetch_attempts", 0) + 1
        try:
            result = fetch_content(url)
            text = result.get("text", "") if result.get("ok") else ""
            if text:
                # 再取得の結果で無条件に上書きすると、一時的なメタ説明・
                # 取得制限下の短い文面で、より良かった既存要約を失う。
                # 深度が上がったか、内容が増えたときだけ差し替える。
                RANK = {None: 0, "": 0, "shallow": 1, "partial": 2, "full": 3}
                old_text = data[idx].get("summary", "") or ""
                new_depth = result.get("depth", "full")
                improved = (RANK.get(new_depth, 0) > RANK.get(data[idx].get("depth"), 0)
                            or len(text) >= len(old_text))
                if improved:
                    data[idx]["summary"] = text
                    data[idx]["depth"] = new_depth
                    data[idx]["missing"] = result.get("missing", [])
                else:
                    print(f"[保持] 既存要約のほうが長いので据え置き "
                          f"(既存{len(old_text)}字 > 新{len(text)}字): {url[:50]}")
                done += 1
                new_depth = result.get("depth", "full")
                if was_partial and new_depth == "full":
                    upgraded += 1
                    print(f"[進捗] {done}/{len(targets)} 完了, {skip} skip  — UPGRADED(partial→full): {url[:60]}")
                else:
                    print(f"[進捗] {done}/{len(targets)} 完了, {skip} skip  — {url[:60]}")
            else:
                reason = classify_skip(url, result)
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                skip += 1
                print(f"[進捗] {done}/{len(targets)} 完了, {skip} skip  — SKIP({reason}): {url[:60]}")
        except Exception as e:
            reason = "例外: " + str(e)[:40]
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            skip += 1
            print(f"[進捗] {done}/{len(targets)} 完了, {skip} skip  — EXCEPTION: {url[:60]}")

        # atomic write（処理のたびに保存して破損リスクを最小化）
        tmp_path = CAPTURES_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CAPTURES_PATH)

    print()
    print(f"[完了] 補完 {done}件 / skip {skip}件 / partial→full 昇格 {upgraded}件")
    if skip_reasons:
        print("[skip 内訳]")
        for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}件")
    print(f"[確認] captures.json 総件数: {len(data)}件")

    # depth 統計
    depth_stats = {}
    for e in data:
        d = e.get("depth", "none")
        depth_stats[d] = depth_stats.get(d, 0) + 1
    print("[depth 統計]")
    for d, count in sorted(depth_stats.items()):
        print(f"  {d}: {count}件")

    # ---- ギャップレポート ----
    # captures.json 全件の missing フィールドを集計する
    missing_type_entries = {}  # type -> [(url, depth), ...]
    upgraded_to_full_count = upgraded  # main ループで計上済み

    for e in data:
        for m_type in e.get("missing", []):
            if m_type not in missing_type_entries:
                missing_type_entries[m_type] = []
            missing_type_entries[m_type].append((e.get("source", ""), e.get("depth", "unknown")))

    known_types = {k: v for k, v in missing_type_entries.items() if k in KNOWN_STRATEGIES}
    unknown_types = {k: v for k, v in missing_type_entries.items() if k not in KNOWN_STRATEGIES}

    # ドメイン別の説明ラベル（件数表示の補足用）
    _DOMAIN_LABEL = {
        "visual_content": "Instagram画像・Reels・Threadsビジュアル",
        "audio_content":  "Instagram Reels・Threads動画",
        "transcript":     "YouTube字幕なし動画",
        "article_body":   "X内コンテンツ=動画/画像リンク",
    }

    print()
    print("=" * 60)
    print("[ギャップレポート]")
    print(f"改善済み（depth: partial→full に昇格）: {upgraded_to_full_count}件")
    print()

    if known_types:
        print("改善不可（既知）:")
        for m_type, entries in sorted(known_types.items()):
            label = _DOMAIN_LABEL.get(m_type, m_type)
            strategy = KNOWN_STRATEGIES[m_type]
            print(f"  - {m_type}: {len(entries)}件 [{label}]")
            print(f"      → 戦略: {strategy}")
    else:
        print("改善不可（既知）: 0件")

    print()
    if unknown_types:
        print("未知の missing タイプ（戦略要研究）:")
        for m_type, entries in sorted(unknown_types.items()):
            sample_urls = [url for url, _ in entries[:3]]
            print(f"  - {m_type}: {len(entries)}件")
            for su in sample_urls:
                print(f"      URL例: {su[:80]}")
    else:
        print("未知の missing タイプ: 0件（全タイプ既知）")

    print("=" * 60)


if __name__ == "__main__":
    main()
