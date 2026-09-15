#!/usr/bin/env python3
"""接口变更监控 —— 给接口契约拍快照，之后对比看服务端偷偷改了什么。

为什么做这个
  移动端最怕的事：服务端悄悄改了字段类型或删了字段，App 上线才发现崩溃。
  把某天的契约存成基线，之后每次抓完包点一下「对比基线」，
  就能看到 哪些字段新增了 / 消失了 / 类型变了。

数据从哪来
  不自己解析报文 —— 走「接口契约」功能对外声明的 /api/contracts。
  这就是平台上功能之间协作的样子：一个功能提供数据，另一个消费它。

零第三方依赖。
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("APIMON_HOME") or SCRIPT_DIR)
SNAPFILE = os.path.join(HOME, "snapshots.jsonl")       # 只追加
CHANGES = os.path.join(HOME, "changes.jsonl")          # 只追加
PIDFILE = os.path.join(HOME, "apimon.pid")
LOGFILE = os.path.join(HOME, "apimon.log")
DEFAULT_PORT = 8901

# 上游：接口契约功能
CONTRACT = os.environ.get("APIMON_CONTRACT", "http://127.0.0.1:8893")


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fetch(path, timeout=30):
    with urllib.request.urlopen(CONTRACT + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def upstream_status():
    try:
        with urllib.request.urlopen(CONTRACT + "/api/status", timeout=5) as r:
            st = json.loads(r.read().decode())
        return {"ok": True, "url": CONTRACT, "source": st.get("source", {})}
    except Exception as exc:
        return {"ok": False, "url": CONTRACT, "error": f"{type(exc).__name__}: {exc}"}


# ══════════════════════════════════════════════════════════════
# 快照
# ══════════════════════════════════════════════════════════════

def load_snapshots():
    out = []
    try:
        with open(SNAPFILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return out


def current_contracts():
    d = fetch("/api/contracts")
    return d.get("contracts") or []


def fingerprint(c):
    """一个接口的指纹：只取 字段路径 + 类型，用来判断有没有变"""
    return {
        "req": {f["path"]: f["type"] for f in c.get("req_fields", [])},
        "resp": {f["path"]: f["type"] for f in c.get("resp_fields", [])},
    }


def take_snapshot(note="", only=None):
    cs = current_contracts()
    if only:
        cs = [c for c in cs if c["endpoint"] in only]
    if not cs:
        return None, "上游没有可快照的接口 —— 先让「抓包工作台」抓到业务报文，再让「接口契约」提取一次"
    snap = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
        "apis": {c["endpoint"]: {"method": c["method"], "name": c.get("name", ""),
                                 "count": c["count"], "fp": fingerprint(c)}
                 for c in cs},
    }
    with open(SNAPFILE, "a", encoding="utf-8") as f:          # 只追加，不覆盖历史
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")
    log(f"[snapshot] {len(snap['apis'])} 个接口 备注={note or '(无)'}")
    return snap, None


def compare(baseline, current):
    """基线 vs 现在：逐接口、逐字段列变化"""
    changes = []
    ba, ca = baseline.get("apis", {}), {c["endpoint"]: c for c in current}
    for ep in sorted(set(ba) | set(ca)):
        b, c = ba.get(ep), ca.get(ep)
        if b and not c:
            changes.append({"endpoint": ep, "level": "接口", "state": "消失了",
                            "detail": "这次没抓到（可能没调、也可能接口下线了）"})
            continue
        if c and not b:
            changes.append({"endpoint": ep, "level": "接口", "state": "新增",
                            "detail": f"{len(c.get('req_fields',[]))} 请求字段 / "
                                      f"{len(c.get('resp_fields',[]))} 响应字段"})
            continue
        cur_fp = fingerprint(c)
        for side, label in (("req", "请求"), ("resp", "响应")):
            bs, cs2 = b["fp"].get(side, {}), cur_fp.get(side, {})
            for p in sorted(set(bs) | set(cs2)):
                if p in bs and p not in cs2:
                    changes.append({"endpoint": ep, "level": label, "state": "字段没了",
                                    "field": p, "was": bs[p], "now": "—"})
                elif p in cs2 and p not in bs:
                    changes.append({"endpoint": ep, "level": label, "state": "新字段",
                                    "field": p, "was": "—", "now": cs2[p]})
                elif bs[p] != cs2[p]:
                    changes.append({"endpoint": ep, "level": label, "state": "类型变了",
                                    "field": p, "was": bs[p], "now": cs2[p]})
    return changes


def summarize(changes):
    if not changes:
        return "✅ 没有变化"
    n = {}
    for c in changes:
        n[c["state"]] = n.get(c["state"], 0) + 1
    return "、".join(f"{k} {v}" for k, v in sorted(n.items(), key=lambda x: -x[1]))


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "apimon/" + VERSION

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
            snaps = load_snapshots()
            return self._send(200, json.dumps({
                "version": VERSION,
                "upstream": upstream_status(),
                "snapshots": len(snaps),
                "last": snaps[-1]["ts"] if snaps else None,
            }, ensure_ascii=False))

        if u.path == "/api/snapshots":
            snaps = load_snapshots()
            return self._send(200, json.dumps({
                "snapshots": [{"ts": s["ts"], "note": s.get("note", ""),
                               "apis": len(s.get("apis", {}))} for s in reversed(snaps)],
            }, ensure_ascii=False))

        if u.path == "/api/compare":
            """和某个基线比。默认和最近一次比"""
            qs = urllib.parse.parse_qs(u.query)
            snaps = load_snapshots()
            if not snaps:
                return self._send(200, json.dumps(
                    {"error": "还没有基线快照 —— 先点「拍个快照」"}, ensure_ascii=False))
            try:
                idx = int(qs.get("idx", ["-1"])[0])
            except Exception:
                idx = -1
            base = snaps[idx] if -1 <= idx < len(snaps) else snaps[-1]
            try:
                cur = current_contracts()
            except Exception as exc:
                return self._send(502, json.dumps(
                    {"error": f"读不到接口契约：{exc}"}, ensure_ascii=False))
            ch = compare(base, cur)
            return self._send(200, json.dumps({
                "baseline": {"ts": base["ts"], "note": base.get("note", ""),
                             "apis": len(base.get("apis", {}))},
                "current_apis": len(cur),
                "changes": ch,
                "summary": summarize(ch),
            }, ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/snapshot":
            snap, err = take_snapshot((b.get("note") or "").strip())
            if err:
                return self._send(200, json.dumps({"ok": False, "error": err},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(
                {"ok": True, "ts": snap["ts"], "apis": len(snap["apis"])},
                ensure_ascii=False))

        if u.path == "/api/record":
            """把这次对比结果记下来（只追加），方便回头看改动历史"""
            ch = b.get("changes") or []
            base = b.get("baseline") or {}
            with open(CHANGES, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "baseline": base.get("ts"), "count": len(ch),
                                    "summary": summarize(ch)}, ensure_ascii=False) + "\n")
            log(f"[record] 记录 {len(ch)} 处变更")
            return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))

        if u.path == "/api/changes":
            rows = []
            try:
                with open(CHANGES, encoding="utf-8") as f:
                    for line in f.readlines()[-30:]:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
            except Exception:
                pass
            return self._send(200, json.dumps({"changes": list(reversed(rows))},
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
    up = upstream_status()
    log(f"接口变更监控 v{VERSION} 已启动 http://127.0.0.1:{port}")
    log(f"[upstream] 接口契约 {CONTRACT} " +
        ("可用" if up["ok"] else f"不可用：{up.get('error')}"))

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
    print(f"✅ 接口变更监控已启动（PID {running_pid()}） http://127.0.0.1:{port}"
          if running_pid() else "❌ 启动失败，看 apimon.log")
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


def cmd_snapshot(args):
    snap, err = take_snapshot(args.note or "")
    if err:
        print("❌ " + err)
        return 1
    print(f"✅ 已拍快照：{snap['ts']}，{len(snap['apis'])} 个接口")
    return 0


def cmd_compare(args):
    snaps = load_snapshots()
    if not snaps:
        print("❌ 还没有基线。先 apimon snapshot")
        return 1
    base = snaps[-1]
    cur = current_contracts()
    ch = compare(base, cur)
    print(f"基线：{base['ts']}（{len(base.get('apis', {}))} 个接口）")
    print(f"现在：{len(cur)} 个接口")
    print(f"结果：{summarize(ch)}\n")
    for c in ch:
        fld = f" {c.get('field','')}" if c.get("field") else ""
        was = f"  {c.get('was','')} → {c.get('now','')}" if c.get("was") else ""
        print(f"  [{c['level']}] {c['state']:<8} {c['endpoint']}{fld}{was}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="apimon", description=f"接口变更监控 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("snapshot"); sp.add_argument("--note"); sp.set_defaults(f=cmd_snapshot)
    sub.add_parser("compare").set_defaults(f=cmd_compare)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
