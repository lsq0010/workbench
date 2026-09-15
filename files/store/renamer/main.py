#!/usr/bin/env python3
"""批量重命名 —— 预览之后才动手，而且能撤销。

为什么做这个
  下载文件夹里一堆 `截屏2026-09-14 21.30.55.png`、素材一堆 `IMG_1234.jpg`，
  手动改太慢；但批量改名最怕"改完发现改错了，撤不回来"。

所以这个工具的规矩是：
  1. 先给**预览**（旧名 → 新名），确认了才执行
  2. 每次执行都写**撤销记录**，一键还原
  3. 绝不覆盖已存在的文件（冲突的会标出来并跳过）

支持：查找替换、加前缀/后缀、序号编号、正则替换、改扩展名。
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
HOME = os.path.abspath(os.environ.get("RENAMER_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "renamer.pid")
LOGFILE = os.path.join(HOME, "renamer.log")
UNDOFILE = os.path.join(HOME, "undo.jsonl")          # 只追加撤销记录
DEFAULT_PORT = 8910


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def list_files(folder, recursive=False, include_dirs=False, exts=None):
    out = []
    if not os.path.isdir(folder):
        return out
    if recursive:
        for cur, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                if exts and not any(f.lower().endswith(e.lower()) for e in exts):
                    continue
                out.append(os.path.join(cur, f))
            if include_dirs:
                for d in dirs:
                    if not d.startswith("."):
                        out.append(os.path.join(cur, d))
    else:
        for f in sorted(os.listdir(folder)):
            if f.startswith("."):
                continue
            p = os.path.join(folder, f)
            if os.path.isfile(p):
                if exts and not any(f.lower().endswith(e.lower()) for e in exts):
                    continue
                out.append(p)
            elif include_dirs and os.path.isdir(p) and include_dirs:
                out.append(p)
    return sorted(out)


def apply_rule(name, rule, index):
    """按规则算出新名字。name 是含扩展名的文件名"""
    base, ext = os.path.splitext(name)
    mode = rule.get("mode") or "replace"

    if mode == "replace":
        find = rule.get("find") or ""
        if not find:
            return name
        if rule.get("regex"):
            flags = re.I if rule.get("ignore_case") else 0
            try:
                base = re.sub(find, rule.get("to") or "", base, flags=flags)
            except re.error as exc:
                return f"__ERR__正则错误：{exc}"
        else:
            if rule.get("ignore_case"):
                base = re.sub(re.escape(find), lambda m: rule.get("to") or "",
                              base, flags=re.I)
            else:
                base = base.replace(find, rule.get("to") or "")

    elif mode == "affix":
        if rule.get("prefix"):
            base = rule["prefix"] + base
        if rule.get("suffix"):
            base = base + rule["suffix"]

    elif mode == "number":
        start = int(rule.get("start") or 1)
        step = int(rule.get("step") or 1)
        pad = int(rule.get("pad") or 2)
        n = start + index * step
        num = str(n).zfill(pad)
        tpl = rule.get("template") or "{n}"
        newbase = tpl.replace("{n}", num).replace("{name}", base)
        base = newbase

    elif mode == "case":
        how = rule.get("how") or "lower"
        if how == "lower":
            base = base.lower()
        elif how == "upper":
            base = base.upper()
        elif how == "title":
            base = base.title()
        elif how == "snake":
            base = re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower()
        elif how == "kebab":
            base = re.sub(r"(?<!^)(?=[A-Z])", "-", base).lower()

    # 扩展名处理
    if rule.get("new_ext"):
        ne = rule["new_ext"]
        if not ne.startswith("."):
            ne = "." + ne
        ext = ne
    if rule.get("drop_ext"):
        ext = ""

    return base + ext


def build_plan(folder, rule, recursive=False, include_dirs=False, exts=None):
    files = list_files(folder, recursive, include_dirs, exts)
    items = []
    for i, p in enumerate(files):
        old = os.path.basename(p)
        new = apply_rule(old, rule, i)
        d = os.path.dirname(p)
        item = {"old": old, "new": new, "dir": d,
                "old_path": p, "new_path": os.path.join(d, new),
                "changed": new != old}
        if new.startswith("__ERR__"):
            item["changed"] = False
            item["error"] = new[7:]
        items.append(item)

    # 标出冲突：目标已存在（且不是它自己）、或两条改成同一个名字
    target_seen = {}
    for it in items:
        if not it["changed"] or it.get("error"):
            continue
        np = it["new_path"]
        if os.path.exists(np) and os.path.abspath(np) != os.path.abspath(it["old_path"]):
            it["conflict"] = "目标已存在"
        if np in target_seen:
            it["conflict"] = "和多条改名后重名"
            items[target_seen[np]]["conflict"] = "和多条改名后重名"
        else:
            target_seen[np] = items.index(it)

    changed = [x for x in items if x["changed"] and not x.get("error")]
    return {"folder": folder, "total": len(items), "changed": len(changed),
            "conflicts": len([x for x in items if x.get("conflict")]),
            "errors": len([x for x in items if x.get("error")]),
            "items": items[:600], "truncated": len(items) > 600}


def execute(plan_items):
    """执行改名。两阶段：先全部改成临时名，再改成目标名，避免互相覆盖"""
    batch = "r_" + uuid.uuid4().hex[:10]
    done = []
    # 阶段1：改成临时名
    tmp_items = []
    for it in plan_items:
        if not it.get("changed") or it.get("conflict") or it.get("error"):
            continue
        if not os.path.exists(it["old_path"]):
            continue
        tmp = it["old_path"] + f".__tmp_{batch}"
        try:
            os.rename(it["old_path"], tmp)
            tmp_items.append((it, tmp))
        except Exception as exc:
            it["error"] = f"改名失败：{exc}"
    # 阶段2：改成目标名
    for it, tmp in tmp_items:
        try:
            os.rename(tmp, it["new_path"])
            done.append({"from": it["old_path"], "to": it["new_path"]})
        except Exception as exc:
            try:
                os.rename(tmp, it["old_path"])      # 尽量还原
            except Exception:
                pass
            it["error"] = f"改成目标名失败：{exc}"

    if done:
        rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "batch": batch,
               "count": len(done), "moves": done}
        with open(UNDOFILE, "a", encoding="utf-8") as f:      # 只追加
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log(f"[rename] {len(done)} 个文件已改名（批次 {batch}）")
    return {"ok": True, "renamed": len(done), "batch": batch, "moves": done}


def undo_last():
    rows = []
    try:
        with open(UNDOFILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        return {"ok": False, "error": "还没有可撤销的记录"}
    if not rows:
        return {"ok": False, "error": "还没有可撤销的记录"}
    rec = rows[-1]
    back = 0
    failed = []
    for mv in reversed(rec["moves"]):
        if os.path.exists(mv["to"]) and not os.path.exists(mv["from"]):
            try:
                os.rename(mv["to"], mv["from"])
                back += 1
            except Exception as exc:
                failed.append(f"{os.path.basename(mv['to'])}: {exc}")
        else:
            failed.append(f"{os.path.basename(mv['to'])}: 找不到或原名已被占用")
    # 撤销也记一笔（追加一条"已撤销"标记，不改历史）
    with open(UNDOFILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "batch": rec["batch"] + ":undone", "count": -back,
                            "moves": []}, ensure_ascii=False) + "\n")
    log(f"[undo] 批次 {rec['batch']} 还原 {back}/{len(rec['moves'])}")
    return {"ok": True, "restored": back, "total": len(rec["moves"]),
            "batch": rec["batch"], "failed": failed[:10]}


def undo_history(limit=10):
    rows = []
    try:
        with open(UNDOFILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    out = [r for r in rows if r.get("count", 0) > 0]
    return [{"ts": r["ts"], "batch": r["batch"], "count": r["count"],
             "sample": os.path.basename(r["moves"][0]["to"]) if r.get("moves") else ""}
            for r in reversed(out[-limit:])]


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "renamer/" + VERSION

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
                "version": VERSION, "home": os.path.expanduser("~"),
                "undo": undo_history(),
            }, ensure_ascii=False))
        if u.path == "/api/folder":
            path = (qs.get("path", [os.path.expanduser("~/Desktop")])[0] or "").strip()
            try:
                items = []
                for name in sorted(os.listdir(path)):
                    if name.startswith("."):
                        continue
                    p = os.path.join(path, name)
                    n = 1
                    if os.path.isdir(p):
                        try:
                            n = len(os.listdir(p))
                        except Exception:
                            n = 0
                        items.append({"name": name, "path": p, "dir": True, "count": n})
                return self._send(200, json.dumps({"path": path, "dirs": items[:200],
                                                   "parent": os.path.dirname(path)},
                                                  ensure_ascii=False))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/plan":
            folder = (b.get("folder") or "").strip()
            if not os.path.isdir(folder):
                return self._send(400, json.dumps({"error": "目录不存在"}, ensure_ascii=False))
            exts = [x for x in (b.get("exts") or []) if x]
            p = build_plan(folder, b.get("rule") or {},
                           recursive=bool(b.get("recursive")),
                           include_dirs=bool(b.get("include_dirs")),
                           exts=exts)
            log(f"[plan] {folder} → {p['changed']} 个待改，{p['conflicts']} 个冲突")
            return self._send(200, json.dumps(p, ensure_ascii=False))
        if u.path == "/api/execute":
            items = b.get("items") or []
            if not items:
                return self._send(400, json.dumps({"error": "没有要执行的项"}, ensure_ascii=False))
            r = execute(items)
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if u.path == "/api/undo":
            return self._send(200, json.dumps(undo_last(), ensure_ascii=False))
        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            if p and os.path.isdir(p):
                subprocess.Popen(["open", p])
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
    log(f"批量重命名 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ 批量重命名已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_plan(args):
    import shlex
    rule = {"mode": "replace", "find": args.find or "", "to": args.to or ""}
    p = build_plan(args.folder, rule, recursive=args.recursive,
                   exts=args.ext.split(",") if args.ext else None)
    print(f"{p['total']} 个文件，{p['changed']} 个会改名，{p['conflicts']} 个冲突")
    for it in p["items"]:
        if not it["changed"]:
            continue
        flag = f"  ⚠️ {it['conflict']}" if it.get("conflict") else ""
        print(f"  {it['old']}\n    → {it['new']}{flag}")
    return 0


def cmd_undo(args):
    r = undo_last()
    if not r.get("ok"):
        print("❌ " + r.get("error", "失败"))
        return 1
    print(f"✅ 已还原 {r['restored']}/{r['total']} 个（批次 {r['batch']}）")
    for f in r.get("failed", []):
        print("  ⚠️ " + f)
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="renamer", description=f"批量重命名 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("plan"); sp.add_argument("folder")
    sp.add_argument("--find"); sp.add_argument("--to"); sp.add_argument("--ext")
    sp.add_argument("--recursive", action="store_true"); sp.set_defaults(f=cmd_plan)
    sub.add_parser("undo").set_defaults(f=cmd_undo)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
