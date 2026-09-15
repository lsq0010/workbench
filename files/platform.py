#!/usr/bin/env python3
"""工作平台 —— 本地个人工作平台的主机。

它做什么
  1. 桌面：把已装的功能铺成一屏网格，点一下就打开
  2. 商店：本地功能目录，一键安装 / 卸载
  3. 管家：按每个功能 manifest.json 里的声明做健康探测、启动、停止

设计取舍
  · 功能可以被注册在**任意路径**（registry.json 记路径），不强制搬进 features/。
    这样"已经能跑的功能"不用为了整齐而搬家 —— 搬家是风险，不是收益。
  · 一切以 manifest.json 为准：功能自己声明端口、打开方式、启停命令、权限。
    平台不猜、不硬编码任何功能的名字。
  · 零第三方依赖，只用标准库（和抓包工具一致），便于以后打包。

约定（新功能必须遵守）
  features/<id>/manifest.json 里要有 platform 段：
    "platform": {
      "icon": "📡", "color": "#2f6df6",
      "open":  "http://127.0.0.1:{web}/",
      "health":"http://127.0.0.1:{web}/api/status",
      "start": ["python3", "main.py", "start"],
      "stop":  ["python3", "main.py", "stop"]
    }
  占位符 {web} {proxy} {lan_web} 会从 ports 段取值。
"""
import hashlib
import json
import os
import re
import shutil
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
HOME = os.path.abspath(os.environ.get("PLATFORM_HOME") or SCRIPT_DIR)
REGISTRY = os.path.join(HOME, "registry.json")
STOREFILE = os.path.join(HOME, "store", "index.json")
FEATURES_DIR = os.path.join(HOME, "features")
LOGFILE = os.path.join(HOME, "platform.log")
PIDFILE = os.path.join(HOME, "platform.pid")
DEFAULT_PORT = 8880


# ══════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════

_lock = threading.Lock()


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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def port_open(port, host="127.0.0.1", timeout=0.4):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def http_ok(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════
# 注册表：功能装在哪
# ══════════════════════════════════════════════════════════════

def load_registry():
    d = read_json(REGISTRY, {"version": 1, "features": []})
    d.setdefault("features", [])
    return d


BACKUP_DIR = os.path.join(HOME, "backups")
DESKTOP_FILE = os.path.join(HOME, "desktop.json")

# 桌面项的类型 —— 用户能自己加的东西
ITEM_KINDS = {
    "folder": {"name": "文件夹", "icon": "📁", "color": "#3b82f6"},
    "file":   {"name": "文件",   "icon": "📄", "color": "#64748b"},
    "app":    {"name": "应用",   "icon": "🚀", "color": "#8b5cf6"},
    "url":    {"name": "网址",   "icon": "🔗", "color": "#0ea5e9"},
    "note":   {"name": "便签",   "icon": "📌", "color": "#f59e0b"},
}


def load_desktop():
    d = read_json(DESKTOP_FILE, {})
    d.setdefault("items", [])
    d.setdefault("order", [])
    return d


def save_desktop(d):
    write_json(DESKTOP_FILE, d)


_digest_desktop_cache = {"t": 0, "v": None}


def _digest_cached_for_desktop(max_age=30):
    """给桌面项用的轻量读法 —— 直接读缓存，不触发重新汇总。

    桌面每刷新一次就调一次，绝不能在这里现算（那会去问所有功能）。
    没缓存就返回空，等后台汇总完自然就有了。
    """
    global _digest_desktop_cache
    now = time.time()
    if _digest_desktop_cache["v"] is not None and \
            now - _digest_desktop_cache["t"] < max_age:
        return _digest_desktop_cache["v"]
    try:
        # 读平台自己的 digest 缓存（不触发重新汇总）
        c = _digest_cache.get("v")
        if c:
            _digest_desktop_cache = {"t": now, "v": c}
            return c
    except Exception:
        pass
    return {"count": 0, "critical": 0}


def desktop_items():
    """桌面上该显示什么：注册的功能 + 用户自己加的，按各自顺序排"""
    d = load_desktop()
    feats = all_features()
    prefs = load_prefs()
    pinned = prefs.get("pinned") or []

    items = []
    # 「今日关注」作为一个内置工具排在最前 —— 它不是装出来的功能，
    # 而是平台把各功能的汇报汇总出来的一个视图。
    # 做成桌面项是为了和别的工具一致：能看见、能点、有角标。
    try:
        dig = _digest_cached_for_desktop()
        items.append({
            "id": "__digest__", "kind": "digest", "name": "今日关注",
            "target": "", "icon": "📋", "color": "#7c3aed", "custom": False,
            "builtin": True,
            "badge": dig.get("count") or 0,
            "critical": dig.get("critical") or 0,
            "note": "平台从各功能汇总 —— 点开看详情",
            "running": True, "broken": None,
            "description": "各功能报上来的、需要你处理的事都汇总在这。",
        })
    except Exception as exc:
        log(f"[digest] 桌面项生成失败：{exc}")

    # 用户自己加的
    for it in d["items"]:
        items.append({
            "id": it["id"], "kind": it.get("kind", "folder"),
            "name": it.get("name") or os.path.basename(it.get("target", "")),
            "target": it.get("target", ""), "icon": it.get("icon") or
            ITEM_KINDS.get(it.get("kind", "folder"), {}).get("icon", "📁"),
            "color": it.get("color") or
            ITEM_KINDS.get(it.get("kind", "folder"), {}).get("color", "#3b82f6"),
            "note": it.get("note", ""), "custom": True,
            "exists": (os.path.exists(it.get("target", ""))
                       if it.get("kind") in ("folder", "file", "app") else True),
        })
    # 注册的功能
    for f in feats:
        st = feature_status(f)
        items.append({
            "id": f["id"], "kind": "feature", "name": f["name"],
            "target": f.get("open_url") or "", "icon": f.get("icon") or "🧩",
            "color": f.get("color") or "#2f6df6", "custom": False,
            "running": st["running"], "broken": f.get("broken"),
            "version": f.get("version"), "description": f.get("description"),
            "ports": f.get("ports"), "open_url": f.get("open_url"),
            "has_start": f.get("has_start"), "has_stop": f.get("has_stop"),
            "pinned": f["id"] in pinned, "status": st,
        })

    # 排序：用户排的 order 优先，其余按 置顶 → 最近 → 名字
    order = d.get("order") or []
    idx = {x: i for i, x in enumerate(order)}

    def key(it):
        # 「今日关注」永远排第一 —— 它是"今天该看什么"的入口，
        # 不该被用户排的顺序或字母序挤到后面去。
        if it.get("kind") == "digest":
            return (-1, 0, "")
        if it["id"] in idx:
            return (0, idx[it["id"]], "")
        if it["kind"] == "feature":
            return (1, 0 if it.get("pinned") else 1, it["name"])
        return (0, 1000 + items.index(it), "")
    return sorted(items, key=key)


def add_desktop_item(kind, target, name=None, icon=None, color=None, note=""):
    d = load_desktop()
    kind = kind if kind in ITEM_KINDS else "folder"
    target = (target or "").strip()
    if not target:
        return False, "没给路径或网址", None
    if kind in ("folder", "file", "app"):
        target = os.path.expanduser(target)
        if not os.path.exists(target):
            return False, "路径不存在：%s" % target, None
        if kind == "app" and not target.endswith(".app"):
            return False, "这不是 .app：%s" % target, None
        if kind == "folder" and not os.path.isdir(target):
            return False, "这不是文件夹：%s" % target, None
    for it in d["items"]:
        if it.get("target") == target:
            return False, "这个已经在桌面上了", it
    fid = "d_" + hashlib.md5((kind + "|" + target).encode()).hexdigest()[:10]
    item = {"id": fid, "kind": kind, "target": target,
            "name": (name or "").strip() or
                    (target.rstrip("/").split("/")[-1] or target),
            "icon": icon or ITEM_KINDS[kind]["icon"],
            "color": color or ITEM_KINDS[kind]["color"],
            "note": note, "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    d["items"].append(item)
    if fid not in d["order"]:
        d["order"].insert(0, fid)          # 新加的排最前
    save_desktop(d)
    log(f"[desktop] 加了 {kind} {item['name']} → {target}")
    return True, "已加到桌面", item


def remove_desktop_item(fid):
    d = load_desktop()
    n = len(d["items"])
    d["items"] = [x for x in d["items"] if x["id"] != fid]
    d["order"] = [x for x in d["order"] if x != fid]
    save_desktop(d)
    if len(d["items"]) == n:
        return False, "桌面上没有这一项（功能卡片不能删，只能卸载）"
    log(f"[desktop] 移除了 {fid}")
    return True, "已从桌面移除"


def reorder_desktop(order):
    d = load_desktop()
    d["order"] = [x for x in (order or []) if isinstance(x, str)]
    save_desktop(d)
    return True, "顺序已保存"


def open_desktop_item(fid):
    """打开桌面上的一个自定义项"""
    d = load_desktop()
    it = next((x for x in d["items"] if x["id"] == fid), None)
    if not it:
        return False, "没有这一项", ""
    kind, target = it.get("kind"), it.get("target", "")
    try:
        if kind == "url":
            subprocess.Popen(["open", target])
        elif kind in ("folder", "file", "app"):
            if not os.path.exists(target):
                return False, "路径不在了：%s" % target, ""
            subprocess.Popen(["open", target])
        else:
            return False, "不认识的类型：%s" % kind, ""
        return True, "已打开", target
    except Exception as exc:
        return False, str(exc), target


def ignored_ids():
    """用户明确卸载过的功能 id —— 自动扫描要跳过，否则会自己装回来"""
    return set(load_registry().get("ignored") or [])


def set_ignored(fid, on=True):
    """把某个功能加进/移出「已卸载」名单"""
    reg = load_registry()
    cur = set(reg.get("ignored") or [])
    if on:
        cur.add(fid)
    else:
        cur.discard(fid)
    reg["ignored"] = sorted(cur)
    save_registry(reg)


def save_registry(d):
    write_json(REGISTRY, d)


def register(path, enabled=True):
    """把一个功能目录注册进来（已注册则更新路径）"""
    path = os.path.abspath(path)
    man = read_json(os.path.join(path, "manifest.json"))
    fid = man.get("id")
    if not fid:
        return None, "manifest.json 里没有 id"
    # 用户主动装回来 → 从「已卸载」名单移除，否则自动扫描还会跳过它
    try:
        if fid in ignored_ids():
            set_ignored(fid, False)
    except Exception:
        pass
    with _lock:
        reg = load_registry()
        for f in reg["features"]:
            if f["id"] == fid:
                f["path"] = path
                f["enabled"] = enabled
                save_registry(reg)
                return fid, "已更新"
        reg["features"].append({"id": fid, "path": path, "enabled": enabled,
                                "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        save_registry(reg)
    return fid, "已注册"


def unregister(fid, remember=True):
    """从注册表移除。remember=True 时记进 ignored 名单 ——
    不记的话下一轮自动扫描会把目录又注册回来，卸载就等于没卸。"""
    with _lock:
        reg = load_registry()
        n = len(reg["features"])
        reg["features"] = [f for f in reg["features"] if f["id"] != fid]
        if remember:
            cur = set(reg.get("ignored") or [])
            cur.add(fid)
            reg["ignored"] = sorted(cur)
        save_registry(reg)
        return n != len(reg["features"])


def scan_local_features():
    """扫 features/ 目录（还没注册的会被自动注册）"""
    out = []
    if not os.path.isdir(FEATURES_DIR):
        return out
    skip = ignored_ids()
    for name in sorted(os.listdir(FEATURES_DIR)):
        p = os.path.join(FEATURES_DIR, name)
        if not os.path.isfile(os.path.join(p, "manifest.json")):
            continue
        man = read_json(os.path.join(p, "manifest.json")) or {}
        fid = man.get("id") or name
        if fid in skip:
            continue          # 用户明确卸载过，不自动装回来
        out.append(p)
    return out


# ══════════════════════════════════════════════════════════════
# 功能：读声明、探测状态、启停
# ══════════════════════════════════════════════════════════════

def expand(tmpl, ports):
    """把 {web} 之类占位符换成端口"""
    if not tmpl:
        return ""
    s = str(tmpl)
    for k, v in (ports or {}).items():
        s = s.replace("{%s}" % k, str(v))
    return s


def describe(entry):
    """把一个注册项 + 它的 manifest 整理成平台能用的结构"""
    path = entry["path"]
    man = read_json(os.path.join(path, "manifest.json"))
    if not man:
        return {"id": entry["id"], "path": path, "enabled": entry.get("enabled", True),
                "broken": "找不到或读不了 manifest.json", "name": entry["id"]}
    pf = man.get("platform") or {}
    ports = man.get("ports") or {}
    open_url = expand(pf.get("open"), ports)
    if not open_url and ports.get("web"):
        open_url = "http://127.0.0.1:%s/" % ports["web"]
    if not open_url and man.get("type") == "static" and man.get("ui", {}).get("entry"):
        open_url = "/view/%s" % man["id"]        # 纯静态功能由平台代管
    health = expand(pf.get("health"), ports) or open_url
    return {
        "id": man.get("id", entry["id"]),
        "name": man.get("name", entry["id"]),
        "version": man.get("version", "?"),
        "description": (man.get("description") or "").strip(),
        "author": man.get("author", ""),
        "type": man.get("type", "local"),
        "path": path,
        "enabled": entry.get("enabled", True),
        "icon": pf.get("icon") or "🧩",
        "color": pf.get("color") or "#2f6df6",
        "ports": ports,
        "open_url": open_url,
        "health_url": health,
        "perms": man.get("permissions") or [],
        "has_start": bool(pf.get("start")),
        "has_stop": bool(pf.get("stop")),
        "produces": [d.get("file") for d in (man.get("data", {}) or {}).get("produces", [])],
        "updated": man.get("version"),
    }


PREFFILE = os.path.join(HOME, "prefs.json")


def load_prefs():
    d = read_json(PREFFILE, {})
    d.setdefault("pinned", [])
    d.setdefault("recent", [])       # [{id, ts}]
    d.setdefault("hidden", [])
    return d


def save_prefs(d):
    write_json(PREFFILE, d)


def touch_recent(fid):
    """记一次"刚打开过"，用于首页排序"""
    with _lock:
        d = load_prefs()
        d["recent"] = [x for x in d["recent"] if x.get("id") != fid]
        d["recent"].insert(0, {"id": fid, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        d["recent"] = d["recent"][:12]
        save_prefs(d)


def ask_feature(f, path, timeout=6):
    """问一个功能要数据。它的 manifest 里声明了 open 地址，这里只用它的 host"""
    base = f.get("open_url") or ""
    m = re.match(r"(https?://[^/]+)", base)
    if not m:
        return None
    try:
        with urllib.request.urlopen(m.group(1) + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


_digest_cache = {"t": 0, "v": None}


def collect_digest(max_age=90):
    """汇总所有功能汇报的"需要注意的事"。

    约定：功能实现 GET /api/digest，返回
      {"items": [{"level": "critical|warn|info", "title": "...", "detail": "...",
                  "action": "打开哪个页面处理"}]}
    没实现的功能直接跳过 —— 不强求。
    """
    if _digest_cache["v"] is not None and time.time() - _digest_cache["t"] < max_age:
        return _digest_cache["v"]

    feats = [f for f in all_features() if f.get("enabled") and not f.get("broken")]
    items, checked, supported = [], 0, 0

    slow = []

    def one(f):
        st = feature_status(f)
        if not st["running"]:
            return None
        t0 = time.time()
        d = ask_feature(f, "/api/digest", timeout=10)
        return f, d, round(time.time() - t0, 2)

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(one, feats):
            checked += 1
            if not r:
                continue
            f, d, secs = r
            if not isinstance(d, dict) or "items" not in d:
                if secs >= 9.5:
                    slow.append(f["name"])
                continue
            supported += 1
            if secs >= 3:
                slow.append(f"{f['name']}（{secs}s）")
            for it in (d.get("items") or [])[:12]:
                items.append({
                    "feature": f["id"], "feature_name": f["name"],
                    "icon": f.get("icon"), "color": f.get("color"),
                    "level": it.get("level") or "info",
                    "title": it.get("title") or "",
                    "detail": it.get("detail") or "",
                    "action": it.get("action") or "",
                })

    # 把 AI 值守的发现也并进来 —— 它主动看出来的问题，用户应该第一眼看到
    try:
        w = _ai_watch()
        if w:
            seen = set()
            for f in w.findings(6):
                if f.get("key") in seen:
                    continue
                seen.add(f.get("key"))
                items.append({
                    "feature": "ai_watch", "feature_name": "AI 值守",
                    "icon": "🤖", "color": "#7c3aed",
                    "level": f.get("level") or "warn",
                    "title": "AI 主动发现：" + str(f.get("title") or ""),
                    "detail": (str(f.get("analysis") or f.get("note") or "")
                               .split("\n")[0][:150]),
                    "action": "点开看 AI 的完整分析",
                    "ai_analysis": f.get("analysis") or "",
                })
    except Exception as exc:
        log(f"[watch] 合并发现失败：{exc}")

    order = {"critical": 0, "warn": 1, "info": 2}
    items.sort(key=lambda x: order.get(x["level"], 3))
    out = {"items": items, "count": len(items),
           "critical": len([x for x in items if x["level"] == "critical"]),
           "warn": len([x for x in items if x["level"] == "warn"]),
           "checked": checked, "supported": supported, "slow": slow,
           "ts": datetime.now().strftime("%H:%M:%S")}
    _digest_cache["t"] = time.time()
    _digest_cache["v"] = out
    return out


def all_features(auto_scan=True):
    reg = load_registry()
    if auto_scan:
        known = {os.path.abspath(f["path"]) for f in reg["features"]}
        added = False
        for p in scan_local_features():
            if p not in known:
                fid, _ = register(p)
                if fid:
                    added = True
        if added:
            reg = load_registry()
    return [describe(f) for f in reg["features"]]


def feature_status(f):
    """探测一个功能在不在跑"""
    st = {"running": False, "detail": ""}
    if f.get("broken"):
        st["detail"] = f["broken"]
        return st
    ports = f.get("ports") or {}
    if ports:
        alive = [k for k, v in ports.items() if port_open(v)]
        st["running"] = bool(alive)
        st["detail"] = ("在听 " + ", ".join(alive)) if alive else "端口都没在听"
    elif f.get("health_url", "").startswith("http"):
        st["running"] = http_ok(f["health_url"])
        st["detail"] = "健康检查通过" if st["running"] else "健康检查不通过"
    else:
        st["detail"] = "无法探测（没有声明端口）"
    return st


def _run(cmd, cwd, wait=0):
    if not cmd:
        return None, "没有声明命令"
    logf = open(LOGFILE, "a")
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=logf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        if wait:
            time.sleep(wait)
        return p, None
    except Exception as exc:
        return None, str(exc)


def start_feature(f):
    pf = read_json(os.path.join(f["path"], "manifest.json")).get("platform") or {}
    cmd = pf.get("start")
    if not cmd:
        return False, "这个功能没有声明启停命令，请手动打开"
    p, err = _run(cmd, f["path"])
    if err:
        return False, err
    for _ in range(40):                     # 最多等 8 秒看端口起没起
        time.sleep(0.2)
        if feature_status(f)["running"]:
            return True, "已启动"
    return True, "已发出启动命令（还没探测到端口，可能还在初始化）"


def stop_feature(f):
    pf = read_json(os.path.join(f["path"], "manifest.json")).get("platform") or {}
    cmd = pf.get("stop")
    if not cmd:
        return False, "这个功能没有声明停止命令"
    _run(cmd, f["path"], wait=1.5)
    for _ in range(20):
        if not feature_status(f)["running"]:
            return True, "已停止"
        time.sleep(0.2)
    return True, "已发出停止命令"


# ══════════════════════════════════════════════════════════════
# 本地商店
# ══════════════════════════════════════════════════════════════

def store_index():
    """商店索引：扫 store/ 下每个子目录的 manifest.json"""
    items = []
    root = os.path.join(HOME, "store")
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            man = read_json(os.path.join(p, "manifest.json"))
            if not man:
                continue
            pf = man.get("platform") or {}
            items.append({
                "id": man.get("id", name),
                "name": man.get("name", name),
                "version": man.get("version", "?"),
                "description": (man.get("description") or "").strip(),
                "author": man.get("author", ""),
                "icon": pf.get("icon") or "🧩",
                "color": pf.get("color") or "#2f6df6",
                "type": man.get("type", "local"),
                "perms": [p2.get("id") for p2 in (man.get("permissions") or [])],
                "dir": p,
                # 「已装」看的是**注册表**，不是目录存不存在 ——
                # 卸载默认保留文件，只看目录的话卸载后会一直显示"已装"
                "installed": any(f["id"] == man.get("id", name)
                                 for f in load_registry()["features"]),
            })
    return items


def feature_diff(fid):
    """比一个功能的「已装版本」和「商店版本」差在哪。

    只报告，不改动 —— 用户看了再决定。
    """
    root = os.path.join(HOME, "store")
    src = None
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        p = os.path.join(root, name)
        man = read_json(os.path.join(p, "manifest.json"))
        if man and man.get("id") == fid:
            src = p
            break
    if not src:
        return {"ok": False, "error": "商店里没有这个功能"}

    reg = load_registry()
    ent = next((f for f in reg["features"] if f["id"] == fid), None)
    if not ent:
        return {"ok": False, "error": "这个功能还没装"}
    dst = ent["path"]

    missing, extra, same = [], [], 0
    for cur, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel_dir = os.path.relpath(cur, src)
        for f in files:
            rel = f if rel_dir == "." else os.path.join(rel_dir, f)
            sp, dp = os.path.join(cur, f), os.path.join(dst, rel)
            if not os.path.exists(dp):
                missing.append(rel)
            elif file_sha(sp) != file_sha(dp):
                extra.append(rel)          # 名字一样但内容不同
            else:
                same += 1
    return {"ok": True, "src": src, "dst": dst, "missing": sorted(missing),
            "differ": sorted(extra), "same": same}


def file_sha(path):
    import hashlib as _h
    try:
        with open(path, "rb") as f:
            return _h.sha256(f.read()).hexdigest()
    except OSError:
        return None


def feature_sync(fid, mode="missing"):
    """从商店补文件。

    mode='missing'（默认）：**只补目标目录里没有的**，已有的一个都不动 ——
                            这样绝不会覆盖用户在功能里攒的数据
    mode='update'          ：连内容不同的也覆盖（用户明确要求时才用），
                            但会先备份被覆盖的文件
    """
    d = feature_diff(fid)
    if not d.get("ok"):
        return d
    copied, backed = [], []
    targets = list(d["missing"])
    if mode == "update":
        targets += list(d["differ"])
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for rel in targets:
        sp = os.path.join(d["src"], rel)
        dp = os.path.join(d["dst"], rel)
        try:
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            if os.path.exists(dp):
                bak = os.path.join(BACKUP_DIR, f"{stamp}__sync__{fid}__"
                                   + rel.replace("/", "__"))
                shutil.copy2(dp, bak)
                backed.append(rel)
            shutil.copy2(sp, dp)
            copied.append(rel)
        except Exception as exc:
            log(f"[sync] {fid}/{rel} 失败：{exc}")
    if copied:
        log(f"[sync] {fid} 补了 {len(copied)} 个文件（模式 {mode}）")
    return {"ok": True, "copied": copied, "backed_up": backed,
            "mode": mode, "note": "已有文件没动" if mode == "missing"
                                  else "内容不同的也覆盖了，原文件已备份"}


def install_feature(fid):
    """从商店装一个功能到 features/ 并注册"""
    for it in store_index():
        if it["id"] != fid:
            continue
        dst = os.path.join(FEATURES_DIR, os.path.basename(it["dir"]))
        reg = load_registry()
        if any(f["id"] == fid for f in reg["features"]):
            return False, "已经装过了"
        if os.path.isdir(dst):
            # 目录还在（多半是之前卸载时保留了文件）—— 直接注册，不重新拷贝，
            # 免得把用户在功能里攒的数据覆盖掉
            fid2, msg = register(dst)
            log(f"[store] 重新启用已存在的目录 {fid} → {dst}（{msg}）")
            return True, "已重新启用（原有文件保留）"
        shutil.copytree(it["dir"], dst)
        fid2, msg = register(dst)
        log(f"[store] 安装 {fid} → {dst}（{msg}）")
        return True, f"已安装到 {dst}"
    return False, "商店里没有这个功能"


def uninstall_feature(fid, delete=False):
    reg = load_registry()
    target = next((f for f in reg["features"] if f["id"] == fid), None)
    if not target:
        return False, "没注册过这个功能"
    path = target["path"]
    st = describe(target)
    if st.get("has_stop"):
        try:
            stop_feature(st)
        except Exception:
            pass
    unregister(fid)
    inside_features = os.path.abspath(path).startswith(os.path.abspath(FEATURES_DIR) + os.sep)
    if delete and inside_features and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
        return True, "已卸载并删除文件"
    return True, ("已从平台移除，文件保留在 %s（再点「安装」可重新启用）" % path)


# ══════════════════════════════════════════════════════════════
# Web：桌面 + 商店
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "platform/" + VERSION

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

    # ── GET ──
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        # 查询串统一在这里解一次 —— 各个路由自己再解容易漏，
        # 漏了就是 UnboundLocalError（这个坑踩过一次）
        qs = urllib.parse.parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            page = os.path.join(HOME, "web", "desktop.html")
            try:
                with open(page, encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception:
                return self._send(500, "<h1>找不到 web/desktop.html</h1>",
                                  "text/html; charset=utf-8")

        if u.path == "/api/features":
            feats = all_features()
            for f in feats:
                f["status"] = feature_status(f)
            # 把各功能的告警数挂到卡片上（用 digest 的缓存，不额外轮询）
            try:
                d = collect_digest()
                cnt = {}
                for it in d.get("items", []):
                    cnt.setdefault(it["feature"], []).append(it)
                for f in feats:
                    f["alerts"] = cnt.get(f["id"], [])
            except Exception:
                pass
            return self._send(200, json.dumps({"features": feats, "home": HOME},
                                              ensure_ascii=False))

        if u.path == "/api/digest":
            qs = urllib.parse.parse_qs(u.query)
            fresh = qs.get("fresh", ["0"])[0] == "1"
            if fresh:
                _digest_cache["v"] = None
            return self._send(200, json.dumps(collect_digest(), ensure_ascii=False))

        if u.path == "/api/feature/diff":
            """看已装版本和商店版本差在哪"""
            fid = (qs.get("id", [""])[0] or "").strip()
            if not fid:
                return self._send(400, json.dumps({"ok": False, "error": "没给 id"},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(feature_diff(fid), ensure_ascii=False))

        if u.path == "/api/ai/shots":
            """最近能看的图 —— 桌面的截图、平台暂存的、以及截图文件夹。

            做这个是让"刚截完图想问 AI"变成两步：点一下、选一张。
            """
            import glob as _glob
            out = []
            def add(p, src):
                try:
                    st = os.stat(p)
                except OSError:
                    return
                low = p.lower()
                if not low.endswith((".png", ".jpg", ".jpeg", ".webp")):
                    return
                out.append({"path": p, "name": os.path.basename(p), "src": src,
                            "mb": round(st.st_size / 1048576, 2), "mtime": st.st_mtime})
            # 平台暂存的（拖进来/别的功能甩过来的）
            for p in _glob.glob(os.path.join(HOME, "tmp-images", "*")):
                add(p, "暂存")
            # 桌面上的图（含 macOS 截图默认落点），只看最近 3 天、只扫一层
            desk = os.path.expanduser("~/Desktop")
            cutoff = time.time() - 3 * 86400
            if os.path.isdir(desk):
                for p in _glob.glob(os.path.join(desk, "*")):
                    if os.path.isfile(p) and os.path.getmtime(p) > cutoff:
                        add(p, "桌面")
            # 截图文件夹
            for d in (os.path.expanduser("~/Pictures/Screenshots"),
                      os.path.expanduser("~/Pictures/截图"),
                      os.path.expanduser("~/Desktop/截图")):
                if os.path.isdir(d):
                    for p in _glob.glob(os.path.join(d, "*")):
                        add(p, os.path.basename(d))
            out.sort(key=lambda x: -x["mtime"])
            for x in out:
                x["when"] = datetime.fromtimestamp(x["mtime"]).strftime("%m-%d %H:%M")
            return self._send(200, json.dumps({"items": out[:40], "count": len(out)},
                                              ensure_ascii=False))

        if u.path == "/api/ai/file":
            """把本地图片内容给浏览器看（显示缩略图用）。

            只允许图片，且限制在平台目录或 tmp-images 里 ——
            不给它当任意文件读取接口用。
            """
            path = (qs.get("path", [""])[0] or "").strip()
            if not path:
                return self._send(400, b"no path", "text/plain")
            ap = os.path.abspath(path)
            root = os.path.abspath(HOME)
            ok_dir = (ap.startswith(root + os.sep)
                      or ap.startswith("/tmp/")
                      or ap.startswith(os.path.expanduser("~/Desktop") + os.sep))
            if not ok_dir or not os.path.isfile(ap):
                return self._send(404, b"not found", "text/plain")
            low = ap.lower()
            ctype = ("image/jpeg" if low.endswith((".jpg", ".jpeg")) else
                     "image/webp" if low.endswith(".webp") else
                     "image/gif" if low.endswith(".gif") else "image/png")
            if not low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                return self._send(403, b"only images", "text/plain")
            try:
                with open(ap, "rb") as f:
                    data = f.read(24 * 1024 * 1024)
            except OSError as exc:
                return self._send(500, str(exc).encode(), "text/plain")
            return self._send(200, data, ctype)

        if u.path == "/api/watch":
            w = _ai_watch()
            if not w:
                return self._send(200, json.dumps({"ok": False, "error": "值守模块不可用"},
                                                  ensure_ascii=False))
            st = w.status()
            st["ok"] = True
            return self._send(200, json.dumps(st, ensure_ascii=False))

        if u.path == "/api/desktop":
            return self._send(200, json.dumps(
                {"items": desktop_items(), "kinds": ITEM_KINDS,
                 "home": os.path.expanduser("~")}, ensure_ascii=False))

        if u.path == "/api/pick":
            """选文件夹/文件用：列一个目录的内容"""
            path = (qs.get("path", [os.path.expanduser("~")])[0] or "").strip()
            path = os.path.expanduser(path)
            show_files = qs.get("files", ["1"])[0] == "1"
            try:
                dirs, files = [], []
                for name in sorted(os.listdir(path)):
                    if name.startswith("."):
                        continue
                    p = os.path.join(path, name)
                    try:
                        if os.path.isdir(p):
                            dirs.append({"name": name, "path": p})
                        elif show_files:
                            files.append({"name": name, "path": p,
                                          "app": name.endswith(".app")})
                    except OSError:
                        continue
                return self._send(200, json.dumps(
                    {"path": path, "parent": os.path.dirname(path),
                     "dirs": dirs[:300], "files": files[:300]},
                    ensure_ascii=False))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))

        if u.path == "/api/ai/core":
            """AI 根入口状态 —— 它就是平台自己，删不掉"""
            core = _ai_core()
            if not core:
                return self._send(200, json.dumps(
                    {"ok": False, "error": "根入口加载失败"}, ensure_ascii=False))
            return self._send(200, json.dumps({
                "ok": True,
                "config": core.config(),
                "integrity": core.integrity(),
                "audit": core.audit_log(20),
                "backups": core.list_backups(15),
                "protected": sorted(core.PROTECTED),
            }, ensure_ascii=False))

        if u.path == "/api/ai/audit":
            core = _ai_core()
            return self._send(200, json.dumps(
                {"audit": core.audit_log(int(qs.get("n", ["50"])[0])) if core else []},
                ensure_ascii=False))

        if u.path == "/api/prefs":
            d = load_prefs()
            d["recent"] = d["recent"][:12]
            return self._send(200, json.dumps(d, ensure_ascii=False))

        if u.path == "/api/settings":
            """平台级设置：AI、身份、路径 —— 在这里统一配，不用去各功能里找"""
            ai = {}
            try:
                sys.path.insert(0, os.path.join(HOME, "lib"))
                import platform_lib
                c = platform_lib.config()
                ai = {"provider": c["provider"], "provider_name": c["provider_name"],
                      "model": c["model"], "base_url": c["base_url"],
                      "ready": platform_lib.ready(),
                      "key_hint": (c["api_key"][:7] + "…" + c["api_key"][-4:]) if c["api_key"] else "",
                      "file": platform_lib.PLATFORM_AI}
            except Exception as exc:
                ai = {"ready": False, "error": str(exc)}
            ident = {}
            try:
                import platform_lib
                ident = platform_lib.identity()
            except Exception:
                pass
            return self._send(200, json.dumps({
                "ai": ai, "identity": ident, "home": HOME,
                "features_dir": FEATURES_DIR, "store_dir": os.path.join(HOME, "store"),
                "port": args_port(),
            }, ensure_ascii=False))

        if u.path == "/api/store":
            return self._send(200, json.dumps({"items": store_index()}, ensure_ascii=False))

        if u.path == "/api/status":
            feats = all_features()
            run = sum(1 for f in feats if not f.get("broken") and feature_status(f)["running"])
            return self._send(200, json.dumps({
                "version": VERSION, "home": HOME, "total": len(feats), "running": run,
                "store": len(store_index()),
            }, ensure_ascii=False))

        # 纯静态功能：由平台代管它的界面
        if u.path.startswith("/view/"):
            fid = u.path[len("/view/"):].strip("/")
            f = next((x for x in all_features() if x["id"] == fid), None)
            if not f:
                return self._send(404, json.dumps({"error": "没有这个功能"}))
            entry = read_json(os.path.join(f["path"], "manifest.json")).get("ui", {}).get("entry")
            if not entry:
                return self._send(404, json.dumps({"error": "这个功能没有界面"}))
            try:
                with open(os.path.join(f["path"], entry), encoding="utf-8") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}))

        return self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))

    # ── POST ──
    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        body = self._body()
        b = body          # 别名：新加的路由习惯用 b，统一在这里给上，
                          # 免得又踩 NameError（这个坑踩过一次）
        fid = (body.get("id") or "").strip()

        def find():
            return next((x for x in all_features() if x["id"] == fid), None)

        if u.path == "/api/start":
            f = find()
            if not f:
                return self._send(404, json.dumps({"ok": False, "error": "没有这个功能"}))
            ok, msg = start_feature(f)
            log(f"[start] {fid}: {msg}")
            return self._send(200, json.dumps({"ok": ok, "message": msg}, ensure_ascii=False))

        if u.path == "/api/stop":
            f = find()
            if not f:
                return self._send(404, json.dumps({"ok": False, "error": "没有这个功能"}))
            ok, msg = stop_feature(f)
            log(f"[stop] {fid}: {msg}")
            return self._send(200, json.dumps({"ok": ok, "message": msg}, ensure_ascii=False))

        if u.path == "/api/install":
            ok, msg = install_feature(fid)
            return self._send(200, json.dumps({"ok": ok, "message": msg}, ensure_ascii=False))

        if u.path == "/api/uninstall":
            ok, msg = uninstall_feature(fid, delete=bool(body.get("delete")))
            return self._send(200, json.dumps({"ok": ok, "message": msg}, ensure_ascii=False))

        if u.path == "/api/register":
            p = (body.get("path") or "").strip()
            if not p or not os.path.isdir(p):
                return self._send(400, json.dumps({"ok": False, "error": "路径不存在"}))
            rid, msg = register(p)
            return self._send(200, json.dumps({"ok": bool(rid), "id": rid, "message": msg},
                                              ensure_ascii=False))

        if u.path == "/api/watch/run":
            w = _ai_watch()
            if not w:
                return self._send(200, json.dumps({"ok": False, "error": "值守模块不可用"},
                                                  ensure_ascii=False))
            new = w.run_once()
            return self._send(200, json.dumps(
                {"ok": True, "found": len(new),
                 "items": [{"title": x.get("title"), "kind": x.get("kind"),
                            "analysis": x.get("analysis", "")[:4000]} for x in new]},
                ensure_ascii=False))

        if u.path == "/api/watch/clear":
            w = _ai_watch()
            if not w:
                return self._send(200, json.dumps({"ok": False, "error": "值守模块不可用"},
                                                  ensure_ascii=False))
            keep = int(b.get("keep_hours") or 0)
            r = w.clear_findings(keep)
            _digest_cache["v"] = None      # 让下次聚合重新取
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/watch/toggle":
            on = bool(b.get("on", True))
            ok, msg = start_ai_watch() if on else stop_ai_watch()
            return self._send(200, json.dumps({"ok": ok, "message": msg},
                                              ensure_ascii=False))

        if u.path == "/api/feature/sync":
            """从商店补文件（默认只补缺的，不覆盖已有的）"""
            fid = (b.get("id") or "").strip()
            if not fid:
                return self._send(400, json.dumps({"ok": False, "error": "没给 id"},
                                                  ensure_ascii=False))
            r = feature_sync(fid, b.get("mode") or "missing")
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/desktop/add":
            ok, msg, item = add_desktop_item(
                b.get("kind") or "folder", b.get("target") or "",
                b.get("name"), b.get("icon"), b.get("color"), b.get("note") or "")
            return self._send(200, json.dumps({"ok": ok, "message": msg, "item": item},
                                              ensure_ascii=False))

        if u.path == "/api/desktop/remove":
            ok, msg = remove_desktop_item((b.get("id") or "").strip())
            return self._send(200, json.dumps({"ok": ok, "message": msg},
                                              ensure_ascii=False))

        if u.path == "/api/desktop/reorder":
            ok, msg = reorder_desktop(b.get("order") or [])
            return self._send(200, json.dumps({"ok": ok, "message": msg},
                                              ensure_ascii=False))

        if u.path == "/api/desktop/open":
            ok, msg, target = open_desktop_item((b.get("id") or "").strip())
            return self._send(200, json.dumps({"ok": ok, "message": msg, "target": target},
                                              ensure_ascii=False))

        if u.path == "/api/ai/chat":
            """AI 对话（流式）。这个入口在平台进程里，不依赖任何功能。"""
            core = _ai_core()
            if not core:
                return self._send(500, json.dumps({"error": "AI 根入口加载失败"},
                                                  ensure_ascii=False))
            if not core.ready():
                return self._send(200, json.dumps(
                    {"error": "AI 还没配好 —— 去「设置」里填 key，或点一键填写"},
                    ensure_ascii=False))
            question = (b.get("q") or "").strip()
            if not question:
                return self._send(400, json.dumps({"error": "问题是空的"},
                                                  ensure_ascii=False))
            # 需要上下文时，把相关文件读进来
            ctx = ""
            rel = (b.get("context_file") or "").strip()
            if rel:
                r = core.read_file(rel)
                ctx = r.get("text", "") if r.get("ok") else ""

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def emit(obj):
                self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) +
                                  "\n\n").encode("utf-8"))
                self.wfile.flush()

            tool_calls = []
            try:
                for kind, text in core.stream(question, b.get("history"), ctx):
                    if kind == "text":
                        emit({"type": "text", "text": text})
                    elif kind == "tool":
                        tool_calls.append(text)
                emit({"type": "tools", "tools": tool_calls})
                emit({"type": "done"})
            except Exception as exc:
                try:
                    emit({"type": "text", "text": "\n❌ " + str(exc)})
                    emit({"type": "done"})
                except Exception:
                    pass
            return

        if u.path == "/api/ai/stage":
            """浏览器拖进来的图先存到临时目录 —— 浏览器给不了本地路径，
            但看图接口需要路径。存完返回路径。

            只接受图片，且限制大小；存到平台目录下的 tmp-images/。
            """
            import base64 as _b64
            data = b.get("data") or ""
            name = (b.get("name") or "image.png").replace("/", "_").replace("\\", "_")
            if not data.startswith("data:image/"):
                return self._send(400, json.dumps(
                    {"ok": False, "error": "只收图片"}, ensure_ascii=False))
            try:
                head, b64 = data.split(",", 1)
                raw = _b64.b64decode(b64)
            except Exception as exc:
                return self._send(400, json.dumps(
                    {"ok": False, "error": "图解码失败：%s" % exc}, ensure_ascii=False))
            if len(raw) > 12 * 1024 * 1024:
                return self._send(400, json.dumps(
                    {"ok": False, "error": "图太大了（超过 12 MB）"}, ensure_ascii=False))
            d = os.path.join(HOME, "tmp-images")
            os.makedirs(d, exist_ok=True)
            ext = os.path.splitext(name)[1].lower() or ".png"
            if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                ext = ".png"
            stamp = datetime.now().strftime("%H%M%S")
            path = os.path.join(d, "shot-%s-%s%s" % (stamp, abs(hash(name)) % 10000, ext))
            try:
                with open(path, "wb") as f:
                    f.write(raw)
            except Exception as exc:
                return self._send(500, json.dumps(
                    {"ok": False, "error": "存图失败：%s" % exc}, ensure_ascii=False))
            # 顺手清掉一天前的，别让临时目录涨起来
            try:
                cutoff = time.time() - 86400
                for fn in os.listdir(d):
                    fp = os.path.join(d, fn)
                    if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
            except Exception:
                pass
            log(f"[vision] 暂存图片 {os.path.basename(path)}（{len(raw)//1024} KB）")
            return self._send(200, json.dumps(
                {"ok": True, "path": path, "bytes": len(raw)}, ensure_ascii=False))

        if u.path == "/api/ai/vision":
            """看图：POST {paths:[...], q:"问什么"}"""
            core = _ai_core()
            if not core:
                return self._send(500, json.dumps({"error": "根入口不可用"},
                                                  ensure_ascii=False))
            paths = b.get("paths") or ([b["path"]] if b.get("path") else [])
            if not paths:
                return self._send(400, json.dumps({"error": "没给图片路径"},
                                                  ensure_ascii=False))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def emit2(obj):
                self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) +
                                  "\n\n").encode("utf-8"))
                self.wfile.flush()

            try:
                for ev in core.describe_image(paths, b.get("q") or ""):
                    emit2(ev)
            except Exception as exc:
                try:
                    emit2({"type": "error", "error": str(exc)})
                except Exception:
                    pass
            return

        if u.path == "/api/ai/agent":
            """让 AI 自己循环完成一个任务（agent 模式）"""
            core = _ai_core()
            if not core:
                return self._send(500, json.dumps({"error": "根入口不可用"},
                                                  ensure_ascii=False))
            if not core.ready():
                return self._send(200, json.dumps(
                    {"error": "AI 还没配好 —— 去「设置」里填 key"}, ensure_ascii=False))
            goal = (b.get("goal") or "").strip()
            if not goal:
                return self._send(400, json.dumps({"error": "没给任务"},
                                                  ensure_ascii=False))
            rounds = max(1, min(int(b.get("max_rounds") or 16), 30))

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def emit(obj):
                self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) +
                                  "\n\n").encode("utf-8"))
                self.wfile.flush()

            try:
                for ev in core.agent_stream(goal, rounds):
                    emit(ev)
            except Exception as exc:
                try:
                    emit({"type": "say", "text": "\n❌ " + str(exc)})
                except Exception:
                    pass
            return

        if u.path == "/api/ai/tool":
            """执行 AI 请求的工具调用（读文件/改文件），带备份和回滚"""
            core = _ai_core()
            if not core:
                return self._send(500, json.dumps({"ok": False, "error": "根入口不可用"},
                                                  ensure_ascii=False))
            call = b.get("call") or {}
            ok, text = core.run_tool(call)
            return self._send(200, json.dumps({"ok": ok, "result": text},
                                              ensure_ascii=False))

        if u.path == "/api/ai/accept":
            core = _ai_core()
            if not core:
                return self._send(500, json.dumps({"ok": False, "error": "根入口不可用"},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(core.accept_current_core(),
                                              ensure_ascii=False))

        if u.path == "/api/ai/restore":
            core = _ai_core()
            if not core:
                return self._send(500, json.dumps({"ok": False, "error": "根入口不可用"},
                                                  ensure_ascii=False))
            r = core.restore_core(b.get("which"))
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/ai/rollback":
            core = _ai_core()
            if not core:
                return self._send(500, json.dumps({"ok": False, "error": "根入口不可用"},
                                                  ensure_ascii=False))
            r = core.rollback(b.get("backup") or "")
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/pin":
            with _lock:
                d = load_prefs()
                fid2 = (b.get("id") or "").strip()
                if fid2 in d["pinned"]:
                    d["pinned"].remove(fid2)
                    pinned = False
                else:
                    d["pinned"].append(fid2)
                    pinned = True
                save_prefs(d)
            return self._send(200, json.dumps({"ok": True, "pinned": pinned},
                                              ensure_ascii=False))

        if u.path == "/api/opened":
            fid2 = (b.get("id") or "").strip()
            if fid2:
                touch_recent(fid2)
            return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))

        if u.path == "/api/ai/save":
            try:
                sys.path.insert(0, os.path.join(HOME, "lib"))
                import platform_lib
                cur = {}
                try:
                    with open(platform_lib.PLATFORM_AI, encoding="utf-8") as f:
                        cur = json.load(f)
                except Exception:
                    pass
                for k in ("provider", "model", "base_url", "api_key"):
                    v = b.get(k)
                    if v is not None and str(v).strip():
                        cur[k] = str(v).strip()
                if b.get("clear_key"):
                    cur.pop("api_key", None)
                os.makedirs(os.path.dirname(platform_lib.PLATFORM_AI), exist_ok=True)
                with open(platform_lib.PLATFORM_AI, "w", encoding="utf-8") as f:
                    json.dump(cur, f, ensure_ascii=False, indent=2)
                try:
                    os.chmod(platform_lib.PLATFORM_AI, 0o600)
                except Exception:
                    pass
                ok, msg = platform_lib.test() if not b.get("skip_test") else (True, "已保存")
                log(f"[ai] 平台 AI 配置已更新 provider={cur.get('provider')}")
                return self._send(200, json.dumps(
                    {"ok": True, "test_ok": ok, "message": msg}, ensure_ascii=False))
            except Exception as exc:
                return self._send(500, json.dumps({"ok": False, "error": str(exc)},
                                                  ensure_ascii=False))

        if u.path == "/api/ai/autofill":
            try:
                sys.path.insert(0, os.path.join(HOME, "lib"))
                import platform_lib
                cands = platform_lib.find_keys()
                if not cands:
                    return self._send(404, json.dumps(
                        {"ok": False, "error": "本机没找到可复用的 key"}, ensure_ascii=False))
                tried = []
                for label, key in cands:
                    with open(platform_lib.PLATFORM_AI, "w", encoding="utf-8") as f:
                        json.dump({"provider": "deepseek", "api_key": key}, f,
                                  ensure_ascii=False, indent=2)
                    try:
                        os.chmod(platform_lib.PLATFORM_AI, 0o600)
                    except Exception:
                        pass
                    ok, msg = platform_lib.test()
                    tried.append({"source": label, "hint": key[:7] + "…" + key[-4:],
                                  "ok": ok, "message": "" if ok else msg})
                    if ok:
                        log(f"[ai] 一键填写采用 {label}")
                        return self._send(200, json.dumps(
                            {"ok": True, "source": label, "hint": key[:7] + "…" + key[-4:],
                             "message": msg, "tried": tried}, ensure_ascii=False))
                return self._send(200, json.dumps(
                    {"ok": False, "tried": tried, "error": "找到的 key 都没测通"},
                    ensure_ascii=False))
            except Exception as exc:
                return self._send(500, json.dumps({"ok": False, "error": str(exc)},
                                                  ensure_ascii=False))

        if u.path == "/api/toggle":
            reg = load_registry()
            for f in reg["features"]:
                if f["id"] == fid:
                    f["enabled"] = not f.get("enabled", True)
            save_registry(reg)
            return self._send(200, json.dumps({"ok": True}))

        return self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def running_pid():
    try:
        pid = int((open(PIDFILE).read() or "0").strip())
    except Exception:
        return 0
    if not pid:
        return 0
    try:
        os.kill(pid, 0)
        return pid
    except Exception:
        return 0


def _ai_core():
    """加载 AI 根入口。它是平台自带的能力，不是某个功能 ——
    这样功能全坏了，AI 入口仍然可用，还能拿来修平台。"""
    try:
        sys.path.insert(0, os.path.join(HOME, "lib"))
        import ai_core
        return ai_core
    except Exception as exc:
        log(f"[ai] 根入口加载失败：{exc}")
        return None


def _ai_watch():
    """加载 AI 值守模块"""
    try:
        sys.path.insert(0, os.path.join(HOME, "lib"))
        import ai_watch
        return ai_watch
    except Exception as exc:
        log(f"[watch] 加载失败：{exc}")
        return None


_watch_state = {"ev": None, "thread": None}


def start_ai_watch(interval=60):
    """开机启动值守 —— 让 AI 主动发现问题，而不是等用户来问"""
    w = _ai_watch()
    if not w:
        return False, "值守模块加载失败"
    if _watch_state["thread"] and _watch_state["thread"].is_alive():
        return True, "已经在值守"
    ev, t = w.start_background(interval)
    _watch_state["ev"] = ev
    _watch_state["thread"] = t
    return True, "值守已启动"


def stop_ai_watch():
    ev = _watch_state.get("ev")
    if ev:
        ev.set()
    return True, "值守已停止"


def args_port():
    try:
        pid = running_pid()
        if pid:
            import subprocess as _sp
            out = _sp.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
                          capture_output=True, text=True, timeout=5).stdout
            m = re.search(r":(\d+)\s*\(LISTEN\)", out)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return DEFAULT_PORT


def cmd_serve(args):
    port = args.port or DEFAULT_PORT
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    log(f"工作平台 v{VERSION} 已启动  桌面 http://127.0.0.1:{port}  数据目录 {HOME}")
    # 顺手把 AI 值守起起来 —— 它主动发现问题，结论会进「今日关注」
    try:
        _ok, _msg = start_ai_watch()
        log(f"[watch] {_msg}")
    except Exception as _e:
        log(f"[watch] 启动失败：{_e}")
    for p in scan_local_features():
        register(p)

    def bye(*_):
        log("收到退出信号")
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
                     stdout=logf, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True, cwd=HOME)
    for _ in range(30):
        time.sleep(0.2)
        if running_pid():
            break
    if running_pid():
        print(f"✅ 工作平台已启动（PID {running_pid()}）")
        print(f"   桌面：http://127.0.0.1:{port}")
        return 0
    print("❌ 启动失败，看 platform.log")
    return 1


def cmd_stop(args):
    pid = running_pid()
    if not pid:
        print("没有在运行")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.8)
    except Exception:
        pass
    try:
        os.remove(PIDFILE)
    except Exception:
        pass
    print("已停止")
    return 0


def cmd_status(args):
    feats = all_features()
    print("═" * 66)
    print(f"  工作平台 v{VERSION}")
    print(f"  状态    : {'✅ 运行中（PID %s）' % running_pid() if running_pid() else '⛔️ 未运行'}")
    print(f"  桌面    : http://127.0.0.1:{args.port or DEFAULT_PORT}")
    print(f"  数据目录: {HOME}")
    print("═" * 66)
    if not feats:
        print("  （还没有功能。把功能目录放进 features/，或用 platform register <路径>）")
        return 0
    print(f"  {'':2} {'功能':<14} {'版本':<8} {'状态':<8} 说明")
    for f in feats:
        st = feature_status(f)
        mark = "🟢 在跑" if st["running"] else ("⚠️  " + st["detail"][:14] if f.get("broken")
                                             else "⚪️ 停止")
        print(f"  {f.get('icon','🧩')} {f['name'][:12]:<14} {f['version']:<8} {mark:<8} "
              f"{(f['description'] or '')[:34]}")
    st_items = store_index()
    if st_items:
        print(f"\n  商店里还有 {len(st_items)} 个可装：")
        for it in st_items:
            tag = "已装" if it["installed"] else "可装"
            print(f"    {it.get('icon','🧩')} {it['name'][:14]:<16} v{it['version']:<8} [{tag}] {it['description'][:30]}")
    return 0


def cmd_start_all(args):
    """把所有声明了 start 命令的功能都拉起来（重启电脑后用）。

    **并行启动** —— 串行的话每个最多等 6 秒，27 个功能能拖到 160 秒，
    排在后面的就来不及（实测就是这个原因导致最后一个功能老是起不来）。
    """
    feats = all_features()
    todo = [f for f in feats if f.get("has_start") and not f.get("broken")]
    print(f"共 {len(todo)} 个功能要启动（并行）…")
    t0 = time.time()
    import concurrent.futures as cf
    results = {}

    def one(f):
        st = feature_status(f)
        if st["running"]:
            return f["name"], True, "已在跑"
        try:
            r, msg = start_feature(f)
            return f["name"], bool(r), msg
        except Exception as exc:
            return f["name"], False, "%s: %s" % (type(exc).__name__, exc)

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for name, ok, msg in ex.map(one, todo):
            results[name] = (ok, msg)
            print(f"  {'✅' if ok else '❌'} {name}：{msg}")

    # 并行启动时有的功能起得慢，再复查一轮
    time.sleep(2)
    still = []
    for f in todo:
        if not feature_status(f)["running"]:
            still.append(f)
    if still:
        print(f"\n  复查：还有 {len(still)} 个没起来，再等 8 秒…")
        for _ in range(16):
            time.sleep(0.5)
            still = [f for f in still if not feature_status(f)["running"]]
            if not still:
                break
    ok_n = len(todo) - len(still)
    print(f"\n{ok_n}/{len(todo)} 个在跑（{round(time.time()-t0, 1)}s）")
    for f in still:
        print(f"  ⚠️  {f['name']} 还没起来 —— 单独试：platform.py 里点「启动」")
    return 0 if not still else 1


def cmd_stop_all(args):
    feats = all_features()
    todo = [f for f in feats if f.get("has_stop")]
    print(f"共 {len(todo)} 个功能要停止…")
    for f in todo:
        if not feature_status(f)["running"]:
            print(f"  ⏭  {f['name']}（本来就没跑）")
            continue
        r, msg = stop_feature(f)
        print(f"  {'✅' if r else '❌'} {f['name']}：{msg}")
    return 0


def cmd_features(args):
    for f in all_features():
        st = feature_status(f)
        print(f"{f.get('icon','🧩')} {f['id']:<14} v{f['version']:<8} "
              f"{'running' if st['running'] else 'stopped':<8} {f['path']}")
    return 0


def cmd_store(args):
    items = store_index()
    if not items:
        print("商店是空的。把功能目录放进 store/ 即可出现在这里。")
        return 0
    print(f"本地商店（{len(items)} 个）")
    for it in items:
        print(f"  {it.get('icon','🧩')} {it['id']:<14} v{it['version']:<8} "
              f"{'[已装]' if it['installed'] else '[可装]':<8} {it['name']}")
        if it["description"]:
            print(f"      {it['description'][:70]}")
        if it["perms"]:
            print(f"      权限: {', '.join(it['perms'])}")
    print("\n装：platform install <id>")
    return 0


def cmd_install(args):
    ok, msg = install_feature(args.id)
    print(("✅ " if ok else "❌ ") + msg)
    return 0 if ok else 1


def cmd_register(args):
    fid, msg = register(args.path)
    print(("✅ " if fid else "❌ ") + f"{fid or ''} {msg}")
    return 0 if fid else 1


def cmd_open(args):
    port = args.port or DEFAULT_PORT
    if not running_pid():
        cmd_start(args)
    url = f"http://127.0.0.1:{port}"
    print(f"打开 {url}")
    webbrowser.open(url)
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="platform", description=f"工作平台 v{VERSION}")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("status"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_status)
    sub.add_parser("features").set_defaults(f=cmd_features)
    sub.add_parser("start-all").set_defaults(f=cmd_start_all)
    sub.add_parser("stop-all").set_defaults(f=cmd_stop_all)
    sub.add_parser("store").set_defaults(f=cmd_store)
    sp = sub.add_parser("install"); sp.add_argument("id"); sp.set_defaults(f=cmd_install)
    sp = sub.add_parser("register"); sp.add_argument("path"); sp.set_defaults(f=cmd_register)
    sp = sub.add_parser("open"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_open)

    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
