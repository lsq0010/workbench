#!/usr/bin/env python3
"""网络诊断 —— 连不上时按顺序查一遍。

为什么做这个
  抓不到包、设备连不上、接口超时 —— 根因常常在网络这一层，
  但要在终端敲一堆命令（ifconfig / route / dig / nc / ping）。
  这里一次全查完，并给出"哪一步断了"的判断。

诊断顺序（和抓包排查逻辑对齐）
  1. 本机网络：网卡、IP、网关
  2. 出口：能不能到公网
  3. DNS：域名能不能解析
  4. 目标端口：能不能连上
  5. 局域网：目标设备在不在、端口通不通（抓包时最常卡在这）

只用系统自带命令，不联网做任何上报。
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("NETDIAG_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "netdiag.pid")
LOGFILE = os.path.join(HOME, "netdiag.log")
DEFAULT_PORT = 8908


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def sh(cmd, timeout=12):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return f"__ERR__{exc}"


# ══════════════════════════════════════════════════════════════
# 各项检查
# ══════════════════════════════════════════════════════════════

def local_network():
    """本机网卡 / IP / 网关"""
    out = {"interfaces": [], "gateway": "", "dns": [], "wifi": {}}
    try:
        txt = sh(["ifconfig"])
        cur = None
        for line in txt.splitlines():
            m = re.match(r"^(\w+):", line)
            if m:
                cur = m.group(1)
                if cur not in ("lo0",) and not cur.startswith("utun"):
                    out["interfaces"].append({"name": cur, "ip": "", "netmask": "", "mac": ""})
                continue
            if not out["interfaces"]:
                continue
            if cur != out["interfaces"][-1]["name"]:
                continue
            m4 = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
            if m4:
                out["interfaces"][-1]["ip"] = m4.group(1)
            mn = re.search(r"netmask (0x[0-9a-f]+)", line)
            if mn:
                try:
                    out["interfaces"][-1]["netmask"] = socket.inet_ntoa(
                        int(mn.group(1), 16).to_bytes(4, "big"))
                except Exception:
                    pass
            me = re.search(r"ether ([0-9a-f:]+)", line)
            if me:
                out["interfaces"][-1]["mac"] = me.group(1)
        out["interfaces"] = [i for i in out["interfaces"] if i["ip"]]
    except Exception as exc:
        out["error"] = str(exc)

    gw = sh(["route", "-n", "get", "default"])
    m = re.search(r"gateway:\s*(\S+)", gw)
    if m:
        out["gateway"] = m.group(1)
    mi = re.search(r"interface:\s*(\S+)", gw)
    if mi:
        out["gateway_iface"] = mi.group(1)

    ns = sh(["scutil", "--dns"])
    out["dns"] = sorted(set(re.findall(r"nameserver\[\d+\]\s*:\s*(\S+)", ns)))[:6]

    # Wi-Fi 名字（判断是不是连错网 / 访客网络）
    try:
        wifi = sh(["networksetup", "-getairportnetwork", "en0"])
        m = re.search(r"Current Wi-Fi Network:\s*(.+)", wifi)
        if m:
            out["wifi"]["ssid"] = m.group(1).strip()
        else:
            out["wifi"]["note"] = wifi.strip()[:80]
    except Exception:
        pass
    # 本机对外 IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        out["lan_ip"] = s.getsockname()[0]
        s.close()
    except Exception:
        out["lan_ip"] = ""
    return out


def ping(host, count=2, timeout=3):
    t0 = time.time()
    r = sh(["ping", "-c", str(count), "-t", str(timeout), host], timeout=timeout * count + 4)
    ok = " 0.0% packet loss" in r or " 0% packet loss" in r
    times = re.findall(r"time=([\d.]+)", r)
    return {"ok": ok, "host": host, "seconds": round(time.time() - t0, 2),
            "avg_ms": round(sum(float(x) for x in times) / len(times), 1) if times else None,
            "raw": r.strip().splitlines()[-3:] if not ok else []}


def dns_lookup(name):
    t0 = time.time()
    r = sh(["dig", "+short", "+time=3", "+tries=1", name], timeout=10)
    ips = [x.strip() for x in r.splitlines() if re.match(r"^\d+\.\d+\.\d+\.\d+$", x.strip())]
    if not ips:
        r2 = sh(["nslookup", name], timeout=10)
        ips = re.findall(r"Address:\s*(\d+\.\d+\.\d+\.\d+)", r2)[1:]
    return {"ok": bool(ips), "host": name, "ips": ips[:6],
            "seconds": round(time.time() - t0, 2)}


def tcp_connect(host, port, timeout=4):
    t0 = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"ok": True, "host": host, "port": port,
                    "ms": round((time.time() - t0) * 1000)}
    except socket.timeout:
        return {"ok": False, "host": host, "port": port, "error": "超时（可能被防火墙或 AP 隔离挡住）"}
    except ConnectionRefusedError:
        return {"ok": False, "host": host, "port": port, "error": "拒绝连接（端口没人监听）"}
    except Exception as exc:
        return {"ok": False, "host": host, "port": port, "error": str(exc)}


def http_probe(url, timeout=8):
    """不只是 TCP 通，还看 HTTP 层通不通（走不走代理都试）"""
    r = sh(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
            "-m", str(timeout), url], timeout=timeout + 3)
    m = re.match(r"(\d{3})\s+([\d.]+)", r.strip())
    if m:
        return {"ok": True, "url": url, "status": int(m.group(1)),
                "seconds": float(m.group(2))}
    return {"ok": False, "url": url, "error": r.strip()[:120] or "连不上"}


def lan_scan(subnet=None, timeout=0.35):
    """扫同网段，看有哪些设备活着（抓包时确认手机在不在）"""
    if not subnet:
        ip = local_network().get("lan_ip") or ""
        subnet = ".".join(ip.split(".")[:3]) if ip.count(".") == 3 else ""
    if not subnet:
        return {"error": "拿不到本机网段"}
    hosts = [f"{subnet}.{i}" for i in range(1, 255)]
    alive = []
    import concurrent.futures as cf

    def one(h):
        try:
            p = subprocess.run(["ping", "-c", "1", "-W", "300", h],
                               capture_output=True, text=True, timeout=2)
            if p.returncode == 0:
                return h
        except Exception:
            pass
        return None

    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        for r in ex.map(one, hosts):
            if r:
                alive.append(r)
    # ARP 表补充（有些设备不响应 ping）
    arp = sh(["arp", "-an"])
    arp_hosts = re.findall(r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-f:]+)", arp)
    macs = {ip: mac for ip, mac in arp_hosts}
    for ip, _ in arp_hosts:
        if ip.startswith(subnet + ".") and ip not in alive:
            alive.append(ip)
    return {"subnet": subnet + ".0/24", "alive": sorted(alive, key=lambda x: int(x.split(".")[-1])),
            "count": len(alive), "macs": macs}


def trace(host, max_hops=12):
    """路由追踪（看在哪一跳断的）"""
    r = sh(["traceroute", "-m", str(max_hops), "-w", "1", "-q", "1", host], timeout=40)
    hops = []
    for line in r.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        hops.append({"hop": parts[0], "host": parts[1] if len(parts) > 1 else "*",
                     "ms": parts[2] if len(parts) > 2 else ""})
    return {"host": host, "hops": hops[:max_hops]}


# ══════════════════════════════════════════════════════════════
# 一键全查
# ══════════════════════════════════════════════════════════════

def full_diagnose(target=None, port=None):
    """按顺序查一遍，给出「哪一步断了」"""
    t0 = time.time()
    steps = []

    net = local_network()
    ok_net = bool(net.get("lan_ip"))
    steps.append({"name": "本机网络", "ok": ok_net,
                  "detail": f"IP {net.get('lan_ip','?')}　网关 {net.get('gateway','?')}"
                            + (f"　Wi-Fi {net['wifi'].get('ssid','')}" if net.get("wifi", {}).get("ssid") else "")})

    if not ok_net:
        steps.append({"name": "出口", "ok": False, "detail": "本机没有网络，后面的不用查了"})
        return {"steps": steps, "network": net, "seconds": round(time.time() - t0, 2),
                "verdict": "本机没连上网 —— 先连 Wi-Fi 或有线"}

    p1 = ping("223.5.5.5", 2, 3)
    steps.append({"name": "出口（公网）", "ok": p1["ok"],
                  "detail": (f"通，{p1['avg_ms']} ms" if p1["ok"]
                             else "不通 —— 可能路由器/上级网络有问题")})

    d1 = dns_lookup("www.baidu.com")
    steps.append({"name": "DNS 解析", "ok": d1["ok"],
                  "detail": (f"www.baidu.com → {d1['ips'][0]}" if d1["ok"]
                             else f"解析不了 —— DNS 服务器 {', '.join(net.get('dns', [])[:2]) or '未配置'}")})

    if target:
        p2 = ping(target, 2, 3)
        steps.append({"name": f"目标可达（{target}）", "ok": p2["ok"],
                      "detail": (f"通，{p2['avg_ms']} ms" if p2["ok"] else "ping 不通")})
        if port:
            c = tcp_connect(target, port)
            steps.append({"name": f"目标端口（{target}:{port}）", "ok": c["ok"],
                          "detail": (f"能连上，{c['ms']} ms" if c["ok"] else c.get("error", ""))})

    # 判断
    verdict = ""
    if not p1["ok"]:
        verdict = "连公网都不通 —— 检查路由器或上游"
    elif not d1["ok"]:
        verdict = "出口通但 DNS 不通 —— 换个 DNS（如 223.5.5.5）试试"
    elif target and port and not any(s["name"].startswith("目标端口") and s["ok"] for s in steps):
        verdict = (f"{target}:{port} 连不上 —— 如果这是局域网设备，"
                   f"最常见的原因是路由器开了「AP 隔离」或设备不在同一网络")
    elif target and not any(s["name"].startswith("目标可达") and s["ok"] for s in steps):
        # ping 不通 ≠ 不可达 —— 很多服务器禁 ICMP 但端口是开的。
        # 这种情况下别吓人，说清楚。
        port_ok = any(s["name"].startswith("目标端口") and s["ok"] for s in steps)
        if port_ok:
            verdict = (f"ping 不到 {target}，但端口是通的 —— 对方只是禁了 ICMP，"
                       f"实际可访问，不用管 ping")
        else:
            verdict = f"ping 不到 {target} —— 它可能不在这个网段，或禁了 ICMP（换个端口/接口再试）"
    else:
        verdict = "网络这几层都正常 —— 问题可能在应用层（看抓包工具的日志）"

    return {"steps": steps, "network": net, "ping_gw": p1, "dns": d1,
            "seconds": round(time.time() - t0, 2), "verdict": verdict}


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "netdiag/" + VERSION

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
            net = local_network()
            return self._send(200, json.dumps({"version": VERSION, "network": net},
                                              ensure_ascii=False))
        if u.path == "/api/local":
            return self._send(200, json.dumps(local_network(), ensure_ascii=False))
        if u.path == "/api/lan":
            t0 = time.time()
            r = lan_scan(qs.get("subnet", [""])[0] or None)
            r["seconds"] = round(time.time() - t0, 2)
            log(f"[lan] {r.get('subnet')} → {r.get('count')} 台（{r['seconds']}s）")
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if u.path == "/api/trace":
            h = (qs.get("host", [""])[0] or "").strip()
            if not h:
                return self._send(400, json.dumps({"error": "给个域名或 IP"}, ensure_ascii=False))
            return self._send(200, json.dumps(trace(h), ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        import urllib.parse
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/diagnose":
            target = (b.get("target") or "").strip() or None
            port = b.get("port")
            try:
                port = int(port) if port else None
            except Exception:
                port = None
            r = full_diagnose(target, port)
            log(f"[diagnose] target={target} port={port} → {r['verdict'][:40]}")
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if u.path == "/api/ping":
            h = (b.get("host") or "").strip()
            if not h:
                return self._send(400, json.dumps({"error": "给个域名或 IP"}, ensure_ascii=False))
            return self._send(200, json.dumps(ping(h, 3, 3), ensure_ascii=False))
        if u.path == "/api/port":
            h = (b.get("host") or "").strip()
            try:
                p = int(b.get("port") or 0)
            except Exception:
                p = 0
            if not h or not p:
                return self._send(400, json.dumps({"error": "要主机和端口"}, ensure_ascii=False))
            return self._send(200, json.dumps(tcp_connect(h, p), ensure_ascii=False))
        if u.path == "/api/dns":
            h = (b.get("host") or "").strip()
            if not h:
                return self._send(400, json.dumps({"error": "给个域名"}, ensure_ascii=False))
            return self._send(200, json.dumps(dns_lookup(h), ensure_ascii=False))
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
    log(f"网络诊断 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ 网络诊断已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_check(args):
    r = full_diagnose(args.target, args.port)
    for s in r["steps"]:
        print(f"  {'✅' if s['ok'] else '❌'} {s['name']:<22} {s['detail']}")
    print()
    print(f"  结论：{r['verdict']}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="netdiag", description=f"网络诊断 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("check"); sp.add_argument("target", nargs="?")
    sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_check)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
