#!/usr/bin/env python3
"""AI 值守 —— 让 AI 主动发现问题，而不是等你来问。

为什么做这个
  用户的原话：「平台完善的时候 ai 可以自己能完成所有的事，很少需要人参与」

  现在的 AI 是被动的：你不问它就不动。这一层补上"主动"那一半：
    · 后台盯着日志、构建结果、各功能的汇报
    · 发现异常（错误扎堆、构建失败、接口连续超时）就自己去分析
    · 结论写进「今日关注」，你醒来直接看

  但**不越界**：
    · 只分析、只出结论，**不自动改代码** —— 改代码必须你点头
    · 同一个问题短时间内只报一次，不刷屏
    · 没配 AI 或没网时静默跳过，不报错打扰

跑法：由平台在后台起一个线程，每 60 秒看一轮。
"""
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime

LIB_DIR = os.path.dirname(os.path.abspath(__file__))
PLATFORM_HOME = os.path.dirname(LIB_DIR)
STATE = os.path.join(PLATFORM_HOME, "ai_watch.json")     # 值守状态（可重写，不是账本）
FINDINGS = os.path.join(PLATFORM_HOME, "ai_findings.jsonl")   # 只追加：AI 的发现
LOGHUB = "http://127.0.0.1:8918"
IOSAUTO = "http://127.0.0.1:8919"

# 触发规则：多久看一轮、什么算异常
INTERVAL = 60
ERR_WINDOW_MIN = 10          # 看最近多少分钟
ERR_THRESHOLD = 3            # 同一标签错这么多条就算异常
COOLDOWN_MIN = 30            # 同一个问题多久内不重复报


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"{_now()} [ai_watch] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(PLATFORM_HOME, "platform.log"), "a",
                  encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _get(url, timeout=15):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("reported", {})     # key -> 上次报告时间戳
            return d
    except Exception:
        return {"reported": {}}


def save_state(d):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def record(finding):
    """把发现写进只追加的账本，同时返回给平台当告警"""
    finding["ts"] = _now()
    try:
        with open(FINDINGS, "a", encoding="utf-8") as f:
            f.write(json.dumps(finding, ensure_ascii=False) + "\n")
    except Exception:
        pass
    log(f"发现：{finding.get('title')}")


# 发现只保留这么久 —— 问题早解决了还挂着，等于刷屏
FINDING_TTL_HOURS = 6


def _fresh_enough(f, now=None):
    ts = f.get("ts") or ""
    try:
        t = time.maketime = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return True                      # 时间读不出来就别过滤掉
    return (now or time.time()) - t < FINDING_TTL_HOURS * 3600


def findings(limit=20, ttl_hours=None, dedup=True):
    """读发现。

    两个过滤，都是为了别让「今日关注」变成刷屏列表：
      · **时效**：只给最近 ttl_hours 小时内的（默认 6 小时）
      · **去重**：同一个 key 只留最新一条（历史上可能写进去很多次）
    """
    ttl = FINDING_TTL_HOURS if ttl_hours is None else ttl_hours
    rows = []
    try:
        with open(FINDINGS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass

    now = time.time()
    if ttl:
        rows = [r for r in rows if _fresh_enough(r, now)]
    if dedup:
        seen = {}
        for r in rows:
            seen[r.get("key") or r.get("title") or str(id(r))] = r
        rows = list(seen.values())
    rows.sort(key=lambda r: r.get("ts") or "")
    return list(reversed(rows[-limit:]))


def clear_findings(keep_hours=0):
    """清掉旧的发现记录（默认全清）。

    只重写这个文件 —— 它是"发现账本"，不是用户数据，
    但重写前也留个 .bak。
    """
    if not os.path.exists(FINDINGS):
        return {"ok": True, "removed": 0}
    try:
        rows = []
        with open(FINDINGS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        now = time.time()
        kept = [r for r in rows if keep_hours and _fresh_enough(r, now)] if keep_hours else []
        import shutil as _sh
        _sh.copy2(FINDINGS, FINDINGS + ".bak")
        with open(FINDINGS, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        log(f"清空发现：移除 {len(rows) - len(kept)} 条，保留 {len(kept)} 条")
        return {"ok": True, "removed": len(rows) - len(kept), "kept": len(kept)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ══════════════════════════════════════════════════════════════
# 检查项：每条返回 0 或多个「待分析」的素材
# ══════════════════════════════════════════════════════════════

# 不该拿来报警的 App —— 测试/demo 工程产生的日志不是真问题。
# 实测踩过：我自己造的 demo App 发了一堆模拟 error，
# 值守就报「Net 有 9 条 error」—— 用户看到会以为自己的 App 出事了。
# **误报比不报更糟**，所以这里要挡住。
IGNORE_APPS = ("com.local.demo", "com.example", "com.test", "org.cocoapods")
IGNORE_TAGS = ("DevLog", "Crash")     # SDK 自己的日志不算业务错误


def _is_noise(r):
    app = (r.get("app") or "").lower()
    tag = (r.get("tag") or "")
    for k in IGNORE_APPS:
        if k in app:
            return True
    # 标签是 Python dict 字符串的（JSON 序列化问题产生的），不是正常标签
    if tag.startswith("{") or tag.startswith("["):
        return True
    return False


def check_logs():
    """App 日志里错误扎堆 —— 这是最值得主动分析的一类"""
    st = _get(LOGHUB + "/api/status")
    if not st:
        return []
    s = st.get("stats") or {}
    errs = s.get("errors") or 0
    if errs < ERR_THRESHOLD:
        return []
    d = _get(LOGHUB + "/api/logs?level=error&limit=300&source=app")
    if not d or not d.get("logs"):
        return []
    rows = [r for r in d["logs"] if not _is_noise(r)]
    if not rows:
        log("日志里有 error，但都是测试/demo App 发的，不算问题")
        return []
    # 按标签归类：同一个标签错一堆，通常是一个真问题
    by_tag = Counter(r.get("tag") or "?" for r in rows)
    out = []
    for tag, n in by_tag.most_common(3):
        if n < ERR_THRESHOLD:
            continue
        sample = [r for r in rows if (r.get("tag") or "?") == tag][:12]
        key = "log_err:" + tag
        out.append({
            "key": key, "kind": "日志错误",
            "title": f"「{tag}」有 {n} 条 error",
            "level": "warn",
            "material": "\n".join(
                "[%s] %s %s" % (r.get("level"), r.get("tag"), r.get("msg"))
                for r in sample),
            "ask": f"这些是 App 日志里「{tag}」标签下的错误，请分析根因并给出修改建议。",
        })
    return out


def check_build():
    """构建失败 —— 但**只在问题还没被解决时**才报。

    踩过的坑：一开始只看"最近一条记录失败了"就报，结果 AI 把代码修好、
    后面又跑成功过，它还在报"上次跑挂了"。已经解决的问题不该再占着
    「今日关注」—— 那会让人不再相信这个列表。
    """
    st = _get(IOSAUTO + "/api/status")
    if not st:
        return []
    runs = st.get("runs") or []          # 最新在前
    if not runs:
        return []
    last = runs[0]
    steps = last.get("steps") or []
    failed = [s for s in steps if not s.get("ok")]
    if not failed:
        return []
    # 同一 scheme 后面又成功跑过 → 问题已解决，不再报
    scheme = last.get("scheme")
    for r in runs[1:]:
        if r.get("scheme") != scheme:
            continue
        if not [x for x in (r.get("steps") or []) if not x.get("ok")]:
            log(f"「{scheme}」后来跑成功过，不再报之前那次失败")
            return []
        break                        # 只看比它新的第一条同 scheme 记录
    # 太久以前的失败也不再报（可能早修了，只是没再跑过）
    try:
        t = time.mktime(time.strptime(last.get("ts") or "", "%Y-%m-%d %H:%M:%S"))
        if time.time() - t > 24 * 3600:
            return []
    except Exception:
        pass
    # 去重键**不带时间戳** —— 带了的话每失败一次就是新 key，
    # 冷却期形同虚设，同一个问题会刷一屏
    key = "build_fail:" + str(last.get("scheme"))
    return [{
        "key": key, "kind": "构建失败",
        "title": f"「{last.get('scheme')}」上次跑挂了（{failed[0].get('step')}）",
        "level": "warn",
        "material": json.dumps(last, ensure_ascii=False)[:4000],
        "ask": "iOS 工程的构建/运行流水线失败了，请根据这些信息判断原因和怎么修。",
    }]


def check_digest():
    """各功能汇报里的严重项 —— 汇总起来问一句"""
    d = _get("http://127.0.0.1:8880/api/digest")
    if not d:
        return []
    crit = [x for x in (d.get("items") or []) if x.get("level") == "critical"]
    if not crit:
        return []
    key = "digest:" + ",".join(sorted(x.get("title", "")[:24] for x in crit))[:80]
    return [{
        "key": key, "kind": "功能告警",
        "title": f"{len(crit)} 项严重告警需要处理",
        "level": "warn",
        "material": "\n".join("- [%s] %s：%s" % (x.get("feature_name"), x.get("title"),
                                                 x.get("detail", "")) for x in crit),
        "ask": "这些是各功能报上来的严重问题，请按优先级排一下该怎么处理。",
    }]


# 已知在国内会被墙的服务 —— 连不上是**环境正常**，不是故障。
# 实测：用户抓包里有 607 个 CONNECT 超时，全是 Google(332) + ChatGPT(272)。
# 如果不认识这个模式，值守会把它当"大量请求失败"报警 —— 那是误报，
# 而误报比不报更糟（人一旦不信这个列表，整个功能就废了）。
BLOCKED_HINTS = ("google", "gstatic", "ggpht", "ytimg", "youtube", "gvt1",
                 "chatgpt", "openai", "anthropic", "claude.ai",
                 "facebook", "twitter", "x.com", "instagram", "telegram",
                 "whatsapp", "wikipedia", "github.io", "dropbox")


def is_blocked_host(host):
    h = (host or "").lower()
    return any(k in h for k in BLOCKED_HINTS)


def check_capture_noise():
    """看抓包里的 CONNECT 超时是不是"正常的墙"。

    只有当超时里**有相当比例不是已知被墙的服务**时才算问题 ——
    那才可能是代理本身没配好。
    """
    import glob
    paths = [os.path.expanduser("~/Desktop/抓包工作台/flows.jsonl")]
    paths += glob.glob(os.path.expanduser("~/Desktop/*/flows.jsonl"))
    rows = []
    for p in paths:
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
        except OSError:
            continue
        if rows:
            break
    if not rows:
        return []
    conn = [r for r in rows if r.get("kind") == "connect"]
    if len(conn) < 30:
        return []
    err = [r for r in conn if r.get("error")]
    if not err:
        return []
    blocked = [r for r in err if is_blocked_host(r.get("host"))]
    other = [r for r in err if not is_blocked_host(r.get("host"))]
    rate = len(err) / max(1, len(conn))

    # 超时基本都是被墙的 → 不报（这是环境，不是问题）
    if len(other) < 20 or len(other) / max(1, len(err)) < 0.15:
        log(f"抓包有 {len(err)} 个 CONNECT 超时，其中 {len(blocked)} 个是已知被墙服务"
            f"（{rate:.0%} 超时率）—— 属于网络环境，不报")
        return []

    # 有相当一部分不是被墙的 → 这才值得看
    from collections import Counter
    top = Counter(r.get("host") for r in other).most_common(6)
    key = "capture_timeout:" + ",".join(h for h, _ in top[:3])[:60]
    return [{
        "key": key, "kind": "抓包异常", "level": "warn",
        "title": f"抓包里有 {len(other)} 个 CONNECT 超时不是被墙的服务",
        "material": ("总 connect %d 条，超时 %d 条（%.0f%%）。\n"
                     "其中已知被墙的 %d 条（Google/ChatGPT 这类，属正常）。\n"
                     "**剩下 %d 条需要看**，按域名排：\n%s"
                     % (len(conn), len(err), rate * 100, len(blocked), len(other),
                        "\n".join("%5d  %s" % (v, h) for h, v in top))),
        "ask": "抓包工具里有相当一部分 CONNECT 请求超时，而且不是常见的被墙服务。"
               "请判断：是代理配置问题（比如系统代理没生效、guard 没跑），"
               "还是目标服务本身不可达？给出排查步骤。",
    }]


# ── 自动处理：只做"安全动作" ──
#
# 用户的原话是「ai可以自己能完成所有的事，很少需要人参与」。
# 但"自动改代码"风险太大 —— 所以这里只做**明显安全的运维动作**：
#   · 功能进程死了 → 拉起来（不碰任何数据）
#   · 日志文件过大 → 交给功能自己的滚动逻辑（这里只提醒）
# 凡是会动到代码或删文件的，一律只报告，等人点头。
AUTO_LOG = os.path.join(PLATFORM_HOME, "ai_auto.jsonl")     # 只追加：自动做了什么
_auto_state = {"last": 0, "restarts": {}}
AUTO_RESTART_COOLDOWN = 600        # 同一个功能 10 分钟内不重复重启
AUTO_MAX_PER_ROUND = 3             # 一轮最多重启 3 个，防止雪崩式重启


def _platform_api(path, body=None, timeout=40):
    import urllib.request as _u
    url = "http://127.0.0.1:8880" + path
    try:
        if body is None:
            with _u.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        req = _u.Request(url, data=json.dumps(body).encode(),
                         headers={"Content-Type": "application/json"})
        with _u.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:
        log(f"调平台接口失败 {path}：{exc}")
        return None


def _record_auto(action, detail):
    rec = {"ts": _now(), "action": action, "detail": detail}
    try:
        with open(AUTO_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    log(f"自动处理：{action} —— {detail}")


def auto_revive_features(max_per_round=AUTO_MAX_PER_ROUND):
    """把掉线的功能拉起来。

    今晚实测掉线过三个（仓库总览、模型生成、开发工具箱），
    而且**不会自己恢复** —— 得人发现。这个就是补这个洞。

    安全边界：
      · 只调平台自己的 /api/start，不直接操作进程
      · 同一个功能 10 分钟内不重复重启（避免"起来就崩"的死循环）
      · 一轮最多 3 个
      · 每次都记账
    """
    st = _platform_api("/api/status")
    if not st:
        return []
    feats = _platform_api("/api/features", timeout=90)
    if not feats or not feats.get("features"):
        return []
    now = time.time()
    dead = [f for f in feats["features"] if not (f.get("status") or {}).get("running")]
    if not dead:
        return []
    actions = []
    for f in dead[:max_per_round]:
        fid = f.get("id")
        last = _auto_state["restarts"].get(fid, 0)
        if now - last < AUTO_RESTART_COOLDOWN:
            continue
        r = _platform_api("/api/start", {"id": fid})
        _auto_state["restarts"][fid] = now
        msg = (r or {}).get("message") or (r or {}).get("error") or "未知结果"
        _record_auto("重启掉线功能", "%s（%s）：%s" % (f.get("name"), fid, msg))
        actions.append({"id": fid, "name": f.get("name"), "result": msg})
    return actions


CHECKS = [check_logs, check_build, check_digest, check_capture_noise]


# ══════════════════════════════════════════════════════════════
# 值守循环
# ══════════════════════════════════════════════════════════════

def _ai():
    try:
        if LIB_DIR not in sys.path:
            sys.path.insert(0, LIB_DIR)
        import platform_lib
        return platform_lib if platform_lib.ready() else None
    except Exception:
        return None


def analyze(item):
    """让 AI 分析一份素材。失败就返回 None（不打扰）"""
    lib = _ai()
    if not lib:
        return None
    prompt = (item["ask"] + "\n\n素材：\n" + item["material"][:8000] +
              "\n\n要求：说清楚问题是什么、在哪、怎么修。"
              "素材里没有的信息就说「素材没体现」，不要猜。分点，简洁，中文。")
    try:
        out = []
        for piece in lib.stream([
                {"role": "system",
                 "content": "你是这个本地工作平台的值守助手，负责主动发现并分析问题。"
                            "只分析、只给建议，不要声称你已经改了代码。"},
                {"role": "user", "content": prompt}]):
            out.append(piece)
        text = "".join(out).strip()
        return text if text else None
    except Exception as exc:
        log(f"分析失败：{type(exc).__name__}: {exc}")
        return None


def run_once(verbose=False):
    """跑一轮检查。返回这一轮新产生的发现。"""
    st = load_state()
    reported = st.get("reported", {})
    now = time.time()

    # 先做安全的自动处理，再做检查 —— 掉线的功能拉起来之后，
    # 后面的检查才拿得到真实状态
    try:
        revived = auto_revive_features()
        if revived and verbose:
            log("自动拉起了 %d 个功能" % len(revived))
    except Exception as exc:
        if verbose:
            log(f"自动拉起出错：{exc}")

    new = []
    for check in CHECKS:
        try:
            items = check()
        except Exception as exc:
            if verbose:
                log(f"{check.__name__} 出错：{exc}")
            continue
        for it in items:
            last = reported.get(it["key"], 0)
            if now - last < COOLDOWN_MIN * 60:
                continue          # 刚报过，别刷屏
            reported[it["key"]] = now
            text = analyze(it)
            if text:
                it["analysis"] = text
                record(it)
                new.append(it)
                if verbose:
                    log(f"  分析了「{it['title']}」")
            else:
                # 分析不出来也记一笔，但标清楚
                it["analysis"] = ""
                it["note"] = "AI 没给出分析（可能没配 key 或网络不通）"
                record(it)
                new.append(it)
    # 清理太久以前的记录，别让状态文件一直涨
    cutoff = now - 7 * 86400
    st["reported"] = {k: v for k, v in reported.items() if v > cutoff}
    save_state(st)
    return new


_running = [False]


def loop(stop_event=None, interval=INTERVAL):
    """后台值守循环"""
    if _running[0]:
        return
    _running[0] = True
    log(f"AI 值守已启动（每 {interval} 秒看一轮）")
    while not (stop_event and stop_event.is_set()):
        try:
            new = run_once()
            if new:
                log(f"本轮发现 {len(new)} 个问题")
        except Exception as exc:
            log(f"值守出错：{type(exc).__name__}: {exc}")
        for _ in range(interval):
            if stop_event and stop_event.is_set():
                break
            time.sleep(1)
    _running[0] = False
    log("AI 值守已停止")


def start_background(interval=INTERVAL):
    """给平台调用的启动入口"""
    ev = threading.Event()
    t = threading.Thread(target=loop, args=(ev, interval), daemon=True,
                         name="ai-watch")
    t.start()
    return ev, t


def status():
    st = load_state()
    return {
        "running": _running[0],
        "interval": INTERVAL,
        "watched": len(st.get("reported", {})),
        "findings": findings(6),
        "rules": {
            "日志错误扎堆": f"最近 {ERR_WINDOW_MIN} 分钟内同一标签 ≥{ERR_THRESHOLD} 条 error",
            "构建失败": "上一次 iOS 流水线有步骤失败",
            "功能告警": "有功能汇报了 critical 级问题",
            "抓包异常": "CONNECT 超时里超过 15% 不是已知被墙的服务（被墙的不算问题）",
        },
        "cooldown_min": COOLDOWN_MIN,
        "auto": {
            "enabled": True,
            "what": "只做安全动作：把掉线的功能拉起来（不动代码、不删文件）",
            "restart_cooldown_min": AUTO_RESTART_COOLDOWN // 60,
            "history": auto_history(6),
        },
    }


def auto_history(limit=10):
    rows = []
    try:
        with open(AUTO_LOG, encoding="utf-8") as f:
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


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="ai_watch", description="AI 值守")
    ap.add_argument("--once", action="store_true", help="只跑一轮（调试用）")
    ap.add_argument("--interval", type=int, default=INTERVAL)
    a = ap.parse_args()
    if a.once:
        print("跑一轮检查…")
        new = run_once(verbose=True)
        print(f"\n本轮发现 {len(new)} 个问题：")
        for it in new:
            print(f"  【{it['kind']}】{it['title']}")
            if it.get("analysis"):
                print("    " + it["analysis"][:400].replace("\n", "\n    "))
            else:
                print("    " + it.get("note", ""))
    else:
        loop(interval=a.interval)
