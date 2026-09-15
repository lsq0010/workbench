#!/usr/bin/env python3
"""代码搜索 —— 在多个仓库/目录里找内容，带上下文和文件类型过滤。

为什么做这个
  查"A 这个函数在哪些地方被调用过""这个字段名在哪些文件里出现"，
  一个个仓库 grep 太慢，而且不想搜进 Pods/build/DerivedData。
  这里一次搜多个目录，自动跳过构建产物，结果按文件分组、带行号上下文。

用系统自带的 grep（macOS 上是 BSD grep，支持 -rn --include），零依赖。
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("SEARCHPAD_HOME") or SCRIPT_DIR)
CONFIGFILE = os.path.join(HOME, "config.json")
PIDFILE = os.path.join(HOME, "searchpad.pid")
LOGFILE = os.path.join(HOME, "searchpad.log")
DEFAULT_PORT = 8898
HISTORY = os.path.join(HOME, "searches.jsonl")      # 只追加

# 默认不搜这些目录 —— 构建产物和依赖里全是噪音
SKIP_DIRS = ("Pods", "build", "DerivedData", "node_modules", ".git", "Carthage",
             "vendor", "__pycache__", ".build", "dist", "target", ".venv")

# 常见类型预设
PRESETS = {
    "全部文本": [],
    "代码": ["*.swift", "*.m", "*.h", "*.mm", "*.java", "*.kt", "*.py", "*.js", "*.ts",
             "*.tsx", "*.jsx", "*.go", "*.rs", "*.c", "*.cpp", "*.rb", "*.php"],
    "Swift": ["*.swift"],
    "配置": ["*.json", "*.yaml", "*.yml", "*.plist", "*.xml", "*.toml", "*.ini", "*.conf"],
    "文档": ["*.md", "*.txt", "*.rst"],
    "脚本": ["*.sh", "*.bash", "*.zsh", "*.py"],
    "前端": ["*.js", "*.ts", "*.tsx", "*.jsx", "*.vue", "*.html", "*.css", "*.scss"],
}


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_json(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return d if d is not None else {}


def write_json(p, d):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def cfg():
    c = read_json(CONFIGFILE, {})
    c.setdefault("roots", [])
    c.setdefault("case_sensitive", False)
    return c


def discover_roots(depth=2):
    """把桌面上的 git 仓库当作搜索根（一个仓库一个根）"""
    out = []
    base = os.path.expanduser("~/Desktop")
    if not os.path.isdir(base):
        return out
    b = base.rstrip("/").count("/")
    for cur, dirs, _ in os.walk(base):
        if cur.count("/") - b >= depth:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
        if ".git" in os.listdir(cur):
            out.append(cur)
            dirs[:] = []
    return sorted(set(out))


# ══════════════════════════════════════════════════════════════
# 搜索
# ══════════════════════════════════════════════════════════════

def build_grep_args(pattern, roots, includes, case_sensitive, regex, context, max_count):
    args = ["grep", "-rn", "--binary-files=without-match",
            "-m", str(max_count), "-C", str(context)]
    if not case_sensitive:
        args.append("-i")
    if not regex:
        args.append("-F")          # 非正则模式：当成普通字符串，避免 . * 被当元字符
    for inc in (includes or []):
        args.append("--include=" + inc)
    for d in SKIP_DIRS:
        args.append("--exclude-dir=" + d)
    args.append("--")
    args.append(pattern)
    args += list(roots)
    return args


LINE_RE = re.compile(r"^(.*?)([-:])(\d+)\2(.*)$")


def run_search(pattern, roots=None, includes=None, case_sensitive=False, regex=False,
               context=1, max_files=200, per_file=20, timeout=25):
    c = cfg()
    roots = roots or c["roots"]
    roots = [r for r in roots if os.path.isdir(r)]
    if not pattern.strip():
        return {"error": "搜索词是空的", "hits": []}
    if not roots:
        return {"error": "还没有设置搜索目录 —— 去「搜索范围」里加几个", "hits": []}

    t0 = time.time()
    args = build_grep_args(pattern, roots, includes, case_sensitive, regex, context, per_file)
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        out = p.stdout
        truncated = False
    except subprocess.TimeoutExpired:
        return {"error": f"搜索超过 {timeout} 秒被中断 —— 缩小范围或加文件类型过滤",
                "hits": [], "seconds": round(time.time() - t0, 2)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "hits": []}

    files = {}
    for line in out.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        path, _, lineno, text = m.group(1), m.group(2), m.group(3), m.group(4)
        f = files.setdefault(path, {"path": path, "name": os.path.basename(path),
                                    "dir": os.path.dirname(path), "lines": []})
        f["lines"].append({"n": int(lineno), "text": text[:400],
                           "hit": pattern.lower() in text.lower() if not case_sensitive
                                  else pattern in text})
    items = sorted(files.values(), key=lambda x: -len(x["lines"]))
    if len(items) > max_files:
        items = items[:max_files]
        truncated = True
    for f in items:
        # 按仓库归类
        f["repo"] = next((os.path.basename(r) for r in sorted(roots, key=len, reverse=True)
                          if f["path"].startswith(r)), "")
    total = sum(len(f["lines"]) for f in items)
    secs = round(time.time() - t0, 2)
    log(f"[search] {pattern!r} → {total} 行 / {len(items)} 文件（{secs}s）")
    try:
        with open(HISTORY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                 "q": pattern, "files": len(items), "lines": total},
                                ensure_ascii=False) + "\n")
    except Exception:
        pass
    return {"pattern": pattern, "hits": items, "files": len(items), "lines": total,
            "seconds": secs, "truncated": truncated}


def list_files(roots=None, includes=None, limit=400):
    """没有搜索词时，列出范围内的文件（给用户一个入口）"""
    c = cfg()
    roots = roots or c["roots"]
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                if includes and not any(f.endswith(i.lstrip("*")) for i in includes):
                    continue
                out.append(os.path.join(cur, f))
                if len(out) >= limit:
                    return out
    return out


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "searchpad/" + VERSION

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HOME, "index.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as exc:
                return self._send(500, f"<h1>{exc}</h1>", "text/html; charset=utf-8")

        if u.path == "/api/status":
            c = cfg()
            return self._send(200, json.dumps({
                "version": VERSION, "roots": c["roots"], "presets": list(PRESETS),
                "skip": list(SKIP_DIRS),
            }, ensure_ascii=False))

        if u.path == "/api/discover":
            found = discover_roots()
            c = cfg()
            return self._send(200, json.dumps({"found": found,
                                               "new": [p for p in found if p not in c["roots"]]},
                                              ensure_ascii=False))

        if u.path == "/api/file":
            qs = urllib.parse.parse_qs(u.query)
            path = (qs.get("path", [""])[0] or "").strip()
            if not path or not os.path.isfile(path):
                return self._send(404, json.dumps({"error": "文件不存在"}, ensure_ascii=False))
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    txt = f.read(200000)
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return self._send(200, json.dumps({"path": path, "text": txt,
                                               "lines": txt.count("\n") + 1},
                                              ensure_ascii=False))

        if u.path == "/api/history":
            rows = []
            try:
                with open(HISTORY, encoding="utf-8") as f:
                    for line in f.readlines()[-20:]:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
            except Exception:
                pass
            return self._send(200, json.dumps({"history": list(reversed(rows))},
                                              ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/search":
            preset = b.get("preset") or "全部文本"
            includes = b.get("includes")
            if includes is None:
                includes = PRESETS.get(preset, [])
            r = run_search(
                b.get("q") or "",
                roots=b.get("roots"),
                includes=includes,
                case_sensitive=bool(b.get("case")),
                regex=bool(b.get("regex")),
                context=int(b.get("context") or 1),
                per_file=int(b.get("per_file") or 20),
            )
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/config":
            c = cfg()
            if b.get("roots") is not None:
                c["roots"] = [p for p in b["roots"] if p]
            if b.get("case_sensitive") is not None:
                c["case_sensitive"] = bool(b["case_sensitive"])
            write_json(CONFIGFILE, c)
            log(f"[config] 搜索范围 {len(c['roots'])} 个")
            return self._send(200, json.dumps({"ok": True, "roots": c["roots"]},
                                              ensure_ascii=False))

        if u.path == "/api/reveal":
            path = (b.get("path") or "").strip()
            if not path:
                return self._send(400, json.dumps({"error": "没有路径"}, ensure_ascii=False))
            target = path if os.path.isdir(path) else os.path.dirname(path)
            if os.path.isdir(target):
                subprocess.Popen(["open", target])
                return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return self._send(404, json.dumps({"error": "目录不存在"}, ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def running_pid():
    try:
        pid = int((open(PIDFILE).read() or "0").strip())
    except Exception:
        return 0
    try:
        os.kill(pid, 0)
        return pid
    except Exception:
        return 0


def cmd_serve(args):
    port = args.port or DEFAULT_PORT
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    c = cfg()
    if not c["roots"]:
        found = discover_roots()
        if found:
            c["roots"] = found
            write_json(CONFIGFILE, c)
            log(f"[init] 自动设置 {len(found)} 个搜索根")
    log(f"代码搜索 v{VERSION} 已启动 http://127.0.0.1:{port}（{len(c['roots'])} 个根目录）")

    def bye(*_):
        try:
            os.remove(PIDFILE)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    try:
        Server(("127.0.0.1", port), Handler).serve_forever()
    except OSError as exc:
        log(f"端口 {port} 起不来：{exc}")
        sys.exit(1)


def cmd_start(args):
    if running_pid():
        print(f"已在运行（PID {running_pid()}）")
        return 0
    port = args.port or DEFAULT_PORT
    logf = open(LOGFILE, "a")
    subprocess.Popen([sys.executable, os.path.realpath(__file__), "serve", "--port", str(port)],
                     stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True, cwd=HOME)
    for _ in range(30):
        time.sleep(0.2)
        if running_pid():
            break
    print(f"✅ 代码搜索已启动（PID {running_pid()}） http://127.0.0.1:{port}"
          if running_pid() else "❌ 启动失败")
    return 0 if running_pid() else 1


def cmd_stop(args):
    pid = running_pid()
    if not pid:
        print("没有在运行")
        return 0
    try:
        os.kill(pid, signal.SIGTERM); time.sleep(0.6)
    except Exception:
        pass
    try:
        os.remove(PIDFILE)
    except Exception:
        pass
    print("已停止")
    return 0


def cmd_search(args):
    c = cfg()
    roots = c["roots"] or discover_roots()
    r = run_search(args.q, roots=roots,
                   includes=PRESETS.get(args.preset, []),
                   case_sensitive=args.case, regex=args.regex)
    if r.get("error"):
        print("❌ " + r["error"])
        return 1
    print(f"{r['lines']} 行 / {r['files']} 个文件（{r['seconds']}s）\n")
    for f in r["hits"]:
        print(f"  {f['repo']}/{f['name']}")
        for ln in f["lines"]:
            mark = "▸" if ln["hit"] else " "
            print(f"   {mark}{ln['n']:>6}  {ln['text'][:150]}")
        print()
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="searchpad", description=f"代码搜索 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("search"); sp.add_argument("q")
    sp.add_argument("--preset", default="全部文本", choices=list(PRESETS))
    sp.add_argument("--case", action="store_true"); sp.add_argument("--regex", action="store_true")
    sp.set_defaults(f=cmd_search)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
