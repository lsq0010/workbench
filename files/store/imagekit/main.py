#!/usr/bin/env python3
"""图片工具箱 —— 批量处理图片：改尺寸、转格式、压缩。

为什么做这个
  截图攒多了很占地方，而且发出去常常要"压到 2MB 以内"。
  一张张用预览改太慢，这里选个文件夹一次处理完，并告诉你省了多少空间。

安全设计
  · 默认**不覆盖原图**，输出到子目录 `_processed/`
  · 处理前给出预估：几张、总共多大、处理后会多大
  · 只在用户点「开始」之后才写文件

依赖 Pillow（用于压缩/缩放）。没装时会明确提示，不会静默失败。
"""
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("IMAGEKIT_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "imagekit.pid")
LOGFILE = os.path.join(HOME, "imagekit.log")
HISTORY = os.path.join(HOME, "jobs.jsonl")          # 只追加
DEFAULT_PORT = 8904

EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif")


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def have_pil():
    try:
        import PIL  # noqa
        return True, getattr(PIL, "__version__", "?")
    except Exception as exc:
        return False, str(exc)


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def list_images(folder, recursive=False):
    out = []
    if not os.path.isdir(folder):
        return out
    if recursive:
        for cur, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "_processed"]
            for f in files:
                if f.lower().endswith(EXTS):
                    out.append(os.path.join(cur, f))
    else:
        for f in sorted(os.listdir(folder)):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and f.lower().endswith(EXTS):
                out.append(p)
    return sorted(out)


def image_info(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return {"w": im.width, "h": im.height, "mode": im.mode,
                    "format": im.format, "bytes": os.path.getsize(path)}
    except Exception as exc:
        return {"error": str(exc), "bytes": os.path.getsize(path) if os.path.exists(path) else 0}


def scan(folder, recursive=False, max_n=400):
    files = list_images(folder, recursive)[:max_n]
    items, total = [], 0
    for p in files:
        info = image_info(p)
        total += info.get("bytes", 0)
        items.append({"path": p, "name": os.path.basename(p), **info})
    return {"folder": folder, "count": len(items), "total": total,
            "total_human": human(total), "items": items}


def process(folder, outdir=None, max_width=0, max_height=0, fmt="", quality=82,
            recursive=False, overwrite=False, max_n=400):
    """真正处理。返回每个文件的处理结果"""
    ok, ver = have_pil()
    if not ok:
        return {"error": f"没装 Pillow，无法压缩/缩放。装：python3 -m pip install --user Pillow（{ver}）"}
    from PIL import Image

    files = list_images(folder, recursive)[:max_n]
    if not files:
        return {"error": "这个目录里没有找到图片"}

    outdir = outdir or os.path.join(folder, "_processed")
    results, before, after = [], 0, 0
    t0 = time.time()

    for p in files:
        try:
            b = os.path.getsize(p)
            before += b
            with Image.open(p) as im:
                src_fmt = (im.format or "").upper()
                target = (fmt or src_fmt or "JPEG").upper()
                if target == "JPG":
                    target = "JPEG"

                # 处理透明通道：转 JPEG 时铺白底
                work = im
                if target == "JPEG" and work.mode in ("RGBA", "LA", "P"):
                    work = work.convert("RGBA")
                    bg = Image.new("RGB", work.size, (255, 255, 255))
                    bg.paste(work, mask=work.split()[-1])
                    work = bg
                elif target == "JPEG" and work.mode != "RGB":
                    work = work.convert("RGB")

                # 缩放：按最长边等比缩，不放大
                w, h = work.size
                scale = 1.0
                if max_width and w > max_width:
                    scale = min(scale, max_width / w)
                if max_height and h > max_height:
                    scale = min(scale, max_height / h)
                if scale < 1.0:
                    work = work.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                       Image.LANCZOS)

                base = os.path.splitext(os.path.basename(p))[0]
                ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "TIFF": ".tif"}.get(target,
                                                                                            "." + target.lower())
                if overwrite:
                    dest = p
                else:
                    os.makedirs(outdir, exist_ok=True)
                    dest = os.path.join(outdir, base + ext)

                save_kw = {}
                if target == "JPEG":
                    save_kw = {"quality": quality, "optimize": True, "progressive": True}
                elif target == "PNG":
                    save_kw = {"optimize": True}
                elif target == "WEBP":
                    save_kw = {"quality": quality, "method": 5}
                work.save(dest, target, **save_kw)

            nb = os.path.getsize(dest)
            after += nb
            results.append({"name": os.path.basename(p), "ok": True,
                            "before": b, "after": nb, "out": dest,
                            "w": work.width, "h": work.height,
                            "saved": b - nb, "ratio": round((1 - nb / b) * 100, 1) if b else 0})
        except Exception as exc:
            results.append({"name": os.path.basename(p), "ok": False, "error": str(exc)})

    secs = round(time.time() - t0, 2)
    job = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "folder": folder,
           "outdir": outdir if not overwrite else "(覆盖原图)",
           "count": len(results), "before": before, "after": after,
           "saved": before - after, "seconds": secs,
           "opt": {"max_width": max_width, "max_height": max_height, "fmt": fmt,
                   "quality": quality}}
    with open(HISTORY, "a", encoding="utf-8") as f:      # 只追加
        f.write(json.dumps(job, ensure_ascii=False) + "\n")
    log(f"[process] {len(results)} 张 {human(before)} → {human(after)}"
        f"（省 {human(before-after)}，{secs}s）")
    return {"results": results, "before": before, "after": after,
            "saved": before - after, "before_human": human(before),
            "after_human": human(after), "saved_human": human(before - after),
            "seconds": secs, "outdir": outdir, "overwrite": overwrite}


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "imagekit/" + VERSION

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
        import urllib.parse
        u = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HOME, "index.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as exc:
                return self._send(500, f"<h1>{exc}</h1>", "text/html; charset=utf-8")

        if u.path == "/api/status":
            ok, ver = have_pil()
            return self._send(200, json.dumps({
                "version": VERSION, "pil": ok, "pil_version": ver,
                "home": HOME,
            }, ensure_ascii=False))

        if u.path == "/api/scan":
            folder = (qs.get("folder", [os.path.expanduser("~/Desktop")])[0] or "").strip()
            rec = qs.get("recursive", ["0"])[0] == "1"
            if not os.path.isdir(folder):
                return self._send(400, json.dumps(
                    {"error": f"目录不存在：{folder}"}, ensure_ascii=False))
            d = scan(folder, rec)
            log(f"[scan] {folder} → {d['count']} 张 {d['total_human']}")
            return self._send(200, json.dumps(d, ensure_ascii=False))

        if u.path == "/api/history":
            rows = []
            try:
                with open(HISTORY, encoding="utf-8") as f:
                    for line in f.readlines()[-20:]:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
            except Exception:
                pass
            return self._send(200, json.dumps({"history": list(reversed(rows))},
                                              ensure_ascii=False))

        if u.path == "/api/folder":
            """列目录（给目录选择用）"""
            folder = (qs.get("path", [os.path.expanduser("~/Desktop")])[0] or "").strip()
            try:
                entries = []
                for name in sorted(os.listdir(folder)):
                    if name.startswith("."):
                        continue
                    p = os.path.join(folder, name)
                    if os.path.isdir(p):
                        entries.append({"name": name, "path": p, "dir": True})
                return self._send(200, json.dumps({"path": folder, "dirs": entries[:100],
                                                   "parent": os.path.dirname(folder)},
                                                  ensure_ascii=False))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        import urllib.parse
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/process":
            folder = (b.get("folder") or "").strip()
            if not os.path.isdir(folder):
                return self._send(400, json.dumps({"error": "目录不存在"}, ensure_ascii=False))
            r = process(folder,
                        outdir=(b.get("outdir") or "").strip() or None,
                        max_width=int(b.get("max_width") or 0),
                        max_height=int(b.get("max_height") or 0),
                        fmt=(b.get("fmt") or "").strip(),
                        quality=int(b.get("quality") or 82),
                        recursive=bool(b.get("recursive")),
                        overwrite=bool(b.get("overwrite")))
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            target = p if os.path.isdir(p) else os.path.dirname(p)
            if target and os.path.isdir(target):
                subprocess.Popen(["open", target])
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
    ok, ver = have_pil()
    log(f"图片工具箱 v{VERSION} 已启动 http://127.0.0.1:{port}"
        + (f"（Pillow {ver}）" if ok else f"（⚠️ 没装 Pillow：{ver}）"))

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
    print(f"✅ 图片工具箱已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_scan(args):
    d = scan(args.folder, args.recursive)
    print(f"{d['folder']}")
    print(f"  {d['count']} 张图片，共 {d['total_human']}")
    for it in d["items"][:args.limit]:
        if it.get("error"):
            print(f"  ❌ {it['name']}  {it['error'][:50]}")
        else:
            print(f"  {it['name'][:44]:<46} {it['w']}×{it['h']:<6} {human(it['bytes'])}")
    return 0


def cmd_process(args):
    r = process(args.folder, max_width=args.width, max_height=args.height,
                fmt=args.format, quality=args.quality, overwrite=args.overwrite)
    if r.get("error"):
        print("❌ " + r["error"])
        return 1
    print(f"处理 {len(r['results'])} 张：{r['before_human']} → {r['after_human']}"
          f"（省 {r['saved_human']}，{r['seconds']}s）")
    print(f"输出到：{r['outdir']}")
    for x in r["results"][:20]:
        if x.get("ok"):
            print(f"  {x['name'][:40]:<42} -{x['ratio']}%  {x['w']}×{x['h']}")
        else:
            print(f"  ❌ {x['name']}: {x['error'][:60]}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="imagekit", description=f"图片工具箱 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("scan"); sp.add_argument("folder"); sp.add_argument("--recursive",
        action="store_true"); sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(f=cmd_scan)
    sp = sub.add_parser("process"); sp.add_argument("folder")
    sp.add_argument("--width", type=int, default=0); sp.add_argument("--height", type=int, default=0)
    sp.add_argument("--format", default=""); sp.add_argument("--quality", type=int, default=82)
    sp.add_argument("--overwrite", action="store_true"); sp.set_defaults(f=cmd_process)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
