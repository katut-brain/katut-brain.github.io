#!/usr/bin/env python3
"""vet — 他人が書いた Python 製の道具を「入れる前に」調べる門番（試作 v0・標準ライブラリのみ）。

  python vet.py <wheel(.whl)かフォルダ> ...   # 検査して JSON を出す。終了コード 0=clean 1=needs_review 2=reject

判定:
  reject        危険の兆候がある（1つでもあれば入れない）
  needs_review  機械では白黒つけられない箇所がある（次の段階で AI が該当箇所を読む。それまで入れない）
  clean         機械が見る範囲では兆候なし（安全の保証ではない）

方針:
  - ファイルは展開しない。zip の中身をメモリで読むだけ（検査自体が危険にならないように）
  - 「危険なもの」ではなく「普通の道具が持たない形」を見る。普通の道具が誤って止まっても、別の候補を探せば足りる
  - 入れるのは wheel のみ。sdist は入れた瞬間に setup.py が動くので、この門の前提から外す
"""
import ast, io, json, math, pathlib, re, sys, warnings, zipfile

FAIL, WARN = "FAIL", "WARN"
MAX_PY_BYTES = 2_000_000
LIMITS = [
    "静的検査は安全の保証ではない（実行時に組み立てられるコード・ネットワーク越しに後から落とすコードは見えない）",
    "検査したのは Python のソースと同梱ファイル名だけ。.pyd/.so などのバイナリの中身は見ていない",
    "依存パッケージは、それぞれ別に検査して初めて意味がある（この関数は渡された1つだけを見る）",
]

DECODERS = {"base64", "zlib", "marshal", "codecs", "binascii", "bz2", "lzma", "gzip"}
NETWORK = {"socket", "urllib", "urllib2", "requests", "httpx", "aiohttp", "http", "ftplib", "smtplib",
           "telnetlib", "websocket", "websockets", "paramiko"}
# 文字列は「パスの形をした短いもの」だけを見る（説明文・URL の中の単語に反応しない）
_SEP = r"(?:^|[/\\~])"
_SL = r"[/\\]"  # パス区切り（/ か \）
SECRET_PATTERNS = [_SEP + r"\.ssh(?:" + _SL + r"|$)", _SEP + r"\.aws(?:" + _SL + r"|$)", _SEP + r"\.gnupg(?:" + _SL + r"|$)",
                   r"\bid_(?:rsa|ed25519|ecdsa|dsa)\b", _SEP + r"\.kube(?:" + _SL + r"|$)", _SEP + r"\.docker" + _SL + r"config",
                   _SEP + r"\.git-credentials$", r"(?:^|" + _SL + r")(?:Login Data|Web Data|Local State|Cookies)$",
                   r"wallet\.dat$", r"Microsoft" + _SL + r"+Credentials", r"Library" + _SL + r"Keychains",
                   _SEP + r"\.config" + _SL + r"gcloud"]
# 通信ライブラリが標準機能として読むことがある資格情報ファイル（reject にはせず要確認）
CRED_FILE_PATTERNS = [_SEP + r"\.netrc$", _SEP + r"\.pypirc$", _SEP + r"\.npmrc$"]
EXFIL_PATTERNS = [r"discord(?:app)?\.com/api/webhooks", r"api\.telegram\.org/bot", r"transfer\.sh", r"webhook\.site",
                  r"requestbin", r"ngrok\.(?:io|app|dev)", r"anonfiles", r"hooks\.slack\.com", r"pastebin\.com/(?:api|post)"]
IP_URL_PATTERNS = [r"https?://\d{1,3}(?:\.\d{1,3}){3}"]
PERSIST_PATTERNS = [r"CurrentVersion" + _SL + r"+Run", r"Start Menu" + _SL + r"+Programs" + _SL + r"+Startup",
                    r"\bschtasks(?:\.exe)?\s+/", r"\bcrontab\s+-", _SEP + r"\.(?:bashrc|zshrc|bash_profile|profile)$",
                    r"Library" + _SL + r"LaunchAgents", r"/etc/rc\.local$", r"systemctl\s+enable"]
MAX_STR_FOR_PATH = 300
FAIL_EXT = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".scr", ".msi", ".com", ".jar", ".hta"}
WARN_EXT = {".dll", ".pyd", ".so", ".dylib"}


def _f(rule, level, path, line, msg, snippet=""):
    return {"rule": rule, "level": level, "file": path, "line": line, "msg": msg, "snippet": snippet.strip()[:100]}


def _call_name(node):
    """Call の関数名をドット連結で返す（a.b.c）。取れなければ ''。"""
    f = node.func
    parts = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
        return ".".join(reversed(parts))
    return ""


def _imports(tree):
    mods, from_names = set(), {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            top = n.module.split(".")[0]
            mods.add(top)
            for a in n.names:
                from_names[a.asname or a.name] = n.module + "." + a.name
    return mods, from_names


def _docstring_nodes(tree):
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                ids.add(id(b[0].value))
    return ids


def _strings(tree):
    """(行, 文字列) を返す。説明文(docstring)と長すぎる文字列は除く。f-string の固定部分も含む。"""
    skip = _docstring_nodes(tree)
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip                 and len(n.value) <= MAX_STR_FOR_PATH:
            yield n.lineno, n.value


def _entropy(s):
    if not s:
        return 0.0
    freq = {c: s.count(c) for c in set(s)}
    return -sum(v / len(s) * math.log2(v / len(s)) for v in freq.values())


# ---------- ルール（それぞれ ctx を受け取り Finding のリストを返す） ----------
def r_pth_code(ctx):
    """.pth の import 行は、Python を起動するたびに実行される（インストール後に自動で動く経路）。"""
    out = []
    if ctx["path"].endswith(".pth"):
        for i, line in enumerate(ctx["text"].splitlines(), 1):
            s = line.strip()
            if s and not s.startswith("#") and re.match(r"import\b", s):
                out.append(_f("pth_code", FAIL, ctx["path"], i, ".pth に実行される行がある（起動のたびに動く）", s))
    return out


def r_exec_obfuscated(ctx):
    """動的実行（eval/exec/compile/__import__）と、復号・展開の部品が同じファイルにある。"""
    tree = ctx["tree"]
    if tree is None:
        return []
    mods, _ = _imports(tree)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _call_name(n) in ("eval", "exec", "compile", "__import__", "builtins.eval", "builtins.exec"):
            if n.args and isinstance(n.args[0], ast.Constant):
                continue
            if mods & DECODERS:
                out.append(_f("exec_obfuscated", FAIL, ctx["path"], n.lineno,
                              "動的実行と復号部品(%s)が同じファイルにある" % ",".join(sorted(mods & DECODERS)), _call_name(n)))
            else:
                out.append(_f("dynamic_exec", WARN, ctx["path"], n.lineno, "中身が定数でない eval/exec/compile", _call_name(n)))
    return out


def _match_any(patterns, tree, text, flags=0):
    hits = []
    if tree is not None:
        for line, s in _strings(tree):
            for p in patterns:
                if re.search(p, s, flags):
                    hits.append((line, s[:100]))
                    break
    else:
        for i, l in enumerate(text.splitlines(), 1):
            for p in patterns:
                if re.search(p, l, flags):
                    hits.append((i, l[:100]))
                    break
    return hits


def _lvl(ctx):
    """テスト置き場の中の兆候は reject にせず要確認に下げる（使うときに読み込まれにくいため。見逃しではなく AI の確認に回す）。"""
    return WARN if ctx.get("in_tests") else FAIL


def r_secret_paths(ctx):
    out = [_f("secret_paths", _lvl(ctx), ctx["path"], ln, "資格情報・ブラウザの保存データの場所を指す文字列", s)
           for ln, s in _match_any(SECRET_PATTERNS, ctx["tree"], ctx["text"])]
    out += [_f("cred_file", WARN, ctx["path"], ln, "資格情報ファイルを読む（通信ライブラリの標準機能のこともある）", s)
            for ln, s in _match_any(CRED_FILE_PATTERNS, ctx["tree"], ctx["text"])]
    return out


def r_exfil_endpoints(ctx):
    out = [_f("exfil_endpoints", _lvl(ctx), ctx["path"], ln, "データの送り先に使われがちな宛先(webhook・匿名置き場等)", s)
           for ln, s in _match_any(EXFIL_PATTERNS, ctx["tree"], ctx["text"], re.I)]
    out += [_f("ip_url", WARN, ctx["path"], ln, "IP アドレス直指定の URL", s)
            for ln, s in _match_any(IP_URL_PATTERNS, ctx["tree"], ctx["text"], re.I)]
    return out


def r_persistence(ctx):
    return [_f("persistence", _lvl(ctx), ctx["path"], ln, "自動起動・常駐の登録に使う文字列", s)
            for ln, s in _match_any(PERSIST_PATTERNS, ctx["tree"], ctx["text"], re.I)]


def r_env_dump(ctx):
    """環境変数の一括読み出し。同じ関数(または同じモジュール直下)に通信呼び出しがあれば reject。"""
    tree = ctx["tree"]
    if tree is None:
        return []
    mods, from_names = _imports(tree)
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p

    def scope_of(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return tree

    def has_network_call(scope):
        for c in ast.walk(scope):
            if isinstance(c, ast.Call):
                name = _call_name(c)
                full = from_names.get(name, name)
                if full.split(".")[0] in NETWORK:
                    return True
        return False

    out = []
    for n in ast.walk(tree):
        is_env = (isinstance(n, ast.Attribute) and n.attr == "environ" and isinstance(n.value, ast.Name) and n.value.id == "os")             or (isinstance(n, ast.Name) and from_names.get(n.id) == "os.environ")
        if not is_env:
            continue
        par = parents.get(n)
        bulk = False
        if isinstance(par, ast.Attribute) and par.attr in ("items", "keys", "values", "copy"):
            bulk = True
        elif isinstance(par, ast.Call) and n in par.args:
            bulk = True
        elif isinstance(par, (ast.For, ast.comprehension)) and par.iter is n:
            bulk = True
        elif isinstance(par, (ast.Assign, ast.Return, ast.keyword, ast.Dict)):
            bulk = True
        if bulk:
            net = has_network_call(scope_of(n))
            out.append(_f("env_dump", FAIL if net else WARN, ctx["path"], n.lineno,
                          "環境変数を丸ごと読む" + ("（同じ関数に通信呼び出しがある）" if net else ""), "os.environ"))
    return out


def r_shell_exec(ctx):
    tree = ctx["tree"]
    if tree is None:
        return []
    _, from_names = _imports(tree)
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        name = _call_name(n)
        full = from_names.get(name, name)
        if not (full.startswith("subprocess.") or full in ("os.system", "os.popen") or full.startswith(("os.exec", "os.spawn"))):
            continue
        shell_true = any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in n.keywords)
        const_cmd = bool(n.args) and isinstance(n.args[0], (ast.Constant, ast.List, ast.Tuple)) and \
            all(isinstance(e, ast.Constant) for e in (n.args[0].elts if isinstance(n.args[0], (ast.List, ast.Tuple)) else [n.args[0]]))
        if (shell_true or full in ("os.system", "os.popen")) and not const_cmd:
            out.append(_f("shell_exec", FAIL, ctx["path"], n.lineno, "組み立てた文字列をシェルで実行", full))
        else:
            out.append(_f("subprocess", WARN, ctx["path"], n.lineno, "外部コマンドを起動する", full))
    return out


def r_risky_modules(ctx):
    tree = ctx["tree"]
    if tree is None:
        return []
    mods, _ = _imports(tree)
    out = []
    for m, why in (("ctypes", "任意のライブラリ・OS 機能を直接呼べる"), ("cffi", "任意のライブラリを直接呼べる"),
                   ("winreg", "Windows レジストリを読み書きできる"), ("_winreg", "Windows レジストリを読み書きできる"),
                   ("pickle", "読み込む相手次第でコードが動く"), ("marshal", "読み込む相手次第でコードが動く"),
                   ("shelve", "内部で pickle を使う"), ("pty", "疑似端末を扱う")):
        if m in mods:
            out.append(_f("risky_module", WARN, ctx["path"], 1, "%s を使う: %s" % (m, why), m))
    if "socket" in mods:
        out.append(_f("raw_socket", WARN, ctx["path"], 1, "生のソケット通信を使う", "socket"))
    return out


def r_blob(ctx):
    tree = ctx["tree"]
    if tree is None:
        return []
    out = []
    for ln, s in _strings(tree):
        if len(s) >= 300 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", s) and _entropy(s) > 4.5:
            out.append(_f("blob", WARN, ctx["path"], ln, "長い符号化らしい文字列（難読化の可能性）", s[:60]))
    return out


RULES = [r_pth_code, r_exec_obfuscated, r_secret_paths, r_exfil_endpoints, r_persistence,
         r_env_dump, r_shell_exec, r_risky_modules, r_blob]


# ---------- 走査 ----------
def scan_file(path, data):
    """1ファイル分(path は包み内の相対名・data は bytes)の Finding を返す。"""
    out = []
    ext = pathlib.PurePosixPath(path.replace("\\", "/")).suffix.lower()
    if ext in FAIL_EXT:
        return [_f("script_binary", FAIL, path, 0, "実行ファイル・スクリプトが同梱されている (%s)" % ext)]
    if ext in WARN_EXT:
        out.append(_f("native_binary", WARN, path, 0, "中身を検査できないバイナリ (%s)" % ext))
    if ext not in (".py", ".pth"):
        return out
    if len(data) > MAX_PY_BYTES:
        return out + [_f("too_large", WARN, path, 0, "大きすぎて未検査 (%d bytes)" % len(data))]
    text = data.decode("utf-8", "replace")
    tree = None
    if ext == ".py":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # 他人のコード内の不正な \エスケープ等の警告は検査結果に関係ない
                tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            out.append(_f("unparseable", WARN, path, 0, "Python として読めない（古い書き方か難読化）"))
    norm = "/" + path.replace("\\", "/").lower()
    in_tests = bool(re.search(r"/(tests?|testing)/", norm)) or pathlib.PurePosixPath(norm).name.startswith("test_")
    ctx = {"path": path, "text": text, "tree": tree, "in_tests": in_tests}
    for rule in RULES:
        out.extend(rule(ctx))
    return out


def _iter_entries(target):
    p = pathlib.Path(target)
    if p.is_dir():
        for f in sorted(p.rglob("*")):
            if f.is_file():
                yield f.relative_to(p).as_posix(), (lambda f=f: f.read_bytes()), f.stat().st_size
    elif p.suffix.lower() == ".whl":
        zf = zipfile.ZipFile(p)
        for info in zf.infolist():
            if not info.is_dir():
                yield info.filename, (lambda i=info: zf.open(i).read(MAX_PY_BYTES + 1)), info.file_size
    else:
        raise ValueError("wheel(.whl) かフォルダだけを受け付ける（sdist は入れない方針）: %s" % target)


def vet(target):
    findings, n_py = [], 0
    for name, read, size in _iter_entries(target):
        ext = pathlib.PurePosixPath(name).suffix.lower()
        if ext == ".py":
            n_py += 1
        if ext in (".py", ".pth") and size > MAX_PY_BYTES:
            findings.append(_f("too_large", WARN, name, 0, "大きすぎて未検査 (%d bytes)" % size))
            continue
        data = read() if (ext in (".py", ".pth")) else b""
        findings.extend(scan_file(name, data))
    if n_py == 0:
        findings.append(_f("no_python", WARN, str(target), 0, "Python のソースが1つも無い（バイナリだけの包み）"))
    verdict = "reject" if any(x["level"] == FAIL for x in findings) else ("needs_review" if findings else "clean")
    return {"target": str(target), "verdict": verdict, "py_files": n_py, "findings": findings, "limits": LIMITS}


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(argv) < 2:
        print(__doc__)
        return 64
    worst = 0
    for t in argv[1:]:
        rep = vet(t)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        worst = max(worst, {"clean": 0, "needs_review": 1, "reject": 2}[rep["verdict"]])
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv))
