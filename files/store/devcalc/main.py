#!/usr/bin/env python3
"""程序员计算器 —— 进制、位运算、颜色、单位、时间戳。

为什么做这个
  调接口时天天要换算：这个 0x1F 是多少、这个权限位都开了啥、
  这个颜色 #2f6df6 的 RGB 是多少、这个 1757850000 是几点。
  系统计算器要么不带这些，要么切来切去。

一个页面全都有，输入即算。
纯本地，零依赖。
"""
import json
import os
import re
import signal
import struct
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("DEVCALC_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "devcalc.pid")
LOGFILE = os.path.join(HOME, "devcalc.log")
DEFAULT_PORT = 8912


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# 进制
# ══════════════════════════════════════════════════════════════

def parse_number(s):
    """智能识别输入的数字：0x/0b/0o 前缀、下划线、结尾的 h/b/o"""
    if s is None:
        return None, "空的"
    t = str(s).strip().replace("_", "").replace(" ", "").replace(",", "")
    if not t:
        return None, "空的"
    neg = t.startswith("-")
    if neg:
        t = t[1:]
    base = 10
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", t):
        base, t = 16, t[2:]
    elif re.fullmatch(r"0[bB][01]+", t):
        base, t = 2, t[2:]
    elif re.fullmatch(r"0[oO][0-7]+", t):
        base, t = 8, t[2:]
    elif re.fullmatch(r"[0-9a-fA-F]+[hH]", t):
        base, t = 16, t[:-1]
    elif re.fullmatch(r"[01]+[bB]", t):
        base, t = 2, t[:-1]
    elif re.fullmatch(r"[0-7]+[oO]", t):
        base, t = 8, t[:-1]
    elif re.fullmatch(r"\d+", t):
        base = 10
    elif re.fullmatch(r"[0-9a-fA-F]+", t):
        base = 16                       # 含字母就当十六进制
    else:
        return None, "认不出这是什么进制"
    try:
        v = int(t, base)
    except ValueError:
        return None, "不是合法的数字"
    return (-v if neg else v), None


def fmt_bases(v):
    """各种进制和各种长度的表示"""
    if v is None:
        return {}
    u = v & 0xFFFFFFFFFFFFFFFF if v < 0 else v
    out = {
        "十进制": str(v),
        "十六进制": ("-" if v < 0 else "") + format(abs(v), "X"),
        "八进制": ("-" if v < 0 else "") + format(abs(v), "o"),
        "二进制": ("-" if v < 0 else "") + format(abs(v), "b"),
    }
    # 带前缀
    out["0x 形式"] = ("-" if v < 0 else "") + "0x" + format(abs(v), "X")
    out["0b 形式"] = ("-" if v < 0 else "") + "0b" + format(abs(v), "b")
    # 常见字节长度的补码表示（调试协议时特别有用）
    for bits in (8, 16, 32, 64):
        if 0 <= v < (1 << bits) or (v < 0 and abs(v) <= (1 << (bits - 1))):
            mask = (1 << bits) - 1
            out[f"{bits} 位"] = "0x" + format(u & mask, f"0{bits // 4}X")
    # 分组显示
    b = format(abs(v), "b")
    grouped = " ".join(b[max(0, i - 4):i] for i in range(len(b), 0, -4))[::-1]
    grouped = " ".join(reversed([b[max(0, len(b) - i - 4):len(b) - i]
                                 for i in range(0, len(b), 4)]))
    out["二进制分组"] = ("-" if v < 0 else "") + grouped
    if 32 <= v <= 0x10FFFF and not (0xD800 <= v <= 0xDFFF):
        try:
            out["Unicode 字符"] = chr(v)
        except Exception:
            pass
    try:
        raw = (u & 0xFFFFFFFF).to_bytes(4, "big")
        out["4 字节(网络序)"] = " ".join(f"{x:02X}" for x in raw)
        out["4 字节(小端)"] = " ".join(f"{x:02X}" for x in raw[::-1])
    except Exception:
        pass
    try:
        f = struct.unpack(">f", struct.pack(">I", u & 0xFFFFFFFF))[0]
        if f == f and abs(f) not in (float("inf"),):
            out["按 float 解释"] = f"{f:.6g}"
    except Exception:
        pass
    return out


# ══════════════════════════════════════════════════════════════
# 位运算
# ══════════════════════════════════════════════════════════════

def bitops(a, b, width=0):
    if a is None or b is None:
        return None
    ops = {
        "A & B（按位与）": a & b,
        "A | B（按位或）": a | b,
        "A ^ B（按位异或）": a ^ b,
        "~A（按位取反）": ~a,
        "A << B": (a << b) if 0 <= b < 64 else None,
        "A >> B": (a >> b) if 0 <= b < 64 else None,
    }
    out = {}
    for k, v in ops.items():
        if v is None:
            out[k] = "（位移量超出范围）"
        else:
            out[k] = f"{v}　({v & 0xFFFFFFFFFFFFFFFF if v >= 0 else v:#x})"
    return out


def describe_bits(v, width=32):
    """把二进制每一位的含义列出来 —— 看权限位/标志位用"""
    if v is None:
        return []
    rows = []
    for i in range(width - 1, -1, -1):
        on = (v >> i) & 1
        rows.append({"bit": i, "on": bool(on), "value": (1 << i) if on else 0})
    return rows


def hamming(a, b):
    if a is None or b is None:
        return None
    x = a ^ b
    return {"不同位数": bin(x).count("1"), "相同位数": bin(~x & 0xFFFFFFFF).count("1") & 0xFFFFFFFF,
            "异或值": f"{x} (0x{x:X})"}


# ══════════════════════════════════════════════════════════════
# 颜色
# ══════════════════════════════════════════════════════════════

def parse_color(s):
    """认 #RGB / #RRGGBB / #AARRGGBB / rgb(r,g,b) / rgba(...) / 整数"""
    if not s:
        return None, "空的"
    t = str(s).strip()
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", t, re.I)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        alpha = float(m.group(4)) if m.group(4) else 1.0
        if max(r, g, b) > 255:
            return None, "RGB 值要在 0~255"
        return {"r": r, "g": g, "b": b, "a": alpha}, None
    h = t.lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", h):
        return {"r": int(h[0] * 2, 16), "g": int(h[1] * 2, 16), "b": int(h[2] * 2, 16),
                "a": 1.0}, None
    if re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return {"r": int(h[0:2], 16), "g": int(h[2:4], 16), "b": int(h[4:6], 16),
                "a": 1.0}, None
    if re.fullmatch(r"[0-9a-fA-F]{8}", h):
        # 前两位当 alpha（iOS/Android 常见写法）
        return {"r": int(h[2:4], 16), "g": int(h[4:6], 16), "b": int(h[6:8], 16),
                "a": int(h[0:2], 16) / 255}, None
    return None, "认不出这个颜色（支持 #RGB / #RRGGBB / #AARRGGBB / rgb() / rgba()）"


def color_info(c):
    r, g, b = c["r"], c["g"], c["b"]
    a = c.get("a", 1.0)
    hx = f"#{r:02X}{g:02X}{b:02X}"
    # 亮度、对比色（决定文字用黑还是白）
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    # HSV
    mx, mn = max(r, g, b), min(r, g, b)
    v = mx / 255
    s = 0 if mx == 0 else (mx - mn) / mx
    if mx == mn:
        hdeg = 0
    elif mx == r:
        hdeg = (60 * ((g - b) / (mx - mn)) + 360) % 360
    elif mx == g:
        hdeg = 60 * ((b - r) / (mx - mn)) + 120
    else:
        hdeg = 60 * ((r - g) / (mx - mn)) + 240
    out = {
        "HEX": hx,
        "HEX(大写)": hx.upper(),
        "RGB": f"rgb({r}, {g}, {b})",
        "RGBA": f"rgba({r}, {g}, {b}, {a:g})",
        "整数(0x)": f"0x{r:02X}{g:02X}{b:02X}",
        "整数(十)": str(r << 16 | g << 8 | b),
        "ARGB": f"#{int(round(a*255)):02X}{r:02X}{g:02X}{b:02X}",
        "HSV": f"{hdeg:.0f}°, {s*100:.0f}%, {v*100:.0f}%",
        "亮度": f"{lum*100:.1f}%",
        "对比色（文字用）": "#000000" if lum > 0.55 else "#FFFFFF",
        "SwiftUI": f"Color(red: {r/255:.3f}, green: {g/255:.3f}, blue: {b/255:.3f})",
        "iOS UIColor": f"UIColor(red: {r/255:.3f}, green: {g/255:.3f}, blue: {b/255:.3f}, alpha: {a:g})",
        "Android": f"#{int(round(a*255)):02X}{r:02X}{g:02X}{b:02X}",
    }
    return out


# ══════════════════════════════════════════════════════════════
# 单位换算 / 时间戳
# ══════════════════════════════════════════════════════════════

UNITS = {
    "长度": {"m": 1, "km": 1000, "cm": 0.01, "mm": 0.001, "inch": 0.0254,
             "foot": 0.3048, "mile": 1609.344, "海里": 1852},
    "重量": {"kg": 1, "g": 0.001, "t": 1000, "lb": 0.45359237, "oz": 0.028349523125},
    "数据": {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4,
             "Kb": 128, "Mb": 131072},
    "时间": {"秒": 1, "毫秒": 0.001, "微秒": 1e-6, "分钟": 60, "小时": 3600, "天": 86400},
    "体积": {"m³": 1, "cm³": 1e-6, "L": 0.001, "mL": 1e-6},
    "速度": {"m/s": 1, "km/h": 1/3.6, "mph": 0.44704, "节": 0.514444},
}


def convert_units(cat, value, frm, to):
    table = UNITS.get(cat)
    if not table:
        return None, "不认识的类别"
    if frm not in table or to not in table:
        return None, "不认识的单位"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None, "不是数字"
    return v * table[frm] / table[to], None


def ts_info(s):
    """时间戳 → 各种表示"""
    t = str(s or "").strip()
    if not t:
        return None, "空的"
    try:
        n = float(t)
    except ValueError:
        return None, "不是数字"
    unit = "秒"
    if abs(n) > 1e14:
        n, unit = n / 1e6, "微秒"
    elif abs(n) > 1e11:
        n, unit = n / 1e3, "毫秒"
    try:
        dt = datetime.fromtimestamp(n)
        utc = datetime.fromtimestamp(n, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        return None, f"超出可表示范围：{exc}"
    delta = dt - datetime.now()
    sec = delta.total_seconds()
    rel = ("还有 " if sec > 0 else "过去 ")
    sec = abs(sec)
    for name, k in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if sec >= k:
            rel += f"{int(sec // k)} {name}"
            break
    else:
        rel += f"{int(sec)} 秒"
    return {
        "按什么解释": unit,
        "本地时间": dt.strftime("%Y-%m-%d %H:%M:%S") + " " + dt.astimezone().strftime("%Z%z"),
        "UTC": utc.strftime("%Y-%m-%d %H:%M:%S") + " UTC",
        "ISO8601": dt.astimezone().isoformat(),
        "日期": dt.strftime("%Y-%m-%d"),
        "星期": "一二三四五六日"[dt.weekday()],
        "相对现在": rel,
        "秒": str(int(n)) if unit == "秒" else str(int(n * 1000 if unit == "毫秒" else n * 1e6)),
        "毫秒": str(int(n * 1000)),
    }, None


def now_info():
    now = time.time()
    dt = datetime.now()
    return {
        "现在(秒)": str(int(now)),
        "现在(毫秒)": str(int(now * 1000)),
        "现在(微秒)": str(int(now * 1e6)),
        "本地时间": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "今天 0 点": str(int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())),
        "本周一 0 点": str(int((dt.replace(hour=0, minute=0, second=0, microsecond=0)
                                - __import__("datetime").timedelta(days=dt.weekday())).timestamp())),
    }


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "devcalc/" + VERSION

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
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HOME, "index.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as exc:
                return self._send(500, f"<h1>{exc}</h1>", "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self._send(200, json.dumps({
                "version": VERSION,
                "units": {k: list(v.keys()) for k, v in UNITS.items()},
                "now": now_info(),
            }, ensure_ascii=False))
        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/number":
            v, err = parse_number(b.get("value"))
            if err:
                return self._send(200, json.dumps({"ok": False, "error": err},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps({
                "ok": True, "value": v, "bases": fmt_bases(v),
                "bits": describe_bits(v & 0xFFFFFFFF, 32) if -2**31 <= v < 2**32 else [],
            }, ensure_ascii=False))

        if u.path == "/api/bitops":
            a, ea = parse_number(b.get("a"))
            c, ec = parse_number(b.get("b"))
            if ea or ec:
                return self._send(200, json.dumps({"ok": False, "error": ea or ec},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps({
                "ok": True, "ops": bitops(a, c), "hamming": hamming(a, c),
                "a_bits": describe_bits(a & 0xFFFFFFFF, 32),
                "b_bits": describe_bits(c & 0xFFFFFFFF, 32),
            }, ensure_ascii=False))

        if u.path == "/api/color":
            c, err = parse_color(b.get("value"))
            if err:
                return self._send(200, json.dumps({"ok": False, "error": err},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps({"ok": True, "info": color_info(c),
                                               "rgb": c}, ensure_ascii=False))

        if u.path == "/api/unit":
            r, err = convert_units(b.get("cat"), b.get("value"), b.get("from"), b.get("to"))
            if err:
                return self._send(200, json.dumps({"ok": False, "error": err},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps({"ok": True, "result": r, "raw": repr(r)},
                                              ensure_ascii=False))

        if u.path == "/api/time":
            info, err = ts_info(b.get("value"))
            if err:
                return self._send(200, json.dumps({"ok": False, "error": err},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps({"ok": True, "info": info}, ensure_ascii=False))

        if u.path == "/api/calc":
            """简单表达式求值 —— 只允许数字和运算符，不用 eval 直接跑原文"""
            expr = (b.get("expr") or "").strip()
            if not expr:
                return self._send(400, json.dumps({"ok": False, "error": "空的"},
                                                  ensure_ascii=False))
            safe = re.fullmatch(r"[0-9a-fA-FxXoObB\s\.\+\-\*/%\(\)<>&|^~_]+", expr)
            if not safe:
                return self._send(200, json.dumps(
                    {"ok": False, "error": "只支持数字（含 0x/0b/0o 前缀）和 + - * / % ( ) < > & | ^ ~ 运算符"},
                    ensure_ascii=False))
            try:
                py = expr.replace("^", "**")     # ^ 当幂会歧义；这里按位异或更常用，故保留
                py = expr
                val = eval(py, {"__builtins__": {}}, {})   # 已用正则白名单限死字符集
                out = {"ok": True, "expr": expr}
                if isinstance(val, (int, float)):
                    out["value"] = val
                    out["bases"] = fmt_bases(int(val)) if isinstance(val, int) or float(val).is_integer() else {}
                    out["display"] = (f"{int(val)}" if isinstance(val, int) or
                                      float(val).is_integer() else f"{val:.10g}")
                else:
                    out["display"] = str(val)
                    out["value"] = val
                return self._send(200, json.dumps(out, ensure_ascii=False))
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
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
    log(f"程序员计算器 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ 程序员计算器已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_num(args):
    v, err = parse_number(args.value)
    if err:
        print("❌ " + err)
        return 1
    for k, val in fmt_bases(v).items():
        print(f"  {k:<18} {val}")
    return 0


def cmd_time(args):
    if args.value:
        info, err = ts_info(args.value)
        if err:
            print("❌ " + err)
            return 1
    else:
        info = now_info()
    for k, v in info.items():
        print(f"  {k:<14} {v}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="devcalc", description=f"程序员计算器 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("num"); sp.add_argument("value"); sp.set_defaults(f=cmd_num)
    sp = sub.add_parser("time"); sp.add_argument("value", nargs="?"); sp.set_defaults(f=cmd_time)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
