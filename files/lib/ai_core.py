#!/usr/bin/env python3
"""AI 根入口 —— 平台的自救能力，**这一层刻意做成不可随意改动的**。

为什么要有这一层
  用户的原话：「ai能力的代码不能改，作为根本入口，不满意你可以自定义ai，
  但是基本的ai要保留，不能改，因为万一改错了，跑不起来了，
  基本入口可以保证还能改」

  所以这里的定位是**兜底**：
    · 它活在平台进程里，不是某个可删除的功能 —— 功能全坏了它还在
    · 改平台代码前先备份，改完做语法检查，不过就自动回滚
    · 每次改动都追加审计记录，谁改了什么、什么时间、成没成
    · 它自己的完整性由 SHA256 清单守护，被改了能看出来、能还原

  用户可以换模型、换 key（在 ai.json 里），但**这层代码本身**是根。

设计原则
  · 只依赖标准库 + platform_lib（不依赖任何功能）
  · 所有写操作都可回滚
  · 不静默失败：出错要说清楚哪一步、为什么
"""
import base64
import hashlib
import shlex
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

LIB_DIR = os.path.dirname(os.path.abspath(__file__))
PLATFORM_HOME = os.path.dirname(LIB_DIR)
BACKUP_DIR = os.path.join(PLATFORM_HOME, "backups")
AUDIT = os.path.join(PLATFORM_HOME, "ai_audit.jsonl")     # 只追加
CORE_FILE = os.path.abspath(__file__)
CORE_HASH_FILE = os.path.join(LIB_DIR, "ai_core.sha256")
PRISTINE_DIR = os.path.join(PLATFORM_HOME, ".core-pristine")

# 这些文件属于「根」，AI 不能改（改了就可能再也起不来）
PROTECTED = {
    "lib/ai_core.py", "lib/ai_core.sha256", "lib/platform_lib.py",
    "lib/ai_watch.py",          # 值守也属于"AI 能力"这一层，同样不能改
    "platform.py", "identity.json",
}
# 平台自己的代码目录（允许改，但必须备份+校验）
EDITABLE_ROOTS = ("web/", "features/", "store/", "lib/", "")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"{_now()} [ai_core] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(PLATFORM_HOME, "platform.log"), "a",
                  encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def audit(action, detail, ok=True):
    """所有对平台的改动都记一笔（只追加，改不掉历史）"""
    try:
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _now(), "action": action, "ok": ok,
                                "detail": detail}, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# 完整性：守护这一层自己
# ══════════════════════════════════════════════════════════════

def file_sha(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def core_hash():
    return file_sha(CORE_FILE)


def ensure_pristine():
    """第一次跑的时候，把干净副本存起来 —— 以后被改坏了从这里还原"""
    os.makedirs(PRISTINE_DIR, exist_ok=True)
    for rel in ("lib/ai_core.py", "lib/platform_lib.py", "platform.py"):
        src = os.path.join(PLATFORM_HOME, rel)
        dst = os.path.join(PRISTINE_DIR, rel)
        if not os.path.isfile(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.isfile(dst):
            shutil.copy2(src, dst)
            log(f"存下干净副本 {rel}")
        elif file_sha(src) != file_sha(dst):
            # 当前文件被改过了：保留最原始的副本不动
            pass


def integrity():
    """检查根这一层有没有被动过"""
    ensure_pristine()
    h = core_hash()
    recorded = None
    if os.path.isfile(CORE_HASH_FILE):
        try:
            recorded = open(CORE_HASH_FILE, encoding="utf-8").read().strip().split()[0]
        except Exception:
            pass
    pristine = os.path.join(PRISTINE_DIR, "lib/ai_core.py")
    pristine_h = file_sha(pristine) if os.path.isfile(pristine) else None

    rows = []
    for rel in sorted(PROTECTED):
        if rel == "lib/ai_core.sha256":
            continue
        p = os.path.join(PLATFORM_HOME, rel)
        pr = os.path.join(PRISTINE_DIR, rel)
        rows.append({
            "file": rel,
            "exists": os.path.isfile(p),
            "hash": (file_sha(p) or "")[:16],
            "matches_pristine": (file_sha(p) == file_sha(pr)) if os.path.isfile(pr) else None,
        })
    ok = all(r["exists"] for r in rows) and (recorded is None or recorded == h)
    if ok:
        note = "根入口没被动过"
    elif not all(r["exists"] for r in rows):
        note = "有根文件不见了 —— 点「还原根入口」能恢复"
    else:
        note = ("根入口被改过（哈希和登记的不一样）。"
                "如果是你自己改的，重新登记一下就行；不确定就点「还原根入口」。")
    return {"ok": ok, "note": note,
            "core_hash": (h or "")[:16], "recorded": (recorded or "")[:16],
            "pristine_hash": (pristine_h or "")[:16], "files": rows,
            "has_pristine": os.path.isdir(PRISTINE_DIR)}


def accept_current_core():
    """把当前版本当作基准重新登记 —— 用户自己有意改过之后用这个。

    不这样做的话，改一次就一直报"完整性不对"，久了就没人看了。
    """
    ensure_pristine()
    h = core_hash()
    if not h:
        return {"ok": False, "error": "读不到 ai_core.py"}
    try:
        with open(CORE_HASH_FILE, "w", encoding="utf-8") as f:
            f.write("%s  lib/ai_core.py\n" % h)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    # 干净副本也更新成当前版本
    import shutil as _sh
    for rel in ("lib/ai_core.py", "lib/platform_lib.py", "lib/ai_watch.py",
                "platform.py"):
        src = os.path.join(PLATFORM_HOME, rel)
        dst = os.path.join(PRISTINE_DIR, rel)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _sh.copy2(src, dst)
    audit("accept_current_core", {"hash": h[:16]})
    return {"ok": True, "hash": h[:16], "note": "已把当前版本登记为基准"}


def restore_core(which=None):
    """从干净副本还原根文件。只在用户明确要求时调用。"""
    restored = []
    for rel in (["lib/ai_core.py", "lib/platform_lib.py"] if not which else [which]):
        src = os.path.join(PRISTINE_DIR, rel)
        dst = os.path.join(PLATFORM_HOME, rel)
        if os.path.isfile(src):
            shutil.copy2(dst, dst + ".before-restore") if os.path.isfile(dst) else None
            shutil.copy2(src, dst)
            restored.append(rel)
    audit("restore_core", {"restored": restored})
    return {"ok": bool(restored), "restored": restored,
            "note": "改动前的版本存成了 .before-restore"}


# ══════════════════════════════════════════════════════════════
# 读写平台代码（AI 改平台的能力，带备份和回滚）
# ══════════════════════════════════════════════════════════════

def safe_path(rel):
    """把路径解析到平台目录内，挡住 ../ 越界。

    相对路径按平台目录解析；**平台目录内的绝对路径也直接接受** ——
    早先 AI 传了绝对路径被拒，白费一轮，没必要卡这个。
    真要越界时，错误信息里把正确写法告诉它。
    """
    raw = (rel or "").strip()
    root = os.path.abspath(PLATFORM_HOME)
    if os.path.isabs(raw):
        abs_p = os.path.abspath(raw)
        if abs_p == root or abs_p.startswith(root + os.sep):
            return abs_p, None
        return None, ("只能操作平台目录（%s）内的文件。你给的是 %s。"
                      "平台内的文件可以直接写绝对路径，也可以用相对路径，"
                      "例如 demo/ios-demo/DemoApp/ViewController.swift"
                      % (PLATFORM_HOME, raw))
    p = os.path.abspath(os.path.join(root, raw.lstrip("/")))
    if not (p == root or p.startswith(root + os.sep)):
        return None, ("路径越界了。请用相对平台目录的路径，"
                      "例如 web/desktop.html 或 features/xxx/main.py")
    return p, None


def read_file(rel, max_bytes=200000, offset=None, limit=None, around=None):
    """读文件。

    支持三种读法 —— 因为**大文件一次读不完**（踩过这个坑：
    AI 想看一个 5 万字节文件里的某段 CSS，read_file 每次只给开头 12000 字，
    它找不到目标只能放弃）：
      · 默认：从头读 max_bytes
      · offset+limit：按行区间读（第 offset 行起，读 limit 行）
      · around=关键词：找包含这个词的地方，读它前后 ±30 行 —— 最常用
    """
    p, err = safe_path(rel)
    if err:
        return {"ok": False, "error": err}
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % rel}
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)

        if around:
            hits = [i for i, l in enumerate(lines) if around in l]
            if not hits:
                return {"ok": True, "path": rel, "text": "",
                        "note": "文件里没有 %r 这个内容（共 %d 行）" % (around, total),
                        "lines": total, "bytes": os.path.getsize(p)}
            lo = max(0, hits[0] - 30)
            hi = min(total, hits[-1] + 31)
            body = "".join("%6d| %s" % (i + 1, lines[i]) for i in range(lo, hi))
            return {"ok": True, "path": rel, "text": body, "lines": total,
                    "bytes": os.path.getsize(p),
                    "matched_lines": [h + 1 for h in hits[:20]],
                    "range": [lo + 1, hi], "truncated": False,
                    "note": "%r 出现在第 %s 行，下面是上下文"
                            % (around, "、".join(str(h + 1) for h in hits[:10]))}

        if offset is not None or limit is not None:
            lo = max(0, int(offset or 1) - 1)
            n = int(limit or 200)
            hi = min(total, lo + n)
            body = "".join("%6d| %s" % (i + 1, lines[i]) for i in range(lo, hi))
            return {"ok": True, "path": rel, "text": body, "lines": total,
                    "bytes": os.path.getsize(p), "range": [lo + 1, hi],
                    "truncated": hi < total,
                    "note": "第 %d~%d 行，共 %d 行" % (lo + 1, hi, total)}

        txt = "".join(lines)
        cut = txt[:max_bytes]
        return {"ok": True, "path": rel, "text": cut,
                "lines": total, "bytes": os.path.getsize(p),
                "truncated": len(txt) > max_bytes,
                "note": ("文件共 %d 行 / %d 字节，上面是开头。要看别的部分："
                         'read_file(rel, around="关键词") 或 read_file(rel, offset=行号, limit=行数)'
                         % (total, os.path.getsize(p))) if len(txt) > max_bytes else ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def search(sub="", pattern="", limit=60):
    """在平台里搜内容 —— 比一个个读文件高效得多。

    sub 限定目录，pattern 是要找的文本（普通子串，不是正则）。
    """
    root, err = safe_path(sub)
    if err:
        return {"ok": False, "error": err}
    if not pattern:
        return {"ok": False, "error": "没给要搜的内容"}
    out = []
    exts = (".py", ".html", ".js", ".json", ".md", ".css", ".swift", ".sh", ".yml",
            ".yaml", ".txt")
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".")
                   and d not in ("__pycache__", "backups", "node_modules",
                                 "DerivedData", ".core-pristine")]
        for f in sorted(files):
            if not f.endswith(exts):
                continue
            p = os.path.join(cur, f)
            try:
                if os.path.getsize(p) > 3 * 1024 * 1024:
                    continue
                with open(p, encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if pattern in line:
                            rel = os.path.relpath(p, PLATFORM_HOME)
                            out.append("%s:%d: %s" % (rel, i, line.strip()[:160]))
                            if len(out) >= limit:
                                return {"ok": True,
                                        "result": "\n".join(out), "truncated": True,
                                        "count": len(out)}
            except (OSError, UnicodeError):
                continue
    if not out:
        return {"ok": True, "result": "没找到 %r" % pattern, "count": 0}
    return {"ok": True, "result": "\n".join(out), "count": len(out),
            "truncated": False}


def list_files(sub="", depth=2, limit=300):
    """列平台目录里的文件，给 AI 当上下文"""
    root, err = safe_path(sub)
    if err:
        return {"ok": False, "error": err}
    out = []
    base_depth = root.rstrip("/").count("/")
    for cur, dirs, files in os.walk(root):
        if cur.count("/") - base_depth >= depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and d not in
                   ("__pycache__", "backups", "node_modules")]
        for f in sorted(files):
            if f.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(cur, f), PLATFORM_HOME)
            try:
                out.append({"path": rel, "bytes": os.path.getsize(os.path.join(cur, f))})
            except OSError:
                pass
            if len(out) >= limit:
                return {"ok": True, "files": out, "truncated": True}
    return {"ok": True, "files": out, "truncated": False}


def is_protected(rel):
    rel = (rel or "").strip().lstrip("/")
    return rel in PROTECTED


def apply_edit(rel, new_text, reason="", expect_old=None):
    """改一个平台文件。流程：备份 → 写 → 语法检查 → 不过就回滚。

    expect_old 给了的话，会先确认文件当前内容包含它 ——
    避免 AI 基于过时的内容去改（那是最容易把代码改坏的情况）。
    """
    p, err = safe_path(rel)
    if err:
        return {"ok": False, "error": err}
    if is_protected(rel):
        return {"ok": False, "error": "这个文件属于根入口，不能改：%s" % rel}
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % rel}

    old = open(p, encoding="utf-8", errors="replace").read()
    if expect_old and expect_old not in old:
        return {"ok": False,
                "error": "文件当前内容里找不到 expect_old（可能已经被改过了），"
                         "为避免改错，这次不动手"}

    # 备份
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = rel.replace("/", "__")
    bak = os.path.join(BACKUP_DIR, f"{stamp}__{safe_name}")
    shutil.copy2(p, bak)

    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(new_text)
    except Exception as exc:
        shutil.copy2(bak, p)
        return {"ok": False, "error": "写入失败已回滚：%s" % exc}

    # 语法检查（只对 Python）
    if p.endswith(".py"):
        try:
            py_compile.compile(p, doraise=True, cfile=p + ".pycheck")
            try:
                os.remove(p + ".pycheck")
            except OSError:
                pass
        except py_compile.PyCompileError as exc:
            shutil.copy2(bak, p)
            audit("apply_edit", {"file": rel, "reason": reason, "rolled_back": True,
                                 "error": str(exc)[:200]}, ok=False)
            return {"ok": False, "error": "语法没过，已回滚：%s" % str(exc)[:300],
                    "rolled_back": True}

    # JSON 也校验一下
    if p.endswith(".json"):
        try:
            json.loads(new_text)
        except Exception as exc:
            shutil.copy2(bak, p)
            audit("apply_edit", {"file": rel, "reason": reason, "rolled_back": True,
                                 "error": str(exc)[:200]}, ok=False)
            return {"ok": False, "error": "JSON 不合法，已回滚：%s" % exc,
                    "rolled_back": True}

    audit("apply_edit", {"file": rel, "reason": reason, "backup": os.path.basename(bak),
                         "bytes_before": len(old), "bytes_after": len(new_text)})
    log(f"改了 {rel}（{reason[:60]}），备份 {os.path.basename(bak)}")
    return {"ok": True, "file": rel, "backup": os.path.basename(bak),
            "bytes_before": len(old), "bytes_after": len(new_text)}


def edit_snippet(rel, find, replace, reason=""):
    """按片段替换 —— 比整文件覆盖安全，且强制要求 find 唯一"""
    p, err = safe_path(rel)
    if err:
        return {"ok": False, "error": err}
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % rel}
    old = open(p, encoding="utf-8", errors="replace").read()
    n = old.count(find)
    if n == 0:
        return {"ok": False, "error": "要替换的片段在这文件里找不到"}
    if n > 1:
        return {"ok": False, "error": "要替换的片段出现 %d 次，不唯一 —— "
                                      "给长一点、带上前后文" % n}
    return apply_edit(rel, old.replace(find, replace, 1), reason=reason,
                      expect_old=find)


# ══════════════════════════════════════════════════════════════
# 跑命令（白名单）
# ══════════════════════════════════════════════════════════════

# 允许跑的命令前缀。写窄一点 —— 只覆盖"看情况"和"验证"需要的，
# 不放 rm / mv / chmod / sudo / 往外的 curl。
ALLOWED_CMDS = {
    "xcodebuild":  ["xcodebuild"],          # 编译/测试/看工程信息
    "xcrun":       ["xcrun"],               # simctl 等
    "swift":       ["swift", "swiftc"],
    "python3":     ["python3"],
    "git":         ["git", "status", "log", "diff", "branch", "show", "rev-parse",
                    "ls-files", "stash"],    # 只读子命令
    "ls":          ["ls"],
    "cat":         ["cat"],
    "grep":        ["grep"],
    "find":        ["find"],
    "wc":          ["wc"],
    "head":        ["head"],
    "tail":        ["tail"],
    "file":        ["file"],
    "plutil":      ["plutil"],
    "lsof":        ["lsof"],
    "networksetup": ["networksetup"],       # 只读查询（-getwebproxy 之类）
    "curl":        ["curl"],                # 只允许本机（下面单独检查）
}

# 只在 macOS 上存在的命令 —— 别的系统上放行也没用（会报"找不到命令"），
# 不如直接摘掉，让 AI 一眼看出"这台机器上没这个工具"
import sys as _sys
if _sys.platform != "darwin":
    # portability-ok: 下面是"要摘掉的命令名"，不是调用
    for _c in ("plutil", "lsof", "networksetup", "sips", "open", "pbcopy",
               "mdfind", "sw_vers", "osascript", "codesign", "security"):
        ALLOWED_CMDS.pop(_c, None)
    # Windows / Linux 上补几个等价的只读查询命令
    if _sys.platform == "win32":
        ALLOWED_CMDS.update({
            "tasklist": ["tasklist"],       # 等价于 ps
            "netstat":  ["netstat"],        # 等价于 lsof
            "where":    ["where"],          # 等价于 which
        })
    else:
        ALLOWED_CMDS.update({
            "ss":    ["ss"],                # 等价于 lsof
            "uname": ["uname"],
        })

# git 只允许这些子命令（别把 reset --hard 交出去）
GIT_READONLY = {"status", "log", "diff", "branch", "show", "rev-parse", "ls-files",
                "stash", "remote", "config", "describe", "tag", "shortlog",
                "for-each-ref", "rev-list", "blame"}

MAX_OUTPUT = 8000
DEFAULT_TIMEOUT = 300


def _dangerous(cmd, args):
    """再拦一道明显的破坏性操作"""
    joined = " ".join(args)
    for bad in ("rm ", "rm -", "mv ", "chmod", "chown", "sudo", ">", ">>", "|",
                "&&", ";", "`", "$(", "kill", "shutdown", "reboot"):
        if bad in joined:
            return "命令里出现了不允许的内容：%r" % bad
    if cmd == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub and sub not in GIT_READONLY:
            return "git %s 不在只读白名单里（只允许 %s）" % (
                sub, "、".join(sorted(GIT_READONLY)))
    if cmd == "curl":
        urls = [a for a in args if a.startswith(("http://", "https://"))]
        for u in urls:
            if not (u.startswith("http://127.0.0.1") or u.startswith("http://localhost")):
                return "curl 只允许访问本机（不给外网，免得被拿去做别的事）"
    return None


def run_command(line, timeout=DEFAULT_TIMEOUT, cwd=None):
    """跑一条白名单里的命令。

    用 shlex 拆参数然后直接 exec —— **不经 shell**，
    所以 `;` `|` `>` 这些都没有特殊含义，注入不进来。
    """
    line = (line or "").strip()
    if not line:
        return {"ok": False, "error": "命令是空的"}
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        return {"ok": False, "error": "命令解析失败：%s" % exc}
    if not parts:
        return {"ok": False, "error": "命令是空的"}

    cmd = os.path.basename(parts[0])
    if cmd not in ALLOWED_CMDS:
        return {"ok": False,
                "error": "不允许执行 %r。可用的：%s" % (
                    cmd, "、".join(sorted(ALLOWED_CMDS)))}
    bad = _dangerous(cmd, parts[1:])
    if bad:
        return {"ok": False, "error": bad}

    # 工作目录限制在平台目录内
    workdir = PLATFORM_HOME
    if cwd:
        w, err = safe_path(cwd)
        if err:
            return {"ok": False, "error": err}
        if os.path.isdir(w):
            workdir = w

    t0 = time.time()
    try:
        p = subprocess.run(parts, capture_output=True, text=True,
                           timeout=timeout, cwd=workdir, errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        ok = p.returncode == 0
    except subprocess.TimeoutExpired:
        audit("run_command", {"cmd": line, "timeout": timeout}, ok=False)
        return {"ok": False, "error": "超时（%ds）—— 命令太大或卡住了" % timeout}
    except Exception as exc:
        audit("run_command", {"cmd": line, "error": str(exc)}, ok=False)
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    secs = round(time.time() - t0, 1)
    audit("run_command", {"cmd": line[:200], "code": p.returncode, "seconds": secs}, ok=ok)
    log("跑了命令：%s（%s，%.1fs）" % (line[:80], "成功" if ok else "返回 %d" % p.returncode,
                                    secs))
    return {"ok": ok, "code": p.returncode, "seconds": secs,
            "output": out[-MAX_OUTPUT:] if len(out) > MAX_OUTPUT else out,
            "truncated": len(out) > MAX_OUTPUT,
            "cwd": workdir}


def list_backups(limit=30):
    out = []
    if os.path.isdir(BACKUP_DIR):
        for name in sorted(os.listdir(BACKUP_DIR), reverse=True)[:limit]:
            p = os.path.join(BACKUP_DIR, name)
            try:
                out.append({"name": name, "bytes": os.path.getsize(p),
                            "ts": datetime.fromtimestamp(
                                os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")})
            except OSError:
                pass
    return out


def rollback(backup_name):
    """从某个备份还原"""
    p = os.path.join(BACKUP_DIR, os.path.basename(backup_name))
    if not os.path.isfile(p):
        return {"ok": False, "error": "没有这个备份"}
    rel = backup_name.split("__", 1)[-1].replace("__", "/")
    dst, err = safe_path(rel)
    if err:
        return {"ok": False, "error": err}
    shutil.copy2(p, dst)
    audit("rollback", {"file": rel, "backup": backup_name})
    return {"ok": True, "file": rel, "from": backup_name}


def audit_log(limit=50):
    rows = []
    try:
        with open(AUDIT, encoding="utf-8") as f:
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
# AI 调用（走 platform_lib，保持"能力"和"通道"分离）
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# Agent 模式：自己循环到做完
# ══════════════════════════════════════════════════════════════

AGENT_PROMPT = """你在自主完成一个任务，会多轮进行。

每一轮你只能做一件事，然后等工具结果：

- 想看清现状 → 输出一行（rel 用相对平台目录的路径）
    TOOL: {"name":"read_file","args":{"rel":"demo/ios-demo/DemoApp/ViewController.swift"}}
- 想看有哪些文件 → 输出一行
    TOOL: {"name":"list_files","args":{"sub":"features/xxx"}}
- 想在大文件里找某段内容（**推荐，比整文件读高效**）→ 输出一行
    TOOL: {"name":"read_file","args":{"rel":"路径","around":"要找的关键词"}}
- 想按行区间读 → 输出一行
    TOOL: {"name":"read_file","args":{"rel":"路径","offset":100,"limit":80}}
- 想在整个平台里搜 → 输出一行
    TOOL: {"name":"search","args":{"pattern":"要找的文本","sub":"features"}}
- 想看清楚一张截图/图片（App 界面、报错弹窗）→ 输出一行
    TOOL: {"name":"look_at_image","args":{"path":"/绝对/路径.png","q":"看什么"}}
- 想跑个命令看情况（编译、跑测试、看 git 状态）→ 输出一行
    TOOL: {"name":"run_command","args":{"cmd":"xcodebuild -list -project 路径"}}
    （只有白名单里的命令能跑；不能删除/移动文件、不能联网）
- 想改一处 → 输出一行
    TOOL: {"name":"edit_snippet","args":{"rel":"路径","find":"原文","replace":"新文","reason":"为什么改"}}
- 想验证语法对不对 → 输出一行
    TOOL: {"name":"check_syntax","args":{"rel":"路径"}}
- **任务做完了** → 输出一行
    TOOL: {"name":"done","args":{"summary":"做了什么、改在哪个文件、怎么回滚"}}
- **做不下去了** → 输出一行
    TOOL: {"name":"give_up","args":{"reason":"卡在哪、缺什么信息"}}

规矩：
1. 一轮只输出**一个** TOOL 行；可以在这行前后写一句你在想什么
2. 改代码前必须先 read_file 看清原文，find 要能唯一匹配
3. 改完用 check_syntax 验一下（Python 有效，其他类型会说跳过）
4. 根入口文件（platform.py / lib/ai_core.py / lib/platform_lib.py /
   lib/ai_watch.py / identity.json）改不了，别去试
5. 不要为了"有进展"而乱改。任务本来就完成了就 done
6. summary 要说清楚：改了哪个文件、改了什么、怎么回滚
7. **别浪费轮数**：能一次读完的就别分两次；改完验证过了就 done，
   不要为了"再确认一下"多花一轮
"""


# ══════════════════════════════════════════════════════════════
# 看图
# ══════════════════════════════════════════════════════════════

VISION_PROMPT = """你在看用户电脑或手机上的截图，帮他看出问题。

怎么说：
- 先说你看到的是什么（哪个界面、在做什么）
- 再说有没有问题：报错、布局错乱、数据不对、文案错、状态异常
- 有问题的指出**具体在哪**（哪个位置、什么文字），并说怎么改
- **看不懂或图上没有的信息就直说"图上看不出来"**，不要猜
- 分点，简洁，中文
"""


def describe_image(paths, question="", system=None):
    """看图。paths 可以是平台目录内的相对路径，也可以是绝对路径。

    产出事件 dict（和 agent_stream 一样是流式的）：
      {"type":"text","text":...}
      {"type":"done"}
      {"type":"error","error":...}
    """
    try:
        lib = _lib()
    except Exception as exc:
        yield {"type": "error", "error": "AI 库加载失败：" + str(exc)}
        return

    # 路径校验：平台外的绝对路径也允许（截图可能在桌面），但必须真实存在
    real = []
    for p in (paths or []):
        if not p:
            continue
        cand = p if os.path.isabs(p) else os.path.join(PLATFORM_HOME, p)
        cand = os.path.abspath(cand)
        if not os.path.isfile(cand):
            yield {"type": "error", "error": "找不到图片：" + p}
            return
        low = cand.lower()
        if not low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            yield {"type": "error", "error": "这个不是图片格式：" + os.path.basename(cand)}
            return
        real.append(cand)
    if not real:
        yield {"type": "error", "error": "没给图片"}
        return

    try:
        for piece in lib.stream_vision(question or "看看这张图有什么问题。",
                                       real, system=system or VISION_PROMPT):
            yield {"type": "text", "text": piece}
    except Exception as exc:
        yield {"type": "error", "error": "%s: %s" % (type(exc).__name__, exc)}
        return
    audit("vision", {"images": [os.path.basename(x) for x in real],
                     "q": (question or "")[:120]})
    yield {"type": "done", "images": len(real)}


def run_tool_ext(call):
    """比 run_tool 多两个：check_syntax 和结束信号"""
    name = (call or {}).get("name")
    args = (call or {}).get("args") or {}
    if name == "check_syntax":
        rel = args.get("rel") or ""
        p, err = safe_path(rel)
        if err:
            return False, err
        if not os.path.isfile(p):
            return False, "文件不存在：" + rel
        if p.endswith(".py"):
            try:
                py_compile.compile(p, doraise=True, cfile=p + ".chk")
                try:
                    os.remove(p + ".chk")
                except OSError:
                    pass
                return True, "语法没问题"
            except py_compile.PyCompileError as exc:
                return False, "语法错误：" + str(exc)[:300]
        if p.endswith(".json"):
            try:
                json.loads(open(p, encoding="utf-8").read())
                return True, "JSON 合法"
            except Exception as exc:
                return False, "JSON 不合法：" + str(exc)
        return True, "（这个类型没有语法检查，跳过）"
    if name == "look_at_image":
        img = args.get("path") or args.get("rel") or ""
        q = args.get("q") or "这张图里有什么问题？"
        cand = img if os.path.isabs(img) else os.path.join(PLATFORM_HOME, img)
        if not os.path.isfile(cand):
            return False, "找不到图片：" + str(img)
        try:
            lib = _lib()
            buf = []
            for piece in lib.stream_vision(q, [cand], system=VISION_PROMPT):
                buf.append(piece)
            text = "".join(buf).strip()
            return True, "【看图 %s】%s" % (os.path.basename(cand), text or "（没看出内容）")
        except Exception as exc:
            return False, "看图失败：%s: %s" % (type(exc).__name__, exc)
    if name == "run_command":
        r = run_command(args.get("cmd") or "", int(args.get("timeout") or DEFAULT_TIMEOUT),
                        args.get("cwd"))
        if r.get("error"):
            return False, r["error"]
        head = "退出码 %s，用时 %ss" % (r.get("code"), r.get("seconds"))
        return True, head + chr(10) + (r.get("output") or "(没有输出)")
    if name in ("done", "give_up"):
        return True, json.dumps(args, ensure_ascii=False)
    return run_tool(call)


def agent_stream(goal, max_rounds=16):
    """自主循环完成一个目标。

    产出事件 dict：
      {"type":"round","n":..,"of":..}   新的一轮
      {"type":"say","text":..}          AI 在想什么/说什么
      {"type":"tool","name":..,"ok":..,"result":..}
      {"type":"done","summary":..,"changes":[..]}     完成
      {"type":"give_up","reason":..}    放弃
      {"type":"limit","rounds":..}      到轮数上限
    """
    try:
        lib = _lib()
    except Exception as exc:
        yield {"type": "say", "text": "AI 库加载失败：" + str(exc)}
        return

    messages = [{"role": "system", "content": AGENT_PROMPT},
                {"role": "user", "content": "任务：" + goal}]
    changes = []          # 记录改过哪些文件，最后汇报用
    NL = chr(10)

    for rnd in range(1, max_rounds + 1):
        yield {"type": "round", "n": rnd, "of": max_rounds}
        buf = ""
        pending = ""
        try:
            for piece in lib.stream(messages):
                buf += piece
                pending += piece
                # 攒够 24 个字再发一次 —— 一个字一个字推太碎，看着像卡顿
                if len(pending) >= 24:
                    yield {"type": "say", "text": pending}
                    pending = ""
            if pending:
                yield {"type": "say", "text": pending}
        except Exception as exc:
            yield {"type": "say", "text": NL + "❌ 调用失败：" + str(exc)}
            return

        messages.append({"role": "assistant", "content": buf})

        m = re.search(r'TOOL:\s*(\{.*?\})\s*(?:' + NL + r'|$)', buf, re.S)
        if not m:
            yield {"type": "done",
                   "summary": "（没给 TOOL 行，按当前输出结束）" + NL + buf.strip()[:2000],
                   "changes": changes}
            return
        try:
            call = json.loads(m.group(1))
        except Exception:
            yield {"type": "say", "text": NL + "（工具调用格式不对，让它重来）"}
            messages.append({"role": "user",
                             "content": "你上面的 TOOL 行不是合法 JSON，重来一次。"})
            continue

        name = call.get("name")
        args = call.get("args") or {}
        if name == "done":
            yield {"type": "done", "summary": args.get("summary", ""),
                   "changes": changes}
            return
        if name == "give_up":
            yield {"type": "give_up", "reason": args.get("reason", ""),
                   "changes": changes}
            return

        ok, result = run_tool_ext(call)
        if ok and name in ("edit_snippet", "apply_edit"):
            rel = args.get("rel")
            if rel:
                changes.append(rel)
        shown = {k: ("(整文件内容)" if k == "new_text" else str(v)[:200])
                 for k, v in args.items()}
        yield {"type": "tool", "name": name, "ok": ok,
               "result": str(result)[:3000], "args": shown}
        messages.append({"role": "user",
                         "content": "工具 " + str(name) + " 的结果：" + NL +
                                    str(result)[:4000] + NL + NL +
                                    "继续。如果任务已完成，输出 done。"})

    yield {"type": "limit", "rounds": max_rounds, "changes": changes}


SYSTEM_PROMPT = """你是这个本地工作平台的助手，运行在用户的 Mac 上。

你的能力：
- 回答技术问题，尤其是 iOS/Swift、uni-app、接口对接、抓包分析
- 读懂用户贴的代码、日志、报文，指出问题
- **修改这个平台自己的代码**（改界面、加功能、修 bug）

改平台代码时的规矩：
1. 先说清楚你要改哪个文件、改什么、为什么
2. 用工具改，不要只说"你应该改成..."。可用工具：
   - read_file(rel)          读平台里的文件
   - list_files(sub)         列目录
   - edit_snippet(rel, find, replace)   按片段替换（find 必须唯一）
   - apply_edit(rel, new_text)          整文件覆盖（慎用）
   调用方式：在回复里输出一行
     TOOL: {"name":"edit_snippet","args":{"rel":"web/desktop.html","find":"...","replace":"..."}}
3. 属于"根入口"的文件不能改（platform.py / lib/ai_core.py / lib/platform_lib.py /
   identity.json）—— 这样万一改坏了，这个 AI 入口还在，还能救回来
4. 改错了可以回滚，备份都在 backups/ 里

回答用中文，简洁直接。不要客套，不要复述用户的话。"""


def _lib():
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)
    import platform_lib
    return platform_lib


def ready():
    try:
        return _lib().ready()
    except Exception:
        return False


def config():
    try:
        c = _lib().config()
        return {"provider": c.get("provider"), "model": c.get("model"),
                "provider_name": c.get("provider_name"),
                "ready": bool(c.get("api_key"))}
    except Exception as exc:
        return {"ready": False, "error": str(exc)}


def build_messages(question, history=None, context=None):
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in (history or [])[-12:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": str(h["content"])[:8000]})
    if context:
        msgs.append({"role": "system",
                     "content": "以下是相关文件内容，供参考：\n" + str(context)[:20000]})
    msgs.append({"role": "user", "content": question})
    return msgs


def stream(question, history=None, context=None):
    """流式回答。产出 (kind, text) —— kind 是 'text' 或 'tool'"""
    try:
        lib = _lib()
    except Exception as exc:
        yield ("text", f"❌ AI 库加载失败：{exc}")
        return

    msgs = build_messages(question, history, context)
    buf = ""
    try:
        for piece in lib.stream(msgs):
            buf += piece
            yield ("text", piece)
    except Exception as exc:
        yield ("text", f"\n❌ 调用失败：{type(exc).__name__}: {exc}")
        return

    # 从回答里抠出工具调用
    for m in re.finditer(r'TOOL:\s*(\{.*?\})\s*(?:\n|$)', buf, re.S):
        try:
            call = json.loads(m.group(1))
        except Exception:
            continue
        yield ("tool", json.dumps(call, ensure_ascii=False))


def run_tool(call):
    """执行 AI 请求的一个工具调用。返回 (ok, text)。

    统一转给 run_tool_ext —— 那边认识全部工具（含 run_command / check_syntax）。
    以前两条路径各认一部分，导致界面上能用的工具在 agent 里用不了（或反过来）。
    """
    name = (call or {}).get("name")
    if name in ("run_command", "check_syntax", "done", "give_up"):
        return run_tool_ext(call)
    args = (call or {}).get("args") or {}
    try:
        if name == "read_file":
            r = read_file(args.get("rel") or args.get("path") or "",
                          offset=args.get("offset"), limit=args.get("limit"),
                          around=args.get("around"))
            if not r.get("ok"):
                return False, r.get("error", "读失败")
            head = ("【%s】%s\n" % (r.get("path"), r.get("note", ""))
                    if r.get("note") else "")
            return True, head + r["text"][:16000]
        if name == "search":
            r = search(args.get("sub") or "", args.get("pattern") or "")
            if not r.get("ok"):
                return False, r.get("error", "搜索失败")
            return True, r["result"][:12000]
        if name == "list_files":
            r = list_files(args.get("sub") or "", int(args.get("depth") or 2))
            if not r.get("ok"):
                return False, r.get("error", "列失败")
            return True, "\n".join("%s  %d 字节" % (f["path"], f["bytes"])
                                   for f in r["files"][:200])
        if name == "search":
            r = search(args.get("sub") or "", args.get("pattern") or "")
            if not r.get("ok"):
                return False, r.get("error", "搜索失败")
            return True, r["result"][:12000]
        if name == "edit_snippet":
            r = edit_snippet(args.get("rel") or "", args.get("find") or "",
                             args.get("replace") or "", args.get("reason") or "AI 修改")
            return bool(r.get("ok")), (r.get("error") or
                                       "✅ 已改 %s（备份 %s）" % (r.get("file"), r.get("backup")))
        if name == "apply_edit":
            r = apply_edit(args.get("rel") or "", args.get("new_text") or "",
                           args.get("reason") or "AI 修改")
            return bool(r.get("ok")), (r.get("error") or
                                       "✅ 已改 %s（备份 %s）" % (r.get("file"), r.get("backup")))
        return False, "不认识的工具：%s" % name
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
