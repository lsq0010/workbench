#!/usr/bin/env python3
"""接口契约 —— 从抓到的报文里提取「字段契约」，并支持两个来源对比。

为什么做这个
  用户反复做的一件事：拿安卓抓的报文和 iOS 的报文"逐字段对齐"，确认
  每个字段的名字、类型、是否必填、单位。手工做一次要半天，而且容易漏。
  这个工具把它变成：选接口 → 自动提取 → 两边并排 diff。

数据从哪来
  不直接读抓包工具的 flows.jsonl（那是它的私有文件），而是走它对外声明的
  只读接口 /api/flows 和 /api/semantics。这就是平台上"功能之间调数据"的样子：
  生产者可以随时改存储，消费者只认接口。

零第三方依赖，只用标准库。
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("APICONTRACT_HOME") or SCRIPT_DIR)
PORTFILE = os.path.join(HOME, "apicontract.pid")
LOGFILE = os.path.join(HOME, "apicontract.log")
DATAFILE = os.path.join(HOME, "contracts.json")      # 自己产出的数据（只追加）
DEFAULT_PORT = 8893

# 抓包工具的接口地址。它 manifest 里声明了 web 端口 8891。
CAPTURE = os.environ.get("APICONTRACT_CAPTURE", "http://127.0.0.1:8891")


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ══════════════════════════════════════════════════════════════
# 数据来源：抓包工具
# ══════════════════════════════════════════════════════════════

def fetch(path, timeout=8):
    with urllib.request.urlopen(CAPTURE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def source_status():
    """抓包工具在不在、有没有数据 —— 界面上要如实告诉用户"""
    try:
        with urllib.request.urlopen(CAPTURE + "/api/status", timeout=4) as r:
            st = json.loads(r.read().decode("utf-8", "replace"))
        return {"ok": True, "total": st.get("total", 0), "business": st.get("business", 0),
                "last": st.get("last"), "url": CAPTURE}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": CAPTURE}


def fetch_flows():
    try:
        rows = fetch("/api/flows")
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        log(f"[source] 取报文失败 {exc}")
        return []


def fetch_semantics():
    try:
        return fetch("/api/semantics")
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════
# 契约提取：把报文变成字段表
# ══════════════════════════════════════════════════════════════

def parse_body(raw):
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    s = str(raw).strip()
    if not s or s[0] not in "[{":
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


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


def flatten(node, prefix=""):
    """把嵌套 JSON 拍平成 {路径: (类型, 样例值)}"""
    out = {}

    def walk(n, p):
        if isinstance(n, dict):
            if not n:
                out[p or "$"] = ("object(空)", "")
            for k, v in n.items():
                walk(v, f"{p}.{k}" if p else k)
        elif isinstance(n, list):
            out[p or "$"] = ("array", f"长度 {len(n)}")
            for i, v in enumerate(n[:2]):        # 数组只看前两个元素，够推结构
                walk(v, f"{p}[]")
        else:
            out[p or "$"] = (type_of(n), n)

    walk(node, prefix)
    return out


def merge_types(types):
    """同一个路径在不同记录里出现过多种类型 → 标出来（这本身就是重要信息）"""
    uniq = []
    for t in types:
        if t not in uniq:
            uniq.append(t)
    return uniq[0] if len(uniq) == 1 else " | ".join(uniq)


def endpoint_of(rec):
    """把一条记录归到一个接口上：方法 + 路径（去掉查询串）"""
    path = (rec.get("path") or "").split("?")[0]
    if rec.get("kind") == "connect":
        return "CONNECT", (rec.get("host") or "") + ":" + str(rec.get("port") or "")
    host = rec.get("host") or ""
    return rec.get("method") or "?", f"{host}{path}"


def build_contracts(rows=None, only_business=True):
    """按接口聚合，提取请求/响应字段契约"""
    rows = rows if rows is not None else fetch_flows()
    sem = fetch_semantics()
    field_sem = {k: v for k, v in (sem.get("fields") or {}).items() if not k.startswith("_")}
    api_sem = {k: v for k, v in (sem.get("apis") or {}).items() if not k.startswith("_")}

    groups = {}
    for r in rows:
        if only_business and (r.get("noise") or r.get("kind") == "connect"):
            continue
        method, ep = endpoint_of(r)
        if method == "CONNECT":
            continue
        g = groups.setdefault(ep, {"endpoint": ep, "method": method, "count": 0,
                                   "req_types": {}, "resp_types": {},
                                   "req_samples": {}, "resp_samples": {},
                                   "statuses": {}, "hosts": set(),
                                   "ids": [], "last": ""})
        g["count"] += 1
        g["hosts"].add(r.get("host") or "")
        if r.get("ts"):
            g["last"] = max(g["last"], r["ts"])
        g["ids"].append(r.get("id"))
        st = r.get("status")
        if st is not None:
            g["statuses"][str(st)] = g["statuses"].get(str(st), 0) + 1

        req = parse_body(r.get("req_body"))
        resp = parse_body(r.get("resp_body"))
        for flat, tkey, skey in ((flatten(req) if req is not None else {}, "req_types", "req_samples"),
                                 (flatten(resp) if resp is not None else {}, "resp_types", "resp_samples")):
            for path, (t, sample) in flat.items():
                g[tkey].setdefault(path, []).append(t)
                if path not in g[skey] and sample != "":
                    g[skey][path] = sample

    out = []
    for ep, g in groups.items():
        # 接口中文名：语义字典里有就用，没有就留空（不猜）
        name, note = "", ""
        for key, meta in api_sem.items():
            if key and key in ep:
                name = meta.get("名称") or ""
                note = meta.get("结论") or ""
                break
        req_fields = []
        for path, types in sorted(g["req_types"].items()):
            fs = field_sem.get(path.split(".")[-1].split("[]")[0])
            req_fields.append({
                "path": path, "type": merge_types(types), "count": len(types),
                "meaning": (fs or {}).get("含义", ""),
                "unit": (fs or {}).get("单位", ""),
                "values": (fs or {}).get("取值"),
                "sample": str(g["req_samples"].get(path, ""))[:60],
            })
        resp_fields = []
        for path, types in sorted(g["resp_types"].items()):
            fs = field_sem.get(path.split(".")[-1].split("[]")[0])
            resp_fields.append({
                "path": path, "type": merge_types(types), "count": len(types),
                "meaning": (fs or {}).get("含义", ""),
                "unit": (fs or {}).get("单位", ""),
                "values": (fs or {}).get("取值"),
                "sample": str(g["resp_samples"].get(path, ""))[:60],
            })
        out.append({
            "endpoint": ep, "method": g["method"], "name": name, "note": note,
            "count": g["count"], "last": g["last"],
            "hosts": sorted(g["hosts"]), "statuses": g["statuses"],
            "req_fields": req_fields, "resp_fields": resp_fields,
            "ids": g["ids"][-5:],
        })
    out.sort(key=lambda x: -x["count"])
    return out


def diff_contracts(a, b, label_a="A", label_b="B"):
    """两个契约的字段级差异"""

    def index(fields):
        return {f["path"]: f for f in fields}

    ia, ib = index(a.get("req_fields", [])), index(b.get("req_fields", []))
    ra, rb = index(a.get("resp_fields", [])), index(b.get("resp_fields", []))

    def cmp(x, y, kind):
        rows = []
        for p in sorted(set(x) | set(y)):
            fa, fb = x.get(p), y.get(p)
            if fa and not fb:
                rows.append({"kind": kind, "path": p, "state": "只在 " + label_a,
                             "a": f'{fa["type"]}', "b": "—",
                             "meaning": fa.get("meaning", "")})
            elif fb and not fa:
                rows.append({"kind": kind, "path": p, "state": "只在 " + label_b,
                             "a": "—", "b": f'{fb["type"]}',
                             "meaning": fb.get("meaning", "")})
            elif fa["type"] != fb["type"]:
                rows.append({"kind": kind, "path": p, "state": "类型不同",
                             "a": fa["type"], "b": fb["type"],
                             "meaning": fa.get("meaning") or fb.get("meaning", "")})
        return rows

    return {
        "a": {"endpoint": a.get("endpoint"), "label": label_a, "count": a.get("count")},
        "b": {"endpoint": b.get("endpoint"), "label": label_b, "count": b.get("count")},
        "diffs": cmp(ia, ib, "请求") + cmp(ra, rb, "响应"),
        "same_req": sorted(set(ia) & set(ib)),
        "same_resp": sorted(set(ra) & set(rb)),
    }


def to_markdown(contracts):
    """导出成可贴进文档/周报的契约表"""
    L = ["# 接口契约（从抓包数据提取）",
         "",
         f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　"
         f"来源：{CAPTURE}",
         "> 字段含义来自抓包工具的数据字典；字典里没有的留空，**不猜**。",
         ""]
    for c in contracts:
        title = c["name"] or c["endpoint"]
        L.append(f"## {title}")
        L.append("")
        L.append(f"- 接口：`{c['method']} {c['endpoint']}`")
        L.append(f"- 样本：{c['count']} 条" + (f"　最近：{c['last'][:19]}" if c["last"] else ""))
        if c["statuses"]:
            L.append("- 状态码：" + "、".join(f"{k}×{v}" for k, v in c["statuses"].items()))
        if c["note"]:
            L.append(f"- 结论：{c['note']}")
        for label, fields in (("请求字段", c["req_fields"]), ("响应字段", c["resp_fields"])):
            if not fields:
                continue
            L.append("")
            L.append(f"**{label}**")
            L.append("")
            L.append("| 字段 | 类型 | 出现 | 含义 | 单位 | 样例 |")
            L.append("|---|---|---|---|---|---|")
            for f in fields:
                vals = ""
                if f.get("values"):
                    vals = " 取值 " + "/".join(f'{k}={v}' for k, v in f["values"].items())
                L.append(f"| `{f['path']}` | {f['type']} | {f['count']} | "
                         f"{f['meaning']}{vals} | {f['unit']} | `{f['sample']}` |")
        L.append("")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "apicontract/" + VERSION

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

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HOME, "index.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as exc:
                return self._send(500, f"<h1>读不到 index.html</h1><p>{exc}</p>",
                                  "text/html; charset=utf-8")

        if u.path == "/api/status":
            return self._send(200, json.dumps({
                "version": VERSION, "source": source_status(), "capture": CAPTURE,
            }, ensure_ascii=False))

        if u.path == "/api/contracts":
            qs = urllib.parse.parse_qs(u.query)
            only_biz = (qs.get("all", ["0"])[0] != "1")
            t0 = time.time()
            cs = build_contracts(only_business=only_biz)
            log(f"[extract] {len(cs)} 个接口，用时 {time.time()-t0:.2f}s")
            return self._send(200, json.dumps(
                {"contracts": cs, "seconds": round(time.time() - t0, 2)},
                ensure_ascii=False))

        if u.path == "/api/diff":
            qs = urllib.parse.parse_qs(u.query)
            a = (qs.get("a", [""])[0] or "").strip()
            b = (qs.get("b", [""])[0] or "").strip()
            la = (qs.get("la", ["A"])[0] or "A")
            lb = (qs.get("lb", ["B"])[0] or "B")
            cs = {c["endpoint"]: c for c in build_contracts()}
            if a not in cs or b not in cs:
                return self._send(404, json.dumps(
                    {"error": "接口不存在", "have": list(cs)[:20]}, ensure_ascii=False))
            return self._send(200, json.dumps(diff_contracts(cs[a], cs[b], la, lb),
                                              ensure_ascii=False))

        if u.path == "/api/markdown":
            md = to_markdown(build_contracts())
            return self._send(200, md, "text/markdown; charset=utf-8")

        if u.path == "/api/export":
            cs = build_contracts()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            rec = {"ts": stamp, "source": CAPTURE, "contracts": cs}
            data = read_json(DATAFILE, {"version": 1, "exports": []})
            data["exports"].append({"ts": stamp, "count": len(cs)})   # 只追加
            write_json(DATAFILE, data)
            path = os.path.join(HOME, f"contracts-{stamp}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(to_markdown(cs))
            log(f"[export] {len(cs)} 个接口 → {path}")
            return self._send(200, json.dumps({"ok": True, "path": path,
                                               "bytes": os.path.getsize(path)},
                                              ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def running_pid():
    try:
        pid = int((open(PORTFILE).read() or "0").strip())
    except Exception:
        return 0
    try:
        os.kill(pid, 0)
        return pid
    except Exception:
        return 0


def cmd_serve(args):
    port = args.port or DEFAULT_PORT
    with open(PORTFILE, "w") as f:
        f.write(str(os.getpid()))
    st = source_status()
    log(f"接口契约 v{VERSION} 已启动 http://127.0.0.1:{port}")
    log(f"[source] 抓包工具 {CAPTURE} " +
        (f"可用，共 {st.get('total')} 条报文" if st["ok"] else f"不可用：{st.get('error')}"))

    def bye(*_):
        try:
            os.remove(PORTFILE)
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
    if running_pid():
        print(f"✅ 接口契约已启动（PID {running_pid()}） http://127.0.0.1:{port}")
        return 0
    print("❌ 启动失败，看 apicontract.log")
    return 1


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
        os.remove(PORTFILE)
    except Exception:
        pass
    print("已停止")
    return 0


def cmd_status(args):
    st = source_status()
    print(f"接口契约 v{VERSION}  {'✅ 运行中（PID %s）' % running_pid() if running_pid() else '⛔️ 未运行'}")
    print(f"  界面    : http://127.0.0.1:{args.port or DEFAULT_PORT}")
    print(f"  数据来源: {CAPTURE} " +
          (f"✅ 共 {st.get('total')} 条报文" if st["ok"] else f"❌ {st.get('error')}"))
    return 0


def cmd_contracts(args):
    cs = build_contracts(only_business=not args.all)
    print(f"共 {len(cs)} 个接口")
    for c in cs[:args.limit]:
        name = f"（{c['name']}）" if c["name"] else ""
        print(f"\n  {c['method']} {c['endpoint']} {name}")
        print(f"    样本 {c['count']} 条  请求字段 {len(c['req_fields'])}  "
              f"响应字段 {len(c['resp_fields'])}")
        for f in c["req_fields"][:args.fields]:
            mean = f"  // {f['meaning']}" if f["meaning"] else ""
            print(f"      → {f['path']:<38} {f['type']:<14}{mean}")
    return 0


def cmd_md(args):
    md = to_markdown(build_contracts())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"✅ 已写入 {args.out}（{len(md)} 字符）")
    else:
        print(md)
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="apicontract", description=f"接口契约 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("status"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_status)
    sp = sub.add_parser("contracts"); sp.add_argument("--all", action="store_true")
    sp.add_argument("--limit", type=int, default=10); sp.add_argument("--fields", type=int, default=15)
    sp.set_defaults(f=cmd_contracts)
    sp = sub.add_parser("md"); sp.add_argument("--out"); sp.set_defaults(f=cmd_md)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
