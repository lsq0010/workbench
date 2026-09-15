#!/usr/bin/env python3
"""Git 周报 —— 扫多个仓库，按需求分组，生成周报。

为什么做这个
  用户最早的诉求就是"做这周的总结，分需求总结，这个分支做了啥，总结功能点"。
  手工翻 git log 拼周报要半小时，这里点一下就有草稿，还能让 AI 润色。

怎么分组
  一个分支通常就是一个需求（比如 dev_lanjianshaomiao_tiji）。
  所以按「仓库 → 分支 → 提交」聚合，并从提交信息里提取功能点。

AI 润色是可选的：没配 key 也能出结构化草稿。
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))

VERSION = "0.1.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.environ.get("GITREPORT_HOME") or SCRIPT_DIR)
CONFIGFILE = os.path.join(HOME, "config.json")
REPORTS = os.path.join(HOME, "reports.jsonl")          # 只追加
PIDFILE = os.path.join(HOME, "gitreport.pid")
LOGFILE = os.path.join(HOME, "gitreport.log")
DEFAULT_PORT = 8894


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_json(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return d if d is not None else {}


def write_json(p, d):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def cfg():
    c = read_json(CONFIGFILE, {})
    c.setdefault("repos", [])
    c.setdefault("author", "")
    return c


def save_cfg(c):
    write_json(CONFIGFILE, c)


# ══════════════════════════════════════════════════════════════
# Git
# ══════════════════════════════════════════════════════════════

def git(repo, *args, timeout=25):
    try:
        p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                           text=True, timeout=timeout, errors="replace")
        return p.stdout if p.returncode == 0 else ""
    except Exception:
        return ""


def is_repo(path):
    return os.path.isdir(os.path.join(path, ".git")) or bool(
        git(path, "rev-parse", "--git-dir").strip())


def discover_repos(roots=None, depth=2):
    """在常见位置找 git 仓库（用户不用手填路径也能开始用）"""
    roots = roots or [os.path.expanduser("~/Desktop"), os.path.expanduser("~/Documents"),
                      os.path.expanduser("~/Projects"), os.path.expanduser("~/code")]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("/").count("/")
        for cur, dirs, _ in os.walk(root):
            if cur.count("/") - base_depth >= depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("node_modules", "Pods", "build", "DerivedData")]
            if ".git" in os.listdir(cur):
                found.append(cur)
                dirs[:] = []
    return sorted(set(found))


def repo_info(path):
    name = os.path.basename(path)
    branch = git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() or "?"
    last = git(path, "log", "-1", "--date=short", "--pretty=%ad").strip()
    n = len(git(path, "rev-list", "--all", "--count").strip() or "")
    return {"path": path, "name": name, "branch": branch, "last_commit": last}


def commits_between(repo, since, until, author=None, all_branches=True):
    """取时间范围内的提交（含各分支）"""
    args = ["log", f"--since={since}", f"--until={until}",
            "--date=format:%Y-%m-%d %H:%M", "--pretty=format:%h\x1f%ad\x1f%an\x1f%d\x1f%s\x1f%b\x1e"]
    if all_branches:
        args.append("--all")
    if author:
        args.append(f"--author={author}")
    out = git(repo, *args)
    items = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        parts = chunk.split("\x1f")
        if len(parts) < 5:
            continue
        h, ad, an, refs, subj = parts[0], parts[1], parts[2], parts[3], parts[4]
        body = parts[5] if len(parts) > 5 else ""
        br = ""
        m = re.search(r"HEAD -> ([^,)]+)", refs) or re.search(r"\(([^,)]+)[,)]", refs)
        if m:
            br = m.group(1).strip()
        items.append({"hash": h, "date": ad, "author": an, "refs": refs.strip(),
                      "branch": br, "subject": subj.strip(), "body": body.strip()})
    return items


# 提交类型 → 中文分类（约定式提交）
TYPES = {
    "feat": "新功能", "fix": "修复", "perf": "性能", "refactor": "重构",
    "style": "样式", "docs": "文档", "test": "测试", "chore": "杂项",
    "build": "构建", "ci": "流程", "revert": "回滚",
}
TYPE_RE = re.compile(r"^\s*(\w+)(?:\(([^)]+)\))?\s*[:：]\s*(.+)$")


# 中文自由提交的分类关键词。用户的实际提交是"修复bug""新增揽件量方"这种，
# 不是 conventional commits —— 按前缀分会把"修复bug"错当成新功能。
KW = [
    ("修复", re.compile(r"修复|修\s*bug|bug|缺陷|问题|错误|异常|崩溃|闪退|"
                        r"不显示|失效|失败|卡死|死锁|报错|漏|错乱")),
    ("新功能", re.compile(r"新增|增加|支持|添加|实现|接入|上线|加入|新建")),
    ("优化", re.compile(r"优化|调整|改进|完善|提升|精简|重构|美化|兼容|适配")),
    ("移除", re.compile(r"删除|去掉|移除|废弃|下线")),
]


def classify(subject):
    m = TYPE_RE.match(subject)
    if m and m.group(1).lower() in TYPES:          # 有约定式前缀就优先用它
        return TYPES[m.group(1).lower()], (m.group(2) or ""), m.group(3).strip()
    for kind, pat in KW:
        if pat.search(subject):
            return kind, "", subject
    return "其他", "", subject


def build_report(repos, since, until, author=None):
    """把多个仓库的提交聚合成周报结构"""
    result = {"since": since, "until": until, "author": author,
              "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
              "repos": [], "total_commits": 0, "by_type": {}, "by_branch": {}}
    for rp in repos:
        if not is_repo(rp):
            continue
        info = repo_info(rp)
        cs = commits_between(rp, since, until, author)
        if not cs:
            result["repos"].append({**info, "commits": [], "count": 0})
            continue
        # 按分支分组（分支 ≈ 需求）
        groups = {}
        for c in cs:
            b = c["branch"] or info["branch"] or "(未知分支)"
            groups.setdefault(b, []).append(c)
        branches = []
        for b, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            feats, fixes, others = [], [], []
            for c in items:
                kind, scope, text = classify(c["subject"])
                result["by_type"][kind] = result["by_type"].get(kind, 0) + 1
                row = {"hash": c["hash"], "date": c["date"], "kind": kind,
                       "scope": scope, "text": text, "author": c["author"]}
                if kind == "新功能":
                    feats.append(row)
                elif kind == "修复":
                    fixes.append(row)
                else:
                    others.append(row)
            branches.append({"branch": b, "count": len(items),
                             "feats": feats, "fixes": fixes, "others": others,
                             "first": items[-1]["date"] if items else "",
                             "last": items[0]["date"] if items else ""})
            result["by_branch"][b] = result["by_branch"].get(b, 0) + len(items)
        result["repos"].append({**info, "commits": cs, "count": len(cs), "branches": branches})
        result["total_commits"] += len(cs)
    return result


def report_markdown(rep):
    """把结构渲染成人能直接用的周报草稿"""
    L = [f"# 周报（{rep['since'][:10]} ~ {rep['until'][:10]}）", ""]
    if rep.get("author"):
        L.append(f"**提交人**：{rep['author']}")
    L.append(f"**统计**：{rep['total_commits']} 次提交，"
             f"{len([r for r in rep['repos'] if r['count']])} 个仓库有改动")
    if rep["by_type"]:
        L.append("**类型分布**：" + "、".join(
            f"{k} {v}" for k, v in sorted(rep["by_type"].items(), key=lambda x: -x[1])))
    L.append("")
    if not rep["total_commits"]:
        L.append("> 这个时间段没有提交。检查一下时间范围或提交人过滤。")
        return "\n".join(L)

    L.append("---")
    L.append("")
    L.append("## 按需求分组")
    L.append("")
    for r in rep["repos"]:
        if not r["count"]:
            continue
        L.append(f"### {r['name']}　`{r['count']}` 次提交")
        L.append("")
        for b in r.get("branches", []):
            L.append(f"#### 分支 `{b['branch']}`　{b['count']} 次提交")
            L.append("")
            if b["feats"]:
                L.append("**新功能**")
                L.append("")
                for x in b["feats"]:
                    sc = f"（{x['scope']}）" if x["scope"] else ""
                    L.append(f"- {x['text']}{sc}　`{x['hash']}` {x['date'][:10]}")
                L.append("")
            if b["fixes"]:
                L.append("**修复**")
                L.append("")
                for x in b["fixes"]:
                    sc = f"（{x['scope']}）" if x["scope"] else ""
                    L.append(f"- {x['text']}{sc}　`{x['hash']}` {x['date'][:10]}")
                L.append("")
            if b["others"]:
                L.append("**其他改动**")
                L.append("")
                for x in b["others"]:
                    L.append(f"- [{x['kind']}] {x['text']}　`{x['hash']}`")
                L.append("")
    L.append("---")
    L.append("")
    L.append("## 功能点汇总")
    L.append("")
    seen = set()
    for r in rep["repos"]:
        for b in r.get("branches", []):
            for x in b["feats"]:
                if x["text"] in seen:
                    continue
                seen.add(x["text"])
                L.append(f"- {x['text']}")
    if not seen:
        L.append("- （本时段没有标记为 feat 的提交）")
    return "\n".join(L)


AI_PROMPT = """你在帮一位 iOS/物流系统开发者写周报。

下面是 git 提交记录的结构化摘要。请据此写一份**可以直接发给主管的周报**：

要求：
- 分「需求」组织，不要按提交罗列。一个分支通常就是一个需求。
- 每个需求下写：做了什么、解决了什么问题、涉及哪些模块
- 最后给一段「本周概览」，3 行以内
- **只依据给出的提交信息**，不要编造功能、不要夸大。信息不足就写"（提交信息未说明）"
- 中文，用 Markdown，简洁，不要客套话

提交记录：
"""


# ══════════════════════════════════════════════════════════════
# Web
# ══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gitreport/" + VERSION

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

    def _range(self, qs):
        """把 ?range=week|lastweek|month|days:N 换算成 since/until"""
        r = (qs.get("range", ["week"])[0] or "week")
        now = datetime.now()
        if r == "today":
            since = now.replace(hour=0, minute=0, second=0)
        elif r == "lastweek":
            monday = now - timedelta(days=now.weekday() + 7)
            since = monday.replace(hour=0, minute=0, second=0)
            now = monday + timedelta(days=7)
        elif r == "month":
            since = now - timedelta(days=30)
        elif r.startswith("days:"):
            since = now - timedelta(days=int(r.split(":")[1]))
        else:                                    # week
            since = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0)
        return since.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HOME, "index.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as exc:
                return self._send(500, f"<h1>{exc}</h1>", "text/html; charset=utf-8")

        if u.path == "/api/status":
            try:
                sys.path.insert(0, os.path.join(HOME, "..", "..", "lib"))
                import platform_lib
                ai_ok = platform_lib.ready()
                ai_model = platform_lib.config()["model"]
            except Exception:
                ai_ok, ai_model = False, ""
            c = cfg()
            return self._send(200, json.dumps({
                "version": VERSION, "repos": len(c["repos"]), "author": c.get("author", ""),
                "ai_ready": ai_ok, "ai_model": ai_model,
            }, ensure_ascii=False))

        if u.path == "/api/repos":
            c = cfg()
            out = []
            for p in c["repos"]:
                if is_repo(p):
                    out.append(repo_info(p))
                else:
                    out.append({"path": p, "name": os.path.basename(p), "broken": "不是 git 仓库"})
            return self._send(200, json.dumps({"repos": out}, ensure_ascii=False))

        if u.path == "/api/discover":
            found = discover_repos()
            c = cfg()
            new = [p for p in found if p not in c["repos"]]
            return self._send(200, json.dumps({"found": found, "new": new,
                                               "count": len(found)}, ensure_ascii=False))

        if u.path == "/api/report":
            since, until = self._range(qs)
            author = (qs.get("author", [""])[0] or cfg().get("author") or "").strip() or None
            repos = cfg()["repos"]
            if qs.get("repos"):
                repos = [p for p in qs["repos"][0].split("|") if p]
            rep = build_report(repos, since, until, author)
            md = report_markdown(rep)
            return self._send(200, json.dumps({"report": rep, "markdown": md},
                                              ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self._body()

        if u.path == "/api/config":
            c = cfg()
            if b.get("repos") is not None:
                c["repos"] = [p for p in b["repos"] if p]
            if b.get("author") is not None:
                c["author"] = b["author"]
            save_cfg(c)
            log(f"[config] 仓库 {len(c['repos'])} 个，提交人 {c.get('author') or '(不过滤)'}")
            return self._send(200, json.dumps({"ok": True, "repos": c["repos"]},
                                              ensure_ascii=False))

        if u.path == "/api/save":
            since, until = self._range({"range": [b.get("range", "week")]})
            repos = cfg()["repos"]
            rep = build_report(repos, since, until, cfg().get("author") or None)
            md = b.get("markdown") or report_markdown(rep)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = os.path.join(HOME, f"周报-{stamp}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(md)
            with open(REPORTS, "a", encoding="utf-8") as f:      # 只追加
                f.write(json.dumps({"ts": stamp, "range": [since, until],
                                    "commits": rep["total_commits"], "path": path},
                                   ensure_ascii=False) + "\n")
            log(f"[save] {rep['total_commits']} 次提交 → {path}")
            return self._send(200, json.dumps({"ok": True, "path": path,
                                               "commits": rep["total_commits"]},
                                              ensure_ascii=False))

        if u.path == "/api/polish":
            # AI 润色：流式返回，让用户看得见它在写
            since, until = self._range({"range": [b.get("range", "week")]})
            repos = b.get("repos") or cfg()["repos"]
            rep = build_report(repos, since, until, cfg().get("author") or None)
            if not rep["total_commits"]:
                return self._send(400, json.dumps(
                    {"error": "这个时间段没有提交，先换个范围"}, ensure_ascii=False))
            # 只把这批提交的要点喂给 AI，不喂完整 diff
            lines = []
            for r in rep["repos"]:
                if not r["count"]:
                    continue
                lines.append(f"\n## 仓库 {r['name']}（{r['count']} 次提交）")
                for br in r.get("branches", []):
                    lines.append(f"\n### 分支 {br['branch']}（{br['count']} 次）")
                    for x in (br["feats"] + br["fixes"] + br["others"])[:60]:
                        lines.append(f"- [{x['kind']}] {x['text']}  ({x['date'][:10]})")
            payload = AI_PROMPT + "\n".join(lines)

            try:
                import platform_lib
            except Exception as exc:
                return self._send(500, json.dumps({"error": f"载入 AI 能力失败：{exc}"},
                                                  ensure_ascii=False))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            log(f"[polish] 交给 AI，{rep['total_commits']} 次提交")

            def sse(t):
                try:
                    self.wfile.write(("data: %s\n\n" % json.dumps({"t": t},
                                                                  ensure_ascii=False)).encode())
                    self.wfile.flush()
                except Exception:
                    pass

            total = 0
            for piece in platform_lib.stream([{"role": "user", "content": payload}],
                                             temperature=0.4):
                total += len(piece)
                sse(piece)
            sse("\n\n<!-- done -->")
            log(f"[polish] AI 输出 {total} 字符")
            return

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
    c = cfg()
    if not c["repos"]:
        found = discover_repos()
        if found:
            c["repos"] = found
            save_cfg(c)
            log(f"[init] 自动发现 {len(found)} 个 git 仓库")
    log(f"Git 周报 v{VERSION} 已启动 http://127.0.0.1:{port}（{len(c['repos'])} 个仓库）")

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
    print(f"✅ Git 周报已启动（PID {running_pid()}） http://127.0.0.1:{port}"
          if running_pid() else "❌ 启动失败，看 gitreport.log")
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


def cmd_discover(args):
    found = discover_repos()
    print(f"发现 {len(found)} 个 git 仓库：")
    for p in found:
        i = repo_info(p)
        print(f"  {i['name']:<28} {i['branch']:<28} 最后提交 {i['last_commit']}")
    if args.save:
        c = cfg(); c["repos"] = found; save_cfg(c)
        print(f"✅ 已保存到配置")
    return 0


def cmd_report(args):
    c = cfg()
    repos = c["repos"] or discover_repos()
    now = datetime.now()
    if args.range == "week":
        since = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0)
    elif args.range == "lastweek":
        since = (now - timedelta(days=now.weekday() + 7)).replace(hour=0, minute=0, second=0)
    elif args.range == "month":
        since = now - timedelta(days=30)
    else:
        since = now - timedelta(days=int(args.range))
    rep = build_report(repos, since.strftime("%Y-%m-%d %H:%M:%S"),
                       now.strftime("%Y-%m-%d %H:%M:%S"), c.get("author") or args.author)
    md = report_markdown(rep)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"✅ 已写入 {args.out}（{rep['total_commits']} 次提交）")
    else:
        print(md)
    return 0


def main():
    import argparse
    p = argparse.ArgumentParser(prog="gitreport", description=f"Git 周报 v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_serve)
    sp = sub.add_parser("start"); sp.add_argument("--port", type=int); sp.set_defaults(f=cmd_start)
    sub.add_parser("stop").set_defaults(f=cmd_stop)
    sp = sub.add_parser("discover"); sp.add_argument("--save", action="store_true")
    sp.set_defaults(f=cmd_discover)
    sp = sub.add_parser("report")
    sp.add_argument("--range", default="week",
                    help="week / lastweek / month / 数字(天数)")
    sp.add_argument("--author"); sp.add_argument("--out")
    sp.set_defaults(f=cmd_report)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
