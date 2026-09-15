#!/usr/bin/env python3
"""正则实验室 —— 写正则时实时看匹配结果。

为什么做这个
  正则写起来最烦的是"到底匹配到没有、匹配到了什么、分组取的对不对"。
  这里把匹配片段高亮出来，分组单独列，还能直接做替换预览。

用 Python 的 re，和你在服务端/脚本里用的是同一套语法（不是浏览器 JS 那套），
所以在这里调通的正则能直接拿去用。

零第三方依赖。
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("REGEXLAB_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "regexlab.pid")
LOGFILE = os.path.join(HOME, "regexlab.log")
DEFAULT_PORT = 8909

# 常用正则，点一下就能试
PRESETS = [
    {"name": "手机号（中国）", "re": r"1[3-9]\d{9}"},
    {"name": "邮箱", "re": r"[\w.+-]+@[\w-]+\.[\w.]+"},
    {"name": "URL", "re": r"https?://[^\s\"'<>]+"},
    {"name": "IP 地址", "re": r"\b(?:\d{1,3}\.){3}\d{1,3}\b"},
    {"name": "运单号（字母+数字）", "re": r"\b[A-Z]{2}\d{8,}\b"},
    {"name": "ISO 时间", "re": r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"},
    {"name": "JSON 里的某个键", "re": r'"(\w+)"\s*:\s*"([^"]*)"'},
    {"name": "驼峰转蛇形（替换用）", "re": r"(?<!^)(?=[A-Z])"},
    {"name": "日志时间戳行", "re": r"^\d{2}:\d{2}:\d{2}"},
    {"name": "重复的空行", "re": r"\n{3,}"},
]


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_regex(pattern, text, flags=None, replace=None, limit=300):
    flags = flags or {}
    f = 0
    if flags.get("i"):
        f |= re.I
    if flags.get("m"):
        f |= re.M
    if flags.get("s"):
        f |= re.S
    if flags.get("x"):
        f |= re.X

    t0 = time.time()
    if not pattern:
        return {"error": "正则是空的"}
    try:
        rx = re.compile(pattern, f)
    except re.error as exc:
        # 把错误位置说清楚
        pos = getattr(exc, "pos", None)
        return {"error": f"正则语法错误：{exc.msg if hasattr(exc,'msg') else exc}"
                         + (f"（位置 {pos}）" if pos is not None else "")}

    matches = []
    truncated = False
    for m in rx.finditer(text):
        if len(matches) >= limit:
            truncated = True
            break
        g = m.group(0)
        item = {
            "start": m.start(), "end": m.end(),
            "text": g if len(g) <= 200 else g[:200] + "…",
            "line": text[:m.start()].count("\n") + 1,
            "col": m.start() - (text.rfind("\n", 0, m.start()) + 1) + 1,
            "groups": [{"i": i, "name": None, "value": v if v is None or len(v) <= 200 else v[:200] + "…"}
                       for i, v in enumerate(m.groups(), 1)],
            "named": {k: (v if v is None or len(v) <= 200 else v[:200] + "…")
                      for k, v in (m.groupdict() or {}).items()},
        }
        matches.append(item)

    # 高亮片段（前端渲染用）
    segments = []
    pos = 0
    for m in matches[:200]:
        if m["start"] > pos:
            segments.append({"t": "plain", "v": text[pos:m["start"]]})
        segments.append({"t": "match", "v": text[m["start"]:m["end"]]})
        pos = m["end"]
    if pos < len(text):
        segments.append({"t": "plain", "v": text[pos:]})
    # 防止片段过大
    if sum(len(s["v"]) for s in segments) > 400000:
        segments = segments[:400]

    result = {
        "count": len(matches), "matches": matches[:200],
        "segments": segments, "truncated": truncated,
        "seconds": round(time.time() - t0, 3),
        "flags": f,
    }

    if replace is not None:
        try:
            result["replaced"] = rx.sub(replace, text, count=0)
            result["replaced_count"] = len(matches)
        except re.error as exc:
            result["replace_error"] = f"替换表达式有问题：{exc}"
        except Exception as exc:
            result["replace_error"] = str(exc)
    return result


def explain(pattern):
    """把正则拆成人话（常见构造的解释）"""
    PARTS = [
        (r"^\^", "字符串开头（多行模式下是行首）"),
        (r"\$$", "字符串结尾（多行模式下是行尾）"),
        (r"\\d", "数字 0-9"),
        (r"\\w", "字母/数字/下划线"),
        (r"\\s", "空白字符（空格、Tab、换行）"),
        (r"\\b", "单词边界"),
        (r"\.", "任意字符（除换行）"),
        (r"\*", "前面那个出现 0 次或多次"),
        (r"\+", "前面那个出现 1 次或多次"),
        (r"\?", "前面那个出现 0 次或 1 次"),
        (r"\{(\d+),(\d+)\}", r"前面那个出现 \1 到 \2 次"),
        (r"\{(\d+)\}", r"前面那个正好出现 \1 次"),
        (r"\[([^\]]+)\]", r"字符集：\1 中任意一个"),
        (r"\(([^)]+)\)", r"分组：\1"),
        (r"\(\?:([^)]+)\)", r"非捕获分组：\1"),
        (r"\(\?P<(\w+)>", r"命名分组 \1"),
        (r"\|", "或者"),
    ]
    out = []
    for pat, desc in PARTS:
        for m in re.finditer(pat, pattern):
            try:
                d = m.expand(desc) if "\\1" in desc else desc
            except Exception:
                d = desc
            out.append({"at": m.start(), "token": m.group(0), "desc": d})
    out.sort(key=lambda x: x["at"])
    return out


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "regexlab/" + VERSION

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
        import urllib.parse
        u = urllib.parse.urlsplit(self.path)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HOME, "index.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as exc:
                return self._send(500, f"<h1>{exc}</h1>", "text/html; charset=utf-8")
        if u.path == "/api/status":
            import sys as _s
            return self._send(200, json.dumps({
                "version": VERSION, "presets": PRESETS,
                "python": _s.version.split()[0],
            }, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        import urllib.parse
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/run":
            r = run_regex(b.get("pattern") or "", b.get("text") or "",
                          b.get("flags") or {},
                          b.get("replace") if b.get("replace") is not None else None)
            if not r.get("error"):
                log(f"[run] /{(b.get('pattern') or '')[:40]}/ → {r['count']} 处")
                r["explain"] = explain(b.get("pattern") or "")
            return self._send(200, json.dumps(r, ensure_ascii=False))
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
    log(f"正则实验室 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ 正则实验室已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_test(args):
    r = run_regex(args.pattern, args.text, replace=args.replace)
    if r.get("error"):
        print("❌ " + r["error"])
        return 1
    print(f"{r['count']} 处匹配（{r['seconds']}s）\n")
    for m in r["matches"][:20]:
        print(f"  第 {m['line']} 行 第 {m['col']} 列: {m['text']}")
        for g in m["groups"]:
            if g["value"] is not None:
                print(f"      组 {g['i']}: {g['value']}")
    if r.get("replaced") is not None:
        print("\n── 替换后 ──")
        print(r["replaced"][:1500])
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="regexlab", description=f"正则实验室 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("test"); sp.add_argument("pattern"); sp.add_argument("text")
    sp.add_argument("--replace"); sp.set_defaults(f=cmd_test)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
