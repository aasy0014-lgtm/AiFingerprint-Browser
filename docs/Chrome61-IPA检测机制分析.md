# Chrome for iOS 61.0.3163.73（App Store IPA）检测机制分析报告

| 项目 | 内容 |
|---|---|
| 分析对象 | `/Users/admin/Downloads/Chrome-v61-3163-73.ipa` |
| 文件大小 | 67,814,248 字节 |
| SHA-256 | `5f9011796284092acfc271f6e429005daa19fb517ec4370663e68413a83a041c` |
| 样本性质 | Google 官方 App Store 版 Chrome for iOS（stable 渠道），**未修改** |
| 分析方法 | 静态分析（签名/DRM/Info.plist/权限/资源包/注入脚本/导入符号） |
| 分析日期 | 2026-10-18 |
| 证据存档 | [`docs/chrome61-analysis/`](chrome61-analysis/00-README.md)（11 份证据文件 + `chromium-src/` 源码快照） |
| 结论可信度 | 文件级证据与源码级交叉验证（第 9 节）均为**已确认**；仅加密段内部逻辑保留**推断**标注 |

---

## 摘要

1. **样本即官方原包**：Apple 商店签名 + FairPlay DRM 完整、无注入动态库、原始 zip 元数据齐全；由共享 Apple ID（`cards666911@gmail.com`）于 2017-09-15 购得。它**不含任何第三方改动**，也**不含指纹伪装功能**。
2. **最直接的"检测机制"是 Safe Mode（安全模式）**：Chrome iOS 会枚举进程内加载的 Mach-O 镜像（dyld API），识别 **MobileSubstrate 越狱插件**（如已内置黑名单 `/Library/MobileSubstrate/DynamicLibraries/CDM.dylib`），在启动崩溃时进行归因，并以"请卸载这些插件"提示用户（含中文文案）。它用于故障排查，**不是拒绝运行型反越狱检测**。触发条件已由源码确认：**连续 ≥2 次启动失败** 且存在建议（检测到插件，或存在 >1 份待上传崩溃报告）（§9.1）。
3. **Google 侧遥测面完整**：Breakpad 崩溃上报（`clients2.google.com/cr/report`）、使用统计开关、Google 账号 SSO/同步、APNs 推送、Safe Browsing、语音搜索、位置、BLE 信标（Physical Web）、Cast 局域网发现、企业设备策略等。源码级确认：上传的 minidump **包含全部已加载镜像的完整路径**（越狱插件名单会随之出站），并附 `metrics_client_id`、`variations` 等崩溃键（§9.2）。
4. **对网页暴露的 JS 检测面很强**：16 个注入脚本在**页面世界**留下 `window.__gCrWeb`（每窗口唯一 ID）、被改写的 `history.*`/`print`/`scrollTo`、`PaymentRequest` polyfill 等；普通网页**没有 `window.chrome`**；UA 暴露 `CriOS/61.0.3163.73` 而引擎是系统 WebKit —— 版本错配本身就是指纹。
5. **对本项目的意义**：此包不能直接改造（FairPlay 绑定原 Apple ID，需越狱解密后才能重签），且 iOS Chrome = WKWebView 壳，深指纹全在系统 WebKit —— 桌面端 CDP 注入路线不可复用；真机指纹伪装必须走「越狱 + WebKit/JS 层 hook」或「自研 WKWebView 壳」。报告中给出了可直接用于自测的检测代码片段（第 8.2 节）。
6. **第二轮补充（源码级交叉验证）**：以样本内置的 Chromium commit（`0cb96822…`）为锚，读取同版官方源码与 DEPS 指定的 Breakpad 源码，逐项核验 Safe Mode、崩溃上报内容、UA 构造、消息通道等，把第一轮的多项「推断」升级为「源码级已确认」，并修正 2 处表述（§9 汇总，原始对照见证据 `11-chromium-source-cross-reference.md`）。

---

## 目录

- [1. 分析目标与方法](#1-分析目标与方法)
- [2. 样本身份与完整性验证](#2-样本身份与完整性验证)
- [3. 包结构总览](#3-包结构总览)
- [4. 检测机制逐项分析](#4-检测机制逐项分析)
  - [4.1 越狱/注入检测：Safe Mode（★核心发现）](#41-越狱注入检测safe-mode核心发现)
  - [4.2 Google 侧识别与遥测](#42-google-侧识别与遥测)
  - [4.3 设备信息采集能力](#43-设备信息采集能力api-层面)
  - [4.4 网页侧 JS 检测面](#44-网页侧-js-检测面注入脚本)
  - [4.5 WebUI 内部页面](#45-webui-内部页面chrome)
  - [4.6 网络行为与端点](#46-网络行为与端点)
  - [4.7 应用互操作探测](#47-应用互操作探测canopenurl--handoff--sso)
- [5. 反篡改与 DRM 分析](#5-反篡改与-drm-分析)
- [6. 检测面总结矩阵](#6-检测面总结矩阵)
- [7. 深度分析路线图](#7-深度分析路线图)
- [8. 对本项目（AiFingerprint-Browser）的结论与建议](#8-对本项目aifingerprint-browser的结论与建议)
- [9. 源码级交叉验证（第二轮补充）](#9-源码级交叉验证第二轮补充)
- [附录 A 证据文件索引](#附录-a-证据文件索引)
- [附录 B 术语表](#附录-b-术语表)
- [附录 C 分析局限](#附录-c-分析局限)

---

## 1. 分析目标与方法

### 1.1 目标

回答三个问题：

1. 这个 IPA 里**是否存在检测机制**？具体有哪些？
2. 这些检测机制**检测什么、为谁检测、强度如何**？
3. 对该样本进行二次开发（注入 hook / 重签名 / 云手机部署）时，**哪些机制会造成影响**？

### 1.2 方法与环境

| 步骤 | 工具 | 产出 |
|---|---|---|
| 解包与清单 | `unzip` / `ls` / `du` | 文件清单、结构 |
| 身份与配置 | `plutil` | Info.plist、entitlements、各类 plist |
| 签名与 DRM | `codesign` / `otool` | 签名链、CDHash、FairPlay 加密段 |
| 资源提取 | 自写 PAK v4 解析器 + `strings` | WebUI 页面（`resources.pak`）、文案（`locale.pak`） |
| 脚本审计 | 直接阅读 | 16 个注入网页的用户脚本 |
| 符号级证据 | 解析 `__LINKEDIT` 未加密尾部 | 导入符号 ~1181 个、ObjC 类 260 个 |

### 1.3 分析边界（重要）

- 主二进制 `__TEXT` 段被 **FairPlay 整体加密**（`cryptid=1`，两架构均加密），应用逻辑字符串、方法名不可直接读取。
- 因此本报告会把结论分为两类：
  - **已确认**：来自包内明文文件（plist / 资源 / 注入脚本 / 未加密符号表）；
  - **推断**：基于 Chromium 公开架构或符号能力推断，标注「推断」。
- 需要拿到加密段内容时，必须先在越狱设备上解密（见第 7 节）。

---

## 2. 样本身份与完整性验证

### 2.1 身份

| 项 | 值 |
|---|---|
| Bundle ID | `com.google.chrome.ios` |
| 版本 | 61.0.3163.73（stable 渠道，目录名 `stable.app`） |
| 源码版本 | `0cb96822e59630c82a826186a57b88d10d998e88-refs/branch-heads/3163@{#1020}` |
| 构建环境 | Xcode 8.2.1 / iOS SDK 10.2 / macOS 16G29 |
| 最低系统 | iOS 9.0；架构 armv7 + arm64（iPhone/iPad） |
| 扩展 | `com.google.chrome.ios.ShareExtension`、`com.google.chrome.ios.TodayExtension` |

### 2.2 签名与 DRM

| 项 | 值 | 说明 |
|---|---|---|
| 签名主体 | `Apple iPhone OS Application Signing` → Apple CA → Apple Root CA | **官方商店分发签名**，非开发/企业/第三方证书 |
| Team ID | `EQHXZ8M8AV` | Google 的 Apple 开发团队 |
| CDHash (sha256) | `03561c09c75863058094152081e1fa6cbe73d555` | |
| 资源封条 | `version=2 rules=15 files=1282` | 1282 个文件被签名覆盖 |
| FairPlay | `SC_Info/Chrome.sinf/.supp/.supf/.supx` + `LC_ENCRYPTION_INFO[_64]` | `cryptid=1`，`__TEXT` 全加密 |
| 注入 dylib | **无** | `otool -L` 仅系统框架 |
| 描述文件 | **无** `embedded.mobileprovision` | App Store 原生包特征 |

### 2.3 购买来源

`iTunesMetadata.plist`（Apple 打包时写入，第三方重打包通常不会保留）：

| 字段 | 值 |
|---|---|
| Apple ID | `cards666911@gmail.com` |
| 用户 | Artur Bondarenko |
| 购买日期 | 2017-09-15 08:04:57Z |
| 商店区域 | 143492-2,32（俄罗斯区） |
| itemId | 535886823（Google Chrome） |

> 该元数据 + 原始 zip 结构（`META-INF/com.apple.ZipMetadata.plist` 等）表明：这是从 App Store 下载的原版 IPA（典型"共享账号下载"来源），不是任何厂商的"改装 Chrome"。

### 2.4 篡改检查清单

| 检查项 | 结果 | 结论 |
|---|---|---|
| 第三方 dylib 注入 | 无 | 通过 |
| 二进制是否被替换（签名一致性） | CodeDirectory/CMSDigest 完整，Apple 链 | 通过 |
| 是否重签（开发/企业证书） | 否，Apple 商店证书 | 通过 |
| 是否夹带描述文件/附加脚本 | 无 | 通过 |
| zip 元数据是否被二次打包 | 保留 Apple 原始元数据 | 通过 |
| 扩展是否被替换 | 两个扩展均为 Apple 签名原版 | 通过 |

**判定：官方原版，未篡改。**

---

## 3. 包结构总览

```
Payload/stable.app/
├── Chrome                        55,568,432 B  通用二进制（FairPlay 加密）
├── Info.plist                    8,795 B       Breakpad/SSO/URL Schemes/权限
├── SafeModeCrashingModules.plist 413 B         ★ 启动崩溃模块黑名单（越狱插件）
├── gm-config/ANY/                2 个配置      OmniboxGeolocation / 语音语言映射
├── icudtl.dat                    6.6 MB        ICU 语言数据
├── resources.pak                 3.2 MB        WebUI 资源（chrome:// 页面）
├── *.js ×16                                    注入网页的用户脚本
├── *.lproj ×40                   40 种语言      locale.pak + Localizable.strings
├── SC_Info/                      FairPlay      DRM 封装
├── _CodeSignature/               CodeResources（1282 文件封条）
├── DeviceManagement.bundle       企业设备策略（Google Device Policy）
├── SSOAuth.bundle                Google 账号 SSO 登录
├── UserDataControls.bundle       活动记录/位置记录开关（跳 Google 页）
├── AccountSettings*.bundle       账号设置
├── FeedbackKit*.bundle           用户反馈
├── GoogleS3KitVoiceSearch.bundle 语音搜索
├── GoogleCastResources.bundle    Chromecast
├── GSKResources.bundle           语言映射资源
├── GoogleKit*.bundle ×8          设置类 UI 组件
├── Material*.bundle ×10          Material 图标/字体
├── QuantumCollections.bundle     书签集合 UI
└── PlugIns/
    ├── share_extension.appex     分享扩展
    └── today_extension.appex     Today 小组件
```

> 顶层共 153 个条目；签名封条覆盖 1282 个文件；解包后 85 MB。
> 完整清单见证据 `07-file-inventory.txt`。

---

## 4. 检测机制逐项分析

### 机制分类框架

```
谁在检测？
├── A. App 自身（对越狱/注入环境）      → 4.1 Safe Mode
├── B. App 对 Google（遥测/识别）       → 4.2 + 4.3 + 4.6
├── C. 网站/风控对浏览器（JS 指纹）      → 4.4 + 4.5
└── D. OS 对 App（DRM/签名/entitlement） → 5
```

---

### 4.1 越狱/注入检测：Safe Mode（★核心发现）

**这是包内唯一一个"对本地环境做检测"的机制。**

#### 证据链

| # | 证据 | 位置 |
|---|---|---|
| 1 | 黑名单文件内含 MobileSubstrate 路径 | `SafeModeCrashingModules.plist` |
| 2 | 7 条 Safe Mode UI 文案（"部分插件导致 Chrome 崩溃，请卸载这些插件："） | `zh_CN.lproj/Localizable.strings` 等 40 语言 |
| 3 | 枚举已加载镜像的 dyld API | 未加密导入符号：`_dyld_image_count`、`_dyld_get_image_name`、`_dyld_get_image_header`、`_dyld_get_image_vmaddr_slide` |
| 4 | 提示页由 WebUI 渲染（同文案出现在 locale.pak 资源 01538/01539） | `resources` + `locale.pak` |
| 5 | Safe Mode 触发/扫描实现的官方源码 | 证据 `11-chromium-source-cross-reference.md` §1 + `chromium-src/` 快照 |

黑名单文件全文（证据 `08-safe-mode-jailbreak-detection.txt`）：

```plist
"StartupCrashModules" => {
  "/Library/MobileSubstrate/DynamicLibraries/CDM.dylib" => {
    "ModuleFriendlyName" => "Chrome Download Manager"
  }
}
```

`CDM.dylib` 是面向越狱 iOS 的 Chrome 下载管理插件（中文社群常用）。
Google 将其内置为"已知启动崩溃模块"，说明该机制在真实世界已在运转。

#### 工作机制（已由源码级确认，详见 §9.1）

1. 启动过程记录启动失败计数（NSUserDefaults：`AppStartupFailureCount`）；**仅当计数 ≥ 2 且"存在建议"** 时进入 Safe Mode（`SafeModeCoordinator.shouldStart`）。
2. "存在建议"判定之一：用 `_dyld_image_count()` / `_dyld_get_image_name()` 枚举全部已加载镜像，并按前缀过滤 `"/Library/MobileSubstrate/DynamicLibraries/"`（`strncmp`）——即"进程内是否加载了 MobileSubstrate 插件"；另一判定：上传已开启且积压崩溃报告 > 1 份（`GetCrashReportCount() > 1`）。
3. 与 `SafeModeCrashingModules.plist` 对照：命中已知模块列`ModuleFriendlyName`；未命中已知名单则显示通用插件文案；两者皆无则显示"未知原因"文案。
4. 渲染 Safe Mode 页（"Aw, Snap!" + 说明 +「重试」/「发送崩溃报告」）；「重试」→ `RestoreDefaultConfiguration()` 重置崩溃报告配置并重新进入浏览器。
5. 崩溃报告（Breakpad minidump）**确实包含全部已加载模块的完整路径**（§9.2 源码级确认）→ 注入 dylib 的路径会随报告发往 `clients2.google.com`（条件：发生崩溃且用户开启上报；Safe Mode 恢复模式会立即强制上传并标记 `uploaded_in_recovery_mode=yes`）。

#### 强度与影响评估

| 维度 | 评估 |
|---|---|
| 是否阻止 tweak 注入 | **否**。不做 ptrace/反调试，不检测 Cydia，不拒绝启动。 |
| 检测范围 | 进程内非常规路径的加载镜像（MobileSubstrate dylib 是主要目标） |
| 触发条件 | **连续 ≥2 次启动失败**且（检测到插件 或 有待上传崩溃报告）时才显示；正常运行不打扰用户（§9.1） |
| 对二次开发的意义 | ① 用 MobileSubstrate 注入会暴露在"插件列表"；② 若引发启动崩溃则被记录；③ 崩溃报告会把模块名单带回 Google（侧信道风险）——**③ 已源码级确认**（§9.2）。 |
| 规避提示 | 使用不写入 `DynamicLibraries` 的注入方式（如 Frida、DYLD_INSERT 由自有 app 启动、直接二进制 patch/framework 替换）可以不出现在该列表中；但要注意一旦崩溃，归因逻辑仍会枚举全部非系统镜像。 |

---

### 4.2 Google 侧识别与遥测

| # | 机制 | 证据（已确认） | 说明 |
|---|---|---|---|
| 1 | **Breakpad 崩溃上报** | Info.plist：`BreakpadURL=https://clients2.google.com/cr/report`、`BreakpadProduct=Chrome_iOS`、`BreakpadReportInterval=3600`、`BreakpadSendAndExit=YES` | 崩溃堆栈 + 系统/设备/版本元数据 + **全部已加载镜像的完整路径** + 运行上下文（`metrics_client_id`/`variations`/后台·内存·登录·播放状态）——源码级确认，见 §9.2 |
| 2 | **崩溃本地记录** | WebUI `chrome://crashes`："Local Crash ID"、"Crash report uploaded on …" | 可查看/补传 |
| 3 | **使用统计（UMA）** | 文案："Help make Chrome better by sending usage statistics and crash reports to Google." / "将使用情况统计信息和崩溃报告自动发送给 Google" | 行为直方图上传 |
| 4 | **Google 账号 SSO** | `SSOAuth.bundle`；OAuth Client ID `77185425430-…`；URL Scheme `com.google.sso.chrome.stable`；keychain 组 `com.google.common.SSO` | 登录态跨 Google App 共享 |
| 5 | **同步（Sync）** | WebUI `chrome://sync-internals`（about/data/events/types）；`AccountSettings` 文案 | 书签/历史/密码等与账号绑定 |
| 6 | **登录调试页** | `chrome://signin-internals` | |
| 7 | **APNs 推送** | Entitlement `aps-environment=production` | Google 可主动推送 |
| 8 | **GCM 推送调试** | WebUI "GCM Internals"（`chrome://gcm-internals`） | 推送通道的调试面板 |
| 9 | **Safe Browsing** | 资源包内含 `chrome://safebrowsing`（实验/首选项面板）与 `chrome://interstitials`（ssl/phishing/malware/captive/blocked 插页）页面资源；但 iOS WebUI 注册表未注册这些页面（§9.5） | 对 Google 校验 URL 与下载（校验在原生层执行；页面资源为跨平台共享） |
| 10 | **语音搜索（音频上传）** | `GoogleS3KitVoiceSearch.bundle`；麦克风权限文案 | 音频送 Google 识别 |
| 11 | **位置** | `CLLocationManager` 类引用；位置权限文案；`gm-config` 中 Omnibox 地理化域名表（49 个 google.* 域） | 网页地理定位 + Omnibox 本地化 |
| 12 | **BLE 信标扫描（Physical Web）** | `CBCentralManager` + `chrome://physical-web`；蓝牙文案："这样可让您找到与目前周遭环境相关的网页。" | 扫描周边信标获取上下文 |
| 13 | **Cast 局域网发现** | GCK* 类（`GCKDeviceScanner` 等）；`cast_sender_iframe.js` | mDNS/局域网设备发现 |
| 14 | **用户反馈** | `FeedbackKit*`（含反馈提示 UI） | 用户主动上报日志/截图/URL |
| 15 | **企业设备策略** | `DeviceManagement.bundle`：Google Device Policy 集成文案 | 托管设备上下文 |
| 16 | **活动记录开关页** | `UserDataControls.bundle`：Activity controls、Google Location History、Account history | 跳转 Google 网页管理 |

### 4.3 设备信息采集能力（API 层面）

以下能力由未加密符号表证实（应用**具备调用能力**；是否实际调用需解密后确认）：

| 能力 | 符号/类 | 对指纹/识别的作用 |
|---|---|---|
| **IDFA** | `ASIdentifierManager` + AdSupport.framework | 广告标识符（跨 App 追踪的基础；iOS 14.5+ 因无 ATT 声明取值为零） |
| 硬件型号 | `sysctl` / `sysctlbyname` | `hw.machine`（iPhone9,1 等机型代号） |
| 网络接口 | `getifaddrs` | 接口枚举（历史用于 MAC） |
| Wi-Fi SSID/BSSID | `CNCopyCurrentNetworkInfo` | 当前网络名称（地理线索） |
| 系统代理 | `CFNetworkCopySystemProxySettings`、PAC | 只读系统代理，**无内置代理配置** |
| 定位 | `CLLocationManager`、`CLCircularRegion` | 位置/围栏 |
| 蓝牙 | `CBCentralManager`、`CBUUID`、`CBAdvertisementDataServiceDataKey` | BLE 信标扫描 |
| Spotlight 索引 | `CSSearchableIndex` 等 5 类 | 浏览内容进入 iOS 全局搜索 |
| Touch ID | `LAContext` | 密码/支付确认 |
| 唯一 ID 能力 | `NSUUID`、`CFUUIDCreate*` | 本地随机标识 |
| 设备信息 | `UIDevice` | 系统名/版本/型号 |

### 4.4 网页侧 JS 检测面（注入脚本）

Chrome iOS 是 **WKWebView 壳**；以下 16 个脚本位于包根目录，其中多数被注入到**页面世界**（page world），因此网站 JS 完全可见（完整表见证据 `04-user-scripts-injection.txt`）。注入方式已由源码确认：早期脚本以 `WKUserScript` 在 **AtDocumentStart**、**仅主框架**（`forMainFrameOnly:YES`）注入；当年 iOS SDK 没有 content-world API → **必然落在页面世界**（§9.4）。

| 脚本 | 面向网页的产物（检测点） |
|---|---|
| `web_bundle.js` | `window.__gCrWeb`（~30 成员），改写 `window.scrollTo` |
| `window_id.js` | `__gCrWeb.windowId = "$(WINDOW_ID)"`（16 随机字节→32 hex，**每次注入重新生成**，§9.4） |
| `nav_bundle.js` | 改写 `history.pushState/replaceState/back/forward/go` |
| `chrome_bundle.js` | 改写 `window.print` |
| `post_request.js` | `__crPostRequestWorkaround`；`messageHandlers.POSTSuccessHandler/POSTErrorHandler` |
| `payment_request.js` | `window.PaymentRequest/PaymentResponse` polyfill |
| `autofill_controller.js` / `password_controller.js` / `suggestion_controller.js` | `__gCrWeb.autofill/findPasswordForms/suggestion…`（扫描表单结构） |
| `contextualsearch.js` | `__gCrWeb.contextualSearch…`（选中文本→宿主） |
| `language_detection.js` | `__gCrWeb.languageDetection…`（正文文本→宿主） |
| `translate_ios.js` / `find_in_page.js` / `plugin_placeholder.js` | `__gCrWeb.translate/findInPage/plugin` |
| `cast_sender_iframe.js` | `chrome.cast.*`（使用 Cast 时） |
| `web_ui_bundle.js` | 仅 WebUI：`window.chrome` / `chrome.send` |

**结论（网站如何识别这个浏览器）：**

1. `'__gCrWeb' in window` → 命中 Chrome iOS（最强单点特征）。
2. `window.webkit.messageHandlers` 存在 → iOS WebView。
3. `String(window.history.pushState).includes('invokeOnHost')` → 被 Chrome 改写。
4. `navigator.vendor` 为 `Apple Computer, Inc.`（WKWebView 默认），与 UA 声明的 Chrome 品牌矛盾。
5. 普通页面**无 `window.chrome`**（与桌面 Chrome 差异）。
6. UA：`… AppleWebKit/xxx (KHTML, like Gecko) CriOS/61.0.3163.73 Mobile/… Safari/xxx` —— 版本号暴露；且**引擎其实是系统 WebKit**，Chromium 61 与系统 WebKit 特性错配，可被特性探测抓到。源码级确认 UA 为**静态表 + sysctl 拼装**（§9.3）：AppleWebKit/Safari 令牌与本机真实引擎无关，完全可离线复现；在 iOS 11 设备上会因表中无对应条目而回退到 10.3 档（AppleWebKit/602.1、Safari/603.1.30）。
7. 页面级副作用：表单结构被 autofill 扫描、选中文本与正文可被上报宿主（隐私层面同样值得注意）。
8. **消息处理器名 `crwebinvoke`**（`window.webkit.messageHandlers.crwebinvoke`）是 Chrome iOS 专属标记（§9.4），比"messageHandlers 存在"更精确。
9. 宿主脚本执行与消息回传都绑定 `__gCrWeb.windowId`（不匹配即拒绝）；页面删除/覆盖这些对象会影响部分功能，但"曾经/正在被 Chrome iOS 打开"的静态特征仍可被检测。

### 4.5 WebUI 内部页面（chrome://）

包内发现的 chrome:// 页面（完整探见证据 `05-webui-pages.txt`）中，**经源码注册表确认为 iOS 可达**的有（§9.5）：

`version`（可见 **User Agent / 命令行 / Variations / 系统与固件版本** 等 15 个字段）、`crashes`、`flags`、`terms`、`credits`、`chrome-urls`、`histograms`、`gcm-internals`、`net-export`、`ntp-tiles-internals`、`omaha`、`physical-web`（实验开关控制）、`signin-internals`、`suggestions`、`sync-internals`。

此外 `resources.pak` 中还打包了 `safebrowsing`、`interstitials`、`policy`、`net-internals`、`dino`、`favicon` 等页面资源，但本版 iOS **未注册**对应 WebUI 控制器（跨平台共享资源/历史残留），普通场景不可达（§9.5）。

这些页面本身只在本地可达，但说明以下模块全部激活：崩溃、统计通道、同步、登录、推送（GCM）、Safe Browsing（原生层）、网络日志、BLE 信标。

### 4.6 网络行为与端点

| 端点/域 | 用途 | 依据 |
|---|---|---|
| `clients2.google.com/cr/report` | Breakpad 崩溃上报 | **已确认**（Info.plist） |
| `accounts.google.com` | 登录/SSO | 推断（SSO 组件 + 标准流程） |
| `clients4.google.com` | 同步（Chrome Sync） | 推断（sync-internals + 标准端点） |
| Safe Browsing 服务 | URL/下载校验 | 推断（safebrowsing 模块） |
| Google 搜索/建议服务 | Omnibox 字词建议 | 推断 |
| GCM/FCM 服务 | 推送注册与下行 | 推断（aps + gcm-internals） |
| `www.googleapis.com` | Cast/账号 API | 推断 |

> 已确认项仅为 Info.plist 中的 Breakpad；其余按 Chromium 公开架构标注为推断。抓包验证方法见第 7 节。

### 4.7 应用互操作探测（canOpenURL / Handoff / SSO）

- **16 个 `LSApplicationQueriesSchemes`**（Chromium、Maps、Gmail、Drive、Docs、Photos、Chat+/gplus、Calendar、Device Policy…）：Chrome 可以**探测本机安装了哪些 Google App**（典型用途是"用其他应用打开"；对风控来说是设备画像来源）。
- **Handoff**：`NSUserActivityTypes = com.google.chrome.handoff`，接续活动可被附近设备感知。
- **SSO URL Scheme**：`com.google.sso.chrome.stable`，与其他 Google App 互相唤起登录流。

---

## 5. 反篡改与 DRM 分析

### 5.1 FairPlay（对 App 的保护）

- `SC_Info` 完整 + `cryptid=1`：二进制只能在**解密授权环境**运行（正常途径 = 购买账号的 App Store 安装）。
- 直接把该 IPA 重签名（Sideloadly/AltStore/企业证书）**不可行**：重签会破坏 DRM 校验，必须先在有越狱环境解密出明文二进制。

### 5.2 重签后的实际后果（不是 App 检测，而是能力缺失）

| 能力 | 重签后的变化 | 后果 |
|---|---|---|
| keychain group `com.google.common.SSO` | 组名随新 TeamID 改变/丢失 | Google SSO 可能无法读取既有凭据，登录异常 |
| App Groups `group.com.google.chrome/common` | 换容器 | 与扩展共享数据、部分历史/书签状态异常 |
| `aps-environment` | 需自己的推送证书或剔除 | Google 推送失效 |
| Associated Domains `applinks:goo.gl` | 需重新申请/失效 | 短链直接跳转异常 |

> 结论：这个包的"防改造"主要来自 **DRM + iOS 签名 + entitlement 绑定**，而不是应用内检测代码。未发现完整性自校验、调试器检测、越狱目录探测（Cydia 等）、ptrace 反调试等典型反篡改逻辑。

### 5.3 明确"没有发现"的机制

- ✗ 越狱**拒绝服务**（检测到越狱就退出/禁用功能）
- ✗ 调试器检测（ptrace/PT_DENY_ATTACH、sysctl 调试标志）
- ✗ 完整性自校验（校验自身哈希）
- ✗ 模拟器/云机识别（如设备传感器一致性校验）
- ✗ 反注入白名单（除 Safe Mode 的崩溃归因外）
- ✗ 服务器侧设备 attestation（DeviceCheck 等；iOS 61 时代无此机制）

（以上仅在可读范围内确认；加密段内部若存在额外逻辑，需解密后复核。）

---

## 6. 检测面总结矩阵

| 视角 | 检测方 | 检测内容 | 证据等级 | 强度 |
|---|---|---|---|---|
| A | App→本地环境 | 已加载注入模块（越狱 tweak），启动崩溃归因、清单展示、随崩溃报告外发（**源码级确认**） | 已确认（黑名单 + dyld 符号 + 文案 + 官方源码 §9.1/§9.2） | 中（不阻断） |
| B | App→Google | 崩溃、统计、账号/同步、推送、Safe Browsing、语音、位置、BLE、Cast、反馈 | 已确认（配置/资源/文案） | 强（产品级遥测） |
| C | 网站→浏览器 | `__gCrWeb`（含 windowId）、`messageHandlers.crwebinvoke`、改写原生方法、无 `window.chrome`、UA=CriOS/61（静态拼接）、引擎/版本特征错配、表单/文本读取 | 已确认（注入脚本原文 + 官方源码 §9.3/§9.4） | 强（难以对网站隐藏） |
| D | OS→App | FairPlay 解密校验、签名校验、entitlement 绑定 | 已确认（签名/DRM 文件） | 强（阻止重签改造） |

---

## 7. 深度分析路线图

| 目标 | 方法 | 说明 |
|---|---|---|
| 拿到明文二进制 | 越狱设备：`frida-ios-dump` / `bagbak` / `dumpdecrypted` | 解除 FairPlay 后才能读应用逻辑字符串 |
| 解出剩余加密逻辑（Variations 取值、IDFA 实际调用点、遥测请求细节等） | 解密后 `strings` + Ghidra/IDA + `class-dump` | UA 构造 / Safe Mode / 崩溃上报内容已由官方源码确认（§9），无需解密；此处针对加密段内仍未确认的调用点 |
| 验证网络端点 | 设备全局代理 + mitmproxy/Charles（安装 CA） | 观察 `clients2/cr/report`、sync、safebrowsing、variations seed 等实际请求 |
| 网页检测面动态验证 | 打开自建测试页（见 8.2 节代码） | 对照本报告 4.4 节清单 |
| Safe Mode 行为验证 | 越狱机注入一个会令 Chrome 启动崩溃的 dylib，观察提示页与 plist/崩溃报告 | 机制已源码级确认（§9.1），动态验证为可选复核 |

---

## 8. 对本项目（AiFingerprint-Browser）的结论与建议

### 8.1 关键结论

1. **该 IPA 不是"指纹浏览器"，而是原版 Chrome** —— 不提供任何伪装（无 canvas/WebGL/UA/时区改写逻辑，也不可能有：iOS 上这些由系统 WebKit 决定）。如果市面上有产品分发这个包并声称"防关联"，伪装能力并不在这个 IPA 里，而是在其外围（越狱 tweak / 私有壳 / 系统级 hook）。
2. **桌面端 CDP 注入路线在 iOS 不可复用**：WKWebView 没有公开的 CDP/DevTools 远程协议。iOS 上可控的面（应用层）：UA、语言、时区、地理位置、权限策略、Cookie/存储隔离；不可控面（内核层）：canvas/WebGL/音频/字体/GPU 渲染指纹。
3. **要在 iOS 真机做指纹伪装，只有两条路**：
   - 越狱 + WebKit/JS 层 hook（tweak/注入；注意 Safe Mode 与崩溃报告的暴露面，见 4.1）；
   - 自研 WKWebView 壳 App（可控应用层面，但深指纹仍受系统 WebKit 约束）。
4. **若用于云手机/群控批量部署**：原包受 DRM+Apple ID 绑定，批量需越狱解密后重签（SSO/推送/App Group 能力会受损）；并且 Chrome 61 的老 UA 与现代 WebKit 的特性错配属于高危指纹，建议先核对目标风控对版本的要求。

### 8.2 自测清单（可直接运行的检测代码）

在待测浏览器与"真机 Chrome iOS"上分别运行，逐项对比：

```js
// Chrome iOS (CriOS) 检测探针 —— 用于自家方案与真机的差异比对
(() => {
  const r = {
    // 1) Chrome iOS 专属全局（最强特征）
    hasGCrWeb: typeof window.__gCrWeb !== 'undefined',
    gCrWebWindowId: (window.__gCrWeb && window.__gCrWeb.windowId) || null,
    // 2) WKWebView 痕迹
    hasWK: !!(window.webkit && window.webkit.messageHandlers),
    wkHandlers: window.webkit && window.webkit.messageHandlers
      ? Object.keys(window.webkit.messageHandlers) : [],
    // 3) 被改写的原生方法
    historyPatched: /invokeOnHost/.test(String(window.history.pushState)),
    printPatched: /invokeOnHost/.test(String(window.print)),
    scrollPatched: /setWebViewScrollViewIsDragging/.test(String(window.scrollTo)),
    // 4) 桌面 Chrome 有 / iOS 普通页没有
    hasChromeObj: typeof window.chrome !== 'undefined',
    // 5) UA / 品牌 / 平台一致性
    ua: navigator.userAgent,
    isCriOS: /CriOS\/[\d.]+/.test(navigator.userAgent),
    vendor: navigator.vendor,              // WKWebView = "Apple Computer, Inc."
    platform: navigator.platform,
    maxTouchPoints: navigator.maxTouchPoints,
    // 6) PaymentRequest 来源（polyfill vs 原生）
    pr: String(window.PaymentRequest || ''),
    // 7) 触控/传感器侧写
    hasTouch: 'ontouchstart' in window,
    devicePixelRatio: window.devicePixelRatio,
    screen: [screen.width, screen.height, screen.colorDepth].join('x'),
    languages: navigator.languages && navigator.languages.join(','),
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  };
  console.table(r); return r;
})();
```

风控侧常用判定伪代码（供自测自查）：

```js
function looksLikeChromeIOS() {
  return ('__gCrWeb' in window)
      || /CriOS\//.test(navigator.userAgent)
      || (/Apple Computer, Inc./.test(navigator.vendor) && !!window.webkit?.messageHandlers);
}
```

### 8.3 路线建议（结合移动端增强现状）

| 场景 | 建议 |
|---|---|
| 移动端指纹伪装（真机） | 优先评估「越狱 + WebKit hook」可行性；注入方式避开 `DynamicLibraries`，并关闭/规避崩溃上报路径，降低 Safe Mode/崩溃报告暴露面 |
| 移动端指纹伪装（免越狱） | 只能做应用层伪装（UA/语言/时区/权限/存储隔离），深指纹保持"真实设备原样"，走"真机真指纹"策略而非"伪造"策略 |
| 现有 CDP 移动仿真（桌面） | 与 iOS 真机是两张皮：仿真抓 CreepJS/Pixelscan 只能验证桌面模拟质量，不能代表 iOS 真机表现，两者分开评估 |
| 云手机采购/部署 | 若供应商给的是本 IPA：确认其为**越狱解密重签版**，并检查 SSO/推送是否可用、UA 版本是否过旧 |

---

## 9. 源码级交叉验证（第二轮补充）

以样本内置 `SCMRevision` 为锚（Chromium `0cb96822e59630c82a826186a57b88d10d998e88`；Breakpad `7a65a47345a86c9e9a3fbc2e92a756a429a0c82f`，由该 commit 的 DEPS 指定），直接读取**同版官方源码**核验第一轮结论。完整代码引用与文件清单见证据 [`chrome61-analysis/11-chromium-source-cross-reference.md`](chrome61-analysis/11-chromium-source-cross-reference.md) 与 [`chrome61-analysis/chromium-src/`](chrome61-analysis/chromium-src/SOURCE-MANIFEST.txt)。

### 9.1 Safe Mode（对照 §4.1）

- **触发**：`SafeModeCoordinator.shouldStart` = `GetFailedStartupAttemptCount() >= 2` **且** `[SafeModeViewController hasSuggestions]`；计数存于 NSUserDefaults 键 `AppStartupFailureCount`（`crash_loop_detection_util.mm`）；进入后立即 `ResetFailedStartupAttemptCount()`。
- **判定**：`hasSuggestions = hasThirdPartyMods || hasReportToUpload`。其中 `hasThirdPartyMods` = `safe_mode_util::GetLoadedImages("/Library/MobileSubstrate/DynamicLibraries/")` 非空 —— 实现为 `_dyld_image_count()` + `_dyld_get_image_name()` + `strncmp` **前缀过滤**（`safe_mode_util.cc` 全文仅 24 行）；`hasReportToUpload` = 上传开启且待传崩溃报告 > 1。
- **动作**：仅 UI 提示 +「重试」（`RestoreDefaultConfiguration()`）/「发送崩溃报告」（`StartUploadingReportsInRecoveryMode()`：加参数 `uploaded_in_recovery_mode=yes`、上传间隔 1s、立即上传）。**不阻断注入、不退出应用、不写持久标记。**

### 9.2 崩溃上报内容（对照 §4.2 / §4.6）

- 生成：`Breakpad::GenerateReport` → `MinidumpGenerator(mach_task_self(), MACH_PORT_NULL).Write()`；`Write()` 的流列表中包含 `WriteModuleListStream`，后者用 `_dyld_image_count()` 枚举全部镜像，并由 `WriteModuleStream` 将 `_dyld_get_image_name(index)` 的**完整路径**写入 minidump（iOS 上 `in_memory=true`）。
- 上传：multipart 字段 `upload_file_minidump`（>2 MB 拒传；iOS 不附带 log 文件）；Google 服务器参数映射 `prod` / `ver` / `guid` / `ptime`；`guid` 为本机随机生成的崩溃客户端 ID。
- 附加参数：`channel`、`metrics_client_id`、`num-experiments`、`variations`（`crash_keys.cc`）+ `crashed_in_background`、`free_memory_in_kb`、`free_disk_in_kb`、`memory_warning_count`、`uptime_at_restore_in_ms`、`uploaded_in_recovery_mode`、`user_application_state`（orient/sizeclass/signIn/pdf/avplay）（`breakpad_helper.mm`）。
- **闭环**：越狱注入的 dylib 路径会在"发生崩溃 + 允许上传"时随报告发往 `clients2.google.com/cr/report`。

### 9.3 UA（CriOS）构造（对照 §4.4 / §8）

- 产品串：`ChromeWebClient::GetProduct() = "CriOS/" + version_info::GetVersionNumber()`（本样本 = `CriOS/61.0.3163.73`）。
- 移动 UA 模板（`user_agent.mm`）：`Mozilla/5.0 (<设备; CPU iPhone OS x_y[_z] like Mac OS X>) AppleWebKit/<表> (KHTML, like Gecko) CriOS/<版本> Mobile/<sysctl kern.osversion> Safari/<表>`；WebKit/Safari 版本取自**静态表**（表止于 iOS 10.3，更高系统命中 `NOTREACHED()` 回退表尾条目）。
- 作用方式（`web_view_internal_creation_util.mm`）：`BuildWKWebView` 在 `user_agent_type != NONE` 时设置 `web_view.customUserAgent = GetUserAgent(type)`；DESKTOP 类型使用固定 macOS Safari UA（`chrome_web_view_factory.mm::kDesktopUserAgent`）。
- 结论：UA 与真实引擎无关、**完全可离线复现**；iOS 11 设备上"声明 AppleWebKit/602.1 + Safari/603.1.30"是确定性错配（风控高危特征）。

### 9.4 页面脚本注入与消息通道（对照 §4.4）

- 注入：`WKUserScript`，`AtDocumentStart` + `forMainFrameOnly:YES`；脚本以 `if (typeof __gCrWeb !== 'object')` 幂等包裹（`page_script_util.mm`）；2017 年 SDK 无 content-world API → **页面世界可见**。
- 消息：WKScriptMessageHandler 名 = **`crwebinvoke`**；JS 端 `window.webkit.messageHandlers['crwebinvoke'].postMessage({crwCommand, crwWindowId})`，发送前 `delete window.webkit` 防页面覆盖；宿主仅接受主框架消息（`frameInfo.mainFrame`）。
- windowId：每次注入用 `crypto::RandBytes(16)` → 32 hex；宿主脚本执行前统一包裹 `if (__gCrWeb['windowId'] === '<id>') { … }`；消息按 `crwWindowId` 校验。
- **订正**：windowId 不是跨页稳定标识（第一轮表述已修正），但"存在 `__gCrWeb` / `messageHandlers.crwebinvoke`"本身仍是稳定检测特征。

### 9.5 WebUI 注册表（对照 §4.5）

- 本版**实际注册**（`chrome_web_ui_ios_controller_factory.mm`）：`chrome-urls`、`histograms`、`credits`、`crashes`、`gcm-internals`、`net-export`、`ntp-tiles-internals`、`omaha`、`physical-web`（实验开关）、`signin-internals`、`suggestions`、`sync-internals`、`terms`、`version`、`flags`。
- `safebrowsing` / `interstitials` / `policy` / `net-internals` / `dino` / `favicon` 等**仅有资源在包、未注册页面**（跨平台共享资源/历史残留）。

### 9.6 结论变更汇总

| # | 第一轮 | 第二轮（源码级） |
|---|---|---|
| 1 | Safe Mode 触发时序为推断 | 触发 = 启动失败计数 ≥ 2 且存在建议；扫描 = 前缀过滤 `/Library/MobileSubstrate/DynamicLibraries/` |
| 2 | 模块名单外发为推断 | minidump 确定写入全部已加载镜像完整路径；上传字段与附加键已确认 |
| 3 | windowId "每窗口唯一" | 每次注入重新随机生成（32 hex），用于消息/脚本校验 |
| 4 | UA 构造推断 | 静态表 + `sysctl` 拼装，离线可复现；高版本系统回退表尾条目 |
| 5 | `chrome://safebrowsing` 等视为可用 | iOS 未注册这些 WebUI（仅资源在包内） |
| 6 | 消息处理器名不明 | `crwebinvoke` |

### 9.7 仍未解决

- 加密段内部：IDFA 的实际调用点、Variations 种子获取与上报细节、遥测请求体内容（仍建议解密/抓包复核）；
- 网络端点除 Breakpad 外未做抓包验证（本轮仅以客户端源码确认了上传路径与字段名）。

---

## 附录 A 证据文件索引

| 文件 | 内容 |
|---|---|
| [`chrome61-analysis/00-README.md`](chrome61-analysis/00-README.md) | 证据目录说明 |
| [`chrome61-analysis/01-identity-hashes.txt`](chrome61-analysis/01-identity-hashes.txt) | 哈希/身份/签名/DRM 原始输出 |
| [`chrome61-analysis/02-entitlements.txt`](chrome61-analysis/02-entitlements.txt) | 主 App+扩展 entitlements |
| [`chrome61-analysis/03-linked-frameworks.txt`](chrome61-analysis/03-linked-frameworks.txt) | 链接框架与能力对照 |
| [`chrome61-analysis/04-user-scripts-injection.txt`](chrome61-analysis/04-user-scripts-injection.txt) | 16 个注入脚本全表 |
| [`chrome61-analysis/05-webui-pages.txt`](chrome61-analysis/05-webui-pages.txt) | chrome:// 页面清单 |
| [`chrome61-analysis/06-telemetry-strings-en-zh.txt`](chrome61-analysis/06-telemetry-strings-en-zh.txt) | 遥测/安全模式中英文原文 |
| [`chrome61-analysis/07-file-inventory.txt`](chrome61-analysis/07-file-inventory.txt) | 文件清单 |
| [`chrome61-analysis/08-safe-mode-jailbreak-detection.txt`](chrome61-analysis/08-safe-mode-jailbreak-detection.txt) | Safe Mode 专项证据 |
| [`chrome61-analysis/09-device-apis-symbols.txt`](chrome61-analysis/09-device-apis-symbols.txt) | 设备 API 符号证据 |
| [`chrome61-analysis/10-reproduce-commands.sh`](chrome61-analysis/10-reproduce-commands.sh) | 复现命令集（含第二轮源码抓取命令） |
| [`chrome61-analysis/11-chromium-source-cross-reference.md`](chrome61-analysis/11-chromium-source-cross-reference.md) | **第二轮：Chromium/Breakpad 源码级交叉验证（同 commit 对照）** |
| [`chrome61-analysis/chromium-src/`](chrome61-analysis/chromium-src/SOURCE-MANIFEST.txt) | 官方源码快照（20+ 文件）与获取清单 |

## 附录 B 术语表

| 术语 | 说明 |
|---|---|
| FairPlay | Apple 的 DRM，App Store 二进制加密（`cryptid=1`），只有授权设备/账号能解密运行 |
| MobileSubstrate | 越狱生态的 tweak 注入框架（Cydia Substrate），dylib 存放于 `/Library/MobileSubstrate/DynamicLibraries/` |
| Safe Mode | Chrome iOS 的启动崩溃恢复流程，会枚举第三方模块并提示卸载 |
| Breakpad | Chromium 的崩溃捕获/上报组件（此处上报至 `clients2.google.com`） |
| WKWebView | iOS 的系统网页渲染组件；Chrome iOS 是它的壳 |
| `__gCrWeb` | Chrome iOS 注入到网页世界的全局对象（本报告 4.4 节） |
| `crwebinvoke` | Chrome iOS 注入网页与宿主通信的 WKScriptMessageHandler 名（`window.webkit.messageHandlers.crwebinvoke`，§9.4） |
| entitlement | iOS 签名中声明能力的字段（App Group、Keychain 组、推送等） |
| IDFA | 广告标识符（AdSupport / ASIdentifierManager） |

## 附录 C 分析局限

1. 主二进制 `__TEXT`（含应用逻辑字符串与 ObjC 方法名）仍被 FairPlay 加密；但样本内置版本锚使本报告能用**同版官方源码**核验主要机制（§9）。剩余不可读项集中于加密段内的应用层调用点（IDFA 实际用途、部分分支的调用时机）与遥测/变体请求细节。
2. 网络端点除 Breakpad 外仍为按公开架构推断，未做设备抓包验证；本轮仅以客户端源码确认了 Breakpad 上传路径与字段名（§9.2）。
3. Safe Mode 的触发时序与判定逻辑已由源码确认（§9.1），第一轮相应"推断"标注随之取消。
4. codesign 对旧资源信封的兼容性提示不影响本报告对签名链完整性的判断。
5. 源码交叉验证以"样本 `SCMRevision` 锚定的官方仓库"为准；若未来解密出的二进制与官方源码存在差异，以二进制为准。

---

*报告生成：2026-10-18 ｜ 第二轮更新：Chromium 源码级交叉验证（§9）｜ 样本哈希与全部证据文件见附录 A*
