"""健康診断の結果を、Slack に流す短い文にする（2026-10-31・本人「全然わからない・もっと簡潔に」）。

  python notify.py <status.json> [<discover の出力>]   # 知らせることが無ければ何も出さない
"""
import sys, json

NAMES = {"x_post": "X", "youtube": "YouTube", "instagram": "Instagram", "threads": "Threads", "web": "Web"}


def message(status, events=""):
    lines = []
    for ch, c in status.get("channels", {}).items():
        measured = [m for m in c.get("methods", []) if m.get("ok") is not None]
        bad = [m for m in measured if not m["ok"]]
        if not bad:
            continue
        name = NAMES.get(ch, ch)
        if len(bad) == len(measured):
            lines.append("🔴 %s：全部の取り方が壊れていて、今は読めない" % name)
        else:
            lines.append("🟡 %s：取り方が1つ壊れた（ほかの取り方で読めている）" % name if len(bad) == 1 else
                         "🟡 %s：取り方が%d個壊れた（ほかの取り方で読めている）" % (name, len(bad)))
    for e in events.splitlines():
        parts = dict(p.split("=", 1) for p in e.split()[1:] if "=" in p)
        name = NAMES.get(parts.get("channel"), parts.get("channel", ""))
        if e.startswith("added_trial"):
            lines.append("🔧 %s：代わりの取り方を見つけて、試しに使い始めた" % name)
        elif e.startswith("promoted"):
            lines.append("✅ %s：試していた代わりの取り方を、正式に使うことにした" % name)
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    status = json.load(open(sys.argv[1], encoding="utf-8"))
    events = open(sys.argv[2], encoding="utf-8").read() if len(sys.argv) > 2 else ""
    print(message(status, events))
