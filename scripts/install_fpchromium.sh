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
REQUESTED_VERSION="${FPWB_FPCHROMIUM_VERSION:-}"
REPOSITORY="${FPWB_FPCHROMIUM_REPO:-adryfish/fingerprint-chromium}"
EXPECTED_SHA256="${FPWB_FPCHROMIUM_SHA256:-}"
LOCAL_ASSET="${FPWB_FPCHROMIUM_ASSET:-}"

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
        ASSET_PATTERN='(_macos\.dmg|arm64-macos[^\"]*\.dmg)'
        echo "✅ macOS ARM (Apple Silicon)"
        ;;
    Linux)
        if [ "$UNAME_M" != "x86_64" ]; then
            echo "❌ 仅支持 Linux x86_64（当前：$UNAME_M）"
            exit 1
        fi
        ASSET_PATTERN='_x86_64_linux\.tar\.xz'
        echo "✅ Linux AMD64"
        ;;
    MINGW*|MSYS*|CYGWIN*)
        ASSET_PATTERN='_windows_x64\.zip'
        echo "✅ Windows (Git Bash)"
        ;;
    *)
        echo "❌ 不支持的系统：$UNAME_S（请手动从 GitHub Releases 下载）"
        exit 1
        ;;
esac

# 2. 选择版本：优先取最新版，最新版缺对应资产时回退到最近一个有资产的版本
echo ""
if [ -n "$REQUESTED_VERSION" ]; then
    echo "🔎 使用指定版本：$REQUESTED_VERSION"
    API="https://api.github.com/repos/$REPOSITORY/releases/tags/$REQUESTED_VERSION"
else
    echo "🔎 查询可用版本（最新版可能未提供 macOS 构建，会自动回退）..."
    API="https://api.github.com/repos/$REPOSITORY/releases?per_page=30"
fi
RESPONSE="$(curl -sf --max-time 30 "$API" || true)"
if [ -z "$RESPONSE" ]; then
    echo "❌ 无法访问 GitHub API，请手动下载后解压到：$TOOLS_DIR"
    exit 1
fi

# 逐行找出第一（最新）个匹配资产的下载地址
DOWNLOAD_URL="$(echo "$RESPONSE" \
    | grep -o '"browser_download_url": *"[^"]*"' \
    | sed -E 's/.*"(https[^\"]+)".*/\1/' \
    | grep -E "$ASSET_PATTERN" \
    | head -n 1)"

if [ -z "$DOWNLOAD_URL" ]; then
    echo "❌ 未找到匹配资产（$ASSET_PATTERN${REQUESTED_VERSION:+，版本 $REQUESTED_VERSION}）"
    echo "   请手动访问：https://github.com/adryfish/fingerprint-chromium/releases"
    exit 1
fi
echo "   下载地址：$DOWNLOAD_URL"

# 3. 下载
mkdir -p "$TOOLS_DIR"
cd "$TOOLS_DIR"
if [ -n "$LOCAL_ASSET" ]; then
    if [ ! -f "$LOCAL_ASSET" ]; then
        echo "❌ 本地资产不存在：$LOCAL_ASSET"
        exit 1
    fi
    FILE_NAME="$(basename "$LOCAL_ASSET")"
    cp "$LOCAL_ASSET" "$TOOLS_DIR/$FILE_NAME"
    echo "   使用本地资产：$LOCAL_ASSET"
else
    FILE_NAME="$(basename "$DOWNLOAD_URL")"
fi
echo ""
if [ -z "$LOCAL_ASSET" ]; then
    echo "⬇️  下载 $FILE_NAME（约 130~190 MB）..."
    curl -L --fail --connect-timeout 15 -o "$FILE_NAME" "$DOWNLOAD_URL"
fi
if [ -n "$EXPECTED_SHA256" ]; then
    ACTUAL_SHA256="$(shasum -a 256 "$FILE_NAME" | awk '{print $1}')"
    if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
        echo "❌ SHA-256 校验失败：$ACTUAL_SHA256"
        rm -f "$FILE_NAME"
        exit 1
    fi
    echo "✅ SHA-256 校验通过"
fi

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
        if [ -e "$TOOLS_DIR/$APP_NAME" ]; then
            # Keep backups out of the *.app namespace.  LaunchServices scans
            # sibling bundles and two Chromium.app instances with the same
            # bundle identifier can make `open` resolve the wrong (stale)
            # executable and report kLSNoExecutableErr.
            BACKUP="$TOOLS_DIR/${APP_NAME%.app}.bak.$(date +%Y%m%d%H%M%S).disabled"
            echo "📦 备份现有内核：$BACKUP"
            mv "$TOOLS_DIR/$APP_NAME" "$BACKUP"
        fi
        ditto "$APP_SRC" "$TOOLS_DIR/$APP_NAME"
        hdiutil detach "$MOUNT_POINT" -quiet || true
        rmdir "$MOUNT_POINT" 2>/dev/null || true
        # 清除 Gatekeeper 隔离属性
        xattr -rd com.apple.quarantine "$TOOLS_DIR/$APP_NAME" 2>/dev/null || true
        # 与 /Applications 中可能存在的旧 Chromium.app 隔离。旧版包常带有
        # 相同的 org.chromium.Chromium Bundle ID；LaunchServices 选中旧记录后
        # 会把新包误报为 kLSNoExecutableErr。工作台使用自己的 Bundle ID，
        # 不影响 Chromium Framework/Helper 的内部标识。
        if command -v plutil >/dev/null 2>&1; then
            plutil -replace CFBundleIdentifier -string com.aifingerprint.fpchromium \
                "$TOOLS_DIR/$APP_NAME/Contents/Info.plist"
            plutil -replace CFBundleName -string "AiFingerprint Chromium" \
                "$TOOLS_DIR/$APP_NAME/Contents/Info.plist"
            plutil -replace CFBundleDisplayName -string "AiFingerprint Chromium" \
                "$TOOLS_DIR/$APP_NAME/Contents/Info.plist" 2>/dev/null || true
        fi
        if command -v codesign >/dev/null 2>&1; then
            codesign --force --deep --sign - "$TOOLS_DIR/$APP_NAME"
        fi
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
