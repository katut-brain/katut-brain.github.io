#!/usr/bin/env python3
# fill_summaries.py — captures.json の summary="" エントリを fetch_content で埋める
#
# 使い方:
#   python3 fill_summaries.py          # 全件実行
#   python3 fill_summaries.py --test   # 先頭5件のみ（動作確認用）

import sys, os, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_content import fetch_content

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CAPTURES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures.json")

# missing タイプとその対処戦略。
# 既知の戦略がある／ない／実装済みを問わず、ここで一元管理する。
# 将来 visual_content の戦略が実装されたら値を更新するだけでレポートに反映される。
KNOWN_STRATEGIES = {
    "article_body":    "t.co展開（実装済み・X内コンテンツはスキップ対象）",
    "visual_content":  "未実装（Meta oEmbed トークン必要 or Claude vision）",
    "audio_content":   "なし",
    "transcript":      "youtube-transcript-api（実装済み・字幕なし動画は取得不可）",
}
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


def main():
    test_mode = "--test" in sys.argv

    # captures.json を読む
    with open(CAPTURES_PATH, encoding="utf-8") as f:
        data = json.load(f)

    total_all = len(data)
    # summary="" または depth="partial" のエントリを収集
    targets = [
        (i, e) for i, e in enumerate(data)
        if e.get("summary", "") == ""           # 未取得
        or e.get("depth") == "partial"          # 部分取得（upgrade 可能）
    ]
    total_targets = len(targets)

    # 内訳を表示
    empty_count = sum(1 for _, e in targets if e.get("summary", "") == "")
    partial_count = sum(1 for _, e in targets if e.get("depth") == "partial" and e.get("summary", "") != "")

    if test_mode:
        targets = targets[:TEST_LIMIT]
        print(f"[TEST モード] 先頭{TEST_LIMIT}件を対象（summary空: {empty_count}件, depth=partial: {partial_count}件 / 総件数: {total_all}件）")
    else:
        print(f"[開始] 対象: {total_targets}件（summary空: {empty_count}件, depth=partial: {partial_count}件）/ 総件数: {total_all}件")

    done = 0
    skip = 0
    upgraded = 0  # partial → full に昇格した件数
    skip_reasons = {}

    for seq, (idx, entry) in enumerate(targets, 1):
        url = entry.get("source", "")
        was_partial = entry.get("depth") == "partial"
        try:
            result = fetch_content(url)
            text = result.get("text", "") if result.get("ok") else ""
            if text:
                data[idx]["summary"] = text
                # depth と missing を書き戻す
                if result.get("ok"):
                    data[idx]["depth"] = result.get("depth", "full")
                    data[idx]["missing"] = result.get("missing", [])
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
