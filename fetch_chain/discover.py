#!/usr/bin/env python3
"""discover — 壊れた取り方の代わりを探して足す（fetch-chain の自動更新・2026-10-30）。標準ライブラリのみ。

  python discover.py run           # 壊れた系統に候補を試して試用で足す・試用を本番に上げる。出来事を1行ずつ出す
  python discover.py wheel <名前> [--approve]
                                   # candidates.json の wheel 候補を落として vet.py で検査し、clean なら隔離 venv に入れる。
                                   # needs_review は入れずに Codex 向けの確認依頼を書く。本人が読みを見て --approve を付けたときだけ入れる

流れ（2026-10-04 の本人決定・[[2026-10-03-fetch-chain-prototype]] の追補）:
  きっかけ = 毎日の canary で、ある手段が同じ場所で DEMOTE_AFTER 回続けて失敗
  窓口型（URL に問い合わせるだけ・他人のコードは動かない）= 完全自動: 仕様の検査（validate_spec）→ 見本で実測 → 試用で足す
  プログラム型（wheel）= 版と sha256 を固定して落とす → vet.py → clean だけ隔離 venv へ。needs_review は Codex に読ませる依頼を書く。
    reject は入れない。sdist は扱わない。入れた wheel を手段として使うには、呼び出し方を人がコードに書く（自動では配線しない）
  昇格 = 試用の手段が同じ場所で PROMOTE_AFTER 回続けて canary に通る
足した手段は STATE/plugins/ に置く（リポジトリの main には書かない。夜間ランが毎晩 push する main とぶつけないため）。
"""
import sys, os, json, hashlib, pathlib, shutil, subprocess, tempfile, datetime, zipfile
import fetchchain as fc
import vet

HERE = pathlib.Path(__file__).resolve().parent
CANDIDATES = HERE / "candidates.json"
PROMOTE_AFTER = 7  # 試用から本番に上げるまでに続けて通る回数（毎日1回なら1週間）


def _now():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def _event(kind, **kw):
    """出来事は events.jsonl に残し、1行の文にして返す（Slack に流す）。URL は書かない。"""
    rec = {"ts": _now(), "env": fc.where_am_i(), "event": kind, **kw}
    fc.STATE.mkdir(parents=True, exist_ok=True)
    with (fc.STATE / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return "%s %s" % (kind, " ".join("%s=%s" % (k, v) for k, v in kw.items()))


def _canary_rows(method):
    """この場所の canary の結果（古い順）。"""
    if not fc.HEALTH.exists():
        return []
    here = fc.place_class(fc.where_am_i())
    out = []
    for line in fc.HEALTH.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("kind") == "canary" and r.get("method") == method and fc.place_class(r.get("env", "pc")) == here:
            out.append(bool(r.get("ok")))
    return out


def broken_channels(channels):
    """この場所で、ある手段が DEMOTE_AFTER 回続けて canary に落ちている系統 → その手段名。"""
    out = {}
    for ch, cfg in channels.items():
        for m in cfg["methods"]:
            if not fc.usable_here(cfg, m):
                continue
            rows = _canary_rows(m)[-fc.DEMOTE_AFTER:]
            if len(rows) == fc.DEMOTE_AFTER and not any(rows):
                out.setdefault(ch, []).append(m)
    return out


def _load_candidates():
    if not CANDIDATES.exists():
        return []
    return json.loads(CANDIDATES.read_text(encoding="utf-8")).get("candidates", [])


def try_window_candidates(channels, broken):
    """壊れた系統ごとに、まだ入っていない窓口型の候補を見本で試し、通ったものを試用で足す。"""
    lines = []
    fc.AUTO_PLUGINS.mkdir(parents=True, exist_ok=True)
    for ch, dead in broken.items():
        for cand in _load_candidates():
            if cand.get("kind") != "http" or cand.get("channel") != ch or cand.get("name") in fc.METHODS:
                continue
            spec = dict(cand, status="trial", where=[fc.place_class(fc.where_am_i())])
            errs = fc.validate_spec(spec, channels)
            if errs:
                lines.append(_event("candidate_invalid", channel=ch, name=cand.get("name"), why="; ".join(errs)[:120]))
                continue
            cfg = channels[ch]
            try:
                fc._check_public(cfg["canary"]["url"])  # 内部アドレスを候補の窓口に渡さない
                data = fc.make_http_method(spec)(cfg["canary"]["url"])
                if cfg["canary"]["expect"].lower() not in (data.get("text") or "").lower():
                    raise fc.FetchError("期待する文字列が無い")
            except fc.FetchError as e:
                lines.append(_event("candidate_failed", channel=ch, name=spec["name"], why=fc._scrub(str(e))))
                continue
            (fc.AUTO_PLUGINS / (spec["name"] + ".json")).write_text(
                json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
            lines.append(_event("added_trial", channel=ch, name=spec["name"], replaces=",".join(dead)))
    return lines


def promote_trials():
    """試用の手段が、この場所で PROMOTE_AFTER 回続けて canary に通っていれば本番に上げる。"""
    lines = []
    if not fc.AUTO_PLUGINS.is_dir():
        return lines
    for p in sorted(fc.AUTO_PLUGINS.glob("*.json")):
        try:
            spec = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if spec.get("status") != "trial":
            continue
        rows = _canary_rows(spec.get("name"))[-PROMOTE_AFTER:]
        if len(rows) == PROMOTE_AFTER and all(rows):
            spec["status"] = "active"
            p.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
            lines.append(_event("promoted", channel=spec.get("channel"), name=spec.get("name"), after=PROMOTE_AFTER))
    return lines


def run():
    channels = fc.load_registry()
    broken = broken_channels(channels)
    return try_window_candidates(channels, broken) + promote_trials()


# ---------- プログラム型（wheel） ----------
def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def wheel(name, pip_download=None, make_venv=None, approve=False):
    """候補の wheel を版と sha256 を固定して落とし、vet.py を通して、clean なら隔離 venv に入れる。"""
    cand = next((c for c in _load_candidates() if c.get("kind") == "wheel" and c.get("name") == name), None)
    if not cand or not all(cand.get(k) for k in ("package", "version", "sha256")):
        return [_event("wheel_invalid", name=name, why="候補に package・version・sha256 が揃っていない")]
    with tempfile.TemporaryDirectory() as tmp:
        # --only-binary で wheel しか落とさない（sdist は入れた瞬間に setup.py が動くので扱わない）。依存は1つずつ別の候補にする
        cmd = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--no-deps", "--no-cache-dir",
               "-d", tmp, "%s==%s" % (cand["package"], cand["version"])]
        rc = (pip_download or (lambda c: subprocess.run(c, capture_output=True, timeout=300).returncode))(cmd)
        wheels = list(pathlib.Path(tmp).glob("*.whl"))
        if rc != 0 or len(wheels) != 1:
            return [_event("wheel_download_failed", name=name, rc=rc, n=len(wheels))]
        whl = wheels[0]
        if _sha256(whl) != cand["sha256"].lower():
            return [_event("wheel_hash_mismatch", name=name)]
        rep = vet.vet(whl)
        if rep["verdict"] == "reject":
            return [_event("wheel_rejected", name=name, rules=",".join(sorted({f["rule"] for f in rep["findings"]
                                                                                if f["level"] == vet.FAIL})))]
        if rep["verdict"] == "needs_review" and not approve:
            # Codex に読ませる依頼（該当の行を必ず引かせる）。読んだ結果は人か次の段が見て、入れるかを決める
            review = fc.STATE / "review"
            review.mkdir(parents=True, exist_ok=True)
            req = review / ("%s-%s.md" % (cand["package"], cand["version"]))
            kept = review / whl.name   # 読む側が実物を開けるように、ハッシュを確かめた wheel を横に置く
            shutil.copyfile(whl, kept)
            req.write_text(_review_request(cand, rep, kept), encoding="utf-8")
            return [_event("wheel_needs_review", name=name, request=req.name)]
        venv = fc.STATE / "venvs" / name
        (make_venv or _make_venv)(venv, whl)
        return [_event("wheel_installed", name=name, version=cand["version"],
                       approved_by_person="yes" if rep["verdict"] == "needs_review" else "not_needed")]


def _make_venv(venv, whl):
    subprocess.run([sys.executable, "-m", "venv", "--clear", str(venv)], check=True, timeout=300)
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # 隔離: 依存を引かない・索引を見ない（落とした1つだけ）・環境変数の鍵を渡さない
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")}
    subprocess.run([str(py), "-m", "pip", "install", "--no-deps", "--no-index", str(whl)],
                   check=True, timeout=300, env=env)


def _excerpt(whl, path, line, around=3):
    """wheel の中の該当行の前後（Codex が wheel を持たずに読めるように）。"""
    try:
        with zipfile.ZipFile(whl) as z:
            lines = z.read(path).decode("utf-8", "replace").splitlines()
    except (KeyError, OSError, zipfile.BadZipFile):
        return ""
    lo, hi = max(0, line - 1 - around), min(len(lines), line + around)
    return "\n".join("%5d| %s" % (i + 1, lines[i]) for i in range(lo, hi))


def _review_request(cand, rep, whl=None):
    rows = "\n".join("- `%s:%s` [%s] %s\n```\n%s\n```" % (f["file"], f["line"], f["rule"], f["msg"],
                                                       _excerpt(whl, f["file"], f["line"]) if whl and f["line"] else f["snippet"])
                     for f in rep["findings"])
    return ("# 入れる前の確認依頼: %s %s（sha256 %s）\n\n"
            "wheel の実物（zip）はこの依頼と同じフォルダの `%s`（sha256 は上と一致を確認済み）。"
            "下の抜粋もそこから取った。\n"
            "vet.py が needs_review にした。下の各行を wheel の中で実際に開き、該当のコードを**行ごとに逐語で引用して**、"
            "危険（外部への送信・鍵やブラウザ保存データの読み取り・任意のコマンド実行・常駐）かを判定する。"
            "引用できない主張は書かない。最後に「入れてよい／入れない」と、その理由を1行で。\n\n%s\n"
            % (cand["package"], cand["version"], cand["sha256"], pathlib.Path(whl).name if whl else "（無し）", rows))


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(argv) >= 2 and argv[1] == "run":
        lines = run()
    elif len(argv) >= 3 and argv[1] == "wheel":
        lines = wheel(argv[2], approve="--approve" in argv[3:])
    else:
        print(__doc__)
        return 64
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
