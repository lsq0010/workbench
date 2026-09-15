#!/usr/bin/env python3
"""JSON 工具箱 —— 格式互转、取值、看结构、生成模型。

为什么做这个
  平时拿到一段 JSON，想干的事无非几种：
    · 格式化成看得懂的
    · 转成 YAML / XML / CSV / QueryString 给别的工具用
    · 从一大坨里把某个字段全捞出来（JSONPath）
    · 看看这坨东西到底长什么样（结构摘要）
    · 生成类型定义
  一个个开网页或写脚本太慢，这里一次做完。

JSONPath 是自己实现的（支持 $.a.b[0]、$..key、$[*]、数组下标），不依赖库。
YAML/TOML 用系统里已有的库；没装时会明确说，不会静默出错。
"""
import csv
import io
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
HOME = os.path.abspath(os.environ.get("JSONKIT_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "jsonkit.pid")
LOGFILE = os.path.join(HOME, "jsonkit.log")
DEFAULT_PORT = 8913


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def have(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


# ══════════════════════════════════════════════════════════════
# XML
# ══════════════════════════════════════════════════════════════

def json_to_xml(obj, root="root", indent=2):
    """JSON → XML。数组元素统一用 <item>，属性用 @ 前缀（和常见约定一致）"""
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    def render(node, tag, level):
        pad = " " * (indent * level)
        if isinstance(node, dict):
            attrs = {k[1:]: v for k, v in node.items()
                     if k.startswith("@") and not isinstance(v, (dict, list))}
            children = {k: v for k, v in node.items() if not k.startswith("@")}
            a = "".join(f' {k}="{esc(v)}"' for k, v in attrs.items())
            if not children:
                return f"{pad}<{tag}{a}/>"
            lines = [f"{pad}<{tag}{a}>"]
            for k, v in children.items():
                if isinstance(v, list):
                    for item in v:
                        lines.append(render(item, k, level + 1))
                else:
                    lines.append(render(v, k, level + 1))
            lines.append(f"{pad}</{tag}>")
            return "\n".join(lines)
        if isinstance(node, list):
            return "\n".join(render(x, tag, level) for x in node)
        if node is None:
            return f"{pad}<{tag}/>"
        if isinstance(node, bool):
            return f"{pad}<{tag}>{'true' if node else 'false'}</{tag}>"
        return f"{pad}<{tag}>{esc(node)}</{tag}>"

    body = render(obj, root, 0)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


def xml_to_json(text):
    """XML → JSON。同名兄弟节点自动收成数组，属性放到 @ 键"""
    import xml.etree.ElementTree as ET

    def conv(el):
        children = list(el)
        attrs = {f"@{k}": v for k, v in el.attrib.items()}
        if not children:
            txt = (el.text or "").strip()
            if attrs:
                if txt:
                    attrs["#text"] = txt
                return attrs
            if txt == "":
                return None
            # 简单类型推断
            if txt in ("true", "false"):
                return txt == "true"
            try:
                return int(txt)
            except ValueError:
                pass
            try:
                return float(txt)
            except ValueError:
                pass
            return txt
        out = dict(attrs)
        grouped = {}
        for ch in children:
            grouped.setdefault(ch.tag, []).append(conv(ch))
        for k, v in grouped.items():
            out[k] = v[0] if len(v) == 1 else v
        return out

    root = ET.fromstring(text)
    return {root.tag: conv(root)}


# ══════════════════════════════════════════════════════════════
# CSV
# ══════════════════════════════════════════════════════════════

def flatten(obj, prefix="", sep="."):
    """把嵌套结构压平成 一层键值，CSV/Excel 用"""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}{sep}{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.update(flatten(v, key, sep))
            else:
                out[key] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = f"{prefix}[{i}]"
            if isinstance(v, (dict, list)):
                out.update(flatten(v, key, sep))
            else:
                out[key] = v
    else:
        out[prefix] = obj
    return out


def json_to_csv(obj):
    """JSON → CSV。对象数组最好；单个对象当一行"""
    if isinstance(obj, dict):
        rows = [obj]
    elif isinstance(obj, list):
        rows = obj
    else:
        return str(obj), 1
    flat = [flatten(r) if isinstance(r, (dict, list)) else {"value": r} for r in rows]
    cols = []
    for r in flat:
        for k in r:
            if k not in cols:
                cols.append(k)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in flat:
        w.writerow({k: ("" if v is None else
                        json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list))
                        else v) for k, v in r.items()})
    return buf.getvalue(), len(flat)


def csv_to_json(text):
    """CSV → JSON。能自动认数字/布尔/null"""
    rows = list(csv.DictReader(io.StringIO(text)))
    def conv(v):
        if v is None:
            return None
        s = v.strip()
        if s == "":
            return ""
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        if s.lower() in ("null", "none"):
            return None
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            pass
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except Exception:
                pass
        return v
    return [{k: conv(v) for k, v in r.items()} for r in rows]


# ══════════════════════════════════════════════════════════════
# QueryString
# ══════════════════════════════════════════════════════════════

def json_to_qs(obj, prefix=""):
    """JSON → QueryString（抓包时对着看参数很方便）"""
    parts = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}[{k}]" if prefix else str(k)
            if isinstance(v, (dict, list)):
                parts.append(json_to_qs(v, key))
            elif v is None:
                parts.append(f"{urllib.parse.quote(str(key))}=")
            elif isinstance(v, bool):
                parts.append(f"{urllib.parse.quote(str(key))}={'true' if v else 'false'}")
            else:
                parts.append(f"{urllib.parse.quote(str(key))}={urllib.parse.quote(str(v))}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = f"{prefix}[{i}]"
            if isinstance(v, (dict, list)):
                parts.append(json_to_qs(v, key))
            else:
                parts.append(f"{urllib.parse.quote(key)}={urllib.parse.quote(str(v))}")
    return "&".join(p for p in parts if p)


def qs_to_json(text):
    """QueryString → JSON。a.b 和 a[b] 都当嵌套"""
    text = text.strip().lstrip("?").lstrip("&")
    out = {}
    for pair in text.split("&"):
        if not pair:
            continue
        if "=" in pair:
            k, v = pair.split("=", 1)
        else:
            k, v = pair, ""
        k = urllib.parse.unquote_plus(k)
        v = urllib.parse.unquote_plus(v)
        # 值做类型推断
        sv = v
        if sv.lower() in ("true", "false"):
            val = sv.lower() == "true"
        elif sv.lower() in ("null", "none", ""):
            val = None if sv.lower() in ("null", "none") else ""
        else:
            try:
                val = int(sv)
            except ValueError:
                try:
                    val = float(sv)
                except ValueError:
                    val = v
        # a[b][c] 或 a.b.c → 嵌套
        keys = re.findall(r"[^.\[\]]+", k)
        cur = out
        for i, kk in enumerate(keys):
            last = i == len(keys) - 1
            nxt = keys[i + 1] if not last else None
            if last:
                if kk in cur and isinstance(cur, list):
                    cur.append(val)
                elif kk in cur:
                    cur[kk] = [cur[kk], val] if not isinstance(cur[kk], list) else cur[kk] + [val]
                else:
                    cur[kk] = val
            else:
                if kk not in cur:
                    cur[kk] = [] if nxt is not None and nxt.isdigit() else {}
                cur = cur[kk]
    return out


# ══════════════════════════════════════════════════════════════
# JSONPath（自己实现，够用就好）
# ══════════════════════════════════════════════════════════════

def jsonpath(obj, path):
    """支持：$.a.b、$['a']、$[0]、$[*]、$..key、$.a[*].b

    返回 [(路径, 值), ...]。这是常用子集，覆盖日常取数 95% 的场景。
    """
    path = (path or "$").strip()
    if path in ("$", ""):
        return [("$", obj)]

    # 把 $..key、$[*]、$[0]、$.a 切成 token
    tokens = []
    i = 0
    if path.startswith("$"):
        i = 1
    while i < len(path):
        c = path[i]
        if c == ".":
            if path[i:i + 2] == "..":
                j = i + 2
                m = re.match(r"[\w\u4e00-\u9fff\-]+", path[j:])
                if not m:
                    return []
                tokens.append(("recursive", m.group(0)))
                i = j + len(m.group(0))
            else:
                m = re.match(r"[\w\u4e00-\u9fff\-]+", path[i + 1:])
                if not m:
                    i += 1
                    continue
                tokens.append(("key", m.group(0)))
                i = i + 1 + len(m.group(0))
        elif c == "[":
            j = path.find("]", i)
            if j < 0:
                return []
            inner = path[i + 1:j].strip()
            if inner == "*":
                tokens.append(("wild", None))
            elif inner.startswith("'") or inner.startswith('"'):
                tokens.append(("key", inner[1:-1]))
            elif re.fullmatch(r"-?\d+", inner):
                tokens.append(("index", int(inner)))
            elif inner.startswith("?"):
                m = re.match(r"\?\(\s*@\.([\w\u4e00-\u9fff\-]+)\s*(==|!=|>|<|>=|<=)\s*(.+?)\s*\)",
                             inner)
                if not m:
                    return []
                tokens.append(("filter", (m.group(1), m.group(2), m.group(3))))
            else:
                tokens.append(("key", inner))
            i = j + 1
        else:
            m = re.match(r"[\w\u4e00-\u9fff\-]+", path[i:])
            if not m:
                i += 1
                continue
            tokens.append(("key", m.group(0)))
            i += len(m.group(0))

    def walk(node, p, rest):
        if not rest:
            return [(p, node)]
        tok, arg = rest[0]
        tail = rest[1:]
        out = []
        if tok == "key":
            if isinstance(node, dict) and arg in node:
                out += walk(node[arg], f"{p}.{arg}", tail)
        elif tok == "index":
            if isinstance(node, list) and -len(node) <= arg < len(node):
                idx = arg if arg >= 0 else len(node) + arg
                out += walk(node[idx], f"{p}[{idx}]", tail)
        elif tok == "wild":
            if isinstance(node, dict):
                for k, v in node.items():
                    out += walk(v, f"{p}.{k}", tail)
            elif isinstance(node, list):
                for idx, v in enumerate(node):
                    out += walk(v, f"{p}[{idx}]", tail)
        elif tok == "recursive":
            def rec(n, np):
                r = []
                if isinstance(n, dict):
                    for k, v in n.items():
                        if k == arg:
                            r += walk(v, f"{np}.{k}", tail)
                        r += rec(v, f"{np}.{k}")
                elif isinstance(n, list):
                    for idx, v in enumerate(n):
                        r += rec(v, f"{np}[{idx}]")
                return r
            out += rec(node, p)
        elif tok == "filter":
            key, op, raw = arg
            raw = raw.strip().strip("'\"")
            val = raw
            for caster in (int, float):
                try:
                    val = caster(raw)
                    break
                except ValueError:
                    continue
            if val == "true":
                val = True
            elif val == "false":
                val = False
            if isinstance(node, list):
                for idx, item in enumerate(node):
                    if isinstance(item, dict) and key in item:
                        a = item[key]
                        try:
                            ok = {"==": a == val, "!=": a != val, ">": a > val,
                                  "<": a < val, ">=": a >= val, "<=": a <= val}[op]
                        except TypeError:
                            ok = False
                        if ok:
                            out += walk(item, f"{p}[{idx}]", tail)
        return out

    return walk(obj, "$", tokens)


# ══════════════════════════════════════════════════════════════
# 结构摘要 / Schema
# ══════════════════════════════════════════════════════════════

def summarize(obj, max_depth=6):
    """看这坨 JSON 长什么样：每个路径的类型、示例值、出现次数"""
    rows = []

    def walk(node, path, depth):
        if depth > max_depth:
            return
        t = ("对象" if isinstance(node, dict) else
             "数组" if isinstance(node, list) else
             "空" if node is None else
             "布尔" if isinstance(node, bool) else
             "整数" if isinstance(node, int) else
             "小数" if isinstance(node, float) else "文本")
        sample = ""
        if isinstance(node, (dict, list)):
            sample = f"{len(node)} 项"
        else:
            sample = json.dumps(node, ensure_ascii=False)
            if len(sample) > 60:
                sample = sample[:60] + "…"
        rows.append({"path": path or "$", "type": t, "sample": sample,
                     "depth": depth})
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k, depth + 1)
        elif isinstance(node, list):
            if node:
                walk(node[0], f"{path}[0]", depth + 1)
                # 数组里不同类型也列出来
                seen = {type(x).__name__ for x in node}
                if len(seen) > 1:
                    rows.append({"path": f"{path}[*]", "type": "混合类型",
                                 "sample": "、".join(sorted(seen)), "depth": depth + 1})

    walk(obj, "", 0)
    return rows


def infer_schema(obj):
    """从数据反推 JSON Schema"""
    def t_of(v):
        if isinstance(v, dict):
            return {"type": "object",
                    "properties": {k: t_of(x) for k, x in v.items()},
                    "required": list(v.keys())}
        if isinstance(v, list):
            if not v:
                return {"type": "array", "items": {}}
            # 合并数组元素的类型
            items = [t_of(x) for x in v]
            first = items[0]
            merged = first
            for other in items[1:]:
                if other != first:
                    merged = {"anyOf": [first, other]}
                    break
            return {"type": "array", "items": merged}
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            return {"type": "boolean"}
        if isinstance(v, int):
            return {"type": "integer"}
        if isinstance(v, float):
            return {"type": "number"}
        out = {"type": "string"}
        if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}.*)?", v):
            out["format"] = "date-time"
        return out

    s = t_of(obj)
    s["$schema"] = "http://json-schema.org/draft-07/schema#"
    return s


# ══════════════════════════════════════════════════════════════
# 主转换
# ══════════════════════════════════════════════════════════════

def parse_input(text, fmt):
    """把输入解析成 Python 对象"""
    text = text or ""
    if fmt == "json":
        return json.loads(text)
    if fmt == "yaml":
        import yaml
        return yaml.safe_load(text)
    if fmt == "toml":
        import toml
        return toml.loads(text)
    if fmt == "xml":
        return xml_to_json(text)
    if fmt == "csv":
        return csv_to_json(text)
    if fmt == "querystring":
        return qs_to_json(text)
    raise ValueError(f"不认识的格式：{fmt}")


def dump_output(obj, fmt, indent=2, sort_keys=False, ensure_ascii=False):
    if fmt == "json":
        return json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii,
                          sort_keys=sort_keys)
    if fmt == "json-min":
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=ensure_ascii,
                          sort_keys=sort_keys)
    if fmt == "yaml":
        import yaml
        return yaml.safe_dump(obj, allow_unicode=True, sort_keys=sort_keys,
                              default_flow_style=False, indent=2)
    if fmt == "toml":
        import toml
        if not isinstance(obj, dict):
            raise ValueError("TOML 的最外层必须是对象（{...}），数组或单值不行")
        return toml.dumps(obj)
    if fmt == "xml":
        return json_to_xml(obj)
    if fmt == "csv":
        text, _ = json_to_csv(obj)
        return text
    if fmt == "querystring":
        return json_to_qs(obj)
    raise ValueError(f"不认识的格式：{fmt}")


def convert(text, src, dst, indent=2, sort_keys=False, ensure_ascii=False):
    t0 = time.time()
    obj = parse_input(text, src)
    out = dump_output(obj, dst, indent=indent, sort_keys=sort_keys,
                      ensure_ascii=ensure_ascii)
    return {"ok": True, "text": out, "seconds": round(time.time() - t0, 4),
            "bytes": len(out.encode("utf-8"))}


def to_excel(obj, path):
    """导出 Excel（每个顶层键一个 sheet，数组展开成表）"""
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)

    def add_sheet(name, rows):
        ws = wb.create_sheet(title=name[:28] or "Sheet")
        if not rows:
            return
        if isinstance(rows, dict):
            rows = [rows]
        flat = [flatten(r) if isinstance(r, (dict, list)) else {"value": r} for r in rows]
        cols = []
        for r in flat:
            for k in r:
                if k not in cols:
                    cols.append(k)
        ws.append(cols)
        for r in flat:
            ws.append(["" if r.get(c) is None else
                       json.dumps(r[c], ensure_ascii=False)
                       if isinstance(r.get(c), (dict, list)) else r.get(c) for c in cols])
        for i, c in enumerate(cols, 1):
            w = min(48, max(10, len(str(c)) + 2))
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w

    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list):
                add_sheet(str(k), v)
            elif isinstance(v, dict):
                add_sheet(str(k), [v])
            else:
                add_sheet("值", [{"键": k, "值": v}])
    elif isinstance(obj, list):
        add_sheet("数据", obj)
    else:
        add_sheet("值", [{"值": obj}])
    if not wb.sheetnames:
        wb.create_sheet("空")
    wb.save(path)
    return path


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "jsonkit/" + VERSION

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
            return self._send(200, json.dumps({
                "version": VERSION,
                "libs": {m: have(m) for m in ("yaml", "toml", "openpyxl")},
                "formats": [
                    {"id": "json", "name": "JSON"},
                    {"id": "yaml", "name": "YAML", "need": "yaml"},
                    {"id": "toml", "name": "TOML", "need": "toml"},
                    {"id": "xml", "name": "XML"},
                    {"id": "csv", "name": "CSV"},
                    {"id": "querystring", "name": "QueryString"},
                ],
            }, ensure_ascii=False))
        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        t0 = time.time()

        if u.path == "/api/convert":
            try:
                r = convert(b.get("text") or "", b.get("from") or "json",
                            b.get("to") or "json", indent=int(b.get("indent") or 2),
                            sort_keys=bool(b.get("sort_keys")),
                            ensure_ascii=bool(b.get("ensure_ascii")))
                log(f"[convert] {b.get('from')} → {b.get('to')} "
                    f"{len((b.get('text') or ''))} → {r['bytes']} 字节")
                return self._send(200, json.dumps(r, ensure_ascii=False))
            except Exception as exc:
                msg = str(exc)
                if "Expecting" in msg or "line" in msg:
                    msg = f"解析失败：{msg}"
                elif "No module named" in msg:
                    mod = msg.split("'")[1] if "'" in msg else "?"
                    msg = f"这台机器没装 {mod}，装一下：python3 -m pip install --user {mod}"
                return self._send(200, json.dumps({"ok": False, "error": msg},
                                                  ensure_ascii=False))

        if u.path == "/api/query":
            try:
                obj = parse_input(b.get("text") or "", b.get("from") or "json")
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": f"解析失败：{exc}"},
                                                  ensure_ascii=False))
            path = b.get("path") or "$"
            hits = jsonpath(obj, path)
            MAX = 300
            truncated = len(hits) > MAX
            items = []
            for p, v in hits[:MAX]:
                try:
                    s = json.dumps(v, ensure_ascii=False)
                except Exception:
                    s = str(v)
                items.append({"path": p,
                              "type": ("对象" if isinstance(v, dict) else
                                       "数组" if isinstance(v, list) else
                                       "空" if v is None else
                                       "布尔" if isinstance(v, bool) else
                                       "数字" if isinstance(v, (int, float)) else "文本"),
                              "value": s if len(s) <= 2000 else s[:2000] + "…",
                              "size": len(s)})
            log(f"[query] {path} → {len(hits)} 个结果")
            return self._send(200, json.dumps({
                "ok": True, "count": len(hits), "items": items, "truncated": truncated,
                "seconds": round(time.time() - t0, 4),
                # 顺手把结果拼成合法 JSON，方便直接复制
                "as_json": json.dumps([v for _, v in hits[:MAX]], ensure_ascii=False, indent=2)
                           if len(hits) != 1 else json.dumps(hits[0][1], ensure_ascii=False, indent=2),
            }, ensure_ascii=False))

        if u.path == "/api/summary":
            try:
                obj = parse_input(b.get("text") or "", b.get("from") or "json")
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": f"解析失败：{exc}"},
                                                  ensure_ascii=False))
            rows = summarize(obj)
            return self._send(200, json.dumps({"ok": True, "rows": rows,
                                               "count": len(rows)}, ensure_ascii=False))

        if u.path == "/api/schema":
            try:
                obj = parse_input(b.get("text") or "", b.get("from") or "json")
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": f"解析失败：{exc}"},
                                                  ensure_ascii=False))
            s = infer_schema(obj)
            return self._send(200, json.dumps({"ok": True,
                                               "schema": json.dumps(s, ensure_ascii=False,
                                                                    indent=2)},
                                              ensure_ascii=False))

        if u.path == "/api/excel":
            if not have("openpyxl"):
                return self._send(200, json.dumps(
                    {"ok": False, "error": "没装 openpyxl，装一下：python3 -m pip install --user openpyxl"},
                    ensure_ascii=False))
            try:
                obj = parse_input(b.get("text") or "", b.get("from") or "json")
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": f"解析失败：{exc}"},
                                                  ensure_ascii=False))
            folder = b.get("folder") or os.path.expanduser("~/Desktop")
            if not os.path.isdir(folder):
                folder = os.path.expanduser("~/Desktop")
            name = (b.get("name") or "数据").strip()[:40] or "数据"
            path = os.path.join(folder, f"{name}.xlsx")
            i = 2
            while os.path.exists(path):
                path = os.path.join(folder, f"{name}-{i}.xlsx")
                i += 1
            try:
                to_excel(obj, path)
                log(f"[excel] {path}")
                return self._send(200, json.dumps({"ok": True, "path": path,
                                                   "bytes": os.path.getsize(path)},
                                                  ensure_ascii=False))
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": str(exc)},
                                                  ensure_ascii=False))

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
    log(f"JSON 工具箱 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ JSON 工具箱已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_conv(args):
    r = convert(args.text if args.text != "-" else sys.stdin.read(),
                args.frm, args.to, sort_keys=args.sort)
    print(r["text"])
    return 0


def cmd_query(args):
    obj = parse_input(args.text if args.text != "-" else sys.stdin.read(), args.frm)
    hits = jsonpath(obj, args.path)
    print(f"{len(hits)} 个结果")
    for p, v in hits[:60]:
        s = json.dumps(v, ensure_ascii=False)
        print(f"  {p}\n    {s[:200]}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="jsonkit", description=f"JSON 工具箱 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("convert"); sp.add_argument("text")
    sp.add_argument("--from", dest="frm", default="json")
    sp.add_argument("--to", default="yaml"); sp.add_argument("--sort", action="store_true")
    sp.set_defaults(f=cmd_conv)
    sp = sub.add_parser("query"); sp.add_argument("text"); sp.add_argument("path")
    sp.add_argument("--from", dest="frm", default="json"); sp.set_defaults(f=cmd_query)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
