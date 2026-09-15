#!/usr/bin/env python3
"""开发工具箱 —— 那些零碎但天天要用的转换。

为什么做这个
  调接口时反复要干的事：URL 编码一下、Base64 解一下、时间戳换算成人能看的时间、
  JSON 转义、算个哈希、比一下两个字符串哪里不一样。每次都开终端敲一行太烦。

全部在本地算，不联网。零依赖。
"""
import base64
import hashlib
import html
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("TEXTKIT_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "textkit.pid")
LOGFILE = os.path.join(HOME, "textkit.log")
DEFAULT_PORT = 8899


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# 各种转换
# ══════════════════════════════════════════════════════════════

def b64_enc(s):
    return base64.b64encode(s.encode("utf-8")).decode()


def b64_dec(s):
    s = s.strip()
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    try:
        return base64.b64decode(s).decode("utf-8")
    except Exception:
        try:
            return base64.b64decode(s).decode("utf-8", "replace") + "\n（含非 UTF-8 字节，已替换）"
        except Exception as exc:
            return "解码失败：" + str(exc)


def ts_to_time(s):
    """时间戳 → 人类可读（秒/毫秒都认，含时区）"""
    s = s.strip()
    if not s:
        return "给个时间戳，比如 1757850000 或 1757850000000"
    try:
        n = float(s)
    except Exception:
        return "不是数字"
    if n > 1e11:            # 毫秒
        n = n / 1000.0
        unit = "毫秒"
    else:
        unit = "秒"
    try:
        dt = datetime.fromtimestamp(n)
        utc = datetime.fromtimestamp(n, timezone.utc)
        return (f"输入按{unit}解释\n"
                f"本地时间 : {dt.strftime('%Y-%m-%d %H:%M:%S')}  （{dt.astimezone().strftime('%Z%z')}）\n"
                f"UTC      : {utc.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"星期     : {'一二三四五六日'[dt.weekday()]}\n"
                f"相对现在 : {human_delta(dt - datetime.now())}")
    except Exception as exc:
        return "超出可表示范围：" + str(exc)


def human_delta(d):
    sec = d.total_seconds()
    fut = sec > 0
    sec = abs(sec)
    for unit, n in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if sec >= n:
            return f"{'还有' if fut else '过去'} {int(sec // n)} {unit}"
    return f"{'还有' if fut else '刚刚过去'} {int(sec)} 秒"


def time_to_ts(s):
    """时间字符串 → 时间戳"""
    s = s.strip()
    if not s:
        return "给个时间，比如 2026-09-14 10:30:00"
    fmts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%m-%d %H:%M", "%H:%M"]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            if f in ("%m-%d %H:%M", "%H:%M"):
                now = datetime.now()
                dt = dt.replace(year=now.year)
                if f == "%H:%M":
                    dt = dt.replace(month=now.month, day=now.day)
                if dt < now - timedelta(days=1):
                    dt += timedelta(days=1)
            return (f"秒       : {int(dt.timestamp())}\n"
                    f"毫秒     : {int(dt.timestamp() * 1000)}\n"
                    f"ISO8601  : {dt.isoformat()}\n"
                    f"UTC      : {dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
        except Exception:
            continue
    return "认不出这个时间格式。支持：2026-09-14 10:30:00 / 2026-09-14 / 10:30 等"


def hashes(s):
    b = s.encode("utf-8")
    return "\n".join([
        f"MD5      : {hashlib.md5(b).hexdigest()}",
        f"SHA1     : {hashlib.sha1(b).hexdigest()}",
        f"SHA256   : {hashlib.sha256(b).hexdigest()}",
        f"长度     : {len(s)} 字符 / {len(b)} 字节",
    ])


def json_escape(s):
    return json.dumps(s, ensure_ascii=False)[1:-1]


def json_unescape(s):
    try:
        return json.loads('"' + s.replace('"', '\\"') + '"')
    except Exception:
        try:
            return json.loads(s)
        except Exception as exc:
            return "反转义失败：" + str(exc)


def text_diff(a, b):
    """逐行对比两段文本（简单 LCS，够用）"""
    la, lb = a.splitlines(), b.splitlines()
    n, m = len(la), len(lb)
    if n * m > 400000:
        return "文本太长（%d × %d 行），先截短一些" % (n, m)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if la[i] == lb[j] else max(dp[i + 1][j], dp[i][j + 1])
    out, i, j = [], 0, 0
    while i < n and j < m:
        if la[i] == lb[j]:
            out.append("  " + la[i]); i += 1; j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            out.append("- " + la[i]); i += 1
        else:
            out.append("+ " + lb[j]); j += 1
    while i < n:
        out.append("- " + la[i]); i += 1
    while j < m:
        out.append("+ " + lb[j]); j += 1
    added = len([x for x in out if x.startswith("+ ")])
    removed = len([x for x in out if x.startswith("- ")])
    head = f"共 {len(out)} 行输出：新增 {added} 行，删除 {removed} 行\n" + "─" * 40 + "\n"
    return head + "\n".join(out)


def count_text(s):
    """字数统计。单独成函数是因为 Python 3.9 的 f-string 里不能写反斜杠。"""
    no_space = re.sub(r"\s", "", s)
    cjk = re.findall(r"[\u4e00-\u9fff]", s)
    return "\n".join([
        "字符（含空白）  : %d" % len(s),
        "字符（不含空白）: %d" % len(no_space),
        "行数            : %d" % len(s.splitlines()),
        "词数（按空白）  : %d" % len(s.split()),
        "字节（UTF-8）   : %d" % len(s.encode("utf-8")),
        "中文字符        : %d" % len(cjk),
    ])


OPS = {
    "b64_enc": ("Base64 编码", b64_enc),
    "b64_dec": ("Base64 解码", b64_dec),
    "url_enc": ("URL 编码", lambda s: urllib.parse.quote(s, safe="")),
    "url_dec": ("URL 解码", lambda s: urllib.parse.unquote(s)),
    "json_esc": ("JSON 转义", json_escape),
    "json_unesc": ("JSON 反转义", json_unescape),
    "json_fmt": ("JSON 格式化", lambda s: json.dumps(json.loads(s), ensure_ascii=False, indent=2)),
    "json_min": ("JSON 压缩", lambda s: json.dumps(json.loads(s), ensure_ascii=False,
                                                 separators=(",", ":"))),
    "ts2time": ("时间戳 → 时间", ts_to_time),
    "time2ts": ("时间 → 时间戳", time_to_ts),
    "hash": ("哈希 / 长度", hashes),
    "upper": ("转大写", lambda s: s.upper()),
    "lower": ("转小写", lambda s: s.lower()),
    "trim": ("去首尾空白", lambda s: s.strip()),
    "sort": ("行排序", lambda s: "\n".join(sorted(s.splitlines()))),
    "uniq": ("行去重", lambda s: "\n".join(dict.fromkeys(s.splitlines()))),
    "rev": ("行倒序", lambda s: "\n".join(reversed(s.splitlines()))),
    "html_esc": ("HTML 转义", lambda s: html.escape(s)),
    "html_unesc": ("HTML 反转义", lambda s: html.unescape(s)),
    "uuid": ("生成 UUID", lambda s: "\n".join(str(uuid.uuid4()) for _ in range(5))),
    "count": ("字数统计", lambda s: count_text(s)),
    "hex_enc": ("Hex 编码", lambda s: s.encode("utf-8").hex()),
    "hex_dec": ("Hex 解码", lambda s: bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", s))
                .decode("utf-8", "replace")),
}

GROUPS = [
    ("编码解码", ["b64_enc", "b64_dec", "url_enc", "url_dec", "hex_enc", "hex_dec",
                  "html_esc", "html_unesc"]),
    ("JSON", ["json_fmt", "json_min", "json_esc", "json_unesc"]),
    ("时间", ["ts2time", "time2ts"]),
    ("文本处理", ["trim", "upper", "lower", "sort", "uniq", "rev", "count"]),
    ("其他", ["hash", "uuid"]),
]


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "textkit/" + VERSION

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
            return self._send(200, json.dumps({
                "version": VERSION,
                "groups": [{"name": g, "ops": [{"id": o, "name": OPS[o][0]} for o in ops]}
                           for g, ops in GROUPS],
            }, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/run":
            op = (b.get("op") or "").strip()
            if op == "text_diff":
                out = text_diff(b.get("a") or "", b.get("b") or "")
                return self._send(200, json.dumps({"out": out}, ensure_ascii=False))
            if op not in OPS:
                return self._send(400, json.dumps({"error": "不认识的转换：" + op},
                                                  ensure_ascii=False))
            try:
                out = OPS[op][1](b.get("text") or "")
            except json.JSONDecodeError as exc:
                return self._send(200, json.dumps(
                    {"out": "", "error": f"JSON 解析失败：{getattr(exc,'msg',exc)}"
                                         f"（第 {exc.lineno} 行第 {exc.colno} 列）"},
                    ensure_ascii=False))
            except Exception as exc:
                return self._send(200, json.dumps(
                    {"out": "", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
            return self._send(200, json.dumps({"out": out, "op": op}, ensure_ascii=False))
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
    log(f"开发工具箱 v{VERSION} 已启动 http://127.0.0.1:{port}（{len(OPS)} 个转换）")

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
    print(f"✅ 开发工具箱已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_run(args):
    if args.op == "text_diff":
        print(text_diff(args.text, args.text2 or ""))
        return 0
    if args.op not in OPS:
        print("可用的转换：")
        for o, (n, _) in OPS.items():
            print(f"  {o:<12} {n}")
        return 1
    print(OPS[args.op][1](args.text))
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="textkit", description=f"开发工具箱 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("run"); sp.add_argument("op"); sp.add_argument("text")
    sp.add_argument("text2", nargs="?"); sp.set_defaults(f=cmd_run)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
