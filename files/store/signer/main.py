#!/usr/bin/env python3
"""签名工具 —— 算哈希、算 HMAC、**反推接口签名是怎么算的**。

为什么做这个（这是从你机器上看出来的需求）
  很多前端项目（uni-app / Vue）都用 crypto-js 做接口签名，
  说明服务端接口要求签名。抓包对齐接口时最头疼的就是这个：
  报文里有个 sign=xxx，但它是怎么算出来的？把参数排序拼起来再 MD5？
  还是 HMAC-SHA256？要不要带 key？要不要 URL 编码？大小写呢？

  「签名侦探」就是干这个的：你把抓到的参数和那个 sign 值贴进来，
  它把常见的拼法**全试一遍**，直接告诉你哪一种能对上。
  对上了 = 你复现出了服务端的签名算法。

纯标准库（hashlib/hmac/base64），零依赖。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("SIGNER_HOME") or SCRIPT_DIR)
PIDFILE = os.path.join(HOME, "signer.pid")
LOGFILE = os.path.join(HOME, "signer.log")
HISTORY = os.path.join(HOME, "found.jsonl")      # 只追加：找到的算法记下来
DEFAULT_PORT = 8914

ALGOS = ["md5", "sha1", "sha256", "sha512", "sha384", "sha224"]


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def digest(algo, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.new(algo, data).digest()


def fmt_all(raw):
    """一份原始摘要的多种表示"""
    return {
        "hex 小写": raw.hex(),
        "hex 大写": raw.hex().upper(),
        "base64": base64.b64encode(raw).decode(),
    }


def hash_text(algo, text, key=None, mode="plain"):
    if isinstance(text, str):
        text = text.encode("utf-8")
    if mode == "hmac" and key is not None:
        raw = hmac.new(key.encode("utf-8") if isinstance(key, str) else key,
                       text, algo).digest()
    else:
        raw = digest(algo, text)
    return {"raw": raw, "formats": fmt_all(raw), "hex": raw.hex()}


# ══════════════════════════════════════════════════════════════
# 签名侦探：把常见拼法全试一遍
# ══════════════════════════════════════════════════════════════

def parse_params(text):
    """把参数解析成 dict。支持 QueryString / JSON / 每行 k=v"""
    text = (text or "").strip()
    if not text:
        return {}, "参数是空的"
    if text.startswith("{"):
        try:
            d = json.loads(text)
            return {k: ("" if v is None else v) for k, v in d.items()}, None
        except Exception as exc:
            return {}, f"JSON 解析失败：{exc}"
    if "=" in text:
        out = {}
        raw = text.lstrip("?").lstrip("&")
        for pair in re.split(r"[&\n]", raw):
            if not pair.strip():
                continue
            if "=" in pair:
                k, v = pair.split("=", 1)
            else:
                k, v = pair, ""
            out[urllib.parse.unquote_plus(k.strip())] = urllib.parse.unquote_plus(v)
        return out, None
    return {}, "认不出参数格式（给 QueryString 或 JSON）"


def build_candidates(params, secret, sign_key="sign", exclude_sign=True):
    """生成各种「待签名原文」的候选拼法。

    每个候选是 (说明, 字符串)。这一步是侦探的核心 ——
    把国内接口最常见的拼法都覆盖到。
    """
    p = dict(params)
    if exclude_sign:
        for k in list(p.keys()):
            if k.lower() == sign_key.lower() or k.lower() in ("signature", "_sign", "sig"):
                p.pop(k, None)
    for k in ("timestamp", "ts", "nonce", "_t"):
        pass  # 这些要保留参与签名，不动

    keys_sorted = sorted(p.keys())
    keys_orig = list(p.keys())

    def kv(k, v):
        return f"{k}={v}"

    def kv_encoded(k, v):
        return f"{k}={urllib.parse.quote(str(v), safe='')}"

    cands = []
    joiners = [("&", "&"), ("", "")]
    for jname, j in joiners:
        for order_name, ks in (("字典序", keys_sorted), ("原顺序", keys_orig)):
            for enc_name, f in (("不编码", kv), ("URL编码", kv_encoded)):
                base = j.join(f(k, p[k]) for k in ks)
                tag = f"{order_name}·{enc_name}·连接符「{jname}」"
                cands.append((f"{tag}　（不加 secret）", base))
                if secret:
                    cands.append((f"{tag} + secret 拼后面", base + secret))
                    cands.append((f"{tag} + &key=secret", base + "&key=" + secret))
                    cands.append((f"{tag} + secret 拼前面", secret + base))
                    cands.append((f"{tag} + 双 secret", base + secret + secret))

    # 常见变体：整体小写/大写、key=value 带 key 名
    more = []
    if secret:
        # key 参与排序（把 secret 当成一个参数）
        p2 = dict(p)
        p2["key"] = secret
        ks2 = sorted(p2.keys())
        more.append(("secret 当作 key 参数一起排序",
                     "&".join(kv(k, p2[k]) for k in ks2)))
        p3 = dict(p)
        p3["secret"] = secret
        ks3 = sorted(p3.keys())
        more.append(("secret 当作 secret 参数一起排序",
                     "&".join(kv(k, p3[k]) for k in ks3)))
    # 值拼接
    more.append(("只拼值（字典序）", "".join(str(p[k]) for k in keys_sorted)))
    more.append(("只拼值（原顺序）", "".join(str(p[k]) for k in keys_orig)))
    # 原样
    more.append(("原样参数串（不重排）", "&".join(kv(k, p[k]) for k in keys_orig)))
    # JSON 体
    more.append(("紧凑 JSON", json.dumps(p, ensure_ascii=False, separators=(",", ":"))))
    more.append(("紧凑 JSON 字典序",
                 json.dumps({k: p[k] for k in sorted(p)}, ensure_ascii=False,
                            separators=(",", ":"))))
    if secret:
        more.append(("紧凑 JSON + secret",
                     json.dumps(p, ensure_ascii=False, separators=(",", ":")) + secret))
    cands.extend(more)
    return cands


def detective(params, secret, observed, sign_key="sign", limit=4000):
    """把常见拼法 × 常见算法全试一遍，看哪种能算出 observed"""
    observed = (observed or "").strip()
    if not observed:
        return {"ok": False, "error": "把抓到的签名值填上才能比对"}
    obs = observed.lower()
    obs_no_eq = obs.rstrip("=")
    cands = build_candidates(params, secret, sign_key)[:limit]

    hits = []
    tried = 0
    t0 = time.time()
    for desc, text in cands:
        if not text:
            continue
        for algo in ALGOS:
            tried += 1
            raw = digest(algo, text)
            hexl = raw.hex()
            variants = {
                "hex小写": hexl,
                "hex大写": hexl.upper(),
                "base64": base64.b64encode(raw).decode(),
                "base64去=": base64.b64encode(raw).decode().rstrip("="),
            }
            for vname, v in variants.items():
                if v.lower() == obs or v.lower().rstrip("=") == obs_no_eq:
                    hits.append({"how": desc, "algo": algo.upper(), "format": vname,
                                 "source": text if len(text) <= 500 else text[:500] + "…"})
                    break
            # HMAC 也试
            if secret:
                tried += 1
                hraw = hmac.new(secret.encode("utf-8"), text.encode("utf-8"), algo).digest()
                hvar = {"HMAC-hex小写": hraw.hex(), "HMAC-hex大写": hraw.hex().upper(),
                        "HMAC-base64": base64.b64encode(hraw).decode()}
                for vname, v in hvar.items():
                    if v.lower() == obs or v.lower().rstrip("=") == obs_no_eq:
                        hits.append({"how": desc + "（HMAC）", "algo": algo.upper(),
                                     "format": vname,
                                     "source": text if len(text) <= 500 else text[:500] + "…"})
                        break

    if hits:
        with open(HISTORY, "a", encoding="utf-8") as f:      # 只追加
            f.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "how": hits[0]["how"], "algo": hits[0]["algo"],
                                "format": hits[0]["format"],
                                "params": list(params.keys())}, ensure_ascii=False) + "\n")
        log(f"[detective] 命中！{hits[0]['algo']} / {hits[0]['how'][:40]}")
    else:
        log(f"[detective] 试了 {tried} 种组合，没对上")

    return {"ok": True, "hits": hits[:20], "tried": tried,
            "candidates": len(cands),
            "seconds": round(time.time() - t0, 2)}


def found_history(limit=15):
    rows = []
    try:
        with open(HISTORY, encoding="utf-8") as f:
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
    server_version = "signer/" + VERSION

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
                "version": VERSION, "algos": ALGOS, "found": found_history(),
            }, ensure_ascii=False))
        if u.path == "/api/digest":
            return self._send(200, json.dumps({"items": []}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/hash":
            text = b.get("text") or ""
            key = b.get("key")
            mode = b.get("mode") or "plain"
            out = {}
            for algo in ALGOS:
                out[algo.upper()] = hash_text(algo, text, key, mode)["formats"]
            return self._send(200, json.dumps(
                {"ok": True, "results": out, "mode": mode,
                 "bytes": len(text.encode("utf-8"))}, ensure_ascii=False))

        if u.path == "/api/detect":
            params, err = parse_params(b.get("params") or "")
            if err:
                return self._send(200, json.dumps({"ok": False, "error": err},
                                                  ensure_ascii=False))
            r = detective(params, b.get("secret") or "", b.get("observed") or "",
                          b.get("sign_key") or "sign")
            r["param_count"] = len(params)
            return self._send(200, json.dumps(r, ensure_ascii=False))

        if u.path == "/api/candidates":
            """只看拼法候选，不比对 —— 让用户理解都有哪些可能"""
            params, err = parse_params(b.get("params") or "")
            if err:
                return self._send(200, json.dumps({"ok": False, "error": err},
                                                  ensure_ascii=False))
            cands = build_candidates(params, b.get("secret") or "",
                                     b.get("sign_key") or "sign")
            return self._send(200, json.dumps({
                "ok": True,
                "items": [{"how": d, "text": t[:300]} for d, t in cands[:60]],
                "total": len(cands),
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
    log(f"签名工具 v{VERSION} 已启动 http://127.0.0.1:{port}")

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
    print(f"✅ 签名工具已启动（PID {running_pid()}） http://127.0.0.1:{port}"
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


def cmd_detect(args):
    params, err = parse_params(args.params)
    if err:
        print("❌ " + err)
        return 1
    r = detective(params, args.secret or "", args.observed, args.sign_key or "sign")
    print(f"参数 {len(params)} 个，试了 {r['tried']} 种组合（{r['seconds']}s）")
    if not r["hits"]:
        print("  没对上。可能：secret 不对、拼法不在常见范围内、或者签名还带了别的固定串。")
        print("  可以看看有哪些候选拼法：--show-candidates")
        return 1
    for h in r["hits"]:
        print(f"  ✅ {h['algo']} / {h['format']}")
        print(f"     {h['how']}")
        print(f"     原文: {h['source'][:160]}")
    return 0


def cmd_hash(args):
    r = hash_text(args.algo, args.text, args.key,
                  "hmac" if args.key else "plain")
    for k, v in r["formats"].items():
        print(f"  {k:<12} {v}")
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="signer", description=f"签名工具 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("hash"); sp.add_argument("text")
    sp.add_argument("--algo", default="sha256"); sp.add_argument("--key"); sp.set_defaults(f=cmd_hash)
    sp = sub.add_parser("detect"); sp.add_argument("params"); sp.add_argument("observed")
    sp.add_argument("--secret"); sp.add_argument("--sign-key"); sp.set_defaults(f=cmd_detect)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
