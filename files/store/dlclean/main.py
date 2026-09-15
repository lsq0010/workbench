#!/usr/bin/env python3
"""下载夹管家 —— 找出可以删的东西，你自己决定删不删。

为什么做这个
  你的下载夹 30G：
    · 30 个 .dmg 安装包（装完就没用了）
    · 8 组重复下载（charles-proxy-5.1 (1).dmg 和 (2).dmg 都还在）
    · 没下完的 .crdownload
    · 一堆随机文件名的图
  手动翻太累，一键扫出来按"能省多少"排好。

安全设计（这类工具最怕误删）
  · **绝不动手删** —— 只列出来，删不删你点
  · 删走**废纸篓**（不是 rm），后悔了能捞回来
  · 每组重复只勾"多余的"，保留最早的那份
  · 装过的安装包才标记为可删，没装的不碰

零依赖。
"""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("DLCLEAN_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "dlclean.pid")
LOGFILE = os.path.join(HOME, "dlclean.log")
TRASHED = os.path.join(HOME, "trashed.jsonl")     # 只追加：删了什么，可追溯
DEFAULT_PORT = 8915

INSTALLER_EXT = (".dmg", ".pkg", ".mpkg", ".iso", ".exe", ".msi", ".zip", ".rar", ".7z",
                 ".tar", ".gz", ".xz")
INCOMPLETE_EXT = (".crdownload", ".part", ".download", ".partial", ".tmp")
# 这些扩展名一律不动（可能是唯一的原件）
NEVER = (".key", ".pem", ".p12", ".mobileprovision", ".cer", ".crt", ".sqlite", ".db")


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


def file_hash(path, chunk=1 << 20, limit=None):
    """算文件哈希。大文件只算头尾各 1MB + 大小，够用来判重"""
    h = hashlib.sha256()
    size = os.path.getsize(path)
    try:
        with open(path, "rb") as f:
            if limit and size > limit * 2:
                h.update(f.read(limit))
                f.seek(-limit, os.SEEK_END)
                h.update(f.read(limit))
                h.update(str(size).encode())
            else:
                while True:
                    b = f.read(chunk)
                    if not b:
                        break
                    h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def installed_apps():
    """已装的应用名（小写），用来判断安装包是不是已经装过了"""
    names = set()
    for d in ("/Applications", os.path.expanduser("~/Applications")):
        if not os.path.isdir(d):
            continue
        try:
            for n in os.listdir(d):
                if n.endswith(".app"):
                    names.add(n[:-4].lower())
        except OSError:
            pass
    return names


def guess_app_from_filename(name):
    """从安装包文件名里猜它是哪个 app"""
    n = name.lower()
    for ext in (".dmg", ".pkg", ".zip", ".iso", ".exe", ".mpkg"):
        if n.endswith(ext):
            n = n[:-len(ext)]
    n = re.sub(r"[\s_\-]*\(?\d+\)?$", "", n)          # 去掉结尾的 (1) -2
    n = re.sub(r"[-_]?(v?\d+(\.\d+)*(\.\d+)?).*$", "", n)   # 去掉版本号
    n = re.sub(r"[-_]?(arm64|x64|apple|silicon|mac|osx|darwin|universal|dmg|setup|installer|install|guanwang|new).*$", "", n)
    return n.strip("-_ .")


def scan(folder=None, deep=False):
    """扫下载夹，分类给出可清理项"""
    folder = folder or os.path.expanduser("~/Downloads")
    if not os.path.isdir(folder):
        return {"error": f"目录不存在：{folder}"}
    t0 = time.time()
    files = []
    try:
        for name in os.listdir(folder):
            if name.startswith("."):
                continue
            p = os.path.join(folder, name)
            if not os.path.isfile(p) or os.path.islink(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append({"name": name, "path": p, "size": st.st_size,
                          "mtime": st.st_mtime,
                          "age_days": (time.time() - st.st_mtime) / 86400,
                          "ext": os.path.splitext(name)[1].lower(),
                          "never": name.lower().endswith(NEVER)})
    except OSError as exc:
        return {"error": str(exc)}

    total = sum(f["size"] for f in files)
    apps = installed_apps()

    # ── 1. 重复文件（先按大小分组，再算哈希）──
    by_size = defaultdict(list)
    for f in files:
        if f["size"] > 0 and not f["never"]:
            by_size[f["size"]].append(f)
    dup_groups = []
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        by_hash = defaultdict(list)
        for f in group:
            h = file_hash(f["path"], limit=1 << 20)
            if h:
                by_hash[h].append(f)
        for h, same in by_hash.items():
            if len(same) >= 2:
                same.sort(key=lambda x: x["mtime"])       # 最早的排前面
                dup_groups.append({
                    "hash": h[:12],
                    "size": size,
                    "size_h": human(size),
                    "keep": same[0]["name"],                   # 建议保留最早的
                    "files": [{"name": x["name"], "path": x["path"],
                               "mtime_h": datetime.fromtimestamp(x["mtime"]).strftime("%Y-%m-%d"),
                               "age_days": round(x["age_days"])} for x in same],
                    "waste": size * (len(same) - 1),           # 删掉多余的能省多少
                })
    dup_groups.sort(key=lambda g: -g["waste"])

    # ── 2. 安装包（装过的、或者放很久的）──
    installers = []
    for f in files:
        if f["ext"] not in INSTALLER_EXT:
            continue
        if f["never"]:
            continue
        guess = guess_app_from_filename(f["name"])
        matched = None
        for a in apps:
            if guess and (guess in a or a in guess) and len(guess) > 3:
                matched = a
                break
        installers.append({
            "name": f["name"], "path": f["path"],
            "size_h": human(f["size"]), "size": f["size"],
            "age_days": round(f["age_days"]),
            "mtime_h": datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d"),
            "guessed_app": guess,
            "installed": bool(matched),
            "matched": matched or "",
            # 只管"装过了"和"超过 60 天"的
            "removable": bool(matched) or f["age_days"] > 60,
            "reason": ("这个应用已经装了" if matched else
                       f"放了 {round(f['age_days'])} 天了" if f["age_days"] > 60 else ""),
        })
    installers.sort(key=lambda x: (not x["removable"], -x["size"]))

    # ── 3. 没下完的 ──
    incomplete = [{"name": f["name"], "path": f["path"], "size_h": human(f["size"]),
                   "size": f["size"],
                   "age_days": round(f["age_days"]),
                   "mtime_h": datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d")}
                  for f in files if f["ext"] in INCOMPLETE_EXT]

    # ── 4. 大文件 ──
    big = sorted([f for f in files if f["size"] > 100 * 1024 * 1024],
                 key=lambda x: -x["size"])[:30]
    big = [{"name": f["name"], "path": f["path"], "size_h": human(f["size"]),
            "size": f["size"], "ext": f["ext"],
            "age_days": round(f["age_days"]),
            "mtime_h": datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d")} for f in big]

    # ── 5. 按类型汇总 ──
    by_ext = defaultdict(lambda: {"count": 0, "size": 0})
    for f in files:
        e = f["ext"] or "(无扩展名)"
        by_ext[e]["count"] += 1
        by_ext[e]["size"] += f["size"]
    ext_rows = sorted(({"ext": k, "count": v["count"], "size": v["size"],
                        "size_h": human(v["size"])} for k, v in by_ext.items()),
                      key=lambda x: -x["size"])[:20]

    reclaim = sum(g["waste"] for g in dup_groups) + \
              sum(i["size"] for i in installers if i["removable"]) + \
              sum(i["size"] for i in incomplete)

    result = {
        "folder": folder, "file_count": len(files), "total": total, "total_h": human(total),
        "dups": dup_groups, "installers": installers, "incomplete": incomplete,
        "big": big, "by_ext": ext_rows,
        "reclaim": reclaim, "reclaim_h": human(reclaim),
        "seconds": round(time.time() - t0, 2),
    }
    log(f"[scan] {folder} {len(files)} 个文件 {human(total)}；"
        f"可回收 {human(reclaim)}（重复 {len(dup_groups)} 组 / "
        f"安装包 {len([i for i in installers if i['removable']])} 个 / "
        f"没下完 {len(incomplete)} 个）")
    return result


def move_to_trash(paths):
    """移到废纸篓（不是直接删）。用 Finder 的 API，能"放回原处"。"""
    done, failed = [], []
    for p in paths:
        if not os.path.exists(p):
            failed.append({"path": p, "error": "文件不存在"})
            continue
        base = os.path.basename(p)
        if base.lower().endswith(NEVER):
            failed.append({"path": p, "error": "这类文件受保护，不动"})
            continue
        # 用 osascript 走 Finder，这样废纸篓里能"放回原处"
        script = f'tell application "Finder" to delete POSIX file {json.dumps(p)}'
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True,
                               text=True, timeout=20)
            if r.returncode == 0:
                done.append(p)
            else:
                # 退路：直接移到 ~/.Trash
                trash = os.path.expanduser("~/.Trash")
                dest = os.path.join(trash, base)
                i = 2
                while os.path.exists(dest):
                    stem, ext = os.path.splitext(base)
                    dest = os.path.join(trash, f"{stem}-{i}{ext}")
                    i += 1
                shutil.move(p, dest)
                done.append(p)
        except Exception as exc:
            failed.append({"path": p, "error": str(exc)})
    if done:
        with open(TRASHED, "a", encoding="utf-8") as f:      # 只追加
            f.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "count": len(done),
                                "paths": [os.path.basename(x) for x in done]},
                               ensure_ascii=False) + "\n")
        log(f"[trash] {len(done)} 个文件移到废纸篓")
    return {"ok": True, "moved": len(done), "failed": failed[:10],
            "moved_names": [os.path.basename(x) for x in done][:30]}


def trash_history(limit=10):
    rows = []
    try:
        with open(TRASHED, encoding="utf-8") as f:
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
    server_version = "dlclean/" + VERSION

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
        qs = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HOME, "index.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as exc:
                return self._send(500, f"<h1>{exc}</h1>", "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self._send(200, json.dumps({
                "version": VERSION,
                "downloads": os.path.expanduser("~/Downloads"),
                "trash": trash_history(),
            }, ensure_ascii=False))
        if u.path == "/api/scan":
            folder = (qs.get("folder", [os.path.expanduser("~/Downloads")])[0] or "").strip()
            return self._send(200, json.dumps(scan(folder), ensure_ascii=False))
        if u.path == "/api/digest":
            """只在下载夹明显失控时才提醒"""
            items = []
            try:
                folder = os.path.expanduser("~/Downloads")
                if os.path.isdir(folder):
                    n = 0
                    total = 0
                    old_installers = 0
                    for name in os.listdir(folder):
                        p = os.path.join(folder, name)
                        if not os.path.isfile(p):
                            continue
                        n += 1
                        total += os.path.getsize(p)
                        if name.lower().endswith(INSTALLER_EXT):
                            age = (time.time() - os.path.getmtime(p)) / 86400
                            if age > 60:
                                old_installers += 1
                    if total > 20 * 1024 ** 3:
                        items.append({"level": "warn",
                                      "title": f"下载夹已经 {human(total)} 了",
                                      "detail": f"{n} 个文件，其中 {old_installers} 个安装包放了 60 天以上",
                                      "action": "打开下载夹管家看看能清什么"})
                    elif old_installers >= 10:
                        items.append({"level": "info",
                                      "title": f"下载夹有 {old_installers} 个老安装包",
                                      "detail": f"总共 {human(total)}",
                                      "action": "打开下载夹管家"})
            except Exception as exc:
                log(f"[digest] {exc}")
            return self._send(200, json.dumps({"items": items}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/trash":
            paths = [p for p in (b.get("paths") or []) if p]
            if not paths:
                return self._send(400, json.dumps({"ok": False, "error": "没有选中任何文件"},
                                                  ensure_ascii=False))
            if len(paths) > 200:
                return self._send(400, json.dumps(
                    {"ok": False, "error": "一次最多处理 200 个"}, ensure_ascii=False))
            return self._send(200, json.dumps(move_to_trash(paths), ensure_ascii=False))
        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            target = p if os.path.isdir(p) else os.path.dirname(p)
            if target and os.path.isdir(target):
                subprocess.Popen(["open", target])
                return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return self._send(404, json.dumps({"ok": False, "error": "目录不存在"},
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
    log(f"下载夹管家 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ 下载夹管家已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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
    r = scan(args.folder)
    if r.get("error"):
        print("❌ " + r["error"])
        return 1
    print(f"{r['folder']}　{r['file_count']} 个文件　共 {r['total_h']}")
    print(f"可回收：{r['reclaim_h']}　（{r['seconds']}s）\n")
    if r["dups"]:
        print(f"  重复文件 {len(r['dups'])} 组，能省 {human(sum(g['waste'] for g in r['dups']))}")
        for g in r["dups"][:8]:
            print(f"    {g['size_h']} × {len(g['files'])} 份  保留 {g['keep']}")
            for f in g["files"]:
                print(f"        {f['mtime_h']}  {f['name'][:60]}")
    inst = [i for i in r["installers"] if i["removable"]]
    if inst:
        print(f"\n  可清理安装包 {len(inst)} 个，能省 {human(sum(i['size'] for i in inst))}")
        for i in inst[:10]:
            print(f"    {i['size_h']:>9}  {i['name'][:52]}　{i['reason']}")
    if r["incomplete"]:
        print(f"\n  没下完的 {len(r['incomplete'])} 个")
        for i in r["incomplete"][:8]:
            print(f"    {i['size_h']:>9}  {i['name'][:52]}　{i['mtime_h']}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="dlclean", description=f"下载夹管家 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("scan"); sp.add_argument("folder", nargs="?"); sp.set_defaults(f=cmd_scan)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
