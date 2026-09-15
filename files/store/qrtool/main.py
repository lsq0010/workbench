#!/usr/bin/env python3
"""二维码工具 —— 生成、识别、批量。

为什么做这个
  Mac 上要生成个二维码，要么开网页（内容传出去），要么装个 app。
  你下载夹里还躺着一个 Windows 版的二维码 exe（Mac 上跑不了）。
  这个工具全在本地算，内容不出这台电脑。

能做什么
  · 生成：文本/网址/WiFi/名片/电话/短信/邮件 —— 按类型自动拼标准格式
  · 样式：尺寸、容错级别、前景背景色、留白、中心放 logo
  · 输出：PNG（位图）和 SVG（矢量，印刷放大不糊）
  · 识别：拖张图进来解出内容（OpenCV + pyzbar 双解码器）
  · 批量：一行一个，一次生成一批

编码器是自己写的纯 Python（qrgen.py），支持版本 1~40；
正确性由 verify.py 对着成熟实现逐位比对保证，运行时零第三方依赖。
识别需要 OpenCV 或 pyzbar，没装时生成功能不受影响。
"""
import base64
import io
import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qrgen
import qrseg

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("QRTOOL_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "qrtool.pid")
LOGFILE = os.path.join(HOME, "qrtool.log")
DEFAULT_PORT = 8911


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def hex2rgb(s, default=(0, 0, 0)):
    s = (s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return default
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


# ══════════════════════════════════════════════════════════════
# 按类型拼标准内容
# ══════════════════════════════════════════════════════════════

def build_content(kind, f):
    """把表单拼成标准的二维码内容格式（别的扫码器认这些格式）"""
    def esc(s):
        # WiFi/vCard 格式里这几个字符要转义
        return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace(":", "\\:").replace('"', '\\"'))

    if kind == "text":
        return f.get("text") or ""

    if kind == "url":
        u = (f.get("url") or "").strip()
        if u and not u.startswith(("http://", "https://", "ftp://", "mailto:", "tel:")):
            u = "https://" + u
        return u

    if kind == "wifi":
        enc = f.get("wifi_enc") or "WPA"
        ssid = esc(f.get("wifi_ssid"))
        pwd = esc(f.get("wifi_pass"))
        hidden = "true" if f.get("wifi_hidden") else "false"
        if enc == "nopass":
            return f"WIFI:T:nopass;S:{ssid};;"
        return f"WIFI:T:{enc};S:{ssid};P:{pwd};H:{hidden};;"

    if kind == "vcard":
        lines = ["BEGIN:VCARD", "VERSION:3.0"]
        name = (f.get("vc_name") or "").strip()
        if name:
            lines.append(f"N:{name}")
            lines.append(f"FN:{name}")
        if f.get("vc_org"):
            lines.append(f"ORG:{f['vc_org']}")
        if f.get("vc_title"):
            lines.append(f"TITLE:{f['vc_title']}")
        if f.get("vc_tel"):
            lines.append(f"TEL;TYPE=CELL:{f['vc_tel']}")
        if f.get("vc_email"):
            lines.append(f"EMAIL:{f['vc_email']}")
        if f.get("vc_url"):
            lines.append(f"URL:{f['vc_url']}")
        if f.get("vc_note"):
            lines.append(f"NOTE:{f['vc_note']}")
        lines.append("END:VCARD")
        return "\n".join(lines)

    if kind == "tel":
        return "tel:" + (f.get("tel") or "")

    if kind == "sms":
        body = f.get("sms_body") or ""
        return f"SMSTO:{f.get('sms_to','')}:{body}" if body else f"sms:{f.get('sms_to','')}"

    if kind == "email":
        q = []
        if f.get("em_subject"):
            q.append("subject=" + urllib.parse.quote(f["em_subject"]))
        if f.get("em_body"):
            q.append("body=" + urllib.parse.quote(f["em_body"]))
        return "mailto:" + (f.get("em_to") or "") + ("?" + "&".join(q) if q else "")

    if kind == "geo":
        return f"geo:{f.get('geo_lat','')},{f.get('geo_lng','')}"

    return f.get("text") or ""


# ══════════════════════════════════════════════════════════════
# 生成
# ══════════════════════════════════════════════════════════════

def make_qr(content, ecc="M", scale=10, border=4, dark="#000000", light="#ffffff",
            logo_b64=None, logo_ratio=0.22, module_radius=0):
    """生成二维码。返回 (png_bytes, svg_str, info)"""
    if not content:
        raise ValueError("内容不能为空")
    ecc = (ecc or "M").upper()
    if ecc not in ("L", "M", "Q", "H"):
        ecc = "M"
    if logo_b64 and ecc != "H":
        # 中心盖住一部分码，容错必须拉满，否则扫不出来
        ecc = "H"
    scale = max(2, min(int(scale or 10), 60))
    border = max(0, min(int(border if border is not None else 4), 20))

    matrix, info = qrgen.encode(content, ecc)
    info["content"] = content

    d = hex2rgb(dark, (0, 0, 0))
    l = hex2rgb(light, (255, 255, 255))

    svg = qrgen.to_svg(matrix, scale=scale, border=border,
                       dark=dark if dark.startswith("#") else "#000000",
                       light=light if light.startswith("#") else "#ffffff",
                       radius=module_radius)

    png = None
    try:
        img = qrgen.to_png(matrix, scale=scale, border=border, dark=d, light=l)
        if logo_b64:
            img = paste_logo(img, logo_b64, logo_ratio)
            info["logo"] = True
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        png = buf.getvalue()
        info["png_bytes"] = len(png)
        info["pixel_size"] = img.size
    except ImportError:
        info["png_error"] = "没装 Pillow，只能导出 SVG（装：python3 -m pip install --user Pillow）"

    info["svg_bytes"] = len(svg.encode("utf-8"))
    return png, svg, info


def paste_logo(img, logo_b64, ratio=0.22):
    """把 logo 贴到中心（先铺白底再贴，避免二维码背景透出来）"""
    from PIL import Image
    raw = logo_b64.split(",")[-1]
    logo = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGBA")
    side = int(min(img.size) * max(0.08, min(ratio, 0.32)))
    logo.thumbnail((side, side), Image.LANCZOS)
    pad = max(2, side // 12)
    plate = Image.new("RGBA", (logo.width + pad * 2, logo.height + pad * 2), (255, 255, 255, 255))
    plate.paste(logo, (pad, pad), logo)
    base = img.convert("RGBA")
    pos = ((base.width - plate.width) // 2, (base.height - plate.height) // 2)
    base.paste(plate, pos, plate)
    return base.convert("RGB")


# ══════════════════════════════════════════════════════════════
# 识别
# ══════════════════════════════════════════════════════════════

def decoders_available():
    out = []
    try:
        import cv2  # noqa
        out.append("OpenCV")
    except ImportError:
        pass
    try:
        from pyzbar import pyzbar  # noqa
        out.append("pyzbar")
    except ImportError:
        pass
    return out


def decode_image(data_b64):
    """解一张图里的二维码。两个解码器都试，谁先出结果用谁"""
    from PIL import Image
    raw = data_b64.split(",")[-1]
    try:
        img = Image.open(io.BytesIO(base64.b64decode(raw)))
    except Exception as exc:
        return {"ok": False, "error": f"读不了这张图：{exc}"}
    img = img.convert("RGB")

    results = []
    errors = []

    try:
        import cv2
        import numpy as np
        det = cv2.QRCodeDetector()
        arr = np.array(img.convert("L"))
        txt, pts, _ = det.detectAndDecode(arr)
        if txt:
            results.append({"text": txt, "by": "OpenCV"})
        # 多码：detectAndDecodeMulti
        try:
            ok, texts, _, _ = det.detectAndDecodeMulti(arr)
            if ok:
                for t in texts:
                    if t and not any(r["text"] == t for r in results):
                        results.append({"text": t, "by": "OpenCV(多码)"})
        except Exception:
            pass
    except ImportError:
        errors.append("没装 OpenCV")
    except Exception as exc:
        errors.append(f"OpenCV: {exc}")

    try:
        from pyzbar import pyzbar
        found = pyzbar.decode(img)
        for sym in found:
            try:
                t = sym.data.decode("utf-8")
            except UnicodeDecodeError:
                t = sym.data.decode("utf-8", "replace")
            if not any(r["text"] == t for r in results):
                results.append({"text": t, "by": "pyzbar",
                                "type": getattr(sym, "type", "QRCODE")})
    except ImportError:
        errors.append("没装 pyzbar")
    except Exception as exc:
        errors.append(f"pyzbar: {exc}")

    if not results:
        return {"ok": False, "results": [], "errors": errors,
                "error": "没识别出二维码。" + ("可用的解码器：" + "、".join(decoders_available())
                                              if decoders_available() else
                                              "本机没有可用的解码器（装 opencv-python 或 pyzbar）")}
    return {"ok": True, "results": results, "errors": errors, "size": img.size}


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "qrtool/" + VERSION

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
                "decoders": decoders_available(),
                "max_bytes": qrgen.data_codewords(40, "L"),
                "ecc_levels": [
                    {"id": "L", "name": "L 低（7%）", "use": "内容多、环境干净时用"},
                    {"id": "M", "name": "M 中（15%）", "use": "默认，够用"},
                    {"id": "Q", "name": "Q 较高（25%）", "use": "会脏会磨损时用"},
                    {"id": "H", "name": "H 最高（30%）", "use": "要放 logo，或者印刷品"},
                ],
            }, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/build":
            """只拼内容，不生成图 —— 让用户先看到"实际写进去的是什么\""""
            try:
                content = build_content(b.get("kind") or "text", b.get("fields") or {})
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": str(exc)},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps({"ok": True, "content": content,
                                               "length": len(content),
                                               "bytes": len(content.encode("utf-8"))},
                                              ensure_ascii=False))

        if u.path == "/api/generate":
            t0 = time.time()
            kind = b.get("kind") or "text"
            content = b.get("content")
            if content is None:
                content = build_content(kind, b.get("fields") or {})
            try:
                png, svg, info = make_qr(
                    content,
                    ecc=b.get("ecc") or "M",
                    scale=b.get("scale") or 10,
                    border=b.get("border"),
                    dark=b.get("dark") or "#000000",
                    light=b.get("light") or "#ffffff",
                    logo_b64=b.get("logo"),
                    logo_ratio=b.get("logo_ratio") or 0.22,
                    module_radius=b.get("module_radius") or 0,
                )
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": str(exc)},
                                                  ensure_ascii=False))
            info["seconds"] = round(time.time() - t0, 3)
            info["kind"] = kind
            log(f"[gen] {kind} {info['version']}v {info['ecc']} mask{info['mask']} "
                f"{info['size']}x{info['size']} ({info['bytes']}字节)")
            return self._send(200, json.dumps({
                "ok": True, "info": info,
                "png": ("data:image/png;base64," + base64.b64encode(png).decode()) if png else None,
                "svg": svg,
            }, ensure_ascii=False))

        if u.path == "/api/decode":
            t0 = time.time()
            r = decode_image(b.get("image") or "")
            r["seconds"] = round(time.time() - t0, 3)
            if r.get("ok"):
                log(f"[decode] 认出 {len(r['results'])} 个码")
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/batch":
            lines = [x for x in (b.get("lines") or "").split("\n") if x.strip()]
            if not lines:
                return self._send(400, json.dumps({"ok": False, "error": "没有内容"},
                                                  ensure_ascii=False))
            if len(lines) > 60:
                lines = lines[:60]
            out = []
            for line in lines:
                try:
                    png, svg, info = make_qr(line, ecc=b.get("ecc") or "M",
                                             scale=b.get("scale") or 8,
                                             border=b.get("border"),
                                             dark=b.get("dark") or "#000000",
                                             light=b.get("light") or "#ffffff")
                    out.append({"ok": True, "text": line, "info": info,
                                "png": "data:image/png;base64," + base64.b64encode(png).decode()
                                       if png else None})
                except Exception as exc:
                    out.append({"ok": False, "text": line, "error": str(exc)})
            log(f"[batch] 生成 {len([x for x in out if x['ok']])}/{len(lines)} 个")
            return self._send(200, json.dumps({"ok": True, "items": out},
                                              ensure_ascii=False))

        if u.path == "/api/save":
            """把生成好的图存到桌面（用户点"保存"才写文件）"""
            raw = b.get("png") or ""
            if not raw:
                return self._send(400, json.dumps({"ok": False, "error": "没有图"},
                                                  ensure_ascii=False))
            name = (b.get("name") or "qrcode").strip()
            name = "".join(c for c in name if c not in '/\\:*?"<>|')[:40] or "qrcode"
            folder = b.get("folder") or os.path.expanduser("~/Desktop")
            if not os.path.isdir(folder):
                folder = os.path.expanduser("~/Desktop")
            path = os.path.join(folder, name + ".png")
            i = 2
            while os.path.exists(path):
                path = os.path.join(folder, f"{name}-{i}.png")
                i += 1
            try:
                with open(path, "wb") as f:
                    f.write(base64.b64decode(raw.split(",")[-1]))
                log(f"[save] {path}")
                return self._send(200, json.dumps({"ok": True, "path": path},
                                                  ensure_ascii=False))
            except Exception as exc:
                return self._send(500, json.dumps({"ok": False, "error": str(exc)},
                                                  ensure_ascii=False))

        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            target = p if os.path.isdir(p) else os.path.dirname(p)
            if target and os.path.isdir(target):
                subprocess.Popen(["open", target])
                return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return self._send(404, json.dumps({"ok": False, "error": "目录不存在"},
                                              ensure_ascii=False))

        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))

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
    dec = decoders_available()
    log(f"二维码工具 v{VERSION} 已启动 http://127.0.0.1:{port}"
        + (f"（识别解码器: {'、'.join(dec)}）" if dec else "（没有解码器，只能生成）"))

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
    print(f"✅ 二维码工具已启动（PID {running_pid()}） http://127.0.0.1:{port}"
          if running_pid() else "❌ 启动失败，看 qrtool.log")
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


def cmd_make(args):
    png, svg, info = make_qr(args.text, ecc=args.ecc, scale=args.scale)
    out = args.out or os.path.join(os.getcwd(), "qrcode.png")
    if args.svg or not png:
        out = os.path.splitext(out)[0] + ".svg"
        with open(out, "w", encoding="utf-8") as f:
            f.write(svg)
    else:
        with open(out, "wb") as f:
            f.write(png)
    print(f"✅ {out}")
    print(f"   版本 v{info['version']}　容错 {info['ecc']}　掩码 {info['mask']}　"
          f"{info['size']}×{info['size']} 模块　{info['bytes']} 字节")
    print(f"   编码模式：{info['mode']}"
          + (f"　分段：{qrseg.describe([(s['mode'], 'x' * s['len']) for s in info['segments']])}"
             if info.get("segments") and len(info["segments"]) > 1 else ""))
    return 0


def cmd_read(args):
    with open(args.image, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    r = decode_image("data:image/png;base64," + data)
    if not r.get("ok"):
        print("❌ " + r.get("error", "没识别出二维码"))
        for e in r.get("errors", []):
            print("   " + e)
        return 1
    for i, item in enumerate(r["results"], 1):
        print(f"  [{i}] 由 {item['by']} 识别：")
        for line in item["text"].split("\n"):
            print("      " + line)
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="qrtool", description=f"二维码工具 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("make"); sp.add_argument("text")
    sp.add_argument("--ecc", default="M"); sp.add_argument("--scale", type=int, default=10)
    sp.add_argument("--out"); sp.add_argument("--svg", action="store_true")
    sp.set_defaults(f=cmd_make)
    sp = sub.add_parser("read"); sp.add_argument("image"); sp.set_defaults(f=cmd_read)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
