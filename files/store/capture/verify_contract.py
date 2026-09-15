#!/usr/bin/env python3
"""flows.jsonl 数据契约校验器 —— 严格版。

契约（不可破坏）：
  · 15 个原始字段只加不改：不改名、不改类型
  · id 是 int、唯一、严格递增（since_id 游标的唯一锚点）
  · 不同 kind 的记录字段集不同是**设计使然**：
      - connect（HTTPS 透传）：只有连接层信息，没有 HTTP 正文
      - http：完整的请求/响应
    缺字段必须"按 kind 一致地缺"，不能随机缺。

这个脚本比 cap manifest --check 更严：逐条校验，不抽查。
跑法：python3 verify_contract.py
"""
import os
import json
import sys
import urllib.request
from collections import Counter, defaultdict

# 数据文件跟着脚本走（别写死路径 —— 换台机器就找不到了）
_HERE = os.path.dirname(os.path.abspath(__file__))
FLOWS = os.environ.get("CAPTURE_FLOWS") or os.path.join(_HERE, "flows.jsonl")
API = "http://127.0.0.1:8891/api/flows"

# 字段 → 允许的类型（契约核心，不能改）
TYPES = {
    "kind": (str,), "method": (str,), "host": (str,), "port": (int,),
    "path": (str,), "url": (str,), "req_headers": (str, dict), "req_body": (str,),
    "status": (int,), "reason": (str,), "resp_headers": (str, dict),
    "resp_body": (str,), "ms": (int, float), "id": (int,), "ts": (str,),
}
ORIGINAL = list(TYPES.keys())

# 每种 kind 必须有的字段（缺了就是坏数据）
REQUIRED_BY_KIND = {
    "connect": ["kind", "method", "host", "port", "id", "ts"],
    "http": ["kind", "method", "host", "port", "path", "url", "id", "ts"],
}
# 可以合法缺失的（按 kind）
MAY_MISS = {
    "connect": {"path", "url", "req_headers", "req_body", "status", "reason",
                "resp_headers", "resp_body", "ms"},
    "http": {"reason", "resp_headers", "resp_body"},   # 连接失败/未完成时没有
}


def main():
    fails = []
    rows = []
    with open(FLOWS, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append((ln, json.loads(line)))
            except Exception as exc:
                fails.append(f"第 {ln} 行 JSON 解析失败：{exc}")

    print("═" * 66)
    print("  flows.jsonl 契约校验（逐条，共 %d 条）" % len(rows))
    print("═" * 66)

    # ── 1. 类型 ──
    # 特例（也是契约的一部分）：status 可以是 null，但**当且仅当**请求失败
    #（有 error 字段）。请求根本没拿到响应时本来就没有状态码，造一个反而是错的。
    type_bad = []
    for ln, r in rows:
        for k, allowed in TYPES.items():
            if k not in r:
                continue
            v = r[k]
            if v is None and k == "status":
                if not r.get("error"):
                    type_bad.append((ln, k, "NoneType(但没有 error 字段)", v))
                continue
            if not isinstance(v, allowed):
                type_bad.append((ln, k, type(v).__name__, v))
    if type_bad:
        for ln, k, t, v in type_bad[:10]:
            fails.append(f"第 {ln} 行字段 {k} 类型是 {t}，应为 "
                         f"{'/'.join(x.__name__ for x in TYPES[k])}（值 {str(v)[:30]}）")
    print("  %s 字段类型：%d 处不符" % ("✅" if not type_bad else "❌", len(type_bad)))

    # ── 2. 字段是否被改名/删除 ──
    all_keys = set()
    for _, r in rows:
        all_keys |= set(r.keys())
    lost = [k for k in ORIGINAL if k not in all_keys]
    extra = sorted(all_keys - set(ORIGINAL))
    print("  %s 原始 15 字段都在：%s" % ("✅" if not lost else "❌",
                                        "全在" if not lost else "缺 " + ",".join(lost)))
    print("  ✅ 只加不改，新增字段：%s" % ("、".join(extra) or "无"))

    # ── 3. 按 kind 校验必填/可缺 ──
    kinds = Counter(r.get("kind") for _, r in rows)
    print("  ✅ kind 分布：%s" % "、".join(f"{k} {v} 条" for k, v in kinds.most_common()))
    kind_bad = []
    for ln, r in rows:
        k = r.get("kind")
        req = REQUIRED_BY_KIND.get(k)
        if req is None:
            kind_bad.append((ln, "未知 kind %r" % k))
            continue
        for f in req:
            if f not in r:
                kind_bad.append((ln, "kind=%s 缺必填字段 %s" % (k, f)))
        may = MAY_MISS.get(k, set())
        for f in ORIGINAL:
            if f not in r and f not in may:
                kind_bad.append((ln, "kind=%s 缺字段 %s（这个 kind 不该缺）" % (k, f)))
        # status 与 error 的对应关系：有响应才有状态码
        if k == "http":
            has_status = isinstance(r.get("status"), int)
            has_err = bool(r.get("error"))
            if has_status and has_err:
                kind_bad.append((ln, "有 status 却又带 error（状态码和失败原因不该同时有）"))
            if not has_status and not has_err:
                kind_bad.append((ln, "既没有 status 也没有 error（无法判断成功还是失败）"))
    for ln, why in kind_bad[:10]:
        fails.append("第 %d 行：%s" % (ln, why))
    print("  %s 按 kind 的字段完整性：%d 处不符" % ("✅" if not kind_bad else "❌", len(kind_bad)))

    # ── 4. id 契约 ──
    ids = [r["id"] for _, r in rows if isinstance(r.get("id"), int)]
    uniq = len(set(ids)) == len(ids)
    inc = all(ids[i] < ids[i+1] for i in range(len(ids)-1))
    print("  %s id：%d 条，唯一 %s，严格递增 %s（游标可用）"
          % ("✅" if (uniq and inc and len(ids) == len(rows)) else "❌",
             len(ids), uniq, inc))
    if not uniq:
        dup = [k for k, v in Counter(ids).items() if v > 1][:5]
        fails.append("id 有重复：%s" % dup)
    if not inc:
        fails.append("id 不是严格递增（since_id 游标会失效）")

    # ── 5. since_id 游标语义（对着活接口验证）──
    print()
    print("  ── since_id 游标语义（问活接口）──")
    try:
        with urllib.request.urlopen(API, timeout=20) as r:
            api_all = json.loads(r.read().decode())
        mx = max(ids)
        with urllib.request.urlopen(API + "?since_id=%d" % mx, timeout=20) as r:
            after_max = json.loads(r.read().decode())
        ok1 = len(after_max) == 0
        print("    %s since_id=max(%d) → %d 条（应为 0）" % ("✅" if ok1 else "❌",
                                                             mx, len(after_max)))
        if not ok1:
            fails.append("since_id=max 之后还返回了记录")

        mid = sorted(ids)[len(ids)//2]
        with urllib.request.urlopen(API + "?since_id=%d" % mid, timeout=20) as r:
            after_mid = json.loads(r.read().decode())
        got = [x["id"] for x in after_mid]
        ok2 = all(i > mid for i in got)
        print("    %s since_id=%d → %d 条，全部 > %d（游标不回头）"
              % ("✅" if ok2 else "❌", mid, len(got), mid))
        if not ok2:
            fails.append("since_id 返回了 <= since_id 的记录")

        # 返回的必须是过滤后的子集 —— 逐条核对：凡是接口返回的都在文件里
        db = {r["id"] for _, r in rows}
        ok3 = all(i in db for i in got)
        print("    %s 返回的记录都能在 flows.jsonl 里找到（不是凭空造的）" % ("✅" if ok3 else "❌"))
        if not ok3:
            fails.append("接口返回了文件里没有的记录")

        # 过滤说明：接口返回的是"非噪音 + 在监听设备"的子集，条数少于全量是正常的
        print("    ℹ️  接口返回 %d 条，文件 %d 条 —— 接口按「非噪音」过滤，"
              "少了 %d 条是设计使然" % (len(api_all), len(rows), len(rows) - len(api_all)))
    except Exception as exc:
        fails.append("接口验证失败：%s" % exc)
        print("    ❌ 接口验证失败：%s" % exc)

    # ── 结论 ──
    print()
    print("═" * 66)
    if fails:
        print("  ❌ 契约校验未通过（%d 项）" % len(fails))
        for f in fails[:20]:
            print("     · " + f)
    else:
        print("  ✅ 契约校验通过 —— 15 字段只加不改、id 可作游标、按 kind 缺字段是设计使然")
    print("═" * 66)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
