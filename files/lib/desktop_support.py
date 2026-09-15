"""桌面快捷入口：识别位置、读取网页标题、调用系统选择窗口。"""
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from html.parser import HTMLParser

_picker_lock = threading.Lock()


def resolve_target(target, kind="auto"):
    target = str(target or "").strip()
    if len(target) > 8192 or not target or any(ord(c) < 32 for c in target):
        raise ValueError("请粘贴一个有效的网址或文件路径")
    if len(target) > 1 and target[0] == target[-1] and target[0] in "\"'":
        target = target[1:-1]
    if target.lower().startswith("file://"):
        u = urllib.parse.urlsplit(target)
        if u.netloc not in ("", "localhost"):
            raise ValueError("请选择本机文件")
        target = urllib.request.url2pathname(u.path)
    local = os.path.expanduser(target)
    is_path = os.path.isabs(local) or bool(re.match(r"^[A-Za-z]:[\\/]", local))
    if is_path:
        local = os.path.normpath(local)
        if not os.path.exists(local):
            raise ValueError("找不到这个位置，请重新选择文件或文件夹")
        app = local.lower().endswith((".app", ".exe", ".lnk", ".desktop"))
        kind = "app" if app else ("folder" if os.path.isdir(local) else "file")
        name = os.path.basename(local) or local
        if kind == "app":
            name = os.path.splitext(name)[0]
        return {"kind": kind, "target": local, "name": name}
    if kind in ("folder", "file", "app"):
        raise ValueError("请选择文件，或粘贴完整路径")
    if "://" not in target:
        target = "https://" + target
    try:
        u = urllib.parse.urlsplit(target)
        if (u.scheme.lower() not in ("http", "https") or not u.hostname or
                u.username is not None or u.password is not None or
                any(c.isspace() for c in target)):
            raise ValueError()
        if "." not in u.hostname and u.hostname != "localhost" and ":" not in u.hostname:
            raise ValueError()
        u.port  # 同时校验端口。
        hostname = u.hostname.encode("idna").decode("ascii")
        host = "[" + hostname + "]" if ":" in hostname else hostname
        if u.port is not None:
            host += ":" + str(u.port)
        url = urllib.parse.urlunsplit((u.scheme.lower(), host,
            urllib.parse.quote(u.path or "/", safe="/%:@!$&'()*+,;=-._~"),
            urllib.parse.quote(u.query, safe="%=&?/:@!$'()*+,;-._~"),
            urllib.parse.quote(u.fragment, safe="%/?=:&-._~")))
        return {"kind": "url", "target": url, "name": u.hostname.removeprefix("www.")
                if hasattr(str, "removeprefix") else re.sub(r"^www\.", "", u.hostname)}
    except (ValueError, UnicodeError):
        raise ValueError("请输入有效网址，例如 https://example.com")


class _TitleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.inside = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def preview_target(target):
    item = resolve_target(target)
    if item["kind"] == "url":
        # 标题只是辅助；网站慢、离线或拒绝访问时照样能添加入口。
        try:
            req = urllib.request.Request(item["target"], headers={"User-Agent": "Workbench/0.1"})
            with urllib.request.urlopen(req, timeout=3) as r:
                if "html" in r.headers.get("Content-Type", "").lower():
                    raw = r.read(65536)
                    encoding = r.headers.get_content_charset()
                    if not encoding:
                        meta = re.search(br'charset\s*=\s*["\x27]?([\w-]+)', raw[:4096], re.I)
                        encoding = meta.group(1).decode("ascii") if meta else "utf-8"
                    p = _TitleParser()
                    p.feed(raw.decode(encoding, "replace"))
                    title = " ".join("".join(p.parts).split())[:120]
                    if title:
                        item["name"] = title
        except Exception:
            pass
    return item


def native_pick(kind):
    if kind not in ("folder", "file", "app"):
        return {"ok": False, "message": "请选择文件夹、文件或应用"}
    if not _picker_lock.acquire(blocking=False):
        return {"ok": False, "message": "选择窗口已经打开，请先完成选择"}
    try:
        if sys.platform == "darwin":
            method = "chooseFolder" if kind == "folder" else "chooseFile"
            options = {"withPrompt": "选择要添加到工作平台的" +
                       {"folder": "文件夹", "file": "文件", "app": "应用"}[kind],
                       "multipleSelectionsAllowed": True}
            if kind == "app":
                options["ofType"] = ["com.apple.application-bundle"]
            script = """var app = Application.currentApplication();
app.includeStandardAdditions = true;
try {
  app.activate();
  var paths = app.METHOD(OPTIONS);
  JSON.stringify({ok:true, paths:paths.map(function(p){return p.toString();})});
} catch(e) {
  JSON.stringify(e.errorNumber === -128 ? {ok:true,cancelled:true,paths:[]} :
    {ok:false,message:String(e)});
}""".replace("METHOD", method).replace("OPTIONS", json.dumps(options, ensure_ascii=False))
            cmd = ["osascript", "-l", "JavaScript", "-e", script]
        else:
            # Tk 必须在独立进程的主线程上运行，不能在 HTTP 工作线程中开窗口。
            script = """import json, sys
try:
 import tkinter as tk
 from tkinter import filedialog
 root=tk.Tk(); root.withdraw(); root.attributes('-topmost',True)
 paths=([filedialog.askdirectory(title='选择文件夹')] if sys.argv[1]=='folder'
        else list(filedialog.askopenfilenames(title='选择文件或应用')))
 root.destroy()
 paths=[p for p in paths if p]
 print(json.dumps({'ok':True,'paths':paths,'cancelled':not paths}))
except Exception:
 print(json.dumps({'ok':False,'message':'系统选择窗口不可用，请粘贴完整路径'}))
"""
            cmd = [sys.executable, "-c", script, kind]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode:
            return {"ok": False, "message": "系统选择窗口未能打开，请粘贴完整路径"}
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "选择窗口已超时，请重新打开"}
    except Exception:
        return {"ok": False, "message": "系统选择窗口不可用，请粘贴完整路径"}
    finally:
        _picker_lock.release()


def local_apps():
    roots = (["/Applications", os.path.expanduser("~/Applications"), "/System/Applications"]
             if sys.platform == "darwin" else
             [os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
              os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs")]
             if sys.platform == "win32" else
             ["/usr/share/applications", os.path.expanduser("~/.local/share/applications")])
    result = []
    seen = set()
    for root in roots:
        for current, dirs, files in os.walk(root):
            candidates = [os.path.join(current, n) for n in dirs + files
                          if n.lower().endswith((".app", ".lnk", ".desktop"))]
            dirs[:] = [n for n in dirs if not n.startswith(".") and not n.endswith(".app")]
            if os.path.relpath(current, root).count(os.sep) >= 2:
                dirs[:] = []
            for path in candidates:
                real = os.path.realpath(path)
                if real not in seen:
                    seen.add(real)
                    result.append({"kind": "app", "target": path,
                                   "name": os.path.splitext(os.path.basename(path))[0]})
    return sorted(result, key=lambda a: a["name"].casefold())
