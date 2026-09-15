#!/usr/bin/env python3
"""报文对比 —— 两份 JSON 的结构化差异，也能直接选抓到的报文来比。

为什么做这个
  调试接口时最常问的两件事：
    "安卓发的是这个、iOS 发的是那个，差在哪？"
    "同一接口上次返回和这次返回，哪个字段变了？"
  逐行看 JSON 太慢，这里并排指出 多/少/值不同 的每个路径。

两种输入
  · 粘贴两段 JSON
  · 从抓包工作台挑两条报文（按 id 或按接口）

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
HOME = os.path.abspath(os.environ.get("JSONDIFF_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "jsondiff.pid")
LOGFILE = os.path.join(HOME, "jsondiff.log")
DEFAULT_PORT = 8895
CAPTURE = os.environ.get("JSONDIFF_CAPTURE", "http://127.0.0.1:8891")


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fetch(path, timeout=8):
    with urllib.request.urlopen(CAPTURE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ══════════════════════════════════════════════════════════════
# 结构化对比
# ══════════════════════════════════════════════════════════════

def type_of(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def walk(node, prefix=""):
    """拍平成 {路径: (类型, 值)}，数组元素用 [] 表示"""
    out = {}

    def go(n, p):
        if isinstance(n, dict):
            if not n:
                out[p or "$"] = ("object(空)", "")
            for k, v in n.items():
                go(v, f"{p}.{k}" if p else k)
        elif isinstance(n, list):
            if not n:
                out[p or "$"] = ("array(空)", "")
            for v in n:
                go(v, f"{p}[]")          # 同类元素合并到同一个路径
        else:
            out[p or "$"] = (type_of(n), n)

    go(node, prefix)
    return out


def preview(v, n=60):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def diff_json(a, b, label_a="A", label_b="B", ignore_order=True):
    """对比两份 JSON，返回逐路径差异"""
    fa, fb = walk(a), walk(b)
    rows = []
    for p in sorted(set(fa) | set(fb)):
        ta, tb = fa.get(p), fb.get(p)
        if ta and not tb:
            rows.append({"path": p, "state": f"只在{label_a}", "a": preview(ta[1]),
                         "b": "—", "ta": ta[0], "tb": ""})
        elif tb and not ta:
            rows.append({"path": p, "state": f"只在{label_b}", "a": "—",
                         "b": preview(tb[1]), "ta": "", "tb": tb[0]})
        elif ta[0] != tb[0]:
            rows.append({"path": p, "state": "类型不同", "a": preview(ta[1]),
                         "b": preview(tb[1]), "ta": ta[0], "tb": tb[0]})
        elif ta[1] != tb[1]:
            rows.append({"path": p, "state": "值不同", "a": preview(ta[1]),
                         "b": preview(tb[1]), "ta": ta[0], "tb": tb[0]})
    return {
        "a": {"label": label_a, "keys": len(fa), "type": type_of(a)},
        "b": {"label": label_b, "keys": len(fb), "type": type_of(b)},
        "diffs": rows,
        "same": len(set(fa) & set(fb)) - len([r for r in rows if r["state"] in ("值不同", "类型不同")]),
        "counts": {
            "只在A": len([r for r in rows if r["state"].endswith(label_a)]),
            "只在B": len([r for r in rows if r["state"].endswith(label_b)]),
            "值不同": len([r for r in rows if r["state"] == "值不同"]),
            "类型不同": len([r for r in rows if r["state"] == "类型不同"]),
        },
    }


def parse(text, what="输入"):
    if isinstance(text, (dict, list)):
        return text, None
    s = (text or "").strip()
    if not s:
        return None, f"{what}是空的"
    try:
        return json.loads(s), None
    except Exception as exc:
        # 给一点定位帮助：报错里的位置换算成行列
        pos = getattr(exc, "pos", None)
        where = ""
        if pos is not None:
            where = f"（第 {s[:pos].count(chr(10)) + 1} 行，第 {pos - s.rfind(chr(10), 0, pos)} 列）"
        return None, f"{what}不是合法 JSON{where}：{exc.msg if hasattr(exc,'msg') else exc}"


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "jsondiff/" + VERSION

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
            src = {"ok": False}
            try:
                with urllib.request.urlopen(CAPTURE + "/api/status", timeout=4) as r:
                    st = json.loads(r.read().decode())
                src = {"ok": True, "total": st.get("total", 0)}
            except Exception as exc:
                src = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return self._send(200, json.dumps({"version": VERSION, "capture": CAPTURE,
                                               "source": src}, ensure_ascii=False))

        if u.path == "/api/flows":
            """给"从抓包选"用的候选列表"""
            try:
                rows = fetch("/api/flows")
            except Exception as exc:
                return self._send(502, json.dumps({"error": str(exc)}, ensure_ascii=False))
            q = (qs.get("q", [""])[0] or "").lower()
            out = []
            for r in rows:
                if r.get("kind") == "connect":
                    continue
                blob = f'{r.get("host")}{r.get("path")}'
                if q and q not in blob.lower():
                    continue
                out.append({"id": r.get("id"), "ts": (r.get("ts") or "")[11:19],
                            "method": r.get("method"), "host": r.get("host"),
                            "path": (r.get("path") or "")[:70], "status": r.get("status"),
                            "has_req": bool(r.get("req_body")), "has_resp": bool(r.get("resp_body"))})
            out.reverse()
            return self._send(200, json.dumps({"flows": out[:200]}, ensure_ascii=False))

        if u.path == "/api/flow":
            try:
                fid = int(qs.get("id", ["0"])[0])
            except Exception:
                fid = 0
            side = (qs.get("side", ["req"])[0] or "req")
            try:
                r = fetch("/api/flow?id=%d" % fid)
            except Exception as exc:
                return self._send(502, json.dumps({"error": str(exc)}, ensure_ascii=False))
            body = r.get("resp_body") if side == "resp" else r.get("req_body")
            return self._send(200, json.dumps({
                "id": fid, "side": side, "body": body or "",
                "method": r.get("method"), "host": r.get("host"),
                "path": r.get("path"), "status": r.get("status"),
            }, ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/diff":
            a, ea = parse(b.get("a"), "左边")
            if ea:
                return self._send(400, json.dumps({"error": ea}, ensure_ascii=False))
            c, eb = parse(b.get("b"), "右边")
            if eb:
                return self._send(400, json.dumps({"error": eb}, ensure_ascii=False))
            la = (b.get("la") or "A").strip() or "A"
            lb = (b.get("lb") or "B").strip() or "B"
            d = diff_json(a, c, la, lb)
            d["markdown"] = to_markdown(d)
            log(f"[diff] {len(d['diffs'])} 处差异（{la} vs {lb}）")
            return self._send(200, json.dumps(d, ensure_ascii=False))

        if u.path == "/api/format":
            v, err = parse(b.get("text"))
            if err:
                return self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            return self._send(200, json.dumps(
                {"text": json.dumps(v, ensure_ascii=False, indent=2)}, ensure_ascii=False))

        if u.path == "/api/query":
            v, err = parse(b.get("text"))
            if err:
                return self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            path = (b.get("path") or "").strip()
            if not path:
                return self._send(400, json.dumps({"error": "路径为空"}, ensure_ascii=False))
            cur, err2 = v, None
            try:
                for seg in path.strip(".").split("."):
                    if seg.endswith("[]"):
                        cur = [x for item in (cur if isinstance(cur, list) else [cur])
                               for x in (item.get(seg[:-2]) if isinstance(item, dict) else [])]
                        continue
                    if isinstance(cur, list):
                        cur = [x.get(seg) if isinstance(x, dict) else None for x in cur]
                    elif isinstance(cur, dict):
                        cur = cur.get(seg)
                    else:
                        err2 = f"在 {seg} 处不能继续下钻（当前是 {type_of(cur)}）"
                        break
            except Exception as exc:
                err2 = str(exc)
            if err2:
                return self._send(400, json.dumps({"error": err2}, ensure_ascii=False))
            return self._send(200, json.dumps({
                "value": cur, "type": type_of(cur),
                "text": json.dumps(cur, ensure_ascii=False, indent=2) if not isinstance(cur, str) else cur,
                "count": len(cur) if isinstance(cur, list) else None,
            }, ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))


def to_markdown(d):
    L = [f"# 报文差异：{d['a']['label']} vs {d['b']['label']}", ""]
    c = d["counts"]
    L.append(f"- {d['a']['label']}：{d['a']['keys']} 个路径")
    L.append(f"- {d['b']['label']}：{d['b']['keys']} 个路径")
    L.append(f"- 差异：只在 {d['a']['label']} {c['只在A']} 处、"
             f"只在 {d['b']['label']} {c['只在B']} 处、"
             f"值不同 {c['值不同']} 处、类型不同 {c['类型不同']} 处")
    L.append("")
    if not d["diffs"]:
        L.append("✅ 两份完全一致")
        return "\n".join(L)
    L += ["| 路径 | 差异 | " + d["a"]["label"] + " | " + d["b"]["label"] + " |",
          "|---|---|---|---|"]
    for r in d["diffs"]:
        L.append(f"| `{r['path']}` | {r['state']} | `{r['a']}` | `{r['b']}` |")
    return "\n".join(L)


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
    log(f"报文对比 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ 报文对比已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_diff(args):
    a, ea = parse(open(args.a, encoding="utf-8").read(), args.a)
    if ea:
        print("❌ " + ea); return 1
    b, eb = parse(open(args.b, encoding="utf-8").read(), args.b)
    if eb:
        print("❌ " + eb); return 1
    d = diff_json(a, b, os.path.basename(args.a), os.path.basename(args.b))
    print(to_markdown(d))
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="jsondiff", description=f"报文对比 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("diff"); sp.add_argument("a"); sp.add_argument("b")
    sp.set_defaults(f=cmd_diff)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
