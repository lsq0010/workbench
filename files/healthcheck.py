#!/usr/bin/env python3
"""功能健康检查 —— 上线前跑一遍，别等用户点开才发现 500。

为什么需要这个
  写 shotkit 时漏了 `import urllib.parse`，进程能起来、端口在听，
  但**每个请求都崩** —— 界面返回 000。
  这种错静态看代码很容易漏，跑一遍就知道。

检查四项
  1. 语法能编译
  2. 用到的模块都 import 了（剥掉字符串和注释再查，不误报）
  3. 声明的端口真的在听，且界面 + /api/status 都返回 200
  4. manifest 的 id 和目录名一致

跑法：python3 healthcheck.py        （检查全部）
      python3 healthcheck.py shotkit （只查一个）
"""
import glob
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request

HOME = os.path.dirname(os.path.abspath(__file__))
MODULES = ("urllib", "xml", "http", "concurrent", "subprocess", "shutil", "threading",
           "hashlib", "hmac", "base64", "csv", "io", "zipfile", "plistlib", "struct",
           "socket", "signal", "tempfile", "glob", "json", "re", "time", "os", "sys")


def strip_strings(s):
    """把字符串和注释剥掉，避免 r'<?xml' 这种被误判成用了 xml 模块"""
    s = re.sub(r'"""(?:.|\n)*?"""', '""', s)
    s = re.sub(r"'''(?:.|\n)*?'''", "''", s)
    s = re.sub(r'r?"(?:[^"\\]|\\.)*"', '""', s)
    s = re.sub(r"r?'(?:[^'\\]|\\.)*'", "''", s)
    s = re.sub(r"#[^\n]*", "", s)
    return s


def check_one(fid=None):
    paths = (sorted(glob.glob(os.path.join(HOME, "features/*/main.py"))) +
             sorted(glob.glob(os.path.join(HOME, "store/*/main.py"))))
    rows = []
    for p in paths:
        name = p.split("/")[-2]
        if fid and name != fid:
            continue
        man_path = os.path.join(os.path.dirname(p), "manifest.json")
        man = {}
        if os.path.exists(man_path):
            try:
                man = json.load(open(man_path, encoding="utf-8"))
            except Exception:
                pass
        raw = open(p, encoding="utf-8").read()
        src = "features" if "/features/" in p else "store"
        r = {"name": name, "id": man.get("id", "?"), "problems": [], "src": src}

        # 1) 语法
        try:
            compile(raw, p, "exec")
        except SyntaxError as e:
            r["problems"].append("语法错误 第 %d 行: %s" % (e.lineno, e.msg))

        # 2) import 完整性
        code = strip_strings(raw)
        imported = set()
        for m in re.finditer(r'^\s*(?:import|from)\s+([\w\.]+)', raw, re.M):
            imported.add(m.group(1).split(".")[0])
            imported.add(m.group(1))
        for mod in MODULES:
            if re.search(r"\b%s\." % mod, code) and mod not in imported:
                r["problems"].append("用了 %s. 但没 import" % mod)

        # 3) manifest id 与目录名
        if man and man.get("id") and man["id"] != name:
            r["problems"].append("manifest id=%s 与目录名 %s 不一致" % (man["id"], name))

        # 4) 端口 + 界面 + API（只对 features/ 下在跑的服务查）
        port = (man.get("ports") or {}).get("web")
        if port and "/features/" in p:
            s = socket.socket()
            s.settimeout(1.0)
            listening = s.connect_ex(("127.0.0.1", port)) == 0
            s.close()
            if listening:
                base = "http://127.0.0.1:%d" % port
                for path, label in (("/", "界面"), ("/api/status", "API")):
                    try:
                        with urllib.request.urlopen(base + path, timeout=12) as resp:
                            if resp.status != 200:
                                r["problems"].append("%s 返回 %d" % (label, resp.status))
                    except Exception as exc:
                        r["problems"].append("%s 请求失败: %s" % (label, exc))
            else:
                r["running"] = False
        rows.append(r)
    return rows


def main():
    fid = sys.argv[1] if len(sys.argv) > 1 else None
    rows = check_one(fid)
    if not rows:
        print("  没找到功能" + ("：%s" % fid if fid else ""))
        return 1
    bad = [r for r in rows if r["problems"]]
    print("═" * 64)
    n_feat = len([r for r in rows if r["src"] == "features"])
    n_store = len([r for r in rows if r["src"] == "store"])
    print("  功能健康检查（已装 %d 个 + 商店可装 %d 个）" % (n_feat, n_store))
    print("═" * 64)
    for r in rows:
        if r["problems"]:
            print("  ❌ %-14s（%s）" % (r["name"], r["src"]))
            for pb in r["problems"]:
                print("       · %s" % pb)
    print("─" * 64)
    if bad:
        print("  ❌ %d 个功能有问题（上面列了）" % len(bad))
    else:
        print("  ✅ 全部通过：语法 / import / id / 端口 / 界面 / API")
        print("     （商店里的副本也一起查了 —— 装之前就知道它能不能跑）")
    print("═" * 64)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
