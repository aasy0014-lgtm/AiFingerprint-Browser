#!/bin/zsh
# 在 macOS 登录桌面会话的 Terminal.app 中启动管理端。
# 双击此文件比从受限自动化终端启动更可靠，浏览器可完成 macOS 应用注册。
set -e
cd "${0:A:h}"

if [[ ! -x .venv/bin/python ]]; then
  print -u2 "未找到 .venv/bin/python，请先完成项目安装。"
  read -k 1 "?按任意键退出..."
  exit 1
fi

# 清除下载/复制过程中附加的隔离属性；不修改项目文件或浏览器数据。
if [[ -d tools/fp-chromium/Chromium.app ]]; then
  xattr -rc tools/fp-chromium/Chromium.app 2>/dev/null || true
  # 更新当前用户的 LaunchServices 记录。安装/替换 .dmg 后旧记录可能仍指向
  # 已移走的 bundle，从而把 open 报成 kLSNoExecutableErr。
  _lsregister="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
  if [[ -x "$_lsregister" ]]; then
    "$_lsregister" -f "$PWD/tools/fp-chromium/Chromium.app" >/dev/null 2>&1 || true
  fi
fi

# 若 LaunchServices 不能从后台/IDE 进程打开 Chromium，内核会把命令
# 转交给当前登录用户的 Terminal.app；显式开启，避免被外部环境覆盖。
export FPWB_LAUNCH_TERMINAL=1
# 当前 macOS 的 LaunchServices 对复制的 Chromium.app 可能返回
# kLSNoExecutableErr；从登录桌面的 Terminal 直接启动 bundle 内二进制更可靠。
# 如需强制走 LaunchServices，可显式设置 FPWB_FPCHROMIUM_DIRECT=0。
export FPWB_FPCHROMIUM_DIRECT="${FPWB_FPCHROMIUM_DIRECT:-1}"

print "指纹浏览器工作台启动中：http://127.0.0.1:18080"
exec .venv/bin/python run.py --host 127.0.0.1 --port 18080
