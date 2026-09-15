#!/usr/bin/env python3
"""工程对比 —— 看同名文件在各个工程之间漂移了多少。

为什么做这个
  你有 5 个 iOS 工程共用同一套代码。实测：

      512 个文件在多个工程里都有
      其中 204 个**内容不一致**

  而且差异不是随机的，是**分组的**。比如 `UIKit+Extension.swift`：

      SDFPRO = SDFPRD2        （37 KB，同一份）
      A 工程 = B 工程           （22 KB，同一份）
      Customer-KAZ             （24 KB，又一份）

  这意味着：**在一个工程里修了 bug，另外两组不会得到这个修复。**
  这类"漂移"平时看不出来，攒久了就是"为什么 A 工程是好的 B 工程有 bug"。

这个工具做的
  ① 扫出所有跨工程共有的文件
  ② 按内容分组：完全一致 / 有差异
  ③ **对差异文件给出逐行对比**，看清到底改了什么
  ④ 标记可疑情况（比如某个工程里是空文件）

安全
  · 全程只读 —— 不改任何工程
  · 只读源码文件（.swift/.m/.h/.xib/.storyboard），不读别的
"""
import difflib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("PROJDIFF_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "projdiff.pid")
LOGFILE = os.path.join(HOME, "projdiff.log")
SCANS = os.path.join(HOME, "scans.jsonl")
CACHE = os.path.join(HOME, "index.json")
DEFAULT_PORT = 8924

SKIP_DIRS = {"Pods", "build", ".git", "DerivedData", ".build", "Carthage",
             "node_modules", ".claude", ".openclaw", ".venv", "__pycache__",
             "xcuserdata"}
EXTS = (".swift", ".m", ".mm", ".h", ".xib", ".storyboard", ".strings",
        ".plist", ".entitlements")
# 这些是生成的，差异是正常的，不算"漂移"
GENERATED = ("R.generated.swift", "Assets.generated.swift",
             "Storyboards.generated.swift", "Localizable.generated.swift",
             "Package.resolved", "Podfile.lock")
MAX_FILE_MB = 4


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} [projdiff] {msg}"
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


def find_projects(roots=None):
    """找桌面/文稿下的 iOS 工程（有足够多 swift 文件的目录）"""
    roots = roots or [os.path.expanduser("~/Desktop"),
                      os.path.expanduser("~/Documents")]
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if not os.path.isdir(p) or name.startswith(".") or name.endswith(".app"):
                continue
            n = 0
            for cur, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                n += sum(1 for f in files if f.endswith(".swift"))
                if n > 30:
                    break
            if n > 30:
                out.append({"path": p, "name": name, "swift": n})
    return out


def scan_project(root):
    """扫一个工程，返回 {相对路径: (md5, 字节数)}"""
    out = {}
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if not f.endswith(EXTS):
                continue
            p = os.path.join(cur, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if sz > MAX_FILE_MB * 1024 * 1024:
                continue
            try:
                with open(p, "rb") as fh:
                    h = hashlib.md5(fh.read()).hexdigest()
            except OSError:
                continue
            out[os.path.relpath(p, root)] = (h, sz)
    return out


def index(projects=None, refresh=False):
    """扫所有工程建索引"""
    if not refresh and os.path.isfile(CACHE):
        try:
            d = json.load(open(CACHE, encoding="utf-8"))
            if time.time() - d.get("ts", 0) < 3600:
                return d
        except Exception:
            pass
    if not projects:
        projects = [p["path"] for p in find_projects()]
    data = {}
    for p in projects:
        if os.path.isdir(p):
            data[os.path.basename(p)] = {"path": p, "files": scan_project(p)}
            log(f"{os.path.basename(p)}：{len(data[os.path.basename(p)]['files'])} 个文件")
    idx = {"ts": time.time(), "projects": data}
    try:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(idx, f)
    except Exception:
        pass
    return idx


def compare(projects=None, refresh=False, only_shared=True):
    """对比各工程"""
    idx = index(projects, refresh)
    projs = idx["projects"]
    if len(projs) < 2:
        return {"ok": False, "error": "少于 2 个工程，没法对比"}
    # 相对路径 -> {工程: (hash, size)}
    byrel = {}
    for name, d in projs.items():
        for rel, (h, sz) in d["files"].items():
            byrel.setdefault(rel, {})[name] = {"hash": h, "size": sz}

    same, diff, uniq = [], [], 0
    for rel, owners in byrel.items():
        if len(owners) < 2:
            uniq += 1
            continue
        hashes = set(v["hash"] for v in owners.values())
        generated = os.path.basename(rel) in GENERATED
        rec = {"file": rel, "projects": {k: v["size"] for k, v in owners.items()},
               "groups": len(hashes), "generated": generated}
        if len(hashes) == 1:
            same.append(rec)
        else:
            # 按内容分组，看清"谁和谁一样"
            grp = {}
            for name, v in owners.items():
                grp.setdefault(v["hash"], []).append(name)
            rec["same_as"] = [sorted(v) for v in grp.values()]
            rec["size_spread"] = max(v["size"] for v in owners.values()) - \
                                 min(v["size"] for v in owners.values())
            # 可疑：某个工程是空的（0~1KB）而别的不空
            sizes = [v["size"] for v in owners.values()]
            rec["suspicious"] = (min(sizes) < 1024 and max(sizes) > 10240)
            diff.append(rec)

    diff.sort(key=lambda x: (-x["size_spread"], x["file"]))
    return {
        "ok": True, "projects": {k: {"path": v["path"], "files": len(v["files"])}
                                 for k, v in projs.items()},
        "shared_same": len(same), "shared_diff": len(diff), "unique": uniq,
        "diff_files": diff[:300],
        "same_files": [x["file"] for x in same[:400]],
        "suspicious": [x for x in diff if x["suspicious"]],
        "verdict": ("%d 个共用文件里有 %d 个内容不一致"
                    % (len(same) + len(diff), len(diff))),
        "why": ("同名文件在不同工程里内容不同 —— 在一个工程修了 bug，"
                "另一个工程不会自动得到修复。生成的 R.generated.swift "
                "差异是正常的，已标记出来。"),
    }


def diff_two(project_a, project_b, rel, context=3):
    """给两个工程的同一个文件做逐行对比"""
    idx = index()
    projs = idx["projects"]
    a = projs.get(project_a) or projs.get(os.path.basename(project_a))
    b = projs.get(project_b) or projs.get(os.path.basename(project_b))
    if not a or not b:
        return {"ok": False, "error": "找不到工程：%s / %s" % (project_a, project_b)}
    pa = os.path.join(a["path"], rel)
    pb = os.path.join(b["path"], rel)
    for p in (pa, pb):
        if not os.path.isfile(p):
            return {"ok": False, "error": "文件不存在：" + p}
        # 防越界：必须在工程目录内
        if not os.path.abspath(p).startswith(os.path.abspath(a["path"] if p == pa
                                                             else b["path"]) + os.sep):
            return {"ok": False, "error": "路径越界"}
    try:
        la = open(pa, encoding="utf-8", errors="replace").read().splitlines()
        lb = open(pb, encoding="utf-8", errors="replace").read().splitlines()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    d = list(difflib.unified_diff(la, lb, fromfile="%s/%s" % (project_a, rel),
                                  tofile="%s/%s" % (project_b, rel),
                                  lineterm="", n=context))
    added = sum(1 for x in d if x.startswith("+") and not x.startswith("+++"))
    removed = sum(1 for x in d if x.startswith("-") and not x.startswith("---"))
    return {"ok": True, "a": project_a, "b": project_b, "file": rel,
            "a_lines": len(la), "b_lines": len(lb),
            "added": added, "removed": removed,
            "diff": "\n".join(d[:1200]),
            "truncated": len(d) > 1200}


# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "projdiff/" + VERSION

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
                "version": VERSION, "projects": find_projects(),
            }, ensure_ascii=False))
        if u.path == "/api/compare":
            r = compare(refresh=qs.get("refresh", ["0"])[0] == "1")
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if u.path == "/api/diff":
            r = diff_two((qs.get("a", [""])[0] or "").strip(),
                         (qs.get("b", [""])[0] or "").strip(),
                         (qs.get("file", [""])[0] or "").strip(),
                         int(qs.get("context", ["3"])[0] or 3))
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            if p and os.path.exists(p):
                subprocess.Popen(["open", p])
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
    log(f"工程对比 v{VERSION} 已启动 http://127.0.0.1:{port}")

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


def cmd_compare(args):
    r = compare(refresh=args.refresh)
    if not r.get("ok"):
        print("  ❌", r.get("error"))
        return 1
    print("  工程：")
    for k, v in r["projects"].items():
        print("    %-14s %d 个源文件" % (k, v["files"]))
    print()
    print("  共用文件 %d 个：一致 %d ✅ / **不一致 %d** ⚠️"
          % (r["shared_same"] + r["shared_diff"], r["shared_same"], r["shared_diff"]))
    print()
    if r["suspicious"]:
        print("  可疑（某个工程里几乎是空的）：")
        for x in r["suspicious"]:
            print("    %s  %s" % (x["file"], x["projects"]))
        print()
    print("  差异最大的 15 个：")
    for x in r["diff_files"][:15]:
        if x["generated"]:
            continue
        print("    %-52s %s" % (x["file"][:52],
              "  ".join("%s:%dKB" % (k[:8], v // 1024)
                        for k, v in sorted(x["projects"].items()))[:70]))
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="projdiff", description=f"工程对比 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("compare"); sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(f=cmd_compare)
    a = p.parse_args()
    sys.exit(a.f(a))


if __name__ == "__main__":
    main()
