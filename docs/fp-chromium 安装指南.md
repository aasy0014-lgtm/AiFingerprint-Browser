# fp-chromium 内核安装指南

## 📥 方式一：手动下载安装（推荐）

### 步骤 1: 从 GitHub Releases 下载

访问官方发布页面：
```
https://github.com/adryfish/fingerprint-chromium/releases
```

选择适合你系统的版本下载：

| 操作系统 | 架构 | 下载文件名 |
|---------|------|-----------|
| macOS   | Apple Silicon (M 系列) | `ungoogled-chromium_<版本>_macos.dmg`（仅此一种，无 zip） |
| Windows | AMD64                | `ungoogled-chromium_<版本>_windows_x64.zip` |
| Linux   | AMD64                | `ungoogled-chromium-<版本>-x86_64_linux.tar.xz` |

> ⚠️ 注意：
> - **最新 Release 可能没有 macOS 构建**（例如 150 只有 Win/Linux），
>   需要往前找最近一个带 `.dmg` 的版本（目前 macOS 最新为 148.0.7778.215）
> - macOS 构建为 **Apple Silicon (arm64) 专用**，Intel Mac 无法运行
> - 版本号与 Chrome 大版本对应（132~150）

### 步骤 2: 解压到项目目录

以 **macOS ARM** 为例（dmg 需先挂载再复制）：

```bash
cd /Users/admin/Documents/AiFingerprint-Browser/tools/fp-chromium

# 挂载 dmg
hdiutil attach -nobrowse -readonly ~/Downloads/ungoogled-chromium_148.0.7778.215-1.1_macos.dmg

# 把 Chromium.app 复制进项目（ditto 保留签名与权限）
ditto "/Volumes/Chromium/Chromium.app" ./Chromium.app

# 卸载 dmg 并清除 Gatekeeper 隔离属性
hdiutil detach /Volumes/Chromium
xattr -rd com.apple.quarantine ./Chromium.app
```

Linux（tar.xz）：`tar -xJf ~/Downloads/ungoogled-chromium-*-x86_64_linux.tar.xz`
Windows（zip）：直接把 `chrome.exe` 解压到 `tools\fp-chromium\`

### 步骤 3: 确认 Chrome 可执行文件位置

```text
tools/fp-chromium/
├── Chromium.app/                  ← macOS（dmg 提取）
│   └── Contents/MacOS/Chromium    ← 真实可执行文件（内核自动发现它）
├── chrome                         ← Linux 可执行文件
├── chrome.exe                     ← Windows 可执行文件
└── README.md
```

> macOS 上**不要**用符号链接（如 `ln -s ... chrome`）指过去：
> 经符号链接启动时 dyld 会按链接路径解析框架导致启动失败，
> 内核已直接支持发现 `Chromium.app` 内的真实二进制。

### 步骤 4: 环境变量（可选）

内核按以下顺序自动查找：`FPWB_FPCHROMIUM` 环境变量 → `tools/fp-chromium/` 目录
（含 `chrome` / `chrome.exe` / `Chromium.app`）。**放进项目目录即可，无需配置环境变量**；
只有当可执行文件在别处时才需要设置：

#### macOS / Linux

```bash
export FPWB_FPCHROMIUM=/Users/admin/Documents/AiFingerprint-Browser/tools/fp-chromium/Chromium.app/Contents/MacOS/Chromium
```

#### Windows

```powershell
$env:FPWB_FPCHROMIUM="D:\AiFingerprint-Browser\tools\fp-chromium\chrome.exe"
[Environment]::SetEnvironmentVariable("FPWB_FPCHROMIUM", $env:FPWB_FPCHROMIUM, "User")
```

---

## 🔧 方式二：使用自动化脚本（推荐，网络可达时）

项目已内置安装脚本 `scripts/install_fpchromium.sh`，自动完成：系统/架构检测 →
选择带对应资产的版本（最新 Release 缺资产时自动回退）→ 下载 → 安装
（macOS 自动挂载 dmg 提取 Chromium.app 并清除隔离属性）→ 版本验证。

```bash
# macOS / Linux（Windows 用户请在 Git Bash 中运行）
cd /Users/admin/Documents/AiFingerprint-Browser
bash scripts/install_fpchromium.sh

# 或先检查脚本会做什么
bash -n scripts/install_fpchromium.sh
```

脚本支持的平台与资产：

| 平台 | 资产 | 说明 |
|------|------|------|
| macOS ARM | `*_macos.dmg` | 自动挂载提取 `Chromium.app` |
| Linux x86_64 | `*_x86_64_linux.tar.xz` | 解压出 `chrome` |
| Windows (Git Bash) | `*_windows_x64.zip` | 解压出 `chrome.exe` |

---

## ✅ 验证安装

安装完成后（放进 `tools/fp-chromium/` 即可，无需环境变量）：

```bash
python -c "from app.kernels.fp_chromium_kernel import is_available; print(is_available())"
# 未安装：(False, '未安装。从 github.com/adryfish/... 下载解压后，...')
# 已安装：(True, '/path/to/tools/fp-chromium/Chromium.app/Contents/MacOS/Chromium')
```

或使用提供的验证脚本（真实启动浏览器并逐项核对移动端仿真）：

```bash
# 内核协议/指纹基础验证（需服务运行在 127.0.0.1:18080）
python scripts/fpchromium_verify.py

# 移动端真实启动验证（独立临时目录，自包含）
python scripts/mobile_launch_verify.py android
python scripts/mobile_launch_verify.py ios
```

---

## 🐛 常见问题

### Q1: 下载速度慢或超时

**解决方案**:
- 使用国内镜像站点（如果有）
- 通过手机热点下载
- 直接从 GitHub 网页点击下载链接

### Q2: 找不到 chrome 可执行文件

**原因**: 解压后路径不对或权限问题

**解决**:
```bash
cd tools/fp-chromium
ls -la              # 确认文件存在
chmod +x chrome     # 添加执行权限
file chrome         # 确认是有效的二进制文件
```

### Q3: macOS Gatekeeper 阻止运行

**错误信息**: "无法打开，因为无法验证开发者"

**解决**:
```bash
# 方法 1: 系统偏好设置
/System/Applications/System Preferences.dmg → Security & Privacy → General → Unblock

# 方法 2: 命令行
sudo xattr -rd com.apple.quarantine /path/to/tools/fp-chromium/chrome
```

### Q4: fp-chromium 版本过新/过旧

**建议**: 
- 使用最新的 stable release
- 如果遇到兼容性问题，尝试指定版本下载

```
https://github.com/adryfish/fingerprint-chromium/releases/tag/v148.0.0
```

---

## 🎯 移动端测试流程

安装完成后：

1. **启动服务**:
   ```bash
   python run.py --port 18080
   ```

2. **创建 Android 环境** (API):
   ```bash
   curl -X POST http://127.0.0.1:18080/api/v1/profiles \
     -H "Content-Type: application/json" \
     -H "X-API-Key: <你的 API Key>" \
     -d '{
       "name": "Galaxy S24 测试",
       "target_os": "android",
       "kernel": "fp-chromium",
       "fingerprint_mode": "generate",
       "launch": {"geoip": true, "humanize": true}
     }'
   ```

3. **启动环境**:
   ```bash
   curl -X POST http://127.0.0.1:18080/api/v1/browser/start \
     -d '{"profile_id": "<上一步返回的 id>"}'
   ```

4. **连接 Playwright**:
   ```python
   from playwright.async_api import async_playwright
   
   async def test():
       async with async_playwright() as p:
           browser = await p.chromium.connect_over_cdp("<ws_endpoint>")
           page = await browser.new_page()
           
           result = await page.evaluate("""
               () => ({
                   mobile: navigator.mobile,
                   maxTouchPoints: navigator.maxTouchPoints,
                   userAgent: navigator.userAgent.substring(0, 80),
                   platform: navigator.platform,
                   devicePixelRatio: window.devicePixelRatio,
               })
           """)
           print(result)
   
   import asyncio
   asyncio.run(test())
   ```

---

## 📚 参考资料

- **官方仓库**: https://github.com/adryfish/fingerprint-chromium
- **项目文档**: https://github.com/adryfish/fingerprint-chromium#readme
- **移动端增强总结**: file:///Users/admin/Documents/AiFingerprint-Browser/移动端增强总结.md
- **实施报告**: file:///Users/admin/Documents/AiFingerprint-Browser/移动端增强_实施报告.md

---

**最后更新**: 2026-10-18  
**状态**: 等待用户手动下载（网络限制）
