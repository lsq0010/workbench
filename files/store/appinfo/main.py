#!/usr/bin/env python3
"""App 包检查 —— 看 .app / .ipa 里到底是什么。

为什么做这个
  发版前常要核对：版本号填对没、entitlements 有没有多余权限、
  包怎么这么大（哪个 framework 占的）、支持的设备架构对不对。
  Xcode 里翻这些要好几层，这里一次列全。

能读 .app 目录和 .ipa 压缩包（ipa 就是 zip，里面是 Payload/xxx.app）。
只读，不改动任何包。
"""
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("APPINFO_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "appinfo.pid")
LOGFILE = os.path.join(HOME, "appinfo.log")
DEFAULT_PORT = 8907

# entitlement 里常见项的中文说明，帮用户判断"这个权限该不该有"
ENT_NOTES = {
    "get-task-allow": "允许调试附加 —— **发布包不该有这一项**",
    "aps-environment": "推送通知",
    "com.apple.developer.associated-domains": "通用链接（Universal Links）",
    "com.apple.security.application-groups": "App Groups（和扩展共享数据）",
    "com.apple.developer.icloud-container-identifiers": "iCloud",
    "com.apple.developer.in-app-payments": "Apple Pay",
    "com.apple.developer.healthkit": "HealthKit",
    "com.apple.developer.homekit": "HomeKit",
    "com.apple.developer.nfc.readersession.formats": "NFC",
    "com.apple.developer.networking.vpn.api": "VPN",
    "com.apple.developer.pass-type-identifiers": "Wallet 卡券",
    "com.apple.developer.siri": "Siri",
    "com.apple.developer.ubiquity-kvstore-identifier": "iCloud 键值存储",
    "application-identifier": "应用标识（TeamID.BundleID）",
    "keychain-access-groups": "钥匙串共享组",
    "com.apple.developer.team-identifier": "开发团队",
}


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def find_app(path):
    """给定 .app / .ipa / 目录，找出真正的 .app 目录"""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return None, None, f"路径不存在：{path}"
    if path.endswith(".app") and os.path.isdir(path):
        return path, None, None
    if path.endswith(".ipa") and os.path.isfile(path):
        return None, path, None
    # 目录里找 .app
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.endswith(".app"):
                return os.path.join(path, name), None, None
        for name in sorted(os.listdir(path)):
            if name.endswith(".ipa"):
                return None, os.path.join(path, name), None
    return None, None, "这里没有 .app 或 .ipa"


def dir_size(path):
    total = 0
    for cur, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(cur, f))
            except OSError:
                pass
    return total


def breakdown(app_dir, limit=25):
    """包内体积构成：主二进制、framework、资源目录"""
    out = []
    try:
        entries = list(os.scandir(app_dir))
    except Exception:
        return out
    for e in entries:
        try:
            if e.is_file(follow_symlinks=False):
                sz = e.stat(follow_symlinks=False).st_size
                kind = ("主二进制" if os.path.splitext(e.name)[0] ==
                        os.path.splitext(os.path.basename(app_dir))[0] else "文件")
                if e.name in ("embedded.mobileprovision",):
                    kind = "描述文件"
                out.append({"name": e.name, "size": sz, "kind": kind})
            elif e.is_dir(follow_symlinks=False):
                sz = dir_size(e.path)
                kind = ("框架" if e.name == "Frameworks" else
                        "插件" if e.name == "PlugIns" else
                        "资源" if e.name.endswith(".bundle") else "目录")
                out.append({"name": e.name, "size": sz, "kind": kind})
        except OSError:
            continue
    out.sort(key=lambda x: -x["size"])
    total = sum(x["size"] for x in out) or 1
    for x in out:
        x["size_h"] = human(x["size"])
        x["pct"] = round(x["size"] / total * 100, 1)
    return out[:limit]


def arch_of(binary):
    """二进制支持哪些架构"""
    try:
        p = subprocess.run(["lipo", "-archs", binary], capture_output=True, text=True,
                           timeout=15, errors="replace")
        if p.returncode == 0:
            return p.stdout.strip().split()
        p2 = subprocess.run(["file", binary], capture_output=True, text=True, timeout=10,
                            errors="replace")
        m = re.findall(r"(arm64|armv7|x86_64|arm64e|i386)", p2.stdout)
        return sorted(set(m))
    except Exception:
        return []


def frameworks(app_dir):
    fw_dir = os.path.join(app_dir, "Frameworks")
    out = []
    if not os.path.isdir(fw_dir):
        return out
    try:
        for name in sorted(os.listdir(fw_dir)):
            p = os.path.join(fw_dir, name)
            sz = dir_size(p) if os.path.isdir(p) else os.path.getsize(p)
            item = {"name": name, "size": sz, "size_h": human(sz)}
            binp = os.path.join(p, os.path.splitext(name)[0]) if os.path.isdir(p) else p
            if os.path.exists(binp):
                item["archs"] = arch_of(binp)
            out.append(item)
    except Exception:
        pass
    out.sort(key=lambda x: -x["size"])
    return out


def inspect(app_dir=None, ipa=None):
    tmp = None
    try:
        if ipa:
            tmp = tempfile.mkdtemp(prefix="appinfo-")
            with zipfile.ZipFile(ipa) as z:
                names = [n for n in z.namelist() if n.startswith("Payload/") and ".app/" in n]
                if not names:
                    return {"error": "这个 ipa 里没有 Payload/*.app"}
                app_name = names[0].split("/")[1]
                # 只解出需要的文件，不整个解压（ipa 可能很大）
                need = [n for n in z.namelist()
                        if n.startswith(f"Payload/{app_name}/") and
                        (n.count("/") == 2 or n.endswith("Info.plist") or
                         n.endswith("embedded.mobileprovision"))]
                z.extractall(tmp, members=need)
            app_dir = os.path.join(tmp, "Payload", app_name)

        info_path = os.path.join(app_dir, "Info.plist")
        if not os.path.exists(info_path):
            return {"error": "这个 .app 里没有 Info.plist"}
        with open(info_path, "rb") as f:
            info = plistlib.load(f)

        exec_name = info.get("CFBundleExecutable", "")
        binary = os.path.join(app_dir, exec_name)
        bin_size = os.path.getsize(binary) if os.path.exists(binary) else 0

        ents = {}
        ent_file = os.path.join(app_dir, "archived-expanded-entitlements.xcent")
        # 从二进制里读 entitlements（发布包最准的来源）
        if os.path.exists(binary):
            try:
                p = subprocess.run(["codesign", "-d", "--entitlements", ":-", binary],
                                   capture_output=True, text=True, timeout=20, errors="replace")
                m = re.search(r"<\?xml.*?</plist>", p.stdout + p.stderr, re.S)
                if m:
                    ents = plistlib.loads(m.group(0).encode())
            except Exception:
                pass

        # 签名信息
        sign = {}
        if os.path.exists(binary):
            try:
                p = subprocess.run(["codesign", "-dv", binary], capture_output=True, text=True,
                                   timeout=20, errors="replace")
                txt = p.stdout + p.stderr
                m = re.search(r"Authority=(.+)", txt)
                if m:
                    sign["authority"] = m.group(1).strip()
                m = re.search(r"TeamIdentifier=(.+)", txt)
                if m:
                    sign["team"] = m.group(1).strip()
                sign["adhoc"] = "Signature=adhoc" in txt
                m = re.search(r"Timestamp=(.+)", txt)
                if m:
                    sign["timestamp"] = m.group(1).strip()
            except Exception:
                pass

        # 描述文件
        prov = {}
        pp = os.path.join(app_dir, "embedded.mobileprovision")
        if os.path.exists(pp):
            try:
                raw = open(pp, "rb").read()
                s = raw.find(b"<?xml"); e = raw.rfind(b"</plist>")
                if s >= 0 and e >= 0:
                    pl = plistlib.loads(raw[s:e + 8])
                    exp = pl.get("ExpirationDate")
                    prov = {
                        "name": pl.get("Name", ""),
                        "team": pl.get("TeamName", ""),
                        "expires": exp.strftime("%Y-%m-%d") if exp else "",
                        "days": (exp - datetime.now(exp.tzinfo)).days if exp else None,
                        "devices": len(pl.get("ProvisionedDevices") or []),
                        "type": "企业/In-House" if not (pl.get("ProvisionedDevices") or [])
                                and pl.get("ProvisionsAllDevices") else
                                ("Ad Hoc/开发" if pl.get("ProvisionedDevices") else "App Store"),
                    }
            except Exception:
                pass

        total = dir_size(app_dir)
        ents_zh = [{"key": k, "value": (v if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False)[:120]),
                    "note": ENT_NOTES.get(k, "")}
                   for k, v in sorted(ents.items()) if not k.startswith("com.apple.private")]

        warn = []
        if ents.get("get-task-allow"):
            warn.append({"level": "critical",
                         "what": "包含 get-task-allow —— 这是调试权限，**发布包不该有**",
                         "detail": "用 Release 配置 + 发布描述文件重新打包"})
        d = prov.get("days")
        if d is not None and d < 0:
            warn.append({"level": "critical", "what": f"内嵌描述文件已过期 {abs(d)} 天",
                         "detail": prov.get("name", "")})
        elif d is not None and d <= 30:
            warn.append({"level": "warn", "what": f"内嵌描述文件 {d} 天后过期",
                         "detail": prov.get("name", "")})
        if sign.get("adhoc"):
            warn.append({"level": "warn", "what": "签名是 adhoc（没有正式证书）",
                         "detail": "只能装到已注册设备，不能上架"})

        return {
            "ok": True,
            "path": app_dir,
            "name": info.get("CFBundleDisplayName") or info.get("CFBundleName", ""),
            "bundle_id": info.get("CFBundleIdentifier", ""),
            "version": info.get("CFBundleShortVersionString", ""),
            "build": info.get("CFBundleVersion", ""),
            "min_os": info.get("MinimumOSVersion", ""),
            "platform": info.get("DTPlatformName", ""),
            "sdk": info.get("DTSDKName", ""),
            "executable": exec_name,
            "binary_size": bin_size,
            "binary_size_h": human(bin_size),
            "total": total,
            "total_h": human(total),
            "archs": arch_of(binary) if os.path.exists(binary) else [],
            "sign": sign,
            "provision": prov,
            "entitlements": ents_zh,
            "warnings": warn,
            "breakdown": breakdown(app_dir),
            "frameworks": frameworks(app_dir)[:20],
            "url_schemes": [s for d2 in (info.get("CFBundleURLTypes") or [])
                            for s in (d2.get("CFBundleURLSchemes") or [])],
            "ats": bool((info.get("NSAppTransportSecurity") or {}).get("NSAllowsArbitraryLoads")),
        }
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "appinfo/" + VERSION

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
            return self._send(200, json.dumps({
                "version": VERSION,
                "recent": recent_builds(),
            }, ensure_ascii=False))

        if u.path == "/api/inspect":
            p = (qs.get("path", [""])[0] or "").strip()
            if not p:
                return self._send(400, json.dumps({"error": "给个 .app 或 .ipa 的路径"},
                                                  ensure_ascii=False))
            app, ipa, err = find_app(p)
            if err:
                return self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            t0 = time.time()
            r = inspect(app, ipa)
            r["seconds"] = round(time.time() - t0, 2)
            log(f"[inspect] {os.path.basename(p)} → {'ok' if r.get('ok') else r.get('error')}")
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/folder":
            path = (qs.get("path", [os.path.expanduser("~/Desktop")])[0] or "").strip()
            try:
                items = []
                for name in sorted(os.listdir(path)):
                    if name.startswith("."):
                        continue
                    p = os.path.join(path, name)
                    if os.path.isdir(p):
                        items.append({"name": name, "path": p, "app": name.endswith(".app")})
                return self._send(200, json.dumps({"path": path, "items": items[:200],
                                                   "parent": os.path.dirname(path)},
                                                  ensure_ascii=False))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        return self._send(404, json.dumps({"error": "not found"}))


def recent_builds():
    """自动找桌面/Xcode 归档里最近的 .app / .ipa，省得用户翻路径"""
    cands = []
    roots = [os.path.expanduser("~/Desktop"), os.path.expanduser("~/Library/Developer/Xcode/Archives")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for cur, dirs, files in os.walk(root):
            if cur.count("/") - root.count("/") > 4:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.endswith(".ipa"):
                    p = os.path.join(cur, f)
                    try:
                        cands.append({"path": p, "name": f, "kind": "ipa",
                                      "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
                    except OSError:
                        pass
            for d in list(dirs):
                if d.endswith(".app"):
                    p = os.path.join(cur, d)
                    try:
                        cands.append({"path": p, "name": d, "kind": "app",
                                      "size": dir_size(p), "mtime": os.path.getmtime(p)})
                    except OSError:
                        pass
    cands.sort(key=lambda x: -x["mtime"])
    for c in cands[:12]:
        c["size_h"] = human(c["size"])
        c["mtime_h"] = datetime.fromtimestamp(c["mtime"]).strftime("%m-%d %H:%M")
    return cands[:12]


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
    log(f"App 包检查 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ App 包检查已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_inspect(args):
    app, ipa, err = find_app(args.path)
    if err:
        print("❌ " + err)
        return 1
    r = inspect(app, ipa)
    if r.get("error"):
        print("❌ " + r["error"])
        return 1
    print("═" * 68)
    print(f"  {r['name']}  {r['version']} ({r['build']})")
    print("═" * 68)
    print(f"  Bundle ID : {r['bundle_id']}")
    print(f"  最低系统  : iOS {r['min_os']}")
    print(f"  体积      : {r['total_h']}（主二进制 {r['binary_size_h']}）")
    print(f"  架构      : {', '.join(r['archs']) or '?'}")
    print(f"  签名      : {r['sign'].get('authority','?')}")
    if r["provision"]:
        p = r["provision"]
        print(f"  描述文件  : {p.get('name','')}  到期 {p.get('expires','')} "
              f"（{p.get('days')} 天）")
    print(f"  URL Scheme: {', '.join(r['url_schemes']) or '—'}")
    if r["ats"]:
        print("  ⚠️ 允许任意 HTTP 加载（NSAllowsArbitraryLoads）")
    for w in r["warnings"]:
        print(f"  {'❌' if w['level']=='critical' else '⚠️'} {w['what']}")
    print()
    print("  ── 体积构成 ──")
    for x in r["breakdown"][:12]:
        print(f"    {x['size_h']:>9}  {x['pct']:>5}%  [{x['kind']}] {x['name']}")
    if r["entitlements"]:
        print()
        print("  ── 权限 ──")
        for e in r["entitlements"]:
            note = f"  ← {e['note']}" if e["note"] else ""
            print(f"    {e['key']}{note}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="appinfo", description=f"App 包检查 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("inspect"); sp.add_argument("path"); sp.set_defaults(f=cmd_inspect)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
