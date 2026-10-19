# Chrome iOS 61 IPA 补丁方案

> **依据**：[`Chrome61-IPA检测机制分析.md`](Chrome61-IPA检测机制分析.md)（2026-10-18；样本 `Chrome-v61-3163-73.ipa`，SHA-256 `5f901179…a041c`，官方原版、FairPlay 加密）
> **目的**：把报告的检测面结论整理为可执行的补丁清单，分两条路线——**A：真机 / 云机（IPA 二次开发）**；**B：本项目仿真层（桌面 CDP 补齐 CriOS 检测面）**。
> **结论先行**（报告 §8.1）：该 IPA 是原版 Chrome，本身不含任何伪装逻辑；"防关联/指纹"能力无论走哪条线都必须在外围补——A 线在设备侧（解密/重签/注入），B 线在本仓库（注入器 + 指纹库）。
> **B 线状态（2026-10-19）**：P0 已落地并验证——`mobileExtra.browser = "chrome_ios"`、CriOS UA、`derive_spec.iosChrome`、`__gCrWeb` / `webkit.messageHandlers` / history·print·scrollTo 改写痕迹、iOS 两口径 `window.chrome = undefined`。P1 的面板/API 浏览器口径也已落地（创建/更新/MCP/面板）。同日加固：移动端平台属性面（Android 抹 WebHID + PDF 插件面收敛 + 补 `NetworkInformation.type='wifi'`；iOS 抹 serial/usb/hid/bluetooth/keyboard/MIDI/presentation/relatedApps/vibrate/showOpenFilePicker/performance.memory + 补 GestureEvent 家族）、输入事件层触摸语义（API/RPA 的 click/scroll）与传感器事件流（Android CDP 虚拟传感器 60Hz 数值流；iOS 补 `requestPermission` 权限面并抹除 `absolute`/`ondeviceorientationabsolute`，见 [`移动端大厂检测对抗优化方案.md`](移动端大厂检测对抗优化方案.md) §2.5）；iOS 引擎面二批（iPhone 元素级全屏面删除 + `<video>` webkit 全屏家族 + `-webkit-touch-callout`/`-webkit-overflow-scrolling` + 传感器构造器不可构造 + 罗盘/legacy 初始化器/`createEvent`，CriOS 同 WebKit 口径，见 §2.6）。数据层 225 项、iOS Chrome 真机级 133 项、iOS Safari 118 项、Android 111 项全绿。**2026-10-19 第二批**：Safari 全局面（`WebKitPoint` + `webkitConvertPointFromNodeToPage/PageToNode` + `webkitCancelRequestAnimationFrame` + `WebKitNamespace`/`UserMessageHandlersNamespace`/`UserMessageHandler` 接口对象，P2-11 关闭）与 `window.webkit` 门控裁定（P1-2：源码级确认 Safari 页面世界无该属性、CriOS 有）——B1-6 随之升级为 WebKitNamespace 实例装配（见 §2.1）。回归：数据层 225 项、iOS Chrome 真机级 133 项、iOS Safari 118 项、Android 111 项全绿。**2026-10-19 第三批**：平台 API 差集（P2-12，见方案 §2.8）——窗口/文档/navigator 三处抹除内核暴露、真机不存在的 Chromium 专有面（iOS 38 项清单：通用传感器家族 / 隐私沙盒 / FSA 选择器 / `navigator.xr`·`virtualKeyboard`·`ink` 等；Android 5 项：`EyeDropper`/`FileSystemObserver`/`queryLocalFonts`/`documentPictureInPicture`/`launchQueue`），Worker 全局差集用 `beforeScriptExecution` 插桩断点补删（Worker 接口对象在首脚本执行时才安装，暂停态删除会落空）；CriOS 与 Safari 共用 WebKit 口径清单。未做：B1-7（`__crPostRequestWorkaround`，真机仅在 POST 导航时暴露，需按导航状态条件注入，留待校准）、B1-8（PaymentRequest）、真机抓取校准、`window.chrome` 深掩。**2026-10-19 第四批**：注入会话「断线自愈」（CDP 通道看门狗 + 自动重连 + 全页面重放 + 面板状态可见，见方案 §2.9——修复「通道静默断开后新标签页完全裸奔」的事故形态）、移动端音频口径（强制 `--disable-spoofing=audio`，回到真机精确采样率）、检测报告 14 项裁决（其中 12 项由断线自愈根除、1 项为口径选择、1 项为检测平台探针误报——原生 Chrome 同值）。回归：数据层 225 项、Android 123 项、iOS Safari 130 项、iOS Chrome 145 项全绿。**2026-10-19 第五批**：iOS 引擎面二批（方案 §2.10）——JavaScriptCore 指纹（调用栈格式 / 原生函数 `toString` 多行形态 / `Error` own `line`·`column` 数据属性 / Proxy 透明性）与 Display P3 色域（CDP `setEmulatedMedia` 原生注入，页面与 OOPIF 双路径下发），另抹除 `Intl.v8BreakIterator` 与 `navigator.webkitTemporaryStorage`/`webkitPersistentStorage`。两项引擎层残余不可修：`isV8ErrorSyntax`（15 分，V8 内部抛出的异常在 C++ 侧创建、不过 `Error` 全局陷阱）、`'chrome' in window`（10 分，属性 `configurable: false` 不可删）。`Error.captureStackTrace` 经 WebKit `ErrorConstructor.cpp:52` 核实为 JSC 自有成员，检测站「V8 专有」文案过时，**不抹除**。回归：数据层 251 项、Android 144 项、iOS Safari 160 项、iOS Chrome 175 项全绿。**2026-10-20 第六批**：WebKit 媒体面反向差集（方案 §2.11，P2-13 关闭）——`HTMLMediaElement.prototype` 的 `webkitShowPlaybackTargetPicker` / `webkitSetMediaKeys` / `webkitCurrentPlaybackTargetIsWireless` / `webkitKeys`、`HTMLVideoElement.prototype.webkitPresentationMode`（注意挂载层级：真机只在视频元素上）、可构造的 `WebKitPlaybackTargetAvailabilityEvent`、五个 `WebKitMediaKey*` EME 接口（`WebKitMediaKeys.new` 抛 `NotSupportedError`、`isTypeSupported` 恒 `false`）。此面桌面内核全无，一行 `typeof` 即判「非 WebKit」；真机 Worker 内该面全缺席，故只断言内核不漏出。回归：数据层 251 项、Android 145 项、iOS Safari 168 项、iOS Chrome 183 项全绿。

---

## 0. 总览：两条路线与选择

| 维度 | A：真机 / 云机（IPA 二次开发） | B：本项目仿真层（fp-chromium + CDP） |
|---|---|---|
| 适用场景 | 需要**真实 WebKit 深指纹**（canvas/WebGL/音频/字体天然为真） | 需要**批量可控**、且目标检测止步于页面 JS 层（CriOS 校验） |
| 前置条件 | 越狱设备解密 FairPlay（**硬门槛**，不满足则本线终止） | 无（现有移动端仿真链路已具备） |
| 检测面优势 | `__gCrWeb` 等页面特征**天然为真** | 批量/成本/环境隔离；与面板一体化 |
| 主要代价 | 解密、重签、Google 能力损失、设备与运维成本 | 引擎是 Chromium：WebKit 深特性无法对齐（既知边界） |
| 报告依据 | §4.1 / §4.2 / §5.2 / §8.3 | §4.4 / §8.2 |

> 报告 §8.3 已明确：桌面仿真与 iOS 真机是"两张皮"，同一业务应固定单线、分开评估。本方案两线独立可执行，B 线全部落在本仓库代码内。

---

## 1. 路线 A：真机 / 云机（IPA 二次开发）

### A0 解密（硬前置）

| 步骤 | 方法 | 校验 | 说明 |
|---|---|---|---|
| 提取明文二进制 | 越狱设备 + `bagbak` / `frida-ios-dump` / `dumpdecrypted`（iOS 9~11 时代设备） | `otool -l` 中 `cryptid=0`；`strings` 出现 `__gCrWeb`、`invokeOnHost` | 解密后原签名必然失效，属预期；报告 §5.1 已确认重签不可行、必须先解密 |

### A1 暴露面剥离（推荐 P0，成本极低）

| # | 报告依据 | 补丁动作 | 收益 |
|---|---|---|---|
| 1 | §4.2 Breakpad（`BreakpadURL=https://clients2.google.com/cr/report`、`BreakpadSendAndExit=YES`） | Info.plist：移除 `BreakpadURL` 或置 `BreakpadSendAndExit=NO` | 崩溃时不再把「已加载模块清单」（含注入 dylib）外发 Google（模块清单为推断项，按最坏情况处理） |
| 2 | §4.1 Safe Mode（dyld 枚举 + `SafeModeCrashingModules.plist` 归因） | ① 注入方式**避开** `/Library/MobileSubstrate/DynamicLibraries/`（用 Frida / 自有启动器 `DYLD_INSERT_LIBRARIES` / 直接二进制 patch）；②（可选）清空 `SafeModeCrashingModules.plist` | 不进入黑名单归因列表；即使崩溃也不被点名。注意：该机制**不阻断注入**，只做崩溃归因 + 提示，日常运行无感 |
| 3 | §4.6 网络端点 | 保留默认；如需最小化可在代理层阻断 `clients2.google.com` 等（可选） | 收敛对外暴露面（可选） |

### A2 重签与能力处置（报告 §5.2 落地表）

| 能力 | 重签后的变化 | 处置建议 |
|---|---|---|
| keychain group `com.google.common.SSO` | 组名随新 TeamID 失效 | 接受：Google SSO 需重新登录，不做凭据共享 |
| App Groups `group.com.google.chrome/common` | 容器改变 | 接受：与扩展共享的数据重建 |
| `aps-environment` | 推送证书失效 | 剔除该 entitlement 或自签（推送关闭） |
| Associated Domains `applinks:goo.gl` | 失效 | 接受：短链直跳异常 |
| `LSApplicationQueriesSchemes`（16 个） | 保留即可 | 无成本 |
| FairPlay 完整性 | 解密后不存在 | 产物仅限自用 / 测试环境，不可再上架或公开分发 |

### A3（可选）指纹可控化——仅当业务需要"防关联"而非"真机真指纹"

- **应用层可控面**（免越狱即可）：`WKWebView.customUserAgent`、语言、地理位置、权限、`WKWebsiteDataStore` 每 profile 隔离（Cookie/存储）。
- **深指纹**（canvas/WebGL/音频/字体）在系统 WebKit 内 → 需越狱 + WKUserScript / 进程级 hook（报告 §8.3 路线一）。
  - **红线**：hook 不得破坏 `__gCrWeb` 的结构与可观察性——它本身是真品特征；注入脚本应在保持 `__gCrWeb` 原样的前提下工作。
- 若走"真机真指纹"策略（报告推荐）：A3 只做存储隔离与设备归组。

### A4 部署检查清单（每台设备）

1. 报告 §8.2 探针逐项与真机基准一致（探针原文见附录 A）；
2. 注入方式不触发 Safe Mode 归因路径；
3. 崩溃上报已断（抓包或 plist 核对）；
4. UA 中 OS token 与设备实际 iOS 版本一致（不伪造版本）。

---

## 2. 路线 B：仿真层补齐 CriOS 检测面（本仓库 · 主体）

### 2.0 口径决策：B-now（推荐）vs B-61

| 选项 | 形态 | 评估 |
|---|---|---|
| B-61：逐项复刻 CriOS 61 | UA `CriOS/61.0.3163.73` + iOS 10/11 时代口径 | 仅当目标风控**明确兼容/收录**该老版本；否则 2026 年出现 2017 年的浏览器本身即高危指纹（报告 §8.3 同判） |
| **B-now：以 61 的检测面结构实现"现代 CriOS"（推荐）** | 同一套 `__gCrWeb` 成员与方法改写痕迹；版本号随当代（`CriOS/148.0.0.0`，与内核实测版本一致） | 作为与 Safari 并列的浏览器口径，检测面同构；不引入年代错配 |

落地形式：`mobileExtra.browser` 目前只有 `"chrome"（Android）/ "safari"（iOS）`，新增 `"chrome_ios"`；`derive_spec` 增加 `iosChrome` 分支；注入器仅在页面世界输出 CriOS 补丁。

### 2.1 补丁清单（网站侧检测面 → 实现）

**前置事实**（本次实测，fp-chromium 148；明细见附录 C）：
- `window.chrome` 是 own 属性且 `configurable: false`、`delete` 返回 false → 页面层**无法删除**，只能赋 `undefined`；
- `window.webkit` 内核为 `undefined` → 需注入（**仅 CriOS 口径**：Safari 页面世界按 WebKit 门控 `shouldHaveWebKitNamespaceForWorld` 同样无该属性，保持 `undefined` 即正确形态）；`window.print` / `window.scrollTo` 本就是 window 自有属性 → 改写挂载层级与真机一致；
- `History.prototype.pushState` 为原型级可配置数据属性 → 原型级改写可行。

**总原则**：全部在页面补丁模板 `_PATCH_TEMPLATE` 的 `D.iosChrome` 段实现（含 OOPIF/iframe，随现有 init 脚本下发）；**worker 模板不动**——真机 worker 内同样没有 `__gCrWeb`。
**反直觉点**：真机的 history/print/scrollTo 改写函数 `toString()` **不是** native 外观，而是含 `invokeOnHost` / `setWebViewScrollViewIsDragging` 字样的 **JS 源码文本**；现有 `__maskNative` 会把补丁函数统一伪装成 `[native code]`，此处需要给 mask 增加"自定义源码文本"能力（第三参 `src`），否则 `String(fn)` 检查不通过。

| # | 报告检测点（§4.4） | 真机形态 | 本次补丁 | 位置 | 验证断言 |
|---|---|---|---|---|---|
| B1-1 | `window.__gCrWeb`（最强单点特征） | ~30 成员对象（清单见附录 B）；`windowId` 每窗口唯一 | `defineProperty(window,'__gCrWeb',…)`：windowId（随机唯一串）+ 命名空间（`common/message/console/stringify/getElementFromPoint`、`autofill/suggestion/contextualSearch/languageDetection/translate/findInPage/plugin`）+ 表单族（`findPasswordForms…selectText`）+ `dispatchPopstateEvent/dispatchHashchangeEvent/replaceWebViewURL`；函数给存在性 + 保守 no-op | `_PATCH_TEMPLATE` 新增 `D.iosChrome` 段（建议插在 plugins 段之后） | `'__gCrWeb' in window`；windowId 非空 |
| B1-2 | `history.pushState/replaceState/back/forward/go` 被改写 | 行为透传原生；`toString` 含 `invokeOnHost` | 原型级包装（行为 call-through）+ 注册含 `invokeOnHost` 的源码文本 | `History.prototype` | `/invokeOnHost/.test(String(history.pushState))` 等 5 项 |
| B1-3 | `window.print` 被改写 | `toString` 含 `invokeOnHost` | 同上包装 + 源文本 | `window` | 正则断言 |
| B1-4 | `window.scrollTo` 被改写 | `toString` 含 `setWebViewScrollViewIsDragging` | 同上包装（行为透传）+ 源文本 | `window` | 正则断言 |
| B1-5 | 普通页面**无 `window.chrome`** | `typeof undefined` | `window.chrome = undefined`（实测属性不可删，见前置事实）；**iOS 两模式（safari / chrome_ios）通用**——此为现有 Safari 口径的同款缺口，一并修复 | 模板 iOS 公共段 | `typeof window.chrome === 'undefined'` |
| B1-6 | `window.webkit.messageHandlers` 存在 | 存在（注入桥在**页面世界**注册消息处理器，`JSDOMWindowCustom.cpp:227` 门控才暴露本体）；`messageHandlers` 为 `UserMessageHandlersNamespace` 实例、通道名是 `UserMessageHandler` 实例：已确认 `crwebinvoke`（桥本体，§9.4）+ `POSTSuccessHandler` / `POSTErrorHandler`（其余以真机抓取校准）；`buffers` 为空命名空间；`evaluateScript` / `createJSHandle` 因 world 门控在页面世界**缺席** | 按 WebKit 机制装配：`window.webkit` = `WebKitNamespace` 实例（原型链对齐、描述符 `{writable:false, enumerable:true, configurable:false}` 对齐 JSC 惰性属性）；`messageHandlers` = `UserMessageHandlersNamespace` 实例，具名通道属性 = `UserMessageHandler` 实例（`postMessage` 原生外观、返回 pending Promise——页面世界分支无回执）；`buffers` = 空命名空间 | `D.iosChrome` 段 | 探针 `hasWK` 为 true、keys 含 `crwebinvoke` + `POST*`；实例原型链 / 描述符 / `postMessage` pending Promise / `buffers` 空对象 |
| B1-7 | `__crPostRequestWorkaround` | 函数（POST 导航时启用） | 注入同名函数（存在性 + 最小语义）；P1 | `D.iosChrome` 段 | `typeof === 'function'` |
| B1-8 | `PaymentRequest/PaymentResponse` 为 JS polyfill | polyfill 源码形态 | **P2 可选**：内核自带原生实现，默认接受形态差异 | — | 记录现状 |
| B1-9 | `navigator.vendor = "Apple Computer, Inc."` | WKWebView 默认 | **已具备**（现 iOS 指纹即此值；CriOS 保持不改为 Google，与真机一致） | — | 现有断言 |
| B1-10 | UA：`… CriOS/<ver> …` | `AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/x.y.z.w Mobile/15E148 Safari/604.1` | 指纹库 iOS 分支新增 CriOS UA 构造（reduced 形态 `CriOS/{major}.0.0.0`，镜像 Android 侧 `Chrome/{major}.0.0.0` 与"版本必须与引擎一致"的既有做法）；iPad 变体同规则 | `app/mobile_fingerprints.py` | UA 正则 + 一致性 |
| B1-11 | 无 UA-CH | `navigator.userAgentData` 不存在；不发 `Sec-CH-UA*` | **已具备**（iOS removeProps 含 `userAgentData`；`uaCh=None`，Accept-CH 回送不启用） | — | 现有断言 |
| B1-12 | 触控 / DPR / screen / 空 plugins / 时区 / 语言 | — | 沿用现有 iOS 补丁，全部保留 | — | 既有 56 项基线 |

实现细节备注：

1. `__maskNative(fn, name, src?)`：第三参存在时 `registry.set(fn, src)`；否则维持 `'function {name}() { [native code] }'`。B1-2/3/4 的源文本只需"与真机同判据"：含相应 token 的合理 JS 源码文本即可，逐字节复刻列入 P2 校准。
2. `navigator.standalone`：现有 iOS 公共段定义了 `standalone=false`（Safari 语义，保持不动）；CriOS 是否暴露该属性**待真机校准**——默认 CriOS 模式下不定义，校准后跟进。
3. `__gCrWeb` 成员函数不做 `[native code]` 伪装（真品是脚本源码）；成员全集以证据 04 为基线（附录 B），尾部未展开项与文本细节由真机 dump 二次校准（P2）。
4. 挂载层级：`history` 五方法原型级 / `print`、`scrollTo` window 属主级（与内核现状及真机改写习惯一致）；实例 own vs 原型的深层差异列入 P2 校准。

### 2.2 数据模型与派生改动

| 文件 | 改动 |
|---|---|
| `app/mobile_fingerprints.py` | `create_mobile_fingerprint` 新增浏览器口径参数（默认 `safari`，保持现网格）；iOS 分支 `browser=="chrome_ios"` 时：CriOS UA、`browserVersion`、`mobileExtra["browser"]`；其余字段（vendor/removeProps/emptyPlugins/platform）不变 |
| `app/mobile_cdp_injector.py` | `derive_spec` 识别 `extra.browser`，输出 `"iosChrome": {version:<major>}`；`__maskNative` 增加 `src` 参数；`_PATCH_TEMPLATE` 新增 iOS-Chrome 段 + iOS 公共段补 `window.chrome = undefined`；`build_mobile_patch` 透传 |
| `scripts/mobile_launch_verify.py` | 新增 iOS-Chrome 模式（第三参数 `chrome_ios`，按 `extra.browser` 选择断言组 §2.3） |
| `scripts/mobile_test.py` | 数据层新增断言：CriOS UA 形态、`browser` 字段一致性、`derive_spec.iosChrome`、口径互斥（实际 +13 项） |
| 面板 / API（P1） | 指纹创建入口透传 browser 形参；UI 在 iOS 下增加"Safari / Chrome"选择（不选默认 Safari） |
| `docs/移动端真实指纹库.md` | 覆盖清单/计数/边界同步；iOS Chrome 口径与 window.chrome 边界入表 |

### 2.3 验证（报告 §8.2 探针自动化）

1. `scripts/mobile_launch_verify.py` iOS-Chrome 分支断言：

   | 探针字段 | 期望值 |
   |---|---|
   | `hasGCrWeb` | `true` |
   | `gCrWebWindowId` | 非空字符串 |
   | `hasWK` / `wkHandlers` | `true`；含 `crwebinvoke` + `POSTSuccessHandler`、`POSTErrorHandler`（校准前只断言已确认项） |
   | `historyPatched` / `printPatched` / `scrollPatched` | `true` |
   | `hasChromeObj` | `false`（`typeof` 口径） |
   | `isCriOS` | `true` |
   | `vendor` | `"Apple Computer, Inc."` |
   | `ua` | 与指纹库数据一致 |
   | `maxTouchPoints / devicePixelRatio / screen / tz / languages` | 沿用既有断言 |
   | `pr`（PaymentRequest 来源） | 仅记录现状（形态差异边界，不断言） |

2. 回归：iOS Safari 168 项、Android 145 项、iOS Chrome 183 项（`mobile_launch_verify.py`）、数据层 251 项（`mobile_test.py`）零回退——两模式互斥，不得污染 Safari 口径。
3. 真实站点冒烟：BrowserScan / CreepJS（iOS-Chrome profile 与 Safari profile 各跑一轮，观察无新增红旗）。
4. （可选）真机对照：在越狱真机 CriOS 上跑同一探针取基准，校准通道名 / 挂载层级 / 文本细节（报告 §7 路线图）。

### 2.4 边界与红线

1. **`window.chrome` 深检查无法在页面层掩盖**：实测属性 `configurable: false`，删除/重定义均不可行；赋值 `undefined` 可过 `typeof`/truthiness 检查，但 `'chrome' in window` 与 `getOwnPropertyDescriptor` 会暴露。仅 descriptor 级深检的少数站点会命中，记为边界（继续深掩需内核层改动，P2 评估）。
2. **引擎是 Chromium，声称 CriOS**：WebKit 私有面（`webkit.messageHandlers` 真实回执、Safari 私有 API、CSS/事件行为差异）无法完全对齐——与现有"内核是 Chromium"同级的既知架构边界（报告 §4.4-6 / §8.3 同判）。网络层 TLS/HTTP2 指纹由内核决定，不在本方案与报告检测面内。
3. **`__gCrWeb` 只做存在性与最小语义**，不实现行为副作用（autofill 扫描、选中文本/正文上报宿主、表单读取）——红线：避免引入真实隐私行为，也无需复刻。
4. PaymentRequest 形态差异（原生 vs polyfill）默认接受。
5. **不建议 B-61 老版本口径**作为默认：年代错配为高危指纹；如确有目标场景需要，按"设备档案 + OS + WebKit token + UA"整链配套再评估。
6. 通道名 / 成员全集 / toString 文本 / standalone 等待真机校准项，一律以报告 §7 的动态验证为准（P2）。

---

## 3. 检测面对照矩阵（报告条款 → 补丁 → 验证）

| 报告条款 | 内容 | 归属 | 补丁/动作 | 验证 |
|---|---|---|---|---|
| §4.1 Safe Mode | dyld 枚举 + 崩溃归因黑名单 | A | A1-2（注入方式 + 可选清空 plist） | 注入方式自检 / 崩溃路径演练 |
| §4.2 Breakpad | 崩溃 + 模块清单外发（推断） | A | A1-1 | plist 核对 / 抓包 |
| §4.2 SSO/同步/推送/Safe Browsing | Google 侧功能与遥测 | A | A2 处置表 | 能力自检 |
| §4.4-1 | `__gCrWeb` | B | B1-1 | 探针断言 |
| §4.4-2/3/4 | history / print / scrollTo 改写（含文本标记） | B | B1-2/3/4 + mask 扩展 | 正则断言 |
| §4.4-4 | vendor 矛盾（Apple vs Chrome 品牌） | B | B1-9（保持真机形态） | 现有断言 |
| §4.4-5 | 无 `window.chrome` | B | B1-5 | `typeof` 断言 |
| §4.4-6 | UA CriOS / 版本错配风险 | B | B1-10（B-now 口径） | UA 正则 + 引擎一致性 |
| §4.4-6 | WKWebView 痕迹（webkit） | B | B1-6 | hasWK 断言 |
| §4.4-7 | 表单/文本读取副作用 | B | 不实现（红线） | — |
| §5.2 | 重签能力损失 | A | A2 | 部署检查 |
| §8.2 | 自测探针 | A+B | §2.3 集成 | 脚本全绿 |

---

## 4. 分期实施（本仓库）

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P0**（1~2 天） | B1-1/2/3/4/5/6/10/11 + `mask` 扩展 + B2 + B3 | iOS-Chrome verify 分支全绿；iOS Safari / Android / 数据层零回退；BrowserScan 冒烟无回退 |
| **P1**（0.5~1 天） | B1-7；面板/API browser 选择；文档同步（B1-5 的 Safari 口径修复已并入 P0） | 数据层断言 +6~10；面板可创建 iOS-Chrome 指纹 |

> P1 现状：面板/API/MCP 与文档同步已完成；B1-7 未做（真机仅 POST 导航时暴露，无条件注入反而新增差异，待与 P2 校准一起处理）。
| **P2**（按需） | B1-8；真机抓取校准（通道名/文本/挂载层级/standalone）；`window.chrome` 内核层深掩评估；路线 A（A0~A4）落地执行 | 校准报告 + 探针与真机逐项一致 |

---

## 附录 A：报告 §8.2 探针（原样收录，作为验证基线）

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
    // 传感器权限面（WebKit 专有：iOS 14.5+ 才有；Chromium 到 152 才实现）
    sensorPermission: typeof (window.DeviceMotionEvent || {}).requestPermission,
    sensorAbsoluteProto: (typeof DeviceOrientationEvent === 'function')
      && ('absolute' in DeviceOrientationEvent.prototype),
    sensorAbsEvent: 'ondeviceorientationabsolute' in window,
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

---

## 附录 B：`__gCrWeb` 成员清单（证据 04 整理）

| 分组 | 成员 |
|---|---|
| 基础 | `windowId`（每窗口唯一）、`common`、`message`、`console`、`stringify`、`getElementFromPoint` |
| 命名空间 | `autofill`、`suggestion`、`contextualSearch`、`languageDetection`、`translate`、`findInPage`、`plugin` |
| 表单/密码族 | `findPasswordForms`、`fillPasswordForm`、`fillPasswordFormWithGeneratedPassword`、`findMatchingPasswordForms`、`isAutofilled`、`setAutofilled`、`clearAutofilledPasswords`、`getPasswordForm`、`getPasswordFormData`、`getPasswordFormDataList`、`getElementByNameWithParent`、`selectText` |
| 事件/导航 | `dispatchPopstateEvent`、`dispatchHashchangeEvent`、`replaceWebViewURL` |

> 证据 04 对 `web_bundle.js` 的尾部仅以 `…` 列举（"约 30 成员"）；实现按本表为基线，尾部成员与函数文本以真机抓取二次校准（P2）。

---

## 附录 C：新增实测记录（2026-10-18，fp-chromium 148.0.7778.215）

```js
// 探针 1：window.chrome 可删性
Object.getOwnPropertyDescriptor(window, 'chrome');
// → { configurable: false, writable: true, enumerable: true }（own data 属性）
delete window.chrome;            // → false（无法删除）
window.chrome = undefined;       // → typeof window.chrome === 'undefined'（可过 §8.2 hasChromeObj）
// 残余暴露：'chrome' in window === true / getOwnPropertyDescriptor 仍可读到（页面层无法掩盖）

// 探针 2：改写目标的挂载层级
typeof window.webkit;            // → "undefined"（内核无；CriOS 需注入，Safari 保持 undefined）
Object.prototype.hasOwnProperty.call(window, 'print');     // → true（window 属主级）
Object.prototype.hasOwnProperty.call(window, 'scrollTo');  // → true（window 属主级）
Object.getOwnPropertyDescriptor(History.prototype, 'pushState');
// → { configurable: true, writable: true, type: 'data' }（原型级可改写）

// 探针 3：内核伪装开关（fp_chromium_kernel）
// --disable-spoofing 支持项：font / audio / canvas / clientrects / gpu
// （无与 window.chrome 或 WK 痕迹相关的内核级开关 → B1-5 只能走页面层降级方案）
```

---

*方案生成：2026-10-18 ｜ 依据报告与全部证据见 [`chrome61-analysis/`](chrome61-analysis/00-README.md)*
