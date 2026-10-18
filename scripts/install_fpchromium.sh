#!/bin/bash
# fp-chromium 内核安装脚本
# - macOS (Apple Silicon)：Release 仅提供 .dmg，自动挂载并提取 Chromium.app
# - Linux (x86_64)：tar.xz
# - Windows (Git Bash / MSYS)：zip（解压出 chrome.exe）
#
# 安装位置：<项目>/tools/fp-chromium/（内核代码会自动发现，无需环境变量）

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS_DIR="$PROJECT_ROOT/tools/fp-chromium"
API="https://api.github.com/repos/adryfish/fingerprint-chromium/releases?per_page=30"

echo "========================================="
echo "fp-chromium 内核安装脚本"
echo "========================================="

# 1. 检测系统，确定资产后缀
UNAME_S="$(uname -s)"
UNAME_M="$(uname -m)"
case "$UNAME_S" in
    Darwin)
        if [ "$UNAME_M" != "arm64" ]; then
            echo "❌ fp-chromium 的 macOS 构建为 Apple Silicon (arm64) 专用，Intel Mac 不可用"
            exit 1
        fi
        ASSET_SUFFIX="_macos.dmg"
        echo "✅ macOS ARM (Apple Silicon)"
        ;;
    Linux)
        if [ "$UNAME_M" != "x86_64" ]; then
            echo "❌ 仅支持 Linux x86_64（当前：$UNAME_M）"
            exit 1
        fi
        ASSET_SUFFIX="_x86_64_linux.tar.xz"
        echo "✅ Linux AMD64"
        ;;
    MINGW*|MSYS*|CYGWIN*)
        ASSET_SUFFIX="_windows_x64.zip"
        echo "✅ Windows (Git Bash)"
        ;;
    *)
        echo "❌ 不支持的系统：$UNAME_S（请手动从 GitHub Releases 下载）"
        exit 1
        ;;
esac

# 2. 选择版本：优先取最新版，最新版缺对应资产时回退到最近一个有资产的版本
echo ""
echo "🔎 查询可用版本（最新版可能未提供 macOS 构建，会自动回退）..."
RESPONSE="$(curl -sf --max-time 30 "$API" || true)"
if [ -z "$RESPONSE" ]; then
    echo "❌ 无法访问 GitHub API，请手动下载后解压到：$TOOLS_DIR"
    exit 1
fi

# 逐行找出第一（最新）个匹配资产的下载地址
DOWNLOAD_URL="$(echo "$RESPONSE" \
    | grep -o '"browser_download_url": *"[^"]*'"$ASSET_SUFFIX"'"' \
    | head -n 1 | sed -E 's/.*"(https[^"]+)".*/\1/')"

if [ -z "$DOWNLOAD_URL" ]; then
    echo "❌ 未找到匹配资产（$ASSET_SUFFIX）"
    echo "   请手动访问：https://github.com/adryfish/fingerprint-chromium/releases"
    exit 1
fi
echo "   下载地址：$DOWNLOAD_URL"

# 3. 下载
mkdir -p "$TOOLS_DIR"
cd "$TOOLS_DIR"
FILE_NAME="$(basename "$DOWNLOAD_URL")"
echo ""
echo "⬇️  下载 $FILE_NAME（约 130~190 MB）..."
curl -L --fail --connect-timeout 15 -o "$FILE_NAME" "$DOWNLOAD_URL"

# 4. 安装
echo ""
case "$UNAME_S" in
    Darwin)
        echo "📦 挂载 dmg 并提取 Chromium.app..."
        MOUNT_POINT="$(mktemp -d /tmp/fpchromium_dmg.XXXXXX)"
        hdiutil attach -nobrowse -readonly -mountpoint "$MOUNT_POINT" "$FILE_NAME" -quiet
        APP_SRC="$(find "$MOUNT_POINT" -maxdepth 2 -name "*.app" | head -n 1)"
        if [ -z "$APP_SRC" ]; then
            hdiutil detach "$MOUNT_POINT" -quiet || true
            echo "❌ dmg 中未找到 .app"
            exit 1
        fi
        APP_NAME="$(basename "$APP_SRC")"
        rm -rf "$TOOLS_DIR/$APP_NAME"
        ditto "$APP_SRC" "$TOOLS_DIR/$APP_NAME"
        hdiutil detach "$MOUNT_POINT" -quiet || true
        rmdir "$MOUNT_POINT" 2>/dev/null || true
        # 清除 Gatekeeper 隔离属性
        xattr -rd com.apple.quarantine "$TOOLS_DIR/$APP_NAME" 2>/dev/null || true
        EXE_PATH="$TOOLS_DIR/$APP_NAME/Contents/MacOS/Chromium"
        ;;
    Linux)
        echo "📦 解压 tar.xz..."
        tar -xJf "$FILE_NAME"
        chmod +x chrome 2>/dev/null || true
        EXE_PATH="$TOOLS_DIR/chrome"
        ;;
    *)
        echo "📦 解压 zip..."
        unzip -q -o "$FILE_NAME"
        EXE_PATH="$TOOLS_DIR/chrome.exe"
        ;;
esac

# 5. 清理与验证
rm -f "$TOOLS_DIR/$FILE_NAME"

echo ""
if [ ! -f "$EXE_PATH" ]; then
    # Linux/Windows 解压后可能嵌套子目录
    EXE_PATH="$(find "$TOOLS_DIR" \( -name "chrome" -o -name "chrome.exe" \) -type f 2>/dev/null | head -n 1)"
fi

if [ -n "$EXE_PATH" ] && [ -f "$EXE_PATH" ]; then
    echo "========================================="
    echo "🎉 安装成功！"
    echo "========================================="
    VERSION_OUT="$("$EXE_PATH" --version 2>/dev/null | head -n 1 || true)"
    echo "   可执行文件：$EXE_PATH"
    echo "   版本：${VERSION_OUT:-（无法启动，请检查系统安全设置）}"
    echo ""
    echo "下一步："
    echo "   1. 启动服务：python run.py（macOS/Linux 用 .venv/bin/python）"
    echo "   2. 校验安装：python -c \"from app.kernels.fp_chromium_kernel import is_available; print(is_available())\""
    echo "   3. 创建环境时选择内核 fp-chromium（移动端 Android/iOS 必须用它）"
else
    echo "❌ 安装失败：未找到 chrome 可执行文件"
    echo "   请检查 $TOOLS_DIR 目录内容，或手动安装（docs/fp-chromium 安装指南.md）"
    exit 1
fi
