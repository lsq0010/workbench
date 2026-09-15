#!/usr/bin/env python3
"""iOS 开发自动化 —— 把「编译→装→跑→看日志」串成一次点击。

为什么做这个
  你机器上有 22 个 Xcode 工程，全是 CocoaPods 的 .xcworkspace。
  日常改一行的循环是：
    pod install（偶尔）→ xcodebuild 编译 → 装到模拟器 → 启动 → 看日志
  每次手敲四五条命令，还要记 -workspace 和 -scheme 哪个对。

这个工具做的事
  · 自动认出工程该用 -workspace 还是 -project（**CocoaPods 工程用错就编不过**）
  · 自动列 scheme、自动挑模拟器
  · **一键流水线**：编译 → 装 → 启动 → 顺手开始抓日志（接到「代码日志」）
  · 单独的构建 / 归档 / 导出 IPA / 截图 / pod install
  · 构建失败时把错误摘出来（不是甩一屏日志给你）

半自动：点了才动，不后台乱跑。所有命令都是你本来会手敲的那些，只是串起来了。
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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("IOSAUTO_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "iosauto.pid")
LOGFILE = os.path.join(HOME, "iosauto.log")
RUNS = os.path.join(HOME, "runs.jsonl")          # 只追加
DEFAULT_PORT = 8919
LOGHUB = os.environ.get("IOSAUTO_LOGHUB", "http://127.0.0.1:8918")


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} [iosauto] {msg}"
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


_xcache = {"t": 0, "v": ""}


def xcode_version(max_age=600):
    """Xcode 版本 —— 缓存 10 分钟。

    这个调用要 2~5 秒，不能每次开界面都现取（界面会卡），
    更不能放在启动路径上（会让一键恢复所有功能变慢）。
    """
    import time as _t
    if _xcache["v"] and _t.time() - _xcache["t"] < max_age:
        return _xcache["v"]
    out = (sh(["xcodebuild", "-version"], timeout=60)[1] or "").split("\n")[0]
    _xcache["v"] = out or "Xcode（读不到版本）"
    _xcache["t"] = _t.time()
    return _xcache["v"]


def sh(cmd, timeout=600, cwd=None):
    """跑一条命令，返回 (code, stdout+stderr)"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd, errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"超时（{timeout}s）：{' '.join(cmd[:3])}…"
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}"


# ══════════════════════════════════════════════════════════════
# 发现工程
# ══════════════════════════════════════════════════════════════

SKIP_DIRS = {"Pods", "build", "DerivedData", ".git", "node_modules", "Carthage",
             ".build", "build.xcresult"}


def discover_projects(roots=None, depth=3):
    """扫出 Xcode 工程。一个目录里同时有 workspace 和 project 时，
    有 Podfile 就用 workspace（CocoaPods 生成的），否则用 project。"""
    roots = roots or [os.path.expanduser("~/Desktop"), os.path.expanduser("~/Documents")]
    found = {}

    def walk(base, cur_depth):
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return
        workspaces = [e for e in entries if e.endswith(".xcworkspace")
                      and e != "project.xcworkspace"]
        projects = [e for e in entries if e.endswith(".xcodeproj")
                    and e != "Pods.xcodeproj"]
        has_podfile = "Podfile" in entries
        if workspaces or projects:
            # 优先 workspace（有 Podfile 的工程必须用它）
            if workspaces and has_podfile:
                pick, kind = workspaces[0], "workspace"
            elif workspaces and not projects:
                pick, kind = workspaces[0], "workspace"
            elif projects:
                pick, kind = projects[0], "project"
            else:
                pick, kind = workspaces[0], "workspace"
            found[base] = {
                "dir": base, "name": os.path.basename(base),
                "entry": os.path.join(base, pick), "kind": kind,
                "pods": has_podfile,
                "has_workspace": bool(workspaces), "has_project": bool(projects),
            }
        if cur_depth >= depth:
            return
        for e in entries:
            p = os.path.join(base, e)
            if os.path.isdir(p) and not e.startswith(".") and e not in SKIP_DIRS \
                    and not e.endswith((".xcodeproj", ".xcworkspace", ".app", ".framework")):
                walk(p, cur_depth + 1)

    for r in roots:
        if os.path.isdir(r):
            for name in sorted(os.listdir(r)):
                p = os.path.join(r, name)
                if os.path.isdir(p) and not name.startswith("."):
                    walk(p, 1)
    return sorted(found.values(), key=lambda x: x["name"].lower())


def extract_json(text):
    """从混杂输出里把 JSON 抠出来。

    踩过的坑：sh() 返回的是 stdout+stderr 拼接，而 xcodebuild 的警告走 stderr
    —— 于是 JSON 后面跟着一堆警告行，json.loads 直接失败，
    退回到正则兜底又抓到 `]` 这种垃圾，scheme 列表就成了 [' ] ']。
    正确做法：从**第一个 { 到最后一个 }** 截，再解析。
    """
    if not text:
        return None
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    frag = text[i:j + 1]
    try:
        return json.loads(frag)
    except Exception:
        # 有些输出里混了多个 JSON 片段，逐层缩小再试
        for end in range(j, i, -1):
            if text[end] != "}":
                continue
            try:
                return json.loads(text[i:end + 1])
            except Exception:
                continue
    return None


def list_schemes(entry, kind):
    flag = "-workspace" if kind == "workspace" else "-project"
    # 先把「真 App target」找出来 —— 比猜名字可靠
    targets = set()
    if kind == "workspace":
        for pr in workspace_main_projects(entry):
            targets |= app_targets(pr)
    else:
        targets |= app_targets(entry)
    code, out = sh(["xcodebuild", "-list", "-json", flag, entry], timeout=180)
    d = extract_json(out)
    if d:
        info = (d.get("workspace") or d.get("project") or {})
        schemes = info.get("schemes") or []
        if schemes:
            if targets:
                # 有权威名单：按它分组
                app = [x for x in schemes if x.lower() in targets]
                lib = [x for x in schemes if x.lower() not in targets]
                # App 里和工程同名的排最前
                pname = os.path.basename(os.path.dirname(entry)).lower()
                app.sort(key=lambda x: (x.lower() != pname, x))
            else:
                app, lib = app_schemes(schemes,
                                       os.path.basename(os.path.dirname(entry)))
            return {"ok": True, "schemes": app + lib,
                    "app_schemes": app, "lib_count": len(lib),
                    "app_targets_known": bool(targets),
                    "configs": info.get("configurations") or []}
    if code != 0:
        return {"ok": False, "error": _short_err(out)}
    # 解析不出来时，用命令行那种人类可读的格式再试一次（它是纯文本，好抓）
    code2, out2 = sh(["xcodebuild", "-list", flag, entry], timeout=180)
    m = re.search(r"Schemes:\s*\n((?:\s+\S.*\n?)+)", out2)
    if m:
        schemes = [x.strip() for x in m.group(1).splitlines() if x.strip()]
        if schemes and not all(x in ("]", "[") for x in schemes):
            return {"ok": True, "schemes": schemes, "configs": []}
    return {"ok": False, "error": "读不出 scheme 列表：\n" + _short_err(out, 600)}


def _short_err(out, limit=1400):
    """从一屏构建日志里摘出真正的错"""
    lines = out.splitlines()
    keep = []
    for i, ln in enumerate(lines):
        if re.search(r"\berror:|\bwarning:.*deprecated|failed|\*\* BUILD FAILED|"
                     r"Undefined symbol|not found|No such", ln, re.I):
            keep.append(ln.strip())
    if not keep:
        keep = [l.strip() for l in lines[-12:] if l.strip()]
    return "\n".join(keep[:24])[:limit] or out[:limit]


def list_sims(available_only=True):
    code, out = sh(["xcrun", "simctl", "list", "devices", "available", "-j"], timeout=60)
    if code != 0:
        return []
    try:
        d = json.loads(out)
    except Exception:
        return []
    rows = []
    for runtime, devs in (d.get("devices") or {}).items():
        rt = runtime.split(".")[-1].replace("-", " ")
        for dev in devs:
            if not dev.get("isAvailable", True):
                continue
            rows.append({"name": dev.get("name"), "udid": dev.get("udid"),
                         "state": dev.get("state"), "runtime": rt,
                         "booted": dev.get("state") == "Booted"})
    # iPhone 优先、已启动优先、新的运行时优先
    def key(x):
        name = x["name"]
        score = 0
        if x["booted"]:
            score -= 100
        if name.startswith("iPhone"):
            score -= 20
        m = re.search(r"\((\d+)\)", name)
        if m:
            score -= int(m.group(1))
        return (score, name)
    return sorted(rows, key=key)


def app_targets(project_path):
    """从一个 .xcodeproj 里找出**真正的 App target**。

    为什么要这么干：78 个 scheme 里 70 多个是 CocoaPods 的库，
    靠名字猜（有没有连字符、像不像库名）不准 —— Alamofire 就没连字符。
    直接读工程文件最可靠：productType 是 com.apple.product-type.application
    的才是 App。
    返回小写名字的集合，用于匹配 scheme。
    """
    names = set()
    pbx = os.path.join(project_path, "project.pbxproj")
    if not os.path.isfile(pbx):
        return names
    try:
        txt = open(pbx, encoding="utf-8", errors="replace").read()
    except OSError:
        return names
    # PBXNativeTarget 段落：名字在注释里，productType 在下面几行
    for m in re.finditer(
            r"([0-9A-F]{24})\s+/\*\s*([^*]+?)\s*\*/\s*=\s*\{(.*?)\n\s*\};",
            txt, re.S):
        body = m.group(3)
        if "PBXNativeTarget" not in body:
            continue
        if "com.apple.product-type.application" not in body:
            continue
        names.add(m.group(2).strip().lower())
    return names


def workspace_main_projects(entry):
    """从 .xcworkspace 里找出主工程（排除 Pods.xcodeproj）"""
    out = []
    data = os.path.join(entry, "contents.xcworkspacedata")
    if not os.path.isfile(data):
        return out
    try:
        txt = open(data, encoding="utf-8", errors="replace").read()
    except OSError:
        return out
    for m in re.finditer(r'location\s*=\s*"group:([^"]+)"', txt):
        rel = m.group(1)
        if "Pods.xcodeproj" in rel:
            continue
        p = os.path.join(os.path.dirname(entry), rel)
        if p.endswith(".xcodeproj") and os.path.isdir(p):
            out.append(p)
    return out


def app_schemes(schemes, project_name=""):
    """从一堆 scheme 里挑出「你自己的 App」，把 Pods 的库排后面。

    实际工程里 78 个 scheme 有 70 多个是 CocoaPods 的库
    （Alamofire、CryptoSwift、SVProgressHUD…），默认选第一个等于选错。
    判据：
      · 带连字符的（Alamofire-Alamofire）是 Pod 的 target，不是 App
      · 名字等于工程名/工程目录名的，几乎肯定是 App
      · 名字里带 Resources / Privacy / Localization / Tests 的，是附属 target
      · 首字母大写的英文单词但很短、又不认识的，仍可能是库 —— 靠"不在已知库里"判断
    """
    KNOWN_POD_HINT = ("Resources", "Privacy", "Localization", "Tests", "UITests",
                      "Bundle", "Framework", "Extension", "Widget", "Watch",
                      "Notification", "Share", "Live Activity")
    app, other = [], []
    pname = (project_name or "").lower()
    for x in schemes:
        if "-" in x or any(h in x for h in KNOWN_POD_HINT):
            other.append(x)
            continue
        if pname and x.lower() == pname:
            app.insert(0, x)          # 和工程同名，最可能是 App
        else:
            app.append(x)
    return app, other


def project_info(entry):
    """读工程的基本信息：bundle id、版本，从 Info.plist / project 里找"""
    d = os.path.dirname(entry)
    out = {"path": d}
    try:
        code, o = sh(["xcodebuild", "-showBuildSettings",
                      "-workspace" if entry.endswith(".xcworkspace") else "-project", entry,
                      "-scheme", "__none__"], timeout=20)
        _ = o
    except Exception:
        pass
    return out


# ══════════════════════════════════════════════════════════════
# 构建 / 安装 / 启动
# ══════════════════════════════════════════════════════════════

def build(entry, kind, scheme, configuration="Debug", destination=None,
          derived=None, timeout=1800):
    """编译。

    destination 不给就**默认模拟器** —— 不给的话 xcodebuild 会去构建真机包，
    然后因为没配签名直接失败（"requires a development team"）。
    日常编译不该卡在签名上。
    """
    if not destination:
        destination = "generic/platform=iOS Simulator"
    flag = "-workspace" if kind == "workspace" else "-project"
    derived = derived or os.path.join(HOME, "DerivedData")
    cmd = ["xcodebuild", flag, entry, "-scheme", scheme,
           "-configuration", configuration, "-derivedDataPath", derived,
           "-quiet"]
    if destination:
        cmd += ["-destination", destination]
    t0 = time.time()
    log(f"编译 {' '.join(cmd[:6])}…")
    code, out = sh(cmd, timeout=timeout)
    secs = round(time.time() - t0, 1)
    ok = code == 0 and "BUILD SUCCEEDED" not in out.upper() is False or code == 0
    # -quiet 下成功不出声，看返回码即可
    ok = (code == 0)
    return {"ok": ok, "seconds": secs, "code": code,
            "error": "" if ok else _short_err(out),
            "derived": derived, "configuration": configuration}


def find_app(derived, scheme, configuration="Debug"):
    """在 DerivedData 里把编出来的 .app 找出来"""
    base = os.path.join(derived, "Build", "Products")
    cands = []
    for root, dirs, files in os.walk(base):
        for d in list(dirs):
            if d.endswith(".app"):
                cands.append(os.path.join(root, d))
    if not cands:
        return None
    # 优先名字接近 scheme 的、优先配置对的
    def key(p):
        n = os.path.basename(p)
        return (0 if scheme.lower() in n.lower() else 1,
                0 if configuration in p else 1, -len(p))
    return sorted(cands, key=key)[0]


def boot_sim(udid):
    code, out = sh(["xcrun", "simctl", "boot", udid], timeout=180)
    # 已经启动会返回非 0，不算错
    sh(["xcrun", "simctl", "bootstatus", udid, "-b"], timeout=240)
    return {"ok": True, "output": out.strip()[-300:]}


def install_app(udid, app_path):
    code, out = sh(["xcrun", "simctl", "install", udid, app_path], timeout=300)
    return {"ok": code == 0, "output": (out or "").strip()[-400:],
            "error": "" if code == 0 else _short_err(out)}


def bundle_id_of(app_path):
    pl = os.path.join(app_path, "Info.plist")
    if not os.path.isfile(pl):
        return None
    code, out = sh(["plutil", "-extract", "CFBundleIdentifier", "raw", "-o", "-", pl],
                   timeout=30)
    return out.strip() if code == 0 else None


def launch_app(udid, bundle_id):
    code, out = sh(["xcrun", "simctl", "launch", udid, bundle_id], timeout=120)
    return {"ok": code == 0, "output": out.strip()[-300:],
            "error": "" if code == 0 else _short_err(out)}


def screenshot(udid, path=None):
    path = path or os.path.join(HOME, "shot-%s.png" % datetime.now().strftime("%H%M%S"))
    code, out = sh(["xcrun", "simctl", "io", udid, "screenshot", path], timeout=120)
    return {"ok": code == 0 and os.path.exists(path), "path": path,
            "error": "" if code == 0 else _short_err(out)}


def start_loghub_capture(udid, process=None):
    """顺手让「代码日志」开始抓这个模拟器 —— 编译跑起来之后立刻能看日志。

    process 传 App 名字：不传的话会把系统所有守护进程的日志都抓进来
    （实测一次 6 万多条），App 自己的日志就淹没了。
    """
    try:
        import urllib.request
        req = urllib.request.Request(
            LOGHUB + "/api/capture/start",
            data=json.dumps({"kind": "simulator", "target": udid,
                             "level": "info", "process": process}).encode(),
            headers={"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        return r
    except Exception as exc:
        return {"ok": False, "message": "代码日志没在跑？%s" % exc}


def pod_install(project_dir):
    if not os.path.isfile(os.path.join(project_dir, "Podfile")):
        return {"ok": False, "error": "这个目录没有 Podfile"}
    t0 = time.time()
    code, out = sh(["pod", "install"], timeout=1800, cwd=project_dir)
    return {"ok": code == 0, "seconds": round(time.time() - t0, 1),
            "error": "" if code == 0 else _short_err(out),
            "tail": "\n".join(out.splitlines()[-8:])}


def archive(entry, kind, scheme, outdir=None):
    flag = "-workspace" if kind == "workspace" else "-project"
    outdir = outdir or os.path.join(HOME, "Archives")
    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H.%M")
    path = os.path.join(outdir, "%s %s.xcarchive" % (scheme, stamp))
    cmd = ["xcodebuild", flag, entry, "-scheme", scheme,
           "-configuration", "Release", "-archivePath", path,
           "-destination", "generic/platform=iOS", "archive"]
    t0 = time.time()
    code, out = sh(cmd, timeout=3600)
    return {"ok": code == 0 and os.path.exists(path), "path": path,
            "seconds": round(time.time() - t0, 1),
            "error": "" if code == 0 else _short_err(out)}


# ══════════════════════════════════════════════════════════════
# 一键流水线（这是这个工具的主入口）
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# 跑测试
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# 各工程的构建产物（扫描 / 清理）
# ══════════════════════════════════════════════════════════════

# 这些目录是构建中间产物：删了只影响下次编译速度，不影响代码
JUNK_DIRS = ("DerivedData", "build", ".build", "Index.noindex",
             "xcuserdata", "Pods.build")


def _has_archive(path):
    """这个目录里有没有 .xcarchive（归档）。

    **`build/` 不等于缓存。** Xcode 默认把归档也放在工程的 build/ 下，
    而归档是产物不是缓存 —— 删了就没了，重新 Archive 也是新的构建。
    我踩过这个坑：把 translate/build 当缓存清了，连带删掉里面
    8 月 27 日的一份 xcarchive。所以扫描时必须把这种目录标出来。
    """
    if path.endswith(".xcarchive"):
        return True
    try:
        for cur, dirs, _files in os.walk(path):
            for d in dirs:
                if d.endswith(".xcarchive"):
                    return True
            if cur.count(os.sep) - path.count(os.sep) > 2:
                dirs[:] = []
    except OSError:
        pass
    return False


def _dir_size(path):
    total = 0
    for cur, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(cur, f))
            except OSError:
                pass
    return total


def _git_ignored(project_dir, rel):
    """问 git：这个目录是不是被忽略了。

    只清被忽略的 —— 没被忽略的可能是有人故意提交的，
    删了会变成一堆 git 改动，那就不该我们动。
    """
    try:
        p = subprocess.run(["git", "check-ignore", "-q", rel], cwd=project_dir,
                           capture_output=True, timeout=15)
        return p.returncode == 0
    except Exception:
        return None                       # 不是 git 仓库，判断不了


def scan_junk(roots=None, extra_dirs=None):
    """扫各工程的构建产物。只报告，不删。"""
    roots = roots or [os.path.expanduser("~/Desktop"), os.path.expanduser("~/Documents")]
    found = []
    seen = set()
    for proj in discover_projects(roots):
        base = proj["dir"]
        if base in seen:
            continue
        seen.add(base)
        for junk in JUNK_DIRS:
            p = os.path.join(base, junk)
            if not os.path.isdir(p):
                continue
            # 跳过嵌套（比如 build/DerivedData）
            if any(os.path.join(base, j) in p for j in JUNK_DIRS if j != junk):
                continue
            size = _dir_size(p)
            if size < 5 * 1024 * 1024:      # 小于 5 MB 的不值得提
                continue
            ign = _git_ignored(base, junk)
            arch = _has_archive(p)
            try:
                days = int((time.time() - os.path.getmtime(p)) / 86400)
            except OSError:
                days = -1
            found.append({
                "project": proj["name"], "dir": base, "sub": junk,
                "path": p, "mb": round(size / 1048576, 1),
                "git_ignored": ign, "days_old": days,
                "has_archive": arch,
                # **带归档的不算"可安全清理"** —— 归档删了就没了
                "safe": (ign is True) and not arch,
                "why_unsafe": ("里面有 .xcarchive 归档，不是缓存，删了不可恢复"
                               if arch else
                               ("git 没忽略，可能是有意保留的"
                                if ign is not True else "")),
            })
    # 也扫我们自己的 DerivedData
    mine = os.path.join(HOME, "DerivedData")
    if os.path.isdir(mine):
        size = _dir_size(mine)
        if size > 5 * 1024 * 1024:
            found.append({"project": "（本工具的）", "dir": HOME, "sub": "DerivedData",
                          "path": mine, "mb": round(size / 1048576, 1),
                          "git_ignored": True, "days_old": 0, "safe": True})
    found.sort(key=lambda x: -x["mb"])
    total = sum(x["mb"] for x in found)
    safe_total = sum(x["mb"] for x in found if x["safe"])
    return {"items": found, "total_mb": round(total, 1),
            "safe_mb": round(safe_total, 1),
            "note": ("只把 git 已忽略、且**不含归档**的目录算作可安全清理。"
                     "编译缓存删了只是下次编译慢；归档删了就没了。")}


def clean_junk(paths, expect_mb=None, max_mb=2048):
    """清理指定的构建产物目录。

    只删**扫描结果里出现过、且 git 确认忽略**的目录 ——
    不接受任意路径，免得被当成删除任意文件的接口。

    还有两道闸，是踩过坑之后加的：
      · **要报出你预计释放多少**（expect_mb），对不上就不动手 ——
        这样"顺手传了个路径"这种误操作会被拦住
      · **单次超过 max_mb（默认 2 GB）必须显式放宽** ——
        大目录删了要重编很久，值得多问一句

    （写这段是因为我自己踩过：本来只想测"会不会拒绝任意路径"，
      结果把 /etc 和一个真实存在的大目录一起传了进去，
      /etc 被拒了，那个 7.7 GB 的 DerivedData 被删了。
      接口本身没做错，但少了一道"你真的知道自己在删多大东西吗"的闸。）
    """
    scan = scan_junk()
    allowed = {x["path"]: x for x in scan["items"]}

    want = [p for p in (paths or []) if p in allowed and allowed[p].get("safe")]
    total_mb = round(sum(allowed[p]["mb"] for p in want), 1)

    if expect_mb is not None:
        try:
            if abs(float(expect_mb) - total_mb) > max(50, total_mb * 0.1):
                return {"ok": False, "freed_mb": 0, "removed": [], "refused": [],
                        "error": ("你说要释放 %.0f MB，但实际选中了 %.0f MB —— "
                                  "对不上，没动手。确认好再来一次。"
                                  % (float(expect_mb), total_mb))}
        except (TypeError, ValueError):
            return {"ok": False, "error": "expect_mb 不是数字"}
    elif total_mb > max_mb:
        return {"ok": False, "freed_mb": 0, "removed": [], "refused": [],
                "error": ("选中了 %.1f GB，超过单次上限 %.1f GB。"
                          "确认要清就带上 expect_mb=%.1f 再来。"
                          % (total_mb / 1024, max_mb / 1024, total_mb))}

    freed, removed, refused = 0, [], []
    for p in paths or []:
        info = allowed.get(p)
        if not info:
            refused.append("%s（不在扫描结果里，拒绝）" % p)
            continue
        if not info.get("safe"):
            refused.append("%s（%s）" % (p, info.get("why_unsafe") or "不安全，不动"))
            continue
        before = _dir_size(p)
        try:
            shutil.rmtree(p, ignore_errors=True)
            after = _dir_size(p) if os.path.isdir(p) else 0
            got = before - after
            freed += got
            removed.append({"path": p, "mb": round(got / 1048576, 1)})
            log(f"清掉构建产物 {p}（{got/1048576:.0f} MB）")
        except Exception as exc:
            refused.append("%s（%s）" % (p, exc))
    return {"ok": True, "freed_mb": round(freed / 1048576, 1),
            "removed": removed, "refused": refused,
            "message": "释放了 %.0f MB" % (freed / 1048576)}


def make_fix_goal(entry, kind, scheme, udid, error_text, project_dir):
    """把「构建失败」转成一个给 AI 的任务描述。

    AI 那边有 run_command / read_file / edit_snippet / check_syntax，
    所以这里只要把上下文给清楚，它自己能接着往下做。
    """
    flag = "workspace" if kind == "workspace" else "project"
    dest = ("id=%s" % udid) if udid else "generic/platform=iOS Simulator"
    return (
        "iOS 工程编译失败了，请帮我修好它。\n\n"
        "工程：%s\n"
        "类型：%s（用 -%s 参数）\n"
        "scheme：%s\n"
        "模拟器目标：%s\n"
        "工程目录：%s\n\n"
        "编译报错（已经摘出来的关键行）：\n%s\n\n"
        "请这样做：\n"
        "1. 先看报错指向哪个文件哪一行，用 read_file 的 around=\"关键词\" 读那一段\n"
        "2. 想清楚为什么错（类型不匹配？少 import？API 用错？）\n"
        "3. 用 edit_snippet 改（find 要唯一，改的理由写进 reason）\n"
        "4. 用 run_command 重新编译验证：\n"
        "   xcodebuild -%s %s -scheme %s -destination '%s' -configuration Debug -quiet\n"
        "5. 还报错就继续改，直到编译通过\n"
        "6. 通过了就 done，说清楚改了哪个文件哪一行、怎么回滚\n\n"
        "注意：只改这个工程里的文件，别动其他工程。"
        % (entry, flag, flag, scheme, dest, project_dir, error_text[:3000],
           flag, entry, scheme, dest)
    )


def derived_size():
    """构建产物占了多少 —— 一次编译能到几百 MB，得让用户看得见、清得掉"""
    d = os.path.join(HOME, "DerivedData")
    if not os.path.isdir(d):
        return 0
    total = 0
    for cur, dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(cur, f))
            except OSError:
                pass
    return total


def clean_derived():
    """清掉本功能自己攒的构建产物。

    只删**自己 DerivedData 目录**里的东西 —— 不碰用户工程里的 build/，
    也不碰 Xcode 全局的 ~/Library/Developer/Xcode/DerivedData。
    """
    d = os.path.join(HOME, "DerivedData")
    if not os.path.isdir(d):
        return {"ok": True, "freed": 0, "message": "没有构建产物"}
    before = derived_size()
    shutil.rmtree(d, ignore_errors=True)
    after = derived_size()
    freed = before - after
    log(f"清理构建产物，释放 {freed/1048576:.1f} MB")
    return {"ok": True, "freed": freed,
            "message": "释放了 %.0f MB（下次编译会重新生成）" % (freed / 1048576)}


def test(entry, kind, scheme, udid=None, timeout=1800, only=None):
    """跑单元测试 / UI 测试。

    xcodebuild test 的输出非常长，这里把结果摘成「过了几个、挂了几个、挂在哪」，
    不甩一屏原文给你。
    """
    flag = "-workspace" if kind == "workspace" else "-project"
    derived = os.path.join(HOME, "DerivedData")
    cmd = ["xcodebuild", flag, entry, "-scheme", scheme,
           "-derivedDataPath", derived, "-destination"]
    dest = ("id=%s" % udid) if udid else "generic/platform=iOS Simulator"
    # 跑测试必须在具体设备上，generic 目标跑不了 test —— 没给就挑一个
    if not udid:
        sims = list_sims()
        if sims:
            dest = "id=%s" % sims[0]["udid"]
    cmd.append(dest)
    if only:
        cmd += ["-only-testing:" + only]
    cmd.append("test")
    t0 = time.time()
    log(f"跑测试 {scheme}…")
    code, out = sh(cmd, timeout=timeout)
    secs = round(time.time() - t0, 1)

    passed = len(re.findall(r"Test Case .* passed", out))
    failed = len(re.findall(r"Test Case .* failed", out))
    # 挂掉的用例名 + 失败原因
    fails = []
    for m in re.finditer(r"Test Case '([^']+)' failed", out):
        fails.append(m.group(1))
    for m in re.finditer(r"^(\S+\.(?:swift|m|mm)):(\d+): error: (.+)$", out, re.M):
        fails.append("%s:%s %s" % (m.group(1), m.group(2), m.group(3)[:120]))
    # 测试总数
    total = 0
    mt = re.search(r"Executed (\d+) tests?, with .*?(\d+) failures?", out)
    if mt:
        total = int(mt.group(1))
    ok = code == 0
    return {"ok": ok, "seconds": secs, "passed": passed, "failed": failed,
            "total": total or (passed + failed),
            "failures": fails[:20],
            "error": "" if ok else _short_err(out, 1000)}


# ══════════════════════════════════════════════════════════════
# 监听改动自动重编译
# ══════════════════════════════════════════════════════════════
_watch = {"thread": None, "stop": None, "state": {}, "running": False}


def _snapshot(project_dir, exts=(".swift", ".m", ".mm", ".h", ".storyboard", ".xib",
                                 ".plist", ".strings", ".json", ".entitlements")):
    """记下源码文件的修改时间 —— 用来判断「有没有人改了代码」"""
    snap = {}
    for cur, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs
                   if d not in ("Pods", "build", "DerivedData", ".git",
                                "node_modules", "Carthage", ".build")
                   and not d.startswith(".")]
        for f in files:
            if not f.endswith(exts):
                continue
            p = os.path.join(cur, f)
            try:
                st = os.stat(p)
                snap[p] = (st.st_mtime, st.st_size)
            except OSError:
                continue
    return snap


def watch_start(entry, kind, scheme, udid=None, interval=2.5, auto_run=False):
    """盯着工程目录，改了源码就自动重编译。

    auto_run=True 时编完还顺手装到模拟器并启动 —— 就是「保存即运行」。
    """
    if _watch["running"]:
        return {"ok": False, "error": "已经在监听了 —— 先停掉再开"}
    project_dir = os.path.dirname(entry)
    stop_ev = threading.Event()
    state = {"changes": 0, "builds": 0, "ok": None, "last": "", "started": _now_str(),
             "message": "已开始监听"}
    _watch.update({"stop": stop_ev, "state": state, "running": True})

    def loop():
        snap = _snapshot(project_dir)
        log(f"开始监听 {project_dir}（{len(snap)} 个源文件）")
        while not stop_ev.is_set():
            time.sleep(interval)
            if stop_ev.is_set():
                break
            now = _snapshot(project_dir)
            changed = [p for p, v in now.items() if snap.get(p) != v]
            changed += [p for p in snap if p not in now]      # 新增/删除也算
            snap = now
            if not changed:
                continue
            names = [os.path.basename(x) for x in changed[:5]]
            state["changes"] += 1
            state["message"] = "检测到改动：%s%s，重新编译…" % (
                "、".join(names), " 等" if len(changed) > 5 else "")
            log(state["message"])
            # 稍等一下，避免一边写一边编（保存大文件时会有多次事件）
            time.sleep(1.0)
            snap = _snapshot(project_dir)
            if auto_run:
                r = run_pipeline(entry, kind, scheme, udid, "Debug", True)
            else:
                r = build(entry, kind, scheme, "Debug",
                          ("id=%s" % udid) if udid else
                          "generic/platform=iOS Simulator")
            state["builds"] += 1
            state["ok"] = bool(r.get("ok"))
            state["message"] = ("✅ 编译通过（%.1fs）" % r.get("seconds", 0)
                                if r.get("ok") else
                                "❌ 编译失败：" + chr(10) + str(r.get("error") or "")[:800])
            state["last"] = _now_str()
            log("自动重编译：" + ("通过" if r.get("ok") else "失败"))
        _watch["running"] = False
        log("停止监听")

    t = threading.Thread(target=loop, daemon=True, name="iosauto-watch")
    _watch["thread"] = t
    t.start()
    return {"ok": True, "message": "开始监听（改了源码会自动重编）",
            "files": len(_snapshot(project_dir))}


def watch_stop():
    if not _watch["running"]:
        return {"ok": False, "error": "没有在监听"}
    ev = _watch.get("stop")
    if ev:
        ev.set()
    _watch["running"] = False
    return {"ok": True, "message": "已停止监听"}


def watch_status():
    st = _watch.get("state") or {}
    return {"running": _watch["running"], **st}


def _now_str():
    return datetime.now().strftime("%H:%M:%S")


def run_pipeline(entry, kind, scheme, udid=None, configuration="Debug",
                 capture_logs=True, do_build=True):
    """编译 → 启动模拟器 → 装 → 跑 → 开始抓日志。一次点击走完日常循环。"""
    steps = []
    t0 = time.time()

    def step(name, fn):
        s0 = time.time()
        try:
            r = fn()
        except Exception as exc:
            r = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        r["step"] = name
        r["seconds"] = round(time.time() - s0, 1)
        steps.append(r)
        log(f"  [{name}] {'✅' if r.get('ok') else '❌'} {r.get('seconds')}s")
        return r

    # 1) 选模拟器
    sims = list_sims()
    if not udid:
        if not sims:
            return {"ok": False, "error": "没有可用的模拟器 —— 在 Xcode 里装一个 iOS 运行时",
                    "steps": steps}
        udid = sims[0]["udid"]
    sim_name = next((s["name"] for s in sims if s["udid"] == udid), udid[:8])

    # 2) 编译
    if do_build:
        r = step("编译", lambda: build(entry, kind, scheme, configuration,
                                      destination="id=%s" % udid))
        if not r.get("ok"):
            # 失败也要记下来 —— 不然"上一次失败了"这个信息就丢了，
            # AI 也就没法基于它给修复方案
            rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "user_id": _user_id(), "entry": entry, "scheme": scheme,
                   "simulator": sim_name, "udid": udid, "ok": False,
                   "seconds": round(time.time() - t0, 1),
                   "steps": [{"step": x["step"], "ok": x.get("ok"),
                              "seconds": x.get("seconds"),
                              "error": str(x.get("error") or "")[:2000]}
                             for x in steps]}
            with open(RUNS, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return {"ok": False, "error": "编译失败", "steps": steps, "udid": udid,
                    "simulator": sim_name, "seconds": round(time.time() - t0, 1)}
        derived = r["derived"]
    else:
        derived = os.path.join(HOME, "DerivedData")

    # 3) 找 .app
    app = find_app(derived, scheme, configuration)
    r = step("找产物", lambda: {"ok": bool(app), "path": app,
                               "error": "" if app else "DerivedData 里没找到 .app，"
                                                       "先编译一次"})
    if not r.get("ok"):
        return {"ok": False, "error": r["error"], "steps": steps,
                "seconds": round(time.time() - t0, 1)}
    bid = bundle_id_of(app)

    # 4) 启动模拟器
    step("启动模拟器", lambda: boot_sim(udid))

    # 5) 装
    r = step("安装", lambda: install_app(udid, app))
    if not r.get("ok"):
        return {"ok": False, "error": "安装失败", "steps": steps, "udid": udid,
                "simulator": sim_name, "seconds": round(time.time() - t0, 1)}

    # 6) 跑
    if bid:
        step("启动 App", lambda: launch_app(udid, bid))

    # 7) 抓日志
    if capture_logs:
        step("开始抓日志", lambda: start_loghub_capture(udid, scheme))

    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "user_id": _user_id(),
           "entry": entry,
           "scheme": scheme, "simulator": sim_name, "udid": udid,
           "bundle": bid, "app": app,
           "seconds": round(time.time() - t0, 1),
           "steps": [{"step": s["step"], "ok": s.get("ok"),
                      "seconds": s.get("seconds")} for s in steps]}
    with open(RUNS, "a", encoding="utf-8") as f:      # 只追加
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"流水线完成：{scheme} → {sim_name}（{rec['seconds']}s）")
    return {"ok": True, "steps": steps, "udid": udid, "simulator": sim_name,
            "bundle": bid, "app": app, "seconds": rec["seconds"],
            "note": "已开始抓日志 —— 去「代码日志」看，或让 AI 分析"}


def run_history(limit=12):
    rows = []
    try:
        with open(RUNS, encoding="utf-8") as f:
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


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "iosauto/" + VERSION

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
                "version": VERSION,
                "xcode": xcode_version(),
                "sims": list_sims()[:40],
                "booted": [s for s in list_sims() if s["booted"]],
                "runs": run_history(8),
                "watch": watch_status(),
                "loghub": LOGHUB,
                "derived_mb": round(derived_size() / 1048576, 1),
                "projects_cache": len(discover_projects()),
            }, ensure_ascii=False))

        if u.path == "/api/projects":
            roots = qs.get("roots")
            if roots:
                roots = [os.path.expanduser(x) for x in roots[0].split(",") if x]
            return self._send(200, json.dumps({"projects": discover_projects(roots)},
                                              ensure_ascii=False))

        if u.path == "/api/schemes":
            entry = os.path.expanduser((qs.get("entry", [""])[0] or "").strip())
            kind = qs.get("kind", ["workspace"])[0]
            if not os.path.exists(entry):
                return self._send(400, json.dumps({"ok": False, "error": "工程不存在"},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(list_schemes(entry, kind), ensure_ascii=False))

        if u.path == "/api/simulators":
            return self._send(200, json.dumps({"sims": list_sims()}, ensure_ascii=False))

        if u.path == "/api/junk":
            """扫各工程的构建产物（只报告）"""
            return self._send(200, json.dumps(scan_junk(), ensure_ascii=False))

        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/run":
            entry = os.path.expanduser((b.get("entry") or "").strip())
            if not os.path.exists(entry):
                return self._send(400, json.dumps({"ok": False, "error": "工程不存在"},
                                                  ensure_ascii=False))
            r = run_pipeline(entry, b.get("kind") or "workspace", b.get("scheme") or "",
                             b.get("udid"), b.get("configuration") or "Debug",
                             capture_logs=b.get("capture_logs", True),
                             do_build=b.get("do_build", True))
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/build":
            entry = os.path.expanduser((b.get("entry") or "").strip())
            r = build(entry, b.get("kind") or "workspace", b.get("scheme") or "",
                      b.get("configuration") or "Debug",
                      b.get("destination"), timeout=int(b.get("timeout") or 1800))
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/archive":
            entry = os.path.expanduser((b.get("entry") or "").strip())
            r = archive(entry, b.get("kind") or "workspace", b.get("scheme") or "")
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/test":
            entry = os.path.expanduser((b.get("entry") or "").strip())
            if not os.path.exists(entry):
                return self._send(400, json.dumps({"ok": False, "error": "工程不存在"},
                                                  ensure_ascii=False))
            r = test(entry, b.get("kind") or "workspace", b.get("scheme") or "",
                     b.get("udid"), int(b.get("timeout") or 1800), b.get("only"))
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/watch/start":
            entry = os.path.expanduser((b.get("entry") or "").strip())
            if not os.path.exists(entry):
                return self._send(400, json.dumps({"ok": False, "error": "工程不存在"},
                                                  ensure_ascii=False))
            r = watch_start(entry, b.get("kind") or "workspace", b.get("scheme") or "",
                            b.get("udid"), float(b.get("interval") or 2.5),
                            bool(b.get("auto_run")))
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/watch/stop":
            return self._send(200, json.dumps(watch_stop(), ensure_ascii=False))

        if u.path == "/api/junk/clean":
            """清理指定的构建产物"""
            r = clean_junk(b.get("paths") or [], b.get("expect_mb"),
                           float(b.get("max_mb") or 2048))
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/fixgoals":
            """把最近一次失败的诊断变成给 AI 的任务描述"""
            runs = run_history(1)
            if not runs:
                return self._send(200, json.dumps(
                    {"ok": False, "error": "还没有跑过流水线"}, ensure_ascii=False))
            last = runs[0]
            failed = [x for x in (last.get("steps") or []) if not x.get("ok")]
            if not failed:
                return self._send(200, json.dumps(
                    {"ok": False, "error": "最近一次是成功的，没有要修的"},
                    ensure_ascii=False))
            err = failed[0].get("error") or failed[0].get("step") or ""
            entry = b.get("entry") or last.get("entry") or ""
            goal = make_fix_goal(entry, b.get("kind") or "workspace",
                                 last.get("scheme") or "", last.get("udid"),
                                 err, os.path.dirname(entry) if entry else "")
            return self._send(200, json.dumps({"ok": True, "goal": goal,
                                               "error": err[:2000]},
                                              ensure_ascii=False))

        if u.path == "/api/clean":
            return self._send(200, json.dumps(clean_derived(), ensure_ascii=False))

        if u.path == "/api/screenshot":
            r = screenshot(b.get("udid") or "")
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/pod":
            d = os.path.expanduser((b.get("dir") or "").strip())
            if not os.path.isdir(d):
                return self._send(400, json.dumps({"ok": False, "error": "目录不存在"},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(pod_install(d), ensure_ascii=False))

        if u.path == "/api/sim":
            """模拟器常用操作"""
            udid = b.get("udid") or ""
            act = b.get("action")
            if act == "boot":
                r = boot_sim(udid)
            elif act == "shutdown":
                code, out = sh(["xcrun", "simctl", "shutdown", udid], timeout=120)
                r = {"ok": code == 0, "output": out.strip()[-200:]}
            elif act == "open":
                subprocess.Popen(["open", "-a", "Simulator"])
                r = {"ok": True, "output": "已打开模拟器窗口"}
            else:
                r = {"ok": False, "error": "不认识的操作"}
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            if p and os.path.exists(p):
                subprocess.Popen(["open", "-R" if os.path.isfile(p) else "", p]
                                 if os.path.isfile(p) else ["open", p])
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
    # 启动时**不要**去调 xcodebuild -version —— 它要好几秒，
    # 会让「一键恢复所有功能」卡在这一步。版本信息改成按需获取并缓存。
    log(f"iOS 开发自动化 v{VERSION} 已启动 http://127.0.0.1:{port}（Xcode 版本按需读取）")

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
    print(f"✅ iOS 开发自动化已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_projects(args):
    ps = discover_projects()
    print("  找到 %d 个 Xcode 工程：" % len(ps))
    for p in ps:
        print("    %-26s %-10s %s%s" % (p["name"][:26], p["kind"],
              os.path.basename(p["entry"]), "  [CocoaPods]" if p["pods"] else ""))
    return 0


def cmd_sims(args):
    for s in list_sims()[:20]:
        print("    %-32s %-14s %s" % (s["name"][:32], s["runtime"],
              "已启动" if s["booted"] else ""))
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="iosauto", description=f"iOS 开发自动化 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sub.add_parser("projects").set_defaults(f=cmd_projects)
    sub.add_parser("sims").set_defaults(f=cmd_sims)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
