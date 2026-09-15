#!/usr/bin/env python3
"""把工作平台做成一个正经的 macOS App（带图标，像 DeepSeek Harness 那样）。

参考的是 DSH 的做法
  查了机器上 DSH 的图标（~/Applications/Chrome Apps.localized/DeepSeek Harness.app）：
    CFBundleExecutable = app_mode_loader     ← Chrome 的应用模式加载器
    CrAppModeShortcutURL = http://127.0.0.1:3080/
  也就是**用 Chrome 的 app 模式打开网页** —— 没有地址栏、没有标签页，
  看起来就是个原生 App。

我这里不用 Chrome 的 PWA 机制（那要靠用户在浏览器里点"安装应用"，
没法自动化），而是自己写个 .app：
  · 可执行文件是个 shell 脚本
  · 先确保平台在跑（没跑就拉起来）
  · 再用 Chrome --app= 打开（效果一样，但没有地址栏）
  · 找不到 Chrome 就退回默认浏览器

产物
  ~/Applications/工作平台.app      主图标（和 DSH 放一起）
  ~/Desktop/工作平台.app           桌面快捷方式（软链）
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

def _protect_vars(script):
    """把 bash 里 `$VAR` 后面紧跟非 ASCII 字符的地方改成 `${VAR}`。

    坑的现场：
        say "…（$WORKBENCH，端口 $PORT）"
    bash 的变量名允许的字节范围比想象中宽，全角逗号「，」的 UTF-8 字节
    会被当成变量名的一部分 → 报 `WORKBENCH?: unbound variable`。
    这个坑只在中文环境才踩得到，而且报错信息里的变量名是乱码，很难认。

    实测踩过两次：install.sh 的 `$PY（`、launch 脚本的 `$WORKBENCH，`。
    """
    return re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])",
                  r"${\1}", script)


APP_NAME = "工作平台"
BUNDLE_ID = "local.workbench.app"

# 找 Chrome（优先，因为有 app 模式）；没有就退回 Safari
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Arc.app/Contents/MacOS/Arc",
]


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def build(app_dir, workbench_home, python_bin, port, icon_path):
    """建 .app 包"""
    if os.path.exists(app_dir):
        shutil.rmtree(app_dir)
    macos = os.path.join(app_dir, "Contents", "MacOS")
    res = os.path.join(app_dir, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(res)

    # 图标
    if os.path.isfile(icon_path):
        shutil.copy2(icon_path, os.path.join(res, "app.icns"))
        has_icon = True
    else:
        has_icon = False

    chrome = find_chrome()
    url = "http://127.0.0.1:%d/" % port

    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>%s</string>
  <key>CFBundleDisplayName</key><string>%s</string>
  <key>CFBundleIdentifier</key><string>%s</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>app%s</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSUIElement</key><false/>
</dict></plist>
""" % (APP_NAME, APP_NAME, BUNDLE_ID, ".icns" if has_icon else "")

    with open(os.path.join(app_dir, "Contents", "Info.plist"), "w",
              encoding="utf-8") as f:
        f.write(plist)

    # 启动脚本
    #
    # 关键设计：**平台在哪不写死**，而是先读配置文件、找不到就自己搜。
    # 这样搬家、换端口之后图标照样能用。
    launch = r"""#!/bin/bash
# 工作平台 —— 双击启动
#   1. 找到平台在哪（配置 → 搜索 → 放弃）
#   2. 没跑就拉起来，顺便拉起所有功能
#   3. 用 Chrome 的应用模式打开（没有地址栏，像个原生 App）
set -u

APP_SUPPORT="$HOME/Library/Application Support/工作平台"
CONF="$APP_SUPPORT/app.json"
CHROME_DEFAULT="__CHROME__"

say() { [ -n "${LOG:-}" ] && echo "$@" | tee -a "$LOG" >/dev/null || echo "$@"; }

# ── 从配置文件里读一个字段（没有 python 也能读，纯 sed）─────
conf_get() {
  [ -f "$CONF" ] || return 1
  sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONF" | head -1
}

# ── 找一个能用的平台目录 ─────────────────────────────────
find_home() {
  # ① 配置文件里记的
  local h
  h="$(conf_get home || true)"
  [ -n "$h" ] && [ -f "$h/platform.py" ] && { echo "$h"; return 0; }
  # ② 常见位置（按可能性排）
  for d in "$HOME/工作平台" "$HOME/Desktop/工作平台" \
           "$HOME/Documents/工作平台" "/Applications/工作平台"; do
    [ -f "$d/platform.py" ] && { echo "$d"; return 0; }
  done
  # ③ 全盘碰运气（限深度，别把硬盘扫穿）
  h="$(find "$HOME" -maxdepth 3 -name platform.py -path "*工作平台*" 2>/dev/null | head -1)"
  [ -n "$h" ] && { echo "$(dirname "$h")"; return 0; }
  return 1
}

# ── 找一个能用的 python ──────────────────────────────────
find_py() {
  local p
  p="$(conf_get python || true)"
  [ -n "$p" ] && [ -x "$p" ] && { echo "$p"; return 0; }
  for p in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    [ -x "$p" ] && { echo "$p"; return 0; }
  done
  command -v python3 2>/dev/null && return 0
  return 1
}

WORKBENCH="$(find_home || true)"
PY="$(find_py || true)"

# ── 找不到就弹个能照着做的提示，而不是干瞪眼 ──────────────
if [ -z "$WORKBENCH" ] || [ -z "$PY" ]; then
  osascript -e 'display alert "找不到工作平台" message "在下面这些位置都没找到 platform.py：
    ~/工作平台
    ~/Desktop/工作平台
    ~/Documents/工作平台

如果平台被挪到别处了，改一下这个文件就行：
    '"$CONF"'

（把 home 改成平台所在的目录）" as critical'
  exit 1
fi

PORT="$(conf_get port || true)"
[ -n "$PORT" ] || PORT=__PORT__
URL="http://127.0.0.1:$PORT/"
CHROME="$(conf_get chrome || true)"
[ -n "$CHROME" ] || CHROME="$CHROME_DEFAULT"
LOG="$WORKBENCH/launcher.log"

# ── 把这次找到的写回配置（下次更快，也修复搬过家的）────────
mkdir -p "$APP_SUPPORT"
cat > "$CONF" <<JSON
{
  "home": "$WORKBENCH",
  "python": "$PY",
  "port": $PORT,
  "chrome": "$CHROME"
}
JSON

say ""
say "  [$(date '+%H:%M:%S')] 启动工作平台…（$WORKBENCH，端口 $PORT）"

# ── 已经有这个应用的窗口吗 ──
# Chrome 的 --app 模式有个坑：同一个 user-data-dir 已经开着窗口时，
# 再执行一次同样的命令**什么都不做**（不新开、不报错、也不保证切到前台）。
# 用户看到的就是"点了没反应"。实测连点两次，窗口数一直是 1。
#
# 所以：有旧窗口就先关掉，再开一个新的 —— 保证点了就一定看到。
# 工作台的状态都在服务端，关窗口不丢任何东西。
APPWIN="$(pgrep -f "user-data-dir=$APP_SUPPORT/chrome" 2>/dev/null | head -5)"
if [ -n "$APPWIN" ]; then
  say "  关掉旧窗口重开（Chrome 不会自己新开）"
  # shellcheck disable=SC2086
  kill $APPWIN 2>/dev/null
  for _ in $(seq 1 20); do
    sleep 0.25
    pgrep -f "user-data-dir=$APP_SUPPORT/chrome" >/dev/null 2>&1 || break
  done
fi

# ── 平台没跑就拉起来 ──
if curl -s -m 2 "$URL/api/desktop" 2>/dev/null | grep -q '"items"'; then
  say "  平台已经在跑"
else
  cd "$WORKBENCH" || exit 1
  "$PY" platform.py start --port "$PORT" >>"$LOG" 2>&1
  for i in $(seq 1 40); do
    sleep 0.5
    curl -s -m 2 "$URL/api/status" >/dev/null 2>&1 && break
  done
  if ! curl -s -m 3 "$URL/api/status" >/dev/null 2>&1; then
    osascript -e 'display alert "工作平台启动失败" message "看日志：'"$LOG"'"'
    exit 1
  fi
  say "  平台起来了，正在拉起功能…"
  nohup "$PY" platform.py start-all >>"$LOG" 2>&1 &
fi

# ── 开窗口 ──
if [ -n "$CHROME" ] && [ -x "$CHROME" ]; then
  # 应用模式：没有地址栏、没有标签页
  # 单独一个 user-data-dir，免得和你日常的 Chrome 窗口混在一起
  exec "$CHROME" \
    --app="$URL" \
    --user-data-dir="$APP_SUPPORT/chrome" \
    --no-first-run --no-default-browser-check \
    --window-size=1440,960
else
  open "$URL"
fi
"""
    launch = (launch.replace("__PORT__", str(port))
                    .replace("__CHROME__", chrome or ""))
    # 统一保护一遍：$VAR 后面跟中文标点会被 bash 当成变量名的一部分
    launch = _protect_vars(launch)

    exe = os.path.join(macos, "launch")
    with open(exe, "w", encoding="utf-8") as f:
        f.write(launch)
    os.chmod(exe, 0o755)
    return has_icon, chrome


def refresh_icon_cache(app_dir):
    """让 Finder/Dock 立刻认到新图标（不然可能显示成白纸）"""
    try:
        subprocess.run(["touch", app_dir], capture_output=True)
        # 清这个 app 的图标缓存
        subprocess.run(["qlmanage", "-r", "cache"], capture_output=True, timeout=20)
    except Exception:
        pass


def install(workbench_home, python_bin, port, icon_path=None, desktop_link=True):
    """装到 ~/Applications 并在桌面放个入口"""
    icon_path = icon_path or os.path.join(HERE, APP_NAME + ".icns")
    apps = os.path.expanduser("~/Applications")
    os.makedirs(apps, exist_ok=True)
    app_dir = os.path.join(apps, APP_NAME + ".app")

    has_icon, chrome = build(app_dir, workbench_home, python_bin, port, icon_path)
    refresh_icon_cache(app_dir)

    result = {"app": app_dir, "icon": has_icon, "chrome": chrome}

    # 桌面上放一个 —— **必须是真实副本，不能用软链**。
    # 踩过的坑：一开始用 os.symlink 指到 ~/Applications 里的那份，
    # 命令行看一切正常（macOS 甚至认它是 application-bundle），
    # 但 Finder 桌面上就是不显示这个图标。用真实副本才稳。
    if desktop_link:
        desk_dir = os.path.expanduser("~/Desktop")
        # 桌面目录可能不存在（新用户、或者被改过位置）—— 建一个，别静默失败
        if not os.path.isdir(desk_dir):
            try:
                os.makedirs(desk_dir, exist_ok=True)
            except Exception as exc:
                result["desktop_error"] = "桌面目录建不了：%s" % exc
                return result
        desk = os.path.join(desk_dir, APP_NAME + ".app")
        try:
            if os.path.islink(desk):
                os.remove(desk)              # 之前留下的软链，清掉
            elif os.path.exists(desk):
                shutil.rmtree(desk, ignore_errors=True)
            # 复制而不是链接（copytree 不跟软链）
            shutil.copytree(app_dir, desk, symlinks=False)
            result["desktop"] = desk
        except Exception as exc:
            result["desktop_error"] = str(exc)

    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="把工作平台做成 macOS App")
    ap.add_argument("--home", default=os.path.expanduser("~/工作平台"))
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--port", type=int, default=8880)
    ap.add_argument("--icon", default=None)
    ap.add_argument("--no-desktop", action="store_true")
    ap.add_argument("--dock", action="store_true",
                    help="顺便固定到 Dock（会重启 Dock）")
    a = ap.parse_args()

    r = install(a.home, a.python, a.port, a.icon, not a.no_desktop)
    print("  ✅ App:    %s" % r["app"])
    print("  %s 图标:   %s" % ("✅" if r["icon"] else "⚠️ 没有", "app.icns"))
    print("  ✅ 用这个打开: %s" % (os.path.basename(r["chrome"]) if r["chrome"]
                                  else "默认浏览器（没找到 Chrome）"))
    if r.get("desktop"):
        print("  ✅ 桌面:   %s" % r["desktop"])
    if r.get("desktop_error"):
        print("  ⚠️ 桌面入口建不了：%s" % r["desktop_error"])

    if a.dock:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import dockutil
            good, msg = dockutil.add_to_dock(r["app"])
            print("  %s Dock: %s" % ("✅" if good else "⚠️", msg))
        except Exception as exc:
            print("  ⚠️ Dock 固定失败：%s" % exc)
