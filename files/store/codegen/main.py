#!/usr/bin/env python3
"""模型生成 —— 从 JSON 报文一键生成 Swift / Kotlin / TypeScript 数据模型。

为什么做这个
  iOS 开发里对接一个接口，第一件事是把响应 JSON 变成 struct。
  手写一遍要十几分钟，还容易写错类型（尤其是 "1" vs 1、可选值、数组嵌套）。
  这里从真实报文反推结构，直接出能编译的代码。

数据来源
  · 粘贴 JSON
  · 或从抓包工作台直接挑一条报文的响应体

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
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("CODEGEN_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "codegen.pid")
LOGFILE = os.path.join(HOME, "codegen.log")
DEFAULT_PORT = 8896
CAPTURE = os.environ.get("CODEGEN_CAPTURE", "http://127.0.0.1:8891")


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
# 结构推断
# ══════════════════════════════════════════════════════════════

def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Node:
    """一个字段的结构：类型集合 + 子节点 + 样例"""

    def __init__(self, name):
        self.name = name
        self.types = set()
        self.children = {}          # 对象字段
        self.item = None            # 数组元素结构
        self.samples = []
        self.null_seen = False
        self.count = 0

    def feed(self, v):
        self.count += 1
        if len(self.samples) < 3:
            self.samples.append(v)
        if v is None:
            self.null_seen = True
            self.types.add("null")
        elif isinstance(v, bool):
            self.types.add("bool")
        elif is_int(v):
            self.types.add("int")
        elif is_num(v):
            self.types.add("double")
        elif isinstance(v, str):
            self.types.add("string")
        elif isinstance(v, dict):
            self.types.add("object")
            for k, x in v.items():
                self.children.setdefault(k, Node(k)).feed(x)
        elif isinstance(v, list):
            self.types.add("array")
            if self.item is None:
                self.item = Node(self.name + "Item")
            for x in v:
                self.item.feed(x)
        else:
            self.types.add("unknown")


def build_tree(data):
    root = Node("Root")
    if isinstance(data, list):
        # 顶层是数组：取元素结构作为主体
        root.types.add("array")
        root.item = Node("RootItem")
        for x in data:
            root.item.feed(x)
    else:
        root.feed(data)
    return root


def type_name(t):
    """把推断出的类型集合收敛成一个可写进代码的类型"""
    ts = {x for x in t if x != "null"}
    if not ts:
        return "String?" if t else "Any"
    if ts == {"int"}:
        return "Int"
    if ts <= {"int", "double"}:
        return "Double"
    if ts == {"double"}:
        return "Double"
    if ts == {"bool"}:
        return "Bool"
    if ts == {"string"}:
        return "String"
    if ts == {"object"}:
        return "Object"
    if ts == {"array"}:
        return "Array"
    if len(ts) > 1:
        return "Any"          # 类型不稳定 —— 生成代码时标出来
    return "Any"


def collect_classes(root, prefix=""):
    """把树里所有 object / array-of-object 收集成待生成的模型"""
    out = []

    def go(node, name):
        # 数组元素：如果元素是对象，也算一个模型
        if node.item is not None and "object" in node.item.types:
            go(node.item, name + "Item")
        elif node.item is not None and node.item.item is not None:
            go(node.item, name + "Item")
        if "object" not in node.types:
            return
        fields = []
        for k, child in node.children.items():
            if child.item is not None and "object" in child.item.types:
                # 类名必须和下面 go(...) 里声明的名字一致 —— 都用 pascal，
                # 否则会出现"引用 [listItem] / 定义 ListItem"这种对不上的代码
                ftype, sub = "[" + pascal(child.name) + "Item]", True
            elif child.item is not None:
                ftype, sub = "[" + type_name(child.item.types) + "]", False
            elif "object" in child.types:
                ftype, sub = pascal(child.name), True
            else:
                ftype, sub = type_name(child.types), False
            optional = ("null" in child.types) or child.count < node.count
            fields.append({"name": k, "type": ftype, "optional": optional,
                           "unstable": "Any" == ftype, "raw_types": sorted(child.types),
                           "samples": [str(s)[:40] for s in child.samples[:2]]})
            if sub and child.name not in [o["name"] for o in out]:
                pass
        out.append({"name": name, "fields": fields})
        # 递归子对象
        for k, child in node.children.items():
            if child.item is not None and "object" in child.item.types:
                go(child.item, child.name + "Item")
            elif "object" in child.types:
                go(child, child.name)

    go(root, prefix or "Root")
    # 去重 + 保持稳定顺序
    seen, uniq = set(), []
    for o in out:
        if o["name"] in seen:
            continue
        seen.add(o["name"])
        uniq.append(o)
    return uniq


# ══════════════════════════════════════════════════════════════
# 代码生成
# ══════════════════════════════════════════════════════════════

# 各语言里已被占用的类型名。撞名会让生成的代码编译不过
# （比如 JSON 里的 data 字段 → struct Data，和 Foundation.Data 冲突）。
RESERVED = {
    "swift": {"Data", "Error", "Set", "Array", "Dictionary", "String", "Int", "Double",
              "Bool", "Any", "Object", "Type", "Protocol", "Self", "Optional", "Result",
              "URL", "Date", "Number", "Character", "Void", "Never", "Task"},
    "kotlin": {"String", "Int", "Double", "Boolean", "Any", "List", "Map", "Set",
               "Object", "Unit", "Nothing"},
    "typescript": {"String", "Number", "Boolean", "Object", "Array", "Date", "Error",
                   "Function", "Symbol", "BigInt", "Record", "Partial"},
}


def safe_name(name, lang):
    """避开语言内置类型名"""
    r = RESERVED.get(lang, set())
    if name in r:
        return name + "Model"
    return name


def camel(s):
    parts = re.split(r"[_\-\s]+", s)
    if not parts:
        return s
    out = parts[0][:1].lower() + parts[0][1:]
    for p in parts[1:]:
        out += p[:1].upper() + p[1:]
    return out


def pascal(s):
    return camel(s)[:1].upper() + camel(s)[1:]


SWIFT_MAP = {"Int": "Int", "Double": "Double", "Bool": "Bool", "String": "String",
             "Any": "AnyCodable", "Object": "[String: Any]", "Array": "[Any]"}


def gen_swift(classes, root_name="Root", use_codable=True):
    L = [f"// 由「模型生成」从真实报文反推生成",
         f"// {datetime.now().strftime('%Y-%m-%d %H:%M')}",
         "",
         "import Foundation", ""]
    if any(f["unstable"] or f["type"] == "Any" for c in classes for f in c["fields"]):
        L += ["/// 字段类型不稳定的兜底类型（报文里同一个字段出现过多种类型）",
              "struct AnyCodable: Codable {",
              "    let value: Any",
              "    init(from decoder: Decoder) throws {",
              "        let c = try decoder.singleValueContainer()",
              "        if let v = try? c.decode(Int.self) { value = v }",
              "        else if let v = try? c.decode(Double.self) { value = v }",
              "        else if let v = try? c.decode(Bool.self) { value = v }",
              "        else if let v = try? c.decode(String.self) { value = v }",
              "        else { value = (try? c.decode([String: AnyCodable].self)) ?? [:] }",
              "    }",
              "    func encode(to encoder: Encoder) throws {}",
              "}", ""]

    for c in classes:
        name = pascal(c["name"])
        if c["name"] == root_name:
            name = pascal(root_name)
        name = safe_name(name, "swift")
        L.append(f"struct {name}: Codable {{")
        for f in c["fields"]:
            t = SWIFT_MAP.get(f["type"], f["type"])
            t = re.sub(r"\[([A-Za-z]+)\]", lambda m: "[" + SWIFT_MAP.get(m.group(1), m.group(1)) + "]", t)
            if t not in ("Int", "Double", "Bool", "String", "AnyCodable") and not t.startswith("["):
                t = safe_name(pascal(f["type"]), "swift")
            # 数组里的自定义类型也要避重名：[Data] → [DataModel]
            t = re.sub(r"\[([A-Za-z][A-Za-z0-9]*)\]",
                       lambda m: "[" + safe_name(m.group(1), "swift") + "]"
                       if m.group(1) not in ("Int", "Double", "Bool", "String", "AnyCodable")
                       else m.group(0), t)
            opt = "?" if f["optional"] else ""
            prop = camel(f["name"])
            # 字段名和 JSON key 不一致时给出 CodingKeys 提示
            note = ""
            if f["unstable"]:
                note = "   // ⚠️ 报文里类型不稳定：" + "/".join(f["raw_types"])
            L.append(f"    let {prop}: {t}{opt}{note}")
        # CodingKeys（只在需要时生成）
        need_keys = [f for f in c["fields"] if camel(f["name"]) != f["name"]]
        if need_keys:
            L.append("")
            L.append("    enum CodingKeys: String, CodingKey {")
            for f in need_keys:
                L.append(f'        case {camel(f["name"])} = "{f["name"]}"')
            L.append("    }")
        L.append("}")
        L.append("")
    # 根类型可能是数组
    L.append(f"// 用法：")
    L.append(f"//   let model = try JSONDecoder().decode({pascal(root_name)}.self, from: data)")
    return "\n".join(L)


def gen_kotlin(classes, root_name="Root"):
    L = [f"// 由「模型生成」从真实报文反推生成",
         f"// {datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
         "import com.google.gson.annotations.SerializedName",
         "import com.google.gson.Gson", ""]
    for c in classes:
        name = pascal(c["name"]) if c["name"] != root_name else pascal(root_name)
        name = safe_name(name, "kotlin")
        L.append(f"data class {name}(")
        rows = []
        for f in c["fields"]:
            t = {"Int": "Int", "Double": "Double", "Bool": "Boolean",
                 "String": "String", "Any": "Any?"}.get(f["type"], pascal(f["type"]))
            t = re.sub(r"\[([A-Za-z]+)\]",
                       lambda m: "List<" + {"Int": "Int", "Double": "Double",
                                            "Bool": "Boolean", "String": "String"}
                       .get(m.group(1), pascal(m.group(1))) + ">", t)
            opt = "?" if f["optional"] else ""
            key = f'    @SerializedName("{f["name"]}") val {camel(f["name"])}: {t}{opt}'
            if f["unstable"]:
                key += "   // ⚠️ 类型不稳定：" + "/".join(f["raw_types"])
            rows.append(key)
        L.append(",\n".join(rows))
        L.append(")")
        L.append("")
    return "\n".join(L)


def gen_ts(classes, root_name="Root"):
    L = [f"// 由「模型生成」从真实报文反推生成",
         f"// {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    for c in classes:
        name = pascal(c["name"]) if c["name"] != root_name else pascal(root_name)
        name = safe_name(name, "typescript")
        L.append(f"export interface {name} {{")
        for f in c["fields"]:
            t = {"Int": "number", "Double": "number", "Bool": "boolean",
                 "String": "string", "Any": "unknown"}.get(f["type"], pascal(f["type"]))
            t = re.sub(r"\[([A-Za-z]+)\]",
                       lambda m: {"Int": "number", "Double": "number", "Bool": "boolean",
                                  "String": "string"}.get(m.group(1), pascal(m.group(1))) + "[]", t)
            opt = "?" if f["optional"] else ""
            note = f'  // ⚠️ 类型不稳定：{"/".join(f["raw_types"])}' if f["unstable"] else ""
            L.append(f"  {f['name']}{opt}: {t};{note}")
        L.append("}")
        L.append("")
    return "\n".join(L)


GENERATORS = {
    "swift": ("Swift (Codable)", gen_swift),
    "kotlin": ("Kotlin (Gson)", gen_kotlin),
    "typescript": ("TypeScript (interface)", gen_ts),
}


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codegen/" + VERSION

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
            src = {"ok": False}
            try:
                with urllib.request.urlopen(CAPTURE + "/api/status", timeout=4) as r:
                    st = json.loads(r.read().decode())
                src = {"ok": True, "total": st.get("total", 0)}
            except Exception as exc:
                src = {"ok": False, "error": str(exc)}
            return self._send(200, json.dumps({"version": VERSION, "source": src,
                                               "languages": {k: v[0] for k, v in GENERATORS.items()}},
                                              ensure_ascii=False))

        if u.path == "/api/flows":
            try:
                rows = fetch("/api/flows")
            except Exception as exc:
                return self._send(502, json.dumps({"error": str(exc)}, ensure_ascii=False))
            out = [{"id": r.get("id"), "ts": (r.get("ts") or "")[11:19], "method": r.get("method"),
                    "host": r.get("host"), "path": (r.get("path") or "")[:70],
                    "status": r.get("status"), "has_resp": bool(r.get("resp_body"))}
                   for r in rows if r.get("kind") != "connect"]
            out.reverse()
            return self._send(200, json.dumps({"flows": out[:200]}, ensure_ascii=False))

        if u.path == "/api/flow":
            qs = urllib.parse.parse_qs(u.query)
            try:
                fid = int(qs.get("id", ["0"])[0])
            except Exception:
                fid = 0
            try:
                r = fetch("/api/flow?id=%d" % fid)
            except Exception as exc:
                return self._send(502, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return self._send(200, json.dumps({
                "body": r.get("resp_body") or r.get("req_body") or "",
                "method": r.get("method"), "path": r.get("path"),
            }, ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/generate":
            raw = (b.get("json") or "").strip()
            if not raw:
                return self._send(400, json.dumps({"error": "先贴一段 JSON 或从抓包选一条"},
                                                  ensure_ascii=False))
            try:
                data = json.loads(raw)
            except Exception as exc:
                pos = getattr(exc, "pos", None)
                where = ""
                if pos is not None:
                    where = f"（第 {raw[:pos].count(chr(10))+1} 行）"
                return self._send(400, json.dumps(
                    {"error": f"不是合法 JSON{where}：{getattr(exc,'msg',exc)}"},
                    ensure_ascii=False))
            lang = (b.get("lang") or "swift").lower()
            if lang not in GENERATORS:
                return self._send(400, json.dumps({"error": "不支持的语言"}, ensure_ascii=False))
            root = (b.get("root") or "Root").strip() or "Root"
            is_list = isinstance(data, list)
            tree = build_tree(data)
            classes = collect_classes(tree, root)
            code = GENERATORS[lang][1](classes, root)
            log(f"[gen] {lang} ← {len(raw)} 字符 JSON，{len(classes)} 个模型")
            return self._send(200, json.dumps({
                "code": code, "classes": classes, "lang": lang,
                "root": root, "is_list": is_list,
                "models": len(classes),
                "fields": sum(len(c["fields"]) for c in classes),
                "unstable": sum(1 for c in classes for f in c["fields"] if f["unstable"]),
            }, ensure_ascii=False))
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
    log(f"模型生成 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ 模型生成已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_gen(args):
    data = json.load(open(args.file, encoding="utf-8"))
    classes = collect_classes(build_tree(data), args.root)
    print(GENERATORS[args.lang][1](classes, args.root))
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="codegen", description=f"模型生成 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("gen"); sp.add_argument("file")
    sp.add_argument("--lang", default="swift", choices=list(GENERATORS))
    sp.add_argument("--root", default="Root"); sp.set_defaults(f=cmd_gen)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
