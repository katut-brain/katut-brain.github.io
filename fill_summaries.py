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
    # summary="" のエントリを収集
    targets = [(i, e) for i, e in enumerate(data) if e.get("summary", "") == ""]
    total_empty = len(targets)

    if test_mode:
        targets = targets[:TEST_LIMIT]
        print(f"[TEST モード] 先頭{TEST_LIMIT}件を対象（全 summary空: {total_empty}件 / 総件数: {total_all}件）")
    else:
        print(f"[開始] summary空: {total_empty}件 / 総件数: {total_all}件")

    done = 0
    skip = 0
    skip_reasons = {}

    for seq, (idx, entry) in enumerate(targets, 1):
        url = entry.get("source", "")
        try:
            result = fetch_content(url)
            text = result.get("text", "") if result.get("ok") else ""
            if text:
                data[idx]["summary"] = text
                done += 1
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
    print(f"[完了] 補完 {done}件 / skip {skip}件")
    if skip_reasons:
        print("[skip 内訳]")
        for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}件")
    print(f"[確認] captures.json 総件数: {len(data)}件")


if __name__ == "__main__":
    main()
