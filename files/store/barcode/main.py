#!/usr/bin/env python3
"""条形码工具 —— 生成各种一维码，全在本地，含解码。

为什么做
  你原来只有二维码。但条形码有它的位置：
    · 快递运单上的单号条码，行业标准就是 **Code128 / ITF-14**
    · 物流外箱的箱唛就是 **ITF-14**
    · 零售商品是 **EAN-13 / UPC-A**
  而且激光扫描枪比二维码枪便宜得多，仓库里通常两把都有。

支持
  Code128（自动压缩数字）/ Code128-C / EAN-13 / EAN-8 / UPC-A /
  Code39 / ITF（交错2/5）/ ITF-14 / Codabar

怎么保证编得对
  不是"看着像条码"，而是**真能扫出来**：
  每个格式都做「编码 → 画图 → 用 macOS Vision 扫回 → 和原文对比」。
  跑 `python3 verify.py` 就能复验（20 项，含 5 个该报错的用例）。
"""
import base64
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("BARCODE_HOME") or HERE)
PIDFILE = os.path.join(HOME, "barcode.pid")
LOGFILE = os.path.join(HOME, "barcode.log")
MAKES = os.path.join(HOME, "makes.jsonl")        # 只追加
OUTDIR = os.path.join(HOME, "out")
DEFAULT_PORT = 8925

sys.path.insert(0, HERE)
import barcodes as B          # noqa: E402
import render as R            # noqa: E402


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} [barcode] {msg}"
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


def build(fmt, data, module=2, height=90, text="", show_text=True,
          bar="#000000", bg="#ffffff", quiet=None):
    """编码 + 渲染，返回各种形式的产物。"""
    bits, name = B.encode(fmt, data)
    q = B.QUIET.get(fmt, 10) if quiet is None else int(quiet)
    label = text if text else data
    png = R.bits_to_png(bits, module=module, height=height, quiet=q,
                        bar=bar, bg=bg, text=label, show_text=show_text)
    svg = R.bits_to_svg(bits, module=module, height=height, quiet=q,
                        bar=bar, bg=bg, text=label, show_text=show_text)
    return {"ok": True, "format": fmt, "name": name, "data": data,
            "bits": len(bits), "width_units": len(bits) + q * 2,
            "png_b64": base64.b64encode(png).decode(),
            "svg": svg, "bytes": len(png)}


def safe_name(s):
    s = re.sub(r"[^\w\u4e00-\u9fff.-]", "_", (s or "barcode"))
    return s[:60] or "barcode"


def save(fmt, data, fmt_label="png", **kw):
    r = build(fmt, data, **kw)
    os.makedirs(OUTDIR, exist_ok=True)
    stamp = datetime.now().strftime("%m%d-%H%M%S")
    if fmt_label == "svg":
        path = os.path.join(OUTDIR, "%s-%s.svg" % (safe_name(data), stamp))
        open(path, "w", encoding="utf-8").write(r["svg"])
    else:
        path = os.path.join(OUTDIR, "%s-%s.png" % (safe_name(data), stamp))
        open(path, "wb").write(base64.b64decode(r["png_b64"]))
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "user_id": _user_id(), "format": fmt, "data": data,
           "file": os.path.basename(path)}
    try:
        with open(MAKES, "a", encoding="utf-8") as f:      # 只追加
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    log(f"生成 {fmt}「{data[:30]}」→ {os.path.basename(path)}")
    return {"ok": True, "path": path, "name": os.path.basename(path),
            "bytes": os.path.getsize(path)}


def history(limit=30):
    rows = []
    try:
        with open(MAKES, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return list(reversed(rows[-limit:]))


def decode_image(path):
    """用 macOS Vision 扫图里的条码（验证/逆向用）。"""
    dec = os.path.join(HERE, "decode.swift")
    if not os.path.isfile(dec):
        return {"ok": False, "error": "找不到 decode.swift"}
    if not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在"}
    try:
        r = subprocess.run(["swift", dec, path], capture_output=True,
                           text=True, timeout=300)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}
    out = (r.stdout or "").strip()
    if not out or out == "NONE":
        return {"ok": False, "error": "没扫到条码" +
                (("：" + r.stderr[:120]) if r.stderr else "")}
    rows = []
    for line in out.split("\n"):
        p = line.split("\t")
        if len(p) > 1:
            rows.append({"symbology": p[0].replace("VNBarcodeSymbology", ""),
                         "data": p[1]})
    return {"ok": True, "items": rows, "count": len(rows)}


# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "barcode/0.1.0"

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
                "version": "0.1.0",
                "pil": R.HAS_PIL,
                "formats": [{"id": k, **v} for k, v in B.FORMATS.items()],
                "history": history(12),
                "outdir": OUTDIR,
            }, ensure_ascii=False))
        if u.path == "/api/file":
            """给界面显示缩略图 / 预览生成的图（只给本功能输出目录里的）"""
            p = (qs.get("path", [""])[0] or "").strip()
            ap = os.path.abspath(p)
            if not ap.startswith(os.path.abspath(OUTDIR) + os.sep):
                return self._send(403, b"only own output", "text/plain")
            if not os.path.isfile(ap):
                return self._send(404, b"not found", "text/plain")
            ctype = "image/svg+xml" if ap.endswith(".svg") else "image/png"
            with open(ap, "rb") as f:
                return self._send(200, f.read(8 * 1024 * 1024), ctype)
        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/build":
            try:
                r = build(b.get("format") or "code128", b.get("data") or "",
                          int(b.get("module") or 2), int(b.get("height") or 90),
                          b.get("text") or "", b.get("show_text", True),
                          b.get("bar") or "#000000", b.get("bg") or "#ffffff")
                return self._send(200, json.dumps(r, ensure_ascii=False))
            except Exception as exc:
                return self._send(200, json.dumps(
                    {"ok": False, "error": str(exc)[:200]}, ensure_ascii=False))
        if u.path == "/api/batch":
            """一个格式，一批内容（比如一批运单号）"""
            fmt = b.get("format") or "code128"
            raw = b.get("data") or ""
            items = [x.strip() for x in re.split(r"[\n,;]+", raw) if x.strip()]
            if not items:
                return self._send(200, json.dumps(
                    {"ok": False, "error": "没有内容"}, ensure_ascii=False))
            if len(items) > 200:
                return self._send(200, json.dumps(
                    {"ok": False, "error": "一次最多 200 个"}, ensure_ascii=False))
            out, bad = [], []
            for d in items:
                try:
                    rr = save(fmt, d, b.get("save_as") or "png",
                              module=int(b.get("module") or 2),
                              height=int(b.get("height") or 90),
                              show_text=b.get("show_text", True))
                    out.append(rr)
                except Exception as exc:
                    bad.append({"data": d, "error": str(exc)[:100]})
            return self._send(200, json.dumps(
                {"ok": True, "count": len(out), "items": out, "failed": bad,
                 "outdir": OUTDIR}, ensure_ascii=False))
        if u.path == "/api/save":
            try:
                r = save(b.get("format") or "code128", b.get("data") or "",
                         b.get("save_as") or "png",
                         module=int(b.get("module") or 2),
                         height=int(b.get("height") or 90),
                         show_text=b.get("show_text", True),
                         bar=b.get("bar") or "#000000",
                         bg=b.get("bg") or "#ffffff")
                return self._send(200, json.dumps(r, ensure_ascii=False))
            except Exception as exc:
                return self._send(200, json.dumps(
                    {"ok": False, "error": str(exc)[:200]}, ensure_ascii=False))
        if u.path == "/api/decode":
            p = (b.get("path") or "").strip()
            if not p:
                return self._send(400, json.dumps({"ok": False, "error": "没给路径"},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(decode_image(p), ensure_ascii=False))
        if u.path == "/api/verify":
            """界面上点「跑一遍验证」时调这个 —— 转发给 verify.py"""
            v = os.path.join(HERE, "verify.py")
            if not os.path.isfile(v):
                return self._send(200, json.dumps(
                    {"ok": False, "error": "找不到 verify.py"}, ensure_ascii=False))
            try:
                r = subprocess.run([sys.executable, v], capture_output=True,
                                   text=True, timeout=900, cwd=HERE)
                out = (r.stdout or "") + (r.stderr or "")
                # 从输出里抓「通过 N，失败 M」
                m = re.search(r"通过 (\d+)，失败 (\d+)", out)
                summary = ("全部通过（%s 项）" % m.group(1)) if (m and m.group(2) == "0") \
                    else (m.group(0) if m else "看输出")
                return self._send(200, json.dumps(
                    {"ok": r.returncode == 0, "summary": summary,
                     "output": out[-4000:]}, ensure_ascii=False))
            except Exception as exc:
                return self._send(200, json.dumps(
                    {"ok": False, "error": str(exc)[:150]}, ensure_ascii=False))

        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            ap = os.path.abspath(p)
            if ap.startswith(os.path.abspath(OUTDIR)) and os.path.exists(ap):
                subprocess.Popen(["open", "-R", ap])
                return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return self._send(404, json.dumps({"ok": False, "error": "路径不对"},
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
    log(f"条形码工具 v0.1.0 已启动 http://127.0.0.1:{port}"
        f"（{len(B.FORMATS)} 种码制）")

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


def cmd_verify(args):
    """跑验证（委托给 verify.py）"""
    v = os.path.join(HERE, "verify.py")
    return subprocess.call([sys.executable, v] + (["-v"] if args.v else []))


def cmd_make(args):
    try:
        r = save(args.format, args.data, args.save_as,
                 module=args.module, height=args.height)
        print("  ✅ %s" % r["path"])
        return 0
    except Exception as exc:
        print("  ❌", exc)
        return 1


def main():
    import argparse
    p = argparse.ArgumentParser(prog="barcode", description="条形码工具")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("verify"); sp.add_argument("-v", action="store_true")
    sp.set_defaults(f=cmd_verify)
    sp = sub.add_parser("make")
    sp.add_argument("data"); sp.add_argument("--format", default="code128")
    sp.add_argument("--module", type=int, default=3)
    sp.add_argument("--height", type=int, default=100)
    sp.add_argument("--save-as", default="png", choices=["png", "svg"])
    sp.set_defaults(f=cmd_make)
    a = p.parse_args()
    sys.exit(a.f(a))


if __name__ == "__main__":
    main()
