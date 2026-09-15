#!/usr/bin/env python3
"""上架截图工坊 —— 把截图裁成 App Store 要的尺寸。

为什么做这个
  你下载夹里有：
    · 审核图片/IMG_0033~0036.PNG —— 1179×2556（6.3 寸 iPhone）
    · snapmonk 的成品 panel-*.png —— 1290×2796（6.9 寸）
  App Store Connect 现在要的是 6.9/6.7 寸那几档，尺寸不对传不上去。
  手工一张张裁太慢，而且很容易裁歪。

这个工具做的事
  · 按 App Store 各档位的**精确尺寸**输出（6.9/6.7/6.5/5.5 寸 + iPad）
  · cover（裁满）或 contain（留边）两种适配，留边可以指定背景色
  · 可以加一行标题文字（字号、颜色、位置可调）
  · 批量处理一整个目录

依赖 Pillow（处理图片）。零网络。
"""
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
HOME = os.path.abspath(os.environ.get("SHOTKIT_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "shotkit.pid")
LOGFILE = os.path.join(HOME, "shotkit.log")
JOBS = os.path.join(HOME, "jobs.jsonl")          # 只追加
DEFAULT_PORT = 8916

# App Store Connect 目前的截图规格（宽 × 高，竖版）
PRESETS = [
    {"id": "iphone-69", "name": 'iPhone 6.9\"（首选 · 传这档就够）', "w": 1320, "h": 2868,
     "note": "现在 App Store 的首选尺寸，传这一档就够"},
    {"id": "iphone-67", "name": 'iPhone 6.9\" 备选尺寸', "w": 1290, "h": 2796,
     "note": "和 6.9 寸二选一；snapmonk 出的是这一档"},
    {"id": "iphone-65", "name": 'iPhone 6.5\"（没 6.9 时必需）', "w": 1242, "h": 2688,
     "note": "老机型，一般不用了"},
    {"id": "iphone-61", "name": 'iPhone 6.3\"（可选档）', "w": 1179, "h": 2556,
     "note": "你审核图片/ 里就是这个尺寸 —— 注意它不是 App Store 要的那档"},
    {"id": "iphone-55", "name": 'iPhone 5.5"（8 Plus）', "w": 1242, "h": 2208,
     "note": "很老的机型，除非必须兼容"},
    {"id": "ipad-13", "name": 'iPad 13"', "w": 2064, "h": 2752,
     "note": "iPad 版应用用这个"},
    {"id": "ipad-129", "name": 'iPad 12.9"', "w": 2048, "h": 2732,
     "note": "老 iPad Pro"},
]


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _user_id():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib")
        if p not in sys.path:
            sys.path.insert(0, p)
        import platform_lib
        return platform_lib.user_id()
    except Exception:
        return ""


def have_pil():
    try:
        import PIL  # noqa
        return True, getattr(PIL, "__version__", "?")
    except Exception as exc:
        return False, str(exc)


def hex2rgb(s, default=(255, 255, 255)):
    s = (s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return default
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def find_font(size):
    """找一个能显示中文的系统字体"""
    cands = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    from PIL import ImageFont
    for p in cands:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def compose(src_path, preset, mode="cover", bg="#ffffff", caption="",
            caption_color="#111111", caption_pos="top", caption_size=0.06,
            pad_ratio=0.0, radius=0, shadow=False, caption_bg="", caption_alpha=0.82):
    """把一张图处理成 App Store 尺寸"""
    from PIL import Image, ImageDraw
    W, H = int(preset["w"]), int(preset["h"])
    im = Image.open(src_path).convert("RGB")

    canvas = Image.new("RGB", (W, H), hex2rgb(bg, (255, 255, 255)))

    if mode == "cover":
        # 裁满：按短边放大，居中裁
        scale = max(W / im.width, H / im.height)
        nw, nh = max(1, round(im.width * scale)), max(1, round(im.height * scale))
        r = im.resize((nw, nh), Image.LANCZOS)
        left = (nw - W) // 2
        top = (nh - H) // 2
        canvas.paste(r.crop((left, top, left + W, top + H)), (0, 0))
    else:
        # 留边：完整放进去，四周留背景
        inner_w = int(W * (1 - pad_ratio * 2))
        inner_h = int(H * (1 - pad_ratio * 2))
        scale = min(inner_w / im.width, inner_h / im.height)
        nw, nh = max(1, round(im.width * scale)), max(1, round(im.height * scale))
        r = im.resize((nw, nh), Image.LANCZOS)
        if radius:
            # 圆角遮罩
            mask = Image.new("L", (nw, nh), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, nw - 1, nh - 1],
                                                   radius=radius, fill=255)
            canvas.paste(r, ((W - nw) // 2, (H - nh) // 2), mask)
        else:
            canvas.paste(r, ((W - nw) // 2, (H - nh) // 2))

    if caption:
        d = ImageDraw.Draw(canvas)
        fs = max(16, int(H * max(0.02, min(caption_size, 0.15))))
        font = find_font(fs)
        # 简单换行：按字符宽度估算
        max_w = int(W * 0.86)
        lines, cur = [], ""
        for ch in caption:
            test = cur + ch
            try:
                tw = d.textlength(test, font=font)
            except Exception:
                tw = len(test) * fs * 0.6
            if tw > max_w and cur:
                lines.append(cur)
                cur = ch
            else:
                cur = test
        if cur:
            lines.append(cur)
        lines = lines[:3]
        line_h = int(fs * 1.35)
        total_h = line_h * len(lines)
        y0 = int(H * 0.06) if caption_pos == "top" else H - total_h - int(H * 0.06)
        y = y0
        # 标题背景条：压在 App 自己的界面上时，没这条会看不清
        #（实测：黑色标题压在橙色 Logo 上几乎读不出来）
        if caption_bg:
            bar_pad = int(fs * 0.55)
            from PIL import Image as _I
            overlay = _I.new("RGBA", (W, total_h + bar_pad * 2), (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            rgb = hex2rgb(caption_bg, (255, 255, 255))
            alpha = int(max(0.0, min(float(caption_alpha), 1.0)) * 255)
            od.rectangle([0, 0, W, total_h + bar_pad * 2], fill=rgb + (alpha,))
            canvas.paste(Image.alpha_composite(
                canvas.crop((0, max(0, y0 - bar_pad), W,
                             max(0, y0 - bar_pad) + total_h + bar_pad * 2)).convert("RGBA"),
                overlay).convert("RGB"), (0, max(0, y0 - bar_pad)))
            y = y0
        for ln in lines:
            try:
                tw = d.textlength(ln, font=font)
            except Exception:
                tw = len(ln) * fs * 0.6
            x = (W - tw) // 2
            if shadow:
                d.text((x + max(1, fs // 25), y + max(1, fs // 25)), ln,
                       font=font, fill=(0, 0, 0, 120))
            d.text((x, y), ln, font=font, fill=hex2rgb(caption_color, (17, 17, 17)))
            y += line_h

    return canvas


def list_images(folder, recursive=False):
    exts = (".png", ".jpg", ".jpeg", ".heic", ".tif", ".tiff", ".bmp")
    out = []
    if not os.path.isdir(folder):
        return out
    if recursive:
        for cur, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in sorted(files):
                if f.lower().endswith(exts) and not f.startswith("."):
                    out.append(os.path.join(cur, f))
    else:
        for f in sorted(os.listdir(folder)):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and f.lower().endswith(exts) and not f.startswith("."):
                out.append(p)
    return out


def inspect_folder(folder):
    """看一个目录里的图都是什么尺寸 —— 先判断"要不要处理" """
    from PIL import Image
    files = list_images(folder)
    rows, sizes = [], {}
    for p in files[:200]:
        try:
            with Image.open(p) as im:
                w, h = im.width, im.height
            rows.append({"name": os.path.basename(p), "path": p, "w": w, "h": h,
                         "bytes": os.path.getsize(p)})
            sizes[(w, h)] = sizes.get((w, h), 0) + 1
        except Exception as exc:
            rows.append({"name": os.path.basename(p), "path": p, "error": str(exc)})
    # 每档预设要处理几张
    need = {}
    for pre in PRESETS:
        need[pre["id"]] = len([r for r in rows
                               if not r.get("error") and (r["w"], r["h"]) != (pre["w"], pre["h"])])
    return {"folder": folder, "count": len(rows), "items": rows,
            "sizes": [{"w": k[0], "h": k[1], "count": v} for k, v in
                      sorted(sizes.items(), key=lambda x: -x[1])],
            "need": need}


def run_batch(folder, preset_id, outdir=None, recursive=False, **opts):
    """按某个尺寸批量导出"""
    ok, ver = have_pil()
    if not ok:
        return {"error": "没装 Pillow，装一下：python3 -m pip install --user Pillow（%s）" % ver}
    pre = next((p for p in PRESETS if p["id"] == preset_id), None)
    if not pre:
        return {"error": "不认识的尺寸档位：%s" % preset_id}

    files = list_images(folder, recursive)
    if not files:
        return {"error": "这个目录里没有图片"}
    outdir = outdir or os.path.join(folder, "上架截图-" + pre["id"])
    os.makedirs(outdir, exist_ok=True)

    t0 = time.time()
    results = []
    for i, p in enumerate(files):
        base = os.path.splitext(os.path.basename(p))[0]
        # App Store 要求文件名有序，加序号
        dest = os.path.join(outdir, "%02d-%s.png" % (i + 1, base))
        try:
            img = compose(p, pre, **opts)
            img.save(dest, "PNG", optimize=True)
            results.append({"ok": True, "name": os.path.basename(p),
                            "out": dest, "w": img.width, "h": img.height,
                            "bytes": os.path.getsize(dest)})
        except Exception as exc:
            results.append({"ok": False, "name": os.path.basename(p), "error": str(exc)})

    n_ok = len([r for r in results if r["ok"]])
    total = sum(r.get("bytes", 0) for r in results)
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "user_id": _user_id(),
           "folder": folder, "preset": preset_id, "size": f"{pre['w']}x{pre['h']}",
           "count": n_ok, "bytes": total, "outdir": outdir,
           "seconds": round(time.time() - t0, 2)}
    with open(JOBS, "a", encoding="utf-8") as f:      # 只追加
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"[batch] {pre['id']} {n_ok}/{len(files)} 张 → {outdir}（{rec['seconds']}s）")
    return {"ok": True, "count": n_ok, "total": len(files), "results": results,
            "outdir": outdir, "preset": pre, "seconds": rec["seconds"],
            "bytes": total}


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "shotkit/" + VERSION

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
            ok, ver = have_pil()
            # 默认看审核图片/ 和桌面
            guesses = [os.path.expanduser("~/Downloads/审核图片"),
                       os.path.expanduser("~/Downloads"),
                       os.path.expanduser("~/Desktop")]
            return self._send(200, json.dumps({
                "version": VERSION, "pil": ok, "pil_version": ver,
                "presets": PRESETS,
                "folders": [g for g in guesses if os.path.isdir(g)],
                "jobs": jobs_history(),
            }, ensure_ascii=False))

        if u.path == "/api/scan":
            folder = (qs.get("folder", [""])[0] or "").strip()
            if not os.path.isdir(folder):
                return self._send(400, json.dumps({"error": "目录不存在：%s" % folder},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(inspect_folder(folder), ensure_ascii=False))

        if u.path == "/api/folder":
            path = (qs.get("path", [os.path.expanduser("~/Downloads")])[0] or "").strip()
            try:
                dirs = []
                for name in sorted(os.listdir(path)):
                    if name.startswith("."):
                        continue
                    p = os.path.join(path, name)
                    if os.path.isdir(p):
                        n = len(list_images(p))
                        dirs.append({"name": name, "path": p, "images": n})
                return self._send(200, json.dumps({"path": path, "dirs": dirs[:200],
                                                   "parent": os.path.dirname(path),
                                                   "images_here": len(list_images(path))},
                                                  ensure_ascii=False))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/preview":
            """单张预览：返回处理后的图（base64），让用户先看效果再批量"""
            src = (b.get("path") or "").strip()
            if not os.path.isfile(src):
                return self._send(400, json.dumps({"ok": False, "error": "文件不存在"},
                                                  ensure_ascii=False))
            pre = next((p for p in PRESETS if p["id"] == (b.get("preset") or "")), None)
            if not pre:
                return self._send(400, json.dumps({"ok": False, "error": "没选尺寸"},
                                                  ensure_ascii=False))
            try:
                import base64
                img = compose(src, pre,
                              mode=b.get("mode") or "cover",
                              bg=b.get("bg") or "#ffffff",
                              caption=b.get("caption") or "",
                              caption_color=b.get("caption_color") or "#111111",
                              caption_pos=b.get("caption_pos") or "top",
                              caption_size=float(b.get("caption_size") or 0.06),
                              pad_ratio=float(b.get("pad_ratio") or 0.0),
                              radius=int(b.get("radius") or 0),
                              shadow=bool(b.get("shadow")),
                              caption_bg=b.get("caption_bg") or "",
                              caption_alpha=float(b.get("caption_alpha") or 0.82))
                # 预览用缩略图，别把大图塞进浏览器
                thumb = img.copy()
                thumb.thumbnail((520, 1100))
                buf = io.BytesIO()
                thumb.save(buf, "PNG", optimize=True)
                return self._send(200, json.dumps({
                    "ok": True, "preview": "data:image/png;base64," +
                    base64.b64encode(buf.getvalue()).decode(),
                    "w": img.width, "h": img.height,
                }, ensure_ascii=False))
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": str(exc)},
                                                  ensure_ascii=False))

        if u.path == "/api/batch":
            folder = (b.get("folder") or "").strip()
            if not os.path.isdir(folder):
                return self._send(400, json.dumps({"ok": False, "error": "目录不存在"},
                                                  ensure_ascii=False))
            r = run_batch(folder, b.get("preset") or "iphone-69",
                          outdir=(b.get("outdir") or "").strip() or None,
                          recursive=bool(b.get("recursive")),
                          mode=b.get("mode") or "cover",
                          bg=b.get("bg") or "#ffffff",
                          caption=b.get("caption") or "",
                          caption_color=b.get("caption_color") or "#111111",
                          caption_pos=b.get("caption_pos") or "top",
                          caption_size=float(b.get("caption_size") or 0.06),
                          pad_ratio=float(b.get("pad_ratio") or 0.0),
                          radius=int(b.get("radius") or 0),
                          shadow=bool(b.get("shadow")),
                          caption_bg=b.get("caption_bg") or "",
                          caption_alpha=float(b.get("caption_alpha") or 0.82))
            if r.get("error"):
                return self._send(200, json.dumps({"ok": False, "error": r["error"]},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            target = p if os.path.isdir(p) else os.path.dirname(p)
            if target and os.path.isdir(target):
                subprocess.Popen(["open", target])
                return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return self._send(404, json.dumps({"ok": False, "error": "目录不存在"},
                                              ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))


def jobs_history(limit=10):
    rows = []
    try:
        with open(JOBS, encoding="utf-8") as f:
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
    log(f"上架截图工坊 v{VERSION} 已启动 http://127.0.0.1:{port}"
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
    print(f"✅ 上架截图工坊已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_presets(args):
    print("  App Store 截图尺寸：")
    for p in PRESETS:
        print("    %-12s %-34s %4d×%-5d %s" % (p["id"], p["name"], p["w"], p["h"], p["note"]))
    return 0


def cmd_scan(args):
    r = inspect_folder(args.folder)
    print(f"  {r['folder']}　{r['count']} 张图")
    print("  ── 出现的尺寸 ──")
    for s in r["sizes"]:
        print("    %4d×%-5d ×%d" % (s["w"], s["h"], s["count"]))
    print("  ── 各档位需要处理的张数 ──")
    for p in PRESETS:
        n = r["need"][p["id"]]
        print("    %-34s %d 张%s" % (p["name"], n, "" if n else "（都已经是这个尺寸）"))
    return 0


def cmd_batch(args):
    r = run_batch(args.folder, args.preset, outdir=args.out,
                  mode=args.mode, caption=args.caption or "", bg=args.bg,
                  caption_bg=args.caption_bg, caption_color=args.caption_color)
    if r.get("error"):
        print("❌ " + r["error"])
        return 1
    print(f"✅ {r['count']}/{r['total']} 张 → {r['outdir']}")
    print(f"   尺寸 {r['preset']['w']}×{r['preset']['h']}　用时 {r['seconds']}s")
    for x in r["results"][:10]:
        if x["ok"]:
            print(f"     {x['name'][:36]:<38} → {os.path.basename(x['out'])}")
        else:
            print(f"     ❌ {x['name']}: {x['error']}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="shotkit", description=f"上架截图工坊 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sub.add_parser("presets").set_defaults(f=cmd_presets)
    sp = sub.add_parser("scan"); sp.add_argument("folder"); sp.set_defaults(f=cmd_scan)
    sp = sub.add_parser("batch"); sp.add_argument("folder")
    sp.add_argument("--preset", default="iphone-69")
    sp.add_argument("--out"); sp.add_argument("--mode", default="contain",
                                              choices=["cover", "contain"])
    sp.add_argument("--caption"); sp.add_argument("--bg", default="#ffffff")
    sp.add_argument("--caption-bg", dest="caption_bg", default="",
                    help="标题背景色，压在 App 界面上时用（例：#ffffff）")
    sp.add_argument("--caption-color", dest="caption_color", default="#111111")
    sp.set_defaults(f=cmd_batch)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
