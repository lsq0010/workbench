#!/usr/bin/env python3
"""代码日志 —— App 里接一行 SDK，日志实时流到工作台，AI 直接分析。

为什么做这个
  用户的原话：「开发调试过程中要能实时看到代码日志……在代码里面集成一个sdk
  或者其他方法，就能实时在工具里看到日志，这样工作台也能拿到日志、分析，
  通过日志做反馈实现自动问题分析、代码优化，唯一需要人的地方就是人来操作手机，
  产生的日志作为反馈」

  所以这个工具是**闭环的中间那一段**：
    手机/模拟器跑 App → SDK 把日志推过来 → 工作台实时显示 → AI 读日志找问题

怎么用（三种来源，任选）
  1. **接 SDK**：把 sdk/DevLog.swift 拖进 Xcode，加一行 `DevLog.start(host:...)`
     —— 自己的日志自己控，能带 tag、能传上下文
  2. **模拟器日志**：不用改代码，直接抓 `xcrun simctl` 的系统日志
  3. **真机日志**：抓设备的 syslog（需要 idevicesyslog，没有就提示怎么装）

日志落盘是**只追加**的，带 user_id；按大小自动滚动，不会把磁盘写满。
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("LOGHUB_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "loghub.pid")
LOGFILE = os.path.join(HOME, "loghub.log")
LOGJSONL = os.path.join(HOME, "logs.jsonl")        # 只追加
ANALYSIS = os.path.join(HOME, "analysis.jsonl")    # 只追加
DEFAULT_PORT = 8918
MAX_MB = 200                                        # 日志文件上限，超了滚动

LEVELS = ["debug", "info", "warn", "error", "fatal"]
LEVEL_RANK = {k: i for i, k in enumerate(LEVELS)}


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} [loghub] {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _user_id():
    try:
        p = os.path.join(HOME, "..", "..", "lib")
        if p not in sys.path:
            sys.path.insert(0, p)
        import platform_lib
        return platform_lib.user_id()
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════
# 内存里的实时缓冲 + 落盘
# ══════════════════════════════════════════════════════════════
_lock = threading.Lock()
# 分两块缓冲，**故意不共用**：
# SDK 日志（App 自己发的）是用户真正要看的，绝不能被系统噪音挤掉；
# 系统抓取的日志量大（实测一次 6 万条），单独放，满了就丢自己那边的。
_recent_app = deque(maxlen=4000)
_recent_sys = deque(maxlen=1500)
_subscribers = []                     # SSE 订阅者队列
_seq = [0]


def _all_recent():
    """两块合起来按序号排 —— 界面要的是时间顺序"""
    return sorted(list(_recent_app) + list(_recent_sys), key=lambda r: r.get("seq") or 0)


def rotate_if_needed():
    try:
        if os.path.exists(LOGJSONL) and os.path.getsize(LOGJSONL) > MAX_MB * 1024 * 1024:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            os.rename(LOGJSONL, os.path.join(HOME, f"logs-{stamp}.jsonl"))
            log(f"日志超过 {MAX_MB}MB，已滚动到 logs-{stamp}.jsonl")
    except Exception as exc:
        log(f"滚动失败：{exc}")


def normalize(entry, source="sdk"):
    """把各种来源的日志统一成一条记录"""
    lvl = str(entry.get("level") or entry.get("lvl") or "info").lower()
    if lvl not in LEVEL_RANK:
        lvl = "info"
    return {
        "seq": None,                     # 落盘时填
        "ts": entry.get("ts") or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "level": lvl,
        "tag": str(entry.get("tag") or entry.get("category") or "App")[:60],
        "msg": str(entry.get("msg") or entry.get("message") or "")[:8000],
        "file": str(entry.get("file") or "")[:200],
        "line": entry.get("line"),
        "func": str(entry.get("func") or entry.get("function") or "")[:120],
        "device": str(entry.get("device") or "")[:80],
        "app": str(entry.get("app") or entry.get("bundle") or "")[:80],
        "extra": entry.get("extra") if isinstance(entry.get("extra"), dict) else None,
        "source": source,
        "user_id": _user_id(),
    }


def ingest(entries, source="sdk"):
    """收日志：落盘 + 推给订阅者"""
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    is_sys = source in ("simulator", "device")
    with _lock:
        for e in entries[:500]:
            # 系统抓取只收有内容的行，别把空行和续行当日志
            if is_sys and not str(e.get("msg") or "").strip():
                continue
            rec = normalize(e, source)
            _seq[0] += 1
            rec["seq"] = _seq[0]
            (_recent_sys if is_sys else _recent_app).append(rec)
            out.append(rec)
        if out:
            try:
                with open(LOGJSONL, "a", encoding="utf-8") as f:
                    for r in out:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
            except Exception as exc:
                log(f"写盘失败：{exc}")
            rotate_if_needed()
        for q in list(_subscribers):
            for r in out:
                try:
                    q.append(r)
                except Exception:
                    pass
    return len(out)


def query(level="", tag="", q="", limit=400, since_seq=0, source=""):
    """查日志：内存里查最近的（快），不够再读文件"""
    rows = [r for r in _all_recent() if r["seq"] > since_seq]
    if not rows and os.path.exists(LOGJSONL):
        try:
            with open(LOGJSONL, encoding="utf-8") as f:
                lines = f.readlines()[-3000:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        except Exception:
            pass
    # 按来源分：app = App 通过 SDK 发的（用户真正要看的）
    #             sys = 从模拟器/真机抓的系统日志（噪音大，要显式选才看）
    if source == "app":
        rows = [r for r in rows if r.get("source") not in ("simulator", "device")]
    elif source == "sys":
        rows = [r for r in rows if r.get("source") in ("simulator", "device")]
    if level:
        mn = LEVEL_RANK.get(level, 0)
        rows = [r for r in rows if LEVEL_RANK.get(r.get("level"), 1) >= mn]
    if tag:
        rows = [r for r in rows if tag.lower() in str(r.get("tag", "")).lower()]
    if q:
        ql = q.lower()
        rows = [r for r in rows
                if ql in str(r.get("msg", "")).lower()
                or ql in str(r.get("tag", "")).lower()
                or ql in str(r.get("file", "")).lower()]
    return rows[-limit:]


# 测试/demo App 的日志不算"你的 App 出问题了"。
# 踩过的坑：我自己造的 demo App 发模拟 error，汇报里就写
# 「App 日志里有 22 条 error」—— 用户看到会以为自己的 App 出事。
# 误报比不报更糟，所以这里挡掉。
NOISE_APP_HINTS = ("com.local.demo", "com.example", "com.test")
NOISE_TAGS = ("DevLog",)


def real_errors(rows=None):
    """真正值得报的 error 条数（排除测试 App 和 SDK 自己的日志）"""
    rows = rows if rows is not None else _all_recent()
    n = 0
    for r in rows:
        if r.get("level") not in ("error", "fatal"):
            continue
        app = (r.get("app") or "").lower()
        tag = (r.get("tag") or "")
        if any(k in app for k in NOISE_APP_HINTS):
            continue
        if tag in NOISE_TAGS:
            continue
        if tag.startswith("{") or tag.startswith("["):
            continue
        n += 1
    return n


def stats(only_app=False):
    rows = list(_recent_app) if only_app else _all_recent()
    by_level = {}
    by_tag = {}
    for r in rows:
        by_level[r["level"]] = by_level.get(r["level"], 0) + 1
        by_tag[r["tag"]] = by_tag.get(r["tag"], 0) + 1
    return {
        "total": len(rows), "by_level": by_level,
        "by_tag": sorted(by_tag.items(), key=lambda x: -x[1])[:20],
        "errors": len([r for r in rows if r["level"] in ("error", "fatal")]),
        "warns": len([r for r in rows if r["level"] == "warn"]),
        "app": len([r for r in rows if r.get("source") not in ("simulator", "device")]),
        "sys": len([r for r in rows if r.get("source") in ("simulator", "device")]),
        "real_errors": real_errors(rows),
    }


# ══════════════════════════════════════════════════════════════
# 从模拟器/真机抓系统日志（不用改代码）
# ══════════════════════════════════════════════════════════════
_capture = {"proc": None, "kind": None, "target": None}


def list_simulators():
    try:
        p = subprocess.run(["xcrun", "simctl", "list", "devices", "booted", "-j"],
                           capture_output=True, text=True, timeout=20)
        d = json.loads(p.stdout or "{}")
        out = []
        for runtime, devs in (d.get("devices") or {}).items():
            for dev in devs:
                out.append({"name": dev.get("name"), "udid": dev.get("udid"),
                            "state": dev.get("state"),
                            "runtime": runtime.split(".")[-1]})
        return out
    except Exception as exc:
        return [{"error": str(exc)}]


def start_capture(kind, target=None, level="info", predicate=None, process=None):
    """开始抓系统日志。kind: simulator | device

    默认**只跟指定的进程** —— 不指定的话会把所有守护进程的日志都抓进来
    （实测一次 6 万多条），App 自己的日志就淹没了。process 一般传 App 名字。
    """
    stop_capture()
    if kind == "simulator":
        if not target:
            sims = [s for s in list_simulators() if s.get("udid")]
            if not sims:
                return False, "没有正在运行的模拟器 —— 先在 Xcode 里跑起来"
            target = sims[0]["udid"]
        cmd = ["xcrun", "simctl", "spawn", target, "log", "stream",
               "--style", "compact", "--level", level]
        # 没给 predicate 就用进程名兜底；两个都没有才全量抓（并提示会很吵）
        if not predicate and process:
            predicate = ('processImagePath CONTAINS "%s" OR '
                         'senderImagePath CONTAINS "%s"' % (process, process))
        if predicate:
            cmd += ["--predicate", predicate]
    elif kind == "device":
        from shutil import which
        if not which("idevicesyslog"):
            return False, ("没装 idevicesyslog。装法：brew install libimobiledevice，"
                           "或者改用「接 SDK」的方式（不需要额外工具）")
        cmd = ["idevicesyslog"]
    else:
        return False, "不认识的来源：%s" % kind

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except Exception as exc:
        return False, "启动失败：%s" % exc
    _capture.update({"proc": proc, "kind": kind, "target": target})
    threading.Thread(target=_pump, args=(proc, kind), daemon=True).start()
    log(f"开始抓 {kind} 日志" + (f"（{target[:8]}…）" if target else ""))
    return True, f"已开始抓 {kind} 日志"


LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\.\d+)?\s*"
    r"(?P<proc>[\w\.\-]+)?\s*[\[\(]?(?P<pid>\d+)?[\]\)]?\s*"
    r"(?P<lvl>Debug|Info|Notice|Warning|Error|Fault|Critical)\s*"
    r"(?P<rest>.*)$", re.I)
LEVEL_MAP = {"debug": "debug", "info": "info", "notice": "info", "warning": "warn",
             "error": "error", "fault": "fatal", "critical": "fatal"}


# 这些是 os_log 多行条目的续行，不是独立日志 —— 合并到上一条
CONT_RE = re.compile(r"^(\s+|[)\]]|\"|\w+\s*=|\w+:\s*$)")


def _pump(proc, kind):
    """把系统日志的每一行转成结构化记录。

    os_log 的多行条目（比如打印一个字典）会被拆成很多行，
    以前每行都当成一条日志，结果一屏全是 `) ignoredTopics (` 这种碎片。
    现在续行合并到上一条，最多攒 800 字符。
    """
    buf = []
    pending = None

    def flush_pending():
        nonlocal pending
        if pending:
            buf.append(pending)
            pending = None

    for line in iter(proc.stdout.readline, ""):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        m = LINE_RE.match(line)
        if m:
            flush_pending()
            lvl = LEVEL_MAP.get((m.group("lvl") or "info").lower(), "info")
            pending = {"level": lvl, "tag": (m.group("proc") or "system")[:40],
                       "msg": (m.group("rest") or line)[:2000],
                       "ts": (m.group("ts") or "").replace("T", " ")[:23]}
        elif pending is not None and CONT_RE.match(line) and len(pending["msg"]) < 800:
            pending["msg"] += "\n" + line.strip()[:400]     # 续行，粘上去
        elif pending is None:
            continue                     # 还没有起始行，跳过这半截
        else:
            flush_pending()
            pending = {"level": "debug", "tag": "system", "msg": line[:2000]}
        if len(buf) >= 20:
            for r in buf:
                r["source"] = kind
            ingest(buf, source=kind)
            buf = []
    flush_pending()
    if buf:
        for r in buf:
            r["source"] = kind
        ingest(buf, source=kind)
    log(f"{kind} 日志流结束")


def stop_capture():
    p = _capture.get("proc")
    if p:
        try:
            p.terminate()
        except Exception:
            pass
    _capture.update({"proc": None, "kind": None, "target": None})


def capture_status():
    p = _capture.get("proc")
    return {"running": bool(p and p.poll() is None), "kind": _capture.get("kind"),
            "target": _capture.get("target")}


# ══════════════════════════════════════════════════════════════
# AI 分析日志
# ══════════════════════════════════════════════════════════════

ANALYZE_PROMPT = """下面是 App 运行时的日志。请像一个有经验的 iOS 开发一样分析：

1. **有没有问题**：崩溃、异常、超时、失败请求、内存警告、约束冲突等
2. **问题在哪**：指出具体的类/文件/接口/行号（日志里有的信息才说，没有就说"日志没体现"）
3. **怎么修**：给具体建议，不要泛泛而谈
4. **值得注意的模式**：重复出现的、时间间隔异常的、调用顺序可疑的

如果日志里没有问题，直接说"没发现明显问题"，不要硬找。
用中文，分点，简洁。"""


def ai_analyze(rows, question=""):
    try:
        lib_dir = os.path.join(HOME, "..", "..", "lib")
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        import platform_lib
        if not platform_lib.ready():
            return {"ok": False, "error": "AI 还没配好 —— 去工作台「设置」里配 key"}
    except Exception as exc:
        return {"ok": False, "error": "AI 库加载失败：%s" % exc}

    # 压缩日志：只留关键字段，控制长度
    lines = []
    for r in rows[-400:]:
        lvl = (r.get("level") or "info").upper()[:4]
        lines.append("[%s] %-4s %-16s %s" % (str(r.get("ts") or "")[-12:], lvl,
                                             str(r.get("tag") or "")[:16],
                                             str(r.get("msg") or "")[:300]))
    body = "\n".join(lines)
    ask = ANALYZE_PROMPT
    if question:
        ask += "\n\n用户特别关心：" + question
    try:
        out = []
        for piece in platform_lib.stream([
                {"role": "system", "content": ask},
                {"role": "user", "content": "日志（共 %d 条，下面是尾部）：\n%s"
                                            % (len(rows), body[:60000])}]):
            out.append(piece)
        text = "".join(out)
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    try:
        with open(ANALYSIS, "a", encoding="utf-8") as f:      # 只追加
            f.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "user_id": _user_id(), "rows": len(rows),
                                "question": question,
                                "result": text[:20000]}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    log(f"[analyze] 分析了 {len(rows)} 条日志")
    return {"ok": True, "text": text, "rows": len(rows)}


def analysis_history(limit=10):
    rows = []
    try:
        with open(ANALYSIS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return list(reversed(rows[-limit:]))


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "loghub/" + VERSION

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

    def do_OPTIONS(self):
        """SDK 从别的域发过来，给个 CORS 头"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HOME, "index.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as exc:
                return self._send(500, f"<h1>{exc}</h1>", "text/html; charset=utf-8")

        # SDK 拉配置：App 里写死一个地址就行，其余从这拿
        if u.path == "/api/sdk/config":
            return self._send(200, json.dumps({
                "version": VERSION, "endpoint": "/api/log",
                "levels": LEVELS, "note": "App 里 DevLog.start(host:port:) 指过来即可",
            }, ensure_ascii=False))

        if u.path == "/api/status":
            c = capture_status()
            return self._send(200, json.dumps({
                "version": VERSION, "stats": stats(),
                "capture": c, "simulators": list_simulators(),
                "analysis": analysis_history(5),
                "port": self.server.server_address[1],
                "logfile": LOGJSONL,
                "logfile_mb": round(os.path.getsize(LOGJSONL) / 1048576, 2)
                              if os.path.exists(LOGJSONL) else 0,
            }, ensure_ascii=False))

        if u.path == "/api/logs":
            rows = query(level=(qs.get("level", [""])[0] or ""),
                         tag=(qs.get("tag", [""])[0] or ""),
                         q=(qs.get("q", [""])[0] or ""),
                         limit=int(qs.get("limit", ["400"])[0] or 400),
                         since_seq=int(qs.get("since_seq", ["0"])[0] or 0),
                         source=(qs.get("source", [""])[0] or ""))
            return self._send(200, json.dumps({"logs": rows, "count": len(rows)},
                                              ensure_ascii=False))

        # SSE 实时流
        if u.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.close_connection = True
            q = deque(maxlen=500)
            with _lock:
                _subscribers.append(q)
            try:
                # 先把最近的补一批，界面不用等新日志
                for r in _all_recent()[-120:]:
                    self.wfile.write(("data: " + json.dumps(r, ensure_ascii=False) +
                                      "\n\n").encode("utf-8"))
                self.wfile.flush()
                last_beat = time.time()
                while True:
                    if q:
                        while q:
                            r = q.popleft()
                            self.wfile.write(("data: " + json.dumps(r, ensure_ascii=False) +
                                              "\n\n").encode("utf-8"))
                        self.wfile.flush()
                        last_beat = time.time()
                    else:
                        time.sleep(0.25)
                        if time.time() - last_beat > 15:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            last_beat = time.time()
            except Exception:
                pass
            finally:
                with _lock:
                    if q in _subscribers:
                        _subscribers.remove(q)
            return

        if u.path == "/api/sdk/swift":
            """直接下载 SDK 文件"""
            p = os.path.join(HOME, "sdk", "DevLog.swift")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/plain; charset=utf-8")
            return self._send(404, "// SDK 文件不在", "text/plain; charset=utf-8")

        if u.path == "/api/sdk/js":
            p = os.path.join(HOME, "sdk", "devlog.js")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/javascript; charset=utf-8")
            return self._send(404, "// SDK 文件不在", "text/plain; charset=utf-8")

        if u.path == "/api/digest":
            real = real_errors()
            items = []
            if real:
                items.append({"level": "warn",
                              "title": f"App 日志里有 {real} 条 error",
                              "detail": "打开代码日志看看，可以让 AI 直接分析",
                              "action": "打开代码日志"})
            return self._send(200, json.dumps({"items": items}, ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        # 日志上报（SDK 用这个）
        if u.path in ("/api/log", "/api/logs"):
            data = b.get("logs") if isinstance(b.get("logs"), list) else b
            n = ingest(data, source=b.get("source") or "sdk")
            return self._send(200, json.dumps({"ok": True, "received": n},
                                              ensure_ascii=False))

        if u.path == "/api/capture/start":
            ok, msg = start_capture(b.get("kind") or "simulator", b.get("target"),
                                    b.get("level") or "info", b.get("predicate"),
                                    b.get("process"))
            return self._send(200, json.dumps({"ok": ok, "message": msg},
                                              ensure_ascii=False))

        if u.path == "/api/capture/stop":
            stop_capture()
            return self._send(200, json.dumps({"ok": True, "message": "已停止抓日志"},
                                              ensure_ascii=False))

        if u.path == "/api/analyze":
            # 默认**只分析 App 自己的日志**。
            # 踩过的坑：不分来源的话，系统日志（一次几千条）会把 App 的十几条
            # 挤到窗口外，AI 拿到的全是 maild / SpringBoard 的噪音，
            # 然后老实告诉你"这日志里没有你的 App" —— 那是我的取数错了，不怪它。
            src = b.get("source")
            if src is None:
                src = "app"
            rows = query(level=b.get("level") or "", tag=b.get("tag") or "",
                         q=b.get("q") or "", limit=int(b.get("limit") or 400),
                         source=src)
            if not rows and src == "app":
                return self._send(200, json.dumps(
                    {"ok": False, "error": "还没有 App 自己的日志。两种来源：\n"
                     "① App 里接 SDK（推荐，见「接入 SDK」）\n"
                     "② 或者把分析范围切到「含系统日志」"}, ensure_ascii=False))
            if not rows:
                return self._send(200, json.dumps(
                    {"ok": False, "error": "没有日志可分析"}, ensure_ascii=False))
            r = ai_analyze(rows, b.get("question") or "")
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/clear":
            """只清内存里的实时缓冲，**不动落盘的日志**"""
            with _lock:
                _recent_app.clear()
                _recent_sys.clear()
            return self._send(200, json.dumps(
                {"ok": True, "message": "已清空界面缓冲（落盘的日志没动）"},
                ensure_ascii=False))

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
    # 启动时把最近的日志读进内存，界面一开就有东西
    if os.path.exists(LOGJSONL):
        try:
            with open(LOGJSONL, encoding="utf-8") as f:
                for line in f.readlines()[-1000:]:
                    line = line.strip()
                    if line:
                        try:
                            r = json.loads(line)
                            _seq[0] = max(_seq[0], int(r.get("seq") or 0))
                            (_recent_sys
                             if r.get("source") in ("simulator", "device")
                             else _recent_app).append(r)
                        except Exception:
                            pass
        except Exception:
            pass
    log(f"代码日志 v{VERSION} 已启动 http://127.0.0.1:{port}"
        f"（已载入 {len(_recent_app)} 条 App 日志 / {len(_recent_sys)} 条系统日志）")

    def bye(*_):
        stop_capture()
        try:
            os.remove(PIDFILE)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    try:
        Server(("0.0.0.0", port), Handler).serve_forever()
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
    print(f"✅ 代码日志已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_push(args):
    """命令行推一条日志 —— 测试用"""
    n = ingest({"level": args.level, "tag": args.tag, "msg": args.msg}, source="cli")
    print(f"  已收 {n} 条")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="loghub", description=f"代码日志 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("push"); sp.add_argument("msg")
    sp.add_argument("--level", default="info"); sp.add_argument("--tag", default="cli")
    sp.set_defaults(f=cmd_push)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
