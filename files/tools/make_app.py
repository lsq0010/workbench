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
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
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
    launch = """#!/bin/bash
# 工作平台 —— 双击启动
#   1. 平台没跑就拉起来（顺便拉起所有功能）
#   2. 用 Chrome 的应用模式打开（没有地址栏，像个原生 App）
set -u

WORKBENCH="%s"
PY="%s"
PORT=%d
URL="%s"
CHROME="%s"
LOG="$WORKBENCH/launcher.log"

say() { echo "$@" | tee -a "$LOG" >/dev/null; }

mkdir -p "$(dirname "$LOG")"
say ""
say "  [$(date '+%%H:%%M:%%S')] 启动工作平台…"

# ── 已经在跑就只开窗口 ──
if curl -s -m 2 "$URL/api/desktop" 2>/dev/null | grep -q '"items"'; then
  say "  已经在跑，直接打开"
else
  cd "$WORKBENCH" || { osascript -e 'display alert "工作平台" message "找不到目录：'"$WORKBENCH"'"'; exit 1; }
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
  exec "$CHROME" \\
    --app="$URL" \\
    --user-data-dir="$HOME/Library/Application Support/工作平台/chrome" \\
    --no-first-run --no-default-browser-check \\
    --window-size=1440,960
else
  open "$URL"
fi
""" % (workbench_home, python_bin, port, url, chrome or "")

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

    # 桌面上放一个（软链即可，改一处两边都生效）
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
            if os.path.islink(desk) or os.path.exists(desk):
                os.remove(desk)
            os.symlink(app_dir, desk)
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
