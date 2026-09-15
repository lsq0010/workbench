#!/usr/bin/env python3
"""端口与进程 —— 谁在监听、谁占了我的端口。

为什么做这个
  开发时最常撞的墙：「端口 8080 已被占用」，然后要 lsof 一层层查。
  服务起不来、代理冲突、上一个进程没退干净 —— 都要看这个。
  这里一次列全，能按端口搜，也能看出是哪个项目在占（cwd 一眼认出来）。

安全：默认只读。要结束进程必须显式点按钮并二次确认。
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
HOME = os.path.abspath(os.environ.get("NETPORT_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "netport.pid")
LOGFILE = os.path.join(HOME, "netport.log")
DEFAULT_PORT = 8905

# 常见开发端口，用来标注"这是干嘛的"
KNOWN = {
    80: "HTTP", 443: "HTTPS", 3000: "Node/React", 3306: "MySQL", 5000: "Flask/AirPlay",
    5173: "Vite", 5432: "PostgreSQL", 6379: "Redis", 8000: "Django/通用",
    8080: "通用 HTTP 备用", 8081: "通用", 8888: "Jupyter/Charles", 8890: "抓包代理",
    8891: "抓包工作台", 8892: "抓包-手机接入页", 8893: "接口契约", 8894: "Git 周报",
    8895: "报文对比", 8896: "模型生成", 8897: "仓库总览", 8898: "代码搜索",
    8899: "开发工具箱", 8901: "接口变更监控", 8902: "便签", 8903: "证书检查",
    8904: "图片工具箱", 8880: "工作平台", 9000: "PHP-FPM", 27017: "MongoDB",
}


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def sh(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return p.stdout
    except Exception:
        return ""


def listener_pids():
    """所有处于 LISTEN 状态的进程 pid → 该进程的端口集合"""
    out = sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    by_pid = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        cmd, pid = parts[0], parts[1]
        name = parts[-2] if len(parts) >= 9 else ""
        # NAME 形如 *:8080 或 127.0.0.1:8891
        m = re.match(r"^(.*?):(\d+)$", parts[-2] if parts[-1] == "(LISTEN)" else parts[-1])
        addr, port = (m.group(1), int(m.group(2))) if m else ("", 0)
        if not port:
            m2 = re.search(r":(\d+)\s*$", line)
            if m2:
                port = int(m2.group(1))
        by_pid.setdefault(pid, {"pid": pid, "command": cmd, "ports": [],
                                "bind": set(), "user": parts[2] if len(parts) > 2 else ""})
        if port:
            by_pid[pid]["ports"].append(port)
            by_pid[pid]["bind"].add(addr)
    # UPD 也扫一遍（有些服务只在 UDP 上）
    out2 = sh(["lsof", "-nP", "-iUDP"])
    for line in out2.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        cmd, pid = parts[0], parts[1]
        m = re.search(r":(\d+)\s*(\(|$)", line)
        if not m:
            continue
        port = int(m.group(1))
        d = by_pid.setdefault(pid, {"pid": pid, "command": cmd, "ports": [],
                                    "bind": set(), "user": parts[2] if len(parts) > 2 else ""})
        if port not in d["ports"]:
            d["ports"].append(port)
        d["udp"] = True
    return by_pid


def process_detail(pid):
    """补上进程的可读信息：完整命令行、工作目录、启动时间、内存"""
    info = {}
    try:
        p = subprocess.run(["ps", "-p", str(pid), "-o",
                            "command=,etime=,rss=,pcpu="],
                           capture_output=True, text=True, timeout=8, errors="replace")
        line = (p.stdout or "").strip()
        if line:
            parts = line.split()
            info["cpu"] = parts[-1] if parts else ""
            info["mem"] = parts[-2] if len(parts) > 1 else ""
            info["uptime"] = parts[-3] if len(parts) > 2 else ""
            info["cmdline"] = " ".join(parts[:-3]) if len(parts) > 3 else line
    except Exception:
        pass
    try:
        out = sh(["lsof", "-p", str(pid), "-a", "-d", "cwd", "-Fn"])
        m = re.search(r"^n(.+)$", out, re.M)
        if m:
            info["cwd"] = m.group(1)
    except Exception:
        pass
    return info


def project_of(detail):
    """从 cwd / cmdline 猜这是哪个项目 —— 一眼认出来比看 pid 有用"""
    cwd = detail.get("cwd") or ""
    cmd = detail.get("cmdline") or ""
    for src in (cwd, cmd):
        m = re.search(r"/(?:Desktop|Projects|code|Documents|work)/([^/\s]+)", src)
        if m:
            return m.group(1)
    if cwd:
        return os.path.basename(cwd.rstrip("/"))
    return ""


def scan():
    by_pid = listener_pids()
    rows = []
    for pid, d in by_pid.items():
        detail = process_detail(pid)
        ports = sorted(set(d["ports"]))
        rows.append({
            "pid": int(pid) if pid.isdigit() else 0,
            "command": d["command"],
            "ports": ports,
            "port_min": ports[0] if ports else 0,
            "bind": sorted(d["bind"]),
            "udp": bool(d.get("udp")),
            "cmdline": (detail.get("cmdline") or d["command"])[:180],
            "cwd": detail.get("cwd", ""),
            "project": project_of(detail),
            "uptime": detail.get("uptime", ""),
            "mem": detail.get("mem", ""),
            "cpu": detail.get("cpu", ""),
            "known": ", ".join(sorted({KNOWN.get(p, "") for p in ports if KNOWN.get(p)})) or "",
        })
    rows.sort(key=lambda r: r["port_min"])
    return rows


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "netport/" + VERSION

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
            return self._send(200, json.dumps({"version": VERSION, "known": KNOWN},
                                              ensure_ascii=False))
        if u.path == "/api/scan":
            t0 = time.time()
            rows = scan()
            log(f"[scan] {len(rows)} 个监听进程（{time.time()-t0:.2f}s）")
            return self._send(200, json.dumps({"rows": rows,
                                               "seconds": round(time.time() - t0, 2)},
                                              ensure_ascii=False))
        if u.path == "/api/check":
            import urllib.parse as up
            qs = up.parse_qs(u.query)
            try:
                port = int(qs.get("port", ["0"])[0])
            except Exception:
                port = 0
            rows = [r for r in scan() if port in r["ports"]]
            return self._send(200, json.dumps({"port": port, "rows": rows,
                                               "free": not rows}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        import urllib.parse
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/kill":
            try:
                pid = int(b.get("pid") or 0)
            except Exception:
                pid = 0
            if not pid or pid == os.getpid():
                return self._send(400, json.dumps({"error": "无效的 pid"}, ensure_ascii=False))
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.4)
                alive = True
                try:
                    os.kill(pid, 0)
                except Exception:
                    alive = False
                log(f"[kill] SIGTERM → {pid}（{'还在' if alive else '已退出'}）")
                return self._send(200, json.dumps(
                    {"ok": True, "alive": alive,
                     "message": "已发送结束信号" + ("（进程还在，可能要等一会或需要更强的方式）" if alive else "")},
                    ensure_ascii=False))
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": str(exc)},
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
    log(f"端口与进程 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ 端口与进程已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_scan(args):
    rows = scan()
    if args.port:
        rows = [r for r in rows if args.port in r["ports"]]
        if not rows:
            print(f"✅ 端口 {args.port} 空闲")
            return 0
    print(f"{'端口':<22} {'PID':<8} {'项目':<16} {'内存':<8} 命令")
    for r in rows:
        ports = ",".join(str(p) for p in r["ports"][:4]) + ("…" if len(r["ports"]) > 4 else "")
        print(f"  {ports:<20} {r['pid']:<8} {r['project'][:14]:<16} {r['mem']:<8} "
              f"{r['cmdline'][:56]}")
        if r["known"]:
            print(f"       ↳ {r['known']}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="netport", description=f"端口与进程 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("scan"); sp.add_argument("port", nargs="?", type=int)
    sp.set_defaults(f=cmd_scan)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
