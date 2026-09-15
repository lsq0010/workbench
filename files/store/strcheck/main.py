#!/usr/bin/env python3
"""文案体检 —— 找出代码里"该翻译却没翻译"的硬编码文案。

为什么做这个
  你的工程支持 19 种语言，但代码里散着一堆硬编码中文。
  真实例子（`EnvironmentManager.swift`）：

      alert.title = "切换环境"
      alert.message = "自定义环境"
      action.title = "取消" / "确定"
      field.placeholder = "请输入host"

  这些**不在 .strings 里**，所以阿语/俄语/西语用户看到的是中文。

判断逻辑（关键在这里，不能只看"有没有中文"）
  · `print(...)` / 日志里的中文 → **不算问题**（开发看的）
  · 已经是本地化 key 的 → 不算问题
  · 生成文件（`R.generated.swift`）→ 跳过（那是 SwiftGen 的默认值，正常）
  · 只有**用户能看到的位置**（alert 标题/按钮、placeholder、label.text…）
    才算"漏翻"
"""
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

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("STRCHECK_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "strcheck.pid")
LOGFILE = os.path.join(HOME, "strcheck.log")
SCANS = os.path.join(HOME, "scans.jsonl")        # 只追加
DEFAULT_PORT = 8923
PLATFORM = "http://127.0.0.1:8880"

# 生成的文件不查（里面的中文是本地化默认值，是"正确"的）
GENERATED = ("R.generated.swift", "Localizable.generated.swift",
             "Assets.generated.swift", "Storyboards.generated.swift")
SKIP_DIRS = ("Pods", "build", ".git", "DerivedData", ".build", "Carthage",
             "node_modules", ".venv", "__pycache__")

# 用户能看到的位置 —— 只有这些才算"漏翻"
UI_HINTS = ("UIAlertController", "UIAlertAction", "alert", "Alert",
            "title:", "message:", "placeholder", ".text =", ".text=",
            "setTitle", "UILabel(", "UIButton(", "label.text", "btn.set",
            "attributedText", "textField", "searchBar", "navigationItem",
            "tabBarItem", "Toast", "toast", "HUD", "showMessage", "showTip",
            "MBProgressHUD", "SVProgressHUD", "UIActionSheet", "confirm",
            "UISegmentedControl", "UIBarButtonItem", "header", "footer")
# 开发看的 —— 这些里的中文不算问题
DEV_HINTS = ("print(", "NSLog(", "debugPrint(", "assert(", "fatalError(",
             "DevLog.", "log(", "DDLog", "os_log", "Logger.", "XCGLogger",
             "// ", "///", "/*", "* ")


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} [strcheck] {msg}"
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


CJK = re.compile(r"[\u4e00-\u9fff]")
STR_LIT = re.compile(r'"((?:[^"\\]|\\.)*)"')


def load_strings(project):
    """读所有 .strings 文件，收集已有的 key 和值。

    用来判断一条中文是不是"已经在本地化表里"。
    """
    keys, values = set(), set()
    for cur, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if not f.endswith((".strings", ".stringsdict")):
                continue
            p = os.path.join(cur, f)
            try:
                txt = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            # .strings 有两种写法 —— **key 带引号的和不带引号的**：
            #   "some.key" = "值";              ← Localizable.strings 常见
            #   CFBundleDisplayName = "值";     ← InfoPlist.strings 常见
            # 一开始只认带引号的，结果 InfoPlist.strings 里的值全没被收进来，
            # 那些文案就被误报成"漏翻"。两种都要认。
            for m in re.finditer(
                    r'(?:"((?:[^"\\]|\\.)*)"|([A-Za-z_][\w\.]*))\s*=\s*'
                    r'"((?:[^"\\]|\\.)*)"\s*;', txt):
                k = m.group(1) if m.group(1) is not None else m.group(2)
                if k:
                    keys.add(k)
                values.add(m.group(3))
    return keys, values


def scan(project, max_files=4000):
    """扫一个 iOS 工程，找漏翻的硬编码文案。只读。"""
    project = os.path.abspath(os.path.expanduser(project))
    if not os.path.isdir(project):
        return {"ok": False, "error": "目录不存在：" + project}
    t0 = time.time()
    keys, values = load_strings(project)
    log(f"本地化表：{len(keys)} 个 key / {len(values)} 个值")

    n_files = 0
    ui_hits, dev_hits, skipped_gen = [], [], 0
    by_file = {}
    for cur, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in sorted(files):
            if not f.endswith((".swift", ".m", ".mm")):
                continue
            if f in GENERATED:
                skipped_gen += 1
                continue
            p = os.path.join(cur, f)
            n_files += 1
            if n_files > max_files:
                break
            try:
                lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                continue
            rel = os.path.relpath(p, project)
            for ln, line in enumerate(lines, 1):
                st = line.strip()
                # 整行注释跳过
                if st.startswith(("//", "*", "/*")):
                    continue
                lits = [m.group(1) for m in STR_LIT.finditer(line)
                        if CJK.search(m.group(1))]
                if not lits:
                    continue
                # 去掉行尾注释里可能的中文（简单处理：注释位置之后的不算）
                is_dev = any(h in line for h in DEV_HINTS)
                for s in lits:
                    if len(s.strip()) < 2:
                        continue
                    # 已经是本地化 key/值 → 不算问题
                    if s in keys or s in values:
                        continue
                    rec = {"file": rel, "line": ln, "text": s[:80],
                           "code": line.strip()[:160], "dev": is_dev}
                    if is_dev:
                        dev_hits.append(rec)
                    else:
                        ui_hits.append(rec)
                        by_file[rel] = by_file.get(rel, 0) + 1

    # 按文件聚合，排个优先级
    top = sorted(by_file.items(), key=lambda x: -x[1])[:20]
    result = {
        "ok": True, "project": project, "files_scanned": n_files,
        "skipped_generated": skipped_gen,
        "strings_keys": len(keys),
        "ui_hits": ui_hits[:400], "ui_count": len(ui_hits),
        "dev_count": len(dev_hits),
        "by_file": [{"file": f, "count": n} for f, n in top],
        "seconds": round(time.time() - t0, 1),
        "verdict": ("没发现漏翻的硬编码文案" if not ui_hits else
                    "%d 处用户可见的硬编码文案没进本地化表" % len(ui_hits)),
        "how": ("只统计**用户能看到的位置**（alert 标题/按钮、placeholder、"
                "label.text…）。`print()` 和日志里的中文不算 —— 那是给开发看的。"
                "已经在 .strings 里的也不算。"),
    }
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "user_id": _user_id(),
           "project": project, "ui": len(ui_hits), "dev": len(dev_hits),
           "files": n_files}
    try:
        with open(SCANS, "a", encoding="utf-8") as f:      # 只追加
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return result


def ai_review(result, question=""):
    """让 AI 判断这些硬编码哪些真的要改。"""
    hits = result.get("ui_hits") or []
    if not hits:
        return {"ok": False, "error": "没有要分析的"}
    lines = []
    for h in hits[:60]:
        lines.append("%s:%d  %s" % (h["file"], h["line"], h["code"]))
    q = question or (
        "这是 iOS 工程里**没有进本地化表**的硬编码中文（都在用户可见的位置）。"
        "这个 App 支持 19 种语言。请判断：\n"
        "1. 哪些是**真的要改**的（用户会看到的）？哪些其实无所谓"
        "（比如只在国内用的调试入口、或者本来就只有中文的模块）？\n"
        "2. 有没有哪几处其实是**同一个文案**，可以合并成一个 key？\n"
        "3. 按「改了收益最大」排序，给出前 5 个要改的位置\n"
        "4. 给出具体改法（用什么 key 名、怎么加进 .strings）\n"
        "分点，简洁，中文。")
    req = urllib.request.Request(
        PLATFORM + "/api/ai/chat",
        data=json.dumps({"q": q + "\n\n素材：\n" + "\n".join(lines)[:9000]}).encode(),
        headers={"Content-Type": "application/json"})
    out = ""
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
                out += ev["text"]
            elif ev.get("type") == "done":
                break
    except Exception as exc:
        return {"ok": False, "error": "AI 调用失败：%s" % exc}
    return {"ok": True, "text": out}


def find_projects(roots=None):
    """找候选的 iOS 工程"""
    roots = roots or [os.path.expanduser("~/Desktop"), os.path.expanduser("~/Documents")]
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if not os.path.isdir(p) or name.startswith(".") or name.endswith(".app"):
                continue
            swifts = 0
            for cur, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                swifts += sum(1 for f in files if f.endswith(".swift"))
                if swifts > 40:
                    break
            if swifts > 20:
                out.append({"path": p, "name": name, "swift": swifts})
    return out


# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "strcheck/" + VERSION

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
                "version": VERSION, "projects": find_projects(),
            }, ensure_ascii=False))
        if u.path == "/api/scan":
            p = (qs.get("project", [""])[0] or "").strip()
            if not p:
                return self._send(400, json.dumps({"ok": False, "error": "没给 project"},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(scan(p), ensure_ascii=False))
        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/ai":
            r = scan(b.get("project") or "")
            if not r.get("ok"):
                return self._send(200, json.dumps(r, ensure_ascii=False))
            a = ai_review(r, b.get("q") or "")
            a["scan"] = {"ui_count": r["ui_count"], "dev_count": r["dev_count"]}
            return self._send(200, json.dumps(a, ensure_ascii=False))
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
    log(f"文案体检 v{VERSION} 已启动 http://127.0.0.1:{port}")

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


def cmd_scan(args):
    r = scan(args.project)
    if not r.get("ok"):
        print("  ❌", r.get("error"))
        return 1
    print(f"  扫了 {r['files_scanned']} 个源文件（跳过 {r['skipped_generated']} 个生成文件）")
    print(f"  本地化表里 {r['strings_keys']} 个 key")
    print(f"  用户可见的硬编码文案：{r['ui_count']} 处")
    print(f"  调试日志里的中文（不算问题）：{r['dev_count']} 处")
    print()
    if r["by_file"]:
        print("  按文件排：")
        for x in r["by_file"][:12]:
            print("    %-56s %d 处" % (x["file"][:56], x["count"]))
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="strcheck", description=f"文案体检 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("scan"); sp.add_argument("project"); sp.set_defaults(f=cmd_scan)
    a = p.parse_args()
    sys.exit(a.f(a))


if __name__ == "__main__":
    main()
