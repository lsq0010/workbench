#!/usr/bin/env python3
"""便签 / 知识库 —— 把"只存在于对话里"的东西落到磁盘。

为什么做这个
  这次会话反复出现同一个问题：结论只存在于聊天记录里，一关就找不回来。
  （"7 条语义里 6 条不在磁盘上" —— 就是这么发现的。）
  这个工具就是给这类东西一个落脚点：命令、配置、接口结论、临时 ID、待办。

设计
  · 一条便签 = 标题 + 正文 + 标签。存 JSONL，只追加不覆盖（改一条会写新版本）
  · 全文搜索（标题/正文/标签）
  · 落到磁盘就是纯文本，别的工具和 AI 都能直接读 —— 不锁在某个 app 里
  · 带平台身份，以后多设备同步时知道是谁写的

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
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("NOTES_HOME") or SCRIPT_DIR)
NOTEFILE = os.path.join(HOME, "notes.jsonl")        # 只追加：改一条 = 追加一个新版本
PIDFILE = os.path.join(HOME, "notes.pid")
LOGFILE = os.path.join(HOME, "notes.log")
DEFAULT_PORT = 8902

# 平台身份（跨功能同一个 user_id）
sys.path.insert(0, os.path.join(HOME, "..", "..", "lib"))


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def user_id():
    try:
        import platform_lib
        return platform_lib.user_id()
    except Exception:
        return ""


def load_notes():
    """读全部便签，同 id 取最后一个版本（后写的赢）"""
    by_id = {}
    order = []
    try:
        with open(NOTEFILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    n = json.loads(line)
                except Exception:
                    continue
                nid = n.get("id")
                if not nid:
                    continue
                if nid not in by_id:
                    order.append(nid)
                if n.get("deleted"):
                    by_id.pop(nid, None)
                    if nid in order:
                        order.remove(nid)
                else:
                    by_id[nid] = n
    except Exception:
        pass
    notes = [by_id[i] for i in order if i in by_id]
    notes.sort(key=lambda n: (not n.get("pinned"), n.get("updated", "")), reverse=False)
    notes.sort(key=lambda n: (not n.get("pinned"),), reverse=False)
    notes.sort(key=lambda n: n.get("updated", ""), reverse=True)
    notes.sort(key=lambda n: not n.get("pinned", False))     # 置顶的排前面
    return notes


def append_note(n):
    with open(NOTEFILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(n, ensure_ascii=False) + "\n")


def save_note(nid=None, title="", body="", tags=None, pinned=None, deleted=False):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = {
        "id": nid or ("n_" + uuid.uuid4().hex[:10]),
        "title": (title or "").strip() or "（无标题）",
        "body": body or "",
        "tags": [t.strip() for t in (tags or []) if str(t).strip()],
        "pinned": bool(pinned),
        "user_id": user_id(),
        "created": now,
        "updated": now,
        "version": 1,
    }
    if nid:
        old = next((x for x in load_notes() if x["id"] == nid), None)
        if old:
            n["created"] = old.get("created", now)
            n["version"] = int(old.get("version", 1)) + 1
            # 没传的字段沿用旧值
            if title is None or title == "":
                n["title"] = old.get("title", n["title"])
            if body is None or body == "":
                n["body"] = old.get("body", "")
            if tags is None:
                n["tags"] = old.get("tags", [])
            if pinned is None:
                n["pinned"] = old.get("pinned", False)
    if deleted:
        n["deleted"] = True
    append_note(n)
    log(f"[note] {'删除' if deleted else ('更新' if nid else '新建')} {n['id']} "
        f"v{n['version']} {n['title'][:30]}")
    return n


def all_tags():
    c = {}
    for n in load_notes():
        for t in n.get("tags", []):
            c[t] = c.get(t, 0) + 1
    return sorted(c.items(), key=lambda x: -x[1])


def search(q):
    q = (q or "").strip().lower()
    if not q:
        return load_notes()
    out = []
    for n in load_notes():
        hay = (n.get("title", "") + "\n" + n.get("body", "") + "\n"
               + " ".join(n.get("tags", []))).lower()
        if q in hay:
            out.append(n)
    return out


def export_markdown():
    L = ["# 便签 / 知识库", "",
         f"> 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　"
         f"共 {len(load_notes())} 条", ""]
    tags = all_tags()
    if tags:
        L.append("**标签**：" + "、".join(f"{t}（{c}）" for t, c in tags))
        L.append("")
    for n in load_notes():
        L.append(f"## {n['title']}")
        L.append("")
        meta = [f"更新 {n.get('updated','')}"]
        if n.get("tags"):
            meta.append("标签 " + " ".join("#" + t for t in n["tags"]))
        if n.get("pinned"):
            meta.append("📌 置顶")
        L.append("*" + "　·　".join(meta) + "*")
        L.append("")
        L.append(n.get("body", ""))
        L.append("")
        L.append("---")
        L.append("")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "notes/" + VERSION

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
            ns = load_notes()
            return self._send(200, json.dumps({
                "version": VERSION, "count": len(ns),
                "pinned": len([n for n in ns if n.get("pinned")]),
                "tags": all_tags(), "file": NOTEFILE, "user_id": user_id(),
            }, ensure_ascii=False))

        if u.path == "/api/notes":
            q = (qs.get("q", [""])[0] or "")
            tag = (qs.get("tag", [""])[0] or "")
            ns = search(q) if q else load_notes()
            if tag:
                ns = [n for n in ns if tag in n.get("tags", [])]
            return self._send(200, json.dumps({"notes": ns}, ensure_ascii=False))

        if u.path == "/api/export":
            md = export_markdown()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = os.path.join(HOME, f"便签-{stamp}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(md)
            return self._send(200, json.dumps({"ok": True, "path": path,
                                               "bytes": len(md)}, ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/save":
            n = save_note(nid=(b.get("id") or None),
                          title=b.get("title") or "",
                          body=b.get("body") or "",
                          tags=b.get("tags"),
                          pinned=b.get("pinned"),
                          deleted=bool(b.get("deleted")))
            return self._send(200, json.dumps({"ok": True, "note": n}, ensure_ascii=False))

        if u.path == "/api/pin":
            nid = b.get("id")
            cur = next((n for n in load_notes() if n["id"] == nid), None)
            if not cur:
                return self._send(404, json.dumps({"ok": False, "error": "没有这条"},
                                                  ensure_ascii=False))
            n = save_note(nid=nid, title=cur["title"], body=cur["body"],
                          tags=cur["tags"], pinned=not cur.get("pinned"))
            return self._send(200, json.dumps({"ok": True, "pinned": n["pinned"]},
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
    log(f"便签 v{VERSION} 已启动 http://127.0.0.1:{port}（{len(load_notes())} 条）")

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
    print(f"✅ 便签已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_add(args):
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    n = save_note(title=args.title, body=args.body or "", tags=tags)
    print(f"✅ 已保存 {n['id']}")
    return 0


def cmd_list(args):
    ns = search(args.q) if args.q else load_notes()
    for n in ns:
        pin = "📌 " if n.get("pinned") else ""
        tags = ("  #" + " #".join(n["tags"])) if n.get("tags") else ""
        print(f"  {pin}{n['title']}{tags}")
        print(f"      {n.get('updated','')}  {n['id']}  {len(n.get('body',''))} 字符")
    print(f"\n共 {len(ns)} 条")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="notes", description=f"便签 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("add"); sp.add_argument("title"); sp.add_argument("body", nargs="?")
    sp.add_argument("--tags"); sp.set_defaults(f=cmd_add)
    sp = sub.add_parser("list"); sp.add_argument("q", nargs="?"); sp.set_defaults(f=cmd_list)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
