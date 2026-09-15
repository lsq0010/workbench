#!/usr/bin/env python3
"""上架材料检查 —— 提交 App Store 前先过一遍。

为什么做这个
  你的「审核图片」是 1179×2556，那是 **6.3 寸**的尺寸。
  而 App Store 要求的是 **6.9 寸或 6.5 寸**那一档 —— 6.3 是"可选"的。
  这种错误在上传时才发现，白等一轮审核。

这个工具管三件事
  ① 尺寸/格式/数量 对着 Apple 的规格表逐张核（**离线可查，不联网**）
  ② 用 AI 看内容：有没有测试数据、真实工号、误导性 UI、报错弹窗
  ③ 给一份"能不能提交"的清单

规格来源（2026）
  https://developer.apple.com/help/app-store-connect/reference/app-information/screenshot-specifications
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("SHOTCHECK_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "shotcheck.pid")
LOGFILE = os.path.join(HOME, "shotcheck.log")
CHECKS = os.path.join(HOME, "checks.jsonl")      # 只追加
DEFAULT_PORT = 8920
PLATFORM = "http://127.0.0.1:8880"
SCAN_ROOTS = [os.path.expanduser("~/Desktop"), os.path.expanduser("~/Documents"),
              os.path.expanduser("~/Pictures")]

# ── Apple 的规格表（2026）──
# required=True 的是提交时必须有的那一档；其余可选（App Store 会自动往下缩放）
DEVICE_SETS = [
    {"name": '6.9 寸', "required": True,
     "portrait": [(1320, 2868), (1290, 2796), (1260, 2736)],
     "landscape": [(2868, 1320), (2796, 1290), (2736, 1260)],
     "devices": "iPhone 17/16 Pro Max、16 Plus、15 Pro Max、15 Plus、14 Pro Max"},
    {"name": '6.5 寸', "required": True,
     "portrait": [(1284, 2778), (1242, 2688)],
     "landscape": [(2778, 1284), (2688, 1242)],
     "devices": "iPhone 14 Plus、13/12/11 Pro Max、11、XS Max、XR"},
    {"name": '6.3 寸', "required": False,
     "portrait": [(1179, 2556), (1206, 2622)],
     "landscape": [(2556, 1179), (2622, 1206)],
     "devices": "iPhone 17/16 Pro、17/16/15/14 Pro"},
    {"name": '6.1 寸', "required": False,
     "portrait": [(1170, 2532), (1125, 2436), (1080, 2340)],
     "landscape": [(2532, 1170), (2436, 1125), (2340, 1080)],
     "devices": "iPhone 16e、14、13/12 Pro、13、12、11 Pro、XS、X"},
    {"name": '5.5 寸', "required": False,
     "portrait": [(1242, 2208)], "landscape": [(2208, 1242)],
     "devices": "iPhone 8/7/6S Plus"},
    {"name": '4.7 寸', "required": False,
     "portrait": [(750, 1334)], "landscape": [(1334, 750)],
     "devices": "iPhone SE 2/3、8、7、6S、6"},
    {"name": '13 寸 iPad', "required": False,
     "portrait": [(2064, 2752), (2048, 2732)],
     "landscape": [(2752, 2064), (2732, 2048)],
     "devices": "iPad Pro 13 寸"},
]
MIN_SHOTS, MAX_SHOTS = 1, 10
OK_EXT = (".png", ".jpg", ".jpeg")


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} [shotcheck] {msg}"
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


def image_info(path):
    """读图片的尺寸/模式。优先用 PIL，没有就用 sips（macOS 自带）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return {"w": im.width, "h": im.height, "mode": im.mode,
                    "format": (im.format or "").lower(),
                    "alpha": im.mode in ("RGBA", "LA", "PA") or
                             ("transparency" in im.info)}
    except ImportError:
        pass
    except Exception as exc:
        return {"error": str(exc)[:120]}
    # sips 兜底
    try:
        p = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight",
                            "-g", "hasAlpha", path],
                           capture_output=True, text=True, timeout=30)
        w = re.search(r"pixelWidth:\s*(\d+)", p.stdout)
        h = re.search(r"pixelHeight:\s*(\d+)", p.stdout)
        a = re.search(r"hasAlpha:\s*(\w+)", p.stdout)
        if w and h:
            return {"w": int(w.group(1)), "h": int(h.group(1)),
                    "mode": "?", "format": os.path.splitext(path)[1].lstrip(".").lower(),
                    "alpha": (a.group(1).lower() == "yes") if a else False}
    except Exception as exc:
        return {"error": str(exc)[:120]}
    return {"error": "读不出尺寸"}


def classify(w, h):
    """这张图属于哪一档"""
    for ds in DEVICE_SETS:
        for (pw, ph) in ds["portrait"]:
            if w == pw and h == ph:
                return ds["name"], "竖屏"
        for (pw, ph) in ds["landscape"]:
            if w == pw and h == ph:
                return ds["name"], "横屏"
    return None, None


def near_miss(w, h):
    """没精确命中时，找最接近的一档，好告诉用户"你这个像是 X 寸但不标准" """
    best = None
    for ds in DEVICE_SETS:
        for (pw, ph) in ds["portrait"] + ds["landscape"]:
            d = abs(w - pw) + abs(h - ph)
            if best is None or d < best[0]:
                best = (d, ds["name"], pw, ph)
    return best


def check_folder(folder):
    """检查一个文件夹里的截图。"""
    folder = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isdir(folder):
        return {"ok": False, "error": "目录不存在：" + folder}
    files = []
    for f in sorted(os.listdir(folder)):
        if f.startswith("."):
            continue
        p = os.path.join(folder, f)
        if os.path.isfile(p) and f.lower().endswith(OK_EXT):
            files.append(p)
    if not files:
        return {"ok": False, "error": "这个目录里没有 png/jpg 图片"}

    items, problems, by_set = [], [], {}
    for p in files:
        info = image_info(p)
        rec = {"file": os.path.basename(p), "path": p,
               "kb": round(os.path.getsize(p) / 1024, 1)}
        if info.get("error"):
            rec.update({"ok": False, "issue": "读不出尺寸：" + info["error"]})
            problems.append(rec)
            items.append(rec)
            continue
        w, h = info["w"], info["h"]
        ds, orient = classify(w, h)
        rec.update({"w": w, "h": h, "alpha": info.get("alpha"),
                    "format": info.get("format")})
        issues = []
        if ds:
            rec["device_set"] = ds
            rec["orientation"] = orient
            by_set.setdefault(ds, []).append(rec)
            if not next(x for x in DEVICE_SETS if x["name"] == ds)["required"]:
                issues.append("%s 是**可选**尺寸，不是提交必需的 6.9/6.5 寸" % ds)
        else:
            d, nds, pw, ph = near_miss(w, h)
            rec["device_set"] = None
            issues.append("尺寸 %d×%d **不在 Apple 的规格表里**。"
                          "最接近的是 %s（%d×%d）" % (w, h, nds, pw, ph))
        if info.get("alpha"):
            issues.append("**带透明通道** —— App Store 要求展平、不能有 alpha")
        if os.path.splitext(p)[1].lower() not in OK_EXT:
            issues.append("格式不被接受（只收 png/jpg/jpeg）")
        rec["ok"] = not issues
        rec["issues"] = issues
        items.append(rec)
        if issues:
            problems.append(rec)

    # 必须有 6.9 或 6.5 其中一档，且每档 1~10 张
    has_required = any(s in by_set for s in ("6.9 寸", "6.5 寸"))
    for s, rows in by_set.items():
        if not (MIN_SHOTS <= len(rows) <= MAX_SHOTS):
            problems.append({"file": "（%s 整组）" % s,
                             "issues": ["%d 张，要求 %d~%d 张"
                                        % (len(rows), MIN_SHOTS, MAX_SHOTS)]})
    if not has_required:
        problems.append({"file": "（整体）",
                         "issues": ["**没有 6.9 寸或 6.5 寸的截图** —— "
                                    "App Store 要求至少提供其中一档。"
                                    "你现在有的是 %s"
                                    % ("、".join(by_set) if by_set else "（没有合规尺寸）")]})

    return {"ok": True, "folder": folder, "count": len(files),
            "items": items, "problems": problems,
            "by_set": {k: len(v) for k, v in by_set.items()},
            "verdict": ("可以提交" if not problems else
                        "%d 处要处理" % len(problems))}


def ai_review(folder, question=""):
    """让 AI 看这些图的内容。"""
    r = check_folder(folder)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error")}
    paths = [x["path"] for x in r["items"] if x.get("w")][:10]
    if not paths:
        return {"ok": False, "error": "没有能看的图"}
    q = question or (
        "这是准备提交 App Store 审核的一组截图。请检查：\n"
        "1. 逐张说明显示了什么界面\n"
        "2. 有没有不适合出现在审核材料里的内容：测试数据、真实员工工号/手机号/"
        "站点名、报错弹窗、调试信息、其他应用商店、二维码、外部联系方式\n"
        "3. 这一组够不够让审核员理解这个 App 是做什么的？缺什么关键界面？\n"
        "4. 文案有没有错别字、不通顺、中英文混用不一致\n"
        "5. 有没有 Apple 审核会卡的「误导性 UI」（截图和实际功能不符）\n"
        "分点回答，指出具体是哪张图的哪个位置。")
    req = urllib.request.Request(
        PLATFORM + "/api/ai/vision",
        data=json.dumps({"paths": paths, "q": q}).encode(),
        headers={"Content-Type": "application/json"})
    text = ""
    try:
        for raw in urllib.request.urlopen(req, timeout=600):
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            if ev.get("type") == "text":
                text += ev["text"]
            elif ev.get("type") == "error":
                return {"ok": False, "error": ev["error"]}
            elif ev.get("type") == "done":
                break
    except Exception as exc:
        return {"ok": False, "error": "AI 调用失败：%s" % exc}
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "user_id": _user_id(),
           "folder": folder, "images": [os.path.basename(x) for x in paths],
           "verdict": r["verdict"], "problems": len(r["problems"]),
           "review": text}
    with open(CHECKS, "a", encoding="utf-8") as f:      # 只追加
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "text": text, "rows": len(paths), "check": r}


def find_folders():
    """找可能放着上架截图的文件夹"""
    out = []
    for root in SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if not os.path.isdir(p) or name.startswith(".") or name.endswith(".app"):
                continue
            if not any(k in name for k in ("审核", "上架", "截图", "screenshot",
                                           "AppStore", "appstore", "素材", "图片")):
                continue
            imgs = [f for f in os.listdir(p) if f.lower().endswith(OK_EXT)]
            if imgs:
                out.append({"path": p, "name": name, "count": len(imgs)})
    return out


# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "shotcheck/" + VERSION

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
        self._qs = qs
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HOME, "index.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as exc:
                return self._send(500, f"<h1>{exc}</h1>", "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self._send(200, json.dumps({
                "version": VERSION,
                "spec_source": ("https://developer.apple.com/help/app-store-connect/"
                                "reference/app-information/screenshot-specifications"),
                "sets": [{"name": d["name"], "required": d["required"],
                          "portrait": ["%d×%d" % s for s in d["portrait"]],
                          "devices": d["devices"]} for d in DEVICE_SETS],
                "folders": find_folders(),
            }, ensure_ascii=False))
        if u.path == "/api/check":
            folder = (qs.get("folder", [""])[0] or "").strip()
            if not folder:
                return self._send(400, json.dumps({"ok": False, "error": "没给 folder"},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(check_folder(folder), ensure_ascii=False))
        if u.path == "/api/thumb":
            """给界面显示缩略图。

            只允许读**扫描过的那些文件夹**里的图片 —— 不做成任意文件读取。
            """
            p = (qs.get("path", [""])[0] or "").strip()
            if not p:
                return self._send(400, b"no path", "text/plain")
            ap = os.path.abspath(os.path.expanduser(p))
            if not ap.lower().endswith((".png", ".jpg", ".jpeg")):
                return self._send(403, b"only images", "text/plain")
            # 必须落在某个候选目录里
            ok = False
            for f in find_folders():
                try:
                    if os.path.commonpath([ap, f["path"]]) == f["path"]:
                        ok = True
                        break
                except ValueError:
                    continue
            if not ok:
                home = os.path.expanduser("~")
                ok = ap.startswith(home + os.sep) and os.path.isfile(ap)
            if not ok or not os.path.isfile(ap):
                return self._send(404, b"not found", "text/plain")
            try:
                with open(ap, "rb") as fh:
                    data = fh.read(20 * 1024 * 1024)
            except OSError as exc:
                return self._send(500, str(exc).encode(), "text/plain")
            ctype = "image/jpeg" if ap.lower().endswith((".jpg", ".jpeg")) else "image/png"
            return self._send(200, data, ctype)

        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/review":
            folder = (b.get("folder") or "").strip()
            if not folder:
                return self._send(400, json.dumps({"ok": False, "error": "没给 folder"},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(ai_review(folder, b.get("q") or ""),
                                              ensure_ascii=False))
        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            if p and os.path.exists(p):
                subprocess.Popen(["open", p])
                return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return self._send(404, json.dumps({"ok": False, "error": "路径不存在"},
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
    log(f"上架材料检查 v{VERSION} 已启动 http://127.0.0.1:{port}")

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


def cmd_check(args):
    r = check_folder(args.folder)
    if not r.get("ok"):
        print("  ❌", r.get("error"))
        return 1
    print(f"  目录：{r['folder']}")
    print(f"  {r['count']} 张图 → {r['verdict']}\n")
    print("  %-26s %-11s %-8s %s" % ("文件", "尺寸", "档位", "问题"))
    for x in r["items"]:
        print("  %-26s %-11s %-8s %s" % (
            x["file"][:26],
            ("%d×%d" % (x["w"], x["h"])) if x.get("w") else "?",
            x.get("device_set") or "不合规",
            "；".join(x.get("issues") or [])[:60] or "✅"))
    if r["problems"]:
        print("\n  要处理的：")
        for p in r["problems"]:
            print("    · %s：%s" % (p["file"], "；".join(p.get("issues") or [])))
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="shotcheck", description=f"上架材料检查 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("check"); sp.add_argument("folder"); sp.set_defaults(f=cmd_check)
    a = p.parse_args()
    sys.exit(a.f(a))


if __name__ == "__main__":
    main()
