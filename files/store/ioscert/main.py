#!/usr/bin/env python3
"""证书与描述文件 —— iOS 签名相关的到期检查。

为什么做这个
  iOS 开发最烦的意外：某天突然构建失败，一查是描述文件过期了，
  或者团队证书被撤销。这些信息系统里都有，但藏得很深：
    · 签名证书：security find-identity
    · 描述文件：~/Library/MobileDevice/Provisioning Profiles/*.mobileprovision
      （是 CMS 包裹的 plist，得先解出来才能读）
  这里一次列全，按到期天数排序，快到期的标红。

零第三方依赖（用系统自带的 security / plutil）。
"""
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("IOSCERT_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "ioscert.pid")
LOGFILE = os.path.join(HOME, "ioscert.log")
DEFAULT_PORT = 8903

PROFILE_DIRS = [
    os.path.expanduser("~/Library/MobileDevice/Provisioning Profiles"),
    os.path.expanduser("~/Library/Developer/Xcode/UserData/Provisioning Profiles"),
]
CERT_DIRS = [os.path.expanduser("~/Library/Keychains")]


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def days_left(dt):
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).days


def level(d):
    if d is None:
        return "unknown"
    if d < 0:
        return "expired"
    if d <= 7:
        return "critical"
    if d <= 30:
        return "warn"
    return "ok"


# ══════════════════════════════════════════════════════════════
# 签名证书
# ══════════════════════════════════════════════════════════════

def list_identities():
    """security find-identity 拿签名身份"""
    out = []
    try:
        p = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"],
                           capture_output=True, text=True, timeout=20, errors="replace")
        text = p.stdout
    except Exception as exc:
        log(f"[cert] find-identity 失败 {exc}")
        return out
    for line in text.splitlines():
        m = re.match(r'\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"([^"]+)"', line)
        if m:
            sha, name = m.group(1), m.group(2)
            # Apple Development: xxx (TEAMID)
            kind, team = "其他", ""
            for k in ("Apple Development", "Apple Distribution", "iPhone Developer",
                      "iPhone Distribution", "Mac Developer", "Developer ID Application",
                      "3rd Party Mac Developer"):
                if name.startswith(k):
                    kind = k
                    break
            tm = re.search(r"\(([A-Z0-9]{10})\)\s*$", name)
            if tm:
                team = tm.group(1)
            out.append({"sha": sha, "name": name, "kind": kind, "team": team,
                        "expires": None, "days": None, "level": "unknown"})
    return out


def all_cert_expiries():
    """一次把钥匙串里所有证书导出成 PEM，解析出「名称 → 到期时间」。

    注意：不能用 `security find-certificate -Z <sha>` 按哈希查 —— 那个 -Z 只是
    "打印哈希"，不是"按哈希查找"，会报 item could not be found。
    正确做法是一次导出全部，再自己配对。
    """
    table = {}
    try:
        p = subprocess.run(["security", "find-certificate", "-a", "-p"],
                           capture_output=True, text=True, timeout=30, errors="replace")
        pems = re.findall(
            r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
            p.stdout, re.S)
    except Exception as exc:
        log(f"[cert] 导出证书失败 {exc}")
        return table

    for pem in pems:
        try:
            r = subprocess.run(["openssl", "x509", "-noout", "-subject", "-enddate"],
                               input=pem, capture_output=True, text=True, timeout=10)
            out = r.stdout
        except Exception:
            continue
        m_sub = re.search(r"subject=\s*(.+)", out)
        m_end = re.search(r"notAfter=(.+)", out)
        if not (m_sub and m_end):
            continue
        subject = m_sub.group(1).strip()
        # CN=xxx 取出来当名字（和 find-identity 显示的名字一致）
        cn = ""
        for part in subject.split("/"):
            if part.strip().startswith("CN="):
                cn = part.strip()[3:]
        if not cn:
            cn = subject
        try:
            dt = datetime.strptime(m_end.group(1).strip(), "%b %d %H:%M:%S %Y %Z")
            dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
        # 同名证书取「到期最晚的那张」—— 通常是你最该关心的那张
        if cn not in table or (dt and table[cn] and dt > table[cn]):
            table[cn] = dt
        if cn not in table:
            table[cn] = dt
    return table


def cert_expiry_by_name(name, table):
    """按名字配对。find-identity 显示的名字就是证书的 CN"""
    if name in table:
        return table[name]
    # 有时代码签名身份名字后面带了空格或描述，做一次宽松匹配
    for cn, dt in table.items():
        if cn in name or name in cn:
            return dt
    return None


# ══════════════════════════════════════════════════════════════
# 描述文件
# ══════════════════════════════════════════════════════════════

def parse_profile(path):
    """解出 .mobileprovision 里的 plist（它是 CMS 包裹的，先剥壳）"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return None
    # 找内嵌的 XML plist
    start = raw.find(b"<?xml")
    end = raw.rfind(b"</plist>")
    if start < 0 or end < 0:
        return None
    blob = raw[start:end + len(b"</plist>")]
    try:
        return plistlib.loads(blob)
    except Exception:
        return None


def list_profiles():
    out = []
    seen = set()
    for d in PROFILE_DIRS:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith((".mobileprovision", ".provisionprofile")):
                continue
            path = os.path.join(d, name)
            key = os.path.basename(path)
            if key in seen:
                continue
            seen.add(key)
            pl = parse_profile(path)
            if not pl:
                out.append({"file": name, "path": path, "broken": "解不出内容"})
                continue
            exp = pl.get("ExpirationDate")
            dleft = days_left(exp)
            ents = pl.get("Entitlements") or {}
            devs = pl.get("ProvisionedDevices") or []
            out.append({
                "file": name,
                "path": path,
                "name": pl.get("Name", ""),
                "uuid": pl.get("UUID", ""),
                "team": pl.get("TeamName", ""),
                "team_id": (pl.get("TeamIdentifier") or [""])[0],
                "app_id": ents.get("application-identifier", ""),
                "bundle": ents.get("application-identifier", "").split(".", 1)[-1],
                "created": pl.get("CreationDate").strftime("%Y-%m-%d") if pl.get("CreationDate") else "",
                "expires": exp.strftime("%Y-%m-%d") if exp else "",
                "days": dleft,
                "level": level(dleft),
                "devices": len(devs),
                "is_enterprise": bool(ents.get("get-task-allow")) and not devs,
                "get_task_allow": bool(ents.get("get-task-allow")),
                "aps": bool(ents.get("aps-environment")),
                "type": ("企业/In-House" if not devs else
                         ("App Store" if not devs else "开发/Ad Hoc")),
            })
    out.sort(key=lambda p: (p.get("days") is None, p.get("days", 9999)))
    return out


def summary(profiles, certs):
    """给一句话结论 —— 用户最想知道的是"有没有要处理的\""""
    issues = []
    for p in profiles:
        d = p.get("days")
        if d is None:
            if p.get("broken"):
                issues.append({"what": f"描述文件「{os.path.basename(p.get('file',''))}」解不出内容",
                               "level": "warn", "detail": p["broken"]})
            continue
        if d < 0:
            issues.append({"what": f"描述文件「{p.get('name') or p['file']}」已过期 {abs(d)} 天",
                           "level": "critical", "detail": f"{p.get('bundle','')} 到期 {p.get('expires')}"})
        elif d <= 7:
            issues.append({"what": f"描述文件「{p.get('name') or p['file']}」{d} 天后过期",
                           "level": "critical", "detail": f"{p.get('bundle','')} 到期 {p.get('expires')}"})
        elif d <= 30:
            issues.append({"what": f"描述文件「{p.get('name') or p['file']}」{d} 天后过期",
                           "level": "warn", "detail": f"{p.get('bundle','')} 到期 {p.get('expires')}"})
    for c in certs:
        d = c.get("days")
        if d is None:
            continue
        if d < 0:
            issues.append({"what": f"签名证书「{c['name']}」已过期", "level": "critical",
                           "detail": f"{abs(d)} 天前到期"})
        elif d <= 30:
            issues.append({"what": f"签名证书「{c['name']}」{d} 天后过期",
                           "level": "warn", "detail": c.get("expires", "")})
    return issues


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ioscert/" + VERSION

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

    def do_GET(self):
        import urllib.parse
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
                "profile_dirs": [d for d in PROFILE_DIRS if os.path.isdir(d)],
                "profile_dir_exists": any(os.path.isdir(d) for d in PROFILE_DIRS),
            }, ensure_ascii=False))

        if u.path == "/api/scan":
            t0 = time.time()
            certs = list_identities()
            table = all_cert_expiries()
            for c in certs:
                exp = cert_expiry_by_name(c["name"], table)
                c["expires"] = exp.strftime("%Y-%m-%d") if exp else ""
                c["days"] = days_left(exp)
                c["level"] = level(c["days"])
            profiles = list_profiles()
            issues = summary(profiles, certs)
            log(f"[scan] 证书 {len(certs)} 张，描述文件 {len(profiles)} 个，"
                f"问题 {len(issues)} 项（{time.time()-t0:.2f}s）")
            return self._send(200, json.dumps({
                "certs": certs, "profiles": profiles, "issues": issues,
                "seconds": round(time.time() - t0, 2),
            }, ensure_ascii=False))

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
    log(f"证书检查 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ 证书检查已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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
    certs = list_identities()
    table = all_cert_expiries()
    for c in certs:
        exp = cert_expiry_by_name(c["name"], table)
        c["expires"] = exp.strftime("%Y-%m-%d") if exp else "?"
        c["days"] = days_left(exp)
    profiles = list_profiles()
    issues = summary(profiles, certs)

    print("═" * 70)
    print("  签名证书（%d 张）" % len(certs))
    print("═" * 70)
    for c in certs or [{"name": "（没有找到签名证书）", "expires": "", "days": None, "team": ""}]:
        d = c.get("days")
        flag = ""
        if d is not None:
            flag = ("❌ 已过期" if d < 0 else
                    f"⚠️ {d} 天" if d <= 30 else f"✅ {d} 天")
        print(f"  {c.get('name','')[:52]:<54} {c.get('expires',''):<12} {flag}")

    print()
    print("═" * 70)
    print("  描述文件（%d 个）" % len(profiles))
    print("═" * 70)
    for p in profiles:
        if p.get("broken"):
            print(f"  ❌ {p['file'][:50]}  {p['broken']}")
            continue
        d = p.get("days")
        flag = ("❌ 已过期 %d 天" % abs(d) if d is not None and d < 0 else
                f"⚠️ 还有 {d} 天" if d is not None and d <= 30 else
                f"✅ {d} 天" if d is not None else "?")
        print(f"  {p.get('name','')[:44]:<46} {p.get('expires',''):<12} {flag}")
        print(f"      bundle {p.get('bundle','')}  设备 {p.get('devices',0)} 个  "
              f"team {p.get('team','')}")

    print()
    if issues:
        print("═" * 70)
        print("  ⚠️ 需要处理（%d 项）" % len(issues))
        print("═" * 70)
        for i in issues:
            mark = "❌" if i["level"] == "critical" else "⚠️"
            print(f"  {mark} {i['what']}")
            if i.get("detail"):
                print(f"      {i['detail']}")
    else:
        print("  ✅ 没有快过期或已过期的东西")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="ioscert", description=f"证书检查 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sub.add_parser("scan").set_defaults(f=cmd_scan)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
