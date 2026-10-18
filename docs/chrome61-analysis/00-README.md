# Chrome 61 IPA 分析 - 证据文件索引

> 本目录是 [`../Chrome61-IPA检测机制分析.md`](../Chrome61-IPA检测机制分析.md) 的原始证据存档。
> 分析样本：`/Users/admin/Downloads/Chrome-v61-3163-73.ipa`
> 分析环境：macOS（Xcode 命令行工具：otool / codesign / plutil / strings / file）
> 分析日期：2026-10-18

| 文件 | 内容 |
|---|---|
| `01-identity-hashes.txt` | 样本哈希、身份信息、购买元数据、签名与 DRM 原始输出 |
| `02-entitlements.txt` | 主 App 与两个扩展的 entitlements |
| `03-linked-frameworks.txt` | 主二进制链接的全部框架（含各能力的对应关系） |
| `04-user-scripts-injection.txt` | 16 个注入网页的用户脚本清单（全局对象 / 宿主命令） |
| `05-webui-pages.txt` | `chrome://` 内部页面清单与 `chrome://version` 字段 |
| `06-telemetry-strings-en-zh.txt` | 遥测/崩溃/安全模式字符串（中英文原文） |
| `07-file-inventory.txt` | `stable.app` 顶层文件清单（含大小） |
| `08-safe-mode-jailbreak-detection.txt` | Safe Mode（越狱注入检测）专项证据 |
| `09-device-apis-symbols.txt` | 设备信息类 API 的导入符号/类引用证据 |
| `10-reproduce-commands.sh` | 复现全部结论的命令集合 |

提取出来的中间产物（pak 解包等）位于分析工作目录，不随项目提交。
