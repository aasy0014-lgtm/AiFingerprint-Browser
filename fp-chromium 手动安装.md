# 📥 fp-chromium 内核 - 手动安装完整指南

由于自动下载遇到网络问题（GitHub API 限制），请按照以下步骤**手动下载安装**。

---

## ✅ 方法 A: GitHub 网页下载（最简单）

### 步骤 1: 访问 Release 页面

打开浏览器访问：
```
https://github.com/adryfish/fingerprint-chromium/releases
```

### 步骤 2: 选择最新版本

找到最新的 stable release（通常是第一个），点击下载对应的资产文件：

| 你的系统 | 需要下载的文件 |
|---------|--------------|
| **macOS (Apple Silicon)** M1/M2 | `fingerprint-chromium-macos-arm64.zip` ⬅️ 你的系统 |
| macOS (Intel) | `fingerprint-chromium-macos-x64.zip` |
| Windows (AMD64) | `fingerprint-chromium-windows-amd64.zip` |
| Linux (AMD64) | `fingerprint-chromium-linux-amd64.tar.gz` |

🔍 **提示**: 如果看不到 `arm64` 选项，尝试查看其他架构或使用通用版

### 步骤 3: 解压到项目目录

**macOS/Linux**:
```bash
cd /Users/admin/Documents/AiFingerprint-Browser/tools/fp-chromium

# 假设下载到了 ~/Downloads 目录
unzip ~/Downloads/fingerprint-chromium-macos-arm64.zip

# 确认内容
ls -la
# 应该看到 chrome 可执行文件或 Chrome.app
```

**Windows**:
```cmd
cd D:\AiFingerprint-Browser\tools\fp-chromium
tar -xf C:\Users\admin\Downloads\fingerprint-chromium-windows-amd64.zip
```

### 步骤 4: 设置环境变量

#### macOS / Linux (zsh)

编辑配置文件：
```bash
echo 'export FPWB_FPCHROMIUM=/Users/admin/Documents/AiFingerprint-Browser/tools/fp-chromium/chrome' >> ~/.zshrc
source ~/.zshrc
```

#### Windows PowerShell

```powershell
$env:FPWB_FPCHROMIUM="D:\AiFingerprint-Browser\tools\fp-chromium\chrome.exe"
[Environment]::SetEnvironmentVariable("FPWB_FPCHROMIUM", $env:FPWB_FPCHROMIUM, "User")
```

或在系统设置 → 环境变量中添加

### 步骤 5: 验证安装

**测试脚本 1 - Python 检查:**
```bash
python -c "from app.kernels.fp_chromium_kernel import is_available; ok, msg = is_available(); print('✅ OK' if ok else f'❌ {msg}')"
```

**测试脚本 2 - 移动端测试:**
```bash
python scripts/test_mobile_end_to_end.py
```

---

## 🔧 方法 B: 终端命令下载（如果 curl/wget 可用）

### 方案 1: 使用 GitHub API（推荐）

```bash
# 检测系统架构
if [[ "$(uname)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
    ASSET="fingerprint-chromium-macos-arm64.zip"
elif [[ "$(uname)" == "Darwin" && "$(uname -m)" == "x86_64" ]]; then
    ASSET="fingerprint-chromium-macos-x64.zip"
elif [[ "$(uname)" == "Linux" && "$(uname -m)" == "x86_64" ]]; then
    ASSET="fingerprint-chromium-linux-amd64.tar.gz"
else
    echo "不支持的系统，请手动下载"
    exit 1
fi

# 获取最新 TAG
TAG=$(curl -s https://api.github.com/repos/adryfish/fingerprint-chromium/releases/latest | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')

# 构建下载 URL
URL="https://github.com/adryfish/fingerprint-chromium/releases/download/${TAG}/${ASSET}"

# 下载并解压
cd tools/fp-chromium
curl -L -o "$ASSET" "$URL" || wget -O "$ASSET" "$URL"
unzip -q "$ASSET" 2>/dev/null || tar -xzf "$ASSET"
rm -f "$ASSET"
chmod +x chrome 2>/dev/null || true

# 设置环境变量
export FPWB_FPCHROMIUM="$(pwd)/chrome"
echo "✅ 安装完成！环境变量已设置"
echo "   永久生效：echo 'export FPWB_FPCHROMIUM=$(pwd)/chrome' >> ~/.zshrc"
```

### 方案 2: 直接使用镜像站（如果可用）

```bash
# 某些镜像站可能提供加速下载
cd tools/fp-chromium
curl -L -O https://ghproxy.com/https://github.com/adryfish/fingerprint-chromium/releases/latest/download/fingerprint-chromium-macos-arm64.zip
unzip fingerprint-chromium-macos-arm64.zip
rm fingerprint-chromium-macos-arm64.zip
chmod +x chrome
```

---

## ⚠️ 常见问题排查

### Q1: 下载链接返回 404

**原因**: GitHub 上的文件名可能与推测的不同

**解决**:
1. 打开 Release 页面直接查看实际文件名
2. 尝试不同的后缀名（.zip vs .tar.gz）
3. 手动下载所有可能的文件，逐一尝试解压

### Q2: 找不到 chrome 可执行文件

**检查解压后结构**:
```bash
cd /Users/admin/Documents/AiFingerprint-Browser/tools/fp-chromium
ls -laR

# 可能出现的文件结构：
# ├── chrome          ← Linux/macOS 直接可执行文件
# ├── Chrome.app/     ← macOS 应用包
# ├── Chrome.exe      ← Windows
# ├── README.md
# └── ...其他资源
```

**针对不同结构的 PATH 设置**:
```bash
# 情况 1: 直接有 chrome
export FPWB_FPCHROMIUM="/path/tools/fp-chromium/chrome"

# 情况 2: macOS 应用包
export FPWB_FPCHROMIUM="/path/tools/fp-chromium/Chrome.app/Contents/MacOS/Chrome"

# 情况 3: Windows
$env:FPWB_FPCHROMIUM="D:\path\tools\fp-chromium\chrome.exe"
```

### Q3: macOS Gatekeeper 阻止

**错误**: "无法打开，因为无法验证开发者"

**解决**:
```bash
# 方法 1: 移除隔离属性
sudo xattr -rd com.apple.quarantine /Users/admin/Documents/AiFingerprint-Browser/tools/fp-chromium/chrome

# 方法 2: 系统偏好设置
# System Settings → Privacy & Security → 在下方找到 blocked app → Unblock
```

### Q4: 权限不足

**解决**:
```bash
chmod +x /Users/admin/Documents/AiFingerprint-Browser/tools/fp-chromium/chrome
```

### Q5: "未找到 fp-chromium" 错误

**确保环境变量已加载**:
```bash
# 检查是否已设置
echo $FPWB_FPCHROMIUM
# 应该输出完整路径

# 如果没有，临时设置测试
export FPWB_FPCHROMIUM=/Users/admin/Documents/AiFingerprint-Browser/tools/fp-chromium/chrome
python scripts/test_mobile_end_to_end.py
```

---

## 🎯 确认安装成功

运行以下命令进行完整验证：

```bash
# 1. 基础功能测试
python scripts/mobile_test.py

# 2. 端到端测试（需已安装 fp-chromium）
python scripts/test_mobile_end_to_end.py

# 3. 启动服务测试
python run.py --port 18080
```

如果看到以下输出，说明安装成功：

```
=== Android 指纹测试 ===
[1] Samsung Galaxy S24 Ultra
  User-Agent: Mozilla/5.0 (Linux; Android 13; SM-S918B)...
  Screen: 1440x3120@30-bit
  GPU: Adreno 750
  Battery: {...}
  Health Score: 100/100
  
======================================================================
✅ 所有测试通过！
```

---

## 📞 仍无法解决？

如果以上方法都失败，请提供以下信息以便进一步诊断：

1. **操作系统版本**: `sw_verser` (macOS) 或 `uname -a` (Linux)
2. **错误日志**: 完整的错误信息
3. **网络状态**: 能否访问 https://github.com

---

## 📚 相关文档

- **完整安装指南**: file:///Users/admin/Documents/AiFingerprint-Browser/docs/fp-chromium%20安装指南.md
- **移动端实施报告**: file:///Users/admin/Documents/AiFingerprint-Browser/移动端增强_实施报告.md
- **官方仓库**: https://github.com/adryfish/fingerprint-chromium
