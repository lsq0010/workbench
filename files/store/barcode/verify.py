#!/usr/bin/env python3
"""条形码编码器的验证 —— 每个格式都做「编码 → 画图 → 扫回来 → 对比」。

为什么这么验
  自己写的编码器"看起来像条码"没有意义 —— 得能**真被扫出来**才算对。
  这里用 macOS 的 Vision 框架扫（和 iPhone 相机同源），
  扫回来的字符串和原文一致才算过。

跑法
    python3 verify.py            # 全部跑一遍
    python3 verify.py -v         # 显示每一项

失败会退出码 1。
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import barcodes as B          # noqa: E402
import render as R            # noqa: E402

DECODER = os.path.join(HERE, "decode.swift")

# (格式, 内容, 期望扫回来的内容) —— 期望值和输入不一定相同：
# EAN/UPC/ITF-14 会自动补校验位，扫出来是补完的完整码
CASES = [
    ("code128", "SF1234567890", "SF1234567890"),
    ("code128", "HELLO-123", "HELLO-123"),
    ("code128", "abcXYZ098", "abcXYZ098"),
    ("code128", "1234567890123456", "1234567890123456"),
    ("code128c", "8612345678901", None),      # 奇数位应当报错
    ("code128c", "861234567890", "861234567890"),
    ("ean13", "690123456789", "6901234567892"),
    ("ean13", "6901234567892", "6901234567892"),
    ("ean8", "1234567", "12345670"),
    # Vision 会把 UPC-A 按 EAN-13 报（前面补个 0）—— 这是正常的，
    # 因为 UPC-A 本来就是 EAN-13 的子集
    ("upca", "03600029145", "0036000291452"),
    ("code39", "ABC-1234", "ABC-1234"),
    ("itf", "12345678", "12345678"),
    ("itf14", "1234567890123", "12345678901231"),
    ("codabar", "123456", "A123456B"),   # Codabar 起止符 A/B 会一起被扫出来
]

# 明确应该报错的（格式不合法）
SHOULD_FAIL = [
    ("code128c", "12345"),        # 奇数位
    ("ean13", "123"),             # 位数不对
    ("itf", "123"),               # 奇数位
    ("code39", "中文"),             # ASCII 之外编不了
    ("ean8", "123"),                # 位数不对
    ("code128", "中文"),           # ASCII 之外
]


def decode(path):
    r = subprocess.run(["swift", DECODER, path], capture_output=True,
                       text=True, timeout=300)
    out = (r.stdout or "").strip()
    if not out or out == "NONE":
        return None, (r.stderr or "")[:200]
    first = out.split("\n")[0].split("\t")
    return (first[1] if len(first) > 1 else out), None


def one(fmt, data, expect, verbose=False):
    """跑一条用例，返回 (ok, 说明)"""
    try:
        bits, name = B.encode(fmt, data)
    except Exception as exc:
        if expect is None:
            return True, "按预期报错：%s" % exc
        return False, "编码失败：%s" % exc
    if expect is None:
        return False, "本该报错但编过了（%s）" % name
    if not bits or set(bits) - {"0", "1"}:
        return False, "位串不合法"
    png = R.bits_to_png(bits, module=3, height=110,
                        quiet=B.QUIET.get(fmt, 10), text=data)
    fd, path = tempfile.mkstemp(suffix=".png"); os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(png)
        got, err = decode(path)
        if got is None:
            return False, "扫不出来" + ("（%s）" % err if err else "")
        if got != expect:
            return False, "扫出来是 %r，期望 %r" % (got, expect)
        return True, "%s → 扫回 %r" % (name, got)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def main():
    verbose = "-v" in sys.argv
    print("  条形码编码器验证（编码 → 画图 → Vision 扫回 → 对比）")
    print("  解码器：macOS Vision（和 iPhone 相机同源）\n")
    if not os.path.isfile(DECODER):
        print("  ❌ 找不到 decode.swift")
        return 1

    ok = bad = 0
    for fmt, data, expect in CASES:
        good, msg = one(fmt, data, expect)
        if good:
            ok += 1
            if verbose:
                print("    ✅ %-9s %-18s %s" % (fmt, data[:18], msg))
        else:
            bad += 1
            print("    ❌ %-9s %-18s %s" % (fmt, data[:18], msg))

    print()
    print("  ── 该报错的 ──")
    for fmt, data in SHOULD_FAIL:
        good, msg = one(fmt, data, None)
        if good:
            ok += 1
            if verbose:
                print("    ✅ %-9s %-18s %s" % (fmt, data[:18], msg))
        else:
            bad += 1
            print("    ❌ %-9s %-18s %s" % (fmt, data[:18], msg))

    print()
    print("  通过 %d，失败 %d" % (ok, bad))
    if not verbose and not bad:
        print("  （加 -v 看每一项）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
