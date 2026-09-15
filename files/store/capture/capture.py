#!/usr/bin/env python3
"""
capture —— 抓包工作台（HTTP/HTTPS 代理 + 命令行工具）

设计要点
--------
* 零依赖：只用 Python 标准库，macOS / Linux 都能跑
* 可移植：数据默认放 ~/.capture，装到别人机器上也能用
* 隐私优先：authorization / cookie 等敏感头默认脱敏后才落盘
* 可自查：cap doctor 一条命令定位"为什么抓不到"
* 对 AI 友好：所有命令支持 --json，数据是追加式 JSONL

快速上手
--------
    python3 capture.py start          # 启动代理（后台）
    python3 capture.py status         # 看状态 + 手机代理怎么填
    python3 capture.py doctor         # 抓不到时先跑这个
    python3 capture.py list --last 20 # 看最近请求
    python3 capture.py show 42        # 看某条的完整请求/响应
"""

import argparse
import gzip
import http.client
import json
import os
import queue
import re
import select
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler

VERSION = "1.0.1"
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

DEFAULT_PORT = 8890
MAX_BODY = 400_000                    # 单条正文最多记 40 万字符
MAX_LOG_BYTES = 80 * 1024 * 1024      # 日志超 80MB 自动轮转

# 默认脱敏的请求/响应头（多用户环境下的隐私保护）
REDACT_HEADERS = {
    "authorization", "cookie", "set-cookie", "proxy-authorization",
    "x-auth-token", "x-token", "token", "x-access-token", "refresh-token",
}

# 系统/浏览器杂音，默认折叠（不影响业务排查）
NOISE_HOSTS = (
    "googleapis.com", "google.com", "gstatic.com", "googleusercontent.com",
    "miui.com", "xiaomi.com", "apple.com", "icloud.com", "mzstatic.com",
    "doubleclick.net", "facebook.com", "crashlytics.com", "bugly.qq.com",
    "umeng.com", "umengcloud.com", "gvt1.com", "gvt2.com", "gvt3.com",
)

# 联网探测域名 → 平台。设备刚连上 WiFi 就会访问这些，是"连上但没发业务请求"时的识别依据
PROBE_HOSTS = {
    "captive.apple.com": "iOS / macOS",
    "www.apple.com": "iOS / macOS",
    "connect.rom.miui.com": "Android（小米）",
    "connectivitycheck.gstatic.com": "Android",
    "android.googleapis.com": "Android",
    "connectivitycheck.platform.hicloud.com": "Android（华为）",
    "connectivitycheck.platform.hihonorcloud.com": "Android（荣耀）",
    "wifi-test.samsungcloud.com": "Android（三星）",
    "connect.qualcomm.com": "Android",
    "www.msftconnecttest.com": "Windows",
    "www.msftncsi.com": "Windows",
    "connectivity-check.ubuntu.com": "Linux",
}

# 业务链路不再硬编码在这里 —— 搬进了 semantics.json（数据，不是代码）。
# 界面、CLI、AI 都读同一份，避免"同一个字段两处含义不同"。
CHAINS = {}          # 由 load_semantics() 填充，见文件下方


# ══════════════════════════════════════════════════════════════
# 路径与配置
# ══════════════════════════════════════════════════════════════

def resolve_home():
    """数据目录 = 程序所在目录（可移植）；CAPTURE_HOME 可覆盖。

    这样"便携文件夹"和"装到 ~/.local/share"两种用法都只需一个规则，
    不会出现程序在 A、数据在 B 的割裂。
    """
    return os.path.abspath(os.environ.get("CAPTURE_HOME") or SCRIPT_DIR)


HOME = resolve_home()
FLOWS = os.path.join(HOME, "flows.jsonl")
PIDFILE = os.path.join(HOME, "capture.pid")
LOGFILE = os.path.join(HOME, "capture.log")
TRACEFILE = os.path.join(HOME, "trace.log")
CONFIGFILE = os.path.join(HOME, "config.json")

DEFAULT_CONFIG = {"port": DEFAULT_PORT, "web_port": 8891, "lan_web_port": 8892,
                  "redact": True, "noise": True, "access_control": True,
                  "monitored": [], "debug": True,
                  # ── AI（key 不在这里，单独存 credentials.json）──
                  "ai_provider": "deepseek",
                  "ai_base_url": "https://api.deepseek.com/v1",
                  "ai_model": "deepseek-chat",
                  "ai_enabled": False,
                  "ai_max_records": 80}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIGFILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(HOME, exist_ok=True)
    with open(CONFIGFILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    _debug_on["v"] = None              # 配置变了，debug 开关缓存作废


# ══════════════════════════════════════════════════════════════
# 语义层（semantics.json）
#
# 「这个字段是什么意思」是买不到的东西 —— 报文里只有 {"areaCode":"NGA00001"}，
# 人贴上"目的网点"这个标签才有意义，而 AI 推不出来。所以语义必须是**数据**，
# 不能散在代码、README 和对话里。界面 / CLI / AI 都读这一份。
# ══════════════════════════════════════════════════════════════

SEMANTICSFILE = os.path.join(HOME, "semantics.json")
_sem_cache = {"sig": None, "data": {}}


def load_semantics():
    """读语义文件（带缓存）。文件不在时返回空壳，不能让主流程挂掉。"""
    try:
        st = os.stat(SEMANTICSFILE)
        sig = (st.st_mtime_ns, st.st_size)
        if _sem_cache["sig"] == sig:
            return _sem_cache["data"]
    except Exception:
        return {"version": 0, "chains": {}, "fields": {}, "apis": {}, "_缺失": True}
    try:
        with open(SEMANTICSFILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        dbg("SEM", "解析失败", f"{type(exc).__name__}: {exc}")
        return {"version": 0, "chains": {}, "fields": {}, "apis": {}, "_错误": str(exc)}
    _sem_cache["sig"] = sig
    _sem_cache["data"] = data
    return data


def save_semantics(data):
    """写回语义文件（AI 核对出新语义后调用）"""
    data["updated"] = datetime.now().strftime("%Y-%m-%d")
    with open(SEMANTICSFILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _sem_cache["sig"] = None
    dbg("SEM", "写入语义", f"字段 {len(data.get('fields', {}))} 个",
        接口=len(data.get("apis", {})), 链路=list(data.get("chains", {})))
    return data


def sem_count(section):
    """数一个语义分区里真正的条目（排除 _comment 这类元数据键）"""
    d = load_semantics().get(section) or {}
    return len([k for k in d if not k.startswith("_")])


def field_meaning(name):
    """查一个业务字段的含义（给 AI 和界面用）"""
    f = (load_semantics().get("fields") or {}).get(name)
    if not f:
        return None
    bits = [f.get("含义", "")]
    if f.get("单位"):
        bits.append(f"单位 {f['单位']}")
    if f.get("取值"):
        bits.append("取值 " + json.dumps(f["取值"], ensure_ascii=False))
    if f.get("备注"):
        bits.append(f["备注"])
    return "；".join(x for x in bits if x)


def reload_chains():
    """把 semantics.json 里的链路同步到内存（CLI 也要用）"""
    global CHAINS
    chains = load_semantics().get("chains") or {}
    CHAINS = {name: [(e.get("match"), e.get("label"))
                     for e in steps if isinstance(e, dict)]
              for name, steps in chains.items() if not name.startswith("_")}
    return CHAINS


# ══════════════════════════════════════════════════════════════
# 身份层（identity.json）
#
# 每条数据属于谁 —— 这是"数据能不能成为资产"的分界线。
# 没有它，库里的记录无法确认来自同一个主体，拼不起来任何东西；
# 以后对外提供数据时，也无法回答"这份数据经过谁授权"。
#
# 编排：先"一台设备一个匿名 id"，但结构按"一个人可以有多个设备"设计。
# 以后登录时把匿名 id 绑到账号，历史数据自然归属过去，不用重写。
# ══════════════════════════════════════════════════════════════

IDENTITYFILE = os.path.join(HOME, "identity.json")
_ident_cache = {"data": None}


def load_identity():
    """读身份；首次运行生成匿名 id（幂等）"""
    if _ident_cache["data"]:
        return _ident_cache["data"]
    try:
        with open(IDENTITYFILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("user_id"):
            _ident_cache["data"] = d
            return d
    except Exception:
        pass
    # 首次运行：生成本机匿名身份
    d = {
        "version": 1,
        "user_id": "u_" + uuid.uuid4().hex[:16],
        "kind": "anonymous-device",     # 以后绑定账号时改成 "account"
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "note": "本机匿名身份。绑定账号后历史数据自然归属该账号，无需迁移。",
    }
    try:
        with open(IDENTITYFILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.chmod(IDENTITYFILE, 0o600)
        print(f"🆔 已生成本机身份：{d['user_id']}（{IDENTITYFILE}）", flush=True)
    except Exception as exc:
        print(f"⚠️  身份文件写入失败：{exc}", flush=True)
    _ident_cache["data"] = d
    return d


def user_id():
    return load_identity().get("user_id", "")


# ══════════════════════════════════════════════════════════════
# 调试日志（trace.log）
#
# 开发调试用：把每个"判断点和它的结果"都记下来，出问题时直接读日志，
# 不用靠截图。格式固定成一行一条，方便 grep / 给 AI 看：
#
#   21:50:03.123 | UI     | 点绿             | ip=192.168.0.105 当前监听=[]
#   21:50:03.150 | MON    | 写入配置         | monitored=['127.0.0.1','192.168.0.105']
#   21:50:03.180 | PROXY  | 放行             | ip=192.168.0.105 网段=192.168.0.0/24
#
# 标签：BOOT 启动 / PROXY 代理 / REQ 请求 / MON 监听开关 / LOCAL 系统代理
#       GUARD 断网保护 / WEB 工作台API / PHONE 手机接入页 / DROP 丢弃
# ══════════════════════════════════════════════════════════════

_trace_lock = threading.Lock()
_debug_on = {"v": None}       # 缓存 debug 开关，避免每条日志都读盘


def debug_enabled():
    v = _debug_on["v"]
    if v is None:
        v = bool(load_config().get("debug", True))
        _debug_on["v"] = v
    return v


def dbg(tag, what, detail="", **kv):
    """写一条调试日志。debug 关掉时几乎零开销。

    用法：dbg("MON", "点绿", "ip=1.2.3.4", cur=monitored_set())
    """
    try:
        if not debug_enabled():
            return
        if os.path.exists(TRACEFILE) and os.path.getsize(TRACEFILE) > 4 * 1024 * 1024:
            os.replace(TRACEFILE, TRACEFILE + ".1")     # 超 4MB 轮转一份
        extra = " ".join(f"{k}={v}" for k, v in kv.items())
        line = (f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} | {tag:<6} | "
                f"{what:<12} | {detail}{' ' if detail and extra else ''}{extra}")
        with _trace_lock:
            with open(TRACEFILE, "a", encoding="utf-8") as f:
                f.write(line.rstrip() + "\n")
    except Exception:
        pass                    # 日志绝不能把主流程搞挂


def trace_tail(n=60, grep=None):
    """读最后 n 条调试日志（给 CLI 和 /api/trace 用）"""
    if not os.path.exists(TRACEFILE):
        return []
    try:
        with open(TRACEFILE, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    if grep:
        g = grep.lower()
        lines = [l for l in lines if g in l.lower()]
    return lines[-n:]


# ══════════════════════════════════════════════════════════════
# 监听哪些设备（web 界面里点绿点切换）
#
# 设计取舍：过滤发生在「读」这一步，不在「写」。
# 没点绿的设备，报文照旧原样落盘（flows.jsonl 只追加、不丢东西），
# 只是不出现在工作台里；点绿的那一刻，它过去的历史记录立刻就在了。
# 所以"监听"是个视图开关，不是数据契约 —— 随时可改，不损失任何信息。
# ══════════════════════════════════════════════════════════════

LOCALHOST = ("127.0.0.1", "::1")
# 早期版本的记录没写 client 字段，无法归到任何设备。给它们一个归集项，
# 否则这批数据永远进不了任何监听视图（点绿也看不到）。
UNKNOWN = "__unknown__"


def norm_client(rec):
    """记录的归属设备；没有 client 字段的老记录归到 UNKNOWN"""
    return (rec.get("client") or UNKNOWN)


def is_local(client):
    return client in LOCALHOST


def monitored_set():
    """当前正在监听的客户端 IP 集合"""
    return {str(c) for c in (load_config().get("monitored") or []) if c}


def set_monitored(clients):
    cfg = load_config()
    before = sorted({str(c) for c in (cfg.get("monitored") or []) if c})
    cfg["monitored"] = sorted({str(c) for c in clients if c})
    save_config(cfg)
    dbg("MON", "写入配置", f"monitored={cfg['monitored']}", 之前=before)
    return cfg["monitored"]


def local_clients():
    """本机自己产生的流量：回环 127.0.0.1 + 本机 LAN IP（代理填了 192.168.x.x 的那部分）"""
    mine = set(LOCALHOST) | {lan_ip()}
    n, first, last = 0, None, None
    for r in load_flows():
        if norm_client(r) not in mine:
            continue
        n += 1
        ts = r.get("ts")
        if ts:
            first = first or ts
            last = ts
    return {"requests": n, "first": first, "last": last}


def unknown_clients():
    """数出没有归属设备的早期记录"""
    n, first, last = 0, None, None
    for r in load_flows():
        if not r.get("client"):
            n += 1
            ts = r.get("ts")
            if ts:
                first = first or ts
                last = ts
    return {"requests": n, "first": first, "last": last}


def traffic_devices():
    """从抓到的流量里统计出有哪些设备。

    **设备 = 真的发过请求的 client**，不看网段、不 ping、不管在不在线。
    用户要的就是这个：连上并抓到东西了才叫设备。

    返回按最后活跃时间倒序（最近在用的排前面）。
    """
    my_ip = lan_ip()
    self_ips = set(LOCALHOST) | {my_ip}
    stats = {}
    for r in load_flows():
        c = norm_client(r)
        if c in self_ips or c == UNKNOWN:
            continue                        # 本机和早期无主记录单独处理
        e = stats.setdefault(c, {"client": c, "requests": 0,
                                 "first": None, "last": None})
        e["requests"] += 1
        ts = r.get("ts")
        if ts:
            e["first"] = e["first"] or ts
            e["last"] = ts
    rows = list(stats.values())
    rows.sort(key=lambda x: (x.get("last") or "", x["requests"]), reverse=True)
    return rows


def device_label(client):
    """给一个 IP 配上人看得懂的名字（图标留给前端决定）"""
    if client == UNKNOWN:
        return "未知来源（早期记录）"
    if is_local(client):
        return "本机（这台 Mac）"
    for d in detect_devices():
        if d.get("client") != client:
            continue
        platform = d.get("platform") or d.get("probe")
        if platform:
            return " ".join(str(x) for x in (platform, d.get("model")) if x)
        if d.get("browser"):
            return "浏览器"
        break
    return "设备"


def assemble_devices():
    """工作台「设备」页要的设备 —— **只列真抓到过流量的**。

    不扫网段、不读 ARP、不 ping 判断在线。
    设备就是"发过请求的那个 IP"，来龙去脉看最后一条流的时间。
    """
    mon = monitored_set()

    def base(ip, requests, first, last, source):
        return {"client": ip, "requests": requests, "first": first, "last": last,
                "source": source, "title": device_label(ip),
                "monitored": ip in mon}

    rows = [base(d["client"], d["requests"], d["first"], d["last"], "device")
            for d in traffic_devices()]

    # 本机：只要抓过流量就列出来（它是最常被抓的那台）
    loc = local_clients()
    if loc["requests"]:
        r = base("127.0.0.1", loc["requests"], loc["first"], loc["last"], "local")
        r["local"] = True
        r["pointing_here"] = bool(local_proxy_state().get("pointing_here"))
        rows.insert(0, r)

    # 早期没有 client 字段的记录：单独归一项
    unk = unknown_clients()
    if unk["requests"]:
        rows.append(base(UNKNOWN, unk["requests"], unk["first"], unk["last"], "unknown"))

    # 排序：本机永远第一，其余按最后活跃时间倒序（最近在用的排前面）。
    # 一次 sort 排完 —— 叠三次容易看晕，也容易把顺序弄反。
    rows.sort(key=lambda r: (0 if r.get("local") else 1,
                             _neg_ts(r.get("last"))))
    return rows


def _neg_ts(ts):
    """把时间戳转成"越大越小"的排序键，好让最新的排前面。

    没有时间戳的排最后。
    """
    if not ts:
        return ""
    # ts 是 ISO 字符串（2026-09-15T17:14:33），按字符倒序不可行，
    # 用一个取反的数值：把所有字符的码位取负
    return tuple(-ord(c) for c in ts)

def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "<本机IP>"


def is_noise(host):
    h = (host or "").lower()
    return any(h == n or h.endswith("." + n) for n in NOISE_HOSTS)


# ── 访问控制：只允许本机和同网段的设备用这个代理 ────────────────

_local_nets = {"data": [], "ts": 0}


def _local_nets_cached():
    """本机所有非回环 IPv4 的 (网络号, 掩码)，60 秒缓存一次"""
    if time.time() - _local_nets["ts"] < 60 and _local_nets["data"]:
        return _local_nets["data"]
    nets = []
    try:
        r = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)", line)
            if m:
                ip_i = struct.unpack("!I", socket.inet_aton(m.group(1)))[0]
                mask = int(m.group(2), 16)
                nets.append((ip_i & mask, mask))
    except Exception:
        pass
    _local_nets["data"] = nets
    _local_nets["ts"] = time.time()
    return nets


def client_allowed(ip):
    """本机 + 同网段放行；其余拒绝（避免变成开放代理）"""
    if ip in ("127.0.0.1", "::1"):
        return True
    if not load_config().get("access_control", True):
        return True
    try:
        ip_i = struct.unpack("!I", socket.inet_aton(ip))[0]
    except Exception:
        return False
    for net, mask in _local_nets_cached():
        if (ip_i & mask) == net:
            return True
    # 可能是刚换了网络、缓存里还是旧网段 → 强制重取一次再判，避免切网后被误拒
    _local_nets["ts"] = 0
    for net, mask in _local_nets_cached():
        if (ip_i & mask) == net:
            return True
    return False


# ── 设备识别 ──────────────────────────────────────────────────

def lan_interface():
    """本机局域网 IP 所在的网卡名（用于筛 ARP 表）"""
    ip = lan_ip()
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    cur = None
    for line in out.splitlines():
        m = re.match(r"^(\w+):", line)
        if m:
            cur = m.group(1)
        if f"inet {ip} " in line:
            return cur
    return None


def lan_hosts():
    """ARP 表里的同网段主机（还没走代理的候选设备）"""
    iface = lan_interface()
    my_ip = lan_ip()
    try:
        out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    hosts = []
    for line in out.splitlines():
        m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-fA-F:]+)", line)
        if not m:
            continue
        ip, mac = m.group(1), m.group(2).lower()
        if ip == my_ip or mac.startswith("ff:ff:ff") or "incomplete" in line:
            continue
        if iface and f"on {iface} " not in line:
            continue
        hosts.append({"ip": ip, "mac": mac})
    return sorted(hosts, key=lambda h: [int(x) for x in h["ip"].split(".")])


def subnet_hosts():
    """本机网段内待扫描的地址（由 IP + 掩码算出，上限 1024 个）"""
    ip = lan_ip()
    iface = lan_interface()
    mask = None
    try:
        out = subprocess.run(["ifconfig", iface], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"inet\s+\S+\s+netmask\s+(0x[0-9a-fA-F]+)", out)
        if m:
            mask = int(m.group(1), 16)
    except Exception:
        pass
    if not mask:
        mask = 0xFFFFFF00
    ip_i = struct.unpack("!I", socket.inet_aton(ip))[0]
    net = ip_i & mask
    bcast = net | (~mask & 0xFFFFFFFF)
    hosts = []
    a = net + 1
    while a < bcast and len(hosts) < 1024:
        if (a & 0xFF) not in (0, 255):
            hosts.append(socket.inet_ntoa(struct.pack("!I", a)))
        a += 1
    return hosts


def ping_once(host, timeout_s=1):
    if sys.platform == "darwin":
        cmd = ["ping", "-c", "1", "-W", str(int(timeout_s * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s))), host]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout_s + 3).returncode == 0
    except Exception:
        return False


def sweep(hosts, timeout_s=0.5, workers=128):
    """并行 ping，返回存活主机"""
    from concurrent.futures import ThreadPoolExecutor
    live = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for h, ok in zip(hosts, ex.map(lambda x: ping_once(x, timeout_s), hosts)):
                if ok:
                    live.append(h)
    except Exception:
        pass
    return live


def parse_scan_source(s):
    """'Android Redmi 21091116C pissarro' -> ('Android', 'Redmi 21091116C')

    兼容几种写法：iOS / iPhone14,3 / Android <品牌> <型号> <代号>
    """
    if not s:
        return (None, None)
    p = str(s).split()
    if not p:
        return (None, None)
    first, low = p[0], p[0].lower()
    rest = " ".join(p[1:3]) if len(p) > 2 else " ".join(p[1:])

    if low in ("ios", "iphone", "ipad") or low.startswith(("iphone", "ipad")):
        return ("iOS", rest or first)
    if low == "android":
        return ("Android", rest or None)
    return (first, rest or None)


def detect_devices():
    """从抓包流量里识别出接入过的设备"""
    dev = {}
    for r in load_flows():
        c = r.get("client")
        if not c or c in ("127.0.0.1", "::1"):
            continue
        d = dev.setdefault(c, {"client": c, "requests": 0, "first": r.get("ts"),
                               "last": r.get("ts")})
        d["requests"] += 1
        if r.get("ts"):
            d["last"] = r["ts"]
            if not d.get("first"):
                d["first"] = r["ts"]

        host = (r.get("host") or "").lower()
        # 联网探测：设备刚连上就会打，用它判断平台（不需要业务请求）
        for ph, plat in PROBE_HOSTS.items():
            if host == ph or host.endswith("." + ph):
                d.setdefault("probe", plat)
                d["probe_host"] = host

        h = r.get("req_headers") or {}
        if h.get("scan-source") and not d.get("platform"):
            d["platform"], d["model"] = parse_scan_source(h["scan-source"])
        for hk, dk in (("version", "app_version"), ("user_selects_country", "country"),
                       ("usersite", "site"), ("timezone", "timezone"), ("lang", "lang")):
            if h.get(hk) and not d.get(dk):
                d[dk] = h[hk]
        ua = h.get("user-agent") or ""
        if "Mozilla" in ua:
            d["browser"] = True
    return list(dev.values())


def connect_steps(platforms):
    """按平台给接入指引；没识别到就两种都给"""
    ip, port = lan_ip(), load_config().get("port", DEFAULT_PORT)
    steps = {}
    if not platforms or "Android" in platforms:
        steps["Android"] = [
            "设置 → WLAN → 点当前连接的网络 → 修改网络",
            "展开「高级选项」→「代理」改为「手动」",
            f"主机名 {ip}    端口 {port}",
        ]
    if not platforms or "iOS" in platforms:
        steps["iOS"] = [
            "设置 → 无线局域网 → 点当前网络右侧的 ⓘ",
            "滑到底部 →「配置代理」→「手动」",
            f"服务器 {ip}    端口 {port}",
        ]
    steps["通用"] = [
        "手机和电脑必须连同一个 Wi-Fi",
        "改完把 App 杀掉重开（否则复用旧连接）",
        "验证：手机浏览器随便开个网页，回来跑 cap list",
    ]
    return steps


def record_is_noise(r):
    """兼容旧数据：老记录没有 noise 字段，按 host 现算"""
    n = r.get("noise")
    return is_noise(r.get("host")) if n is None else bool(n)


# ══════════════════════════════════════════════════════════════
# 存储
# ══════════════════════════════════════════════════════════════

_count = {"n": 0}
_lock = threading.Lock()

# 解析缓存：轮询要反复读同一个文件，按 (mtime, size) 判断是否需要重解析。
# 过滤监听设备时只需重算过滤，不必重新读盘。
_flows_cache = {"sig": None, "rows": []}


def invalidate_flows_cache():
    _flows_cache["sig"] = None


def load_flows(path=None):
    path = path or FLOWS
    if not os.path.exists(path):
        return []
    try:
        st = os.stat(path)
        sig = (path, st.st_mtime_ns, st.st_size)
        if path == FLOWS and _flows_cache["sig"] == sig:
            return _flows_cache["rows"]
    except Exception:
        sig = None
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    if path == FLOWS and sig is not None:
        _flows_cache["sig"] = sig
        _flows_cache["rows"] = out
    return out


def rotate_if_needed():
    try:
        if os.path.exists(FLOWS) and os.path.getsize(FLOWS) > MAX_LOG_BYTES:
            os.replace(FLOWS, FLOWS + ".1")
    except Exception:
        pass


def redact_headers(h):
    if not load_config().get("redact", True):
        return h
    out = {}
    for k, v in h.items():
        if k.lower() in REDACT_HEADERS:
            s = str(v)
            out[k] = (s[:4] + "…<已脱敏>") if len(s) > 4 else "<已脱敏>"
        else:
            out[k] = v
    return out


def write_flow(entry):
    entry["id"] = _count["n"] = _count["n"] + 1
    entry["ts"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    entry["user_id"] = user_id()          # 数据归属：只加字段，不动原有 15 个
    with _lock:
        rotate_if_needed()
        with open(FLOWS, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        invalidate_flows_cache()
    tag = f"{entry.get('method','')} {entry.get('host','')}{entry.get('path','')}"
    print(f"[{entry['id']}] {entry['ts'][11:]} {tag} → {entry.get('status','')}", flush=True)
    note_client(entry.get("client"))
    broadcast("flow", entry)          # 实时推给浏览器
    return entry


# ── 实时推送（SSE） ───────────────────────────────────────────

_subs = []
_subs_lock = threading.Lock()


def broadcast(event, data):
    """把事件推给所有打开的浏览器（没有订阅者时几乎零开销）"""
    if not _subs:
        return
    msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    with _subs_lock:
        for q in list(_subs):
            try:
                q.put_nowait(msg)
            except Exception:
                pass


BINARY_CT_PREFIX = ("image/", "video/", "audio/", "font/")
BINARY_CT_EXACT = ("application/octet-stream", "application/zip", "application/pdf",
                   "application/gzip", "application/x-gzip", "application/x-tar",
                   "application/x-7z-compressed", "application/x-rar-compressed",
                   "application/wasm", "application/x-protobuf", "application/msword",
                   "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
                   "application/vnd.openxmlformats-officedocument")


def _is_text(data):
    """能不能严格按 UTF-8 解码 —— 对 JSON 接口来说这是最准的二进制判据，且不会误伤中文。
    注意不能用「非 ASCII 占比」判断：中文在 UTF-8 里全是高位字节，会被整片误杀。"""
    if b"\x00" in data[:8192]:
        return False
    probe = data[:65536]
    for trim in (0, 1, 2, 3):          # 允许正文末尾正好断在多字节字符中间
        chunk = probe[:len(probe) - trim] if trim else probe
        try:
            chunk.decode("utf-8")
            return True
        except UnicodeDecodeError:
            continue
    return False


def decode_body(raw, headers):
    if not raw:
        return ""
    data = raw
    if "gzip" in (headers.get("content-encoding") or "").lower():
        try:
            data = gzip.decompress(raw)
        except Exception:
            pass
    ct = (headers.get("content-type") or "").split(";")[0].strip().lower()
    # 图片/视频/压缩包这类二进制：存下来没用，还会撑爆日志、点开把浏览器拖死
    if ct.startswith(BINARY_CT_PREFIX) or ct in BINARY_CT_EXACT or not _is_text(data):
        return f"<二进制内容 {len(data)} 字节，未保存{('（' + ct + '）') if ct else ''}>"
    text = data.decode("utf-8", "replace")
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY] + f"\n…[已截断，原始 {len(data)} 字节]"
    return text


# ══════════════════════════════════════════════════════════════
# 代理服务端
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"capture/{VERSION}"

    def log_message(self, *a):
        pass

    @property
    def client_ip(self):
        return self.client_address[0] if self.client_address else ""

    # ---- HTTPS：透传（不改动任何东西） ----
    def do_CONNECT(self):
        host, _, port = self.path.partition(":")
        port = int(port or 443)
        entry = {"kind": "connect", "method": "CONNECT", "host": host, "port": port,
                 "client": self.client_ip, "noise": is_noise(host),
                 "note": "HTTPS 透传，未解密"}
        try:
            up = socket.create_connection((host, port), timeout=15)
        except Exception as exc:
            # 连不上也要标明原因，否则界面上跟「连上了」看不出区别，排查断网会被误导
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["note"] = "HTTPS 隧道建立失败"
            write_flow(entry)
            try:
                self.send_error(502, f"connect failed: {exc}")
            except Exception:
                pass
            return
        write_flow(entry)
        self.send_response(200, "Connection Established")
        self.end_headers()
        self._tunnel(self.connection, up)

    def _tunnel(self, a, b):
        a.setblocking(False)
        b.setblocking(False)
        try:
            while True:
                r, _, x = select.select([a, b], [], [a, b], 60)
                if x or not r:
                    return
                for s in r:
                    other = b if s is a else a
                    try:
                        data = s.recv(65536)
                    except Exception:
                        return
                    if not data:
                        return
                    other.sendall(data)
        finally:
            try:
                b.close()
            except Exception:
                pass

    # ---- HTTP：转发 + 完整记录 ----
    def _proxy(self):
        t0 = time.time()
        raw_url = self.path
        if not raw_url.lower().startswith("http"):
            self.send_error(400, "请把本工具配置为 HTTP 代理使用")
            return
        u = urllib.parse.urlsplit(raw_url)
        host, port = u.hostname, (u.port or 80)

        n = int(self.headers.get("Content-Length") or 0)
        req_raw = self.rfile.read(n) if n > 0 else b""
        req_h = {k.lower(): v for k, v in self.headers.items()}

        entry = {
            "kind": "http", "method": self.command, "host": host, "port": port,
            "path": u.path + (("?" + u.query) if u.query else ""), "url": raw_url,
            "client": self.client_ip, "noise": is_noise(host),
            "req_headers": redact_headers({k: v for k, v in req_h.items()
                                           if k not in ("proxy-connection",)}),
            "req_body": decode_body(req_raw, req_h),
        }
        try:
            conn = http.client.HTTPConnection(host, port, timeout=30)
            fwd = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("proxy-connection", "proxy-authorization",
                                        "connection", "host")}
            fwd["Host"] = u.netloc
            fwd["Connection"] = "close"
            conn.request(self.command, u.path + (("?" + u.query) if u.query else ""),
                         body=req_raw, headers=fwd)
            resp = conn.getresponse()
            raw = resp.read()
            rh = {k.lower(): v for k, v in resp.getheaders()}
            entry.update({"status": resp.status, "reason": resp.reason,
                          "resp_headers": redact_headers(rh),
                          "resp_body": decode_body(raw, rh),
                          "ms": int((time.time() - t0) * 1000)})
            write_flow(entry)

            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() in ("connection", "transfer-encoding", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            conn.close()
        except Exception as exc:
            entry.update({"status": None, "error": f"{type(exc).__name__}: {exc}",
                          "ms": int((time.time() - t0) * 1000)})
            write_flow(entry)
            try:
                self.send_error(502, f"upstream failed: {exc}")
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _proxy


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def verify_request(self, request, client_address):
        ip = client_address[0]
        ok = client_allowed(ip)
        if ok:
            dbg("PROXY", "放行", f"来自 {ip}")
            return True
        # 被拒 = 手机永远连不上，这是最容易卡住的一步，必须留痕
        nets = [f"{socket.inet_ntoa(struct.pack('!I', n))}/{bin(m).count('1')}"
                for n, m in _local_nets_cached()]
        dbg("DROP", "拒绝", f"来自 {ip}", 允许网段=nets, 原因="不在允许网段")
        print(f"[拒绝] {ip} 不在允许网段 —— 如需放开：cap config set access_control false", flush=True)
        return False


# ── Web 工作台（只读，仅监听 127.0.0.1） ──────────────────────

class WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"capture-web/{VERSION}"

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

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        # body 只读一次就存下来 —— 以前是"读掉并丢弃"，导致后面的 handler
        # 再读 rfile 只能拿到空串（AI 对话就踩了这个坑）
        self._body = self.rfile.read(n) if n else b""
        if u.path == "/api/local":
            qs = urllib.parse.parse_qs(u.query)
            act = (qs.get("set", [""])[0] or "").lower()
            if act == "on":
                svc = local_proxy_on()
                return self._send(200, json.dumps({"ok": True, "service": svc,
                                                   "state": local_proxy_state()}, ensure_ascii=False))
            if act == "off":
                svc, restored = local_proxy_off()
                return self._send(200, json.dumps({"ok": True, "service": svc,
                                                   "restored": restored,
                                                   "state": local_proxy_state()}, ensure_ascii=False))
            return self._send(400, json.dumps({"error": "set=on|off"}, ensure_ascii=False))
        if u.path == "/api/ai/chat":
            return self._ai_chat()

        if u.path == "/api/ai/save":
            n = int(self.headers.get("Content-Length") or 0)
            raw = getattr(self, "_body", b"")
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                return self._send(400, json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            cr = load_creds()
            for k in ("provider", "base_url", "model", "api_key"):
                v = body.get(k)
                if v is not None and str(v).strip() != "":
                    cr[k] = str(v).strip()
            if body.get("clear_key"):
                cr.pop("api_key", None)
            save_creds(cr)
            cfg = load_config()
            for src, dst in (("enabled", "ai_enabled"), ("provider", "ai_provider"),
                             ("model", "ai_model"), ("base_url", "ai_base_url")):
                if body.get(src) is not None:
                    cfg[dst] = body[src]
            save_config(cfg)
            tested, msg = (True, "已保存（未测）") if body.get("skip_test") else ai_test()
            return self._send(200, json.dumps(
                {"ok": True, "test_ok": tested, "message": msg,
                 "ready": ai_ready()}, ensure_ascii=False))

        if u.path == "/api/ai/autofill":
            cands = find_keys()
            if not cands:
                return self._send(404, json.dumps(
                    {"ok": False, "error": "本机没找到可复用的 key，请手动填写"},
                    ensure_ascii=False))
            tried, good = [], None
            for src, prov, key in cands:
                cr = load_creds()
                keep = dict(cr)
                cr["provider"] = prov
                cr["api_key"] = key
                cr.setdefault("model", PROVIDERS[prov]["models"][0])
                save_creds(cr)
                ok, msg = ai_test()
                tried.append({"source": src, "provider": prov,
                              "hint": key[:7] + "…" + key[-4:], "ok": ok,
                              "message": msg if not ok else ""})
                if ok:
                    good = (src, prov, key, msg)
                    break
                save_creds(keep)                 # 这个不行，回退，试下一个
            if not good:
                return self._send(200, json.dumps(
                    {"ok": False, "tried": tried,
                     "error": "找到 %d 个 key，但都没测通 —— 请手动填一个能用的"
                              % len(tried)}, ensure_ascii=False))
            src, prov, key, msg = good
            cfg = load_config()
            cfg["ai_provider"] = prov
            save_config(cfg)
            return self._send(200, json.dumps(
                {"ok": True, "message": msg, "test_ok": True, "source": src,
                 "tried": tried,
                 "hint": key[:7] + "…" + key[-4:]}, ensure_ascii=False))

        if u.path == "/api/ai/test":
            ok, msg = ai_test()
            return self._send(200, json.dumps({"ok": ok, "message": msg}, ensure_ascii=False))

        if u.path == "/api/semantics":
            n = int(self.headers.get("Content-Length") or 0)
            raw = getattr(self, "_body", b"")
            try:
                patch = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                return self._send(400, json.dumps({"ok": False, "error": f"JSON 解析失败：{exc}"},
                                                  ensure_ascii=False))
            sem = load_semantics()
            for section in ("fields", "apis", "chains"):
                if isinstance(patch.get(section), dict):
                    sem.setdefault(section, {}).update(patch[section])
            save_semantics(sem)
            reload_chains()
            return self._send(200, json.dumps({
                "ok": True,
                "version": sem.get("version"),
                "fields": sem_count("fields"),
                "apis": sem_count("apis"),
            }, ensure_ascii=False))

        if u.path == "/api/monitor":
            qs = urllib.parse.parse_qs(u.query)
            act = (qs.get("set", [""])[0] or "").strip()
            note = None
            dbg("MON", "收到切换", f"set={act!r}", 客户端=self.client_address[0])
            try:
                if act == "all":
                    mon = set_monitored({str(d["client"]) for d in assemble_devices()})
                elif act in ("", "none"):
                    mon = set_monitored(set())
                else:                                   # 点一下：绿↔灰
                    cur = monitored_set()
                    mon = set_monitored(cur - {act} if act in cur else cur | {act})

                # 本机被选中 = 开始抓这台 Mac：系统代理指向本工具
                if is_local(act):
                    if act in mon:
                        if not local_proxy_state().get("pointing_here"):
                            svc = local_proxy_on()
                            note = f"已把系统代理设为 127.0.0.1:{load_config().get('port', DEFAULT_PORT)}（{svc}）"
                        else:
                            note = "系统代理已指向本工具"
                    else:
                        svc, restored = local_proxy_off()
                        note = ("已还原系统代理" if restored
                                else "当前系统代理不是本工具，未改动" if restored is None
                                else "已关闭系统代理")
                dbg("MON", "切换完成", f"monitored={mon}", 结果=note or "（无副作用）")
            except Exception as exc:
                dbg("MON", "切换失败", f"set={act!r}", 错误=f"{type(exc).__name__}: {exc}")
                return self._send(500, json.dumps({"ok": False, "error": str(exc),
                                                   "monitored": sorted(monitored_set())},
                                                  ensure_ascii=False))
            broadcast("monitored", {"monitored": sorted(mon)})
            lstate = local_proxy_state()
            lstate["guard"] = bool(guard_running())
            return self._send(200, json.dumps({
                "ok": True, "monitored": sorted(mon), "note": note,
                "local": lstate,
            }, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def _ai_chat(self):
        """流式对话：用 SSE 把模型的增量一块块推给浏览器。

        为什么不用一次返回：诊断可能要十几秒，用户得看见它在动，
        而不是盯着一个转圈等。这和 flows 的实时推送是同一套机制。
        """
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(getattr(self, "_body", b"").decode("utf-8")) if n else {}
        except Exception:
            body = {}
        q = (body.get("q") or body.get("message") or "").strip()
        scope = body.get("scope") or "auto"
        history = body.get("history") or []
        if not q:
            return self._send(400, json.dumps({"error": "空问题"}, ensure_ascii=False))
        if scope == "auto":
            # 自动判断：问"怎么/为什么/连不上/报错"偏诊断；问"分析/字段/统计"偏数据
            kw = ("怎么", "为什么", "连不上", "抓不到", "报错", "失败", "装", "配置", "没用", "没有数据")
            scope = "diagnose" if any(k in q for k in kw) else "analyze"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True      # 流式响应没有 Content-Length，必须主动关连接，
                                          # 否则客户端 read() 永远等不到结束（浏览器报 network error）

        def sse(event, data):
            try:
                self.wfile.write(("event: %s\ndata: %s\n\n" % (
                    event, json.dumps(data, ensure_ascii=False))).encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

        dbg("AI", "提问", f"scope={scope} 长度={len(q)}", 问题=q[:60])
        msgs = [{"role": "system", "content": ai_system_prompt(scope)}]
        for h in history[-6:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                msgs.append({"role": h["role"], "content": str(h["content"])[:4000]})
        msgs.append({"role": "user", "content": q})

        sse("meta", {"scope": scope,
                     "model": ai_config()["model"],
                     "provider": ai_config()["provider_name"]})
        total = 0
        try:
            for piece in ai_stream(msgs):
                total += len(piece)
                sse("delta", {"t": piece})
        except Exception as exc:
            dbg("AI", "流异常", str(exc)[:80])
            sse("delta", {"t": "\n\n⚠️ " + str(exc)[:200]})
        sse("done", {"chars": total})
        dbg("AI", "回答完成", f"{total} 字符")
        try:
            self.wfile.flush()
        except Exception:
            pass
        self.close_connection = True

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)

        if u.path in ("/", "/index.html"):
            page = os.path.join(HOME, "web.html")
            try:
                with open(page, encoding="utf-8") as f:
                    html = f.read()
                # 注入版本号：前端发现版本变了会自动 reload，用户不用手动刷新
                # 注入两个版本号：
                #   __UI_VERSION__  页面缓存标识，web.html 一改前端就自动 reload
                #   __APP_VERSION__ 给人看的软件版本
                html = html.replace(
                    "</head>",
                    f'<script>window.__UI_VERSION__="{ui_version()}";'
                    f'window.__APP_VERSION__="{VERSION}";</script></head>', 1)
            except Exception:
                html = ("<h1>找不到 web.html</h1>"
                        f"<p>请确认它和 capture.py 在同一目录：{esc_html(HOME)}</p>")
            return self._send(200, html, "text/html; charset=utf-8")

        if u.path == "/api/flows":
            qs = urllib.parse.parse_qs(u.query)
            since = (qs.get("since_id", ["0"])[0] or "0")
            rows = load_flows()
            total = len(rows)
            if since.isdigit() and int(since) > 0:
                rows = [r for r in rows if (r.get("id") or 0) > int(since)]
            after_since = len(rows)
            # 只给正在监听的设备（没监听的照样在盘上，点绿即刻可见）
            mon = monitored_set()
            rows = [r for r in rows if norm_client(r) in mon]
            dbg("WEB", "取请求", f"since={since}",
                文件=total, 增量=after_since, 返回=len(rows), 监听=sorted(mon))
            return self._send(200, json.dumps(rows, ensure_ascii=False))

        if u.path == "/api/flow":
            qs = urllib.parse.parse_qs(u.query)
            try:
                fid = int(qs.get("id", ["0"])[0])
            except Exception:
                fid = 0
            hits = [r for r in load_flows() if r.get("id") == fid]
            if len(hits) > 1:
                # 历史数据里 id 不唯一（早期版本续号有 bug）—— 说出来，别静默给错
                dbg("API", "id 重复", f"id={fid} 命中 {len(hits)} 条",
                    提示="早期版本重启时计数器从 1 重来导致；取最后一条")
            if hits:
                return self._send(200, json.dumps(hits[-1], ensure_ascii=False))
            return self._send(404, json.dumps({"error": "not found"}))

        if u.path == "/api/events":
            # SSE：服务端主动推送，新请求/设备状态变化立刻到浏览器
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = queue.Queue(maxsize=2000)
            with _subs_lock:
                _subs.append(q)
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(msg.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")   # 心跳，防连接被回收
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with _subs_lock:
                    if q in _subs:
                        _subs.remove(q)
            return

        if u.path == "/api/local":
            st = local_proxy_state()
            st["guard"] = bool(guard_running())     # 断网保护是否在岗
            return self._send(200, json.dumps(st, ensure_ascii=False))

        if u.path == "/api/semantics":
            qs = urllib.parse.parse_qs(u.query)
            what = (qs.get("what", [""])[0] or "").strip()
            sem = load_semantics()
            if what == "fields":
                return self._send(200, json.dumps(sem.get("fields", {}), ensure_ascii=False))
            if what == "apis":
                return self._send(200, json.dumps(sem.get("apis", {}), ensure_ascii=False))
            if what == "chains":
                return self._send(200, json.dumps(sem.get("chains", {}), ensure_ascii=False))
            if what:                                  # 查单个字段的含义
                m = field_meaning(what)
                return self._send(200 if m else 404,
                                  json.dumps({"field": what, "meaning": m}, ensure_ascii=False))
            return self._send(200, json.dumps(sem, ensure_ascii=False))

        if u.path == "/api/identity":
            d = dict(load_identity())
            try:
                rows = load_flows()
                d["my_records"] = len([r for r in rows if r.get("user_id") == d["user_id"]])
                d["legacy_records"] = len([r for r in rows if not r.get("user_id")])
            except Exception:
                pass
            return self._send(200, json.dumps(d, ensure_ascii=False))

        if u.path == "/api/ai/config":
            a = ai_config()
            safe = {k: v for k, v in a.items() if k != "api_key"}
            safe["key_set"] = bool(a["api_key"])
            safe["key_hint"] = (a["api_key"][:5] + "…" + a["api_key"][-4:]) if a["api_key"] else ""
            safe["ready"] = ai_ready()
            safe["providers"] = {k: {"name": v["name"], "base_url": v["base_url"],
                                     "models": v["models"], "key_url": v["key_url"]}
                                 for k, v in PROVIDERS.items()}
            safe["credfile"] = CREDFILE
            return self._send(200, json.dumps(safe, ensure_ascii=False))

        if u.path == "/api/ai/models":
            return self._send(200, json.dumps({"models": ai_list_models()}, ensure_ascii=False))

        if u.path == "/api/trace":
            qs = urllib.parse.parse_qs(u.query)
            try:
                n = int(qs.get("n", ["80"])[0])
            except Exception:
                n = 80
            g = (qs.get("grep", [""])[0] or "").strip() or None
            lines = trace_tail(n, g)
            return self._send(200, json.dumps({"lines": lines, "file": TRACEFILE},
                                              ensure_ascii=False))

        if u.path == "/api/stats":
            rows = load_flows()
            return self._send(200, json.dumps({
                "total": len(rows),
                "business": len([r for r in rows if not record_is_noise(r)]),
                "clients": sorted({r.get("client", "") for r in rows if r.get("client")}),
                "last": rows[-1]["ts"] if rows else None,
            }, ensure_ascii=False))

        if u.path == "/api/digest":
            """汇报抓包侧要注意的事：本机代理没收、设备掉了、没人监听"""
            items = []
            try:
                # 设备现在是连上就自动抓，不再有"没点绿所以看不到"这种事。
                # 只有"一台设备都没有"才值得提醒一句。
                if not assemble_devices():
                    items.append({"level": "info",
                                  "title": "还没有设备连上抓包工具",
                                  "detail": "把手机的 Wi-Fi 代理填成工作台上显示的地址即可",
                                  "action": "打开抓包工作台"})
                if local_proxy_state().get("pointing_here") and not guard_running():
                    items.append({"level": "critical",
                                  "title": "系统代理被接管，但断网保护没在跑",
                                  "detail": "工具一旦崩溃，这台电脑会断网",
                                  "action": "跑 cap guard start，或在抓包工具里点一下本机"})
                rows = load_flows()
                devs = {}
                for r in rows[-500:]:
                    c = r.get("client") or ""
                    if c and c not in ("127.0.0.1", "::1"):
                        devs[c] = max(devs.get(c, ""), r.get("ts") or "")
                if devs and not any(d in mon for d in devs):
                    items.append({"level": "info",
                                  "title": f"有 {len(devs)} 台设备发过请求，但都没在监听",
                                  "detail": "　".join(list(devs)[:3]),
                                  "action": "打开抓包工作台点绿"})
            except Exception as exc:
                log(f"[digest] {exc}")
            return self._send(200, json.dumps({"items": items}, ensure_ascii=False))

        if u.path == "/api/status":
            cfg = load_config()
            rows = load_flows()
            pid = running_pid()
            return self._send(200, json.dumps({
                "running": bool(pid),
                "proxy_port": cfg.get("port", DEFAULT_PORT),
                "web_port": cfg.get("web_port", 8891),
                "host": lan_ip(),
                "home": HOME,
                "total": len(rows),
                "http": len([r for r in rows if r.get("kind") == "http"]),
                "business": len([r for r in rows if not record_is_noise(r)]),
                "last": rows[-1]["ts"] if rows else None,
                "redact": cfg.get("redact", True),
                "ui_version": ui_version(),
                "sse_clients": len(_subs),
                "on_path": any(os.path.exists(os.path.join(p, "cap"))
                               for p in os.environ.get("PATH", "").split(":")),
            }, ensure_ascii=False))

        if u.path == "/api/devices":
            devs = assemble_devices()
            loc = next((d for d in devs if d.get("local")), {})
            dbg("WEB", "取设备表", f"共 {len(devs)} 台",
                监听=[d["client"] for d in devs if d.get("monitored")],
                在线=len([d for d in devs if d.get("online") is True]),
                本机记录=loc.get("requests"),
                系统代理=loc.get("pointing_here"))
            hosts = lan_hosts()
            dev_ips = {d["client"] for d in devs if d.get("source") == "device"}
            plats = {d.get("platform") for d in devs if d.get("platform")}
            return self._send(200, json.dumps({
                "proxy": {"host": lan_ip(), "port": load_config().get("port", DEFAULT_PORT)},
                "setup_url": lan_web_url(),
                "semantics": {"version": load_semantics().get("version", 0),
                              "fields": sem_count("fields"),
                              "chains": list(reload_chains().keys())},
                "identity": {"user_id": user_id()},
                "connected": devs,
                "monitored": sorted(monitored_set()),

                "usb": usb_devices(),
                "guide": connect_steps(plats),
            }, ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))


class WebServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def ui_version():
    """web.html 的版本号（改动后前端会自动刷新）"""
    try:
        st = os.stat(os.path.join(HOME, "web.html"))
        return f"{int(st.st_mtime)}-{st.st_size}"
    except Exception:
        return "0"


def esc_html(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def serve_web(port):
    try:
        WebServer(("127.0.0.1", port), WebHandler).serve_forever()
    except OSError as exc:
        print(f"[Web] 启动失败（端口 {port}）：{exc}", flush=True)


# ══════════════════════════════════════════════════════════════
# 手机接入页（局域网只读）
#
# 二维码要能扫，就必须让手机能打开一个页面 —— 但"手机能打开工作台"等于
# 把抓到的报文给整个网段看，不行。所以另开一个端口，只提供一个静态的
# 接入说明页：显示代理地址、按机型给步骤。不含任何抓包数据。
# ══════════════════════════════════════════════════════════════

MOBILE_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>连上抓包工作台</title>
<style>
 :root{--line:#e5e7eb;--sub:#6b7280;--blue:#2563eb;--green:#1f9d55;--amber:#b45309;--bg:#f7f8fa}
 *{box-sizing:border-box}
 body{margin:0;padding:18px 16px 40px;font:15px/1.6 -apple-system,BlinkMacSystemFont,
      "PingFang SC","Helvetica Neue",Arial,sans-serif;background:var(--bg);color:#111827}
 h1{font-size:19px;margin:0 0 4px}
 .card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin:14px 0}
 .addr{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#f3f6fc;
       border:1px dashed #c7d7f5;border-radius:10px;padding:12px;margin:10px 0}
 .addr .row{display:flex;justify-content:space-between;align-items:center;padding:5px 0}
 .addr .row+.row{border-top:1px solid #e3ebfa}
 .addr b{font-size:19px;color:var(--blue)}
 .copy{border:1px solid var(--line);background:#fff;border-radius:8px;padding:7px 14px;
       font-size:14px;color:var(--blue);cursor:pointer}
 ol{padding-left:22px;margin:8px 0}
 ol li{margin:7px 0}
 .tag{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:20px;
      padding:1px 10px;font-size:13px;margin-right:6px}
 .tabs{display:flex;gap:8px;margin:12px 0 4px}
 .tabs button{flex:1;border:1px solid var(--line);background:#fff;border-radius:20px;
      padding:8px 0;font-size:14px;color:var(--sub);cursor:pointer}
 .tabs button.on{background:#eef4ff;border-color:#c7dbff;color:var(--blue);font-weight:600}
 .hint{color:var(--sub);font-size:13.5px}
 .warn{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:10px;
       padding:11px 13px;font-size:13.5px;margin-top:12px}
 code{background:#f1f2f4;border-radius:5px;padding:1px 6px;
      font-family:ui-monospace,Menlo,monospace;font-size:14px}
 .foot{color:var(--sub);font-size:12.5px;text-align:center;margin-top:18px}
</style></head><body>
<h1>连上抓包工作台</h1>
<div class="hint">把下面这行填进手机的 Wi-Fi 代理，填完它就会出现在电脑的设备列表里。</div>

<div class="card">
  <div class="addr">
    <div class="row"><span>主机名</span><b>__HOST__</b></div>
    <div class="row"><span>端口</span><b>__PORT__</b></div>
  </div>
  <button class="copy" onclick="cp('__HOST__')">复制主机名</button>
  <button class="copy" onclick="cp('__HOST__:__PORT__')">复制地址:端口</button>
</div>

<div class="card">
  <div class="tabs">
    <button data-t="android">Android</button>
    <button data-t="ios">iPhone / iPad</button>
  </div>
  <ol id="steps"></ol>
  <div class="hint" id="tail"></div>
  <div class="warn">手机和这台电脑必须在<b>同一个 Wi-Fi</b>；改完把 App <b>杀掉重开</b>一次。
   如果一直连不上，多半是路由器开了「AP 隔离 / 客户端隔离」，要进路由器关掉。</div>
</div>

<div class="card">
  <div><span class="tag">最后一步</span>回电脑上，在「设备与状态」里点这台设备左边的<b>圆点变绿</b> —— 绿了它的日志才会进来。</div>
</div>

<div class="foot">capture 抓包工作台 · 本页只显示接入方式，不包含任何抓包数据</div>

<script>
var S = {
  android: ["打开「设置」→「WLAN / 无线网络」",
            "长按（或点箭头进入）当前连着的那个 Wi-Fi",
            "找到「代理」，从「无 / 自动」改成 <code>手动</code>",
            "主机名填 <code>__HOST__</code>，端口填 <code>__PORT__</code>，保存"],
  ios: ["打开「设置」→「无线局域网」",
        "点当前 Wi-Fi 右边的 <code>ⓘ</code> 图标",
        "划到最下面「配置代理」→ 选 <code>手动</code>",
        "「服务器」填 <code>__HOST__</code>，「端口」填 <code>__PORT__</code>，右上角存储"]
};
function show(t){
  document.querySelectorAll(".tabs button").forEach(function(b){
    b.classList.toggle("on", b.dataset.t === t); });
  document.getElementById("steps").innerHTML =
    S[t].map(function(s){ return "<li>" + s + "</li>"; }).join("");
  document.getElementById("tail").textContent =
    t === "android" ? "有些机型在「高级选项」里才看得到代理设置。"
                    : "填完不需要重启手机，但要把 App 杀掉重开。";
}
document.querySelectorAll(".tabs button").forEach(function(b){
  b.onclick = function(){ show(b.dataset.t); }; });
show(/iPhone|iPad|iPod/i.test(navigator.userAgent) ? "ios" : "android");
function cp(t){
  if(navigator.clipboard){ navigator.clipboard.writeText(t).then(function(){ flash("已复制"); }); }
  else { flash("请长按上面的地址手动复制"); }
}
function flash(m){
  var d = document.createElement("div");
  d.textContent = m;
  d.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);" +
    "background:#1f2329;color:#fff;padding:9px 18px;border-radius:8px;font-size:14px;z-index:9";
  document.body.appendChild(d);
  setTimeout(function(){ d.remove(); }, 1400);
}
</script></body></html>
"""


def lan_web_url(port=None):
    """手机接入页的地址（二维码指向它）"""
    port = port or load_config().get("lan_web_port", 8892)
    return f"http://{lan_ip()}:{port}/"


class LanWebHandler(BaseHTTPRequestHandler):
    """只服务手机接入页 —— 不碰 flows，不给任何抓包数据"""

    server_version = f"capture/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass                                  # 手机来扫个码，不用刷日志

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path in ("/", "/index.html", "/setup"):
            dbg("PHONE", "打开接入页", f"来自 {self.client_address[0]}",
                UA=(self.headers.get("User-Agent") or "")[:60])
            cfg = load_config()
            html = (MOBILE_PAGE
                    .replace("__HOST__", esc_html(lan_ip()))
                    .replace("__PORT__", str(cfg.get("port", DEFAULT_PORT))))
            return self._send(200, html)
        if u.path == "/ping":
            return self._send(200, json.dumps({"ok": True}), "application/json")
        return self._send(404, "<h1>404</h1><p>这里只有手机接入页。</p>")

    do_HEAD = do_GET


def serve_lan_web(port):
    """手机接入页：绑所有网卡（手机才够得着），端口被占就往后顺延"""
    for p in range(port, port + 10):
        try:
            LanWebServer(("0.0.0.0", p), LanWebHandler).serve_forever()
            return
        except OSError:
            continue
        except Exception:
            break
    print(f"[手机接入页] 端口 {port}–{port + 9} 都占用了，跳过", flush=True)


class LanWebServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def cmd_serve(args):
    os.makedirs(HOME, exist_ok=True)
    try:
        rows = load_flows()
        if rows:
            ids = [(r.get("id") or 0) for r in rows]
            _count["n"] = max(ids)
            dup = len(ids) - len(set(ids))
            dbg("BOOT", "续号", f"最大 id={_count['n']} 记录数={len(ids)}",
                重复id=dup, 提示="重复>0 说明历史数据里 id 不唯一")
            for r in rows:                     # 预热客户端列表，探测线程直接用
                note_client(r.get("client"))
    except Exception:
        pass
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    cfg = load_config()
    # 首次运行（或从旧版本升上来）：一台都不预先勾选。
    # 所有设备（含本机）都以灰点出现，想听哪台就点哪台 —— 这是"默认不监听"的落点。
    # 尤其不能默认勾本机：那等于一装好就接管系统代理，工作台一停整机断网。
    if "monitored" not in cfg:
        cfg["monitored"] = []
        save_config(cfg)
    reconcile_local_proxy()          # 系统代理 ↔ 「本机是否被监听」对齐
    load_identity()                  # 身份：首次运行生成匿名 id
    reload_chains()                  # 语义：链路从 semantics.json 读
    _sem = load_semantics()
    if _sem.get("_缺失"):
        print("⚠️  找不到 semantics.json —— 字段含义不可用（AI 会读到裸字符串）", flush=True)
    web_port = cfg.get("web_port", 8891)
    lan_port = cfg.get("lan_web_port", 8892)
    threading.Thread(target=serve_web, args=(web_port,), daemon=True).start()
    threading.Thread(target=serve_lan_web, args=(lan_port,), daemon=True).start()
    dbg("BOOT", "服务已起", f"工作台 127.0.0.1:{web_port}（仅本机）",
        手机接入页=f"0.0.0.0:{lan_port}（局域网）")
    threading.Thread(target=presence_loop, daemon=True).start()

    dbg("BOOT", f"启动 v{VERSION}", f"PID {os.getpid()}",
        代理端口=args.port, 工作台=web_port, 手机页=lan_port,
        监听=monitored_set(), 本机IP=lan_ip(),
        系统代理=local_proxy_state().get("pointing_here"),
        断网保护=guard_running() or "未运行")
    print(f"capture v{VERSION} 已启动")
    print(f"  代理端口 : {args.port}")
    print(f"  手机填   : {lan_ip()}  端口 {args.port}")
    print(f"  工作台   : http://127.0.0.1:{web_port}")
    print(f"  手机扫码 : {lan_web_url(lan_port)}（工作台里有二维码）")
    print(f"  数据目录 : {HOME}")
    print(f"  脱敏     : {'开' if cfg.get('redact', True) else '关'}")
    print(f"  续号自   : {_count['n'] + 1}", flush=True)

    def bye(*_):
        # 退出前必须还原系统代理，否则电脑直接断网
        dbg("BOOT", "收到退出信号", f"PID {os.getpid()}")
        restore_proxy_if_ours()
        try:
            os.remove(PIDFILE)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    try:
        Server(("0.0.0.0", args.port), Handler).serve_forever()
    except OSError as exc:
        print(f"启动失败：{exc}")
        print(f"→ 端口 {args.port} 可能被占用，换一个：capture.py start --port 8891")
        try:
            os.remove(PIDFILE)
        except Exception:
            pass
        sys.exit(1)


# ══════════════════════════════════════════════════════════════
# 进程管理
# ══════════════════════════════════════════════════════════════

def running_pid():
    if not os.path.exists(PIDFILE):
        return None
    try:
        pid = int(open(PIDFILE).read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def port_listener(port):
    """看端口被谁占用（用于 doctor）"""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
        lines = [l for l in out.strip().split("\n")[1:] if l.strip()]
        return lines[0] if lines else None
    except Exception:
        return None


def cmd_start(args):
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)
    if running_pid():
        print(f"已在运行（PID {running_pid()}）")
        return 0
    os.makedirs(HOME, exist_ok=True)
    log = open(LOGFILE, "a")
    subprocess.Popen([sys.executable, os.path.realpath(__file__), "serve", "--port", str(port)],
                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True, cwd=HOME)
    for _ in range(30):
        time.sleep(0.2)
        if running_pid():
            break
    pid = running_pid()
    if not pid:
        print("启动失败，看 capture.log 最后几行：")
        try:
            print("".join(open(LOGFILE, encoding="utf-8", errors="replace").readlines()[-8:]))
        except Exception:
            pass
        return 1
    print(f"✅ 已启动（PID {pid}），代理端口 {port}")
    print(f"📱 手机代理填：{lan_ip()}  端口 {port}")
    print(f"🖥  工作台打开：http://127.0.0.1:{load_config().get('web_port', 8891)}")
    return 0


def cmd_stop(args):
    pid = running_pid()
    if not pid:
        # 进程已经不在了，但系统代理可能还指着我们（崩溃 / 被 kill -9）→ 兜底还原
        if restore_proxy_if_ours():
            print("⚠️  服务已不在运行，但系统代理仍指向本工具，已自动还原")
        else:
            print("没有在运行")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as e:
        print(f"停止失败：{e}")
        return 1
    time.sleep(0.5)
    try:
        os.remove(PIDFILE)
    except Exception:
        pass
    # 服务进程收到 SIGTERM 会自己还原；这里兜底覆盖进程已僵死的情况
    restore_proxy_if_ours(quiet=True)
    print(f"已停止（PID {pid}）")
    return 0


def cmd_devices(args):
    port = load_config().get("port", DEFAULT_PORT)
    seen = detect_devices()
    seen_ips = {d["client"] for d in seen}
    others = [h for h in lan_hosts() if h["ip"] not in seen_ips]
    platforms = {d.get("platform") for d in seen if d.get("platform")}
    steps = connect_steps(platforms)

    if args.json:
        print(json.dumps({
            "proxy": {"host": lan_ip(), "port": port},
            "connected": seen,
            "on_lan": others,
            "usb": [] if args.no_usb else usb_devices(),
            "how_to_connect": steps,
        }, ensure_ascii=False, indent=2))
        return 0

    print("═" * 62)
    print("  设备")
    print("═" * 62)

    if not seen:
        print("  （还没有任何设备接入）")
    for d in sorted(seen, key=lambda x: -x["requests"]):
        if d.get("platform"):
            title = d["platform"] + (f" · {d['model']}" if d.get("model") else "")
        elif d.get("probe"):
            title = f"{d['probe']}（只看到联网探测，还没发业务请求）"
        elif d.get("browser"):
            title = "浏览器流量（非 App）"
        else:
            title = "未识别（只有 HTTPS 透传，看不到头）"
        print(f"\n  ▸ {d['client']}   {title}")
        bits = []
        if d.get("app_version"):
            bits.append(f"App {d['app_version']}")
        if d.get("country"):
            bits.append(d["country"])
        if d.get("site"):
            bits.append(f"网点 {d['site']}")
        if d.get("timezone"):
            bits.append(d["timezone"])
        if bits:
            print("      " + " · ".join(bits))
        print(f"      {d['requests']} 个请求 · 最近 {(d.get('last') or '')[11:19]}")

    print()
    if others:
        print(f"  局域网内其他设备（{len(others)} 台在 ARP 表里，未走代理）")
        for h in others[:8]:
            print(f"      {h['ip']:<16} {h['mac']}")
        if len(others) > 8:
            print(f"      …… 另有 {len(others)-8} 台")
    else:
        print("  局域网内没有扫到其他设备")

    usb = [] if args.no_usb else usb_devices()
    print()
    if usb:
        print(f"  USB 上连接的设备（{len(usb)} 台）")
        for d in usb:
            bits = [d.get("platform") or "未知", d.get("name") or ""]
            if d.get("model") and d["model"] != d.get("name"):
                bits.append(d["model"])
            if d.get("os_version"):
                bits.append("iOS " + d["os_version"])
            print("      " + " · ".join(x for x in bits if x))
            if d.get("udid"):
                print(f"          UDID {d['udid']}")
        print("      → USB 连着的设备不走 Wi-Fi 代理；要抓它的包，得改用 Wi-Fi 代理")
    else:
        print("  USB 上没有检测到设备（插上手机再跑一次）")

    print()
    print("═" * 62)
    print("  怎么让设备接进来")
    print("═" * 62)
    for plat, lines in steps.items():
        print(f"\n  【{plat}】")
        for l in lines:
            print(f"    {l}")

    if not seen:
        print("\n  ⚠️ 当前没有任何设备接入 —— 按上面步骤设置手机代理即可")
    elif len(seen) == 1:
        print(f"\n  ✅ 已有 1 台设备在抓（{seen[0]['client']}）。换设备时把代理改到同一地址即可")
    else:
        print(f"\n  ✅ 已有 {len(seen)} 台设备在抓")
    return 0


def cmd_scan(args):
    """主动扫网段：不依赖设备发请求，也能发现连上的设备"""
    port = load_config().get("port", DEFAULT_PORT)
    iface = lan_interface()

    if args.subnet:
        base = args.subnet.rstrip(".")
        hosts = [f"{base}.{i}" for i in range(1, 255)]
        label = f"{base}.1 – {base}.254"
    else:
        hosts = subnet_hosts()
        label = f"{hosts[0]} – {hosts[-1]}" if hosts else "本网段"

    t0 = time.time()
    live = [] if args.no_ping else sweep(hosts, args.timeout)
    elapsed = time.time() - t0

    arp = {h["ip"]: h["mac"] for h in lan_hosts()}
    proxied = {d["client"]: d for d in detect_devices()}
    my_ip = lan_ip()

    if args.json:
        print(json.dumps({
            "subnet": label, "scanned": len(hosts), "alive": live,
            "seconds": round(elapsed, 1),
            "proxied": list(proxied.values()),
            "arp": arp,
        }, ensure_ascii=False, indent=2))
        return 0

    print("═" * 62)
    print(f"  扫描 {label}    共 {len(hosts)} 个地址    用时 {elapsed:.1f}s")
    if args.no_ping:
        print("  （跳过了 ping 扫描，只看 ARP 表）")
    print("═" * 62)

    proxied_ips = set(proxied)
    idle = [ip for ip in live if ip in arp and ip not in proxied_ips and ip != my_ip]
    others = [ip for ip in sorted(arp, key=lambda x: [int(i) for i in x.split(".")])
              if ip not in proxied_ips and ip != my_ip and ip not in idle]

    if proxied:
        print(f"\n  ✅ 已经在走代理（{len(proxied)} 台）")
        for ip, d in sorted(proxied.items()):
            tag = d.get("platform") or d.get("probe") or "未识别"
            model = f" · {d['model']}" if d.get("model") else ""
            extra = "（只看到联网探测）" if (not d.get("platform") and d.get("probe")) else ""
            print(f"      {ip:<16} {tag}{model}{extra}   {d['requests']} 个请求")
    else:
        print("\n  ⚠️ 还没有设备走代理")

    if idle:
        print(f"\n  📶 网络上活着、但没走代理（{len(idle)} 台）")
        for ip in idle[:12]:
            print(f"      {ip:<16} {arp.get(ip,'')}")
        if len(idle) > 12:
            print(f"      …… 另有 {len(idle)-12} 台")

    print(f"\n  🌐 ARP 表里的其他设备（共 {len(others)} 台，最多列 8 台）")
    for ip in others[:8]:
        print(f"      {ip:<16} {arp.get(ip,'')}")
    if len(others) > 8:
        print(f"      …… 另有 {len(others)-8} 台")

    print()
    print("═" * 62)
    print(f"  让设备接进来：把手机 Wi-Fi 代理设为 {my_ip} 端口 {port}")
    print("  Android：设置 → WLAN → 修改网络 → 高级 → 代理：手动")
    print("  iOS    ：设置 → 无线局域网 → ⓘ → 配置代理 → 手动")
    print("  详细步骤：cap devices")
    return 0


def guess_platform(name):
    n = (name or "").lower()
    if any(k in n for k in ("iphone", "ipad", "ipod")):
        return "iOS"
    if any(k in n for k in ("android", "redmi", "mi ", "xiaomi", "samsung", "sm-",
                            "huawei", "honor", "oppo", "vivo", "oneplus", "pixel",
                            "tecno", "infinix", "itel", "nokia", "motorola", "moto ")):
        return "Android"
    return None


def usb_devices():
    """USB 上连接的设备（手机等）。三条来源，能拿到哪条算哪条。"""
    found = []

    # ① iOS：libimobiledevice（idevice_id 通常随 iTunes/系统自带）
    try:
        r = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True, timeout=6)
        for udid in [x.strip() for x in r.stdout.splitlines() if x.strip()]:
            d = {"udid": udid, "platform": "iOS", "source": "usb"}
            for key, fld in (("DeviceName", "name"), ("ProductType", "model"),
                             ("ProductVersion", "os_version")):
                try:
                    rr = subprocess.run(["ideviceinfo", "-u", udid, "-k", key],
                                        capture_output=True, text=True, timeout=6)
                    v = rr.stdout.strip()
                    if v and "error" not in v.lower():
                        d[fld] = v
                except Exception:
                    pass
            d.setdefault("name", "iOS 设备")
            found.append(d)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # ② ioreg：通用 USB 设备名（Android 手机也能看到）
    try:
        r = subprocess.run(["ioreg", "-p", "IOUSB", "-w0"],
                           capture_output=True, text=True, timeout=6)
        for line in r.stdout.splitlines():
            m = re.search(r"\+-o (.+?)\s+<class (IOUSBHostDevice|IOUSBDevice)", line)
            if not m:
                continue
            name = m.group(1).strip()
            if not name or name == "Root":
                continue
            if any(d.get("name") == name for d in found):
                continue
            found.append({"name": name, "platform": guess_platform(name), "source": "usb"})
    except Exception:
        pass

    # ③ adb：装了的话能看到 Android
    try:
        r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=6)
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                found.append({"udid": parts[0], "name": "Android 设备",
                              "platform": "Android", "source": "usb"})
    except FileNotFoundError:
        pass
    except Exception:
        pass

    return found


# ── 设备在线状态（主动探测，做到"拔了立刻知道"） ──────────────

_presence = {"data": {}, "lock": threading.Lock()}


def known_client_ips():
    """所有在流量里出现过的客户端 IP（内存缓存，不再每次读文件）"""
    with _presence["lock"]:
        return sorted(_presence.setdefault("clients", set()))


def note_client(ip):
    """记录一个新的客户端 IP（由 write_flow 调用）

    本机（127.0.0.1）也收 —— 它要作为一台可选设备出现在工作台里，
    不能因为"是回环地址"就从设备列表里消失。
    """
    if ip and ip != "":
        with _presence["lock"]:
            new = ip not in _presence.setdefault("clients", set())
            _presence["clients"].add(ip)
        if new:
            # 新设备第一次冒头 —— 排查"手机到底连上没有"就靠这一行
            dbg("DEV", "发现新设备", f"ip={ip}",
                已监听=ip in monitored_set(), 提示="未监听则不会进日志视图")


def presence_loop(interval=2.0, timeout=0.6):
    """后台线程：定期 ping 每台已知设备，状态一变立刻推给浏览器"""
    while True:
        try:
            for ip in known_client_ips():
                # 本机不用 ping，它跟着这个进程活着
                ok = True if is_local(ip) else ping_once(ip, timeout)
                with _presence["lock"]:
                    rec = _presence["data"].setdefault(ip, {})
                    changed = rec.get("online") != ok
                    if changed and rec.get("checked") is not None:
                        dbg("DEV", "状态变化", f"ip={ip} → {'在线' if ok else '离线'}")
                    rec["online"] = ok
                    rec["checked"] = time.time()
                    if ok:
                        rec["last_ok"] = time.time()
                if changed:
                    broadcast("presence", {"client": ip, "online": ok})
        except Exception:
            pass
        time.sleep(interval)


def presence_of(ip):
    with _presence["lock"]:
        rec = _presence["data"].get(ip)
        return dict(rec) if rec else None


# ── 让本机流量也走本工具（等价于 Charles 的 "macOS Proxy"） ────

LOCAL_BACKUP = os.path.join(HOME, "system-proxy-backup.json")


def _networksetup(*args):
    try:
        return subprocess.run(["networksetup", *args],
                              capture_output=True, text=True, timeout=12)
    except Exception as e:
        return None


def primary_service():
    """本机主网卡对应的网络服务名（一般是 Wi-Fi）"""
    iface = lan_interface()
    r = _networksetup("-listnetworkserviceorder")
    if r and r.stdout:
        cur = None
        for line in r.stdout.splitlines():
            m = re.match(r"\(\d+\)\s+(.+)", line.strip())
            if m:
                cur = m.group(1).strip()
            if iface and f"Device: {iface}" in line and cur:
                return cur
    return "Wi-Fi"


def _parse_proxy(text):
    d = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            d[k.strip().lower()] = v.strip()
    return d


def local_proxy_state():
    """当前系统代理状态"""
    svc = primary_service()
    http = _networksetup("-getwebproxy", svc)
    https = _networksetup("-getsecurewebproxy", svc)
    h = _parse_proxy(http.stdout if http else "")
    s = _parse_proxy(https.stdout if https else "")
    return {
        "service": svc,
        "http": h,
        "https": s,
        "pointing_here": (h.get("server") == "127.0.0.1"
                          and h.get("port") == str(load_config().get("port", DEFAULT_PORT))
                          and h.get("enabled") == "Yes"),
    }


def local_proxy_on(port=None):
    """把系统代理指向本工具（先备份原值）"""
    port = port or load_config().get("port", DEFAULT_PORT)
    svc = primary_service()
    if not os.path.exists(LOCAL_BACKUP):
        st = local_proxy_state()
        with open(LOCAL_BACKUP, "w", encoding="utf-8") as f:
            json.dump({"service": svc, "http": st["http"], "https": st["https"]},
                      f, ensure_ascii=False, indent=2)
    _networksetup("-setwebproxy", svc, "127.0.0.1", str(port))
    _networksetup("-setsecurewebproxy", svc, "127.0.0.1", str(port))
    g = spawn_guard()    # 接管系统代理的同时拉起断网保护：代理崩了它负责还原
    dbg("LOCAL", "接管系统代理", f"{svc} → 127.0.0.1:{port}",
        断网保护=(f"PID {g}" if g else "未确认（可能还在启动，用 cap guard status 查）"))
    return svc


def local_proxy_off():
    """还原系统代理。返回 (服务名, restored)：
    restored=True 用备份还原 / False 没有备份直接关掉 / None 当前不是指向本工具，没敢动"""
    if not os.path.exists(LOCAL_BACKUP):
        svc = primary_service()
        # 没备份过 = 不是我们开的。只有当前确实指向本工具时才关，
        # 否则会把用户自己的代理（Clash / Charles 等）一起关掉
        if not local_proxy_state().get("pointing_here"):
            dbg("LOCAL", "未动代理", "当前不指向本工具，怕是用户自己的代理")
            return svc, None
        _networksetup("-setwebproxystate", svc, "off")
        _networksetup("-setsecurewebproxystate", svc, "off")
        dbg("LOCAL", "关闭系统代理", f"{svc}（没有备份，直接关）")
        return svc, False
    with open(LOCAL_BACKUP, encoding="utf-8") as f:
        b = json.load(f)
    svc = b.get("service") or primary_service()
    for kind, on_cmd, state_cmd in (("http", "-setwebproxy", "-setwebproxystate"),
                                    ("https", "-setsecurewebproxy", "-setsecurewebproxystate")):
        d = b.get(kind) or {}
        if d.get("server"):
            # 先把地址/端口写回原值（这步会顺带开启），再按原来的开关状态关掉。
            # 只 setwebproxystate off 的话，端口字段会残留成本工具的 8890，
            # 用户下次在系统设置里手动打开就会指向一个已经停掉的端口。
            _networksetup(on_cmd, svc, d["server"], str(d.get("port") or "0"))
        if str(d.get("enabled", "No")).lower() != "yes":
            _networksetup(state_cmd, svc, "off")
    try:
        os.remove(LOCAL_BACKUP)
    except Exception:
        pass
    dbg("LOCAL", "还原系统代理", f"{svc} 回到备份值",
        http=f"{b.get('http', {}).get('server')}:{b.get('http', {}).get('port')}",
        原状态="开" if str(b.get("http", {}).get("enabled", "No")).lower() == "yes" else "关")
    return svc, True


def restore_proxy_if_ours(quiet=False):
    """本工具退出前把系统代理还原，否则代理指向一个已停的端口 = 整台电脑断网。
    只在「确实指向本工具」时才动，用户没开过本机代理就完全不受影响。"""
    try:
        if not local_proxy_state().get("pointing_here"):
            return False
    except Exception:
        return False
    try:
        svc, restored = local_proxy_off()
    except Exception as exc:
        if not quiet:
            print(f"⚠️  系统代理自动还原失败（{exc}）")
            print("   请手动执行：cap local off")
        return False
    if restored is None:        # 当前不是指向本工具，不能动
        return False
    print(f"🔌 已还原系统代理（{svc}）"
          + ("，回到你原来的设置" if restored else "，已关闭代理，网络恢复正常"))
    return True


def reconcile_local_proxy():
    """启动时让系统代理和「本机是否被监听」保持一致。

    两种需要收拾的残局：
      1. 本机在监听，但系统代理没指向本工具（上次干净退出还原掉了）
         → 重新接上，否则界面显示绿点却一条都抓不到
      2. 本机没在监听，系统代理却还指着本工具（上次是崩溃退出的）
         → 还原掉，否则这台电脑等于断网
    """
    try:
        pointing = bool(local_proxy_state().get("pointing_here"))
        want = "127.0.0.1" in monitored_set()
        if want and not pointing:
            svc = local_proxy_on()
            print(f"🔌 正在监听本机 → 已把系统代理接到 127.0.0.1:"
                  f"{load_config().get('port', DEFAULT_PORT)}（{svc}）")
        elif pointing and not want:
            svc, restored = local_proxy_off()
            if restored is not None:
                print(f"🔌 本机不在监听，但系统代理还指着本工具 → 已还原（{svc}）")
    except Exception as exc:
        print(f"⚠️  系统代理状态校正失败（{exc}）—— 可用 cap local on/off 手动处理")


# ══════════════════════════════════════════════════════════════
# 断网保护（看门狗）
#
# 系统代理一旦指向本工具，本工具挂掉 = 这台电脑断网 —— 这是所有
# 抓包代理（Charles / Proxyman / mitmproxy）共同的命门，它们靠"退出时
# 还原"兜底，但强杀 / 崩溃时没人兜。
#
# 这个看门狗独立于代理进程存在：每 2 秒查一次「代理是否指着本工具、
# 而本工具已经不在了」。是 → 立刻还原系统代理，网络几秒内自愈。
# ══════════════════════════════════════════════════════════════

GUARDFILE = os.path.join(HOME, "capture.guard.pid")
GUARD_GRACE = 8          # 启动宽限：给代理进程留出起来的时间，避免自己人打自己人


def _pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def proxy_service_alive():
    """本工具的代理进程是否还活着"""
    try:
        with open(PIDFILE) as f:
            pid = int((f.read() or "0").strip())
    except Exception:
        pid = 0
    if pid and _pid_alive(pid):
        return True
    if pid:
        return False
    # 没有 pid 文件（比如用 cap serve 前台跑）：退回探测端口
    port = load_config().get("port", DEFAULT_PORT)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.8):
            return True
    except Exception:
        return False


def guard_running():
    try:
        with open(GUARDFILE) as f:
            pid = int((f.read() or "0").strip())
    except Exception:
        return 0
    return pid if _pid_alive(pid) else 0


def guard_loop(interval=2.0):
    """看门狗主循环：代理指着本工具、但本工具没了 → 还原系统代理"""
    with open(GUARDFILE, "w") as f:
        f.write(str(os.getpid()))
    dbg("GUARD", "上岗", f"PID {os.getpid()} 每 {interval}s 查一次")
    t0 = time.time()
    announced = False
    try:
        while True:
            time.sleep(interval)
            try:
                if not local_proxy_state().get("pointing_here"):
                    dbg("GUARD", "收工", "系统代理已不指向本工具（正常关闭）")
                    return                      # 代理已还原，收工
                if proxy_service_alive():
                    announced = False
                    continue
                # 服务不在了。刚起来的那几秒不算 —— 可能正在启动
                if time.time() - t0 < GUARD_GRACE:
                    dbg("GUARD", "宽限中", f"服务未就绪，还差 {GUARD_GRACE-(time.time()-t0):.1f}s")
                    continue
                if not announced:
                    dbg("GUARD", "触发保护", "代理指向本工具，但服务已不在 → 还原系统代理")
                    print(f"[guard] 代理指向本工具，但服务已不在 —— 还原系统代理", flush=True)
                    announced = True
                restore_proxy_if_ours(quiet=True)
                return                          # 还原完成，收工
            except Exception:
                continue                        # 看门狗绝不能自己死掉
    finally:
        try:
            if guard_running() == os.getpid():
                os.remove(GUARDFILE)
        except Exception:
            pass


def spawn_guard():
    """起一个脱离父进程的看门狗（父进程被 kill -9 也不会带走它）"""
    if guard_running():
        return guard_running()
    try:
        log = open(os.path.join(HOME, "guard.log"), "a")
    except Exception:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "guard"],
            stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, cwd=HOME)
    except Exception:
        return 0
    # 等它把 pid 文件写出来再返回。之前只等 0.15s，而 Python 进程启动要 0.3~0.6s，
    # 于是日志里写"启动失败"、其实 20 秒后正常上岗 —— 这条假日志还误导过 AI。
    for _ in range(30):                      # 最长等 3 秒
        time.sleep(0.1)
        pid = guard_running()
        if pid:
            return pid
    return 0


def cmd_guard(args):
    """cap guard [status]（一般不用手敲，起代理时自动拉起）"""
    act = args.action or "status"
    pid = guard_running()
    if act == "start":
        if pid:
            print(f"✅ 断网保护已在运行（PID {pid}）")
            return 0
        pid = spawn_guard()
        print(f"✅ 断网保护已启动（PID {pid}）" if pid else "⚠️  断网保护启动失败")
        return 0
    if act == "stop":
        if not pid:
            print("ℹ️  断网保护没在运行")
            return 0
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"✅ 已停止断网保护（PID {pid}）")
        except Exception as exc:
            print(f"⚠️  停止失败：{exc}")
        return 0
    if pid:
        print(f"✅ 断网保护运行中（PID {pid}）")
        print("   代理进程若崩溃 / 被强杀，它会自动还原系统代理，网络几秒内恢复")
    else:
        print("⚪️  断网保护未运行")
        print("   把「本机」点绿时会自动拉起；也可 cap guard start")
    return 0


def cmd_local(args):
    """cap local [on|off|status]"""
    act = args.action or "status"
    port = load_config().get("port", DEFAULT_PORT)

    if act == "on":
        svc = local_proxy_on(port)
        print(f"✅ 已把系统代理设为 127.0.0.1:{port}（服务：{svc}）")
        print("   现在这台电脑上的浏览器/App 流量也会被抓到")
        print("   ⚠️ 用完记得关：cap local off（否则本工具停了电脑上不了网）")
        return 0
    if act == "off":
        svc, restored = local_proxy_off()
        if restored is None:
            print(f"ℹ️  当前系统代理不是本工具（{svc}），未做改动")
            print("   —— 不会误关你自己的代理（Clash / Charles 等）")
        else:
            print(f"✅ 已关闭系统代理（服务：{svc}）" + ("，已还原为原来的设置" if restored else ""))
        return 0

    st = local_proxy_state()
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    print(f"  网络服务 : {st['service']}")
    print(f"  HTTP     : {st['http'].get('enabled','?')}  {st['http'].get('server','')}:{st['http'].get('port','')}")
    print(f"  HTTPS    : {st['https'].get('enabled','?')}  {st['https'].get('server','')}:{st['https'].get('port','')}")
    print(f"  指向本工具: {'是 ✅' if st['pointing_here'] else '否'}")
    return 0


def cmd_status(args):
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)
    pid = running_pid()
    rows = load_flows()
    http_rows = [r for r in rows if r.get("kind") == "http"]
    clients = sorted({r.get("client", "") for r in rows
                      if r.get("client") and r["client"] not in ("127.0.0.1", "::1")})
    last = rows[-1]["ts"][11:19] if rows else "—"

    if args.json:
        print(json.dumps({"version": VERSION, "running": bool(pid), "pid": pid,
                          "port": port, "home": HOME, "lan_ip": lan_ip(),
                          "total": len(rows), "http": len(http_rows),
                          "clients": clients, "last": last,
                          "redact": cfg.get("redact", True)}, ensure_ascii=False, indent=2))
        return 0

    print("═" * 58)
    print(f"  capture v{VERSION}")
    print(f"  状态    : {'✅ 运行中' if pid else '⛔️ 未运行'}" + (f"（PID {pid}）" if pid else ""))
    print(f"  端口    : {port}")
    print(f"  数据目录: {HOME}")
    print(f"  已记录  : {len(rows)} 条（HTTP {len(http_rows)} / CONNECT {len(rows)-len(http_rows)}）")
    print(f"  最近一条: {last}")
    if clients:
        print(f"  客户端  : {', '.join(clients)}")
    print("═" * 58)
    if not pid:
        # 最危险的组合：服务没在跑，系统代理却还指着我们 → 整台电脑上不了网
        try:
            stranded = local_proxy_state().get("pointing_here")
        except Exception:
            stranded = False
        if stranded:
            print("  🚨 系统代理仍指向本工具，但服务已停止 —— 这台电脑现在上不了网！")
            print("     修复：cap local off   （或 cap stop 会自动还原）")
            print("═" * 58)
            return 1
        print("  → 启动：cap start")
    else:
        print("  📱 手机 Wi-Fi 代理填：")
        print(f"       主机名 {lan_ip()}")
        print(f"       端口   {port}")
        print(f"  🖥  工作台：http://127.0.0.1:{cfg.get('web_port', 8891)}")
    return 0


def cmd_doctor(args):
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)
    rows = load_flows()
    clients = sorted({r.get("client", "") for r in rows
                      if r.get("client") and r["client"] not in ("127.0.0.1", "::1")})
    pid = running_pid()
    problems, tips = [], []

    # 1 环境
    py_ok = sys.version_info >= (3, 7)
    if not py_ok:
        problems.append(f"Python 版本过低：{sys.version.split()[0]}（需要 3.7+）")

    # 2 数据目录
    try:
        os.makedirs(HOME, exist_ok=True)
        t = os.path.join(HOME, ".w_test")
        open(t, "w").close()
        os.remove(t)
        writable = True
    except Exception as e:
        writable = False
        problems.append(f"数据目录不可写：{HOME}（{e}）")

    # 3 代理进程
    if not pid:
        problems.append("代理未运行")

    # 4 端口占用
    listener = port_listener(port)
    port_taken_by_other = bool(listener and not pid)

    # 5 客户端
    if pid and not clients:
        tips.append("还没有手机流量进来 —— 检查手机 Wi-Fi 代理是否指向本机")

    # 6 PATH
    exe = os.path.join(HOME, "cap")
    on_path = any(os.path.exists(os.path.join(p, "cap"))
                  for p in os.environ.get("PATH", "").split(":"))

    if args.json:
        print(json.dumps({
            "version": VERSION, "python": sys.version.split()[0], "python_ok": py_ok,
            "home": HOME, "home_writable": writable, "running": bool(pid), "pid": pid,
            "port": port, "port_listener": listener, "port_conflict": port_taken_by_other,
            "clients": clients, "total": len(rows), "on_path": on_path,
            "problems": problems, "tips": tips,
        }, ensure_ascii=False, indent=2))
        return 0 if not problems else 1

    def line(ok, label, detail=""):
        print(f"  {'✅' if ok else '❌'} {label:<14}{detail}")

    print("═" * 58)
    print(f"  capture 自检  v{VERSION}")
    print("═" * 58)
    line(py_ok, "Python", sys.version.split()[0])
    line(writable, "数据目录", HOME)
    line(bool(pid), "代理进程", f"PID {pid}" if pid else "未运行")
    line(not port_taken_by_other, "端口 " + str(port),
         (listener[:60] if port_taken_by_other else "可用"))
    line(bool(clients), "手机客户端", ", ".join(clients) if clients else "还没有流量进来")
    line(on_path, "PATH 命令", "cap 已可用" if on_path else "未装到 PATH")
    print("═" * 58)

    if port_taken_by_other:
        print(f"\n⚠️ 端口 {port} 被别的进程占用：")
        print(f"   {listener}")
        print(f"   → 换端口启动：cap start --port 8891")

    if problems:
        print("\n发现的问题：")
        for p in problems:
            print(f"  • {p}")

    if not pid:
        print("\n修复：cap start")
    elif not clients:
        print("\n排查顺序：")
        print("  1. 手机和电脑必须连同一个 Wi-Fi")
        print(f"  2. 手机 Wi-Fi → 修改网络 → 高级 → 代理：手动")
        print(f"     主机名 {lan_ip()}   端口 {port}")
        print("  3. 改完把 App 杀掉重开（否则还走旧连接）")
        print("  4. 手机浏览器打开 http://example.com ，回来跑 cap list 验证")
    else:
        print("\n一切正常 ✅  手机上操作 App 即可，随时 cap list 查看")

    if not on_path:
        print(f"\n想把 cap 装进 PATH：bash {SCRIPT_DIR}/install.sh")
    return 0 if not problems else 1


def cmd_config(args):
    cfg = load_config()
    if args.action == "show" or (args.action is None and args.key is None):
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0
    if args.action == "set" and args.key:
        val = args.value
        if args.key in ("port", "web_port"):
            val = int(val)
            val = int(val)
        elif args.key in ("redact", "noise"):
            val = str(val).lower() in ("1", "true", "yes", "on")
        cfg[args.key] = val
        save_config(cfg)
        print(f"已设置 {args.key} = {val}")
        if args.key == "port":
            print("→ 重启生效：cap stop && cap start")
        return 0
    print("用法：cap config           查看\n      cap config set port 8891\n      cap config set redact false")
    return 1


# ══════════════════════════════════════════════════════════════
# 查询
# ══════════════════════════════════════════════════════════════

def _parse_since(s):
    m = re.fullmatch(r"(\d+)([smh])", s)
    now = datetime.now()
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n), "h": timedelta(hours=n)}[unit]
        return (now - delta).strftime("%Y-%m-%dT%H:%M:%S")
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        return now.strftime("%Y-%m-%dT") + s + ":00"
    return s


def _filter(rows, args):
    if getattr(args, "host", None):
        rows = [r for r in rows if args.host in str(r.get("host", ""))]
    if getattr(args, "path", None):
        rows = [r for r in rows if args.path in str(r.get("path", ""))]
    if getattr(args, "method", None):
        rows = [r for r in rows if str(r.get("method", "")).upper() == args.method.upper()]
    if getattr(args, "status", None):
        rows = [r for r in rows if str(r.get("status")) == str(args.status)]
    if getattr(args, "client", None):
        rows = [r for r in rows if args.client in str(r.get("client", ""))]
    if getattr(args, "since", None):
        cut = _parse_since(args.since)
        if cut:
            rows = [r for r in rows if r.get("ts", "") >= cut]
    if getattr(args, "grep", None):
        kw = args.grep
        rows = [r for r in rows if kw in json.dumps(r, ensure_ascii=False)]
    # 杂音过滤：默认折叠，--all 显示
    if not getattr(args, "all", False):
        rows = [r for r in rows if not record_is_noise(r)]
    return rows


def brief(r):
    if r.get("kind") == "connect":
        line = f"[{r['id']:>5}] {r['ts'][11:19]}  CONNECT  {r.get('host')}:{r.get('port')}"
        return line + ("   ❌ 连不上" if r.get("error") else "")
    st = r.get("status")
    if st is None:
        # 没有状态码 = 请求根本没走通，把原因露出来，别只显示一个 None
        tail = f"→ ❌ {str(r.get('error') or '无响应').split(':')[0]}"
    else:
        tail = f"→ {st} ({r.get('ms')}ms)"
    return (f"[{r['id']:>5}] {r['ts'][11:19]}  {str(r.get('method')):<6} "
            f"{r.get('host')}:{r.get('port')}{r.get('path')}  {tail}")


def pretty(text, limit=None):
    if not text:
        return "(空)"
    s = text
    try:
        s = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except Exception:
        pass
    if limit and len(s) > limit:
        s = s[:limit] + f"\n…[共 {len(s)} 字符]"
    return s


def cmd_list(args):
    all_rows = load_flows()
    rows = _filter(all_rows, args)
    hidden = len(all_rows) - len([r for r in all_rows if not record_is_noise(r)]) \
        if not args.all else 0
    if args.last:
        rows = rows[-args.last:]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"共 {len(rows)} 条" + (f"（已折叠 {hidden} 条系统杂音，--all 可看）" if hidden and not args.all else ""))
    for r in rows:
        print(brief(r))
    return 0


def cmd_show(args):
    rows = {r["id"]: r for r in load_flows()}
    r = rows.get(args.id)
    if not r:
        print(f"没有 id={args.id} 的记录")
        return 1
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print("─" * 66)
    print(brief(r))
    if r.get("kind") == "connect":
        if r.get("error"):
            print("  ❌ HTTPS 隧道建立失败（连不上目标，业务请求根本没发出去）")
            print(f"  【错误】{r['error']}")
        else:
            print("  HTTPS 透传，正文未解密（只记录了目标主机）")
        return 0
    print("─" * 66)
    print("【请求头】")
    print(pretty(json.dumps(r.get("req_headers", {}), ensure_ascii=False), 3000))
    print("\n【请求体】")
    print(pretty(r.get("req_body", ""), args.limit))
    print("\n【响应头】")
    print(pretty(json.dumps(r.get("resp_headers", {}), ensure_ascii=False), 1500))
    print("\n【响应体】")
    print(pretty(r.get("resp_body", ""), args.limit))
    if r.get("error"):
        print(f"\n【错误】{r['error']}")
    return 0


def cmd_trace(args):
    """cap trace [--last N] [--grep 关键字] [--follow] —— 读调试日志"""
    if getattr(args, "clear", False):
        open(TRACEFILE, "w").close()
        print(f"已清空 {TRACEFILE}")
        return 0
    n = getattr(args, "last", None) or 60
    g = getattr(args, "grep", None)
    lines = trace_tail(n, g)
    if not lines:
        print(f"（还没有调试日志：{TRACEFILE}）")
        print("  日志在服务处理请求时产生；确认 config 里 debug 没被关掉")
        return 0
    print("═" * 78)
    print(f"  调试日志  {TRACEFILE}" + (f"   过滤: {g}" if g else ""))
    print("═" * 78)
    for l in lines:
        print("  " + l)
    if getattr(args, "follow", False):
        print("── 持续跟踪（Ctrl-C 退出）──")
        seen = len(lines)
        try:
            while True:
                time.sleep(0.6)
                cur = trace_tail(4000, g)
                if len(cur) < seen:
                    seen = 0
                for l in cur[seen:]:
                    print("  " + l, flush=True)
                seen = len(cur)
        except KeyboardInterrupt:
            pass
    return 0


def cmd_tail(args):
    n = len(load_flows())
    print(f"从第 {n} 条开始监听（Ctrl-C 退出）……")
    try:
        while True:
            time.sleep(1)
            rows = load_flows()
            for r in rows[n:]:
                if record_is_noise(r) and not args.all:
                    continue
                print(brief(r), flush=True)
            n = len(rows)
    except KeyboardInterrupt:
        return 0



# ══════════════════════════════════════════════════════════════
# AI 模块
#
# 干什么：用户装不上、抓不到、看不懂报文时，用自然语言问它；
#        它读的是**语义化的现场数据**（设备状态 + 日志 + 数据字典），
#        所以能给出"你的手机没连过来，因为 X"这种结论，而不是泛泛而谈。
#
# 不绑定厂商：走 OpenAI 兼容协议，换 base_url 即可。
# ══════════════════════════════════════════════════════════════

CREDFILE = os.path.join(HOME, "credentials.json")

PROVIDERS = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                 "models": ["deepseek-chat", "deepseek-reasoner"],
                 "key_url": "https://platform.deepseek.com/api_keys"},
    "openai":   {"name": "OpenAI", "base_url": "https://api.openai.com/v1",
                 "models": ["gpt-4o-mini", "gpt-4o"],
                 "key_url": "https://platform.openai.com/api-keys"},
    "moonshot": {"name": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1",
                 "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
                 "key_url": "https://platform.moonshot.cn/console/api-keys"},
    "dashscope": {"name": "阿里通义", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "models": ["qwen-plus", "qwen-turbo"],
                  "key_url": "https://bailian.console.aliyun.com/"},
    "custom":   {"name": "自定义（任何 OpenAI 兼容接口）", "base_url": "",
                 "models": [], "key_url": ""},
}


def load_creds():
    """读凭据文件（0600）。不存在返回空壳。"""
    try:
        with open(CREDFILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_creds(d):
    os.makedirs(HOME, exist_ok=True)
    with open(CREDFILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CREDFILE, 0o600)
    except Exception:
        pass
    dbg("AI", "保存凭据", f"provider={d.get('provider')} model={d.get('model')}",
        key=("已设置(" + str(len(d.get("api_key", ""))) + "位)") if d.get("api_key") else "空")


def ai_config():
    """合并 config 与 credentials，得到实际生效的 AI 配置"""
    cfg = load_config()
    cr = load_creds()
    prov = cr.get("provider") or cfg.get("ai_provider", "deepseek")
    preset = PROVIDERS.get(prov, PROVIDERS["custom"])
    base = (cr.get("base_url") or cfg.get("ai_base_url") or preset["base_url"] or "").rstrip("/")
    return {
        "provider": prov,
        "provider_name": preset["name"],
        "base_url": base,
        "model": cr.get("model") or cfg.get("ai_model") or (preset["models"] or [""])[0],
        "api_key": cr.get("api_key") or "",
        "enabled": bool(cfg.get("ai_enabled", False)),
        "max_records": int(cfg.get("ai_max_records", 80)),
        "preset_models": preset["models"],
        "key_url": preset["key_url"],
    }


# 本机可能存着 key 的地方。顺序 = 优先级，越靠前越先试。
KEY_SOURCES = [
    ("环境变量 DEEPSEEK_API_KEY", "env:DEEPSEEK_API_KEY", "deepseek"),
    ("环境变量 OPENAI_API_KEY",   "env:OPENAI_API_KEY",   "openai"),
    ("~/.zshrc",                  "~/.zshrc",             "deepseek"),
    ("~/.bash_profile",           "~/.bash_profile",      "deepseek"),
    ("~/.bashrc",                 "~/.bashrc",            "deepseek"),
    ("~/.profile",                "~/.profile",           "deepseek"),
    ("DSH 配置",                  "~/.dsh/settings.yaml", "deepseek"),
    ("~/.deepseek/config.json",   "~/.deepseek/config.json", "deepseek"),
]


def _read_text(where):
    if where.startswith("env:"):
        return os.environ.get(where[4:], "") or ""
    try:
        with open(os.path.expanduser(where), encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def find_keys():
    """扫描本机可能存 key 的地方，返回 [(来源说明, 厂商, key), ...]（去重，保序）"""
    out, seen = [], set()
    pat = re.compile(r"""(?:api[_-]?key|API[_-]?KEY|token)["']?\s*[:=]\s*["']?(sk-[A-Za-z0-9_\-]{16,})""")
    loose = re.compile(r"sk-[A-Za-z0-9_\-]{16,}")
    for label, where, prov in KEY_SOURCES:
        txt = _read_text(where)
        if not txt:
            continue
        keys = pat.findall(txt) or (loose.findall(txt) if where.startswith("env:") else [])
        for k in keys:
            if k in seen:
                continue
            seen.add(k)
            out.append((label, prov, k))
    return out


def ai_ready():
    a = ai_config()
    return bool(a["api_key"] and a["base_url"] and a["model"])


def _ai_post(path, payload, timeout=60, stream=False):
    a = ai_config()
    url = a["base_url"].rstrip("/") + path
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + a["api_key"],
                 "Accept": "text/event-stream" if stream else "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def ai_error_text(exc):
    """把 HTTP 错误翻译成人能看懂、能照做的话"""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            body = ""
        code = exc.code
        if code == 401:
            return "API Key 无效或已失效 —— 请在下方重新填写（到服务商控制台重新生成一个）"
        if code == 402:
            return "账户余额不足 —— 请到服务商控制台充值"
        if code == 403:
            return "没有权限访问该模型 —— 检查 key 的权限或换一个模型"
        if code == 404:
            return "接口地址或模型名不存在 —— 检查 Base URL 是否要带 /v1，模型名是否拼对"
        if code == 429:
            return "请求太频繁或超出配额 —— 稍等一下再试"
        if code >= 500:
            return f"服务商暂时故障（HTTP {code}）—— 稍后重试"
        return f"请求被拒绝（HTTP {code}）：{body}"
    if isinstance(exc, urllib.error.URLError):
        return f"连不上服务商 —— 检查网络/代理，或 Base URL 是否写错（{exc.reason}）"
    return f"{type(exc).__name__}: {exc}"


def ai_stream(messages, temperature=0.3, timeout=90):
    """流式对话，逐块 yield 文本增量。出错时 yield 一句人话。"""
    if not ai_ready():
        yield "⚠️ 还没配置 AI。请在「AI 助手」面板里填 API Key（可以点「一键填写」自动读取本机已有的 key）。"
        return
    a = ai_config()
    payload = {"model": a["model"], "messages": messages, "stream": True,
               "temperature": temperature}
    try:
        resp = _ai_post("/chat/completions", payload, timeout=timeout, stream=True)
    except Exception as exc:
        dbg("AI", "请求失败", ai_error_text(exc)[:80])
        yield "⚠️ " + ai_error_text(exc)
        return
    got = False
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            ch = (obj.get("choices") or [{}])[0]
            piece = (ch.get("delta") or {}).get("content") or ""
            if piece:
                got = True
                yield piece
            if ch.get("finish_reason") == "length":
                yield "\n\n_（回复被长度限制截断）_"
    except Exception as exc:
        yield "\n\n⚠️ 读取流时中断：" + str(exc)[:200]
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if not got:
        yield "⚠️ 模型没有返回内容。可能是模型名不对，或该模型不支持流式。"


def ai_list_models():
    """拉取服务商可用模型（用来填下拉框）"""
    if not ai_config()["api_key"]:
        return []
    try:
        a = ai_config()
        req = urllib.request.Request(
            a["base_url"].rstrip("/") + "/models",
            headers={"Authorization": "Bearer " + a["api_key"]})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return sorted(m.get("id", "") for m in (d.get("data") or []) if m.get("id"))
    except Exception as exc:
        dbg("AI", "拉模型列表失败", ai_error_text(exc)[:70])
        return []


def ai_test():
    """最小连通性测试：发一句，看能不能拿到回复"""
    if not ai_ready():
        return False, "还没填 API Key"
    try:
        resp = _ai_post("/chat/completions", {
            "model": ai_config()["model"],
            "messages": [{"role": "user", "content": "回复两个字：可用"}],
            "max_tokens": 12, "stream": False}, timeout=30)
        d = json.loads(resp.read().decode("utf-8", "replace"))
        txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return True, (txt or "连上了").strip()[:40]
    except Exception as exc:
        return False, ai_error_text(exc)


# ── 上下文包：把"现场"整理成模型能读懂的东西 ──────────────

def ai_context(scope="auto", limit_records=None):
    """组装给模型的现场数据。全部来自语义层和实时状态，不含猜测。"""
    cfg = load_config()
    a = ai_config()
    lim = limit_records or a["max_records"]
    sem = load_semantics()
    ident = load_identity()
    rows = load_flows()
    mine = [r for r in rows if r.get("user_id") == ident.get("user_id")]

    # 1) 我是谁 / 装在哪
    ctx = {
        "身份": {"user_id": ident.get("user_id"), "类型": ident.get("kind"),
                 "本机IP": lan_ip(), "数据目录": HOME},
        "服务": {
            "代理端口": cfg.get("port"), "工作台": "http://127.0.0.1:%s" % cfg.get("web_port"),
            "手机接入页": lan_web_url(), "监听中的设备": sorted(monitored_set()),
            "系统代理被本工具接管": bool(local_proxy_state().get("pointing_here")),
            "断网保护": bool(guard_running()),
        },
        "数据规模": {"总记录": len(rows), "带身份的新记录": len(mine),
                     "语义版本": sem.get("version"), "字段数": sem_count("fields")},
    }

    # 2) 设备现场：谁在线、谁在监听、谁连不上
    devs = []
    for d in assemble_devices()[:14]:
        item = {"ip": d.get("client"), "名称": d.get("title"),
                "来源": d.get("source"), "记录数": d.get("requests"),
                "在线": d.get("online"), "已监听": bool(d.get("monitored"))}
        if d.get("mac"):
            item["mac"] = d["mac"]
        devs.append(item)
    ctx["设备"] = devs

    # 3) 语义字典（模型靠它把字段名翻译成人话）
    ctx["数据字典"] = {
        "字段": {k: v for k, v in (sem.get("fields") or {}).items() if not k.startswith("_")},
        "接口": {k: v.get("名称") for k, v in (sem.get("apis") or {}).items()
                 if not k.startswith("_")},
        "链路": {k: [e.get("label") for e in v]
                 for k, v in (sem.get("chains") or {}).items() if not k.startswith("_")},
        "还没核对的": (sem.get("pending") or {}).get("items", []),
    }

    # 4) 最近发生了什么（日志：诊断的关键证据）
    # 日志标签也要有字典 —— 否则模型会把「GUARD 上岗」误读成"保护启动失败"
    ctx["日志标签说明"] = {
        "BOOT": "进程启动/退出。'服务已起'=端口就绪；'续号'=从第几条继续编号",
        "PROXY": "放行了一个客户端连接，说明设备请求确实到了本工具",
        "DROP": "拒绝了一个客户端（不在允许网段）—— 连上了但被网段限制挡掉",
        "DEV": "发现新设备 / 设备在线状态翻转",
        "MON": "监听开关的切换：谁改的、改成了什么、结果如何",
        "LOCAL": "系统代理的接管/还原，含备份的原值",
        "GUARD": "'上岗'=断网保护开始值守（正常）；'收工'=代理已还原所以它退出（正常）；"
                 "'触发保护'=代理崩了它去救网络（异常事件）",
        "WEB": "工作台取数据：文件总条数 → 过滤后返回几条",
        "API": "接口层的异常情况",
        "PHONE": "有设备打开了手机接入页（二维码扫到了）",
        "SEM": "数据字典的读写",
        "AI": "AI 模块的提问与回答",
    }
    ctx["最近日志"] = trace_tail(50)

    # 5) 最近的报文
    # 用一行一条的紧凑文本，不用 JSON 对象数组 —— 200 条对象要 3 万字符，
    # 换成行文本后只有几千，省下的都是模型的注意力（和用户的电费）。
    recent = rows[-lim:]
    lines = ["格式: id|时刻|方法|主机|路径|状态|耗时ms|设备|备注"]
    for r in recent:
        note = []
        if r.get("kind") == "connect":
            note.append("隧道")
        if r.get("noise"):
            note.append("杂音")
        if r.get("error"):
            note.append("错误:" + str(r["error"])[:40])
        lines.append("|".join([
            str(r.get("id")), (r.get("ts") or "")[11:19], str(r.get("method") or ""),
            str(r.get("host") or ""), (r.get("path") or "")[:60],
            str(r.get("status") or ""), str(r.get("ms") or ""),
            str(r.get("client") or ""), ",".join(note),
        ]))
    ctx["最近报文"] = "\n".join(lines)
    return ctx


SYSTEM_DIAGNOSE = """你是「抓包工作台」的内置助手，坐在用户的 Mac 上帮他排查问题。

工作台是什么：一个 HTTP(S) 抓包代理。手机或本机把 Wi-Fi 代理指向它，报文就会落盘成
JSONL，人和 AI 都能直接读。界面在 http://127.0.0.1:{web_port}。

重要机制（诊断时必须用上）：
- 「监听」是显式开关：设备列表里每个设备左边有个圆点，**变绿才抓**。没点绿的设备，
  报文照旧落盘，但不进界面列表 —— 所以"手机连上了但看不到数据"最常见的原因就是没点绿。
- 代理绑在 0.0.0.0:{port}，手机要填 http://{ip}:{port} 作为 Wi-Fi 代理。
- 手机接入页在 {phone_url}，用手机浏览器能打开就说明网络通。
- 本机流量需要把系统代理指向本工具（会把 Mac 所有流量都抓进来，有断网保护看门狗兜底）。
- HTTPS 只记录域名（CONNECT 隧道），看不到内容；要解密得在手机上装根证书。
- 同网段设备扫不到/连不上，常见原因是路由器开了 AP 隔离。

排查顺序（按这个来，别乱猜）：
1. 设备列表里有没有这台设备？没有 → 请求根本没到本工具（网络/代理没配对/AP 隔离）
2. 有设备但「离线」→ 连过又断了
3. 在线、但没点绿 → 点绿
4. 点绿了还没数据 → 看最近日志里 PROXY 有没有放行、WEB 取请求返回几条
5. HTTPS 只有域名 → 这是设计如此，不是故障

{context}

回答要求：
- 用中文，直接说结论和下一步动作，不要罗列所有可能性
- **引用你看到的证据**（哪个设备、哪条日志、哪个数字），不要说"可能""大概"
- 需要用户操作时，说清楚点哪里/填什么
- 不确定就说不确定，并说明怎么验证 —— 绝对不要编造字段含义或接口名
"""

SYSTEM_ANALYZE = """你是「抓包工作台」的内置数据分析助手。用户抓到了报文，想从里面得出结论。

你拿到的数据里有一个「数据字典」，它记录了**已经人工核对过**的字段含义、单位和取值。
这是你理解报文的唯一依据：
- 字典里有的 → 直接用，并在回答里体现（例如 unitVolume 的单位是 cm³）
- 字典里没有的 → 明确说"这个字段的含义还没核对过"，**绝对不要猜**
- 「还没核对的」列表里的东西，说明确实存疑

分析要求：
- 用中文，先给结论，再给支撑它的具体记录（id、时间、接口、字段值）
- 数字要能对上：你说"3 次失败"，就要能指出是哪 3 条
- 发现异常时，说清异常在哪一步、正常应该是什么样
- 用户问的是"如何验证某个字段怎么传的"这类问题时，告诉他该看哪几条记录的哪个字段

{context}
"""


def ai_system_prompt(scope="auto"):
    cfg = load_config()
    ctx = ai_context(scope)
    tmpl = SYSTEM_ANALYZE if scope == "analyze" else SYSTEM_DIAGNOSE
    head = tmpl.format(
        web_port=cfg.get("web_port", 8891), port=cfg.get("port", DEFAULT_PORT),
        ip=lan_ip(), phone_url=lan_web_url(),
        context="【当前现场数据（实时采集）】\n" +
                json.dumps(ctx, ensure_ascii=False, separators=(",", ":")))
    return head


MANIFESTFILE = os.path.join(HOME, "manifest.json")


def load_manifest():
    try:
        with open(MANIFESTFILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def cmd_manifest(args):
    """cap manifest [--check] —— 看功能自述；--check 校验声明与现实是否一致

    声明和现实一定会漂移（改了端口忘了改 manifest、加了接口没登记）。
    所以不靠自觉，靠这个命令查。
    """
    m = load_manifest()
    if not m:
        print(f"❌ 找不到或读不了 {MANIFESTFILE}")
        return 1

    if not getattr(args, "check", False):
        print(f"【{m.get('name')}】 {m.get('id')} v{m.get('version')}  ({m.get('type')})")
        print(f"  {m.get('description', '')}")
        print(f"  入口      : {m.get('runtime', {}).get('entry')}")
        print(f"  端口      : {m.get('ports')}")
        print(f"  权限      : {', '.join(p['id'] for p in m.get('permissions', []))}")
        prod = m.get("data", {}).get("produces", [])
        print(f"  产出数据  : {', '.join(d['file'] for d in prod)}")
        ep = m.get("exposes", {}).get("endpoints", [])
        print(f"  对外接口  : {len(ep)} 个只读 + "
              f"{len(m.get('exposes', {}).get('writes', []))} 个写入")
        print(f"\n  cap manifest --check   校验声明与现实是否一致")
        return 0

    print("═" * 62)
    print(f"  校验【{m.get('id')}】的声明 vs 现实")
    print("═" * 62)
    bad = 0

    def chk(ok, label, detail=""):
        nonlocal bad
        if not ok:
            bad += 1
        print(f"  {'✅' if ok else '❌'} {label:<34} {detail}")

    # 文件存在
    rt = m.get("runtime", {})
    chk(bool(rt.get("entry")) and os.path.exists(os.path.join(HOME, rt["entry"])),
        "入口文件存在", rt.get("entry", "?"))
    ui = m.get("ui", {})
    chk(bool(ui.get("entry")) and os.path.exists(os.path.join(HOME, ui["entry"])),
        "界面文件存在", ui.get("entry", "?"))
    for d in m.get("data", {}).get("produces", []):
        chk(os.path.exists(os.path.join(HOME, d["file"])), "数据文件存在", d["file"])
        if d.get("semantics"):
            chk(os.path.exists(os.path.join(HOME, d["semantics"])),
                "语义文件存在", d["semantics"])

    # 端口与配置一致
    cfg = load_config()
    ports = m.get("ports", {})
    if "proxy" in ports:
        chk(ports["proxy"] == cfg.get("port", DEFAULT_PORT), "代理端口与 config 一致",
            f"manifest={ports['proxy']} config={cfg.get('port', DEFAULT_PORT)}")
    if "web" in ports:
        chk(ports["web"] == cfg.get("web_port", 8891), "工作台端口与 config 一致",
            f"manifest={ports['web']} config={cfg.get('web_port', 8891)}")
    if "lan_web" in ports:
        chk(ports["lan_web"] == cfg.get("lan_web_port", 8892), "手机页端口与 config 一致",
            f"manifest={ports['lan_web']} config={cfg.get('lan_web_port', 8892)}")

    # 声明的接口在服务里真的存在
    try:
        import urllib.request
        base = m.get("exposes", {}).get("base", "http://127.0.0.1:8891")
        for ep in m.get("exposes", {}).get("endpoints", []):
            path = ep["path"]
            probe = {"flows": "?since_id=999999", "flow": "?id=1",
                     "semantics": "?what=orderType"}.get(path.rsplit("/", 1)[-1], "")
            try:
                with urllib.request.urlopen(base + path + probe, timeout=3) as r:
                    code = r.status
            except Exception:
                code = 0
            chk(code == 200, "接口可达 " + path, f"HTTP {code}")
    except Exception as exc:
        chk(False, "接口校验", str(exc))

    # 权限声明与实际行为相符（改了系统代理就必须声明）
    perm_ids = {p["id"] for p in m.get("permissions", [])}
    chk("system.proxy" in perm_ids or not os.path.exists(LOCAL_BACKUP),
        "系统代理权限已声明", "用到就必须声明")

    # 字段类型（契约的一部分：id 是整数，写成字符串会让续号崩掉）
    try:
        rows = load_flows()
        if rows:
            chk(isinstance(rows[0].get("id"), int), "id 字段是整数",
                "实际是 " + type(rows[0].get("id")).__name__)
            badt = [k for k in ("kind", "method", "host", "path", "ts", "client")
                    if rows[0].get(k) is not None and not isinstance(rows[0].get(k), str)]
            chk(not badt, "文本字段是字符串", ("异常: " + ",".join(badt)) if badt else "抽查通过")
    except Exception as exc:
        chk(False, "字段类型校验", str(exc))

    # id 唯一 + 单调（游标协议的地基：不唯一/不递增，界面就会漏数据）
    _rows = []
    try:
        _rows = load_flows()
        ids = [(r.get("id") or 0) for r in _rows]
        dup = len(ids) - len(set(ids))
        chk(dup == 0, "记录 id 唯一", f"重复 {dup} 个" if dup else f"{len(ids)} 条")
        noninc = sum(1 for i in range(len(ids) - 1) if ids[i] >= ids[i + 1])
        chk(noninc == 0, "记录 id 递增（游标可用）", f"{noninc} 处逆序" if noninc else "单调")
    except Exception as exc:
        chk(False, "id 校验", str(exc))

    # ── 数据契约：逐条校验（不抽查）──
    # 契约要点：15 个原始字段只加不改；按 kind 的字段集差异是设计使然
    #（connect 是 HTTPS 透传，本来就没有 HTTP 正文）；
    # status 可以为 null，但**当且仅当**请求失败（有 error），因为没拿到响应就没有状态码。
    _TYPES = {
        "kind": (str,), "method": (str,), "host": (str,), "port": (int,),
        "path": (str,), "url": (str,), "req_headers": (str, dict), "req_body": (str,),
        "status": (int,), "reason": (str,), "resp_headers": (str, dict),
        "resp_body": (str,), "ms": (int, float), "id": (int,), "ts": (str,),
    }
    _REQ_BY_KIND = {
        "connect": ["kind", "method", "host", "port", "id", "ts"],
        "http": ["kind", "method", "host", "port", "path", "url", "id", "ts"],
    }
    _MAY_MISS = {
        "connect": {"path", "url", "req_headers", "req_body", "status", "reason",
                    "resp_headers", "resp_body", "ms"},
        "http": {"reason", "resp_headers", "resp_body"},
    }
    try:
        if _rows:
            bad_type, bad_kind = [], []
            for r in _rows:
                for k, allowed in _TYPES.items():
                    if k not in r:
                        continue
                    v = r[k]
                    if v is None and k == "status":
                        if not r.get("error"):
                            bad_type.append(f"id={r.get('id')} status=null 但没有 error")
                        continue
                    if not isinstance(v, allowed):
                        bad_type.append(f"id={r.get('id')} {k}={type(v).__name__}")
                kd = r.get("kind")
                req = _REQ_BY_KIND.get(kd)
                if req is None:
                    bad_kind.append(f"id={r.get('id')} 未知 kind={kd!r}")
                    continue
                for f in req:
                    if f not in r:
                        bad_kind.append(f"id={r.get('id')} kind={kd} 缺必填 {f}")
                for f in _TYPES:
                    if f not in r and f not in _MAY_MISS.get(kd, set()):
                        bad_kind.append(f"id={r.get('id')} kind={kd} 缺字段 {f}")
                if kd == "http":
                    hs, he = isinstance(r.get("status"), int), bool(r.get("error"))
                    if hs and he:
                        bad_kind.append(f"id={r.get('id')} status 和 error 同时有")
                    if not hs and not he:
                        bad_kind.append(f"id={r.get('id')} 既无 status 也无 error")
            chk(not bad_type, "字段类型逐条校验（15 字段不改类型）",
                ("; ".join(bad_type[:3]) + (f" 等 {len(bad_type)} 处" if len(bad_type) > 3 else ""))
                if bad_type else f"{len(_rows)} 条全部通过")
            chk(not bad_kind, "按 kind 的字段完整性",
                ("; ".join(bad_kind[:3]) + (f" 等 {len(bad_kind)} 处" if len(bad_kind) > 3 else ""))
                if bad_kind else "connect/http 各自该有的都在")
            # 原始字段没被改名/删除
            _allk = set()
            for r in _rows:
                _allk |= set(r.keys())
            _lost = [k for k in _TYPES if k not in _allk]
            chk(not _lost, "原始 15 字段未被删改",
                ("丢了 " + ",".join(_lost)) if _lost else
                "全在（新增字段：" + "、".join(sorted(_allk - set(_TYPES))) + "）")
    except Exception as exc:
        chk(False, "数据契约逐条校验", str(exc))

    # 语义版本
    sem = load_semantics()
    chk(sem.get("version", 0) > 0, "语义文件可读",
        f"v{sem.get('version')} 字段 {sem_count('fields')} 个 / 接口 {sem_count('apis')} 个")

    print("─" * 62)
    print(f"  {'✅ 声明与现实一致' if not bad else f'⚠️  {bad} 处不一致 —— 改代码后记得同步 manifest'}")
    return 1 if bad else 0


def cmd_chain(args):
    reload_chains()
    rows = _filter(load_flows(), args)
    name = args.name
    if name not in CHAINS:
        print(f"未知链路 {name}，可选：{', '.join(CHAINS)}")
        return 1
    if args.json:
        print(json.dumps({label: [r for r in rows if kw in str(r.get("path", ""))]
                          for kw, label in CHAINS[name]}, ensure_ascii=False, indent=2))
        return 0
    print(f"链路【{name}】   共 {len(rows)} 条记录")
    print("═" * 66)
    for kw, label in CHAINS[name]:
        hits = [r for r in rows if kw in str(r.get("path", ""))]
        print(f"\n▸ {label}   命中 {len(hits)} 条")
        for r in hits[-args.limit_rows:]:
            print("   " + brief(r))
            if args.full and r.get("kind") == "http":
                print("   【请求体】" + pretty(r.get("req_body", ""), 1200).replace("\n", "\n   "))
                print("   【响应体】" + pretty(r.get("resp_body", ""), 1200).replace("\n", "\n   "))
    return 0


def cmd_digest(args):
    rows = _filter(load_flows(), args)
    if not rows:
        print("(没有记录)")
        return 0
    http = [r for r in rows if r.get("kind") == "http"]
    errors = [r for r in http if r.get("error") or (r.get("status") or 0) >= 400]
    agg = {}
    for r in http:
        key = f"{r.get('method')} {r.get('path','').split('?')[0]}"
        a = agg.setdefault(key, {"n": 0, "codes": {}, "ms": []})
        a["n"] += 1
        a["codes"][str(r.get("status"))] = a["codes"].get(str(r.get("status")), 0) + 1
        if r.get("ms"):
            a["ms"].append(r["ms"])
    top = sorted(agg.items(), key=lambda kv: -kv[1]["n"])

    if args.json:
        print(json.dumps({
            "range": [rows[0].get("ts"), rows[-1].get("ts")],
            "total": len(rows), "http": len(http), "errors": len(errors),
            "endpoints": [{"endpoint": k, "count": v["n"], "codes": v["codes"],
                           "avg_ms": (sum(v["ms"]) // len(v["ms"])) if v["ms"] else None}
                          for k, v in top],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"时间范围 : {rows[0]['ts'][11:19]} ~ {rows[-1]['ts'][11:19]}")
    print(f"总量     : {len(rows)} 条（HTTP {len(http)} / CONNECT {len(rows)-len(http)}）")
    print(f"异常     : {len(errors)} 条" + ("  ⚠️" if errors else "  ✅"))
    print(f"客户端   : {', '.join(sorted({r.get('client','') for r in rows if r.get('client')}))}")
    print("\n接口聚合（按调用次数）")
    print("─" * 78)
    for k, v in top[: args.top]:
        avg = (sum(v["ms"]) // len(v["ms"])) if v["ms"] else 0
        codes = " ".join(f"{c}×{n}" for c, n in v["codes"].items())
        print(f"  {v['n']:>4}次  {codes:<14} {avg:>5}ms  {k}")
    if errors:
        print("\n异常明细")
        print("─" * 78)
        for r in errors[:10]:
            print("  " + brief(r))
            if r.get("error"):
                print(f"        {r['error']}")
    return 0


def cmd_grep(args):
    kw = args.keyword
    rows = [r for r in load_flows() if kw in json.dumps(r, ensure_ascii=False)]
    if not args.all:
        rows = [r for r in rows if not record_is_noise(r)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"命中 {len(rows)} 条（关键字：{kw}）")
    for r in rows[-args.last:]:
        print(brief(r))
        for field in ("req_body", "resp_body"):
            body = r.get(field) or ""
            if kw in body:
                i = body.find(kw)
                print(f"    {field}: …{body[max(0,i-80):i+160].strip()}…")
    return 0


def cmd_clear(args):
    if os.path.exists(FLOWS):
        os.replace(FLOWS, FLOWS + f".{datetime.now().strftime('%Y%m%d%H%M%S')}")
    print("已清空（旧文件保留为备份）")
    return 0


def cmd_open(args):
    """在浏览器打开工作台"""
    cfg = load_config()
    port = args.port or cfg.get("web_port", 8891)
    url = f"http://127.0.0.1:{port}"
    if not running_pid():
        print("代理没在运行，先启动：cap start")
        return 1
    try:
        subprocess.run(["open", url], check=False) if sys.platform == "darwin" \
            else subprocess.run(["xdg-open", url], check=False)
    except Exception:
        pass
    print(f"已在浏览器打开：{url}")
    return 0


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(prog="capture", description=f"抓包工作台 v{VERSION}")
    p.add_argument("--version", action="version", version=f"capture {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_filters(sp):
        sp.add_argument("--json", action="store_true", help="输出 JSON（给 AI 用）")
        sp.add_argument("--host", help="按主机过滤")
        sp.add_argument("--path", help="按路径过滤")
        sp.add_argument("--method", help="按方法过滤")
        sp.add_argument("--status", help="按状态码过滤")
        sp.add_argument("--client", help="按客户端 IP 过滤")
        sp.add_argument("--since", help="起始时间：5m / 2h / HH:MM")
        sp.add_argument("--grep", help="全文搜索（含请求/响应体）")
        sp.add_argument("--all", action="store_true", help="包含系统杂音")

    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int, required=True); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("status"); sp.add_argument("--port", type=int); sp.add_argument("--json", action="store_true"); sp.set_defaults(f=cmd_status)
    sp = sub.add_parser("doctor"); sp.add_argument("--port", type=int); sp.add_argument("--json", action="store_true"); sp.set_defaults(f=cmd_doctor)
    sp = sub.add_parser("devices"); sp.add_argument("--json", action="store_true"); sp.add_argument("--no-usb", action="store_true"); sp.set_defaults(f=cmd_devices)
    sp = sub.add_parser("local"); sp.add_argument("action", nargs="?", choices=["on", "off", "status"]); sp.add_argument("--json", action="store_true"); sp.set_defaults(f=cmd_local)
    sp = sub.add_parser("guard"); sp.add_argument("action", nargs="?", choices=["start", "stop", "status"]); sp.set_defaults(f=cmd_guard)
    sp = sub.add_parser("scan")
    sp.add_argument("--subnet", help="只扫某个前缀，如 172.17.38")
    sp.add_argument("--timeout", type=float, default=0.5, help="单机 ping 超时秒数")
    sp.add_argument("--no-ping", action="store_true", help="跳过 ping 扫描，只看 ARP 表")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(f=cmd_scan)
    sp = sub.add_parser("config"); sp.add_argument("action", nargs="?", choices=["show", "set"]); sp.add_argument("key", nargs="?"); sp.add_argument("value", nargs="?"); sp.set_defaults(f=cmd_config)

    sp = sub.add_parser("list"); add_filters(sp); sp.add_argument("--last", type=int); sp.set_defaults(f=cmd_list)
    sp = sub.add_parser("show"); sp.add_argument("id", type=int); sp.add_argument("--json", action="store_true"); sp.add_argument("--limit", type=int, default=4000); sp.set_defaults(f=cmd_show)
    sp = sub.add_parser("tail"); sp.add_argument("--all", action="store_true"); sp.set_defaults(f=cmd_tail)
    sp = sub.add_parser("chain"); add_filters(sp); sp.add_argument("--name", default="录单"); sp.add_argument("--full", action="store_true"); sp.add_argument("--limit-rows", type=int, default=3); sp.set_defaults(f=cmd_chain)
    sp = sub.add_parser("digest"); add_filters(sp); sp.add_argument("--top", type=int, default=20); sp.set_defaults(f=cmd_digest)
    sp = sub.add_parser("grep"); sp.add_argument("keyword"); sp.add_argument("--json", action="store_true"); sp.add_argument("--last", type=int, default=5); sp.add_argument("--all", action="store_true"); sp.set_defaults(f=cmd_grep)
    sp = sub.add_parser("manifest"); sp.add_argument("--check", action="store_true")
    sp.set_defaults(f=cmd_manifest)
    sp = sub.add_parser("trace"); sp.add_argument("--last", type=int, default=60)
    sp.add_argument("--grep"); sp.add_argument("--follow", "-f", action="store_true")
    sp.add_argument("--clear", action="store_true"); sp.set_defaults(f=cmd_trace)
    sub.add_parser("clear").set_defaults(f=cmd_clear)
    sp = sub.add_parser("open"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_open)

    args = p.parse_args()
    if args.cmd == "guard" and not args.action:
        # 内部入口：spawn_guard() 用 `capture.py guard`（不带动作）把看门狗跑起来
        guard_loop()
        sys.exit(0)
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
