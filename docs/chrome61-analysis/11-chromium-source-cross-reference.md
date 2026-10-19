# 11 - Chromium 官方源码交叉验证（第二轮）

> 第一轮为包内文件级静态分析（见本目录 01–10 号证据与主报告 §1–§8）。
> 本轮以样本 `Info.plist` 内置的 **SCMRevision** 为锚，直接读取与样本**同 commit**
> 的 Chromium 官方源码，以及由该 commit 的 DEPS 指定的 Breakpad 源码，逐项验证机制。

## 0. 锚点与获取方式

| 项目 | 值 |
|---|---|
| 样本 | Chrome for iOS 61.0.3163.73（stable），SHA-256 `5f901179…041c` |
| Chromium commit | `0cb96822e59630c82a826186a57b88d10d998e88`（来自样本 `SCMRevision`） |
| Breakpad 修订 | `7a65a47345a86c9e9a3fbc2e92a756a429a0c82f`（来自该 commit 的 `DEPS` → `src/breakpad/src`） |
| 获取模板（Chromium） | `https://chromium.googlesource.com/chromium/src/+/<commit>/<path>?format=TEXT` |
| 获取模板（Breakpad） | `https://chromium.googlesource.com/breakpad/breakpad/src/+/<rev>/<path>?format=TEXT` |
| 本机快照 | 见 `chromium-src/` 目录与 `chromium-src/SOURCE-MANIFEST.txt`（含每个文件用途） |

> 结论等级说明：**[源码级已确认]** = 在官方源码中找到与样本同版本的实现；
> **[文件级已确认]** = 第一轮包内证据；**[推断]** = 仍需解密/抓包验证。

---

## 1. Safe Mode（对照主报告 §4.1）

### 1.1 触发条件 [源码级已确认]

`ios/chrome/app/safe_mode/safe_mode_coordinator.mm`：

```objc
namespace {
const int kStartupCrashLoopThreshold = 2;
}
...
+ (BOOL)shouldStart {
  // Check whether there appears to be a startup crash loop. If not, don't look
  // at anything else.
  if (crash_util::GetFailedStartupAttemptCount() < kStartupCrashLoopThreshold)
    return NO;

  return [SafeModeViewController hasSuggestions];
}
```

`ios/chrome/browser/crash_loop_detection_util.mm`：

```objc
NSString* const kAppStartupFailureCountKey = @"AppStartupFailureCount";
int GetFailedStartupAttemptCount() { ... [defaults integerForKey:kAppStartupFailureCountKey]; }
```

`SafeModeCoordinator -start`：把 `SafeModeViewController` 设为 window 根控制器，并**立即**调用
`crash_util::ResetFailedStartupAttemptCount()`（避免下次启动仍进安全模式）。

**含义**：Safe Mode 只在"启动失败计数 ≥ 2"（连续两次启动失败）且存在"建议"时出现；
不是每次启动都扫描，也不是越狱检测即弹窗。

### 1.2 "存在建议"的判定 [源码级已确认]

`ios/chrome/app/safe_mode/safe_mode_view_controller.mm`：

```objc
+ (BOOL)hasSuggestions {
  return [self hasThirdPartyMods] || [self hasReportToUpload];
}
...
std::vector<std::string> thirdPartyMods = safe_mode_util::GetLoadedImages(
    "/Library/MobileSubstrate/DynamicLibraries/");
return (thirdPartyMods.size() > 0);
...
// If uploading is enabled and more than one report has stacked up...
return breakpad_helper::IsUploadingEnabled() &&
       breakpad_helper::GetCrashReportCount() > 1;
```

`ios/chrome/app/safe_mode_util.cc`（**唯一的"本地环境检测"实现**）：

```cpp
std::vector<std::string> GetLoadedImages(const char* path_filter) {
  std::vector<std::string> images;
  uint32_t image_count = _dyld_image_count();
  for (uint32_t i = 0; i < image_count; ++i) {
    const char* path = _dyld_get_image_name(i);
    if (path_filter && strncmp(path, path_filter, strlen(path_filter)) != 0)
      continue;
    images.push_back(path);
  }
  return images;
}
```

**含义**：
- 检测方式 = 枚举**全部已加载 Mach-O 镜像**，保留路径以
  `/Library/MobileSubstrate/DynamicLibraries/` 开头的项（`strncmp` 前缀匹配）；
- 不做正则、不查 Cydia、不检查其它越狱目录、不做反调试。

### 1.3 展示与动作 [源码级已确认]

- 已知模块命中 `SafeModeCrashingModules.plist` 时，用 `ModuleFriendlyName` 列名
  （读取逻辑见 `safe_mode_crashing_modules_config.mm`：`StartupCrashModules` / `ModuleFriendlyName`）；
- 命中路径但不在名单 → 通用"插件"文案；两者皆无 → "未知原因"文案；
- 相关文案 ID（与 locale.pak/40 语言 `.strings` 中的 7 条完全对应）：

```text
IDS_IOS_SAFE_MODE_AW_SNAP
IDS_IOS_SAFE_MODE_NAMED_TWEAKS_FOUND
IDS_IOS_SAFE_MODE_TWEAKS_FOUND
IDS_IOS_SAFE_MODE_UNKNOWN_CAUSE
IDS_IOS_SAFE_MODE_RELOAD_CHROME
IDS_IOS_SAFE_MODE_SENDING_CRASH_REPORT
IDS_IOS_SAFE_MODE_CRASH_REPORT_SENT
```

- 按钮流：`RELOAD_CHROME`（重试）→ `breakpad_helper::RestoreDefaultConfiguration()`
  + `startBrowserFromSafeMode`（`coordinatorDidExitSafeMode`）；
  「发送崩溃报告」→ `StartUploadingReportsInRecoveryMode()` → 完成后文案变 `CRASH_REPORT_SENT`。

### 1.4 恢复模式上传 [源码级已确认]

`ios/chrome/browser/crash_report/breakpad_helper.mm`：

```objc
void StartUploadingReportsInRecoveryMode() {
  [[BreakpadController sharedInstance] stop];
  [[BreakpadController sharedInstance] setParametersToAddAtUploadTime:@{
    kUploadedInRecoveryMode : @"yes"        // = @"uploaded_in_recovery_mode"
  }];
  [[BreakpadController sharedInstance] setUploadInterval:1];
  [[BreakpadController sharedInstance] start:NO];
  [[BreakpadController sharedInstance] setUploadingEnabled:YES];
}
```

**含义**：Safe Mode 不会阻断注入、不会退出应用；其"最强动作"是用
`uploaded_in_recovery_mode=yes` 标记并**立即上传**积压的崩溃报告。

### 1.5 与第一轮的差异（修正）

| 第一轮表述 | 第二轮结论 |
|---|---|
| "启动时（及崩溃检测流程中）枚举镜像"（推断） | 枚举函数本身随时可调用；**是否进入 Safe Mode** 取决于"启动失败计数 ≥ 2 且存在建议" |
| "若发生启动崩溃，将模块与 plist 对照" | 对照清单存在（`StartupCrashModules`），未命中仍会展示通用插件文案 |
| 触发条件"主要是启动崩溃归因路径" | 精确为 `GetFailedStartupAttemptCount() >= 2`（NSUserDefaults 计数） |

---

## 2. 崩溃上报管线（对照主报告 §4.2 / §4.6）

### 2.1 上传路径与表单字段 [源码级已确认]

- `BreakpadController.mm`：
  - 启动时 `BreakpadAddUploadParameter(breakpadRef_, @"platform", GetPlatform())`；
  - `setUploadingEnabled:` / `setUploadInterval:` / `setParametersToAddAtUploadTime:` 控制上传；
- `breakpad/client/ios/Breakpad.mm` `GenerateReport()`：

```objc
google_breakpad::MinidumpGenerator generator(mach_task_self(), MACH_PORT_NULL);
std::string dumpFilename = generator.UniqueNameInDirectory(dumpDir, &dumpId);
bool success = generator.Write(dumpFilename.c_str());
```

- `breakpad/client/mac/sender/uploader.mm`：
  - google 服务器参数映射：`ptime`←处理时长、`prod`←产品名、`ver`←版本、`guid`←崩溃客户端 ID（`NSUserDefaults` 的 `kClientIdPreferenceKey`；`srandom(time)+random()×3` 生成，与 MAC 无关）；
  - minidump 文件 > 2 MB 则**不传 minidump**；
  - multipart 字段：`upload_file_minidump`（iOS 上不附带 `log` 文件，`readLogFileData` 在 `TARGET_OS_IPHONE` 返回 NO）。

### 2.2 minidump 包含全部已加载镜像的完整路径 [源码级已确认]

`breakpad/client/mac/handler/minidump_generator.cc`：

```cpp
bool MinidumpGenerator::Write(const char *path) {
  WriteStreamFN writers[] = {
    &MinidumpGenerator::WriteThreadListStream,
    &MinidumpGenerator::WriteMemoryListStream,
    &MinidumpGenerator::WriteSystemInfoStream,
    &MinidumpGenerator::WriteModuleListStream,   // ← 模块清单流
    ...
```

```cpp
bool MinidumpGenerator::WriteModuleListStream(MDRawDirectory *module_list_stream) {
  uint32_t image_count = dynamic_images_ ? dynamic_images_->GetImageCount()
                                         : _dyld_image_count();   // 枚举全部镜像
  ...
}
```

```cpp
// WriteModuleStream(...) 中：
const char* name = _dyld_get_image_name(index);      // 完整路径
...
writer_.WriteString(name, 0, &string_location);
module->module_name_rva = string_location.rva;       // 写入模块名（路径）
...
bool in_memory = false;
#if TARGET_OS_IPHONE
in_memory = true;
#endif
```

**含义（循环闭合）**：任何**已加载**的 dylib（例如
`/Library/MobileSubstrate/DynamicLibraries/CDM.dylib`、Frida/注入框架的 dylib）
其**完整路径**都会写进 minidump；在"发生崩溃 + 上传开启"时，随
`POST https://clients2.google.com/cr/report`（multipart，字段 `upload_file_minidump`）发往 Google。
第一轮"模块名单可能随报告外发（推断）"至此**升级为源码级确认**。

### 2.3 除模块清单外的附加字段 [源码级已确认]

`ios/chrome/browser/crash_report/crash_keys.cc`（注册的固定崩溃键；字面值见
`components/crash/core/common/crash_keys.cc`）：

| 常量 | 字面值 | 说明 |
|---|---|---|
| `kChannel` | `channel` | 渠道（启动时由 `AddReportParameter(@"channel", …)` 写入） |
| `kMetricsClientId` | `metrics_client_id` | UMA 客户端 ID |
| `kNumVariations` | `num-experiments` | 生效实验数量 |
| `kVariations` | `variations` | 实验列表（Huge 槽位） |
| `kBug464926CrashKey` | `bug-464926-info` | 历史遗留调试键 |
| `kZombie` / `kZombieTrace` | `zombie` / `zombie_trace` | 僵尸对象诊断 |

`breakpad_helper.mm` 额外上传参数：`crashed_in_background`、
`free_disk_in_kb`、`free_memory_in_kb`、`memory_warning_in_progress`、
`memory_warning_count`、`uptime_at_restore_in_ms`、`uploaded_in_recovery_mode`、
以及合并参数 `user_application_state`（`orient`/`sizeclass`/`signIn`/`pdf`/`avplay`）。

**含义**：崩溃报告 = minidump（线程/内存/系统/模块/杂项）+ 表单参数
（`prod`/`ver`/`guid`/`ptime`/`platform`/渠道/崩溃键/运行上下文）。
对指纹/隐私视角而言，这是"设备画像 + 注入模块名录"的完整出站通道。

---

## 3. UA（CriOS）构造（对照主报告 §4.4 / §8）

### 3.1 产品串 [源码级已确认]

`ios/chrome/browser/web/chrome_web_client.mm`：

```cpp
std::string ChromeWebClient::GetProduct() const {
  std::string product("CriOS/");
  product += version_info::GetVersionNumber();     // "61.0.3163.73"
  return product;
}

std::string ChromeWebClient::GetUserAgent(web::UserAgentType type) const {
  if (type == web::UserAgentType::DESKTOP)
    return base::SysNSStringToUTF8(ChromeWebView::kDesktopUserAgent);
  ... // 命令行 --user-agent 覆盖（仅调试场景有效）
  return web::BuildUserAgentFromProduct(GetProduct());
}
```

`chrome_web_view_factory.mm`（Request Desktop Site 的固定 UA）：

```objc
NSString* const kDesktopUserAgent =
    @"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_4) "
    @"AppleWebKit/600.7.12 (KHTML, like Gecko) "
    @"Version/8.0.7 "
    @"Safari/600.7.12";
```

### 3.2 移动 UA 模板 [源码级已确认]

`ios/web/public/user_agent.mm`：

```cpp
base::StringAppendF(&user_agent,
                    "Mozilla/5.0 (%s) AppleWebKit/%s"
                    " (KHTML, like Gecko) %s Mobile/%s Safari/%s",
                    BuildOSCpuInfo().c_str(),      // "iPhone; CPU iPhone OS 10_3 like Mac OS X"
                    ua_versions.webkit_version_string,
                    product.c_str(),               // "CriOS/61.0.3163.73"
                    kernel_version,                // sysctl(CTL_KERN, KERN_OSVERSION)
                    ua_versions.safari_version_string);
```

WebKit/Safari 版本为**静态查表**（表最新条目为 iOS 10.3；更高系统命中
`NOTREACHED()` 并回退到表尾条目）：

```cpp
static const OSVersionMap version_map[] = {
    {10, 3, {"602.1", "603.1.30"}},
    {10, 0, {"602.1", "602.1.50"}},
    {9, 0, {"601.1.46", "601.1"}},
    {8, 0, {"600.1.4", "600.1.4"}},
    {7, 1, {"9537.53", "537.51.2"}},
    {7, 0, {"9537.53", "537.51.1"}},
};
```

### 3.3 应用到 WKWebView [源码级已确认]

`ios/web/web_state/web_view_internal_creation_util.mm`：

```objc
if (user_agent_type != web::UserAgentType::NONE) {
  web_view.customUserAgent = base::SysNSStringToUTF8(
      web::GetWebClient()->GetUserAgent(user_agent_type));
}
```

**含义**：
1. `CriOS/<版本>` 与 AppleWebKit/Safari 令牌**与真实引擎无关**，可离线复现；
2. 在本样本所对应的源码版本上，iOS > 10.3 的系统会拿到表尾条目
   （AppleWebKit/602.1 + Safari/603.1.30），"UA 声明引擎版本 vs 系统实际 WebKit"
   的错配是**确定性事实**（对风控是高危指纹）；
3. 同一台设备上，"普通 UA / 请求桌面版 UA"是两套固定字符串。

---

## 4. 页面脚本注入与消息通道（对照主报告 §4.4）

### 4.1 注入方式与时机 [源码级已确认]

`ios/web/web_state/ui/wk_web_view_configuration_provider.mm`：

```objc
WKUserScript* InternalGetEarlyPageScript(BrowserState* browser_state) {
  return [[WKUserScript alloc]
        initWithSource:GetEarlyPageScript(browser_state)
         injectionTime:WKUserScriptInjectionTimeAtDocumentStart
      forMainFrameOnly:YES];
}
...
[configuration.userContentController addUserScript:InternalGetEarlyPageScript(browser_state_)];
```

`ios/web/web_state/js/page_script_util.mm`：早期页面脚本被包裹在
`if (typeof __gCrWeb !== 'object') { … }` 中（幂等，一页只注入一次）。

**含义**：脚本在**文档开始**、**仅主框架**注入；2017 年的 iOS SDK 没有
content-world API，因此**必然注入页面世界**（网站 JS 直接可见）。

### 4.2 消息通道 [源码级已确认]

`crw_web_controller.mm`：

```objc
NSString* const kScriptMessageName = @"crwebinvoke";
```

网页侧可见：`window.webkit.messageHandlers.crwebinvoke`。
`ios/web/web_state/js/resources/message.js`：

```js
var messageQueue_ = { scheme: 'crwebinvoke', ... };
...
var oldWebkit = window.webkit;
delete window['webkit'];                 // 防止页面覆盖原生实现
window.webkit.messageHandlers[queueObject.scheme].postMessage({
    "crwCommand": command,
    "crwWindowId": __gCrWeb['windowId']
});
window.webkit = oldWebkit;
```

`crw_web_controller.mm` 的主框架与 windowID 校验：

```objc
- (BOOL)respondToWKScriptMessage:(WKScriptMessage*)scriptMessage {
  if (!scriptMessage.frameInfo.mainFrame) {
    // Messages from iframes are not currently supported.
    return NO;
  }
  ...
  message->GetString("crwWindowId", &windowID);
  if (base::SysNSStringToUTF8([_windowIDJSManager windowID]) != windowID) { ... return NO; }
```

### 4.3 windowId 生成与脚本包裹 [源码级已确认]

`ios/web/web_state/js/crw_js_window_id_manager.mm`：

```cpp
const size_t kUniqueKeyLength = 16;   // 32 个 hex 字符
...
- (void)inject {
  _windowID.reset([[self class] newUniqueKey]);       // 每次注入重新生成
  NSString* script = [web::GetPageScript(@"window_id")
      stringByReplacingOccurrencesOfString:@"$(WINDOW_ID)" withString:_windowID];
  ...
}
+ (NSString*)newUniqueKey {
  char randomBytes[kUniqueKeyLength];
  crypto::RandBytes(randomBytes, kUniqueKeyLength);
  return base::SysUTF8ToNSString(base::HexEncode(randomBytes, kUniqueKeyLength));
}
```

`crw_web_controller.mm` 执行宿主脚本时的统一包裹：

```objc
- (NSString*)scriptByAddingWindowIDCheckForScript:(NSString*)script {
  NSString* kTemplate = @"if (__gCrWeb['windowId'] === '%@') { %@; }";
  ...
}
```

**订正**：第一轮曾把 `windowId` 描述为"每窗口唯一/可跨页标识"。
源码显示它在**每次注入时重新随机生成**（16 随机字节 → 32 hex），
用途是"页面身份校验"（消息/脚本只在同一 windowId 下生效），
**不适合作为跨页追踪标识**，但"存在性"本身仍是检测特征。

---

## 5. WebUI 注册表（对照主报告 §4.5）

`ios/chrome/browser/ui/webui/chrome_web_ui_ios_controller_factory.mm`
（本 commit 实际注册，按源码顺序）：

| chrome:// 页面 | 控制器 |
|---|---|
| `chrome-urls`、`histograms`、`credits` | AboutUI |
| `crashes` | CrashesUI |
| `gcm-internals` | GCMInternalsUI |
| `net-export` | NetExportUI |
| `ntp-tiles-internals` | NTPTilesInternalsUI |
| `omaha` | OmahaUI |
| `physical-web`（实验开关控制） | PhysicalWebUI |
| `signin-internals` | SignInInternalsUIIOS |
| `suggestions` | SuggestionsUI |
| `sync-internals` | SyncInternalsUI |
| `terms` | TermsUI |
| `version` | VersionUI |
| `flags` | FlagsUI |

**与资源包（`resources.pak`）对照**：包内还打包了 `safebrowsing`、
`interstitials`、`policy`、`net-internals`、`dino`、`favicon` 等页面资源，
但 iOS 的控制器工厂**未注册**这些页面 —— 属跨平台共享资源/历史残留，
普通页面不可达（`chrome://safebrowsing` 在 iOS 61 上不存在实际面板；
安全浏览的校验逻辑在原生层执行）。

WebUI 专属桥：`ios/web/webui/resources/web_ui_bundle.js` =
`__crWeb.mojoApi` + `__crWeb.webUIBind` + `__crWeb.webUISend`
（对应包内 `web_ui_bundle.js`；仅 WebUI 页面具备 `window.chrome` 能力）。

---

## 6. 其它核对

| 项 | 结论 |
|---|---|
| `DEPS` → Breakpad 修订 | `7a65a47345a86c9e9a3fbc2e92a756a429a0c82f`（见 `breakpad-DEPS-entry.txt`） |
| 产品版本串 | `version_info::GetVersionNumber()` → UA 中的 `CriOS/61.0.3163.73` |
| UA 命令行覆盖 | `switches::kUserAgent`（`--user-agent`）对 MOBILE UA 生效；App Store 包无命令行入口 |
| 包内 JS 与源码关系 | 包内为重编译产物（Closure ADVANCED），保留的公共名与源文件语义一致（`chrome_bundle.js`⇒`print.js`；`nav_bundle.js`⇒`navigation.js`；`web_bundle.js`⇒bundle 定义） |
| iframe 消息 | 宿主**不接受**子框架消息（`frameInfo.mainFrame` 检查） |

---

## 7. 结论变更清单（相对第一轮）

| # | 第一轮 | 第二轮 |
|---|---|---|
| 1 | Safe Mode 触发时序未知（推断） | **触发 = 启动失败计数 ≥ 2 且存在建议**；检测实现 = 前缀过滤 `/Library/MobileSubstrate/DynamicLibraries/` [源码级已确认] |
| 2 | "崩溃 minidump 可能包含模块列表"（推断） | **minidump 确定写入全部已加载镜像的完整路径**；上传字段 `upload_file_minidump`；另有 `guid/prod/ver/ptime/platform` 与崩溃键（`metrics_client_id`、`variations` 等）[源码级已确认] |
| 3 | "windowId 每窗口唯一" | **每次注入重新生成**（16 随机字节→32 hex），用于消息/脚本校验 [源码级已确认] |
| 4 | UA "推断含 CriOS 版本" | UA 由静态表 + `sysctl` 拼装，**完全可离线复现**；iOS > 10.3 时回退到表尾版本，错配为确定事实 [源码级已确认] |
| 5 | `chrome://safebrowsing` 等页面视为可用 | iOS 注册表**未注册**这些页面（仅资源在包内）[源码级已确认] |
| 6 | 消息处理器名未明确 | `crwebinvoke`（`window.webkit.messageHandlers.crwebinvoke`）[源码级已确认] |
| 7 | 注入时机/框架范围推断 | **AtDocumentStart + 仅主框架 + 页面世界** [源码级已确认] |

---

## 附：本轮源码快照

见 `chromium-src/`（29 个文件：20 个源码快照 + 6 个节选 + 清单）。
所有来源 URL 与用途见 `chromium-src/SOURCE-MANIFEST.txt`。
