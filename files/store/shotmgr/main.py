#!/usr/bin/env python3
"""截图管家 —— 把散落的截图理清楚，还能让 AI 看懂每张是什么。

为什么做这个
  你桌面上有 21 张散落的截图，一天就产生 10 张。
  截图是"临时看一眼"的东西，看完就没用了 —— 但攒着又乱。

这个工具管三件事
  ① 找出所有截图（桌面、截图文件夹），按时间列清
  ② **用 AI 看每张是什么**（写一句说明），这样不用点开就知道内容
  ③ 一键归档到「截图/年-月/」，或者删掉重复的

安全
  · **默认只读**。归档/删除都要你明确点，而且先给预览
  · 删除走废纸篓（macOS 的 Trash），不是 rm —— 反悔还能捞回来
  · 原图不动，除非你点归档
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("SHOTMGR_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "shotmgr.pid")
LOGFILE = os.path.join(HOME, "shotmgr.log")
INDEX = os.path.join(HOME, "shots.jsonl")        # 只追加：AI 看过的说明
DEFAULT_PORT = 8921
PLATFORM = "http://127.0.0.1:8880"

# 从哪儿找截图
SOURCES = [
    (os.path.expanduser("~/Desktop"), "桌面"),
    (os.path.expanduser("~/Pictures/Screenshots"), "截图文件夹"),
    (os.path.expanduser("~/Pictures/截图"), "截图文件夹"),
    (os.path.expanduser("~/Desktop/截图"), "桌面/截图"),
]
IMG_EXT = (".png", ".jpg", ".jpeg", ".heic", ".webp")
# macOS 截图文件名：截屏2026-09-14 21.30.42.png / Screenshot 2026-09-14 at 21.30.42.png
SHOT_RE = re.compile(
    r"(截屏|Screenshot|截图|CleanShot|Screen Shot)\s*"
    r"(\d{4})[-年.](\d{1,2})[-月.](\d{1,2})日?\s*(?:at\s*)?"
    r"(\d{1,2})[.：:](\d{2})[.：:]?(\d{2})?", re.I)
ARCHIVE_ROOT = os.path.expanduser("~/Desktop/截图归档")


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} [shotmgr] {msg}"
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


def is_screenshot(path):
    """判断这是不是一张截图（靠文件名，不读内容 —— 快）"""
    name = os.path.basename(path)
    if not name.lower().endswith(IMG_EXT):
        return None
    m = SHOT_RE.search(name)
    if m:
        try:
            y, mo, d = int(m.group(2)), int(m.group(3)), int(m.group(4))
            hh, mm = int(m.group(5)), int(m.group(6))
            ss = int(m.group(7) or 0)
            return datetime(y, mo, d, hh, mm, ss)
        except ValueError:
            pass
    # 名字里带"截屏/截图/screenshot"也算，时间用文件时间
    if re.search(r"截屏|截图|screenshot|screencap", name, re.I):
        try:
            return datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            return None
    return None


def md5_of(path, limit=8 * 1024 * 1024):
    import hashlib
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(65536)
                if not b:
                    break
                h.update(b)
                if f.tell() > limit:
                    break
    except OSError:
        return None
    return h.hexdigest()


def load_index():
    """读 AI 看过的说明（同一路径取最新一条）"""
    out = {}
    try:
        with open(INDEX, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("path"):
                    out[r["path"]] = r
    except Exception:
        pass
    return out


def scan(days=30, limit=300):
    """扫出截图。只读，不动任何文件。"""
    seen, items = set(), []
    cutoff = time.time() - days * 86400
    for root, src in SOURCES:
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            p = os.path.join(root, n)
            if not os.path.isfile(p) or p in seen:
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            ts = is_screenshot(p)
            if not ts:
                continue
            seen.add(p)
            items.append({"path": p, "name": n, "src": src,
                          "kb": round(st.st_size / 1024, 1),
                          "shot_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
                          "month": ts.strftime("%Y-%m"),
                          "mtime": st.st_mtime})
    # 找重复（同样大小 + 同样 md5）
    by_size = {}
    for x in items:
        by_size.setdefault(x["kb"], []).append(x)
    dups = 0
    for kb, group in by_size.items():
        if len(group) < 2:
            continue
        hashes = {}
        for x in group:
            h = md5_of(x["path"])
            x["md5"] = h
            if h and h in hashes:
                x["dup_of"] = hashes[h]
                dups += 1
            elif h:
                hashes[h] = x["path"]
    items.sort(key=lambda x: -x["mtime"])
    idx = load_index()
    for x in items:
        r = idx.get(x["path"])
        x["desc"] = (r or {}).get("desc") or ""
    return {"items": items[:limit], "total": len(items), "dups": dups,
            "roots": [r for r, _ in SOURCES if os.path.isdir(r)],
            "archive_root": ARCHIVE_ROOT}


def describe(paths, force=False):
    """让 AI 看这些截图，各写一句说明。"""
    idx = load_index()
    todo = [p for p in paths
            if os.path.isfile(p) and (force or not (idx.get(p) or {}).get("desc"))]
    if not todo:
        return {"ok": True, "done": 0, "note": "这些都已经看过了"}
    done, failed = 0, []
    for p in todo[:8]:                       # 一次最多看 8 张，免得等太久
        try:
            req = urllib.request.Request(
                PLATFORM + "/api/ai/vision",
                data=json.dumps({"paths": [p], "q":
                    "用一句话（不超过 30 字）说明这张截图是什么内容。"
                    "直接给这一句，不要分点、不要客套。"}).encode(),
                headers={"Content-Type": "application/json"})
            text = ""
            for raw in urllib.request.urlopen(req, timeout=240):
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except Exception:
                    continue
                if ev.get("type") == "text":
                    text += ev["text"]
                elif ev.get("type") == "error":
                    failed.append((os.path.basename(p), ev["error"]))
                    text = ""
                    break
                elif ev.get("type") == "done":
                    break
            text = re.sub(r"\s+", " ", text).strip().strip('"').strip()
            text = re.sub(r"^\*\*|\*\*$", "", text)[:80]
            if text:
                rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "user_id": _user_id(), "path": p,
                       "name": os.path.basename(p), "desc": text}
                with open(INDEX, "a", encoding="utf-8") as f:   # 只追加
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                done += 1
        except Exception as exc:
            failed.append((os.path.basename(p), str(exc)[:80]))
    return {"ok": True, "done": done, "failed": failed,
            "left": max(0, len(todo) - 8)}


def archive(paths, dry=True):
    """按月份归档到 ~/Desktop/截图归档/年-月/。dry=True 时只报告。"""
    plan, missing = [], []
    for p in paths or []:
        if not os.path.isfile(p):
            missing.append(p)
            continue
        ts = is_screenshot(p) or datetime.fromtimestamp(os.path.getmtime(p))
        sub = ts.strftime("%Y-%m")
        dst_dir = os.path.join(ARCHIVE_ROOT, sub)
        dst = os.path.join(dst_dir, os.path.basename(p))
        # 重名就加序号
        base, ext = os.path.splitext(os.path.basename(p))
        i = 1
        while os.path.exists(dst):
            dst = os.path.join(dst_dir, "%s-%d%s" % (base, i, ext))
            i += 1
        plan.append({"from": p, "to": dst, "month": sub})
    if dry:
        return {"ok": True, "dry": True, "plan": plan, "count": len(plan),
                "root": ARCHIVE_ROOT,
                "note": "这是预览，点了「确认归档」才会真动"}
    moved = []
    for x in plan:
        try:
            os.makedirs(os.path.dirname(x["to"]), exist_ok=True)
            shutil.move(x["from"], x["to"])
            moved.append(x)
        except Exception as exc:
            log(f"归档失败 {x['from']}：{exc}")
    log(f"归档了 {len(moved)} 张到 {ARCHIVE_ROOT}")
    return {"ok": True, "dry": False, "moved": len(moved), "root": ARCHIVE_ROOT}


def trash(paths):
    """把文件移到废纸篓 —— **不是 rm**，反悔还能捞回来。"""
    moved, failed = [], []
    trash_dir = os.path.expanduser("~/.Trash")
    for p in paths or []:
        if not os.path.isfile(p):
            failed.append((p, "文件不在"))
            continue
        # 只允许删"扫描出来的截图" —— 不做成任意文件删除接口
        t = is_screenshot(p)
        if not t:
            failed.append((p, "不是截图，拒绝"))
            continue
        try:
            base, ext = os.path.splitext(os.path.basename(p))
            dst = os.path.join(trash_dir, os.path.basename(p))
            i = 1
            while os.path.exists(dst):
                dst = os.path.join(trash_dir, "%s-%d%s" % (base, i, ext))
                i += 1
            shutil.move(p, dst)
            moved.append(p)
        except Exception as exc:
            failed.append((p, str(exc)[:80]))
    log(f"移到废纸篓 {len(moved)} 张")
    return {"ok": True, "moved": len(moved), "failed": failed,
            "note": "都进了废纸篓，反悔了去那儿捞"}


# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "shotmgr/" + VERSION

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
                "version": VERSION, "archive_root": ARCHIVE_ROOT,
                "sources": [r for r, _ in SOURCES if os.path.isdir(r)],
            }, ensure_ascii=False))
        if u.path == "/api/scan":
            days = int(qs.get("days", ["30"])[0] or 30)
            return self._send(200, json.dumps(scan(days), ensure_ascii=False))
        if u.path == "/api/thumb":
            """缩略图 —— 只给扫描得到的截图，不做成任意文件读取"""
            p = (qs.get("path", [""])[0] or "").strip()
            if not p or not os.path.isfile(p):
                return self._send(404, b"not found", "text/plain")
            if not is_screenshot(p):
                return self._send(403, b"not a screenshot", "text/plain")
            try:
                with open(p, "rb") as f:
                    data = f.read(20 * 1024 * 1024)
            except OSError as exc:
                return self._send(500, str(exc).encode(), "text/plain")
            low = p.lower()
            ctype = ("image/jpeg" if low.endswith((".jpg", ".jpeg")) else
                     "image/webp" if low.endswith(".webp") else "image/png")
            return self._send(200, data, ctype)
        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/describe":
            return self._send(200, json.dumps(
                describe(b.get("paths") or [], bool(b.get("force"))),
                ensure_ascii=False))
        if u.path == "/api/archive":
            return self._send(200, json.dumps(
                archive(b.get("paths") or [], dry=bool(b.get("dry", True))),
                ensure_ascii=False))
        if u.path == "/api/trash":
            return self._send(200, json.dumps(trash(b.get("paths") or []),
                                              ensure_ascii=False))
        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            if p and os.path.exists(p):
                subprocess.Popen(["open", "-R", p] if os.path.isfile(p) else ["open", p])
                return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return self._send(404, json.dumps({"ok": False, "error": "路径不存在"},
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
    log(f"截图管家 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    subprocess.Popen([sys.executable, os.path.realpath(__file__), "serve",
                      "--port", str(port)],
                     stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True, cwd=HOME)
    for _ in range(30):
        time.sleep(0.2)
        if running_pid():
            break
    print(f"✅ 已启动 http://127.0.0.1:{port}" if running_pid() else "❌ 启动失败")
    return 0 if running_pid() else 1


def cmd_stop(args):
    pid = running_pid()
    if not pid:
        print("没有在运行")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.6)
    except Exception:
        pass
    try:
        os.remove(PIDFILE)
    except Exception:
        pass
    print("已停止")
    return 0


def cmd_scan(args):
    r = scan(args.days)
    print(f"  找到 {r['total']} 张截图（{r['dups']} 张重复）")
    print("  %-44s %-18s %s" % ("文件", "时间", "说明"))
    for x in r["items"][:40]:
        print("  %-44s %-18s %s" % (x["name"][:44], x["shot_at"][:18],
                                    x["desc"][:30] or ""))
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="shotmgr", description=f"截图管家 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("scan"); sp.add_argument("--days", type=int, default=30)
    sp.set_defaults(f=cmd_scan)
    a = p.parse_args()
    sys.exit(a.f(a))


if __name__ == "__main__":
    main()
