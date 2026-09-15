#!/usr/bin/env python3
"""磁盘占用 —— 到底什么占了空间。

为什么做这个
  "存储空间不足" 时，系统设置只告诉你"文稿 120GB"这种粒度，没法定位。
  这里从任意目录往下钻，一层层告诉你哪个子目录最大，能按大小排序、过滤。

做法
  用 Python 走目录 + os.scandir，遇到大目录也能跑；同一个文件只算一次（硬链接）；
  跳过 .git 之类的元数据可以选。

只读，不删除任何东西。
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("DISKUSAGE_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "diskusage.pid")
LOGFILE = os.path.join(HOME, "diskusage.log")
DEFAULT_PORT = 8906

# 系统里常见的"空间黑洞"，标出来让用户知道能不能动
BIG_OFFENDERS = {
    "DerivedData": "Xcode 编译缓存 —— 删了会重新编译，安全",
    "node_modules": "npm 依赖 —— 删了 npm install 能装回来",
    "Pods": "CocoaPods 依赖 —— 删了 pod install 能装回来",
    "__pycache__": "Python 字节码缓存 —— 安全",
    ".build": "Swift PM 构建产物 —— 安全",
    "build": "构建产物 —— 一般安全",
    ".Trash": "废纸篓 —— 清空即可",
    "Library/Caches": "应用缓存 —— 大部分安全",
    "iOS DeviceSupport": "旧设备符号 —— 老设备不用了可以删",
    "Archives": "Xcode 归档 —— 里面是历史包，确认不需要再删",
    "Simulators": "模拟器运行时 —— 删了会重新下",
    "Containers": "应用沙盒数据",
    "Downloads": "下载目录",
    "Photos Library.photoslibrary": "照片图库 —— 别手动删",
}


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def dir_size(path, skip_names=None, seen=None, budget=None):
    """算目录大小。seen 用来避免硬链接重复计数"""
    skip_names = skip_names or set()
    seen = seen if seen is not None else set()
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name in skip_names:
                    continue
                try:
                    if e.is_symlink():
                        continue
                    if e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                        total += st.st_size
                    elif e.is_dir(follow_symlinks=False):
                        total += dir_size(e.path, skip_names, seen, budget)
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        pass
    return total


def top_level(path, skip_names=None, limit=40):
    """一个目录下，各子项的大小排行"""
    out = []
    try:
        entries = list(os.scandir(path))
    except Exception as exc:
        return {"error": f"读不了这个目录：{exc}"}
    seen = set()
    for e in entries:
        if e.name in (skip_names or set()):
            continue
        try:
            if e.is_symlink():
                continue
            if e.is_file(follow_symlinks=False):
                st = e.stat(follow_symlinks=False)
                out.append({"name": e.name, "path": e.path, "dir": False,
                            "size": st.st_size, "mtime": st.st_mtime,
                            "note": BIG_OFFENDERS.get(e.name, "")})
            elif e.is_dir(follow_symlinks=False):
                sz = dir_size(e.path, skip_names, seen)
                out.append({"name": e.name, "path": e.path, "dir": True,
                            "size": sz, "mtime": e.stat(follow_symlinks=False).st_mtime,
                            "note": BIG_OFFENDERS.get(e.name, "")})
        except (PermissionError, OSError):
            continue
    out.sort(key=lambda x: -x["size"])
    total = sum(x["size"] for x in out)
    for x in out:
        x["size_h"] = human(x["size"])
        x["pct"] = round(x["size"] / total * 100, 1) if total else 0
        x["mtime_h"] = datetime.fromtimestamp(x["mtime"]).strftime("%Y-%m-%d")
    return {"path": path, "items": out[:limit], "total": total,
            "total_h": human(total), "count": len(out)}


def biggest_files(path, limit=30, min_size=50 * 1024 * 1024):
    """找出最大的文件（默认 50MB 以上）"""
    out = []
    seen = set()
    for cur, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".") or d in (".Trash",)]
        for f in files:
            p = os.path.join(cur, f)
            try:
                st = os.stat(p)
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                if st.st_size >= min_size:
                    out.append({"name": f, "path": p, "size": st.st_size,
                                "size_h": human(st.st_size),
                                "mtime": st.st_mtime,
                                "mtime_h": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")})
            except (OSError, PermissionError):
                continue
        if len(out) > 4000:
            break
    out.sort(key=lambda x: -x["size"])
    return out[:limit]


def disk_info():
    """各挂载点用了多少"""
    out = []
    try:
        p = subprocess.run(["df", "-h"], capture_output=True, text=True, timeout=10,
                           errors="replace")
        for line in p.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 9:
                continue
            fs, size, used, avail, cap, mnt = parts[0], parts[1], parts[2], parts[3], parts[4], parts[-1]
            if not fs.startswith("/dev/") and fs != "map":
                continue
            out.append({"fs": fs, "size": size, "used": used, "avail": avail,
                        "cap": cap, "mount": mnt})
    except Exception:
        pass
    return out


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "diskusage/" + VERSION

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
        qs = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HOME, "index.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as exc:
                return self._send(500, f"<h1>{exc}</h1>", "text/html; charset=utf-8")

        if u.path == "/api/status":
            return self._send(200, json.dumps({
                "version": VERSION, "disks": disk_info(),
                "home": os.path.expanduser("~"),
                "offenders": BIG_OFFENDERS,
            }, ensure_ascii=False))

        if u.path == "/api/scan":
            path = (qs.get("path", [os.path.expanduser("~")])[0] or "").strip()
            if not os.path.isdir(path):
                return self._send(400, json.dumps({"error": "目录不存在"}, ensure_ascii=False))
            t0 = time.time()
            d = top_level(path)
            d["seconds"] = round(time.time() - t0, 2)
            log(f"[scan] {path} → {d.get('count',0)} 项 {d.get('total_h','')}（{d['seconds']}s）")
            return self._send(200, json.dumps(d, ensure_ascii=False))

        if u.path == "/api/bigfiles":
            path = (qs.get("path", [os.path.expanduser("~")])[0] or "").strip()
            mb = int(qs.get("min_mb", ["50"])[0])
            t0 = time.time()
            files = biggest_files(path, min_size=mb * 1024 * 1024)
            total = sum(f["size"] for f in files)
            log(f"[bigfiles] {path} → {len(files)} 个 ≥{mb}MB，共 {human(total)}")
            return self._send(200, json.dumps({
                "files": files, "total": total, "total_h": human(total),
                "count": len(files), "seconds": round(time.time() - t0, 2),
            }, ensure_ascii=False))

        if u.path == "/api/folder":
            path = (qs.get("path", [os.path.expanduser("~")])[0] or "").strip()
            try:
                dirs = []
                for name in sorted(os.listdir(path)):
                    if name.startswith("."):
                        continue
                    p = os.path.join(path, name)
                    if os.path.isdir(p):
                        dirs.append({"name": name, "path": p})
                return self._send(200, json.dumps({"path": path, "dirs": dirs[:200],
                                                   "parent": os.path.dirname(path)},
                                                  ensure_ascii=False))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = self.__class__.__mro__ and None
        import urllib.parse
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            target = p if os.path.isdir(p) else os.path.dirname(p)
            if target and os.path.isdir(target):
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
    log(f"磁盘占用 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ 磁盘占用已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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
    d = top_level(args.path or os.path.expanduser("~"))
    if d.get("error"):
        print("❌ " + d["error"])
        return 1
    print(f"{d['path']}  共 {d['total_h']}")
    for x in d["items"][:args.limit]:
        bar = "█" * max(1, int(x["pct"] / 2))
        tag = f"  ← {x['note']}" if x.get("note") else ""
        print(f"  {x['size_h']:>9}  {x['pct']:>5}%  {bar} {x['name'][:40]}{tag}")
    return 0


def cmd_big(args):
    files = biggest_files(args.path or os.path.expanduser("~"),
                          min_size=args.min_mb * 1024 * 1024)
    print(f"最大的文件（≥{args.min_mb}MB）：")
    for f in files[:args.limit]:
        print(f"  {f['size_h']:>9}  {f['mtime_h']}  {f['path'][:88]}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="diskusage", description=f"磁盘占用 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("scan"); sp.add_argument("path", nargs="?")
    sp.add_argument("--limit", type=int, default=20); sp.set_defaults(f=cmd_scan)
    sp = sub.add_parser("big"); sp.add_argument("path", nargs="?")
    sp.add_argument("--min-mb", type=int, default=100); sp.add_argument("--limit", type=int, default=25)
    sp.set_defaults(f=cmd_big)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
