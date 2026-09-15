#!/usr/bin/env python3
"""纯 Python 二维码编码器 —— 支持版本 1~40，ECC L/M/Q/H，自动选掩码。

为什么要自己写
  生成是这个工具的主路径，不该依赖用户装了什么库。
  参数表从成熟实现提取一次内嵌（qrtables.py），运行时零依赖。
  正确性由 verify.py 对着 python-qrcode 逐位比对保证。

实现依据 ISO/IEC 18004：
  1. 数据编码（数字 / 字母数字 / 字节三种模式，自动挑最短的）
  2. 分块 + Reed-Solomon 纠错 + 交错
  3. 矩阵排布（定位图形、校正图形、时序、暗模块、格式/版本信息）
  4. 8 种掩码按惩罚分选最优
"""

from qrtables import RS_BLOCKS, ALIGN_POS

# ── 模式指示符 ──
MODE_NUM = 0b0001
MODE_ALNUM = 0b0010
MODE_BYTE = 0b0100
MODE_KANJI = 0b1000          # 不实现，只用于容量判断

ALNUM_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
ECC_ORDER = ["L", "M", "Q", "H"]      # RS_BLOCKS 里的顺序
ECC_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}   # 格式信息里的 2 位

# ── GF(256)，本原多项式 0x11D ──
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def rs_generator(n):
    """生成 n 次 Reed-Solomon 生成多项式"""
    g = [1]
    for i in range(n):
        # 乘 (x - α^i)
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c                      # × x
            ng[j + 1] ^= gf_mul(c, EXP[i])  # × α^i
        g = ng
    return g


def rs_encode(data, n_ecc):
    """给 data 算 n_ecc 个纠错码字"""
    g = rs_generator(n_ecc)
    rem = list(data) + [0] * n_ecc
    for i in range(len(data)):
        coef = rem[i]
        if coef == 0:
            continue
        for j in range(len(g)):
            rem[i + j] ^= gf_mul(g[j], coef)
    return rem[len(data):]


# ── 位流 ──
class Bits:
    def __init__(self):
        self.bits = []

    def put(self, value, length):
        for i in range(length - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def __len__(self):
        return len(self.bits)

    def to_bytes(self):
        out = bytearray()
        for i in range(0, len(self.bits), 8):
            b = 0
            for bit in self.bits[i:i + 8]:
                b = (b << 1) | bit
            b <<= (8 - len(self.bits[i:i + 8]))
            out.append(b)
        return bytes(out)


# ── 各版本容量 ──
def data_codewords(version, ecc):
    """该版本该纠错级别能装多少数据码字"""
    groups = RS_BLOCKS[version - 1][ECC_ORDER.index(ecc)]
    total = 0
    for i in range(0, len(groups), 3):
        count, _tot, dat = groups[i], groups[i + 1], groups[i + 2]
        total += count * dat
    return total


def char_count_bits(version, mode):
    """字符计数字段的位数（版本分档）"""
    if 1 <= version <= 9:
        return {MODE_NUM: 10, MODE_ALNUM: 9, MODE_BYTE: 8}[mode]
    if 10 <= version <= 26:
        return {MODE_NUM: 12, MODE_ALNUM: 11, MODE_BYTE: 16}[mode]
    return {MODE_NUM: 14, MODE_ALNUM: 13, MODE_BYTE: 16}[mode]


def pick_mode(data):
    """挑编码模式：数字/字母数字/字节里能装下且最短的"""
    if all(c in "0123456789" for c in data):
        return MODE_NUM
    if all(c in ALNUM_CHARS for c in data):
        return MODE_ALNUM
    return MODE_BYTE


def encode_data(data, mode):
    """按模式编码成位流（不含模式指示符和字符计数）"""
    b = Bits()
    if mode == MODE_NUM:
        for i in range(0, len(data), 3):
            chunk = data[i:i + 3]
            b.put(int(chunk), {1: 4, 2: 7, 3: 10}[len(chunk)])
    elif mode == MODE_ALNUM:
        for i in range(0, len(data) - 1, 2):
            b.put(ALNUM_CHARS.index(data[i]) * 45 + ALNUM_CHARS.index(data[i + 1]), 11)
        if len(data) % 2:
            b.put(ALNUM_CHARS.index(data[-1]), 6)
    else:
        raw = data.encode("utf-8")
        for byte in raw:
            b.put(byte, 8)
    return b


def payload_len(data, mode):
    """这个模式下实际要写多少位"""
    if mode == MODE_NUM:
        n, r = divmod(len(data), 3)
        return n * 10 + {0: 0, 1: 4, 2: 7}[r]
    if mode == MODE_ALNUM:
        n, r = divmod(len(data), 2)
        return n * 11 + r * 6
    return len(data.encode("utf-8")) * 8


def build_codewords(data, version, ecc, mode, segments=None):
    """数据 → 码字（含模式指示符、字符计数、终止符、填充）。

    segments 给了就按分段写（混合模式，更省位）；否则整串用 mode。
    """
    cap_bits = data_codewords(version, ecc) * 8
    b = Bits()
    if segments:
        for m, text in segments:
            b.put(m, 4)
            count = len(text) if m != MODE_BYTE else len(text.encode("utf-8"))
            b.put(count, char_count_bits(version, m))
            payload = encode_data(text, m)
            for bit in payload.bits:
                b.bits.append(bit)
    else:
        b.put(mode, 4)
        count = len(data) if mode != MODE_BYTE else len(data.encode("utf-8"))
        b.put(count, char_count_bits(version, mode))
        payload = encode_data(data, mode)
        for bit in payload.bits:
            b.bits.append(bit)
    # 终止符（最多 4 位）
    for _ in range(min(4, cap_bits - len(b))):
        b.bits.append(0)
    # 补齐到字节
    while len(b) % 8:
        b.bits.append(0)
    cw = bytearray(b.to_bytes())
    # 交替填充 0xEC / 0x11
    pad = [0xEC, 0x11]
    i = 0
    while len(cw) < data_codewords(version, ecc):
        cw.append(pad[i % 2])
        i += 1
    return bytes(cw)


def add_ecc_and_interleave(cw, version, ecc):
    """分块 → 每块算纠错 → 按标准交错"""
    groups = RS_BLOCKS[version - 1][ECC_ORDER.index(ecc)]
    blocks = []
    pos = 0
    for i in range(0, len(groups), 3):
        count, total, dat = groups[i], groups[i + 1], groups[i + 2]
        n_ecc = total - dat
        for _ in range(count):
            chunk = cw[pos:pos + dat]
            pos += dat
            blocks.append({"data": chunk, "ecc": rs_encode(chunk, n_ecc)})
    # 交错：先按列取所有块的数据码字，再按列取纠错码字
    out = bytearray()
    max_data = max(len(b["data"]) for b in blocks)
    for i in range(max_data):
        for b in blocks:
            if i < len(b["data"]):
                out.append(b["data"][i])
    max_ecc = max(len(b["ecc"]) for b in blocks)
    for i in range(max_ecc):
        for b in blocks:
            if i < len(b["ecc"]):
                out.append(b["ecc"][i])
    return bytes(out)


# ── 矩阵 ──
def new_matrix(size):
    return [[None] * size for _ in range(size)]


def place_finder(m, row, col):
    """定位图形 + 分隔符"""
    size = len(m)
    for r in range(-1, 8):
        for c in range(-1, 8):
            rr, cc = row + r, col + c
            if not (0 <= rr < size and 0 <= cc < size):
                continue
            if 0 <= r <= 6 and 0 <= c <= 6:
                edge = r in (0, 6) or c in (0, 6)
                inner = 2 <= r <= 4 and 2 <= c <= 4
                m[rr][cc] = 1 if (edge or inner) else 0
            else:
                m[rr][cc] = 0          # 分隔符


def place_alignment(m, version):
    pos = ALIGN_POS[version - 1]
    size = len(m)
    for r in pos:
        for c in pos:
            if m[r][c] is not None:      # 和定位图形重叠就跳过
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < size and 0 <= cc < size:
                        m[rr][cc] = 1 if max(abs(dr), abs(dc)) != 1 else 0


def place_timing(m):
    size = len(m)
    for i in range(8, size - 8):
        bit = 1 if i % 2 == 0 else 0
        if m[6][i] is None:
            m[6][i] = bit
        if m[i][6] is None:
            m[i][6] = bit


def version_of(size):
    """从矩阵边长反推版本号（size = 4 * version + 17）"""
    return (size - 17) // 4


def reserve_format(m):
    """先把格式/版本信息的位置占住（值稍后填）"""
    size = len(m)
    for i in range(9):
        if m[8][i] is None:
            m[8][i] = 0
        if m[i][8] is None:
            m[i][8] = 0
    for i in range(8):
        if m[8][size - 1 - i] is None:
            m[8][size - 1 - i] = 0
        if m[size - 1 - i][8] is None:
            m[size - 1 - i][8] = 0
    m[size - 8][8] = 1                   # 固定的暗模块
    # 版本信息（v>=7）：18 位，两个 3×6 的块
    #   · 右上：(0..5, size-11 .. size-9)
    #   · 左下：(size-11 .. size-9, 0..5)
    # ⚠️ 这里必须占位！漏了的话这 36 个格子会被当成数据格，
    #    数据位流从那里开始整体错位 —— v7 及以上全部扫不出来。
    if version_of(size) >= 7:
        for r in range(6):
            for c in range(3):
                if m[r][size - 11 + c] is None:
                    m[r][size - 11 + c] = 0
                if m[size - 11 + c][r] is None:
                    m[size - 11 + c][r] = 0


def bch_format(data):
    """格式信息：5 位 → 15 位（BCH(15,5)，生成多项式 0x537）"""
    d = data << 10
    for i in range(4, -1, -1):
        if d & (1 << (i + 10)):
            d ^= 0x537 << i
    return ((data << 10) | d) ^ 0x5412


def bch_version(version):
    """版本信息：6 位 → 18 位（BCH(18,6)，生成多项式 0x1F25）"""
    d = version << 12
    for i in range(5, -1, -1):
        if d & (1 << (i + 12)):
            d ^= 0x1F25 << i
    return (version << 12) | d


def place_format_test(m, version):
    """给掩码评分时用的占位：格式信息、版本信息、暗模块全部置浅色。

    标准做法（ISO 18004 以及成熟实现）在**挑掩码**阶段并不填真的格式信息，
    而是把它们当作浅色来算惩罚分；选完才填真值。
    如果拿带真值的矩阵去评分，分数会和标准实现不一样，选出的掩码就可能不同 ——
    二维码本身仍然能扫，但和别的实现产出不一致，也没法用逐位比对来验证。
    """
    size = len(m)
    for i in range(9):
        m[8][i] = 0
        m[i][8] = 0
    for i in range(8):
        m[8][size - 1 - i] = 0
        m[size - 1 - i][8] = 0
    if version >= 7:
        for i in range(18):
            r, c = i // 3, i % 3
            m[size - 11 + c][r] = 0
            m[r][size - 11 + c] = 0


def place_format(m, ecc, mask):
    """放置格式信息（15 位，两份副本）。

    排布依据 ISO/IEC 18004（也对着 qrcode 库的源码核过）：
      · 竖直一份在第 8 列：bit0~5 在 (0..5,8)，bit6~7 在 (7,8)(8,8)，
        bit8~14 在 (size-7 .. size-1, 8)
      · 水平一份在第 8 行：bit0~7 在 (8, size-1 .. size-8)，
        bit8 在 (8,7)，bit9~14 在 (8,5 .. 8,0)

    ⚠️ 这两份曾经被我整个写反（竖直的写成水平），
       结果就是数据区完全正确、但任何扫码器都读不出来。
       靠 OpenCV 解码 + 和 qrcode 库逐位比对才定位到。
    """
    size = len(m)
    fmt = bch_format((ECC_BITS[ecc] << 3) | mask)
    for i in range(15):
        bit = (fmt >> i) & 1
        # 竖直
        if i < 6:
            m[i][8] = bit
        elif i < 8:
            m[i + 1][8] = bit
        else:
            m[size - 15 + i][8] = bit
        # 水平
        if i < 8:
            m[8][size - i - 1] = bit
        elif i < 9:
            m[8][7] = bit
        else:
            m[8][15 - i - 1] = bit
    m[size - 8][8] = 1          # 固定的暗模块


def place_version(m, version):
    if version < 7:
        return
    size = len(m)
    vi = bch_version(version)
    for i in range(18):
        bit = (vi >> i) & 1
        r, c = i // 3, i % 3
        m[size - 11 + c][r] = bit
        m[r][size - 11 + c] = bit


def place_data(m, codewords):
    """数据按之字形从右下往上填"""
    size = len(m)
    bits = []
    for byte in codewords:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:                 # 跳过时序列
            col -= 1
        for i in range(size):
            row = (size - 1 - i) if upward else i
            for c in (col, col - 1):
                if m[row][c] is None:
                    m[row][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        upward = not upward
        col -= 2


def mask_fn(mask, r, c):
    if mask == 0: return (r + c) % 2 == 0
    if mask == 1: return r % 2 == 0
    if mask == 2: return c % 3 == 0
    if mask == 3: return (r + c) % 3 == 0
    if mask == 4: return (r // 2 + c // 3) % 2 == 0
    if mask == 5: return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6: return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


def is_function(version, size, r, c):
    """这个位置是不是功能图形（不能参与掩码）"""
    if r < 9 and c < 9: return True
    if r < 9 and c >= size - 8: return True
    if r >= size - 8 and c < 9: return True
    if r == 6 or c == 6: return True
    for ar in ALIGN_POS[version - 1]:
        for ac in ALIGN_POS[version - 1]:
            if abs(r - ar) <= 2 and abs(c - ac) <= 2:
                if not ((r < 9 and c < 9) or (r < 9 and c >= size - 8)
                        or (r >= size - 8 and c < 9)):
                    return True
    if version >= 7:
        if r < 6 and c >= size - 11 and c < size - 8: return True
        if c < 6 and r >= size - 11 and r < size - 8: return True
    return False


def apply_mask(m, version, mask, reserved=None):
    """按掩码翻转数据格。reserved 是放置时打的标记，比几何推断可靠"""
    size = len(m)
    out = [row[:] for row in m]
    for r in range(size):
        for c in range(size):
            is_fn = reserved[r][c] if reserved is not None else is_function(version, size, r, c)
            if not is_fn and mask_fn(mask, r, c):
                out[r][c] ^= 1
    return out


def penalty(m):
    """ISO 18004 的四条掩码惩罚规则。

    这里刻意和成熟实现对齐到**逐位一致**，两个细节容易写错：
      · 规则3 的 1:1:3:1:1 模式：朴素扫描会把重叠匹配重复计分，
        标准实现遇到 col+10 为深色时会跳一格（horspool）。照做。
      · 规则4 用**浮点**算偏离比例再取整，先整除会在边界上差一档。
    """
    size = len(m)
    score = 0

    # 规则1：同行/同列连续同色 ≥5 → 每个这样的段罚 (长度 - 2)
    container = [0] * (size + 1)
    for line in list(m) + [list(col) for col in zip(*m)]:
        prev = line[0]
        length = 0
        for v in line:
            if v == prev:
                length += 1
            else:
                if length >= 5:
                    container[length] += 1
                length = 1
                prev = v
        if length >= 5:
            container[length] += 1
    score += sum(container[L] * (L - 2) for L in range(5, size + 1))

    # 规则2：2×2 同色 → 每块罚 3
    for r in range(size - 1):
        row, nxt = m[r], m[r + 1]
        c = 0
        while c < size - 1:
            if nxt[c + 1] != row[c + 1]:
                c += 2                      # 这一列不可能构成同色 2×2，跳过
                continue
            if row[c + 1] == row[c] == nxt[c]:
                score += 3
            c += 1

    # 规则3：1:1:3:1:1（深:浅:深:浅:深）且一侧有 4 格浅色 → 每次罚 40
    PAT1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    PAT2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for line in list(m) + [list(col) for col in zip(*m)]:
        c = 0
        while c < size - 10:
            seg = line[c:c + 11]
            if seg == PAT1 or seg == PAT2:
                score += 40
            c += 2 if line[c + 10] else 1   # 和标准实现同样的跳跃

    # 规则4：深色占比每偏离 50% 达 5% → 罚 10
    dark = sum(sum(row) for row in m)
    percent = float(dark) / (size ** 2)
    score += int(abs(percent * 100 - 50) / 5) * 10
    return score


def best_version(data, ecc, min_version=1, max_version=40, use_segments=True):
    """挑能装下的最小版本，并给出最优分段。

    先用单模式估一版（便宜），再用最优分段精算（省位）——
    两者取小的那个版本。
    """
    import qrseg
    mode = pick_mode(data)
    if not use_segments:
        for v in range(min_version, max_version + 1):
            need = 4 + char_count_bits(v, mode) + payload_len(data, mode)
            if need <= data_codewords(v, ecc) * 8:
                return v, mode, [(mode, data)]
        return None, mode, None
    single_v = None
    for v in range(min_version, max_version + 1):
        need = 4 + char_count_bits(v, mode) + payload_len(data, mode)
        if need <= data_codewords(v, ecc) * 8:
            single_v = v
            break
    if single_v is None:
        return None, mode, None

    # 用单模式版本当作分段计算的上限（分段只会更省，不会更费）
    seg_v = None
    for v in range(min_version, single_v + 1):
        segs = qrseg.optimal_segments(data, v)
        if qrseg.total_bits(segs, v) <= data_codewords(v, ecc) * 8:
            seg_v = v
            break
    if seg_v is not None and seg_v < single_v:
        segs = qrseg.optimal_segments(data, seg_v)
        return seg_v, mode, segs
    segs = qrseg.optimal_segments(data, single_v)
    return single_v, mode, segs


def encode(text, ecc="M", min_version=1, max_version=40, mask=None, use_segments=True):
    """生成二维码矩阵。返回 (matrix, info)"""
    if not text:
        raise ValueError("内容不能为空")
    version, mode, segments = best_version(text, ecc, min_version, max_version,
                                           use_segments=use_segments)
    if version is None:
        raise ValueError(f"内容太长了，版本 40 装不下（{len(text.encode('utf-8'))} 字节）")
    cw = build_codewords(text, version, ecc, mode, segments)
    final = add_ecc_and_interleave(cw, version, ecc)

    size = version * 4 + 17
    m = new_matrix(size)
    # reserved[r][c] = True 表示这里是功能图形，不参与掩码。
    # 关键：这个标记必须在**放置时**打，不能用几何规则事后推断 ——
    # 校正图形和定位图形重叠时会被跳过，几何规则却仍以为那块是功能图形，
    # 结果掩码漏掉那几个格子，扫码器就读不出来了。
    reserved = [[False] * size for _ in range(size)]

    def mark(fn, *a):
        fn(*a)
        for r in range(size):
            for c in range(size):
                if m[r][c] is not None:
                    reserved[r][c] = True

    mark(place_finder, m, 0, 0)
    mark(place_finder, m, 0, size - 7)
    mark(place_finder, m, size - 7, 0)
    mark(place_alignment, m, version)
    mark(place_timing, m)
    mark(reserve_format, m)
    place_data(m, final)

    if mask is None:
        best, best_score = 0, None
        for mk in range(8):
            cand = apply_mask(m, version, mk, reserved)
            place_format_test(cand, version)     # 评分阶段格式信息占位为浅色
            s = penalty(cand)
            if best_score is None or s < best_score:
                best, best_score = mk, s
        mask = best
    out = apply_mask(m, version, mask, reserved)
    place_format(out, ecc, mask)
    place_version(out, version)
    return out, {"version": version, "ecc": ecc, "mask": mask,
                 "mode": {MODE_NUM: "数字", MODE_ALNUM: "字母数字",
                          MODE_BYTE: "字节(UTF-8)"}[mode],
                 "size": size, "bytes": len(text.encode("utf-8")),
                 "capacity": data_codewords(version, ecc),
                "segments": [{"mode": m, "len": len(t)} for m, t in (segments or [])]}


def to_svg(matrix, scale=8, border=4, dark="#000000", light="#ffffff", radius=0):
    """输出 SVG（矢量，放大不糊）"""
    n = len(matrix)
    total = (n + border * 2) * scale
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="{total}" '
             f'viewBox="0 0 {total} {total}" shape-rendering="crispEdges">',
             f'<rect width="{total}" height="{total}" fill="{light}"/>']
    # 合并同一行的连续模块，SVG 小很多
    for r in range(n):
        c = 0
        while c < n:
            if matrix[r][c]:
                start = c
                while c < n and matrix[r][c]:
                    c += 1
                x = (border + start) * scale
                y = (border + r) * scale
                w = (c - start) * scale
                if radius:
                    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{scale}" '
                                 f'rx="{radius}" fill="{dark}"/>')
                else:
                    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{scale}" '
                                 f'fill="{dark}"/>')
            else:
                c += 1
    parts.append('</svg>')
    return "\n".join(parts)


def to_png(matrix, scale=8, border=4, dark=(0, 0, 0), light=(255, 255, 255), path=None):
    """输出 PNG。需要 Pillow；没装就抛异常让上层提示"""
    from PIL import Image
    n = len(matrix)
    total = (n + border * 2) * scale
    img = Image.new("RGB", (total, total), light)
    px = img.load()
    for r in range(n):
        for c in range(n):
            if matrix[r][c]:
                for dy in range(scale):
                    for dx in range(scale):
                        px[(border + c) * scale + dx, (border + r) * scale + dy] = dark
    if path:
        img.save(path)
    return img


if __name__ == "__main__":
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    m, info = encode(text)
    print("  %s" % info)
    for row in m[:1]:
        pass
    # 终端预览
    for r in range(0, len(m), 2):
        line = ""
        for c in range(len(m)):
            top = m[r][c]
            bot = m[r + 1][c] if r + 1 < len(m) else 0
            line += "█" if (top and bot) else ("▀" if top else ("▄" if bot else " "))
        print("  " + line)
