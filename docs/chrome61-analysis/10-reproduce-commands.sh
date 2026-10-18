#!/bin/bash
# ==============================================================================
# Chrome-v61-3163-73.ipa 复现命令集
# 用途: 复现 docs/Chrome61-IPA检测机制分析.md 的全部结论
# 环境: macOS + Xcode 命令行工具 (otool/codesign/plutil/strings/file/unzip)
# ==============================================================================
set -x
IPA=/Users/admin/Downloads/Chrome-v61-3163-73.ipa
WORK="${TMPDIR:-/tmp}/chrome61-analysis"

# --- 0. 校验和 ---------------------------------------------------------------
shasum -a 256 "$IPA"

# --- 1. 解包 -----------------------------------------------------------------
mkdir -p "$WORK" && cd "$WORK"
unzip -o -q "$IPA"
APP="$WORK/Payload/stable.app"

# --- 2. 身份与元数据 ----------------------------------------------------------
plutil -p "$APP/Info.plist"
plutil -p "$WORK/iTunesMetadata.plist"
plutil -p "$APP/SafeModeCrashingModules.plist"        # ★ Safe Mode 黑名单
plutil -p "$APP/zh_CN.lproj/Localizable.strings"      # ★ Safe Mode 文案
plutil -p "$APP/zh_CN.lproj/InfoPlist.strings"        # 权限用途说明
plutil -p "$APP/gm-config/ANY/OmniboxGeolocation.plist"

# --- 3. 签名 / 加密 -----------------------------------------------------------
codesign -dvvv "$APP"
codesign -d --entitlements - "$APP"
codesign -d --entitlements - "$APP/PlugIns/share_extension.appex"
codesign -d --entitlements - "$APP/PlugIns/today_extension.appex"
otool -f  "$APP/Chrome"                         # fat 头
otool -hv "$APP/Chrome"                         # Mach-O 头
otool -l "$APP/Chrome" | grep -A5 LC_ENCRYPTION_INFO   # FairPlay 加密段
otool -L "$APP/Chrome"                          # 链接框架(无第三方 dylib)

# --- 4. 用户脚本（注入网页的 JS） ---------------------------------------------
ls -la "$APP"/*.js
cat "$APP/web_bundle.js"      # window.__gCrWeb
cat "$APP/nav_bundle.js"      # history.* 改写
cat "$APP/window_id.js"       # __gCrWeb.windowId
grep -o 'command:"[^"]*"' "$APP"/*.js | sort -u

# --- 5. WebUI 资源（.pak 为 UTF-8 明文; 可用自写 pak_dump.py 解包） -----------
file "$APP/resources.pak" "$APP/en.lproj/locale.pak"
strings -a "$APP/resources.pak" | grep -i "chrome://" | sort -u
# pak v4 头: [4B version=4][4B count][1B encoding][ (2B id, 4B offset) x count ]

# --- 6. 未加密导入符号（__LINKEDIT 尾部；__TEXT 全部被 FairPlay 加密） --------
python3 - <<'EOF'
data = open('Payload/stable.app/Chrome','rb').read()
v7  = data[16384+22102016 : 16384+25032016]     # armv7 未加密尾部
a64 = data[25051136+26771456 : 25051136+30517296]  # arm64 未加密尾部
open('v7_tail.bin','wb').write(v7); open('a64_tail.bin','wb').write(a64)
EOF
strings -a -n 6 a64_tail.bin | sort -u > a64_symbols.txt
grep -E '^@?_?dyld_|ASIdentifier|CNCopy|getifaddrs|sysctl|CLLocation|CBCentral' a64_symbols.txt
grep 'OBJC_CLASS_\$_' a64_symbols.txt | sed 's/.*\$//' | sort -u   # 260 个类

# --- 7. 结论 -----------------------------------------------------------------
# 若需要进一步分析（应用逻辑字符串、UA 构造、IDFA 用途）:
#   需在越狱设备上用 frida-ios-dump / bagbak 解密后重复步骤 4-6。
