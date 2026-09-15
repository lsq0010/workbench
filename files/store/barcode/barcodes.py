#!/usr/bin/env python3
"""条形码编码器 —— 纯 Python，不依赖第三方库。

支持
  Code128（自动选 B/C 码表，数字多时自动压缩）
  EAN-13 / EAN-8 / UPC-A（带校验位，能给就自动补）
  Code39（含全 ASCII 扩展，可选）
  ITF-14（物流箱唛，带校验位）
  Codabar

为什么做条形码
  你只有二维码工具。二维码适合扫码，但**条形码有它的位置**：
    · 运单上的单号条码 —— 快递行业标准就是 Code128 / ITF-14
    · 箱唛 ITF-14 —— 物流外箱必备
    · 零售商品 EAN-13
  而且条形码能用激光枪扫，比二维码枪便宜得多。

怎么保证对的
  1. 每个格式的编码表都手写核对过
  2. 校验位按标准算（EAN 的 mod10 加权、Code128 的 mod103、ITF-14 的 mod10）
  3. `verify.py` 里有一套断言测试 —— 编码完自己跑一遍
  4. 生成的图能用 zbar / 系统扫码器读回来（如果装了）
"""
import re

# ══════════════════════════════════════════════════════════════
# Code 128
# ══════════════════════════════════════════════════════════════
# 107 个码字，每个 11 位（3 条 + 3 空），值 0~106。
# 0~102 是数据，103/104/105 是起始符（A/B/C），106 是终止符。
CODE128_PATTERNS = [
    "11011001100", "11001101100", "11001100110", "10010011000", "10010001100",
    "10001001100", "10011001000", "10011000100", "10001100100", "11001001000",
    "11001000100", "11000100100", "10110011100", "10011011100", "10011001110",
    "10111001100", "10011101100", "10011100110", "11001110010", "11001011100",
    "11001001110", "11011100100", "11001110100", "11101101110", "11101001100",
    "11100101100", "11100100110", "11101100100", "11100110100", "11100110010",
    "11011011000", "11011000110", "11000110110", "10100011000", "10001011000",
    "10001000110", "10110001000", "10001101000", "10001100010", "11010001000",
    "11000101000", "11000100010", "10110111000", "10110001110", "10001101110",
    "10111011000", "10111000110", "10001110110", "11101110110", "11010001110",
    "11000101110", "11011101000", "11011100010", "11011101110", "11101011000",
    "11101000110", "11100010110", "11101101000", "11101100010", "11100011010",
    "11101111010", "11001000010", "11110001010", "10100110000", "10100001100",
    "10010110000", "10010000110", "10000101100", "10000100110", "10110010000",
    "10110000100", "10011010000", "10011000010", "10000110100", "10000110010",
    "11000010010", "11001010000", "11110111010", "11000010100", "10001111010",
    "10100111100", "10010111100", "10010011110", "10111100100", "10011110100",
    "10011110010", "11110100100", "11110010100", "11110010010", "11011011110",
    "11011110110", "11110110110", "10101111000", "10100011110", "10001011110",
    "10111101000", "10111100010", "11110101000", "11110100010", "10111011110",
    "10111101110", "11101011110", "11110101110", "11010000100", "11010010000",
    "11010011100", "1100011101011",
]
CODE128_START_B = 104
CODE128_START_C = 105
CODE128_STOP = 106
# 码表 B：值 0~94 对应 ASCII 32~126
CODE128_B = {chr(i + 32): i for i in range(95)}
# 码表 C：值 0~99 直接是两位数
CODE128_A = {}
for _i in range(64):
    CODE128_A[chr(_i + 32)] = _i          # ' ' ~ '_'
for _i in range(32):
    CODE128_A[chr(_i)] = _i + 64          # 控制字符
CODE128_A["\x7f"] = 95
CODE128_A.update({chr(i + 96): i + 64 for i in range(32)})   # 小写 → 控制字符


def code128_encode(data, charset="auto"):
    """Code128 编码，返回位串（'1' 是黑条）。

    charset:
      auto → 数字够多就用 C 表（两位一个码字，更短）
      B    → 全用 B 表
      C    → 全用 C 表（data 必须是偶数位数字）
    """
    if not data:
        raise ValueError("内容不能为空")
    codes = []
    if charset == "auto":
        # 连续数字 >= 4 位就切 C 表更划算（每两位一个码字）
        m = re.match(r"^\d{4,}$", data)
        if m and len(data) % 2 == 0:
            charset = "C"
        elif re.match(r"^\d{4,}", data):
            # 前面一段数字（偶数位）用 C，后面用 B
            n = len(re.match(r"^\d+", data).group(0))
            n -= n % 2
            if n >= 4:
                codes.append(CODE128_START_C)
                for i in range(0, n, 2):
                    codes.append(int(data[i:i + 2]))
                rest = data[n:]
                if rest:
                    codes.append(100)          # Code B
                    for ch in rest:
                        if ch not in CODE128_B:
                            raise ValueError("Code128-B 编不了的字符：%r" % ch)
                        codes.append(CODE128_B[ch])
                codes.append(CODE128_STOP)
                return _code128_bits(codes)
            charset = "B"
        else:
            charset = "B"
    if charset == "C":
        if not data.isdigit():
            raise ValueError("Code128-C 只接受数字")
        if len(data) % 2:
            raise ValueError("Code128-C 要求偶数位数字")
        codes.append(CODE128_START_C)
        for i in range(0, len(data), 2):
            codes.append(int(data[i:i + 2]))
    else:
        codes.append(CODE128_START_B)
        for ch in data:
            if ch not in CODE128_B:
                raise ValueError("Code128-B 编不了的字符：%r（只支持 ASCII 32~126）" % ch)
            codes.append(CODE128_B[ch])
    codes.append(CODE128_STOP)
    return _code128_bits(codes)


def _code128_bits(codes):
    """码字序列 → 位串。顺便算校验位。"""
    body = codes[:-1]
    check = body[0]
    for i, c in enumerate(body[1:], start=1):
        check += c * i
    check %= 103
    out = []
    for c in codes[:-1]:
        out.append(CODE128_PATTERNS[c])
    out.append(CODE128_PATTERNS[check])
    out.append(CODE128_PATTERNS[CODE128_STOP])
    return "".join(out)


# ══════════════════════════════════════════════════════════════
# EAN-13 / EAN-8 / UPC-A
# ══════════════════════════════════════════════════════════════
# L/G/R 三组编码，各 7 位
EAN_L = ["0001101", "0011001", "0010011", "0111101", "0100011",
         "0110001", "0101111", "0111011", "0110111", "0001011"]
EAN_G = ["0100111", "0110011", "0011011", "0100001", "0011101",
         "0111001", "0000101", "0010001", "0001001", "0010111"]
EAN_R = ["1110010", "1100110", "1101100", "1000010", "1011100",
         "1001110", "1010000", "1000100", "1001000", "1110100"]
# 首位数字决定后 6 位的 L/G 组合
EAN13_PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
                "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


def ean_check_digit(digits):
    """EAN/UPC 的 mod10 校验位：从右往左 3、1 交替加权。"""
    s, weight = 0, 3
    for ch in reversed(digits):
        s += int(ch) * weight
        weight = 1 if weight == 3 else 3
    return str((10 - s % 10) % 10)


def ean13_encode(data, add_check=True):
    """EAN-13。data 12 位（自动补校验位）或 13 位（校验位对就收，错就报）。"""
    data = re.sub(r"\D", "", data or "")
    if len(data) == 12 and add_check:
        data += ean_check_digit(data)
    if len(data) != 13:
        raise ValueError("EAN-13 要 12 或 13 位数字，收到 %d 位" % len(data))
    if add_check and data[:12] + ean_check_digit(data[:12]) != data:
        raise ValueError("校验位不对：第 13 位应该是 %s" % ean_check_digit(data[:12]))
    first = int(data[0])
    parity = EAN13_PARITY[first]
    bits = "101"                                  # 左护线
    for i, ch in enumerate(data[1:7]):
        bits += EAN_L[int(ch)] if parity[i] == "L" else EAN_G[int(ch)]
    bits += "01010"                               # 中护线
    for ch in data[7:]:
        bits += EAN_R[int(ch)]
    bits += "101"                                 # 右护线
    return bits


def ean8_encode(data, add_check=True):
    """EAN-8。data 7 位（自动补）或 8 位。"""
    data = re.sub(r"\D", "", data or "")
    if len(data) == 7 and add_check:
        data += ean_check_digit(data)
    if len(data) != 8:
        raise ValueError("EAN-8 要 7 或 8 位数字，收到 %d 位" % len(data))
    bits = "101"
    for ch in data[:4]:
        bits += EAN_L[int(ch)]
    bits += "01010"
    for ch in data[4:]:
        bits += EAN_R[int(ch)]
    bits += "101"
    return bits


def upca_encode(data, add_check=True):
    """UPC-A。data 11 位（自动补）或 12 位。"""
    data = re.sub(r"\D", "", data or "")
    if len(data) == 11 and add_check:
        data += ean_check_digit(data)
    if len(data) != 12:
        raise ValueError("UPC-A 要 11 或 12 位数字，收到 %d 位" % len(data))
    if add_check and data[:11] + ean_check_digit(data[:11]) != data:
        raise ValueError("校验位不对：第 12 位应该是 %s" % ean_check_digit(data[:11]))
    # UPC-A 的结构和 EAN-13 一样（左 6 位 + 中护线 + 右 6 位），
    # 只是没有首位数字、左右护线都是 101。
    #
    # 踩过的坑：一开始想"从 EAN-13 结果里切一段"省事，
    # 结果切片把左护线和一个数据组切掉了 —— 图能画出来但扫不出。
    # 老老实实按结构拼才对。
    bits = "101"
    for ch in data[:6]:
        bits += EAN_L[int(ch)]
    bits += "01010"
    for ch in data[6:]:
        bits += EAN_R[int(ch)]
    bits += "101"
    return bits


# ══════════════════════════════════════════════════════════════
# Code 39
# ══════════════════════════════════════════════════════════════
# 每个字符 9 个元素（5 条 4 空），其中 3 个是宽的。
# nnnnntnnn = 窄窄窄窄窄宽窄窄窄
CODE39_TABLE = {
    "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn",
    "4": "nnnwwnnnw", "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw",
    "8": "wnnwnnwnn", "9": "nnwwnnwnn", "A": "wnnnnwnnw", "B": "nnwnnwnnw",
    "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn", "F": "nnwnwwnnn",
    "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww",
    "O": "wnnnwnnwn", "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn",
    "S": "nnwnnnwwn", "T": "nnnnwnwwn", "U": "wwnnnnnnw", "V": "nwwnnnnnw",
    "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn", "Z": "nwwnwnnnn",
    "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn", "$": "nwnwnwnnn",
    "/": "nwnwnnnwn", "+": "nwnnnwnwn", "%": "nnnwnwnwn", "*": "nwnnwnwnn",
}
CODE39_NARROW, CODE39_WIDE = 1, 3        # 宽窄比（2:1 或 3:1，这里用 3:1）


def code39_encode(data, add_checksum=False, full_ascii=False):
    """Code39。内容会转大写，两端自动加起始/终止符 '*'。"""
    data = (data or "").upper()
    if full_ascii:
        data = _code39_full_ascii(data)
    if not data:
        raise ValueError("内容不能为空")
    if add_checksum:
        s = sum(list(CODE39_TABLE).index(c) for c in data if c in CODE39_TABLE)
        data += list(CODE39_TABLE)[s % 43]
    bits = []
    for ch in "*" + data + "*":
        if ch not in CODE39_TABLE:
            raise ValueError("Code39 编不了的字符：%r" % ch)
        pat = CODE39_TABLE[ch]
        for i, e in enumerate(pat):
            w = CODE39_NARROW if e == "n" else CODE39_WIDE
            bits.append(("1" if i % 2 == 0 else "0") * w)   # 偶数位是条
        bits.append("0")                                   # 字符间一个窄空
    return "".join(bits)


def _code39_full_ascii(data):
    """Code39 全 ASCII 扩展：用 $ / % + 组合表示小写和其他符号。

    比如小写 'a' → '+A'；这是 Code39 的标准扩展方式。
    """
    out = []
    for ch in data:
        o = ord(ch)
        if ch in CODE39_TABLE and ch != "*":
            out.append(ch)
        elif o < 32:
            out.append("$" + chr(o + 64))
        elif ch in "$/+%":
            out.append("/" + chr(o + 32))
        elif 97 <= o <= 122:                    # 小写
            out.append("+" + chr(o - 32))
        else:
            out.append("%" + chr(o - 32))
    return "".join(out)


# ══════════════════════════════════════════════════════════════
# ITF-14（物流箱唛）
# ══════════════════════════════════════════════════════════════
# 交错 2/5 码：数字成对出现，第一个用条、第二个用空。
ITF_PATTERNS = ["nnwwn", "wnnnw", "nwnnw", "wwnnn", "nnwnw",
                "wnwnn", "nwwnn", "nnnww", "wnnwn", "nwnwn"]


def itf_encode(data, wide=3):
    """ITF（交错 2/5）。data 必须是偶数位数字。"""
    data = re.sub(r"\D", "", data or "")
    if not data:
        raise ValueError("内容不能为空")
    if len(data) % 2:
        raise ValueError("ITF 要求偶数位数字，收到 %d 位" % len(data))
    bits = []
    for i in range(0, len(data), 2):
        a, b = ITF_PATTERNS[int(data[i])], ITF_PATTERNS[int(data[i + 1])]
        for x, y in zip(a, b):                  # x 是条宽，y 是空宽
            bits.append("1" * (wide if x == "w" else 1))
            bits.append("0" * (wide if y == "w" else 1))
    # 起始 1010、终止 11101（标准规定）
    return "1010" + "".join(bits) + "11101"


def itf14_encode(data, add_check=True):
    """ITF-14 箱唛。data 13 位（自动补校验）或 14 位。"""
    data = re.sub(r"\D", "", data or "")
    if len(data) == 13 and add_check:
        data += ean_check_digit(data)
    if len(data) != 14:
        raise ValueError("ITF-14 要 13 或 14 位数字，收到 %d 位" % len(data))
    if add_check and data[:13] + ean_check_digit(data[:13]) != data:
        raise ValueError("校验位不对：第 14 位应该是 %s" % ean_check_digit(data[:13]))
    return itf_encode(data)


# ══════════════════════════════════════════════════════════════
# Codabar
# ══════════════════════════════════════════════════════════════
CODABAR_TABLE = {
    "0": "nnnnnww", "1": "nnnnwwn", "2": "nnnwnnw", "3": "wwnnnnn",
    "4": "nnwnnwn", "5": "wnnnnwn", "6": "nwnnnnw", "7": "nwnnwnn",
    "8": "nwwnnnn", "9": "wnnwnnn", "-": "nnnwwnn", "$": "nnwwnnn",
    ":": "wnnnwnw", "/": "wnwnnnw", ".": "wnwnwnn", "+": "nnwnwnw",
    "A": "nnwwnwn", "B": "nwnwnnw", "C": "nnnwnww", "D": "nnnwwwn",
}


def codabar_encode(data, start="A", stop="B"):
    """Codabar。常用于血库、图书馆、快递单。"""
    data = (data or "").upper()
    if not data:
        raise ValueError("内容不能为空")
    out = []
    for ch in start + data + stop:
        if ch not in CODABAR_TABLE:
            raise ValueError("Codabar 编不了的字符：%r" % ch)
        pat = CODABAR_TABLE[ch]
        for i, e in enumerate(pat):
            out.append(("1" if i % 2 == 0 else "0") * (3 if e == "w" else 1))
        out.append("0")                     # 字符间窄空
    return "".join(out)


# ══════════════════════════════════════════════════════════════
# 统一入口
# ══════════════════════════════════════════════════════════════
FORMATS = {
    "code128":  {"name": "Code128", "hint": "通用，运单号最常用。数字多会自动压缩",
                 "sample": "SF1234567890"},
    "code128c": {"name": "Code128-C", "hint": "纯数字且偶数位，最短",
                 "sample": "123456789012"},
    "ean13":    {"name": "EAN-13", "hint": "13 位，零售商品。12 位会自动补校验位",
                 "sample": "690123456789"},
    "ean8":     {"name": "EAN-8", "hint": "8 位小商品", "sample": "1234567"},
    "upca":     {"name": "UPC-A", "hint": "12 位，北美零售", "sample": "12345678901"},
    "code39":   {"name": "Code39", "hint": "老式工业码，只能大写字母和数字",
                 "sample": "ABC-1234"},
    "itf":      {"name": "ITF（交错2/5）", "hint": "偶数位数字", "sample": "12345678"},
    "itf14":    {"name": "ITF-14", "hint": "物流箱唛，13 位会自动补校验位",
                 "sample": "1234567890123"},
    "codabar":  {"name": "Codabar", "hint": "血库/图书馆/快递单", "sample": "123456"},
}


def encode(fmt, data, **kw):
    """按格式编码，返回 (位串, 说明)。

    位串里 '1' 是黑条、'0' 是白空。调用方按这个画图。
    """
    fmt = (fmt or "code128").lower()
    if fmt == "code128":
        return code128_encode(data), "Code128"
    if fmt == "code128c":
        return code128_encode(data, charset="C"), "Code128-C"
    if fmt == "code128b":
        return code128_encode(data, charset="B"), "Code128-B"
    if fmt == "ean13":
        return ean13_encode(data), "EAN-13"
    if fmt == "ean8":
        return ean8_encode(data), "EAN-8"
    if fmt == "upca":
        return upca_encode(data), "UPC-A"
    if fmt == "code39":
        return code39_encode(data, add_checksum=kw.get("checksum", False),
                             full_ascii=kw.get("full_ascii", False)), "Code39"
    if fmt == "itf":
        return itf_encode(data), "ITF"
    if fmt == "itf14":
        return itf14_encode(data), "ITF-14"
    if fmt == "codabar":
        return codabar_encode(data), "Codabar"
    raise ValueError("不认识的条码格式：%s（支持 %s）" % (fmt, "、".join(FORMATS)))


# 各格式要求的最小静区（左右留白），单位是"窄条宽度"的倍数
QUIET = {"code128": 10, "code128c": 10, "code128b": 10,
         "ean13": 11, "ean8": 7, "upca": 9,
         "code39": 10, "itf": 10, "itf14": 10, "codabar": 10}
