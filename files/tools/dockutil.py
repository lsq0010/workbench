"""让安装时自动把「工作平台」固定到 Dock 上。

⚠️ 这里有个必须记住的坑
   重启 Dock **不能用 `killall Dock`**。
   实测：killall 之后 Dock 没能自己起来（留下两次崩溃报告 Dock-*.ips），
   屏幕底部直接空了 —— 用户看到的就是"图标没了"。
   正确做法是 `launchctl kickstart -k gui/<uid>/com.apple.Dock.agent`。

   所以这个脚本：
     · 先备份 Dock 的 plist
     · 只 append 一项，不动别人已有的
     · 用 launchctl 重启（不是 killall）
     · 起不来就用备份回滚
"""
import os
import plistlib
import shutil
import subprocess
import time
import urllib.parse

APP_NAME = "工作平台"


def _url_for(path):
    p = path if path.endswith("/") else path + "/"
    return "file://" + urllib.parse.quote(p, safe="/")


def _dock_running():
    return bool(subprocess.run(["pgrep", "-x", "Dock"],
                               capture_output=True, text=True).stdout.strip())


def _restart_dock():
    """重启 Dock —— 必须用 launchctl，不能用 killall Dock。

    killall 会让 Dock 起不来（实测踩过，留了崩溃报告）。
    """
    subprocess.run(["killall", "cfprefsd"], capture_output=True)
    time.sleep(0.8)
    subprocess.run(["launchctl", "kickstart", "-k",
                    "gui/%d/com.apple.Dock.agent" % os.getuid()],
                   capture_output=True)
    for _ in range(12):
        time.sleep(0.5)
        if _dock_running():
            return True
    return False


def add_to_dock(app_path, verbose=True):
    """把 app_path 固定到 Dock。返回 (成功, 说明)。"""
    def say(m):
        if verbose:
            print(m)

    app_path = os.path.abspath(app_path)
    if not os.path.isdir(app_path):
        return False, "找不到 %s" % app_path

    plist = os.path.expanduser("~/Library/Preferences/com.apple.dock.plist")
    if not os.path.isfile(plist):
        return False, "读不到 Dock 配置"

    backup = "/tmp/dock.before-workbench.plist"
    try:
        shutil.copy2(plist, backup)
    except Exception as exc:
        return False, "备份失败：%s" % exc

    try:
        with open(plist, "rb") as f:
            d = plistlib.load(f)
    except Exception as exc:
        return False, "Dock 配置读不了：%s" % exc

    apps = d.get("persistent-apps") or []
    want = _url_for(app_path)

    for a in apps:
        cur = ((a.get("tile-data") or {}).get("file-data") or {})
        if (cur.get("_CFURLString") or "").rstrip("/") == want.rstrip("/"):
            say("  ℹ️ 已经在 Dock 里了")
            return True, "已在 Dock"

    apps.append({
        "tile-type": "file-tile",
        "tile-data": {
            "file-data": {"_CFURLString": want, "_CFURLStringType": 15},
            "file-label": APP_NAME,
            "file-type": 41,
            "dock-extra": False,
            "is-beta": False,
        },
    })
    d["persistent-apps"] = apps
    try:
        with open(plist, "wb") as f:
            plistlib.dump(d, f)
    except Exception as exc:
        return False, "写 Dock 配置失败：%s" % exc

    if not _restart_dock():
        # 起不来就回滚，别让人家桌面底部空着
        try:
            shutil.copy2(backup, plist)
            _restart_dock()
        except Exception:
            pass
        return False, "Dock 重启失败，已回滚"

    return True, "已固定到 Dock"
