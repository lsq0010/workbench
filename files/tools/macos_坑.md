"""记录 macOS 系统层面的坑 —— 这些是踩出来的，不是查出来的。

写在代码注释里，因为它跟这个平台的功能无关，
是"在这台 Mac 上做自动化"本身的坑。
"""
LESSONS = """
macOS 自动化的坑（实测踩出来的）
════════════════════════════════════════════════════════════════

1. **绝不要 `killall Dock`**
   想重启 Dock 让配置生效时，很容易顺手写 `killall Dock`。
   实测后果：Dock 崩了两次（留下 Dock-*.ips 崩溃报告），而且**没能自己起来**。
   屏幕底部直接空了 —— 用户看到的就是"图标全没了"。
   更糟的是它连累了 Apps 启动器（macOS 26 里取代 Launchpad 的东西），
   之后点 Apps 一直没反应，重启 Dock、重启 Spotlight 都救不回来。

   正确做法：
       launchctl kickstart -k gui/$(id -u)/com.apple.Dock.agent

2. **`killall Spotlight` 同理**
   killall 之后 Spotlight 也没起来，`open -a Spotlight` 报
   POSIX error 162（Launchd job spawn failed）。
   正确做法：
       launchctl kickstart -k gui/$(id -u)/com.apple.Spotlight

   而且 Spotlight 挂了之后，Apps 启动器也一起废了 ——
   macOS 26 的 Apps 界面其实是 Spotlight 在画：
       点 Apps → 启动 Apps.app 存根（立刻退出）
              → Dock 给 Spotlight 发 .launchAppsBrowsing
              → Spotlight 显示界面

3. **桌面上的 .app 不能用符号链接**
   `os.symlink` 指到 ~/Applications 里的那份，命令行看一切正常
   （`mdls` 甚至认它是 com.apple.application-bundle），
   但 **Finder 桌面上就是不显示**。
   要用真实副本（shutil.copytree）。

4. **Dock 的常驻项只能改 plist**
   没有官方命令行。读 ~/Library/Preferences/com.apple.dock.plist，
   append 到 persistent-apps，再 kickstart Dock。
   改之前务必备份 —— 而且失败要能回滚。

5. **改系统设置前先问**
   这一整套折腾的起因只是"想让图标出现在 Dock 上"。
   动用户的 Dock / Spotlight / Finder 之前应该先说一声，
   而不是想当然地觉得"重启一下而已"。
"""

if __name__ == "__main__":
    print(LESSONS)
