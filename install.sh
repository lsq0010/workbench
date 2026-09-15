#!/usr/bin/env bash
# 工作台 —— 一行命令安装
#
#   curl -fsSL https://cdn.jsdelivr.net/gh/lsq0010/workbench@main/install.sh | bash
#
# （别用 raw.githubusercontent.com —— 国内实测连不上）
#
# 做的事：
#   1. 检查系统（macOS + Python 3.7+）
#   2. 把代码下到 ~/工作平台
#   3. 把功能商店里的功能装起来
#   4. 缺 pillow 就装上（不装也能用，5 个画图功能会提示）
#   5. 在桌面上建一个「工作平台」图标
#   6. 启动
#
# 重复运行 = 升级（代码更新，你的数据不动）
set -euo pipefail

REPO="${WORKBENCH_REPO:-lsq0010/workbench}"
BRANCH="${WORKBENCH_BRANCH:-main}"
APP="$HOME/工作平台"
PORT="${WORKBENCH_PORT:-8880}"

# ── 输出 ────────────────────────────────────────────────────
if [ -t 1 ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
  B=""; DIM=""; G=""; Y=""; R=""; N=""
fi
say()  { printf "  %s\n" "$*"; }
ok()   { printf "  ${G}✅${N} %s\n" "$*"; }
warn() { printf "  ${Y}⚠️ ${N} %s\n" "$*"; }
die()  { printf "  ${R}❌${N} %s\n" "$*" >&2; exit 1; }
head_() { printf "\n  ${B}%s${N}\n" "$*"; }

printf "\n  ${B}工作台${N} ${DIM}本地个人工作平台${N}\n"
printf "  ${DIM}装到 %s${N}\n" "$APP"

# ── 1. 环境检查 ─────────────────────────────────────────────
head_ "① 检查环境"

[ "$(uname -s)" = "Darwin" ] || die "这个平台目前只支持 macOS（很多功能用了 macOS 自带的工具）"
ok "$(sw_vers -productVersion 2>/dev/null || echo macOS)"

PY=""
for c in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
  command -v "$c" >/dev/null 2>&1 || continue
  v=$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0)
  maj=${v%%.*}; min=${v##*.}
  if [ "$maj" -ge 3 ] && [ "$min" -ge 7 ]; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "需要 Python 3.7 以上。装一个：brew install python3"
ok "${PY}（$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')）"

command -v curl >/dev/null 2>&1 || die "需要 curl"
command -v tar  >/dev/null 2>&1 || die "需要 tar"

# 端口占没占
# 端口检查：**要分清"是工作台"还是"别的程序"**。
# 很多功能也有 /api/status，光看它响应会误判（我就把 textkit 认成工作台了）。
# 工作台有个别处没有的 /api/desktop，用它对暗号。
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  if curl -s -m 3 "http://127.0.0.1:$PORT/api/desktop" 2>/dev/null | grep -q '"items"'; then
    warn "端口 $PORT 上已经有一个工作台在跑 —— 这次会更新它的代码，然后重启"
  else
    who="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -1 | awk '{print $1}')"
    die "端口 $PORT 被「${who}」占了。换个端口：WORKBENCH_PORT=8899 bash install.sh"
  fi
fi

# ── 2. 下载代码 ─────────────────────────────────────────────
head_ "② 下载代码"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
TARBALL="$TMP/wb.tar.gz"

# 本地模式：WORKBENCH_LOCAL=/path/to/workbench bash install.sh
# （用来在推 GitHub 之前先测一遍，也给没网的人用）
if [ -n "${WORKBENCH_LOCAL:-}" ]; then
  [ -d "$WORKBENCH_LOCAL/files" ] || die "WORKBENCH_LOCAL 里没有 files/：$WORKBENCH_LOCAL"
  SRC="$WORKBENCH_LOCAL"
  ok "本地模式：用 $SRC"
else

# 几个源挨个试 —— 国内 raw.githubusercontent 经常连不上
SOURCES=(
  "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
  "https://cdn.jsdelivr.net/gh/$REPO@$BRANCH/archive.tar.gz"
  "https://gh-proxy.com/https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
)

got=0
for url in "${SOURCES[@]}"; do
  host="$(printf '%s' "$url" | sed -E 's|https?://([^/]+).*|\1|')"
  printf "  ${DIM}试 %s …${N}" "$host"
  if curl -fsSL -m 120 -o "$TARBALL" "$url" 2>/dev/null && \
     tar tzf "$TARBALL" >/dev/null 2>&1; then
    printf "\r  ${G}✅${N} 从 %s 下载完成（%s）\n" "$host" \
      "$(du -h "$TARBALL" | awk '{print $1}')"
    got=1
    break
  fi
  printf "\r  ${DIM}%s 不通${N}\n" "$host"
done
[ "$got" = "1" ] || die "所有下载源都不通。检查网络，或手动下载后本地安装（见 README）"

tar xzf "$TARBALL" -C "$TMP"
SRC="$(find "$TMP" -maxdepth 1 -type d -name 'workbench-*' | head -1)"
[ -n "$SRC" ] || SRC="$(find "$TMP" -maxdepth 1 -type d ! -path "$TMP" | head -1)"
[ -d "$SRC/files" ] || die "下载的包里没有 files/ 目录，可能仓库结构不对"
ok "解压完成"
fi

# ── 3. 装到 ~/工作平台 ──────────────────────────────────────
head_ "③ 安装"

FRESH=1
[ -f "$APP/platform.py" ] && FRESH=0

mkdir -p "$APP"
# 代码覆盖，数据保留：
# platform.py / lib / web / store 是代码 —— 覆盖
# registry.json / desktop.json / identity.json / flows.jsonl 是数据 —— 绝不动
for item in platform.py healthcheck.py README.md lib web store; do
  [ -e "$SRC/files/$item" ] || continue
  rm -rf "$APP/$item.new"
  cp -R "$SRC/files/$item" "$APP/$item.new"
  rm -rf "$APP/$item"
  mv "$APP/$item.new" "$APP/$item"
done
ok "代码已$( [ "$FRESH" = "1" ] && echo 安装 || echo 更新 )到 $APP"

# ── 4. 依赖 ─────────────────────────────────────────────────
head_ "④ 检查依赖"

if "$PY" -c 'import PIL' >/dev/null 2>&1; then
  ok "pillow 已有（画图、二维码、条码、截图这几个功能要用）"
else
  say "缺 pillow（5 个画图功能需要）。正在装…"
  if "$PY" -m pip install --user --quiet pillow >/dev/null 2>&1 && \
     "$PY" -c 'import PIL' >/dev/null 2>&1; then
    ok "pillow 装好了"
  else
    warn "pillow 没装上。平台能正常用，但这几个功能会提示缺东西："
    warn "  二维码工具 / 条形码工具 / 图片工具箱 / 上架截图工坊 / 上架材料检查"
    warn "  想补装：$PY -m pip install --user pillow"
  fi
fi

# 可选工具，缺了只是对应功能不能用
for c in xcodebuild git; do
  command -v "$c" >/dev/null 2>&1 && ok "$c 有" || warn "$c 没有（相关功能会提示）"
done

# ── 5. 初始化 ───────────────────────────────────────────────
head_ "⑤ 初始化"

cd "$APP"
[ -f registry.json ] || echo '{"version":1,"features":[],"ignored":[]}' > registry.json
[ -d features ] || mkdir -p features

# 把商店里的功能装起来（新装才有；升级时保留你现在的状态）
if [ "$FRESH" = "1" ]; then
  n=0
  for d in store/*/; do
    [ -d "$d" ] || continue
    id="$(basename "$d")"
    [ -f "$d/manifest.json" ] || continue
    cp -R "$d" "features/$id" 2>/dev/null && n=$((n+1))
  done
  ok "从商店装了 $n 个功能"
else
  # 升级：补上新功能，已有的不动
  n=0
  for d in store/*/; do
    [ -d "$d" ] || continue
    id="$(basename "$d")"
    [ -f "$d/manifest.json" ] || continue
    [ -d "features/$id" ] && continue
    cp -R "$d" "features/$id" 2>/dev/null && n=$((n+1))
  done
  [ "$n" -gt 0 ] && ok "商店里有 $n 个新功能，已补上" || ok "功能都是最新的"
fi

# ── 6. 桌面图标 ─────────────────────────────────────────────
head_ "⑥ 建桌面图标"

LAUNCH="$APP/工作平台.app"
mkdir -p "$LAUNCH/Contents/MacOS"
cat > "$LAUNCH/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>工作平台</string>
  <key>CFBundleDisplayName</key><string>工作平台</string>
  <key>CFBundleIdentifier</key><string>local.workbench.launcher</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
  <key>LSUIElement</key><false/>
</dict></plist>
PLIST

cat > "$LAUNCH/Contents/MacOS/launch" <<SH
#!/bin/bash
# 工作平台启动器
cd "$APP" || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:\$PATH"

# 已经在跑就只开浏览器
if curl -s -m 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
  open "http://127.0.0.1:$PORT/"
  exit 0
fi

echo "正在启动工作平台…"
"$PY" platform.py start --port $PORT >/dev/null 2>&1

# 等它起来（最多 20 秒）
for i in \$(seq 1 40); do
  sleep 0.5
  if curl -s -m 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
    echo "正在拉起所有功能…"
    "$PY" platform.py start-all >/dev/null 2>&1
    open "http://127.0.0.1:$PORT/"
    exit 0
  fi
done
echo "启动失败。看日志：$APP/platform.log"
read -r -p "按回车关掉…" _
SH
chmod +x "$LAUNCH/Contents/MacOS/launch"
ok "桌面图标：$LAUNCH"

# ── 7. 启动 ─────────────────────────────────────────────────
head_ "⑦ 启动"

"$PY" platform.py start --port "$PORT" >/dev/null 2>&1 || true
for i in $(seq 1 40); do
  sleep 0.5
  curl -s -m 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1 && break
done

if curl -s -m 3 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
  "$PY" platform.py start-all >/dev/null 2>&1 &
  ok "工作台已启动"
  say ""
  say "  ${B}打开：${N}http://127.0.0.1:$PORT/"
  say "  ${DIM}正在拉起所有功能，大概十几秒。稍等再打开也行。${N}"
  say ""
  say "  ${DIM}以后启动：双击桌面上的「工作平台」${N}"
  say "  ${DIM}停止：$PY $APP/platform.py stop-all${N}"
  open "http://127.0.0.1:$PORT/" 2>/dev/null || true
else
  warn "启动了但没响应。看看日志：tail -20 $APP/platform.log"
  exit 1
fi

head_ "装好了"
say "  如果 AI 功能要配 key：打开工作台 → 设置 → AI 配置"
say "  有什么问题看 README：$APP/README.md"
say ""
