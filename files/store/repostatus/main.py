#!/usr/bin/env python3
"""仓库总览 —— 一屏看清所有 git 仓库的状态。

为什么做这个
  手里十几个仓库时，最烦的是"我到底哪个仓库还有没提交的改动？哪个分支落后了？"
  一个个 cd 进去 git status 太慢。这里一次列全：分支、脏不脏、领先/落后远端、
  有几条 stash、最后一次提交。

顺带能做的动作（只读之外都标了风险）
  · 一键 git fetch（只更新远端引用，不动工作区）
  · 复制仓库路径
  · 打开仓库目录

零第三方依赖。
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("REPOSTATUS_HOME") or SCRIPT_DIR)
CONFIGFILE = os.path.join(HOME, "config.json")
PIDFILE = os.path.join(HOME, "repostatus.pid")
LOGFILE = os.path.join(HOME, "repostatus.log")
DEFAULT_PORT = 8897


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
    c.setdefault("repos", [])
    return c


def git(repo, *args, timeout=20):
    try:
        p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                           text=True, timeout=timeout, errors="replace")
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def is_repo(path):
    return os.path.isdir(os.path.join(path, ".git")) or bool(git(path, "rev-parse", "--git-dir"))


def discover(roots=None, depth=2):
    roots = roots or [os.path.expanduser("~/Desktop"), os.path.expanduser("~/Documents"),
                      os.path.expanduser("~/Projects")]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        base = root.rstrip("/").count("/")
        for cur, dirs, _ in os.walk(root):
            if cur.count("/") - base >= depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("node_modules", "Pods", "build", "DerivedData")]
            if ".git" in os.listdir(cur):
                found.append(cur)
                dirs[:] = []
    return sorted(set(found))


def repo_status(path):
    """一个仓库的全部状态，一次 git 调用拿多条信息"""
    name = os.path.basename(path)
    if not is_repo(path):
        return {"path": path, "name": name, "broken": "不是 git 仓库"}

    st = {"path": path, "name": name}
    # 分支 / HEAD 状态
    head = git(path, "status", "--porcelain=v1", "--branch")
    lines = head.splitlines()
    branch, ahead, behind = "", 0, 0
    if lines and lines[0].startswith("##"):
        b = lines[0][3:]
        m = re.search(r"ahead (\d+)", b)
        if m:
            ahead = int(m.group(1))
        m = re.search(r"behind (\d+)", b)
        if m:
            behind = int(m.group(1))
        branch = b.split("...")[0].strip()
    st["branch"] = branch or "?"
    st["ahead"], st["behind"] = ahead, behind
    st["detached"] = branch.startswith("HEAD") or "(no branch)" in branch

    files = [l for l in lines[1:] if l.strip()]
    st["changed"] = len(files)
    st["staged"] = len([l for l in files if l[0] not in " ?"])
    st["unstaged"] = len([l for l in files if len(l) > 1 and l[1] not in " ?"])
    st["untracked"] = len([l for l in files if l.startswith("??")])
    st["conflicts"] = len([l for l in files if l[:2] in ("UU", "AA", "DD", "AU", "UA", "DU", "UD")])

    st["last_subject"] = git(path, "log", "-1", "--pretty=%s")[:70]
    st["last_date"] = git(path, "log", "-1", "--date=format:%m-%d %H:%M", "--pretty=%ad")
    st["last_author"] = git(path, "log", "-1", "--pretty=%an")
    stash = git(path, "stash", "list")
    st["stashes"] = len([x for x in stash.splitlines() if x.strip()])
    st["head"] = git(path, "rev-parse", "--short", "HEAD")
    st["remote"] = git(path, "remote", "get-url", "origin")[:80]
    # 有没有 .git 目录被锁（合并/变基中断）
    gr = os.path.join(path, ".git")
    st["merging"] = os.path.exists(os.path.join(gr, "MERGE_HEAD"))
    st["rebasing"] = os.path.isdir(os.path.join(gr, "rebase-merge")) or \
                     os.path.isdir(os.path.join(gr, "rebase-apply"))
    st["branches"] = len([x for x in git(path, "branch", "--list").splitlines() if x.strip()])
    return st


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "repostatus/" + VERSION

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
            c = cfg()
            return self._send(200, json.dumps({"version": VERSION, "repos": len(c["repos"])},
                                              ensure_ascii=False))

        if u.path == "/api/repos":
            c = cfg()
            t0 = time.time()
            out = [repo_status(p) for p in c["repos"]]
            out.sort(key=lambda r: (-(r.get("changed") or 0), -(r.get("conflicts") or 0),
                                    r.get("name", "")))
            log(f"[scan] {len(out)} 个仓库，用时 {time.time()-t0:.2f}s")
            return self._send(200, json.dumps({"repos": out, "seconds": round(time.time()-t0, 2)},
                                              ensure_ascii=False))

        if u.path == "/api/discover":
            found = discover()
            c = cfg()
            return self._send(200, json.dumps({"found": found,
                                               "new": [p for p in found if p not in c["repos"]]},
                                              ensure_ascii=False))

        if u.path == "/api/detail":
            repo = (qs.get("path", [""])[0] or "").strip()
            if not is_repo(repo):
                return self._send(404, json.dumps({"error": "不是 git 仓库"}, ensure_ascii=False))
            return self._send(200, json.dumps({
                "status": git(repo, "status", "--short"),
                "log": git(repo, "log", "-15", "--date=format:%m-%d %H:%M",
                           "--pretty=format:%h|%ad|%an|%s"),
                "branches": git(repo, "branch", "-vv"),
                "stash": git(repo, "stash", "list"),
            }, ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/config":
            c = cfg()
            if b.get("repos") is not None:
                c["repos"] = [p for p in b["repos"] if p]
            write_json(CONFIGFILE, c)
            log(f"[config] 仓库列表 {len(c['repos'])} 个")
            return self._send(200, json.dumps({"ok": True, "repos": c["repos"]},
                                              ensure_ascii=False))

        if u.path == "/api/fetch":
            # 只更新远端引用，不碰工作区 —— 安全
            repo = (b.get("path") or "").strip()
            if not is_repo(repo):
                return self._send(404, json.dumps({"error": "不是 git 仓库"}, ensure_ascii=False))
            t0 = time.time()
            out = git(repo, "fetch", "--all", "--prune", timeout=60)
            log(f"[fetch] {os.path.basename(repo)} 用时 {time.time()-t0:.1f}s")
            return self._send(200, json.dumps({"ok": True, "output": out[-400:],
                                               "seconds": round(time.time()-t0, 1)},
                                              ensure_ascii=False))

        if u.path == "/api/open":
            repo = (b.get("path") or "").strip()
            if not os.path.isdir(repo):
                return self._send(404, json.dumps({"error": "目录不存在"}, ensure_ascii=False))
            subprocess.Popen(["open", repo])
            return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))

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
    if not c["repos"]:
        found = discover()
        if found:
            c["repos"] = found
            write_json(CONFIGFILE, c)
            log(f"[init] 自动发现 {len(found)} 个仓库")
    log(f"仓库总览 v{VERSION} 已启动 http://127.0.0.1:{port}（{len(c['repos'])} 个仓库）")

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
    print(f"✅ 仓库总览已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_list(args):
    c = cfg()
    repos = c["repos"] or discover()
    rows = [repo_status(p) for p in repos]
    rows.sort(key=lambda r: (-(r.get("changed") or 0), r.get("name", "")))
    print(f"{'仓库':<22} {'分支':<34} {'改动':<6} {'领先/落后':<10} 最后提交")
    for r in rows:
        if r.get("broken"):
            print(f"  {r['name']:<20} {r['broken']}")
            continue
        dirty = f"{r['changed']} 个" if r["changed"] else "干净"
        ab = f"+{r['ahead']}/-{r['behind']}" if (r["ahead"] or r["behind"]) else "—"
        flag = " ⚠️冲突" if r["conflicts"] else (" 🔀合并中" if r["merging"] else
                                              (" ⏳变基中" if r["rebasing"] else ""))
        print(f"  {r['name']:<20} {r['branch'][:32]:<34} {dirty:<6} {ab:<10} "
              f"{r['last_date']} {r['last_subject'][:30]}{flag}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="repostatus", description=f"仓库总览 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sub.add_parser("list").set_defaults(f=cmd_list)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
