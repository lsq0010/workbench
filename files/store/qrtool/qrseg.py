"""给 qrgen 加最优分段（混合模式）—— 让码更小。

为什么值得做
  URL 里常混着长串数字（运单号），整串用字节模式要 8 位/字符；
  而数字模式只要 3.33 位/字符。拆成「字节段 + 数字段」能显著省位，
  结果就是**二维码版本更小、模块更少、更好扫**。

算法
  动态规划：在每个位置、对每种模式，算"从这里开始用这个模式编一段"的代价，
  取总位最少的切法。代价含每段的模式指示符 + 字符计数 + 数据位。
"""
import re

from qrgen import (MODE_NUM, MODE_ALNUM, MODE_BYTE, ALNUM_CHARS,
                   char_count_bits, payload_len)

_NUM_RE = re.compile(r"[0-9]+")
_ALNUM_RE = re.compile(r"[0-9A-Z $%*+\-./:]+")


def _seg_cost(mode, length, version):
    """一段的位数 = 模式指示符(4) + 字符计数 + 数据位"""
    if length <= 0:
        return None
    return 4 + char_count_bits(version, mode) + payload_len_for(mode, length)


def payload_len_for(mode, length):
    if mode == MODE_NUM:
        n, r = divmod(length, 3)
        return n * 10 + {0: 0, 1: 4, 2: 7}[r]
    if mode == MODE_ALNUM:
        n, r = divmod(length, 2)
        return n * 11 + r * 6
    return length * 8


def optimal_segments(data, version, max_segments=8):
    """把 data 切成若干段，每段用最省位的模式。返回 [(mode, 子串), ...]

    max_segments 限制段数 —— 段太多反而因为每段都要付"模式+计数"的开销而变差。
    """
    if not data:
        return []
    n = len(data)
    # 预处理：从每个位置开始，各模式能连续覆盖多长
    num_len = [0] * (n + 1)
    alnum_len = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        ch = data[i]
        num_len[i] = num_len[i + 1] + 1 if ch.isdigit() and ch.isascii() else 0
        alnum_len[i] = alnum_len[i + 1] + 1 if ch in ALNUM_CHARS else 0

    INF = float("inf")
    # best[i] = (最少总位, 段数, 上一位置, 用的模式, 段长)
    best = [None] * (n + 1)
    best[0] = (0, 0, -1, None, 0)

    for i in range(1, n + 1):
        for j in range(i):
            if best[j] is None:
                continue
            prev_bits, prev_segs, _, _, _ = best[j]
            if prev_segs >= max_segments:
                continue
            length = i - j
            # 从 j 到 i 这一段的模式选择
            for mode, cover in ((MODE_BYTE, length),
                                (MODE_ALNUM, alnum_len[j]),
                                (MODE_NUM, num_len[j])):
                if cover < length:
                    continue
                add = _seg_cost(mode, length, version)
                if add is None:
                    continue
                total = prev_bits + add
                if best[i] is None or total < best[i][0]:
                    best[i] = (total, prev_segs + 1, j, mode, length)

    if best[n] is None:
        return [(MODE_BYTE, data)]

    # 回溯
    segs = []
    i = n
    while i > 0:
        _, _, j, mode, length = best[i]
        segs.append((mode, data[j:i]))
        i = j
    segs.reverse()

    # 合并相邻同类段（避免为了 1 个字符多付一次头开销）
    merged = []
    for mode, text in segs:
        if merged and merged[-1][0] == mode:
            merged[-1] = (mode, merged[-1][1] + text)
        else:
            merged.append((mode, text))
    return merged


def total_bits(segments, version):
    """这些段一共要多少位"""
    total = 0
    for mode, text in segments:
        length = len(text) if mode != MODE_BYTE else len(text.encode("utf-8"))
        total += 4 + char_count_bits(version, mode) + payload_len_for(mode, length)
    return total


def describe(segments):
    """给人看的分段说明"""
    names = {MODE_NUM: "数字", MODE_ALNUM: "字母数字", MODE_BYTE: "字节"}
    return " + ".join(f"{names.get(m,'?')}({len(t)})" for m, t in segments)
