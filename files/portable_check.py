#!/usr/bin/env python3
"""可移植性自检 —— 确保平台本体不会悄悄退回到 macOS 专有调用。

为什么需要
  平台要同时跑在 macOS 和 Windows 上。系统专有的东西
  （open / lsof / ps / SIGKILL / start_new_session）只能出现在
  sys_* 那组跨平台函数里。别处冒出来一个，Windows 上就崩一片。

  这种事靠人肉 review 一定会漏，所以写成脚本。

标记
    某一行只是提到命令名、并不执行（比如"这些命令 Windows 上没有"的清单），
    在行尾加注释 `# portability-ok` 即可豁免。

跑法
    python3 portable_check.py          # 只查平台本体
    python3 portable_check.py --all    # 连功能一起查（功能可以有自己的判断）
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 只有这些函数里允许出现系统专有的调用（它们是跨平台的实现处）
ALLOWED_ZONES = (
    "sys_open", "sys_reveal", "sys_pids_on_port", "sys_cmdline",
    "sys_alive", "sys_kill", "sys_detached_kwargs", "sys_tmpdir",
    "args_port",
)

# 判据
#   关键：**只在"真的会执行"的地方判**。
#   光看命令名会把「命令白名单」「要摘掉的命令清单」这类声明也抓进来 ——
#   那是误报，而误报多了这个自检就没人看了。
#
#   所以：命令名必须出现在 subprocess 调用里才算；另外几个是 API 层面的，
#   只要出现就是问题（没有"声明"这种用法）。
MAC_CMDS = r'(open|lsof|ps|networksetup|osascript|pbcopy|pbpaste|mdfind|mdls|sips|plutil|codesign|security|sw_vers|launchctl|iconutil|ditto|hdiutil)'
CALL = r'(subprocess\.(run|Popen|call|check_output|check_call)|_sp\.(run|Popen|call))'
PATTERNS = [
    (CALL + r'\s*\(\s*\[[^\]]*["\']' + MAC_CMDS + r'["\']',
     '在 subprocess 里调 macOS 专有命令', '用 sys_* 里的跨平台版本'),
    (CALL + r'\s*\(\s*["\']' + MAC_CMDS + r'["\']',
     '在 subprocess 里调 macOS 专有命令', '用 sys_* 里的跨平台版本'),
    (r'signal\.SIGKILL', 'SIGKILL（Windows 没有）', '用 sys_kill(pid, hard=True)'),
    (r'start_new_session\s*=', 'start_new_session（POSIX 专有）', '用 sys_detached_kwargs()'),
    (r'os\.kill\(\s*\w+\s*,\s*0\s*\)', 'os.kill(pid,0) 探活', '用 sys_alive()'),
    (r'["\']/tmp/["\']', '硬编码 /tmp', '用 sys_tmpdir()'),
]

# 这些是"字段名/命令名"，不是系统调用，放行
WHITELIST = (
    '"open":',            # manifest 里的 open 字段
    'add_parser("open")',  # 子命令名
    'get("open")',        # 读 manifest 字段
    "def sys_",           # 跨平台函数自己
)


def zones(text):
    """算出允许区的行号范围。"""
    out = []
    lines = text.split("\n")
    for fn in ALLOWED_ZONES:
        i = text.find("def %s(" % fn)
        if i < 0:
            continue
        j = text.find("\ndef ", i + 5)
        start = text[:i].count("\n") + 1
        end = text[:j].count("\n") + 1 if j > 0 else len(lines)
        out.append((fn, start, end))
    return out


def check(path, verbose=False):
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as exc:
        return [("", "读不了：%s" % exc)]
    zs = zones(text)
    problems = []
    lines = text.split("\n")

    # 把跨行的语句拼成一块再判断。
    # 为什么：命令名清单常写成
    #     for _c in ("plutil", "lsof", "networksetup",
    #                "mdfind", "osascript"):
    # 标记只能落在其中一行上。按行判断必然误报，按语句才准。
    stmts = []          # (起始行号, [行内容...], 原文)
    buf, start = [], 0
    depth = 0
    for i, line in enumerate(lines, 1):
        if not buf:
            start = i
        buf.append(line)
        # 粗略算括号深度（跳过引号里的括号）
        clean = re.sub(r'"[^"]*"', '""', line)
        clean = re.sub(r"\[[^\]]*\]", "[]", clean)   # 去掉下标那种
        depth += clean.count("(") + clean.count("[") + clean.count("{")
        depth -= clean.count(")") + clean.count("]") + clean.count("}")
        if depth <= 0:
            stmts.append((start, buf, "\n".join(buf)))
            buf, depth = [], 0
    if buf:
        stmts.append((start, buf, "\n".join(buf)))

    for start, blines, whole in stmts:
        if any(w in whole for w in WHITELIST):
            continue
        # 字典键声明（命令白名单表项）不算调用
        if re.match(r'^\s*"[a-z_]+"\s*:', whole.lstrip()):
            continue
        # 显式豁免：只提到命令名、不实际执行
        if "portability-ok" in whole:
            continue
        flat = re.sub(r"\s+", " ", whole)      # 拼成一行，跨行调用也能匹配
        for pat, what, fix in PATTERNS:
            if not re.search(pat, flat):
                continue
            n = start
            in_zone = next((z[0] for z in zs if z[1] <= n <= z[2]), None)
            if in_zone:
                if verbose:
                    print("    · 第 %-4d %-40s → %s（允许）" % (n, what, in_zone))
                continue
            head = whole.strip().split("\n")[0][:88]
            problems.append((path, "第 %d 行有 %s —— %s\n        %s" % (n, what, fix, head)))
    return problems


def main():
    verbose = "-v" in sys.argv
    all_files = "--all" in sys.argv

    targets = ["platform.py", "healthcheck.py"]
    for d in ("lib",):
        p = os.path.join(HERE, d)
        if os.path.isdir(p):
            targets += [os.path.join(d, f) for f in sorted(os.listdir(p))
                        if f.endswith(".py")]

    print("  可移植性自检 —— 平台本体里不该有 macOS 专有调用")
    print("  （允许出现的只有 sys_* 那组跨平台函数）\n")

    allp = []
    for t in targets:
        p = os.path.join(HERE, t)
        if not os.path.isfile(p):
            continue
        probs = check(p, verbose)
        allp += probs
        print("    %-28s %s" % (t, "✅" if not probs else "❌ %d 处" % len(probs)))

    if all_files:
        print()
        print("  ── 功能（这些可以有自己的平台判断，只做提示）──")
        fdir = os.path.join(HERE, "store")
        if os.path.isdir(fdir):
            hit = 0
            for name in sorted(os.listdir(fdir)):
                mp = os.path.join(fdir, name, "main.py")
                if not os.path.isfile(mp):
                    continue
                probs = check(mp)
                if probs:
                    hit += 1
            print("    %d 个功能用了系统专有调用（Windows 上会自动跳过它们）" % hit)

    print()
    if allp:
        print("  ❌ %d 处需要修：" % len(allp))
        for path, msg in allp:
            print("    %s" % msg)
        return 1
    print("  ✅ 平台本体没有 macOS 专有调用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
