#!/usr/bin/env python3
"""崩溃日志符号化 —— 把一堆十六进制地址变成能看懂的函数名。

为什么做这个
  App 崩了拿到的日志长这样：

      8   MyApp   0x0000000104a1b2c4 0x104a00000 + 111300

  这行信息量为零。要变成：

      8   MyApp   RecordOrderViewController.viewDidLoad() + 84   RecordOrderVC.swift:218

  得拿**同一份构建**的 dSYM 去查。麻烦在于：
    · dSYM 在一堆归档里，得按 UUID 找
    · 找错了 UUID，符号化出来是错的（比不符号化更危险）
    · 命令是 atos / symbolicatecrash，参数记不住

这个工具做的事
  ① 读崩溃日志，把里面所有二进制的 UUID 列出来
  ② 在你的归档 / DerivedData 里按 UUID 找对应 dSYM
  ③ **明确告诉你哪个找到了、哪个没找到**（没找到的不会瞎猜）
  ④ 跑 atos 符号化，输出带文件名行号的结果

安全
  · 全程只读 —— 不碰崩溃日志、不碰 dSYM、不碰工程
  · 找不到 dSYM 就直说，不用别的版本糊弄（那会得出错误结论）
"""
import glob
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
HOME = os.path.abspath(os.environ.get("CRASHLAB_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "crashlab.pid")
LOGFILE = os.path.join(HOME, "crashlab.log")
RESULTS = os.path.join(HOME, "results.jsonl")      # 只追加
DEFAULT_PORT = 8922

ARCHIVE_ROOTS = [
    os.path.expanduser("~/Library/Developer/Xcode/Archives"),
    os.path.expanduser("~/Library/Developer/Xcode/DerivedData"),
    os.path.expanduser("~/Library/Developer/Xcode/iOS DeviceSupport"),
]
CRASH_DIRS = [
    os.path.expanduser("~/Library/Logs/DiagnosticReports"),
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Downloads"),
]


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} [crashlab] {msg}"
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


# ── 读崩溃日志 ──────────────────────────────────────────────

def parse_crash(path):
    """支持 .ips（现代）和 .crash（老式）。返回统一结构。"""
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在：" + path}
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    out = {"ok": True, "path": path, "name": os.path.basename(path),
           "format": "ips" if path.endswith(".ips") else "crash"}

    if path.endswith(".ips"):
        # .ips = 第一行是 header JSON，后面是 body JSON
        nl = raw.find("\n")
        if nl < 0:
            return {"ok": False, "error": "文件格式不对（没有换行）"}
        try:
            head = json.loads(raw[:nl])
            body = json.loads(raw[nl:])
        except Exception as exc:
            return {"ok": False, "error": "解析 .ips 失败：%s" % exc}
        out["app"] = head.get("app_name")
        out["version"] = head.get("app_version")
        out["build"] = head.get("build_version")
        out["bundle"] = head.get("bundleID")
        out["os"] = head.get("os_version")
        out["device"] = head.get("modelCode") or head.get("device")
        out["uuid"] = (head.get("slice_uuid") or "").lower()
        exc_info = body.get("exception") or {}
        out["exception"] = exc_info.get("type") or exc_info.get("codes") or ""
        out["signal"] = exc_info.get("signal") or ""
        term = body.get("termination") or {}
        out["termination"] = term.get("indicator") or term.get("reason") or ""
        # 二进制映像表：imageIndex -> {name, uuid, base, path}
        images = {}
        for i, im in enumerate(body.get("usedImages") or []):
            images[i] = {"name": im.get("name"), "uuid": (im.get("uuid") or "").lower(),
                         "base": im.get("base"), "path": im.get("path")}
        out["images"] = images
        # 崩溃线程的帧
        threads = body.get("threads") or []
        idx = body.get("faultingThread")
        if idx is None:
            idx = 0 if threads else -1
        frames = (threads[idx].get("frames") or []) if 0 <= idx < len(threads) else []
        out["frames"] = [{"imageIndex": f.get("imageIndex"),
                          "imageOffset": f.get("imageOffset"),
                          "symbol": f.get("symbol") or "",
                          "symbolLocation": f.get("symbolLocation"),
                          "sourceFile": f.get("sourceFile") or "",
                          "sourceLine": f.get("sourceLine")}
                         for f in frames]
        out["thread_name"] = (threads[idx].get("name") if 0 <= idx < len(threads) else "") or ""
        return out

    # 老式 .crash：文本格式
    txt = raw
    def grab(pat, default=""):
        m = re.search(pat, txt)
        return m.group(1).strip() if m else default
    out["app"] = grab(r"^Process:\s+(\S+)", "")
    out["version"] = grab(r"^Version:\s+(\S+)", "")
    out["bundle"] = grab(r"^Identifier:\s+(\S+)", "")
    out["os"] = grab(r"^OS Version:\s+(.+)$", "")
    out["device"] = grab(r"^Hardware Model:\s+(\S+)", "")
    out["exception"] = grab(r"^Exception Type:\s+(.+)$", "")
    out["termination"] = grab(r"^Termination Reason:\s+(.+)$", "")
    # Binary Images 段：0x... - 0x... Name arch <uuid> /path
    images = {}
    imgs = re.findall(
        r"^(0x[0-9a-f]+)\s+-\s+(0x[0-9a-f]+)\s+(\S+)\s+(\S+)\s+<([0-9a-fA-F-]+)>\s+(\S+)",
        txt, re.M)
    for i, (lo, hi, name, arch, uuid, p) in enumerate(imgs):
        images[i] = {"name": name, "uuid": uuid.lower(),
                     "base": int(lo, 16), "path": p}
    out["images"] = images
    out["uuid"] = (images.get(0) or {}).get("uuid", "")
    # 崩溃线程的 Backtrace
    frames = []
    m = re.search(r"^Thread \d+ Crashed:(.*?)(?=^Thread \d+|^Binary Images:)",
                  txt, re.S | re.M)
    if m:
        for line in m.group(1).splitlines():
            fm = re.match(r"^\d+\s+(\S+)\s+(0x[0-9a-f]+)\s+(.*)$", line.strip())
            if not fm:
                continue
            name, addr, rest = fm.group(1), fm.group(2), fm.group(3)
            # rest 可能是 "0x... + 123" 或 "symbol + 123 (file:line)"
            sym = ""
            sm = re.match(r"^(.+?)\s+\+\s+\d+", rest)
            if sm and not sm.group(1).startswith("0x"):
                sym = sm.group(1)
            # 通过地址反查属于哪个 image
            ai, off = None, None
            try:
                a = int(addr, 16)
                best = None
                for i, im in images.items():
                    b = im.get("base") or 0
                    if b and a >= b and (best is None or b > (images[best].get("base") or 0)):
                        best = i
                if best is not None:
                    ai = best
                    off = a - (images[best].get("base") or 0)
            except ValueError:
                pass
            frames.append({"imageIndex": ai, "imageOffset": off,
                           "symbol": sym, "sourceFile": "", "sourceLine": None})
    out["frames"] = frames
    return out


# ── 找 dSYM ────────────────────────────────────────────────

def macho_uuids(path):
    """直接读 Mach-O 头拿 UUID —— **不要调 dwarfdump**。

    踩过的坑：原来每个 dSYM 都 spawn 一次 dwarfdump，
    116 个归档里几千个 dSYM，扫一次 60 秒都跑不完，接口直接超时。
    其实 UUID 就在 Mach-O 的 LC_UUID 里，读文件头就能拿到 ——
    这样是毫秒级。
    """
    out = []
    try:
        with open(path, "rb") as f:
            data = f.read(64 * 1024)      # 头部足够装下所有 load command
    except OSError:
        return out
    import struct
    MAGICS = {0xfeedface: ("<", 32), 0xfeedfacf: ("<", 64),   # 32/64 位小端
              0xcefaedfe: (">", 32), 0xcffaedfe: (">", 64)}  # 大端
    # fat binary（通用二进制）
    if len(data) >= 8 and data[:4] in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        return out                        # fat 的 dSYM 少见，交给 dwarfdump 兜底
    if len(data) < 32:
        return out
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic not in MAGICS:
        magic = struct.unpack_from(">I", data, 0)[0]
    if magic not in MAGICS:
        return out
    endian, bits = MAGICS[magic]
    try:
        ncmds = struct.unpack_from(endian + "I", data, 16)[0]
        off = 32 if bits == 64 else 28
        for _ in range(min(ncmds, 4096)):
            if off + 8 > len(data):
                break
            cmd, cmdsize = struct.unpack_from(endian + "II", data, off)
            if cmdsize < 8:
                break
            if cmd == 0x1b and off + 24 <= len(data):      # LC_UUID
                u = data[off + 8:off + 24]
                h = u.hex()
                out.append(("%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16],
                                                h[16:20], h[20:]), path))
                break
            off += cmdsize
    except Exception:
        pass
    return out


def dsym_uuid(dsym_path):
    """读一个 dSYM 的 UUID（可能有多个架构切片）"""
    binp = os.path.join(dsym_path, "Contents", "Resources", "DWARF")
    if not os.path.isdir(binp):
        return []
    out = []
    for f in os.listdir(binp):
        p = os.path.join(binp, f)
        if os.path.isfile(p):
            out.extend(macho_uuids(p))
    return out


def index_dsyms(roots=None, max_seconds=60):
    """扫归档建索引：UUID -> dSYM 路径。

    116 个归档里有成千上万个 dSYM，全扫很慢 ——
    所以带时间上限，扫到够用就返回。
    """
    roots = roots or ARCHIVE_ROOTS
    # UUID 是构建时就定死的，不会变 —— 索引缓存到磁盘，第二次秒开
    cache = os.path.join(HOME, "dsym_index.json")
    try:
        st = os.stat(cache)
        if time.time() - st.st_mtime < 7 * 86400 and not roots_changed(roots):
            with open(cache, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("idx"):
                return d["idx"], d.get("count", 0), False
    except Exception:
        pass
    idx, scanned = {}, 0
    t0 = time.time()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for cur, dirs, files in os.walk(root):
            if time.time() - t0 > max_seconds:
                log(f"索引扫描到时间上限（{max_seconds}s），扫了 {scanned} 个 dSYM")
                return idx, scanned, True
            dirs[:] = [d for d in dirs if d != "Index.noindex"]
            for d in list(dirs):
                if d.endswith(".dSYM"):
                    p = os.path.join(cur, d)
                    for u, binp in dsym_uuid(p):
                        idx.setdefault(u, p)
                    scanned += 1
                    dirs.remove(d)
            for d in list(dirs):
                if d.endswith(".framework") or d.endswith(".app"):
                    pass
    log(f"dSYM 索引完成：{scanned} 个，{len(idx)} 个 UUID，用时 {time.time()-t0:.1f}s")
    try:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"idx": idx, "count": scanned, "ts": time.time(),
                       "roots": roots}, f)
        # **顺手把 stamp 也写了** —— 不然第二次跑时 roots_changed 看到
        # 没有 stamp 就返回 True，缓存又被跳过（得第三次才命中）
        stamp = os.path.join(HOME, "dsym_index.stamp")
        now = [os.path.getmtime(r) for r in roots if os.path.isdir(r)]
        with open(stamp, "w", encoding="utf-8") as f:
            json.dump(now, f)
    except Exception:
        pass
    return idx, scanned, False


def roots_changed(roots):
    """归档目录的 mtime 变了吗（有新归档就重建索引）"""
    try:
        stamp = os.path.join(HOME, "dsym_index.stamp")
        now = tuple(os.path.getmtime(r) for r in roots if os.path.isdir(r))
        old = json.load(open(stamp, encoding="utf-8")) if os.path.exists(stamp) else None
        changed = old != list(now)
        json.dump(list(now), open(stamp, "w", encoding="utf-8"))
        return changed
    except Exception:
        return False


# ── 符号化 ─────────────────────────────────────────────────

def atos_symbolicate(binary, load_addr, addresses, arch="arm64"):
    """用 atos 把地址批量转成符号"""
    if not addresses:
        return {}
    cmd = ["atos", "-arch", arch, "-o", binary, "-l",
           hex(load_addr) if isinstance(load_addr, int) else str(load_addr)]
    cmd += [hex(a) if isinstance(a, int) else str(a) for a in addresses]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        lines = [l for l in (r.stdout or "").splitlines()]
        return {a: lines[i] if i < len(lines) else ""
                for i, a in enumerate(addresses)}
    except Exception as exc:
        return {a: "（atos 失败：%s）" % exc for a in addresses}


def symbolicate(path, uuid_filter=None, max_frames=40):
    """符号化一个崩溃日志。只读。"""
    c = parse_crash(path)
    if not c.get("ok"):
        return c
    idx, n_dsym, capped = index_dsyms()
    images = c.get("images") or {}
    # 逐个 image 找 dSYM
    resolved = {}
    for i, im in images.items():
        u = (im.get("uuid") or "").lower()
        if not u:
            continue
        p = idx.get(u)
        resolved[i] = {"uuid": u, "name": im.get("name"),
                       "found": bool(p), "dsym": p,
                       "binary": None}
        if p:
            binp = os.path.join(p, "Contents", "Resources", "DWARF")
            if os.path.isdir(binp):
                # 优先取和 image 同名的那个
                cand = os.path.join(binp, im.get("name") or "")
                if os.path.isfile(cand):
                    resolved[i]["binary"] = cand
                else:
                    fs = [os.path.join(binp, f) for f in os.listdir(binp)
                          if os.path.isfile(os.path.join(binp, f))]
                    resolved[i]["binary"] = fs[0] if fs else None
    # 逐帧符号化（按 image 分组批量调 atos，快）
    frames = c.get("frames") or []
    todo = {}
    for n, f in enumerate(frames[:max_frames]):
        ai = f.get("imageIndex")
        off = f.get("imageOffset")
        if ai is None or off is None:
            continue
        r = resolved.get(ai)
        if not r or not r.get("binary"):
            continue
        base = (images.get(ai) or {}).get("base")
        if base is None:
            continue
        todo.setdefault(ai, []).append((n, base + off, base))
    for ai, lst in todo.items():
        r = resolved[ai]
        base = lst[0][2]
        addrs = [x[1] for x in lst]
        got = atos_symbolicate(r["binary"], base, addrs)
        for n, addr, _b in lst:
            frames[n]["resolved"] = got.get(addr, "")

    # 统计
    have = sum(1 for f in frames if f.get("resolved") or f.get("symbol"))
    miss_imgs = [r for r in resolved.values() if not r["found"]]
    result = {
        "ok": True, "crash": {
            "name": c["name"], "app": c.get("app"), "version": c.get("version"),
            "bundle": c.get("bundle"), "os": c.get("os"), "device": c.get("device"),
            "exception": c.get("exception"), "signal": c.get("signal"),
            "termination": c.get("termination"), "thread": c.get("thread_name"),
        },
        "frames": frames[:max_frames],
        "total_frames": len(frames),
        "resolved_frames": have,
        "images": [{"index": i, **v} for i, v in sorted(resolved.items())],
        "missing_dsym": [{"index": i, "name": v["name"], "uuid": v["uuid"]}
                         for i, v in resolved.items() if not v["found"]],
        "dsym_index": {"count": n_dsym, "uuids": len(idx), "capped": capped},
        "how_to_read": ("上面每一行：帧号 / 二进制 / 地址 / 符号。"
                        "符号里带文件名和行号的那几帧，就是你要去改的地方。"
                        "标了「没找到 dSYM」的二进制，它的帧符号化不出来 —— "
                        "**别用别的版本的 dSYM 去凑**，那会得出错误结论。"),
    }
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "user_id": _user_id(),
           "crash": c["name"], "app": c.get("app"),
           "resolved": have, "total": len(frames),
           "missing": len(miss_imgs)}
    try:
        with open(RESULTS, "a", encoding="utf-8") as f:      # 只追加
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return result


def find_crashes(days=14):
    """找最近的崩溃日志"""
    out = []
    cutoff = time.time() - days * 86400
    for d in CRASH_DIRS:
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not (n.endswith(".ips") or n.endswith(".crash")):
                continue
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            out.append({"path": p, "name": n, "dir": d,
                        "kb": round(st.st_size / 1024, 1),
                        "when": datetime.fromtimestamp(st.st_mtime)
                                .strftime("%m-%d %H:%M")})
    out.sort(key=lambda x: x["when"], reverse=True)
    return out


def dsym_stats():
    idx, n, capped = index_dsyms(max_seconds=25)
    return {"dsym_count": n, "uuid_count": len(idx), "capped": capped,
            "roots": [r for r in ARCHIVE_ROOTS if os.path.isdir(r)]}


# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "crashlab/" + VERSION

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
                "version": VERSION, "crashes": find_crashes(30),
                "archive_roots": [r for r in ARCHIVE_ROOTS if os.path.isdir(r)],
            }, ensure_ascii=False))
        if u.path == "/api/dsym":
            return self._send(200, json.dumps(dsym_stats(), ensure_ascii=False))
        if u.path == "/api/parse":
            p = (qs.get("path", [""])[0] or "").strip()
            if not p:
                return self._send(400, json.dumps({"ok": False, "error": "没给 path"},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(parse_crash(p), ensure_ascii=False))
        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()
        if u.path == "/api/symbolicate":
            p = (b.get("path") or "").strip()
            if not p:
                return self._send(400, json.dumps({"ok": False, "error": "没给 path"},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(
                symbolicate(p, max_frames=int(b.get("max_frames") or 40)),
                ensure_ascii=False))
        if u.path == "/api/reveal":
            p = (b.get("path") or "").strip()
            if p and os.path.exists(p):
                subprocess.Popen(["open", "-R", p] if os.path.isfile(p) else ["open", p])
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
    log(f"崩溃日志符号化 v{VERSION} 已启动 http://127.0.0.1:{port}")

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


def cmd_symbolicate(args):
    r = symbolicate(args.crash)
    if not r.get("ok"):
        print("  ❌", r.get("error"))
        return 1
    c = r["crash"]
    print(f"  {c['app']} {c['version']}（{c['bundle']}）")
    print(f"  异常：{c['exception']} {c.get('signal','')}")
    print(f"  设备：{c['device']}  系统：{c['os']}")
    print(f"  符号化 {r['resolved_frames']}/{r['total_frames']} 帧\n")
    for i, f in enumerate(r["frames"][:20]):
        sym = f.get("resolved") or f.get("symbol") or "(没符号)"
        img = (r["images"][f.get("imageIndex")] or {}).get("name") if f.get("imageIndex") is not None else "?"
        print("  %2d  %-24s %s" % (i, (img or "?")[:24], sym[:96]))
    if r["missing_dsym"]:
        print("\n  没找到 dSYM 的二进制（这些帧符号化不出来）：")
        for m in r["missing_dsym"][:8]:
            print("    %-28s %s" % (m["name"], m["uuid"]))
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="crashlab", description=f"崩溃日志符号化 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int)
    sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("symbolicate"); sp.add_argument("crash")
    sp.set_defaults(f=cmd_symbolicate)
    a = p.parse_args()
    sys.exit(a.f(a))


if __name__ == "__main__":
    main()
