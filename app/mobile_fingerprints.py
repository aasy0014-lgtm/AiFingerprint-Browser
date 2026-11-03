"""移动端真实机型指纹库（Android/iOS）

数据文件: app/data/mobile_devices.json
- 机型档案: 屏幕(物理/CSS/视口)、DPR、GPU(真实 WebGL 上报字符串)、CPU/内存、
  可运行系统版本及在网权重、机型标识（经 UA-CH `Sec-CH-UA-Model` 下发；
  Android UA 串自 Chrome 110 起已缩减，不再含机型/系统版本）
- 版本库: Chrome 主版本 → 完整构建号（Android UA/UA-CH 使用）

生成链路：按权重抽取机型 → 按机型可运行版本配对系统版本 → 配对浏览器版本，
保证 UA / UA-CH / 屏幕 / DPR / GPU / 硬件并发 全部相互印证。

模式:
- preset:  真实机型预设（系统与浏览器版本取该机型的当前主流组合，不随机化）
- generate: 基于真实机型库的合成（系统/浏览器版本按在网权重随机）

运行期（fp-chromium 内核 + CDP 注入）：
- 屏幕/D如下 PR/触控由 CDP 设备仿真落地
- GPU 字符串、Android UA-CH、deviceMemory、Battery、iOS 特性差异由 JS 注入层落地
  （见 app/mobile_cdp_injector.py，注入值全部取自本模块生成的 fingerprint 数据）

目标 OS: android, ios
"""
import json
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_LIBRARY_PATH = Path(__file__).resolve().parent / "data" / "mobile_devices.json"

_SEED_MAX = 4_294_967_294


# ------------------------------------------------------------------ 数据加载
def _load_library() -> Dict[str, Any]:
    try:
        with open(_LIBRARY_PATH, "r", encoding="utf-8") as fh:
            lib = json.load(fh)
    except FileNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(f"移动端机型库缺失: {_LIBRARY_PATH}") from exc
    devices = lib.get("devices") or []
    if not devices:
        raise RuntimeError(f"移动端机型库为空: {_LIBRARY_PATH}")
    for dev in devices:
        missing = [k for k in ("brand", "platform", "type", "model", "screen", "gpu", "weight") if k not in dev]
        if missing:
            raise RuntimeError(f"机型记录缺字段 {missing}: {dev.get('market_name', dev)}")
        screen = dev["screen"]
        for k in ("w", "h", "dpr", "css_w", "css_h", "viewport_h"):
            if k not in screen:
                raise RuntimeError(f"机型 {dev['market_name']} 屏幕字段缺失: {k}")
    return lib


_LIBRARY: Dict[str, Any] = _load_library()

_DEVICES: List[Dict[str, Any]] = list(_LIBRARY["devices"])
_ANDROID_BROWSERS: List[Dict[str, Any]] = list(_LIBRARY.get("android_browsers") or [])


# ------------------------------------------------------------------ 工具
def _weighted_choice(weights: Dict[str, Any]) -> str:
    """按 {值: 权重} 抽取（保持 JSON 中的插入顺序作为同权重兜底顺序）"""
    keys = list(weights.keys())
    w = [max(0.0001, float(weights[k])) for k in keys]
    return random.choices(keys, weights=w, k=1)[0]


def _version_key(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).split("."))


def _memory_class(ram_gb: int) -> int:
    """Chrome navigator.deviceMemory 上报桶: 取不超过物理内存的 2 的幂，上限 8。"""
    for cap in (8, 4, 2, 1):
        if ram_gb >= cap:
            return cap
    return 1


def _device_identifier_matches(device: Dict[str, Any], device_model: str) -> bool:
    """匹配用户可见机型名或设备型号代码（大小写不敏感）。"""
    wanted = str(device_model or "").strip().casefold()
    if not wanted:
        return False
    return wanted in {
        str(device.get("market_name") or "").strip().casefold(),
        str(device.get("model") or "").strip().casefold(),
    }


def find_device(target_os: str, device_model: str,
                manufacturer: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """按平台、可选品牌和具体机型查找档案，供 API 校验与生成共用。"""
    if target_os not in ("android", "ios") or not str(device_model or "").strip():
        return None
    for device in _DEVICES:
        if device["platform"] != target_os or not _device_identifier_matches(device, device_model):
            continue
        if manufacturer and str(device["brand"]).casefold() != str(manufacturer).strip().casefold():
            continue
        return device
    return None


def _pick_device(target_os: str, manufacturer: Optional[str],
                 device_model: Optional[str] = None) -> Dict[str, Any]:
    """按平台/品牌/具体机型筛选；未指定具体机型时按权重抽取。"""
    pool = [d for d in _DEVICES if d["platform"] == target_os]
    if not pool:
        raise ValueError(f"机型库中没有平台 {target_os} 的设备")
    if manufacturer:
        matched = [d for d in pool if str(d["brand"]).lower() == manufacturer.lower()]
        if matched:
            pool = matched
    if device_model:
        matched = [d for d in pool if _device_identifier_matches(d, device_model)]
        if not matched:
            raise ValueError(f"机型库中不存在具体机型 {device_model}（{target_os}）")
        return matched[0]
    return random.choices(pool, weights=[max(0.0001, float(d["weight"])) for d in pool], k=1)[0]


# Android 的 CSS `color-gamut` 是显示面板能力，不是 GPU/渲染器能力。
# 设备库目前没有把它作为必填 SSOT 字段（历史档案也因此没有该键），所以
# 运行期先接受可选的 `display_gamut`/`displayGamut` 覆盖；对已经在库中的
# Pixel 8 系列保留一条有来源的兜底事实：Actua OLED 的默认 Adaptive 模式
# 为 DCI-P3。其余机型在没有显式面板证据时按 sRGB，避免把「旗舰 OLED」
# 推断成 P3，导致色彩模式与真机不一致。将来补齐数据字段后，显式值会优先。
_ANDROID_P3_DISPLAY_MODELS = frozenset({"Pixel 8", "Pixel 8 Pro"})


def _android_display_gamut(device: Dict[str, Any]) -> str:
    """返回 Android profile 的显示色域档位（``p3`` 或 ``srgb``）。"""
    raw = device.get("display_gamut", device.get("displayGamut"))
    value = str(raw or "").strip().lower().replace("-", "")
    if value in {"p3", "displayp3", "dcip3"}:
        return "p3"
    if value in {"srgb", "rgb", "standard"}:
        return "srgb"
    name = str(device.get("market_name") or device.get("model") or "").strip()
    return "p3" if name in _ANDROID_P3_DISPLAY_MODELS else "srgb"


def _pick_os_version(device: Dict[str, Any], mode: str) -> str:
    versions = device.get("os_versions") or device.get("ios_versions") or {}
    if not versions:
        raise RuntimeError(f"机型 {device['market_name']} 缺少系统版本池")
    if mode == "preset":
        # 当前主流：权重最高；同权重取版本号更高者（更新更常见）
        return max(versions.items(), key=lambda kv: (float(kv[1]), _version_key(kv[0])))[0]
    return _weighted_choice(versions)


def _kernel_browser() -> Optional[Dict[str, Any]]:
    """内核实测的 Chromium 版本（首选）。

    UA / UA-CH 声称的 Chrome 版本必须与内核引擎一致：检测站会做
    feature 探测（引擎实际版本）与 UA 版本比对，声称 156 而引擎是 148
    会直接命中「浏览器版本不同」。内核版本随安装包变化，这里动态读取，
    未安装内核时回退到库内版本池（仅用于无内核环境生成指纹）。
    """
    try:
        from .kernels import fp_chromium_kernel
        full = fp_chromium_kernel.chrome_version()
    except Exception:
        full = None
    if not full:
        return None
    return {"major": full.split(".")[0], "full": full, "weight": 1.0,
            "source": "kernel"}


def _pick_android_browser(mode: str) -> Dict[str, Any]:
    kernel = _kernel_browser()
    if kernel:
        return kernel
    if not _ANDROID_BROWSERS:
        return {"major": "156", "full": "156.0.8078.35", "weight": 1}
    if mode == "preset":
        return max(_ANDROID_BROWSERS, key=lambda b: (float(b["weight"]), _version_key(b["full"])))
    return random.choices(
        _ANDROID_BROWSERS,
        weights=[max(0.0001, float(b["weight"])) for b in _ANDROID_BROWSERS],
        k=1,
    )[0]


def _pick_ios_browser() -> Dict[str, Any]:
    """Chrome iOS（CriOS）版本：与内核实测引擎一致（与 Android 侧同法）。

    Chrome iOS 的大版本与桌面 Chrome 同步演进（120 ↔ 120），因此直接复用
    「内核版本优先」策略，避免 UA 声称版本与引擎特性错位被检测站命中。
    未安装内核时回退到库内 Chrome 版本池（仅用于无内核环境生成指纹）。
    """
    kernel = _kernel_browser()
    if kernel:
        return kernel
    if _ANDROID_BROWSERS:
        fb = max(_ANDROID_BROWSERS, key=lambda b: (float(b["weight"]), _version_key(b["full"])))
        return {"major": fb["major"], "full": fb["full"], "weight": 1.0, "source": "library"}
    return {"major": "156", "full": "156.0.8078.35", "weight": 1.0, "source": "fallback"}


def _normalize_browser(target_os: str, browser: Optional[str]) -> str:
    """归一化浏览器口径：Android 恒为 chrome；iOS 为 safari / chrome_ios。"""
    value = (browser or "").strip().lower()
    if target_os == "android":
        if value in ("", "chrome"):
            return "chrome"
        raise ValueError(f"Android 端浏览器口径仅支持 chrome（收到 {browser}）")
    if value in ("", "safari"):
        return "safari"
    if value in ("chrome", "chrome_ios"):
        return "chrome_ios"
    raise ValueError(f"iOS 端浏览器口径仅支持 safari / chrome_ios（收到 {browser}）")


# Chromium 的 UA-CH 品牌表由 major version 派生，不是常量
# （components/embedder_support/user_agent_utils.cc:214-256、535-551）：
#   * GREASE 品牌 = "Not" + c[seed%11] + "A" + c[(seed+1)%11] + "Brand"，
#     版本 = ["8","99","24"][seed%3]，seed 就是 major version；
#   * 三项品牌再用 GetRandomOrder(seed, 3) 打乱顺序。
# 真机 Chrome 156 实测 ['Not:A-Brand/8','Chromium/156','Google Chrome/156']，
# 与本函数输出逐字一致。写死成固定 GREASE 串（早期库里存的是
# Not/A)Brand/99）会同时错掉品牌名、版本与顺序三项——检测站拿到低熵
# brands 就能直接比对。
_UA_CH_GREASE_CHARS = (" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_")
_UA_CH_GREASE_VERSIONS = ("8", "99", "24")
_UA_CH_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))


def _ua_ch_brands(major: str, version: str, version_type: str) -> List[Dict[str, str]]:
    """按 Chromium 的 seed 规则生成 UA-CH 品牌表（低熵 brands / 高熵 fullVersionList）。"""
    seed = int(major)
    greasey_brand = "Not" + _UA_CH_GREASE_CHARS[seed % 11] + "A" + _UA_CH_GREASE_CHARS[(seed + 1) % 11] + "Brand"
    greasey_version = _UA_CH_GREASE_VERSIONS[seed % 3]
    if version_type == "full":
        greasey_version += ".0.0.0"
    base = [(greasey_brand, greasey_version),
            ("Chromium", version),
            ("Google Chrome", version)]
    order = _UA_CH_ORDERS[seed % len(_UA_CH_ORDERS)]
    shuffled: List[Dict[str, str]] = [{"brand": "", "version": ""} for _ in base]
    for i, target in enumerate(order):
        shuffled[target] = {"brand": base[i][0], "version": base[i][1]}
    return shuffled


# UA 缩减（Chrome 110 全量；Android M110+ 2023-05-11 起 100%）：
# 真机 UA 串里的 OS 版本与机型被**冻结**成字面量，真实值只走高熵提示
# Sec-CH-UA-Platform-Version / Sec-CH-UA-Model。
#   * UA 段：`Linux; Android 10; K`
#     （components/embedder_support/user_agent_utils.cc GetUnifiedPlatform()
#     的 `return "Linux; Android 10; K";`）
#   * navigator.platform：`Linux armv81`，硬编码、不按 ABI 取值
#     （third_party/blink/renderer/core/execution_context/navigator_base.cc
#     GetReducedNavigatorPlatform()；仅 Android desktop/XR 且启用
#     kAndroidDesktopUASpoofAsChromeOS 时才是 `Linux x86_64`）
# 两处取值都以 152.0.7977.82 tag 逐字核对。生成端、内核命令行与运行期注入
# 三处必须同源，否则会出现「UA 已缩减、platform 仍按 ABI」的跨面矛盾。
ANDROID_REDUCED_UA_PLATFORM = "Linux; Android 10; K"
ANDROID_REDUCED_NAV_PLATFORM = "Linux armv81"


def _reduced_android_ua(ua: Any) -> str:
    """把 Android UA 收敛成真机的缩减形态（只重写括号内 platform 段）。

    存量档案存的是缩减前串（`Linux; Android 16; <Build.MODEL>`）；运行期与
    内核命令行都走这里改写，其余（AppleWebKit / Mobile / Safari 段）原样保留。
    """
    text = str(ua or "")
    if not text:
        return text
    return re.sub(r"\(Linux; Android [^)]*\)",
                  f"({ANDROID_REDUCED_UA_PLATFORM})", text, count=1)


def _android_ua_ch(device: Dict[str, Any], os_version: str, browser: Dict[str, Any]) -> Dict[str, Any]:
    """Chrome Android 的 navigator.userAgentData（含高熵值）。"""
    major = str(browser["major"])
    full = str(browser["full"])
    return {
        "brands": _ua_ch_brands(major, major, "major"),
        "fullVersionList": _ua_ch_brands(major, full, "full"),
        "fullVersion": full,
        "platform": "Android",
        "platformVersion": f"{os_version}.0.0",
        # Chromium 源码行为：Android 手机端 architecture / bitness 恒为空字符串
        # （components/embedder_support/user_agent_utils.cc：GetCpuArchitecture /
        # GetCpuBitness 在 IS_ANDROID 且非桌面/XR 设备上返回 ""）。
        # 填 "arm" 会与真机组合不符，服务端 OS 一致性检查可据此判定为桌面系统。
        "architecture": "",
        "bitness": "",
        "model": str(device["model"]),
        "mobile": device["type"] == "phone",
        # F-97：Chrome 124+ 的 Sec-CH-UA-Form-Factors 高熵提示。
        "formFactors": ["Tablet"] if device["type"] == "tablet" else ["Mobile"],
        "wow64": False,
    }


# ------------------------------------------------------------------ 字体
_MOBILE_FONTS = {
    # iOS 侧按 Apple「Fonts for Apple platforms」页的 iOS 标注重写
    # （system font iOS / document support iOS，抓取于 2026-10）。
    #
    # 早先这里混着两类不存在的族：
    #   * 标注为 downloadable iOS 的（Tahoma / Comic Sans MS / Bookman /
    #     Lucida Console / Monaco / Lucida Grande）：iOS 出厂不带，要在
    #     设置里手动装。声明了就等于放行抢占，macOS 宿主的同名族照样解析
    #     ——Monaco 在真机上量不到，仿真却量得到，是最明显的一条。
    #   * 宿主 macOS 上根本没有的（SF Pro Text / SF Pro Icons /
    #     ITC Avant Garde Gothic / Sand / System Font / Arial MT /
    #     Georgia MT）：本地() 探测本来就落空，声明它们只是噪音。
    # 保留下来的都是 iOS 真机预装的族，见 _IOS_DEVICE_FONTS。
    "ios": {
        "essential": [
            'Helvetica Neue', 'Arial', 'Times New Roman', 'Courier New',
            'Georgia', 'Verdana', 'Palatino', 'Trebuchet MS',
            'Menlo', 'Helvetica', 'PingFang SC', 'PingFang HK',
        ],
        "extra": [
            'Chalkboard SE', 'Didot', 'Apple SD Gothic Neo',
            'Avenir', 'Avenir Next', 'Baskerville', 'Futura', 'Optima',
            'Hiragino Sans', 'Hiragino Kaku Gothic ProN', 'Apple Color Emoji',
        ],
    },
    # Android 侧只列 **AOSP 预装的族名**，取自
    # platform/frameworks/base/data/fonts/{fonts.xml,alias.json} 与同目录的
    # 字体文件清单（Roboto-Regular / DroidSansMono / NotoSerif / NotoSans* /
    # SourceSansPro / CutiveMono / ComingSoon / DancingScript /
    # CarroisGothicSC / NotoColorEmoji / NotoSansSymbols / RobotoFlex）。
    #
    # 早先这里放的是 Open Sans / Montserrat / Lato / Merriweather / Poppins /
    # PT Sans / PT Serif 等**网页字体**——Android 系统里没有这些文件。后果
    # 不是「多报几个字体」这么轻：声明表同时是 F-93③ 的放行名单，声明了
    # 就不抢占该族名。PT Sans / PT Serif 恰好是宿主 macOS 预装的（macOS 带
    # 的 PT 系），于是 BrowserLeaks 的 3564 名扫描把它们量成「存在」，与
    # Android 机型自相矛盾。按 AOSP 清单重写后这些族回到抢占列表，扫描结果
    # 里不再出现宿主专有族。
    #
    # 通用族别名（Arial/Helvetica/Tahoma/Verdana/Times/Georgia/Courier/
    # Palatino/Monaco/Baskerville）**故意不列**：真机上它们由 alias.json 解析
    # 到 sans-serif/serif/monospace，而我们没法让一个族名解析到宿主通用族
    # （抢占只能把族名变成「不存在」）。列进来会让 macOS 的原生 Arial 字形
    # 顶上去——那正是 F-93③ 要挡的宿主字形。
    "android": {
        "essential": [
            # AOSP 的 /system/fonts 自带 DroidSansMono.ttf，族名就是
            # "Droid Sans Mono"（与 "Droid Sans" 是两个族）。creepjs 的
            # ANDROID_FONTS 用它判定 Android，漏了会让 FontFace 探测得到 0/51。
            # Dancing Script / Noto Color Emoji 同理在 AOSP 清单里，且
            # creepjs 会探测。
            'Roboto', 'Droid Sans Mono', 'Dancing Script', 'Noto Color Emoji',
            'Noto Sans', 'Noto Serif', 'Source Sans Pro',
        ],
        "extra": [
            'Cutive Mono', 'Coming Soon', 'Carrois Gothic SC',
            'Noto Sans SC', 'Noto Sans Symbols', 'Roboto Flex',
        ],
    },
}

# ------------------------------------------------------------------ iOS 设备字体白名单
# F-93③ 的放行名单：注入器把宿主 macOS 上按名字可解析、但 iPhone 上**没有**的
# 族名逐个用同名空 FontFace 抢占，让字体探测落空。判据是清单里那句「宿主有、
# 设备没有」——所以这里列的是 iPhone 真机预装、可以照常解析的族。
#
# 数据源：Apple「Fonts for Apple platforms」页里标注为 `system font iOS` 或
# `document support iOS` 的族（抓取于 2026-10）。标注为 `downloadable iOS` 的
# **不列**——那些要在设置里手动装，出厂没有。
#
# 两条实测出来的细节：
#   * 该标注对 CJK **不可信**：PingFang SC/TC/HK、Songti、Kaiti、Heiti 这些
#     iOS 默认中文字体全被标成 downloadable（Apple 页把它们当可选下载项列）。
#     所以 CJK 单独手列在 _IOS_CJK_FAMILIES，不依赖标注。
#   * 宿主枚举出来的是 PostScript 名（STSongti-SC-Regular、PingFangHK-Light、
#     .Hiragino Sans GB Interface），Apple 只列族名+样式名，两者不同构。
#     CJK 用前缀匹配覆盖这些变体，拉丁族必须精确匹配——用 `arial` 前缀会把
#     macOS 专有的 Arial Narrow / Arial Unicode MS 一起放行。
_IOS_DEVICE_FONTS = (
    'Academy Engraved LET Plain:1.0', 'Al Nile', 'Al Nile Bold', 'American Typewriter',
    'American Typewriter Bold', 'American Typewriter Condensed', 'American Typewriter Condensed Bold',
    'American Typewriter Condensed Light', 'American Typewriter Light', 'American Typewriter Semibold',
    'Apple Color Emoji', 'Apple SD Gothic Neo Bold', 'Apple SD Gothic Neo Light',
    'Apple SD Gothic Neo Medium', 'Apple SD Gothic Neo Regular', 'Apple SD Gothic Neo SemiBold',
    'Apple SD Gothic Neo Thin', 'Apple SD Gothic Neo UltraLight', 'Apple Symbols',
    'AppleGothic Regular', 'Arial', 'Arial Bold', 'Arial Bold Italic', 'Arial Hebrew',
    'Arial Hebrew Bold', 'Arial Hebrew Light', 'Arial Italic', 'Arial Rounded MT Bold',
    'Asphalt Medium', 'Athelas Bold', 'Athelas Bold Italic', 'Athelas Italic',
    'Athelas Regular', 'Avenir Black', 'Avenir Black Oblique', 'Avenir Book', 'Avenir Book Oblique',
    'Avenir Heavy', 'Avenir Heavy Oblique', 'Avenir Light', 'Avenir Light Oblique',
    'Avenir Medium', 'Avenir Medium Oblique', 'Avenir Next Bold', 'Avenir Next Bold Italic',
    'Avenir Next Condensed Bold', 'Avenir Next Condensed Bold Italic', 'Avenir Next Condensed Demi Bold',
    'Avenir Next Condensed Demi Bold Italic', 'Avenir Next Condensed Heavy', 'Avenir Next Condensed Heavy Italic',
    'Avenir Next Condensed Italic', 'Avenir Next Condensed Medium', 'Avenir Next Condensed Medium Italic',
    'Avenir Next Condensed Regular', 'Avenir Next Condensed Ultra Light', 'Avenir Next Condensed Ultra Light Italic',
    'Avenir Next Demi Bold', 'Avenir Next Demi Bold Italic', 'Avenir Next Heavy',
    'Avenir Next Heavy Italic', 'Avenir Next Italic', 'Avenir Next Medium', 'Avenir Next Medium Italic',
    'Avenir Next Regular', 'Avenir Next Ultra Light', 'Avenir Next Ultra Light Italic',
    'Avenir Oblique', 'Avenir Roman', 'Balega Regular', 'Baskerville', 'Baskerville Bold',
    'Baskerville Bold Italic', 'Baskerville Italic', 'Baskerville SemiBold', 'Baskerville SemiBold Italic',
    'Bebas Neue', 'Bodoni 72 Oldstyle Bold', 'Bodoni 72 Oldstyle Book', 'Bodoni 72 Oldstyle Book Italic',
    'Bodoni 72 Smallcaps Book', 'Bodoni Ornaments', 'Bradley Hand Bold', 'Carlito',
    'Carlito Bold', 'Carlito Bold Italic', 'Carlito Italic', 'Chalkboard SE Bold',
    'Chalkboard SE Light', 'Chalkboard SE Regular', 'Chalkduster', 'Charter Black',
    'Charter Black Italic', 'Charter Bold', 'Charter Bold Italic', 'Charter Italic',
    'Charter Roman', 'Cochin', 'Cochin Bold', 'Cochin Bold Italic', 'Cochin Italic',
    'Copperplate', 'Copperplate Bold', 'Copperplate Light', 'Courier', 'Courier Bold',
    'Courier Bold Oblique', 'Courier New', 'Courier New Bold', 'Courier New Bold Italic',
    'Courier New Italic', 'Courier Oblique', 'Damascus Bold', 'Damascus Light',
    'Damascus Medium', 'Damascus Regular', 'Damascus Semi Bold', 'DB LCD Temp Black',
    'Dear Joe Four Small Semibold', 'Devanagari Sangam MN', 'Devanagari Sangam MN Bold',
    'Didot', 'Didot Bold', 'Didot Italic', 'DIN Alternate Bold', 'DIN Condensed Bold',
    'Druk Bold', 'Druk Bold Italic', 'Druk Heavy', 'Druk Heavy Italic', 'Druk Medium',
    'Druk Medium Italic', 'Druk Super', 'Druk Super Italic', 'Druk Text Bold',
    'Druk Wide Bold', 'Druk Wide Bold Italic', 'Druk Wide Medium', 'Druk Wide Medium Italic',
    'Euphemia UCAS', 'Euphemia UCAS Bold', 'Euphemia UCAS Italic', 'Fakt Slab Stencil Pro Medium',
    'Farah Regular', 'Futura Bold', 'Futura Condensed ExtraBold', 'Futura Condensed Medium',
    'Futura Medium', 'Futura Medium Italic', 'Galvji', 'Galvji Bold', 'Geeza Pro Bold',
    'Geeza Pro Regular', 'Georgia', 'Georgia Bold', 'Georgia Bold Italic', 'Georgia Italic',
    'Gill Sans', 'Gill Sans Bold', 'Gill Sans Bold Italic', 'Gill Sans Italic',
    'Gill Sans Light', 'Gill Sans Light Italic', 'Gill Sans SemiBold', 'Gill Sans SemiBold Italic',
    'Gill Sans UltraBold', 'Grantha Sangam MN Bold', 'Grantha Sangam MN Regular',
    'Helvetica', 'Helvetica Bold', 'Helvetica Bold Oblique', 'Helvetica Light',
    'Helvetica Light Oblique', 'Helvetica Neue', 'Helvetica Neue Bold', 'Helvetica Neue Bold Italic',
    'Helvetica Neue Condensed Black', 'Helvetica Neue Condensed Bold', 'Helvetica Neue Italic',
    'Helvetica Neue Light', 'Helvetica Neue Light Italic', 'Helvetica Neue Medium',
    'Helvetica Neue Medium Italic', 'Helvetica Neue Thin', 'Helvetica Neue Thin Italic',
    'Helvetica Neue UltraLight', 'Helvetica Neue UltraLight Italic', 'Helvetica Oblique',
    'Hiragino Kaku Gothic Pro W3', 'Hiragino Kaku Gothic Pro W6', 'Hiragino Kaku Gothic ProN W3',
    'Hiragino Kaku Gothic ProN W6', 'Hiragino Kaku Gothic Std W8', 'Hiragino Kaku Gothic StdN W8',
    'Hiragino Maru Gothic Pro W4', 'Hiragino Maru Gothic ProN W4', 'Hiragino Mincho Pro W3',
    'Hiragino Mincho Pro W6', 'Hiragino Mincho ProN W3', 'Hiragino Mincho ProN W6',
    'Hiragino Sans W3', 'Hiragino Sans W4', 'Hiragino Sans W5', 'Hiragino Sans W6',
    'Hiragino Sans W7', 'Hiragino Sans W8', 'Hoefler Text', 'Hoefler Text Black',
    'Hoefler Text Black Italic', 'Hoefler Text Italic', 'Impact', 'Iowan Old Style Bold',
    'Iowan Old Style Bold Italic', 'Iowan Old Style Italic', 'Iowan Old Style Roman',
    'Journal Sans New Inline', 'Kailasa Bold', 'Kailasa Regular', 'Kefa Regular',
    'Khmer Sangam MN', 'Kohinoor Bangla', 'Kohinoor Bangla Light', 'Kohinoor Bangla Semibold',
    'Kohinoor Devanagari Light', 'Kohinoor Devanagari Regular', 'Kohinoor Devanagari Semibold',
    'Kohinoor Gujarati Bold', 'Kohinoor Gujarati Light', 'Kohinoor Gujarati Regular',
    'Kohinoor Telugu', 'Kohinoor Telugu Light', 'Kohinoor Telugu Medium', 'Lao Sangam MN',
    'Malayalam Sangam MN', 'Malayalam Sangam MN Bold', 'Marion Bold', 'Marion Italic',
    'Marion Regular', 'Marker Felt Thin', 'Marker Felt Wide', 'Menlo Bold', 'Menlo Bold Italic',
    'Menlo Italic', 'Menlo Regular', 'Mishafi Regular', 'MuktaMahee Bold', 'MuktaMahee Light',
    'MuktaMahee Regular', 'Myanmar Sangam MN', 'Myanmar Sangam MN Bold', 'Noteworthy Bold',
    'Noteworthy Light', 'Noto Nastaliq Urdu', 'Noto Nastaliq Urdu Bold', 'Noto Sans Adlam Regular',
    'Noto Sans Armenian Bold', 'Noto Sans Armenian Light', 'Noto Sans Armenian Regular',
    'Noto Sans Avestan Regular', 'Noto Sans Bamum Regular', 'Noto Sans Bassa Vah Regular',
    'Noto Sans Batak Regular', 'Noto Sans Bhaiksuki Regular', 'Noto Sans Brahmi Regular',
    'Noto Sans Buginese Regular', 'Noto Sans Buhid Regular', 'Noto Sans Canadian Aboriginal Regular',
    'Noto Sans Carian Regular', 'Noto Sans Caucasian Albanian Regular', 'Noto Sans Chakma Regular',
    'Noto Sans Cham Regular', 'Noto Sans Coptic Regular', 'Noto Sans Cuneiform Regular',
    'Noto Sans Cypriot Regular', 'Noto Sans Duployan Regular', 'Noto Sans Egyptian Hieroglyphs Regular',
    'Noto Sans Elbasan Regular', 'Noto Sans Glagolitic Regular', 'Noto Sans Gothic Regular',
    'Noto Sans Gunjala Gondi Regular', 'Noto Sans Hanifi Rohingya Regular', 'Noto Sans Hanunoo Regular',
    'Noto Sans Hatran Regular', 'Noto Sans Imperial Aramaic Regular', 'Noto Sans Inscriptional Pahlavi Regular',
    'Noto Sans Inscriptional Parthian Regular', 'Noto Sans Javanese Regular', 'Noto Sans Kaithi Regular',
    'Noto Sans Kannada Bold', 'Noto Sans Kannada Light', 'Noto Sans Kannada Regular',
    'Noto Sans Kayah Li Regular', 'Noto Sans Kharoshthi Regular', 'Noto Sans Khojki Regular',
    'Noto Sans Khudawadi Regular', 'Noto Sans Lepcha Regular', 'Noto Sans Limbu Regular',
    'Noto Sans Linear A Regular', 'Noto Sans Linear B Regular', 'Noto Sans Lisu Regular',
    'Noto Sans Lycian Regular', 'Noto Sans Lydian Regular', 'Noto Sans Mahajani Regular',
    'Noto Sans Mandaic Regular', 'Noto Sans Manichaean Regular', 'Noto Sans Marchen Regular',
    'Noto Sans Masaram Gondi Regular', 'Noto Sans Meetei Mayek Regular', 'Noto Sans Mende Kikakui Regular',
    'Noto Sans Meroitic Regular', 'Noto Sans Miao Regular', 'Noto Sans Modi Regular',
    'Noto Sans Mongolian', 'Noto Sans Mro Regular', 'Noto Sans Multani Regular',
    'Noto Sans Myanmar Bold', 'Noto Sans Myanmar Light', 'Noto Sans Myanmar Regular',
    'Noto Sans Nabataean Regular', 'Noto Sans New Tai Lue Regular', 'Noto Sans Newa Regular',
    'Noto Sans NKo Regular', 'Noto Sans Ol Chiki Regular', 'Noto Sans Old Hungarian Regular',
    'Noto Sans Old Italic Regular', 'Noto Sans Old North Arabian Regular', 'Noto Sans Old Permic Regular',
    'Noto Sans Old Persian Regular', 'Noto Sans Old South Arabian Regular', 'Noto Sans Old Turkic Regular',
    'Noto Sans Oriya', 'Noto Sans Oriya Bold', 'Noto Sans Osage Regular', 'Noto Sans Osmanya Regular',
    'Noto Sans Pahawh Hmong Regular', 'Noto Sans Palmyrene Regular', 'Noto Sans Pau Cin Hau Regular',
    'Noto Sans PhagsPa Regular', 'Noto Sans Phoenician Regular', 'Noto Sans Psalter Pahlavi Regular',
    'Noto Sans Rejang Regular', 'Noto Sans Samaritan Regular', 'Noto Sans Saurashtra Regular',
    'Noto Sans Sharada Regular', 'Noto Sans Siddham Regular', 'Noto Sans Sora Sompeng Regular',
    'Noto Sans Sundanese Regular', 'Noto Sans Syloti Nagri Regular', 'Noto Sans Syriac Regular Black',
    'Noto Sans Syriac Regular Bold', 'Noto Sans Syriac Regular ExtraBold', 'Noto Sans Syriac Regular ExtraLight',
    'Noto Sans Syriac Regular Light', 'Noto Sans Syriac Regular Medium', 'Noto Sans Syriac Regular Regular',
    'Noto Sans Syriac Regular SemiBold', 'Noto Sans Syriac Regular Thin', 'Noto Sans Tagalog Regular',
    'Noto Sans Tagbanwa Regular', 'Noto Sans Tai Le Regular', 'Noto Sans Tai Tham',
    'Noto Sans Tai Viet Regular', 'Noto Sans Takri Regular', 'Noto Sans Thaana Regular',
    'Noto Sans Tifinagh Regular', 'Noto Sans Tirhuta Regular', 'Noto Sans Ugaritic Regular',
    'Noto Sans Vai Regular', 'Noto Sans Wancho Regular', 'Noto Sans Warang Citi Regular',
    'Noto Sans Yi Regular', 'Noto Sans Zawgyi Bold', 'Noto Sans Zawgyi Light',
    'Noto Sans Zawgyi Regular', 'Noto Serif Ahom Regular', 'Noto Serif Balinese Regular',
    'Noto Serif Hmong Nyiakeng Regular', 'Noto Serif Yezidi Regular', 'Optima Bold',
    'Optima Bold Italic', 'Optima ExtraBlack', 'Optima Italic', 'Optima Regular',
    'Palatino', 'Palatino Bold', 'Palatino Bold Italic', 'Palatino Italic', 'Papyrus',
    'Papyrus Condensed', 'Party LET Plain', 'Rockwell', 'Rockwell Bold', 'Rockwell Bold Italic',
    'Rockwell Italic', 'Savoye LET Plain:1.0', 'Scheme-Bold', 'Seravek', 'Seravek Bold',
    'Seravek Bold Italic', 'Seravek ExtraLight', 'Seravek ExtraLight Italic', 'Seravek Italic',
    'Seravek Light', 'Seravek Light Italic', 'Seravek Medium', 'Seravek Medium Italic',
    'SignPainter-HouseScript Semibold', 'Sinhala Sangam MN', 'Sinhala Sangam MN Bold',
    'Snell Roundhand', 'Snell Roundhand Black', 'Snell Roundhand Bold', 'STIX Two Math Regular',
    'STIX Two Text Bold', 'STIX Two Text Italic Bold Italic', 'STIX Two Text Italic Italic',
    'STIX Two Text Italic Medium Italic', 'STIX Two Text Italic SemiBold Italic',
    'STIX Two Text Medium', 'STIX Two Text Regular', 'STIX Two Text SemiBold',
    'STIXGeneral-Bold', 'STIXGeneral-BoldItalic', 'STIXGeneral-Italic', 'STIXGeneral-Regular',
    'Sukhumvit Set Bold', 'Sukhumvit Set Light', 'Sukhumvit Set Medium', 'Sukhumvit Set Semi Bold',
    'Sukhumvit Set Text', 'Sukhumvit Set Thin', 'Superclarendon Black', 'Superclarendon Black Italic',
    'Superclarendon Bold', 'Superclarendon Bold Italic', 'Superclarendon Italic',
    'Superclarendon Light', 'Superclarendon Light Italic', 'Superclarendon Regular',
    'Symbol', 'Tamil Sangam MN', 'Tamil Sangam MN Bold', 'The Hand Serif Semibold',
    'Thonburi', 'Thonburi Bold', 'Thonburi Light', 'Times New Roman', 'Times New Roman Bold',
    'Times New Roman Bold Italic', 'Times New Roman Italic', 'Trebuchet MS', 'Trebuchet MS Bold',
    'Trebuchet MS Bold Italic', 'Trebuchet MS Italic', 'Verdana', 'Verdana Bold',
    'Verdana Bold Italic', 'Verdana Italic', 'Zapf Dingbats', 'Zapfino',
)


_IOS_CJK_FAMILIES = (
    'PingFang SC', 'PingFang TC', 'PingFang HK', 'PingFang MO', 'Songti SC', 'Songti TC',
    'Kaiti SC', 'Kaiti TC', 'Yuanti SC', 'Yuanti TC', 'Heiti SC', 'Heiti TC', 'STHeiti',
    'STSong', 'STKaiti', 'STFangsong', 'Hiragino Sans GB', 'Hiragino Sans', 'Hiragino Kaku Gothic ProN',
    'Hiragino Mincho ProN', 'Hiragino Maru Gothic ProN', 'Apple SD Gothic Neo',
    'Apple LiGothic', 'Apple LiSung', 'AppleMyungjo', 'NanumGothic', 'NanumMyeongjo',
    'GungSeo', 'PCMyungjo', 'PilGi', 'Apple Color Emoji',
)


_IOS_CJK_PREFIXES = (
    'applecoloremoji', 'appleligothic', 'applelisung', 'applemyungjo', 'applesdgothicneo',
    'dotapplecoloremoji', 'dotappleligothic', 'dotapplelisung', 'dotapplemyungjo',
    'dotapplesdgothicneo', 'dotgungseo', 'dotheitisc', 'dotheititc', 'dothiraginokakugothicpron',
    'dothiraginomarugothicpron', 'dothiraginominchopron', 'dothiraginosans', 'dothiraginosansgb',
    'dotkaitisc', 'dotkaititc', 'dotnanumgothic', 'dotnanummyeongjo', 'dotpcmyungjo',
    'dotpilgi', 'dotpingfanghk', 'dotpingfangmo', 'dotpingfangsc', 'dotpingfangtc',
    'dotsongtisc', 'dotsongtitc', 'dotstfangsong', 'dotstheiti', 'dotstkaiti',
    'dotstsong', 'dotyuantisc', 'dotyuantitc', 'gungseo', 'heitisc', 'heititc',
    'hiraginokakugothicpron', 'hiraginomarugothicpron', 'hiraginominchopron', 'hiraginosans',
    'hiraginosansgb', 'kaitisc', 'kaititc', 'nanumgothic', 'nanummyeongjo', 'pcmyungjo',
    'pilgi', 'pingfanghk', 'pingfangmo', 'pingfangsc', 'pingfangtc', 'songtisc',
    'songtitc', 'stapple', 'stapplecoloremoji', 'stappleligothic', 'stapplelisung',
    'stapplemyungjo', 'stapplesdgothicneo', 'stfangsong', 'stgungseo', 'stheiti',
    'stheitisc', 'stheititc', 'sthiragino', 'sthiraginokakugothicpron', 'sthiraginomarugothicpron',
    'sthiraginominchopron', 'sthiraginosans', 'sthiraginosansgb', 'stkaiti', 'stkaitisc',
    'stkaititc', 'stnanumgothic', 'stnanummyeongjo', 'stpcmyungjo', 'stpilgi',
    'stpingfang', 'stpingfanghk', 'stpingfangmo', 'stpingfangsc', 'stpingfangtc',
    'stsong', 'stsongti', 'stsongtisc', 'stsongtitc', 'ststfangsong', 'ststheiti',
    'ststkaiti', 'ststsong', 'styuanti', 'styuantisc', 'styuantitc', 'yuantisc',
    'yuantitc',
)


def _norm_font_name(name: str) -> str:
    """族名归一化：小写、只留字母数字，并去掉 `Plain:1.0` 这类版本后缀。"""
    return re.sub(r"[^0-9a-z]", "", re.sub(r":[\d.]+$", "", str(name)).lower())


_FONT_STYLE_WORDS = frozenset(
    "plain regular bold italic oblique light medium semibold demibold heavy black "
    "thin ultralight extralight book roman semi demi extra ultra".split())


def _font_family_forms(name: str) -> set:
    """一个字体名可能对应的所有族形态：整体 + 逐个剥掉尾部样式词。

    Apple 按「族名 + 样式名」逐条列举（"Avenir Book"、"Charter Black"），宿主
    枚举的既有族名（"Avenir"）也有完整名（"Avenir Book"），两边都要展开才能
    对上。condensed/narrow/wide/expanded 是**族名成分**不是样式词，剥掉会把
    "Avenir Next Condensed" 误归约成 "Avenir Next"。
    """
    text = re.sub(r":[\d.]+$", "", str(name).strip())
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    words = [w for w in re.split(r"[^0-9A-Za-z]+",
                                 text.replace("-", " ").replace("_", " ")) if w]
    forms = {_norm_font_name("".join(words))}
    while len(words) > 1 and words[-1].lower() in _FONT_STYLE_WORDS:
        words = words[:-1]
        forms.add(_norm_font_name("".join(words)))
    return {f for f in forms if len(f) >= 3}


_IOS_DECLARED_FONTS = frozenset(
    form
    for name in tuple(_IOS_DEVICE_FONTS) + tuple(_IOS_CJK_FAMILIES)
    for form in _font_family_forms(name))


def _ios_font_visible(name: str) -> bool:
    """该族名在 iPhone 上能否按名字解析（真机预装 → True）。"""
    key = _norm_font_name(name)
    if key in _IOS_DECLARED_FONTS:
        return True
    return any(key.startswith(p) for p in _IOS_CJK_PREFIXES)


# ------------------------------------------------------------------ 文本度量
# F-92：TextMetrics.emHeightAscent / emHeightDescent 的字体度量表。
# 键是**解析后**的字体族名（小写、去引号、取首个可用族），值是
# (ascent, descent) 相对 em 的比值——即字体 hhea 表的 ascender/descender
# 除以 unitsPerEm（WebKit 在 FontCoreText.cpp 的 platformInit 里换算）。
#
# 关键细节（实测 iOS 27.0.1 / Safari 27.0 系统 WebKit，660 行矩阵）：
#   * 修正项按**解析后的族**取：只有 (ascent, descent) 落在
#     _WK_EM_ADJ 三个元组里的族才加 `lround(size * 0.15)` 到 ascent，与族名
#     无关。表里所有 (0.9, 0.25) 项其实都是 Times 回退，按 0.75/0.25 参与。
#   * 其余族无修正，比值恒定；整数位 lround(ascent/descent) 在 660 行上零误差。
#   * 表里 0.9 / 0.25 的项是 macOS WebKit 侧「族名解析不到、回退到 Times」
#     的实测值——它们要么在 iOS 上确实存在（如 Palatino、Didot），要么就是
#     回退口径，两者取值一致，无需区分。
#   * hiragino sans 的 descent 有 3px 下限，只在 6 ≤ size < 25 生效。
#   * 乘积以 float32 存储（WebKit 内部是 float），故注入器用 Math.fround。
_WK_EM_RATIO: Dict[str, tuple] = {
    "sans-serif": (0.77001953125, 0.22998046875),
    "serif": (0.75, 0.25),
    "monospace": (0.75390625, 0.24609375),
    "cursive": (1.11767578125, 0.46484375),
    "fantasy": (0.93994140625, 0.60302734375),
    "system-ui": (0.966796875, 0.2109375),
    "ui-sans-serif": (0.966796875, 0.2109375),
    "ui-serif": (0.9521484375, 0.2412109375),
    "ui-monospace": (0.966796875, 0.2109375),
    "-apple-system": (0.966796875, 0.2109375),
    "blinkmacsystemfont": (0.75, 0.25),
    "helvetica": (0.77001953125, 0.22998046875),
    "helvetica neue": (0.951995849609375, 0.2129974365234375),
    "helveticaneue": (0.9519958496, 0.2129974365),
    "times": (0.75, 0.25),
    "times new roman": (0.89111328125, 0.21630859375),
    "courier": (0.75390625, 0.24609375),
    "courier new": (0.83251953125, 0.30029296875),
    "arial": (0.9052734375, 0.2119140625),
    "arial mt": (0.9, 0.25),
    "georgia": (0.9169921875, 0.21923828125),
    "georgia mt": (0.9, 0.25),
    "verdana": (1.00537109375, 0.2099609375),
    "tahoma": (1.0004882812, 0.2065429688),
    "menlo": (0.92822265625, 0.23583984375),
    "monaco": (1.0, 0.25),
    "lucida grande": (0.966796875, 0.2109375),
    "lucida console": (0.75, 0.25),
    "geneva": (1.0, 0.25),
    "palatino": (0.82275390625, 0.27734375),
    "didot": (0.9409942626953125, 0.2989959716796875),
    "baskerville": (0.89794921875, 0.24609375),
    "futura": (1.03857421875, 0.259765625),
    "optima": (0.91900634765625, 0.26800537109375),
    "avenir": (1.0, 0.365997314453125),
    "avenir next": (1.0, 0.365997314453125),
    "impact": (1.0087890625, 0.2109375),
    "trebuchet ms": (0.93896484375, 0.22216796875),
    "comic sans ms": (1.10205078125, 0.29150390625),
    "american typewriter": (0.9040069580078125, 0.25),
    "chalkboard": (0.9801177979, 0.2828674316),
    "chalkboard se": (1.1314849853515625, 0.282867431640625),
    "pingfang sc": (1.06000214771948, 0.3400006890296936),
    "pingfang hk": (1.06000214771948, 0.3400006890296936),
    "hiragino sans": (0.8800017881393433, 0.12000024504959583),
    "sf pro text": (0.75, 0.25),
    "sf pro icons": (0.75, 0.25),
    "system font": (0.966796875, 0.2109375),
    "garamond": (0.75, 0.25),
    "bookman": (0.75, 0.25),
    "century gothic": (0.75, 0.25),
    "itc avant garde gothic": (0.9, 0.25),
    "sand": (0.9, 0.25),
}
# 需要 lround(size * 0.15) 修正的 (ascent, descent) 元组——按解析结果匹配，
# 不按族名（表里名字不同但解析到同一字体的族取值完全一致，例如所有
# (0.9, 0.25) 项都是 Times 回退，走 0.75/0.25 的修正）。
_WK_EM_ADJ = ((0.77001953125, 0.22998046875),
              (0.75, 0.25),
              (0.75390625, 0.24609375))


def _generate_mobile_font_subset(target_os: str, *, full: bool = False) -> List[str]:
    """移动端字体子集：preset 用完整集合（固定），generate 随机子集。"""
    table = _MOBILE_FONTS.get(target_os, _MOBILE_FONTS["android"])
    essential, extra = table["essential"], table["extra"]
    if full or not extra:
        return list(essential) + list(extra)
    picked = random.sample(extra, min(len(extra), random.randint(5, len(extra))))
    return list(essential) + picked


# ------------------------------------------------------------------ 辅助数据
def _generate_battery_api() -> Dict[str, Any]:
    """Android Chrome navigator.getBattery() 的初始值（iOS 无该 API，返回 None）。

    按规范只存「另一侧」的有限时长：充电时 dischargingTime 为 Infinity，
    放电时 chargingTime 为 Infinity。Infinity 无法写进存储的 JSON（HTTP 序列化
    会 500），故这里存 None，由注入器在运行期按 charging 推导。
    """
    level = round(random.uniform(0.2, 1.0), 2)
    charging = random.random() < 0.15
    return {
        "level": level,
        "charging": charging,
        "chargingTime": None if not charging else random.randint(1800, 7200),
        "dischargingTime": random.randint(14400, 43200) if not charging else None,
    }


_MOBILE_GEOLOCATION = {
    "geolocation": {
        "accessibility": True,
        "mocked": False,
    },
}


def _generate_camera_api() -> Dict[str, Any]:
    return {
        "hasCamera": True,
        "rearCamera": True,
        "frontCamera": True,
        "videoCapture": True,
        "imageCapture": True,
    }


_MOBILE_WEBRTC = {
    "webrtc": {
        "supported": True,
        "privacyMode": False,
    },
}

# iOS Safari 不存在、运行期需要从内核（桌面 Chromium）上抹掉的 Navigator 属性。
# 首项是早期的 Safari/桌面差异（connection 等）；其余为 WebKit 从未实现的
# 桌面 Chromium API（MDN BCD，2026-10 核实）：Web Serial / WebUSB / WebHID /
# Web Bluetooth / Keyboard Lock / Web MIDI / Presentation /
# getInstalledRelatedApps / navigator.vibrate。桌面内核全部携带，不抹除时
# `'serial' in navigator` 这类一行探测即可判定「Chromium 装 iOS」。
# pdfViewerEnabled 不在此列：BCD safari_ios=16.4，真机恒 true（与插件集合
# 为空不矛盾），运行期显式置 true。
_IOS_REMOVE_NAV_PROPS = [
    "connection", "userAgentData", "getBattery", "deviceMemory",
    "serial", "usb", "hid", "bluetooth", "keyboard", "requestMIDIAccess",
    "presentation", "getInstalledRelatedApps", "vibrate",
    # F-04：WebKit 27.2 实测已无、桌面 Chromium 148 仍带的成员。前两项
    # 在 BCD 里记的是「移除」（doNotTrack 7、getUserMedia 12），后三项
    # 是 Chromium 专有面（Scheduling API / Storage Buckets / PWA 标题栏）。
    "doNotTrack", "getUserMedia", "webkitGetUserMedia",
    "scheduling", "storageBuckets", "windowControlsOverlay",
    # F-69：NavigatorLogin / NavigatorManagedData 对应的 nav 属性面（与 window
    # 类名是两套暴露面，只删类名仍可 `'login' in navigator` 判假）
    "login", "managed",
    # F-70：WebKit 的协议处理器实现早已移除（Bugzilla 仅 2017 GTK 回归记录），
    # 真机 Safari 无。
    "registerProtocolHandler", "unregisterProtocolHandler",
    # F-90：徽标 API 由 AppBadgeEnabled 门控，该 pref 的 defaultValue 为 false
    # （UnifiedWebPreferences.yaml，无 iOS 覆盖），WebKit 27.2 实测
    # navigator.setAppBadge/clearAppBadge 均为 undefined。BCD 记的 16.4 是
    # Home Screen Web App 口径，普通浏览上下文不暴露。
    "setAppBadge", "clearAppBadge",
]

# Android Chrome 不存在、需从桌面内核上抹掉的 Navigator 属性（WebHID 仅桌面，
# 见 BCD chrome_android=false；WebUSB/Web Serial 在 Chrome Android 148 已支持，
# 不可误删——版本相关差异必须逐项核对，不能按「桌面 API」一概而论）。
_ANDROID_REMOVE_NAV_PROPS = [
    "hid",
    # F-96：Window Controls Overlay 是桌面 PWA 专有（BCD chrome_android=false）；
    # F-74 只删了构造函数/全局，Navigator 上的 getter 仍在。
    "windowControlsOverlay",
    # F-78：crbug 40964464 —— Chrome Android 无协议处理器入口；
    # setAppBadge 仅 set 缺席（clearAppBadge BCD=81 保留，勿成对删）。
    "registerProtocolHandler", "unregisterProtocolHandler", "setAppBadge",
]


# --------------------------------------------------------------------------
# 媒体可播放能力表（HTMLMediaElement.prototype.canPlayType）
# --------------------------------------------------------------------------
# 内核是桌面 Chromium（自建 FFmpeg 管线），canPlayType 对 ogg/webm/opus 一律
# "probably"；真机 iOS Safari 走系统 AVFoundation，容器白名单里**没有** ogg/webm，
# 单行 `canPlayType('audio/ogg; codecs="vorbis"')` 即可判「不是 iOS」。
#
# 机制取证（WebKit main，2026-10）：
#   Source/WebCore/platform/graphics/avfoundation/objc/AVAssetMIMETypeCache.mm
#   的 isStaticContainerType 白名单仅 30 项（3gpp/aac/aiff/basic/mp3/mp4/mpeg/
#   wav/x-m4a/x-wav/mpegurl/mp2t… + video/3gpp/3gpp2/mp4/mpeg/quicktime/x-m4v），
#   无平台条件分支——ogg/webm 不在其中，故 iOS 上落到 AVFoundation 扩展查询并
#   被拒；macOS AVFoundation 自带 WebM/ogg 解码器，同一份 WebKit 会返回
#   "probably"（实测 Playwright WebKit 26.5：ogg/webm 全 "probably"）——
#   macOS WebKit **不能**当作 iOS 基线，本表按 iOS 真机口径写死。
#   hvc1 在 iOS 走 VideoToolbox 硬件解码（A10 起）→ "probably"；hev1 不被接受
#   → ""。内核 ffmpeg 的 hvc1 支持与容器串形态相关（'hvc1' → ""、
#   'hvc1.1.6.L93.B0' → "probably"），一并收敛为 iOS 口径。
#
# 本表是**覆盖层**，不是兜底：未命中的串由注入层的 __cptIos 按 WebKit
# MIMETypeCache::canDecodeType 算法判定（30 项 AVFoundation 静态容器白名单 +
# iOS 动态项 + codecs 谓词，见 mobile_cdp_injector._CPT_IOS_SRC）。F-143
# 之前这里是「表外透传宿主原生」，会让 `audio/mp4; codecs="opus"` 继承
# Blink 的 "probably"（真机 iOS 为 ''）。
# 判定顺序：先剥离 `; codecs=` 后按整串匹配，再按 codecs 逐个匹配（大小写不敏感）。
#
# Android Chrome 侧为**显式豁免**：内核 ffmpeg 对 hvc1 的返回值形态不一致
# （裸 'hvc1' 为 ""），真机 Android Chrome 有 HEVC 硬件解码（芯片级），
# 故 hvc1 家族单列校准为 "probably"；ogg/webm/opus 内核已与真机同口径，不动。
_CODEC_CAPABILITY_IOS: Dict[str, str] = {
    # --- AVFoundation 容器白名单外：拒绝 ---
    # ogg 不在此表：iOS 18.4 起 WebKit 支持 Ogg 容器（vorbis/opus/flac），
    # 低于 18.4 才整族缺席。版本门控在注入层 __cptIos / __mseSupports 按
    # opt.ogg 判定，表里写死会把这档机型答成「不是 iOS」。
    # video/ogg 在所有版本都缺席（AVFoundation 无 theora 解码器），可由
    # 算法判空，故同样不列。
    # F-199：webm 不再写死。iOS 17.4 起 AVFoundation 带 WebM 解码器
    # （caniuse webm：ios_saf 17.3 仍是「仅 WebRTC 内 VP8/VP9」的 a 档，
    # 17.4 转 y），覆盖层若继续钉成 '' 会把 17.4+ 的机型答成「不是 iOS」。
    # 平台差由注入层 __cptIos 按 opt.webm 判定（<17.4 走容器门拒绝 → ''，
    # ≥17.4 裸容器 → "maybe"、带 vp8/vp9/opus/vorbis → "probably"）。
    # --- Apple VideoToolbox：HEVC 硬件解码 ---
    'video/mp4; codecs="hvc1"': "probably",
    'video/mp4; codecs="hvc1.1.6.L93.B0"': "probably",
    'video/mp4; codecs="hev1"': "",
    # --- F-19：容器白名单内、未带 codecs 的查询 ---
    # WebKit MIMETypeCache::canDecodeType（Source/WebCore/platform/graphics/
    # MIMETypeCache.cpp:74-100）：容器命中而 codecs 为空 → MayBeSupported
    # （"maybe"）；带 codecs 时只有 canDecodeExtendedType 成功才 IsSupported
    # （"probably"）。桌面 Chromium 的 FFmpeg 管线对 audio/mpeg|mp3|aac|flac
    # 直接给 "probably"，一行 canPlayType 即可判假。
    "audio/mpeg": "maybe",
    "audio/mp3": "maybe",
    "audio/aac": "maybe",
    "audio/flac": "maybe",
    "video/quicktime": "maybe",
    # 带 codecs 的常见查询走 canDecodeExtendedType：'mp3' 命中 WebKit 的
    # shouldOverrideExtendedType 特例（MIMETypeCache.cpp:163-175，直接
    # IsSupported）；flac 只在 MP4 容器内被改写成 MP4RA 的 'fLaC' 后再问
    # AVFoundation（AVAssetMIMETypeCache.mm:108-112）→ audio/flac 容器带
    # codecs="flac" 反而不可播（WebKit 实测 ""），audio/mp4 带 flac 可播。
    'audio/mpeg; codecs="mp3"': "probably",
    'audio/flac; codecs="flac"': "",
    'audio/mp4; codecs="flac"': "probably",
    # Dolby Vision：A12（iPhone XS/XR）及以后全系可解（dvh1 → HEVC 主层），
    # 指纹库内机型均晚于该代 → "probably"。未列入的 av01 由 _codec_capability
    # 按机型能力位补充（A17 Pro 起才可解）。
    'video/mp4; codecs="dvh1.05.06"': "probably",
    # AV1：A17 Pro 之前无硬解，Safari 报 ""（而非 Chromium 的 "probably"）。
    'video/mp4; codecs="av01.0.05M.08"': "",
}
_CODEC_CAPABILITY_ANDROID: Dict[str, str] = {
    # --- 真机 Android Chrome：HEVC 硬件解码（内核裸 'hvc1' 形态不一致）---
    'video/mp4; codecs="hvc1"': "probably",
    'video/mp4; codecs="hvc1.1.6.L93.B0"': "probably",
}


# AV1 硬解机型（A17 Pro / A18 / A19 / M3+ 家族）。按机型名匹配指纹库里的
# market_name：iPhone 15 Pro(Pro Max)、iPhone 16 全系（含 16e）、iPhone 17
# 全系、iPhone Air（A19 Pro）、iPad mini (A17 Pro)、iPad Air (M3)、
# iPad Pro (M4/M5)。真机门控是 VideoToolbox 的 VTIsHardwareDecodeSupported
# ('av01')（AV1UtilitiesCocoa.mm:198-205），与 CPU 代次一一对应。
_IOS_AV1_MARKET_TOKENS = (
    "iPhone 15 Pro", "iPhone 16", "iPhone 17", "iPhone Air", "A17 Pro",
    "(M3)", "(M4)", "(M5)",
)


def _ios_av1_capable(model: str, market_name: str = "") -> bool:
    name = f"{model} {market_name}"
    return any(tok in name for tok in _IOS_AV1_MARKET_TOKENS)


def _codec_capability(target_os: str, *, av1: bool = False) -> Dict[str, str]:
    """目标平台相对宿主内核需要校准的 canPlayType 返回值（键为小写 MIME）。"""
    table = dict(_CODEC_CAPABILITY_IOS if target_os == "ios" else _CODEC_CAPABILITY_ANDROID)
    if target_os == "ios" and av1:
        table['video/mp4; codecs="av01.0.05M.08"'] = "probably"
    return {k.lower(): v for k, v in table.items()}


# --------------------------------------------------------------------------
# NetworkInformation.downlinkMax（仅 Android，单位 Mbps）
# --------------------------------------------------------------------------
# 与 type / ontypechange 同属 NetInfoDownlinkMax 运行期开关，BCD
# api.NetworkInformation.downlinkMax：chrome_android=38，桌面 chrome 仅
# ChromeOS（partial）→ 桌面内核上 'downlinkMax' in navigator.connection 为
# false，是与 type 同源的「桌面装 Android」判据。
#
# 取值不是编造的常量，而是内核自己的连接子类型表（net/base/
# network_change_notifier.cc: GetMaxBandwidthMbpsForConnectionSubtype）：
# 真机 Android 的 subtype 来自 ConnectivityManager（WiFi/Ethernet 走
# NetworkInfo.getSubtype，蜂窝走 cellularSubtypeFromKbps(下行带宽)），
# 再按该表折算。type 已声明 wifi，故取该表的 Wi-Fi 条目值域——
# 11 / 54 / 600 / 1300 / 7000（b/g/n/ac/ad），真机取值落在这个集合内。
# 取 1300（Wi-Fi 5 / 802.11ac）：2023 年后的主流机型与路由器均为 ac，
# 且与「5G 手机」的代数口径一致；ax 机型该值仍是常见读数（内核表未区分 ax）。
_ANDROID_DOWNLINK_MAX_MBPS = 1300.0


# --------------------------------------------------------------------------
# WebGL 参数上限与扩展集（按 GPU 家族，非按机型）
# --------------------------------------------------------------------------
# 宿主是 Apple M 系列（Metal/ANGLE）：MAX_TEXTURE_SIZE=16384、
# MAX_VIEWPORT_DIMS=[16384,16384]、MAX_SAMPLES=4，扩展集带桌面专属
# EXT_texture_compression_bptc / rgtc / WEBGL_polygon_mode /
# EXT_clip_control / WEBGL_blend_func_extended 等——「渲染器字符串是
# Adreno 710、参数上限却是 M 系列」的三元组矛盾，gpuinfo.org 数据集比对
# 可直接命中（方案 P2-1）。
#
# 取值依据：移动 GPU 的 OpenGL ES 上限受硬件约束，且各家族长期稳定——
#   * Adreno（Qualcomm，A6xx/A7xx/A8xx 全系）：MAX_TEXTURE_SIZE 16384、
#     MAX_VIEWPORT_DIMS 16384×16384、MAX_RENDERBUFFER_SIZE 16384、
#     MAX_CUBE_MAP_TEXTURE_SIZE 16384、MAX_SAMPLES 4、MAX_DRAW_BUFFERS 8
#   * Mali（ARM，Valhall/5th Gen）：MAX_TEXTURE_SIZE 16384（G715/G615 段），
#     MAX_SAMPLES 4
#   * Xclipse（Samsung/RDNA）、PowerVR（ImgTec）：同为 16384 档
# 这些上限与桌面 Apple M 系列**数值相同**，所以单看 MAX_TEXTURE_SIZE 并不
# 穿帮；真正穿帮的是**扩展集**——S3TC/BPTC/RGTC/polygon_mode 是桌面
# OpenGL/D3D 纹理与光栅化扩展，移动 GPU 驱动不导出（iOS Metal 更是绝不
# 支持 S3TC，检测站对 iOS 目标直接按 15 分计）。故本表的核心是扩展集
# 白名单，MAX_* 只覆盖确有家族差异的项。
#
# F-165 更正（2026-10-24）：此前把「真机 Android」的对照值取成了 ANGLE 的
# **Vulkan** 后端读数（gpuinfo r8256 等，渲染器串形如 `... on Vulkan 1.3.x`），
# 与本仓声明的渲染器串不是同一后端。本仓 Adreno/Mali 串收尾是
# `OpenGL ES 3.2`——ANGLE 的 DisplayGL::getVersionString 只在原生上下文为
# GLES 时加这个前缀（DisplayGL.cpp:206-212 + SanitizeVersionString:57-85），
# 即 **GL/GLES 后端**。Vulkan 后端的读数（4096 uniform vectors、96 纹理
# 单元、8192 三维纹理、63 texel offset）不适用。
# GLES 后端经 ANGLE 两层钳位后，Adreno 的多个上限**低于**宿主，可如实声明
# 而不出现「报得上、用不了」；见 _GL_LIMITS_ADRENO。
#
# 扩展集来源：内核在移动 GPU 上会导出的 WebGL1 扩展（Chrome 148 实测集
# 合中剔除桌面专属项）。缺失项由注入层从 getSupportedExtensions() 过滤，
# getExtension() 对过滤项返回 null（与「驱动不导出」同语义）。
_GL_EXT_DESKTOP_ONLY = (
    # 桌面 D3D/OpenGL 纹理压缩（DXT/BC 家族）——移动 GPU 不导出
    "WEBGL_compressed_texture_s3tc",
    "WEBGL_compressed_texture_s3tc_srgb",
    "EXT_texture_compression_bptc",
    "EXT_texture_compression_rgtc",
    # 桌面光栅化/着色器扩展
    "WEBGL_polygon_mode",
    "EXT_clip_control",
    "EXT_depth_clamp",
    "EXT_polygon_offset_clamp",
    "WEBGL_blend_func_extended",
    "NV_shader_noperspective_interpolation",
    "EXT_texture_mirror_clamp_to_edge",
    "EXT_render_snorm",
    "EXT_texture_norm16",
)
# iOS（Metal/ANGLE-Metal）额外：Spectre 侧信道计时扩展在 Apple 物理封禁。
# F-23：此前这里把整套桌面扩展（S3TC/BPTC/RGTC/clip_control/polygon_mode…）
# 也一并剔除，但 iOS Safari 自 15 起同样走 ANGLE-Metal，扩展集与 macOS
# WebKit 一致——真机**有**这 16 项，剔除反而制造「声明 iOS 却缺 iOS 扩展」的
# 反向判据。iOS 只保留计时扩展的剔除。
_GL_EXT_DESKTOP_ONLY_IOS = (
    "EXT_disjoint_timer_query",
    "EXT_disjoint_timer_query_webgl2",
)
# iOS 真机有、桌面 Chromium 不导出的扩展（F-23）：PVRTC 是 PowerVR 家族的
# 压缩纹理扩展，WebKit 在 Apple 平台仍作为 WEBKIT_ 前缀面导出（iOS GPU 家族
# 特征位），Chromium 从不提供。
_GL_EXT_IOS_ADD = (
    "WEBKIT_WEBGL_compressed_texture_pvrtc",
)

_GL_LIMITS_MOBILE: Dict[str, Any] = {
    # 各家族一致的移动端上限（覆盖宿主同值项，语义上仍是「声明值」）
    "MAX_TEXTURE_SIZE": 16384,
    "MAX_RENDERBUFFER_SIZE": 16384,
    "MAX_CUBE_MAP_TEXTURE_SIZE": 16384,
    "MAX_VIEWPORT_DIMS": [16384, 16384],
    "MAX_SAMPLES": 4,
    "MAX_DRAW_BUFFERS": 8,
    "MAX_VERTEX_ATTRIBS": 16,
    "MAX_TEXTURE_IMAGE_UNITS": 16,
    "MAX_VERTEX_TEXTURE_IMAGE_UNITS": 16,
    "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 32,
    "MAX_VERTEX_UNIFORM_VECTORS": 1024,
    "MAX_FRAGMENT_UNIFORM_VECTORS": 1024,
    "MAX_VARYING_VECTORS": 30,
    "ALIASED_POINT_SIZE_RANGE": [1, 511],
    "ALIASED_LINE_WIDTH_RANGE": [1, 1],
}


# Adreno 家族在 ANGLE **GLES 后端**下的真实上限（覆盖 _GL_LIMITS_MOBILE）。
#
# 推导链：原生 Adreno GLES 驱动读数（gpuinfo opengles r8242/8244/8245/
# 8247/8250/8254，Adreno 610/619/830/840/850）→ ANGLE 钳位 → WebGL 报值。
# 钳位两处：
#   renderergl_utils.cpp:1472-1479
#       maxVertexUniformVectors = min(1024, native)          # Adreno 原生 256
#       maxShaderUniformComponents[V] = min(vectors * 4, native)
#   Context.cpp:4415-4500 ANGLE_LIMIT_CAP
#       maxVertexAttributes        ≤ MAX_VERTEX_ATTRIBS (16)
#       maxShaderUniformBlocks[*]  ≤ 16
#       maxCombinedUniformBlocks   ≤ 60
#       maxUniformBufferBindings   ≤ 72
#       maxCombinedTextureImageUnits ≤ 64 (ES3.2 以下)
# Chrome 的 WebGL 层不再加钳位（gles2_cmd_decoder.cc:3597-3598、3736-3739
# 直接透传 ANGLE 的 caps），所以 ANGLE 的 GLES 数值就是真机 WebGL 报值。
#
# 只列**与宿主不同且可如实声明**的项：Adreno 经钳位后落在宿主之下
# （1024→256、4096→1024、16→14、32→28、4294967294→2147483647），
# 声明后仍可真实使用，不产生「报得上、用不了」的二次矛盾。
#
# 仍未声明的项（GLES 后端下仍高于宿主，声明即「报得上、用不了」）：
#   MAX_UNIFORM_BUFFER_BINDINGS 72（宿主 32）、MAX_UNIFORM_BLOCK_SIZE 65536
#   （宿主 16384）、MAX_VERTEX_OUTPUT_COMPONENTS / MAX_FRAGMENT_INPUT_
#   COMPONENTS 128（宿主 120）、MAX_VARYING_VECTORS 31（宿主 30）、
#   MAX_COMBINED_VERTEX/FRAGMENT_UNIFORM_COMPONENTS 230400（宿主 69632）、
#   MAX_TEXTURE_LOD_BIAS 16（宿主 15）。这些保留宿主真值。
_GL_LIMITS_ADRENO: Dict[str, Any] = {
    # Adreno 原生 256 → ANGLE min(1024, 256) = 256
    "MAX_VERTEX_UNIFORM_VECTORS": 256,
    "MAX_FRAGMENT_UNIFORM_VECTORS": 256,
    # Adreno 原生 1024 → ANGLE min(256 * 4, 1024) = 1024
    "MAX_VERTEX_UNIFORM_COMPONENTS": 1024,
    "MAX_FRAGMENT_UNIFORM_COMPONENTS": 1024,
    # Adreno 原生 14 → ANGLE min(14, 16) = 14
    "MAX_VERTEX_UNIFORM_BLOCKS": 14,
    "MAX_FRAGMENT_UNIFORM_BLOCKS": 14,
    # Adreno 原生 84 → CapCombinedLimitToESShaders 取 V+F = 28，再 min(28, 60)
    "MAX_COMBINED_UNIFORM_BLOCKS": 28,
    # Adreno 原生 2147483647（ANGLE 只钳上界 UINT_MAX-1，不抬高）
    "MAX_ELEMENT_INDEX": 2147483647,
}


def _webgl_limits_for(target_os: str, renderer: str = "") -> Dict[str, Any]:
    """按 GPU 家族返回上限覆盖表；渲染器串缺省时退回平台基线。"""
    limits = dict(_GL_LIMITS_MOBILE)
    if target_os == "android" and "Adreno" in renderer:
        limits.update(_GL_LIMITS_ADRENO)
    return limits


def _webgl_profile(target_os: str, renderer: str = "") -> Dict[str, Any]:
    """目标平台 WebGL 面校准规格：上限覆盖 + 扩展集剔除/补充（按 GPU 家族）。"""
    limits = _webgl_limits_for(target_os, renderer)
    if target_os == "ios":
        return {
            "limits": limits,
            "dropExtensions": list(_GL_EXT_DESKTOP_ONLY_IOS),
            "addExtensions": list(_GL_EXT_IOS_ADD),
        }
    return {
        "limits": limits,
        "dropExtensions": list(_GL_EXT_DESKTOP_ONLY),
    }


# --------------------------------------------------------------------------
# 平台 API 差集（窗口 / 文档 / navigator 三处统一按名删除）
# --------------------------------------------------------------------------
# 桌面内核会暴露一批「所属平台真机没有」的 API，属一行判假面。逐项证据 =
# MDN BCD（/bcd/api/v0/current/）的 safari_ios / chrome_android 支持值；
# WebKit 从未实现的家族（传感器 / 隐私沙盒）同时有 WebKit 源码面佐证。
# 删除时同步清 window / Window.prototype / Document.prototype / document /
# Navigator.prototype / navigator，并按需在原型上兜底 defineGet(undefined)。
#
# ---- iOS（Safari 与 CriOS 同为 WebKit 内核，口径一致）----
_IOS_REMOVE_GLOBALS = [
    # 通用传感器 API：WebKit 从未实现（BCD api.Accelerometer /
    # api.Gyroscope / api.GravitySensor / api.LinearAccelerationSensor /
    # api.AbsoluteOrientationSensor / api.RelativeOrientationSensor /
    # api.SensorErrorEvent / api.Sensor: safari_ios=false）。
    # Chrome Android 自 67 起具备，Android 侧保持（注入层由 CDP 虚拟
    # 传感器喂数，读数与事件流同源）。
    "Sensor", "SensorErrorEvent", "Accelerometer", "Gyroscope",
    "LinearAccelerationSensor", "GravitySensor",
    "AbsoluteOrientationSensor", "RelativeOrientationSensor",
    # Chromium 专有、Safari 无（BCD 逐项 safari_ios=false）：
    # EyeDropper(95/desktop-only) / IdleDetector(94) / WebSocketStream(124) /
    # ImageDecoder(94) / FileSystemObserver(133) / queryLocalFonts(103) /
    # documentPictureInPicture(116) / launchQueue(102) / Scheduler(94) /
    # OTPCredential(93) / Ink(94) / MediaStreamTrackGenerator(94)
    "EyeDropper", "IdleDetector", "WebSocketStream", "ImageDecoder",
    "FileSystemObserver", "queryLocalFonts", "documentPictureInPicture",
    "launchQueue", "Scheduler", "scheduler", "OTPCredential", "Ink",
    "MediaStreamTrackGenerator",
    # 隐私沙盒（WebKit 明确拒绝实现）：Shared Storage / Fenced Frames /
    # Topics / Protected Audience（BCD api.Window.sharedStorage /
    # api.Window.fence / api.HTMLFencedFrameElement / api.Document.browsingTopics
    # / api.Navigator.runAdAuction 等：safari_ios=false）
    "sharedStorage", "SharedStorage", "fence", "Fence",
    "HTMLFencedFrameElement", "browsingTopics",
    "runAdAuction", "joinAdInterestGroup", "leaveAdInterestGroup",
    "deprecatedReplaceInURN", "updateAdInterestGroups", "protectedAudience",
    # File System Access 的「选择器」面：Safari 只实现 OPFS
    # （FileSystemHandle 家族 Safari 15.2+ 具备、不可误删），
    # show{,Open,Save}FilePicker / showDirectoryPicker 三项均为
    # Chromium 专有（BCD safari_ios=false）
    "showSaveFilePicker", "showDirectoryPicker",
    # navigator 成员：WebXR / VirtualKeyboard / Ink（BCD safari_ios=false）
    "xr", "virtualKeyboard", "ink",
    # V8 专有、WebKit 从未实现的两项。检测站各占一档独立判据：
    #   Intl.v8BreakIterator —— V8 的旧版 ICU 断词器，WebKit 的 Intl 无此成员；
    #   navigator.webkitTemporaryStorage / webkitPersistentStorage —— Blink
    #   分叉时遗留的配额 API，WebKit main 的 NavigatorStorage.idl 只声明
    #   `storage`（Source/WebCore/page/NavigatorStorage.idl，2026-10 核实），
    #   真机 Safari/CriOS 均无。桌面内核两者俱全，一行 `in` 即可判假。
    "v8BreakIterator", "webkitTemporaryStorage", "webkitPersistentStorage",
    # 真机 Safari 无 window.chrome（F-64 口径仍然成立）。内核 152 的
    # window.chrome 是 own + **configurable:true**（实测 /tmp/fb05/chromedel3.py：
    # 文档起点 `delete window.chrome` 后 `'chrome' in window === false`，
    # 0ms 定时器复查仍为 false，不会被惰性装回）——F-169 当年那句
    # 「configurable:false，delete 静默失败」是内核 148 时代的观测，对 152
    # 已失效。删干净后 creepjs 的守卫
    # `if (!('chrome' in window && 'runtime' in chrome)) return false;`
    # 在第一支即短路（实测 guard 返回 true），不会像「值改成 undefined」
    # 那样对 undefined 取 `in` 抛 TypeError 击穿 getHeadlessFeatures。
    "chrome",
    # F-05 / F-09 / F-10 / F-21：WebKit 27.2 实测缺席、桌面内核仍带的入口
    # 接口对象与 Blink 遗留别名。整族只收「入口」——家族成员的 `in` 随
    # 入口缺席而消失。webkit* 前缀族是 Chromium 的遗留别名，真机 iOS 用
    # 的是完全不同的命名（如 SpeechRecognition 家族），比裸名更刺眼。
    # 反例（iOS 真支持，勿加）：styleMedia / BarcodeDetector / PaymentRequest /
    # SpeechRecognition / MediaStreamTrackProcessor / DigitalCredential /
    # DevicePosture / DeviceMotionEventAcceleration / DeviceMotionEventRotationRate。
    "External", "external", "crashReport", "viewport", "credentialless",
    "InputDeviceCapabilities", "isExtended",
    "NavigatorUAData", "NetworkInformation",
    "BackgroundFetchManager", "SyncManager", "BeforeInstallPromptEvent",
    "WindowControlsOverlay", "requestIdleCallback", "cancelIdleCallback",
    "EditContext", "TextFormat", "TextUpdateEvent", "CharacterBoundsUpdateEvent",
    "CropTarget", "DelegatedInkTrailPresenter", "ChapterInformation",
    "MediaStreamTrackAudioStats", "MediaStreamTrackVideoStats", "AudioSinkInfo",
    "FencedFrameConfig", "SharedStorageWorklet", "Sanitizer",
    "LaunchQueue", "LaunchParams", "TimelineTrigger", "NotRestoredReasons",
    "WebSocketError", "WebGLObject", "DOMError", "QuotaExceededError",
    "FeaturePolicy", "AsyncDisposableStack", "DisposableStack",
    "SuppressedError", "CrashReportContext",
    "webkitMediaStream", "webkitRTCPeerConnection",
    "webkitSpeechRecognitionError", "webkitSpeechRecognitionEvent",
    "webkitSpeechGrammar", "webkitSpeechGrammarList",
    # Blink 专有事件处理器属性（F-08）：WebKit 用的是 webglcontextlost /
    # webglcontextrestored 等另一套命名，命名不同即一行判假。
    "onsearch", "oncontextlost", "oncontextrestored", "onbeforeinstallprompt",
    "onappinstalled",
    # F-10：Blink 前缀可见性别名（WebKit 14 起已移除该前缀面）
    "webkitHidden", "webkitVisibilityState",
    # F-139：Device Posture 在 WebKit 里由 `DevicePostureAPIEnabled` 偏好
    # 门控，`UnifiedWebPreferences.yaml` 记 `status: testable` +
    # `defaultValue: false`——出货版 Safari 不开，普通浏览上下文恒缺席，
    # 与 iOS 版本无关（BCD 的 safari_ios 条目本身就是 preference flag）。
    # 放版本门控表会在 27+ 档漏出，故按无条件删除处理。
    "DevicePosture", "devicePosture",
    # ---- F-62 / F-63 / F-65 / F-66 / F-67 / F-68 / F-75：Chromium 专有家族 ----
    # 证据：原生 fp-chromium 148（https 页面，零注入）对 WebKit 基线取差集，
    # 再逐族过 BCD safari_ios=false（取证见清单 §23/§24/§25）。整族按名删除
    # 而非只删入口——Chromium 的家族成员是独立全局，入口删掉成员仍在，
    # `'HIDDevice' in window` 依旧为 true。
    # 硬件直连（Web Bluetooth / WebHID / WebUSB / Web Serial / Web MIDI /
    # Keyboard Lock）：WebKit 一个都没实现。BluetoothUUID 一并删——真机
    # 没有该命名空间对象，留着等于留下整族的指纹。
    "Bluetooth", "BluetoothCharacteristicProperties", "BluetoothDevice",
    "BluetoothRemoteGATTCharacteristic", "BluetoothRemoteGATTDescriptor",
    "BluetoothRemoteGATTServer", "BluetoothRemoteGATTService",
    "BluetoothUUID",
    "HID", "HIDConnectionEvent", "HIDDevice", "HIDInputReportEvent",
    "USB", "USBAlternateInterface", "USBConfiguration", "USBConnectionEvent",
    "USBDevice", "USBEndpoint", "USBInTransferResult", "USBInterface",
    "USBIsochronousInTransferPacket", "USBIsochronousInTransferResult",
    "USBIsochronousOutTransferPacket", "USBIsochronousOutTransferResult",
    "USBOutTransferResult",
    "Serial", "SerialPort",
    "MIDIAccess", "MIDIConnectionEvent", "MIDIInput", "MIDIInputMap",
    "MIDIMessageEvent", "MIDIOutput", "MIDIOutputMap", "MIDIPort",
    "Keyboard",
    # WebXR：Safari 从不支持（navigator.xr 已在上面删除，类名整族 50 个同删）。
    "XRAnchor", "XRAnchorSet", "XRBoundedReferenceSpace",
    "XRCPUDepthInformation", "XRCamera", "XRCompositionLayer", "XRCubeLayer",
    "XRCylinderLayer", "XRDOMOverlayState", "XRDepthInformation",
    "XREquirectLayer", "XRFrame", "XRHand", "XRHitTestResult",
    "XRHitTestSource", "XRInputSource", "XRInputSourceArray",
    "XRInputSourceEvent", "XRInputSourcesChangeEvent", "XRJointPose",
    "XRJointSpace", "XRLayer", "XRLayerEvent", "XRLightEstimate",
    "XRLightProbe", "XRPlane", "XRPlaneSet", "XRPose", "XRProjectionLayer",
    "XRQuadLayer", "XRRay", "XRReferenceSpace", "XRReferenceSpaceEvent",
    "XRRenderState", "XRRigidTransform", "XRSession", "XRSessionEvent",
    "XRSpace", "XRSubImage", "XRSystem", "XRTransientInputHitTestResult",
    "XRTransientInputHitTestSource", "XRView", "XRViewerPose", "XRViewport",
    "XRVisibilityMaskChangeEvent", "XRWebGLBinding", "XRWebGLDepthInformation",
    "XRWebGLLayer", "XRWebGLSubImage",
    # 广告 / 隐私沙盒残项（本体已在上面，这些是同族的方法对象与子类）
    "ProtectedAudience", "SharedStorageAppendMethod", "SharedStorageClearMethod",
    "SharedStorageDeleteMethod", "SharedStorageModifierMethod",
    "SharedStorageSetMethod", "InterestEvent", "Subscriber",
    "RestrictionTarget", "NotRestoredReasonDetails",
    # 呈现 / 后台同步 / 存储分桶（三类 WebKit 均未实现，见下方 F-90 段落对
    # 「BCD 记 16.4」的辨析：那是 Home Screen Web App 口径）
    "Presentation", "PresentationConnection", "PresentationConnectionAvailableEvent",
    "PresentationConnectionCloseEvent", "PresentationConnectionList",
    "PresentationReceiver", "PresentationRequest",
    # F-92：同族的两个类名漏项。入口 navigator.presentation /
    # navigator.storageBuckets 已在 _IOS_REMOVE_NAV_PROPS 删除，但类名是
    # 独立全局，`'StorageBucket' in window` 仍为 true。三方证据：
    # BCD safari_ios=false；Playwright WebKit 27.2 两者均 undefined；
    # 原生 fp-chromium 148 两者均暴露（StorageBucket 原型带
    # name/indexedDB/caches/estimate/expires/persisted/setExpires/persist）。
    "PresentationAvailability", "StorageBucket",
    "PeriodicSyncManager", "StorageBucketManager",
    # F-92：Gamepad 震动面。WebKit GamepadHapticActuator.idl 受
    # EnabledBySetting=GamepadVibrationActuatorEnabled 门控，而该开关默认值
    # 取自 defaultGamepadVibrationActuatorEnabled()
    # （Source/WebKit/Shared/WebPreferencesDefaultValues.cpp:246）：
    # 仅 HAVE(WIDE_GAMECONTROLLER_SUPPORT) / WPE / GTK 为 true，而
    # HAVE_WIDE_GAMECONTROLLER_SUPPORT 只在 PLATFORM(MAC) 下定义
    # （Source/WTF/wtf/PlatformHave.h:607）→ iOS 恒 false。
    # 故 iOS 既无该类名，也无 Gamepad.prototype.vibrationActuator
    # （后者见 _IOS_DROP_PROTO_MEMBERS）。BCD safari_ios=false 与此一致；
    # Playwright WebKit 显示存在是 macOS 构建口径，不可直接套 iOS。
    "GamepadHapticActuator",
    # 调度器 / 性能观测（safari_ios 全 false；IdleDeadline 仅 preview）
    "Scheduling", "TaskController", "TaskSignal", "TaskPriorityChangeEvent",
    "TaskAttributionTiming", "Observable", "IdleDeadline", "LayoutShift",
    "LayoutShiftAttribution", "Profiler", "PerformanceLongTaskTiming",
    "PerformanceScriptTiming", "PerformanceLongAnimationFrameTiming",
    "PerformanceElementTiming", "PerformanceTimingConfidence",
    # 视口 / 虚拟键盘（KeyboardLayoutMap 已在上面）
    "Viewport", "VirtualKeyboard", "VirtualKeyboardGeometryChangeEvent",
    # 凭证 / 联邦身份（Credential / CredentialsContainer 保留；DigitalCredential
    # 是 iOS 26+ 才有的版本门控项，见 _IOS_MIN_VERSIONS）
    "FederatedCredential", "PasswordCredential", "NavigatorLogin",
    "NavigatorManagedData",
    # F-75 剩余零散面
    "BackgroundFetchRecord", "BackgroundFetchRegistration",
    "BatteryManager", "BrowserCaptureMediaStreamTrack",
    "NavigationPrecommitController", "GPUError", "VisibilityStateEntry",
    "CSSMarginRule", "CSSPositionValue", "ClipboardChangeEvent", "SnapEvent",
    "HTMLGeolocationElement", "IntegrityViolationReportBody", "PaymentManager",
    "TextFormatUpdateEvent", "WindowControlsOverlayGeometryChangeEvent",
    "MediaStreamEvent", "TimelineTriggerRange", "TimelineTriggerRangeList",
    "SpeechGrammar", "SpeechGrammarList", "SpeechRecognitionPhrase",
    "webkitRequestFileSystem", "webkitResolveLocalFileSystemURL",
    # ---- F-64 / F-65 / F-71：对象成员与 Document 原型面 ----
    # F-64：window.chrome 是 Chrome 的经典探测向量，iOS Safari 恒无。
    # F-169：但**不能删**——Blink 的该属性 own 且 configurable:false，delete
    # 静默失败，只能把值改成 undefined；而 creepjs 的 hasBadChromeRuntime
    # （creep.js:5503）守卫是 `if (!('chrome' in window && 'runtime' in chrome))
    # return false;`，对 undefined 取 `in` 抛 TypeError 且落在内层 try 之外，
    # 会击穿 getHeadlessFeatures 的整块 try/catch，Headless 块退化成五行
    # blocked。原生桌面 Chromium 的 window.chrome 是 {loadTimes, csi, app}、
    # 同样没有 runtime，守卫第一支即 return false——留原生对象才与真机一致。
    # 代价：`'chrome' in window` 在 Blink 上恒为 true（内核层不可消除），
    # 属已知边界；注入器侧有对应说明（mobile_cdp_injector.py:3371）。
    # F-65：FLEDGE / Protected Audience 的 Navigator 方法面（类名在上）。
    # 拼写以 Blink 实际导出名为准（F-83 实测：真名是
    # deprecatedRun**AdAuction**EnforcesKAnonymity，此前误写成
    # deprecatedRunAuction…，delete 全部落空——18.6 上该 boolean 暴露为 false）。
    "adAuctionComponents", "canLoadAdAuctionFencedFrame",
    "clearOriginJoinedAdInterestGroups", "createAuctionNonce",
    "deprecatedRunAdAuctionEnforcesKAnonymity", "deprecatedURNToURL",
    "getInterestGroupAdAuctionData",
    # F-71：Document.prototype 的指针锁 / Page Lifecycle / Scroll Snap /
    # 隐私沙盆成员。pointerLock 族 BCD safari=10.1 但 safari_ios=false。
    # 全屏族 13 名（标准 5 + webkit 别名 8）按 F-76 翻转保留，不在此列；
    # ariaNotify 是 iOS 27+ 的版本门控项，见 _IOS_MIN_VERSIONS。
    "pointerLockElement", "exitPointerLock", "prerendering", "wasDiscarded",
    "featurePolicy", "hasPrivateToken", "hasRedemptionRecord",
    "hasUnpartitionedCookieAccess", "requestStorageAccessFor", "moveBefore",
    "onbeforexrselect", "onfreeze", "onpointerlockchange",
    "onpointerlockerror", "onpointerrawupdate", "onprerenderingchange",
    "onresume", "onscrollsnapchange", "onscrollsnapchanging",
    # 真机复核队列的两项：BCD safari=false 且 WebKit Bugzilla 零记录，
    # 按删除处理（若真机复核推翻，从本表移除即可）
    "AnimationTrigger", "FontFaceSetLoadEvent",
    # F-21：第三轮新出现的 Chromium 专有入口（BCD safari_ios=false）。
    # Chrome 内置 AI（138+）：WebKit 无 LLM/翻译/摘要任何一面。
    "LanguageDetector", "LanguageModel", "Translator", "Summarizer",
    # 画中画 / 编辑
    "DocumentPictureInPicture", "DocumentPictureInPictureEvent",
    # 屏幕 / 窗口管理（Window Management API 全是 Chromium 专有）
    "getScreenDetails", "ScreenDetailed", "ScreenDetails", "CookieStoreManager",
    # 采集 / 传感器 / 输入
    "CaptureController", "CreateMonitor", "PressureObserver", "PressureRecord",
    "OrientationSensor", "KeyboardLayoutMap",
    # 媒体
    "ImageTrack", "ImageTrackList", "AudioPlaybackStats", "FontData",
    # Privacy Sandbox / 归因
    "IdentityCredential", "IdentityProvider", "IdentityCredentialError",
    "fetchLater", "FetchLaterResult",
    # 其它 Chromium 新增面
    "NotRestoredReasonsDetails",
    # ---- F-90：Web Notifications / Push / 徽标（iOS 普通浏览上下文全无）----
    # WebKit 的这族能力全部由 WebPreferences 开关门控，而 iOS 的默认值是关：
    #   NotificationsEnabled      defaultValue PLATFORM(IOS_FAMILY)=false
    #   PushAPIEnabled            defaultValue false（embedder 未打开）
    #   AppBadgeEnabled           defaultValue false
    #   NotificationEventEnabled  defaultValue ENABLE(NOTIFICATION_EVENT) &&
    #                             !PLATFORM(IOS_FAMILY)（见
    #                             Source/WTF/Scripts/Preferences/UnifiedWebPreferences.yaml）
    # 对应 IDL 全部带 EnabledBySetting（Notification.idl / PushManager.idl /
    # ServiceWorkerRegistration+PushAPI.idl / ServiceWorkerRegistration.idl 的
    # showNotification、getNotifications / NavigatorBadge.idl），所以普通页
    # 上这些名字与成员**整体缺席**，不是「存在但被拒」。
    # 旁证：iOS TestExpectations「# Web Notifications are not supported on
    # iOS.」+ b/240579 对 shownotification-* / getnotifications* /
    # http/tests/push-api 整片 skip；BCD safari_ios 对 Notification 记
    # partial_implementation（"undefined, unless the page is a web app saved
    # to the home screen"），PushManager 16.4 同款 Home Screen 口径，
    # Window.pushManager 则是 18.4。
    # Playwright WebKit 27.2（macOS 构建）这 9 名全 undefined，与上述一致。
    # 本仓库没有 standalone / manifest 判定（navigator.standalone 恒 false），
    # 因此所有 iOS profile 都按普通浏览上下文处理。
    #
    # F-90 勘误（推翻 F-66 的「PushManager 禁删」与 F-102 备注的「勿连带删除」）：
    # 那两条判断依据的是 BCD 的 16.4 条目，而 BCD 对 PushManager /
    # PushSubscription / PushSubscriptionOptions / NotificationEvent 的
    # safari_ios 条目都带同一条 notes——"Notifications are supported in web
    # apps saved to the home screen"。PushManager.idl 与
    # ServiceWorkerRegistration+PushAPI.idl 双双标 EnabledBySetting=PushAPIEnabled，
    # 而该 pref 的 defaultValue 为 false（status: embedder，WebKit 未在
    # iOS 上打开）；NotificationsEnabled 更是显式 PLATFORM(IOS_FAMILY)=false。
    # 因此普通浏览上下文里整族缺席，与 Notification 同因同源，必须同删。
    # Window.pushManager（18.4）同属 PushAPIEnabled 门控，一并覆盖。
    "Notification", "NotificationEvent", "NotificationPermission",
    "PushManager", "PushSubscription", "PushSubscriptionOptions",
    "PushEvent", "PushMessageData", "PushPermissionState",
]

# ---- Android Chrome（真实 Chrome Android 无、桌面内核有）----
# BCD chrome_android=false：EyeDropper / FileSystemObserver（133）/
# queryLocalFonts（103）/ documentPictureInPicture（116）/ launchQueue（102）。
# 注意反向：WebHID 已由 _ANDROID_REMOVE_NAV_PROPS 处理；WebUSB/Web Serial/
# FSA 选择器（chrome_android=132）/ 传感器家族 / IdleDetector / OTPCredential /
# Ink 等在 Chrome Android 真实存在，不得误删（回归断言锁定）。
_ANDROID_REMOVE_GLOBALS = [
    "EyeDropper", "FileSystemObserver", "queryLocalFonts",
    "documentPictureInPicture", "launchQueue",
    # F-78（BCD chrome_android=false + 运行时仍暴露，双证据）：WebHID 族 /
    # Window Controls Overlay / Document PiP / Chrome 内置 AI / 杂项。
    "HID", "HIDDevice", "HIDConnectionEvent", "HIDInputReportEvent",
    "WindowControlsOverlay", "WindowControlsOverlayGeometryChangeEvent",
    "DocumentPictureInPicture", "DocumentPictureInPictureEvent",
    "RestrictionTarget", "SpeechRecognitionPhrase",
    "LanguageModel", "Translator", "Summarizer",
]

# ---- Worker 全局作用域内的同名差集 ----
# 内核在 Worker 里同样暴露（实测）：IdleDetector / WebSocketStream /
# ImageDecoder / FileSystemObserver / Scheduler / scheduler（对象）。
# WebKit 全部未实现（BCD safari_ios=false）；Chrome Android 侧除
# FileSystemObserver 外均存在（chrome_android=false → Android 也删）。
_IOS_REMOVE_WORKER_GLOBALS = [
    "IdleDetector", "WebSocketStream", "ImageDecoder", "FileSystemObserver",
    "Scheduler", "scheduler",
]
_ANDROID_REMOVE_WORKER_GLOBALS = ["FileSystemObserver"]

# ---- Worker 全局面：iOS 真机有、fp-chromium Worker 没有的接口（F-37 / F-38）----
# 证据：Playwright WebKit 27.2 的 Worker 作用域实测（`/tmp/wkworker/probe.py`、
# `kinds.py`、`vals.py`、`vals2.py`）对 fp-chromium 148 的同一探针
# （`/tmp/wkworker/chprobe.py`）取差集——WebKit 多 40 项、Chromium 多 31 项。
#
# 只登记「真机有、仿真缺」方向；反方向的 31 项走删除清单（见
# _IOS_WORKER_GLOBAL_DROPS）。字段口径与页面 _IOS_ADD_SPEC["interfaces"] 一致：
#   construct  'illegal' = 带不带 new 都抛 Illegal constructor
#              'args'    = 必须 new，缺参抛 Not enough arguments
#              'ok'      = 必须 new，可零参
#   extends    真机原型链父接口（Worker 作用域内同名，实测 chain）
#   init       实例初值；members '!' = 方法、'=' = 可写属性、裸名 = 只读；
#              方法名后接 ':N' 表示真机 Function#length 为 N（缺省 0）
#   statics    构造器静态成员：'!name' = 方法（按真机返回值）
#   values     静态方法的固定返回（isConfigSupported 类，见注入器）
#   brandErr   'webkit' = 真机文案（The X.m getter can only be used on
#              instances of X / Can only call X.m on instances of X）；
#              缺省沿用注入器既有的 DOM 文案 'Illegal invocation'
#   noBrand    真机不做接收者校验的成员（传 {} 也照常返回，实测）
_IOS_WORKER_ADD_INTERFACES: Dict[str, Any] = {
    # ---- WebCodecs（WebKit 的 Worker 侧比 Chromium 多 Audio/VideoDecoder+Encoder）----
    "AudioDecoder": {"construct": "args", "length": 1, "extends": "EventTarget",
                     "init": {"state": "unconfigured", "decodeQueueSize": 0},
                     "members": ["state", "decodeQueueSize", "=ondequeue",
                                 "!configure:1", "!decode:1", "!flush",
                                 "!reset", "!close"],
                     "noBrand": ["flush"],
                     "statics": ["!isConfigSupported"]},
    "AudioEncoder": {"construct": "args", "length": 1, "extends": "EventTarget",
                     "init": {"state": "unconfigured", "encodeQueueSize": 0},
                     "members": ["state", "encodeQueueSize", "=ondequeue",
                                 "!configure:1", "!encode:1", "!flush",
                                 "!reset", "!close"],
                     "noBrand": ["flush"],
                     "statics": ["!isConfigSupported"]},
    "VideoDecoder": {"construct": "args", "length": 1, "extends": "EventTarget",
                     "init": {"state": "unconfigured", "decodeQueueSize": 0},
                     "members": ["state", "decodeQueueSize", "=ondequeue",
                                 "!configure:1", "!decode:1", "!flush",
                                 "!reset", "!close"],
                     "noBrand": ["flush"],
                     "statics": ["!isConfigSupported"]},
    "VideoEncoder": {"construct": "args", "length": 1, "extends": "EventTarget",
                     "init": {"state": "unconfigured", "encodeQueueSize": 0},
                     "members": ["state", "encodeQueueSize", "=ondequeue",
                                 "!configure:1", "!encode:1", "!flush",
                                 "!reset", "!close"],
                     "noBrand": ["flush"],
                     "statics": ["!isConfigSupported"]},
    # ---- 媒体轨道配置字典 ----
    "AudioTrackConfiguration": {"construct": "illegal", "length": 0,
                                "members": ["codec", "sampleRate",
                                            "numberOfChannels", "bitrate"]},
    "VideoTrackConfiguration": {"construct": "illegal", "length": 0,
                                "members": ["codec", "width", "height",
                                            "bitrate", "framerate", "colorSpace"]},
    # ---- Managed Media Source（iOS 17.1+）----
    "ManagedMediaSource": {"construct": "ok", "length": 0,
                           "extends": "MediaSource",
                           "init": {"quality": "high", "streaming": False},
                           "members": ["quality", "=onqualitychange",
                                       "streaming", "=onstartstreaming",
                                       "=onendstreaming"],
                           "statics": ["!isTypeSupported"]},
    "ManagedSourceBuffer": {"construct": "illegal", "length": 0,
                            "extends": "SourceBuffer",
                            "members": ["=onbufferedchange"]},
    "BufferedChangeEvent": {"construct": "args", "length": 2,
                            "extends": "Event",
                            "members": ["addedRanges", "removedRanges"]},
    # ---- MediaStreamTrack 家族 ----
    "MediaStreamTrack": {"construct": "illegal", "length": 0,
                         "extends": "EventTarget",
                         "init": {"enabled": True, "muted": False,
                                  "readyState": "live", "contentHint": ""},
                         "members": ["kind", "id", "label", "=enabled",
                                     "=contentHint", "muted", "=onmute",
                                     "=onunmute", "=onconfigurationchange",
                                     "readyState", "=onended", "!clone", "!stop",
                                     "!getCapabilities", "!getConstraints",
                                     "!getSettings", "!applyConstraints"],
                         "noBrand": ["applyConstraints"]},
    "MediaStreamTrackProcessor": {"construct": "args", "length": 1,
                                  "members": ["readable"]},
    "VideoTrackGenerator": {"construct": "ok", "length": 0,
                            "init": {"muted": False},
                            "members": ["writable", "=muted", "track"]},
    # ---- 规范内、Chromium Worker 侧缺失的杂项 ----
    "NavigationPreloadManager": {"construct": "illegal", "length": 0,
                                 "members": ["!enable", "!disable",
                                             "!setHeaderValue:1", "!getState"],
                                 "noBrand": ["enable", "disable",
                                             "setHeaderValue", "getState"]},
    "Report": {"construct": "illegal", "length": 0,
               "members": ["type", "url", "body", "!toJSON"]},
    "FontFaceSet": {"construct": "illegal", "length": 1, "noNew": "args",
                    "extends": "EventTarget",
                    "members": ["size", "=onloading", "=onloadingdone",
                                "=onloadingerror", "ready", "status",
                                "!has:1", "!add:1", "!delete:1", "!clear",
                                "!load:1", "!check:1", "!entries", "!keys",
                                "!values", "!forEach:1"],
                    "noBrand": ["ready", "load"]},
    "StylePropertyMapReadOnly": {
        "construct": "illegal", "length": 0,
        "members": ["size", "!get:1", "!getAll:1", "!has:1", "!entries",
                    "!keys", "!values", "!forEach:1"]},
    # ---- CSS Typed OM（Worker 里整族都在，Chromium Worker 只有 CSSSkewX/Y）----
    "CSSStyleValue": {"construct": "illegal", "length": 0,
                      "members": ["!toString"]},
    "CSSImageValue": {"construct": "illegal", "length": 0,
                      "extends": "CSSStyleValue", "members": []},
    "CSSKeywordValue": {"construct": "args", "length": 1,
                        "extends": "CSSStyleValue", "members": ["=value"]},
    "CSSNumericValue": {"construct": "illegal", "length": 0,
                        "extends": "CSSStyleValue",
                        "members": ["!add", "!sub", "!mul", "!div", "!min",
                                    "!max", "!equals", "!to:1", "!toSum",
                                    "!type"]},
    "CSSUnitValue": {"construct": "args", "length": 2,
                     "extends": "CSSNumericValue",
                     "members": ["=value", "unit"]},
    "CSSUnparsedValue": {"construct": "args", "length": 1,
                         "extends": "CSSStyleValue",
                         "members": ["length", "!entries", "!forEach",
                                     "!keys", "!values"],
                         "noBrand": ["entries", "keys", "values", "forEach"]},
    "CSSVariableReferenceValue": {"construct": "args", "length": 1,
                                  "members": ["=variable", "fallback"]},
    "CSSNumericArray": {"construct": "illegal", "length": 0,
                        "members": ["length", "!entries", "!forEach",
                                    "!keys", "!values"],
                        "noBrand": ["entries", "keys", "values", "forEach"]},
    "CSSMathValue": {"construct": "illegal", "length": 0,
                     "extends": "CSSNumericValue", "members": ["operator"]},
    "CSSMathSum": {"construct": "args", "length": 0,
                   "extends": "CSSMathValue",
                   "init": {"operator": "sum"}, "members": ["values"]},
    "CSSMathProduct": {"construct": "args", "length": 0,
                       "extends": "CSSMathValue",
                       "init": {"operator": "product"}, "members": ["values"]},
    "CSSMathMin": {"construct": "args", "length": 0,
                   "extends": "CSSMathValue",
                   "init": {"operator": "min"}, "members": ["values"]},
    "CSSMathMax": {"construct": "args", "length": 0,
                   "extends": "CSSMathValue",
                   "init": {"operator": "max"}, "members": ["values"]},
    "CSSMathNegate": {"construct": "args", "length": 1,
                      "extends": "CSSMathValue",
                      "init": {"operator": "negate"}, "members": ["value"]},
    "CSSMathInvert": {"construct": "args", "length": 1,
                      "extends": "CSSMathValue",
                      "init": {"operator": "invert"}, "members": ["value"]},
    "CSSMathClamp": {"construct": "args", "length": 3,
                     "extends": "CSSMathValue",
                     "init": {"operator": "clamp"},
                     "members": ["lower", "value", "upper"]},
    "CSSTransformComponent": {"construct": "illegal", "length": 0,
                              "init": {"is2D": True},
                              "members": ["=is2D", "!toMatrix", "!toString"]},
    "CSSTransformValue": {"construct": "args", "length": 1,
                          "extends": "CSSStyleValue",
                          "members": ["length", "is2D", "!toMatrix",
                                      "!entries", "!forEach", "!keys",
                                      "!values"],
                          "noBrand": ["entries", "keys", "values", "forEach"]},
    "CSSTranslate": {"construct": "args", "length": 2,
                     "extends": "CSSTransformComponent",
                     "members": ["=x", "=y", "=z"]},
    "CSSRotate": {"construct": "args", "length": 1,
                  "extends": "CSSTransformComponent",
                  "init": {"x": 0, "y": 0, "z": 1},
                  "members": ["=x", "=y", "=z", "=angle"]},
    "CSSScale": {"construct": "args", "length": 2,
                 "extends": "CSSTransformComponent",
                 "init": {"z": 1},
                 "members": ["=x", "=y", "=z"]},
    "CSSSkew": {"construct": "args", "length": 2,
                "extends": "CSSTransformComponent",
                "members": ["=ax", "=ay"]},
    "CSSPerspective": {"construct": "args", "length": 1,
                       "extends": "CSSTransformComponent",
                       "members": ["=length"]},
    "CSSMatrixComponent": {"construct": "args", "length": 1,
                           "extends": "CSSTransformComponent",
                           "members": ["=matrix"]},
    # ---- ServiceWorker 家族（F-77 D3）----
    # 三个接口的 IDL 都是 Exposed=(Window,Worker)，真机 WebKit 的 Worker
    # 全局三者俱全；Chromium 的 Worker 只有 ServiceWorkerRegistration。
    # 接收者校验文案实测（/tmp/f77p/swbrand.py wk）：
    #   「The X.m getter can only be used on instances of X」
    #   「Can only call X.m on instances of X」
    # 与注入器 __wkBrandErrW 的默认文案一致，故不写 brandErr / noBrand。
    "ServiceWorker": {
        "construct": "illegal", "length": 0, "extends": "EventTarget",
        "members": ["scriptURL", "state", "=onstatechange", "=onerror",
                    "!postMessage:1"]},
    "ServiceWorkerContainer": {
        "construct": "illegal", "length": 0, "extends": "EventTarget",
        "init": {"controller": None},
        "members": ["controller", "ready", "=oncontrollerchange",
                    "=onmessage", "=onmessageerror", "!register:1",
                    "!getRegistration", "!getRegistrations",
                    "!startMessages"]},
    # repair：Chromium 的 Worker 已有该接口，构造文案是 Blink 形态
    # （`Failed to construct 'ServiceWorkerRegistration': Illegal constructor`），
    # 换成真机的裸文案并把 constructor 挪回原型首位即可。成员不动——多出的
    # paymentManager / backgroundFetch / pushManager 等由 embedder 设置门控，
    # Playwright 基线与真机 Safari 本就不同源，页面侧保持同一口径，不在
    # Worker 里单删（否则跨上下文自相矛盾）。
    "ServiceWorkerRegistration": {
        "construct": "illegal", "length": 0, "repair": True, "members": []},
}

# Worker 全局面反方向：fp-chromium Worker 有、真机 WebKit Worker 没有。
# 与页面 _IOS_REMOVE_GLOBALS 是两套表（两边的缺口不同）：页面里被删的
# PushManager / SyncManager / NetworkInformation 等在这里同样要删，而
# 页面存在的 BarcodeDetector / URLPattern 在 Worker 里两侧都有、不能删。
# Worker 的 WebKit 侧删除清单 = 本表 + _IOS_REMOVE_WORKER_GLOBALS（旧表，
# 实测 WebKit Worker 同样没有 IdleDetector / WebSocketStream / ImageDecoder /
# FileSystemObserver / Scheduler）。
_IOS_WORKER_GLOBAL_DROPS = [
    # Blink 存储/后台同步/推送（WebKit Worker 均无；PushManager 只挂在页面
    # ServiceWorkerRegistration 上，不在 Worker 全局）
    # Notification：WebKit 的 IDL 虽是 Exposed=(Window,DedicatedWorker,
    # ServiceWorker)，但 NotificationsEnabled 在 PLATFORM(IOS_FAMILY) 默认
    # false，Worker 全局同样缺席；Blink 的 DedicatedWorker 有 Notification。
    "Notification",
    "PushManager", "PushSubscription", "PushSubscriptionOptions",
    "SyncManager", "PeriodicSyncManager",
    "BackgroundFetchManager", "BackgroundFetchRegistration",
    "BackgroundFetchRecord",
    # Blink 调度/任务优先级
    "TaskController", "TaskSignal", "TaskPriorityChangeEvent",
    # 传感器与压力观测
    "PressureObserver", "PressureRecord",
    # 媒体
    "SourceBufferList", "WebSocketError",
    # 显式资源管理（V8 专有，iOS 无）
    "AsyncDisposableStack", "DisposableStack", "SuppressedError",
    # Blink 杂项
    "CropTarget", "RestrictionTarget", "UserActivation", "Observable",
    "Subscriber", "QuotaExceededError", "WebGLObject",
    # UA-CH / Network Information：真机 iOS Worker 的 WorkerNavigator 没有
    # 这两个接口（页面侧 iOS 同样删除，见 _IOS_REMOVE_GLOBALS）
    "NavigatorUAData", "NetworkInformation",
    # webkit* 前缀的 FileSystem 遗留别名：真机 iOS 页面有
    # webkitRequestFileSystem，但 Worker 里 WebKit 一个都不暴露
    "webkitRequestFileSystem", "webkitRequestFileSystemSync",
    "webkitResolveLocalFileSystemURL", "webkitResolveLocalFileSystemSyncURL",
]

# Worker 里真机 WebKit 的 WorkerNavigator 原型面（WebKit 27.2 Worker 内实测）：
# 只有这 16 个成员（全部 enumerable getter），Chromium 的 WorkerNavigator 多
# deviceMemory / userAgentData / connection 等。删除后 Worker 内
# `'deviceMemory' in navigator` 才与真机一致（页面侧已有的 removeProps
# 复用同表，这里只补 Worker 独有的项）。
#
# 注意 globalPrivacyControl **不在**真机列表里：WebKit 的
# NavigatorGlobalPrivacyControl.idl 用 [EnabledBySetting=GlobalPrivacyControlEnabled]
# 门控，未由 embedder 打开时属性不出现（WebKit commit c7b5fcf1c5 / bug 319630）。
# Worker 侧的 WorkerNavigator 也不 include NavigatorGlobalPrivacyControl
# （Navigator.idl 才 include，见 WebKit main），所以 Worker 里应当缺席，
# 而不是恒 false——注入器不再合成该成员。
_IOS_WORKER_NAV_PROTO = [
    "hardwareConcurrency", "gpu", "mediaCapabilities", "permissions", "locks",
    "appCodeName", "appName", "appVersion", "platform", "product", "userAgent",
    "language", "languages", "onLine", "serviceWorker", "storage",
]

# Worker 独有的全局项（真机 WebKit Worker 有、Chromium Worker 全局没有）。
# WebKit 把 ServiceWorker 家族暴露在 Worker 全局（WorkerGlobalScope.idl 的
# ServiceWorkerContainer 与 ServiceWorker 注册表），而 Chromium 的 Worker
# 只有 ServiceWorkerRegistration。
#   serviceWorker  —— WorkerNavigator.prototype 上的 getter（enumerable true、
#                     get.length 0、无 setter），返回容器实例；
#   ServiceWorkerContainer —— 容器构造器：Illegal constructor、length 0、
#                     extends EventTarget，实例零自有属性、无公开构造路径。
# 接口本体在 _IOS_WORKER_ADD_INTERFACES 里（同一套补建机制），这里只登记
# 「实例 + 原型 getter」的接法。
_IOS_WORKER_ADD_GLOBALS: Dict[str, Any] = {
    "serviceWorker": {"instance": "ServiceWorkerContainer"},
}


# --------------------------------------------------------------------------
# 原型级成员差集（F-26 / F-27）
# --------------------------------------------------------------------------
# 现有删除机制只处理「全局名 / navigator 名」。一批 Chromium 残留挂在
# **接口原型**上，`'addStream' in RTCPeerConnection.prototype` 这类一行探测
# 即可判假；它们不能靠删全局（构造器本身是真机也有的），只能按成员删。
#
# 证据：BCD safari_ios 逐项 + WebKit 27.2 运行期实测（/tmp/batch2_members.py）。
# 判定「该删」的条件是 **BCD safari_ios=false（或记录为已移除）且 WebKit
# 运行期实测缺席**——方向存疑项（GestureEvent.rotation/scale、
# DeviceOrientationEvent.webkitCompass*）一律不动，避免反向制造判据。
#
# 键 = window 上的接口名，值 = 该接口原型上要删的成员名。
# 顺序无意义；删除后若仍在（configurable:false），注入层以 undefined getter
# 兜底（与 removeProp 同语义）。
_IOS_DROP_PROTO_MEMBERS: Dict[str, List[str]] = {
    # F-26：iOS 12 起移除的 WebRTC 遗留面（BCD addStream：safari_ios 11 →
    # removed 12），Chromium 至今保留。
    "RTCPeerConnection": [
        "addStream", "removeStream", "createDTMFSender",
        "getLocalStreams", "getRemoteStreams",
        "onaddstream", "onremovestream",
    ],
    # F-27：FontFace 的 Chromium 描述符扩展。WebKit FontFace.idl 只有
    # ascent/descent/lineGapOverride（且由 CSSFontFaceMetricOverrideDescriptors
    # 开关门控，iOS 未开）、没有 variationSettings/variant。
    # 注意 width 是反向项（WebKit 有、仿真缺）——见 _IOS_ADD_PROTO_MEMBERS。
    "FontFace": [
        "ascentOverride", "descentOverride", "lineGapOverride",
        "variationSettings", "variant",
    ],
    # BCD incremental：safari_ios removed 17.2
    "HTMLInputElement": ["incremental"],
    # BCD pointerevents.persistentDeviceId：safari_ios=false
    "PointerEvent": ["persistentDeviceId"],
    # BCD 均 safari_ios=false
    "IntersectionObserver": ["delay", "trackVisibility"],
    "XMLHttpRequest": ["setAttributionReporting", "setPrivateToken"],
    "Element": [
        "setHTML", "moveBefore", "elementTiming", "fakeShadowRoot",
        "activeViewTransition",
        # F-105：Element.startViewTransition 是 Chrome 147 新增的重载；
        # Document.prototype 上那名自 iOS 18 起真机就有，**保留**。
        "startViewTransition",
        # F-115：Blink 把 onbeforecopy/cut/paste 挂在 Element，WebKit 挂在
        # HTMLElement（下方 add-spec 补回）；onsearch 是 Blink 专有事件。
        "onbeforecopy", "onbeforecut", "onbeforepaste", "onsearch",
        # F-141：ariaNotify 是 `EnabledBySetting=IsARIANotifyEnabled` 的
        # Element 方法，该 pref 在出货版 Safari 恒为 false（不是版本门控，
        # 按全局名门控对它无效——它不是全局），故走原型删除表。
        # WebKit 27.2 实测 Element/Document 两端皆缺席（/tmp/f476）。
        "ariaNotify",
        # F-160：Blink 在 Element.prototype 上留了 requestPointerLock 的
        # 兼容垫片（真实现挂在 Document）。WebKit 27.2 实测 Element /
        # Document 两侧都没有指针锁面（requestPointerLock / exitPointerLock /
        # pointerLockElement / onpointerlockchange / onpointerlockerror 全缺席，
        # /tmp/f507）。Element 侧直接删；Document 侧不补，保持 WebKit 缺席。
        "requestPointerLock",
    ],
    "ElementInternals": [
        # F-159：Blink 的 ElementInternals 比 WebKit 多三名字符串反射属性
        # （ariaColIndexText / ariaRowIndexText / ariaDescription）。WebKit
        # `AriaAttributes.idl` 只在 Element 一侧声明它们，ElementInternals
        # 走的是 `ElementInternals.idl` 里另列的 ARIA mixin 面，缺这三名
        # （实测 WebKit 27.2 `/tmp/f485`、`/tmp/f498`）。
        "ariaColIndexText", "ariaRowIndexText", "ariaDescription",
    ],
    "Document": [
        "exitPointerLock", "requestStorageAccessFor", "featurePolicy",
        "hasPrivateToken", "hasRedemptionRecord", "hasUnpartitionedCookieAccess",
        "wasDiscarded", "prerendering", "pointerLockElement",
        # F-141：见上条 Element 项。
        "ariaNotify",
    ],
    # isExtended 是 F-09 的同一项；onchange 为 WebKit 未实现的 Screen 事件
    "Screen": ["isExtended", "onchange"],
    # iOS Safari exposes only the read-only orientation state.  Blink keeps the
    # lock()/unlock() pair on ScreenOrientation.prototype, although the WebKit
    # IDL does not expose either method on iPhone/iPad.  Leaving the pair in
    # place is a one-line capability probe (`'lock' in screen.orientation`) and
    # also makes the injected type/angle state look internally inconsistent.
    "ScreenOrientation": ["lock", "unlock"],
    "OffscreenCanvas": ["oncontextlost", "oncontextrestored"],
    "ShadowRoot": ["getSelection", "pointerLockElement", "setHTML"],
    "VideoFrame": ["flip", "rotation", "metadata"],
    # F-102：Blink 的 ServiceWorkerRegistration 扩展成员。前五项是 BCD
    # safari_ios=false 的 Blink 专有面；pushManager / cookies 同为 pref 门控
    # （PushAPIEnabled 默认 false、CookieStoreManagerEnabled 默认 false），
    # 在 iOS 普通浏览上下文里同样缺席——Playwright WebKit 27.2 的
    # has_touch+is_mobile 上下文（走 iOS_FAMILY 默认值）实测这六项全部 absent，
    # 与页面侧一致。navigationPreload（默认 true）不在此列。
    "ServiceWorkerRegistration": [
        "sync", "periodicSync", "backgroundFetch", "paymentManager",
        "cookies", "pushManager",
    ],
    "TrustedTypePolicyFactory": ["getTypeMapping"],
    "HighlightRegistry": ["highlightsFromPoint"],
    "ImageData": ["pixelFormat"],
    "VisualViewport": ["onscrollend"],
    "WebGLRenderingContext": [
        # 常量 RGB8/RGBA8 只存在于 WebGL2RenderingContext（WebKit 实测），
        # 但 WebIDL 常量在接口对象与原型上都是不可配置属性，删不掉；这里
        # 只处理可删/可遮蔽的原型成员。
        # F-106：Blink 在 WebGL1/2 两个接口上各生成一份自有成员（WebGL2
        # 原型并不继承 WebGL1 的生成面），所以两个宿主都要删；只改键会把
        # WebGL1 重新露出来。
        "makeXRCompatible", "unpackColorSpace",
        "drawingBufferStorage", "drawingBufferFormat",
    ],
    "WebGL2RenderingContext": [
        "makeXRCompatible", "unpackColorSpace",
        "drawingBufferStorage", "drawingBufferFormat",
    ],
    "File": ["lastModifiedDate"],
    "EventTarget": ["when"],
    # Privacy Sandbox 归因属性（HTMLAnchor/Image/Button/Script 同族）
    "HTMLAnchorElement": [
        "attributionSrc", "interestForElement", "browsingTopics",
        "sharedStorageWritable",
    ],
    "HTMLImageElement": [
        "attributionSrc", "interestForElement", "browsingTopics",
        # F-113：F-27 的「同族」注释写了但名单漏了这名。
        "sharedStorageWritable",
    ],
    "HTMLButtonElement": ["interestForElement"],
    "HTMLScriptElement": ["attributionSrc"],
    # F-112：<area> 上的归因/兴趣组族（BCD safari_ios=false）。
    "HTMLAreaElement": ["attributionSrc", "interestForElement"],
    # F-111：iframe 的 Blink 专有 + Privacy Sandbox 反射属性。
    # loading 是 iOS 16.4+ 真机面，不在名单里。
    "HTMLIFrameElement": [
        "adAuctionHeaders", "allowPaymentRequest", "browsingTopics",
        "credentialless", "csp", "featurePolicy", "privateToken",
        "sharedStorageWritable",
    ],
    # F-103：Capture Handle 是桌面 Chrome 专有面——BCD 对 safari_ios 与
    # chrome_android 都是 false，因此这是少数「Android 也漏」的成员。
    "MediaStreamTrack": ["getCaptureHandle", "oncapturehandlechange"],
    # F-104：Canvas 2D 的 Blink 专有属性面。filter 自 iOS 18 起真机有
    # （BCD safari_ios=18），不在此列。
    "CanvasRenderingContext2D": [
        "fontKerning", "fontStretch", "fontVariantCaps", "isContextLost",
        "lang", "textRendering",
    ],
    # F-105：零散 Blink 原型成员。Element.ariaNotify 自 iOS 27 起真机有，
    # 不在此列；Element 与 Document 的 startViewTransition 见上方。
    "MediaMetadata": ["chapterInfo"],
    "MediaRecorder": ["audioBitrateMode"],
    "MediaKeys": ["getStatusForPolicy"],
    "Request": ["isHistoryNavigation", "targetAddressSpace"],
    # F-108：Blink 的媒体元素扩展。webkit*Decoded* 计数器是真机面，勿删。
    "HTMLMediaElement": ["captureStream", "controlsList", "loading"],
    # F-109：webkitSpeechRecognition 构造器本身 iOS 真机有，只清成员；
    # grammars 在 Chrome Android 33 起是真机面，Android 侧另表处理。
    "webkitSpeechRecognition": ["grammars", "phrases", "processLocally"],
    # F-114：HTMLElement 落点的事件处理器（F-08/F-71 只删了 window/Document）。
    # ontouch*/ongesture* 是 iOS 真机面，勿删。
    "HTMLElement": [
        "onbeforexrselect", "oncontextlost", "oncontextrestored",
        "onpointerrawupdate", "onscrollsnapchange", "onscrollsnapchanging",
        # F-105：Chromium 的 EditContext / 虚拟键盘策略反射属性。
        "editContext", "virtualKeyboardPolicy",
    ],
    # F-17 附带项：Chromium 的 AudioContext 输出设备选择 / 播放统计面
    # （BCD safari_ios=false）。注意这些成员实际定义在 BaseAudioContext /
    # AudioContext 各自的原型上，两边都尝试删。
    # F-96：onerror 是 Blink 独有的 EventHandler（Blink `audio_context.idl`
    # 有 `attribute EventHandler onerror`，WebKit `AudioContext.idl` 没有；
    # /tmp/f586 `onerror` 三端对照）。同表内一并删。
    "AudioContext": ["playbackStats", "setSinkId", "sinkId", "onsinkchange",
                     "onerror"],
    "BaseAudioContext": ["playbackStats"],
    # 真机实测（/tmp/wkprobe/f25all.py）：RTCDataChannel.reliable 是 Chromium
    # 专有（WebKit 原型无此成员）。
    "RTCDataChannel": ["reliable"],
    # ---- F-40 / F-49 / F-51：对象子属性与原型方法缺口 ----
    # 键既可以是接口构造器（删原型成员），也可以是命名空间对象（删自身成员，
    # 注入器按 `window[key].prototype || window[key]` 取宿主）。
    # F-40：BCD getDisplayMedia safari_ios 27、chrome_android=false——iOS 26.x
    # 与全量 Android 真机均无此入口，Chromium 内核有（实测原生
    # `MediaDevices.prototype.getDisplayMedia` = function）。
    # F-103：setCaptureHandleConfig 是桌面 Chrome 专有面（BCD safari_ios 与
    # chrome_android 均为 false），与 getDisplayMedia 同宿主。
    "MediaDevices": ["getDisplayMedia", "setCaptureHandleConfig"],
    # F-49：CSS Paint API 从未进入 Safari；paintWorklet 是 CSS 对象自身成员
    # （实测 `Object.getOwnPropertyNames(CSS)` 含 paintWorklet），删全局名无效。
    "CSS": ["paintWorklet"],
    # F-51：HTMLSelectElement.showPicker 是 Chromium 121+ 新增（BCD
    # safari_ios=false）；input.showPicker 真机 iOS 18.4+ 有，**禁删**。
    "HTMLSelectElement": ["showPicker"],
    # F-58：Blink 把 pageX/pageY/layerX/layerY 挪到 MouseEvent.prototype，
    # UIEvent 实例上不再有（实测 `'pageX' in new UIEvent('x')` = false）；
    # WebKit 的 UIEvent.prototype 仍是 8 名口径。sourceCapabilities 是
    # Blink 专有（WebKit 无）。
    "UIEvent": ["sourceCapabilities"],
    # F-92：iOS 的 Gamepad 不带震动扩展（开关默认值见
    # _IOS_REMOVE_GLOBALS 里 GamepadHapticActuator 的取证）。WebKit 是
    # partial 接口声明，成员落在 Gamepad.prototype 上；Blink 同位置，按名删。
    "Gamepad": ["vibrationActuator"],
}


# --------------------------------------------------------------------------
# 权限查询口径（F-20）
# --------------------------------------------------------------------------
# navigator.permissions.query({name}) 的真机 iOS 结果面与桌面 Chromium 差异
# 很大，且两个方向都有。表值语义：
#   "granted" / "prompt" / "denied" → Promise.resolve({state: <值>})
#   "NotSupportedError" / "TypeError" → Promise.reject(对应 DOMException)
# 未列入的名称一律回退内核原生结果（避免把真机本就一致的面改坏）。
# 证据：BCD safari_ios 逐项 + WebKit 源码头文件（Permissions.cpp 只实现
# geolocation/notifications/push/…；未实现的名称抛 TypeError）。
_IOS_PERMISSIONS: Dict[str, str] = {
    # WebKit 从未实现的权限名 → DOMException NotSupportedError
    "accelerometer": "NotSupportedError",
    "gyroscope": "NotSupportedError",
    "magnetometer": "NotSupportedError",
    "midi": "NotSupportedError",
    "background-fetch": "NotSupportedError",
    "display-capture": "NotSupportedError",
    # 权限枚举里根本没有的名称 → TypeError（WebIDL 枚举转换失败）
    "background-sync": "TypeError",
    "clipboard-read": "TypeError",
    "clipboard-write": "TypeError",
    "persistent-storage": "TypeError",
    "local-fonts": "TypeError",
    "window-management": "TypeError",
    "payment-handler": "TypeError",
    # 真机为 prompt（未授权），桌面内核 headless 给 granted
    "screen-wake-lock": "prompt",
    "storage-access": "prompt",
    # 反向项：iOS 16.4+ 支持 Web Push，首访应为 prompt（内核桌面版无
    # push 权限名 → NotSupportedError）。UA 声明 >=16.4 时才可用，注入层
    # 无条件声明 16.4+ 机型（指纹库最低 iOS 版本已高于 16.4）。
    "push": "prompt",
}


def _ios_permissions(target_os: str) -> Dict[str, str]:
    return dict(_IOS_PERMISSIONS) if target_os == "ios" else {}


# --------------------------------------------------------------------------
# iOS 的 MediaTrackSupportedConstraints 白名单（F-94）
# --------------------------------------------------------------------------
# WebKit 不跑 Blink 那套「引擎全能力」表，而是逐项回填
# `MediaDevices::getSupportedConstraints()`（Source/WebCore/Modules/
# mediastream/MediaDevices.cpp），未回填的键一律缺席。桌面内核是 Blink，
# 36 项全集，检测站 `getSupportedConstraints().voiceIsolation` 一行即判。
#
# 版本差异（WebKit 发布分支逐支核对）：
#   · 618.x（Safari 17.x）：15 项，无 backgroundBlur / powerEfficient
#   · 619.x 起（Safari 18.0+）：17 项，加 backgroundBlur / powerEfficient
# 设备库最低档是 iOS 17.5.1，故按版本分两段。torch 在 618 的
# MediaDevices.cpp 里没有显式赋值，但 IDL 默认 `= true`，输出仍在。
# 键序是**字母序**而非 IDL 声明序：WebKit 的字典转换（convertDictionaryToJS）
# 按生成代码里的排序成员表遍历，真机 Object.keys 实测即字母序（/tmp/f589）。
_IOS_SUPPORTED_CONSTRAINTS_BASE: List[str] = [
    "aspectRatio", "deviceId", "displaySurface", "echoCancellation",
    "facingMode", "frameRate", "groupId", "height", "sampleRate", "sampleSize",
    "torch", "volume", "whiteBalanceMode", "width", "zoom",
]
# Safari 18.0（WebKit 619）起新增
_IOS_SUPPORTED_CONSTRAINTS_180: List[str] = ["backgroundBlur", "powerEfficient"]


def _ios_supported_constraints(target_os: str, os_version: Any = None) -> List[str]:
    """iOS 的 getSupportedConstraints 键集；非 iOS 返回空表（保持内核口径）。"""
    if target_os != "ios":
        return []
    names = list(_IOS_SUPPORTED_CONSTRAINTS_BASE)
    version = _parse_ios_version_local(os_version)
    if version is None or version >= (18,):
        names.extend(_IOS_SUPPORTED_CONSTRAINTS_180)
    return sorted(names)


def _parse_ios_version_local(value: Any) -> Optional[tuple]:
    text = str(value or "").strip()
    if not text:
        return None
    parts = []
    for chunk in text.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


# --------------------------------------------------------------------------
# Android 的 TTS 语音表（F-45）
# --------------------------------------------------------------------------
# 真机 Chrome Android 预装 Google TTS，`speechSynthesis.getVoices()` 返回
# 数十条；宿主 macOS 上 Blink 返回的是宿主语音（婷婷/Albert/Samantha…），
# 两者都不是 Android 真机形态。字段顺序为
#   [voiceURI, lang, localService, default]
#
# 名字与 lang 的形态**照抄 Chromium 的构造方式**，不能凭印象编：
# content/public/android/java/.../TtsPlatformImpl.java 遍历
# `Locale.getAvailableLocales()`，对 `isLanguageAvailable(locale) > 0` 且
# variant 为空的 locale 生成
#     name = locale.getDisplayLanguage() [+ " " + getDisplayCountry()]
#     lang = locale.toString()
# 即 en-US 设备上得到 "English United States" / "en_US"——名字是**语言+国家的
# 显示名**，不是 "Google xxx"；lang 是 Java 的 `_` 分隔形态而非 BCP-47 的 `-`。
# 早先按 "Google 中文（简体）" 造表是错的：Google 品牌名是**远程**语音的命名
# （remote=true → localService=false），把它和 localService=true 拼在一起是
# 自相矛盾，IPHey 的 BROWSER 判据直接给 "inconsistent browser fingerprint"，
# 分数卡在 60。改成源码口径后该判据消失（实测 60 → 90，余下只剩 location）。
# 每条都是本地引擎（native=true → remote=false → localService=true）。
_ANDROID_TTS_VOICES: List[List[Any]] = [
    ["English United States", "en_US", True, True],
    ["English United Kingdom", "en_GB", True, False],
    ["English India", "en_IN", True, False],
    ["English Australia", "en_AU", True, False],
    ["English Canada", "en_CA", True, False],
    ["Chinese China", "zh_CN", True, False],
    ["Chinese Taiwan", "zh_TW", True, False],
    ["Chinese Hong Kong", "zh_HK", True, False],
    ["Japanese Japan", "ja_JP", True, False],
    ["Korean Korea", "ko_KR", True, False],
    ["German Germany", "de_DE", True, False],
    ["French France", "fr_FR", True, False],
    ["Spanish Spain", "es_ES", True, False],
    ["Spanish United States", "es_US", True, False],
    ["Italian Italy", "it_IT", True, False],
    ["Portuguese Brazil", "pt_BR", True, False],
    ["Portuguese Portugal", "pt_PT", True, False],
    ["Russian Russia", "ru_RU", True, False],
    ["Thai Thailand", "th_TH", True, False],
    ["Vietnamese Vietnam", "vi_VN", True, False],
    ["Indonesian Indonesia", "in_ID", True, False],
    ["Hindi India", "hi_IN", True, False],
    ["Turkish Turkey", "tr_TR", True, False],
    ["Dutch Netherlands", "nl_NL", True, False],
    ["Polish Poland", "pl_PL", True, False],
    ["Arabic Egypt", "ar_EG", True, False],
]


def _android_tts_voices(target_os: str, locale: str = "") -> List[List[Any]]:
    """Android 身份的 TTS 语音表；iOS 保持空列表（真机 Safari 无手势时为空）。

    F-161：`default` 必须落在与 Intl locale 同语族的语音上。真机 Android
    的默认 TTS 语音跟随系统语言，因此 en-US 出口设备上默认语音一定是
    `en-*`。表原先写死 `zh-CN` 为 default，与 geoip 解析出的 `en-US` 并存
    时，creepjs 的 `voiceLangMismatch`（默认语音语族 ≠ Intl locale 语族）
    直接成立并把 Intl/Timezone 两行标红——这是比「语音表为空」更隐蔽的
    一致性缺陷。

    `locale` 为空时（旧调用方式）不设默认语音：宁可没有默认，也不给出与
    运行期 Intl 可能冲突的那一条。默认语音只设**一条**——creepjs 仅在
    `default && localService` 恰好 1 条时取默认值，多条会静默绕过判据。
    """
    if target_os != "android":
        return []
    rows = [list(v) for v in _ANDROID_TTS_VOICES]
    want = str(locale or "").strip().lower()
    default_idx = None
    if want:
        default_idx = _pick_default_voice(rows, want)
    for i, row in enumerate(rows):
        row[3] = (i == default_idx)
    return rows


def _pick_default_voice(rows: List[List[Any]], locale: str) -> Optional[int]:
    """按 locale 挑默认 TTS 语音的下标；无匹配语族返回 None。

    优先级：完全同 locale（`en-US` → `en_US`）＞ 同语族（`en-US` → `en_GB`
    等）。两侧都归一成 `-` 分隔再比：调用方给的是 BCP-47（`en-US`），而表里
    是 Java `Locale.toString()` 的 `_` 形态（`en_US`）。
    """
    def norm(s: str) -> str:
        return str(s or "").strip().lower().replace("_", "-")

    want = norm(locale)
    base = want.split("-")[0]
    same_family = None
    for i, row in enumerate(rows):
        lang = norm(row[1])
        if lang == want:
            return i
        if same_family is None and lang.split("-")[0] == base:
            same_family = i
    return same_family


# --------------------------------------------------------------------------
# 原型成员枚举顺序
# --------------------------------------------------------------------------
# `Object.getOwnPropertyNames(Ctor.prototype)` 的顺序是独立判据：JSC 按 IDL
# 声明序、V8 按字典序（且 constructor 排最后）。本表 = WebKit 27.2 实测
# （/tmp/f471，105 个构造器全扫；/tmp/f473 验重排不改变任何描述符身份、
# 47 项功能冒烟全过）。
#
# 未收录的构造器：非可配置成员删不掉、始终排在最前，收录了也对不齐——
#   * Node（18 名 ELEMENT_NODE 一族）、WebGL2RenderingContext（559 名常量）；
#   * Event / FileReader / KeyboardEvent / PerformanceNavigation / WebSocket /
#     XMLHttpRequest（各自的常量族在 V8 里钉在最前、JSC 里在尾部）。
# MediaDevices 顺序本可对齐，但 getDisplayMedia 按版本门控删除（F-40），
# 删除后目标序失效，故不收录（/tmp/f475 实测）。
# 值 = 目标顺序，逗号分隔；缺席成员自动跳过。
_IOS_PROTO_ORDER: Dict[str, str] = {
    "AbortController":         "constructor,signal,abort",
    "AbortSignal":             "constructor,aborted,reason,onabort,throwIfAborted",
    "Attr":                    "constructor,namespaceURI,prefix,localName,name,value,ownerElement,specified",
    "Blob":                    "constructor,size,type,slice,stream,text,arrayBuffer,bytes",
    "BroadcastChannel":        "constructor,name,onmessage,onmessageerror,postMessage,close",
    "CookieStore":             "constructor,onchange,get,getAll,set,delete",
    "Crypto":                  "constructor,subtle,getRandomValues,randomUUID",
    "CustomEvent":             "constructor,detail,initCustomEvent",
    "DOMMatrix":               "constructor,a,b,c,d,e,f,m11,m12,m13,m14,m21,m22,m23,m24,m31,m32,m33,m34,m41,m42,m43,m44,multiplySelf,preMultiplySelf,translateSelf,scaleSelf,scale3dSelf,rotateSelf,rotateFromVectorSelf,rotateAxisAngleSelf,skewXSelf,skewYSelf,invertSelf,setMatrixValue",
    "DOMPoint":                "constructor,x,y,z,w",
    "DOMQuad":                 "constructor,p1,p2,p3,p4,getBounds,toJSON",
    "DOMRect":                 "constructor,x,y,width,height",
    "DOMRectReadOnly":         "constructor,x,y,width,height,top,right,bottom,left,toJSON",
    "DOMTokenList":            "constructor,length,value,item,contains,add,remove,toggle,replace,supports,toString,entries,forEach,keys,values",
    "Element":                 "constructor,namespaceURI,prefix,localName,tagName,id,className,classList,slot,part,customElementRegistry,attributes,shadowRoot,onfocusin,onfocusout,onbeforeload,onwebkitneedkey,onwebkitpresentationmodechanged,onwebkitcurrentplaybacktargetiswirelesschanged,onwebkitplaybacktargetavailabilitychanged,role,ariaActiveDescendantElement,ariaAtomic,ariaAutoComplete,ariaBrailleLabel,ariaBrailleRoleDescription,ariaBusy,ariaChecked,ariaColCount,ariaColIndex,ariaColIndexText,ariaColSpan,ariaControlsElements,ariaCurrent,ariaDescribedByElements,ariaDescription,ariaDetailsElements,ariaDisabled,ariaErrorMessageElements,ariaExpanded,ariaFlowToElements,ariaHasPopup,ariaHidden,ariaInvalid,ariaKeyShortcuts,ariaLabel,ariaLabelledByElements,ariaLevel,ariaLive,ariaModal,ariaMultiLine,ariaMultiSelectable,ariaOrientation,ariaOwnsElements,ariaPlaceholder,ariaPosInSet,ariaPressed,ariaReadOnly,ariaRelevant,ariaRequired,ariaRoleDescription,ariaRowCount,ariaRowIndex,ariaRowIndexText,ariaRowSpan,ariaSelected,ariaSetSize,ariaSort,ariaValueMax,ariaValueMin,ariaValueNow,ariaValueText,scrollTop,scrollLeft,scrollWidth,scrollHeight,clientTop,clientLeft,clientWidth,clientHeight,currentCSSZoom,outerHTML,innerHTML,previousElementSibling,nextElementSibling,children,firstElementChild,lastElementChild,childElementCount,assignedSlot,hasAttributes,getAttributeNames,getAttribute,getAttributeNS,setAttribute,setAttributeNS,removeAttribute,removeAttributeNS,toggleAttribute,hasAttribute,hasAttributeNS,getAttributeNode,getAttributeNodeNS,setAttributeNode,setAttributeNodeNS,removeAttributeNode,attachShadow,closest,matches,webkitMatchesSelector,getElementsByTagName,getElementsByTagNameNS,getElementsByClassName,insertAdjacentElement,insertAdjacentText,animate,getAnimations,before,after,replaceWith,remove,getClientRects,getBoundingClientRect,checkVisibility,scrollIntoView,scroll,scrollTo,scrollBy,scrollIntoViewIfNeeded,insertAdjacentHTML,setPointerCapture,releasePointerCapture,hasPointerCapture,computedStyleMap,setHTMLUnsafe,getHTML,prepend,append,replaceChildren,querySelector,querySelectorAll",
    "ElementInternals":        "constructor,shadowRoot,form,willValidate,validity,validationMessage,labels,states,role,ariaActiveDescendantElement,ariaAtomic,ariaAutoComplete,ariaBrailleLabel,ariaBrailleRoleDescription,ariaBusy,ariaChecked,ariaColCount,ariaColIndex,ariaColSpan,ariaControlsElements,ariaCurrent,ariaDescribedByElements,ariaDetailsElements,ariaDisabled,ariaErrorMessageElements,ariaExpanded,ariaFlowToElements,ariaHasPopup,ariaHidden,ariaInvalid,ariaKeyShortcuts,ariaLabel,ariaLabelledByElements,ariaLevel,ariaLive,ariaModal,ariaMultiLine,ariaMultiSelectable,ariaOrientation,ariaOwnsElements,ariaPlaceholder,ariaPosInSet,ariaPressed,ariaReadOnly,ariaRelevant,ariaRequired,ariaRoleDescription,ariaRowCount,ariaRowIndex,ariaRowSpan,ariaSelected,ariaSetSize,ariaSort,ariaValueMax,ariaValueMin,ariaValueNow,ariaValueText,setFormValue,setValidity,checkValidity,reportValidity",
    "EventTarget":             "constructor,addEventListener,removeEventListener,dispatchEvent",
    "File":                    "constructor,name,lastModified,webkitRelativePath",
    "FormData":                "constructor,append,delete,get,getAll,has,set,entries,keys,values,forEach",
    "Geolocation":             "constructor,getCurrentPosition,watchPosition,clearWatch",
    "HTMLCanvasElement":       "constructor,width,height,getContext,toDataURL,toBlob,transferControlToOffscreen,captureStream",
    "HTMLCollection":          "constructor,length,item,namedItem",
    "HTMLDivElement":          "constructor,align",
    "HTMLFormElement":         "constructor,acceptCharset,action,autocomplete,enctype,encoding,method,name,noValidate,target,rel,relList,elements,length,submit,requestSubmit,reset,checkValidity,reportValidity",
    "HTMLIFrameElement":       "constructor,src,srcdoc,name,sandbox,allow,allowFullscreen,width,height,referrerPolicy,loading,contentDocument,contentWindow,align,scrolling,frameBorder,longDesc,marginHeight,marginWidth,getSVGDocument",
    "HTMLImageElement":        "constructor,alt,src,srcset,sizes,crossOrigin,useMap,isMap,width,height,naturalHeight,naturalWidth,complete,currentSrc,referrerPolicy,decoding,loading,name,lowsrc,align,hspace,vspace,longDesc,border,fetchPriority,x,y,decode",
    "HTMLScriptElement":       "constructor,text,innerText,textContent,htmlFor,event,charset,async,defer,src,type,crossOrigin,noModule,integrity,referrerPolicy,fetchPriority,blocking",
    "Headers":                 "constructor,append,delete,get,getSetCookie,has,set,entries,keys,values,forEach",
    "History":                 "constructor,length,scrollRestoration,state,back,forward,go,pushState,replaceState",
    "ImageData":               "constructor,width,height,data,colorSpace",
    "IntersectionObserver":    "constructor,root,rootMargin,scrollMargin,thresholds,observe,unobserve,disconnect,takeRecords",
    "MediaQueryList":          "constructor,media,matches,onchange,addListener,removeListener",
    "MessageChannel":          "constructor,port1,port2",
    "MessagePort":             "constructor,onmessage,onmessageerror,postMessage,start,close",
    "MutationObserver":        "constructor,observe,disconnect,takeRecords",
    "NamedNodeMap":            "constructor,length,item,getNamedItem,getNamedItemNS,setNamedItem,setNamedItemNS,removeNamedItem,removeNamedItemNS",
    "NodeList":                "constructor,length,item,entries,forEach,keys,values",
    "OfflineAudioContext":     "constructor,length,oncomplete,startRendering,resume,suspend",
    "Performance":             "constructor,timeOrigin,eventCounts,interactionCount,navigation,timing,onresourcetimingbufferfull,now,toJSON,getEntries,getEntriesByType,getEntriesByName,clearResourceTimings,setResourceTimingBufferSize,mark,clearMarks,measure,clearMeasures",
    "PerformanceObserver":     "constructor,observe,disconnect,takeRecords",
    "PerformanceTiming":       "constructor,navigationStart,unloadEventStart,unloadEventEnd,redirectStart,redirectEnd,fetchStart,domainLookupStart,domainLookupEnd,connectStart,connectEnd,secureConnectionStart,requestStart,responseStart,responseEnd,domLoading,domInteractive,domContentLoadedEventStart,domContentLoadedEventEnd,domComplete,loadEventStart,loadEventEnd,toJSON",
    "Permissions":             "constructor,query",
    "PointerEvent":            "constructor,pointerId,width,height,pressure,tangentialPressure,tiltX,tiltY,twist,altitudeAngle,azimuthAngle,pointerType,isPrimary,getCoalescedEvents,getPredictedEvents",
    "RTCPeerConnection":       "constructor,localDescription,currentLocalDescription,pendingLocalDescription,remoteDescription,currentRemoteDescription,pendingRemoteDescription,signalingState,iceGatheringState,iceConnectionState,connectionState,canTrickleIceCandidates,onnegotiationneeded,onicecandidate,onsignalingstatechange,oniceconnectionstatechange,onicegatheringstatechange,onconnectionstatechange,onicecandidateerror,ontrack,sctp,ondatachannel,createOffer,createAnswer,setLocalDescription,setRemoteDescription,addIceCandidate,restartIce,getConfiguration,setConfiguration,close,getSenders,getReceivers,getTransceivers,addTrack,removeTrack,addTransceiver,createDataChannel,getStats",
    "Request":                 "constructor,method,url,headers,destination,referrer,referrerPolicy,mode,credentials,cache,redirect,integrity,keepalive,duplex,signal,body,bodyUsed,clone,arrayBuffer,blob,bytes,formData,json,text",
    "ResizeObserver":          "constructor,observe,unobserve,disconnect",
    "Response":                "constructor,type,url,redirected,status,ok,statusText,headers,body,bodyUsed,clone,arrayBuffer,blob,bytes,formData,json,text",
    "Screen":                  "constructor,height,width,colorDepth,pixelDepth,availLeft,availTop,availHeight,availWidth,orientation",
    "ServiceWorkerContainer":  "constructor,controller,ready,oncontrollerchange,onmessage,onmessageerror,register,getRegistration,getRegistrations,startMessages",
    "SharedWorker":            "constructor,port,onerror",
    "SpeechSynthesis":         "constructor,pending,speaking,paused,onvoiceschanged,speak,cancel,pause,resume,getVoices",
    "SpeechSynthesisUtterance": "constructor,text,lang,voice,volume,rate,pitch,onstart,onend,onerror,onpause,onresume,onmark,onboundary",
    "Storage":                 "constructor,length,key,getItem,setItem,removeItem,clear",
    "StorageManager":          "constructor,persisted,persist,estimate,getDirectory",
    "SubtleCrypto":            "constructor,encrypt,decrypt,sign,verify,digest,generateKey,deriveKey,deriveBits,importKey,exportKey,wrapKey,unwrapKey",
    "TextDecoder":             "constructor,encoding,fatal,ignoreBOM,decode",
    "TextEncoder":             "constructor,encoding,encode,encodeInto",
    "UIEvent":                 "constructor,view,detail,layerX,layerY,pageX,pageY,which,initUIEvent",
    "URL":                     "constructor,href,origin,protocol,username,password,host,hostname,port,pathname,hash,search,searchParams,toJSON,toString",
    "URLSearchParams":         "constructor,size,append,delete,get,getAll,has,set,sort,entries,keys,values,forEach,toString",
    "VisualViewport":          "constructor,offsetLeft,offsetTop,pageLeft,pageTop,width,height,scale,onresize,onscroll",
    "Worker":                  "constructor,onmessage,onmessageerror,onerror,terminate,postMessage",
}

# iPad 的 Element 面比 iPhone 多一整块全屏族（BCD：safari_ios 16.4 注
# 「Only available on iPad, not on iPhone」；桌面 WebKit 27.2 实测 149 名，
# 上表 141 名恰为其去掉这 7 名，/tmp/f610）。顺序按桌面 WebKit 实测插入位：
# 4 个处理属性在 currentCSSZoom/outerHTML 之后、innerHTML 之前；3 个方法在
# insertAdjacentHTML 之后、setPointerCapture 之前。
#
# 为什么必须补进顺序表：__protoOrder 的实现是「删光可配置成员 → 按目标序
# 重新 defineProperty」，顺序表因此同时是**成员集合**的白名单——iPad 上
# 全屏族被删掉后没有回到表里，Element.prototype 上就永久缺失（真机 iPad
# 有、仿真没有，一行 `'requestFullscreen' in document.documentElement` 判假）。
_IOS_PROTO_ORDER_IPAD: Dict[str, str] = {
    **_IOS_PROTO_ORDER,
    "Element": _IOS_PROTO_ORDER["Element"]
        .replace("currentCSSZoom,outerHTML,innerHTML",
                 "currentCSSZoom,outerHTML,"
                 "onfullscreenchange,onfullscreenerror,"
                 "onwebkitfullscreenchange,onwebkitfullscreenerror,innerHTML")
        .replace("insertAdjacentHTML,setPointerCapture",
                 "insertAdjacentHTML,requestFullscreen,webkitRequestFullScreen,"
                 "webkitRequestFullscreen,setPointerCapture"),
}


def _drop_proto_members(target_os: str,
                        os_version: Any = None) -> Dict[str, List[str]]:
    """目标平台需要从接口原型上删除的成员（F-26/F-27）。

    F-40：getDisplayMedia 是唯一一项**两端都要删**的成员——BCD
    `chrome_android=false`（72–88 曾暴露但恒 NotAllowedError，视为无）、
    `safari_ios=27`（iOS 27 起才有），而设备库 Android 机型全量、iOS 机型
    绝大多数是 26.x。iOS 27 的身份由调用方按版本回填（derive_spec 里
    直接 pop 掉 MediaDevices 项）。
    """
    if target_os != "ios":
        # F-103 / F-109：这两族在 chrome_android 上同样是 false——桌面
        # Chromium 原型被原样带进 Android 仿真。grammars 是 Chrome
        # Android 33 起的真机面，保留。
        # F-121：setSinkId/sinkId 是桌面 Chrome 49 起的音频输出选择面，
        # BCD chrome_android=false——真机 Chrome Android 不暴露。
        return {
            "MediaDevices": ["getDisplayMedia", "setCaptureHandleConfig"],
            "MediaStreamTrack": ["getCaptureHandle", "oncapturehandlechange"],
            "webkitSpeechRecognition": ["phrases", "processLocally"],
            "HTMLMediaElement": ["setSinkId", "sinkId"],
        }
    table = {k: list(v) for k, v in _IOS_DROP_PROTO_MEMBERS.items()}
    md = table.setdefault("MediaDevices", [])
    if "getDisplayMedia" not in md:
        md.append("getDisplayMedia")
    # F-122：BCD safari_ios=18.4 才有这两名，静态表不能承载版本门控
    # （iOS 18.4+ 必须保留）。18.4 以下按档案动态追加。
    version = _parse_os_version(os_version)
    if version is not None and version < (18, 4):
        hm = table.setdefault("HTMLMediaElement", [])
        hm.extend(n for n in ("setSinkId", "sinkId") if n not in hm)
    return table


# --------------------------------------------------------------------------
# 补成员清单（F-25 / F-36）：真机 WebKit 有、Chromium 内核没有的面
# --------------------------------------------------------------------------
# 取值口径全部来自 macOS WebKit 27 实测（`/tmp/wkprobe/f25v.py`），与 iPhone
# Safari 共用同一 WebKit 源码；Chromium 侧缺口用同一探针在 fp-chromium 上
# 复核（`/tmp/wkprobe/f25ch.py`）。只登记「仿真缺、真机有」的项，方向存疑的
# 一律不登记（GestureEvent.rotation、DeviceOrientationEvent.webkitCompass*、
# SpeechRecognitionAlternative/Result 在 macOS WebKit 亦缺，故不补）。
#
# protos 的 kind：fn = 方法（固定返回值），get = 只读属性，rw = 可写属性，
# elementRef = ARIA 元素反射（内部元素列表 + 属性文本两条来源，见下方 F-141 项），
# emHeight = 按 ctx.font / textBaseline 逐实例计算的 TextMetrics 字体度量，
# fontStretch = CSS `font-stretch` 语义的可写属性（值按 CSS 语法校验后归一化）。
# interfaces 的 members：'!m' 前缀为方法，其余为属性 getter。
_IOS_ADD_SPEC: Dict[str, Any] = {
    # 复杂语义的成员（Canvas 状态改写、媒体轨道对象、事件访问器）在注入器里
    # 逐条实现，这里只放「固定返回值」的简单成员。
    "protos": [
        {"ctor": "Range", "name": "compareNode", "kind": "fn"},
        # F-92：TextMetrics 的 emHeight 两名不是固定值——真机按 ctx.font 解析出
        # 的字体度量与字号逐实例算出（**未归一化**，ascent+descent ≈ 1.13em；
        # WebKit main 分支的 1em 归一化没进 27.0.1 发布版）。取值与 _WK_EM_RATIO
        # 表由注入器实现，这里只登记「需要按实例计算」的标记。
        {"ctor": "TextMetrics", "name": "emHeightAscent", "kind": "emHeight"},
        {"ctor": "TextMetrics", "name": "emHeightDescent", "kind": "emHeight"},
        # FontFace.width 是**可写**属性（WebKit 实测 get+set 齐全，初值
        # "normal"）；赋非法串抛 DOMException SyntaxError，赋合法值后归一化
        # 回写（"1e2%" → "100%"）。Chromium 侧完全没有该成员，赋值只落成
        # 普通自有属性，必须整条补上。
        {"ctor": "FontFace", "name": "width", "kind": "fontStretch",
         "value": "normal"},
        {"ctor": "CSSStyleDeclaration", "name": "cssRules", "kind": "get",
         "value": None},
        {"ctor": "CSSStyleDeclaration", "name": "getPropertyCSSValue",
         "kind": "fn"},
        {"ctor": "MediaStreamTrack", "name": "onconfigurationchange",
         "kind": "rw", "value": None},
        {"ctor": "Worker", "name": "onmessageerror", "kind": "rw", "value": None},
        # 真机 VideoPlaybackQuality 原型比 Chromium 多 totalFrameDelay
        # （实测真机初值 0）。
        {"ctor": "VideoPlaybackQuality", "name": "totalFrameDelay",
         "kind": "get", "value": 0},
        # F-14 / F-77 D3：真机 Safari 的 Navigator.prototype.globalPrivacyControl
        # 是只读 getter（enumerable true、get.length 0、值 false）。WebKit 的
        # NavigatorGlobalPrivacyControl.idl 用 [EnabledBySetting=…] 门控、且
        # UnifiedWebPreferences.yaml 把默认值交给 embedder——Playwright WebKit
        # 因此看不到它，而 Safari 打开该设置。Blink 侧完全没有这个成员。
        {"ctor": "Navigator", "name": "globalPrivacyControl", "kind": "get",
         "value": False},
        # F-84：真机 WebKit 的 AudioSession 是 Singleton 双暴露面——window 上是
        # 接口对象，navigator.audioSession 是同一实例（实测 /tmp/f77p/audio.py：
        # 原型只读 getter、enumerable true、零自有属性、type 初值 "auto"、
        # `navigator.audioSession instanceof AudioSession` 为真、赋值被静默忽略）。
        {"ctor": "Navigator", "name": "audioSession", "singleton": "AudioSession",
         "init": {"type": "auto"}},
        # F-107：真机 Safari 18.4 起 MediaSession 有 setScreenshareActive
        # （WebKit 26.5 实测：原型自有、enumerable/writable、length 1、返回
        # undefined）；Chromium 内核没有，且 BCD chrome_android=false，只补 iOS。
        {"ctor": "MediaSession", "name": "setScreenshareActive", "kind": "fn",
         "value": None},
        # F-141 / F-159：ARIA 元素反射（`FrozenArray<Element>?` /
        # `Element?`）。WebKit 的模型是「内部元素列表 + 属性文本」两条来源：
        #   * Element 侧（`Element::elementsArrayForAttributeInternal`）：setter
        #     只把内部列表记进 explicitlySetAttrElementsMap、并把属性写成空串；
        #     任何**值变化**的属性写入都会清掉内部列表（`attributeChanged`
        #     的 default 分支，而它第一行是 oldValue == newValue 就返回），
        #     清掉后退回按属性文本里的 id 解析。
        #   * ElementInternals 侧（`CustomElementDefaultARIA`）：只有内部列表，
        #     属性文本完全不参与，且可见性多两条（目标断连恒可见、目标在文档
        #     树里恒可见）。
        # Blink 同名成员在语义上基本一致，但接收者校验文案（Illegal
        # invocation）、序列转换文案、shadow 可见性口径都不同，故整条强制
        # 替换（force）。实测 /tmp/f493、/tmp/f494、/tmp/f496、/tmp/f499。
        {"ctor": "Element", "name": "ariaControlsElements", "kind": "elementRef",
         "force": True, "iface": "Element", "attr": "aria-controls"},
        {"ctor": "Element", "name": "ariaDescribedByElements", "kind": "elementRef",
         "force": True, "iface": "Element", "attr": "aria-describedby"},
        {"ctor": "Element", "name": "ariaDetailsElements", "kind": "elementRef",
         "force": True, "iface": "Element", "attr": "aria-details"},
        {"ctor": "Element", "name": "ariaErrorMessageElements", "kind": "elementRef",
         "force": True, "iface": "Element", "attr": "aria-errormessage"},
        {"ctor": "Element", "name": "ariaFlowToElements", "kind": "elementRef",
         "force": True, "iface": "Element", "attr": "aria-flowto"},
        {"ctor": "Element", "name": "ariaLabelledByElements", "kind": "elementRef",
         "force": True, "iface": "Element", "attr": "aria-labelledby"},
        {"ctor": "Element", "name": "ariaOwnsElements", "kind": "elementRef",
         "force": True, "iface": "Element", "attr": "aria-owns"},
        {"ctor": "Element", "name": "ariaActiveDescendantElement",
         "kind": "elementRef", "force": True, "iface": "Element",
         "single": True, "attr": "aria-activedescendant"},
        # ElementInternals 的成员没有对应属性面（attr 省略 = null）。
        {"ctor": "ElementInternals", "name": "ariaControlsElements",
         "kind": "elementRef", "force": True, "iface": "ElementInternals"},
        {"ctor": "ElementInternals", "name": "ariaDescribedByElements",
         "kind": "elementRef", "force": True, "iface": "ElementInternals"},
        {"ctor": "ElementInternals", "name": "ariaDetailsElements",
         "kind": "elementRef", "force": True, "iface": "ElementInternals"},
        {"ctor": "ElementInternals", "name": "ariaErrorMessageElements",
         "kind": "elementRef", "force": True, "iface": "ElementInternals"},
        {"ctor": "ElementInternals", "name": "ariaFlowToElements",
         "kind": "elementRef", "force": True, "iface": "ElementInternals"},
        {"ctor": "ElementInternals", "name": "ariaLabelledByElements",
         "kind": "elementRef", "force": True, "iface": "ElementInternals"},
        # Blink 的 ElementInternals 完全没有这一名（实测 /tmp/f499）。
        {"ctor": "ElementInternals", "name": "ariaOwnsElements",
         "kind": "elementRef", "iface": "ElementInternals"},
        {"ctor": "ElementInternals", "name": "ariaActiveDescendantElement",
         "kind": "elementRef", "force": True, "iface": "ElementInternals",
         "single": True},
    ],
    "statics": [
        {"ctor": "DataView", "name": "BYTES_PER_ELEMENT", "value": 1},
        {"ctor": "RegExp", "name": "multiline", "value": False},
    ],
    "constants": [
        {"ctor": "MouseEvent", "name": "WEBKIT_FORCE_AT_MOUSE_DOWN", "value": 1},
        {"ctor": "MouseEvent", "name": "WEBKIT_FORCE_AT_FORCE_MOUSE_DOWN",
         "value": 2},
        {"ctor": "Range", "name": "NODE_BEFORE", "value": 0},
        {"ctor": "Range", "name": "NODE_AFTER", "value": 1},
        {"ctor": "Range", "name": "NODE_BEFORE_AND_AFTER", "value": 2},
        {"ctor": "Range", "name": "NODE_INSIDE", "value": 3},
        # CSS 遗留值常量：这些接口在 Chromium 里整体不存在，原型上的常量
        # 也要补（真机实测：构造函数与原型两侧同值）。
        {"ctor": "CSSValue", "name": "CSS_INHERIT", "value": 0},
        {"ctor": "CSSValue", "name": "CSS_PRIMITIVE_VALUE", "value": 1},
        {"ctor": "CSSValue", "name": "CSS_VALUE_LIST", "value": 2},
        {"ctor": "CSSValue", "name": "CSS_CUSTOM", "value": 3},
        {"ctor": "SVGViewSpec", "name": "SVG_ZOOMANDPAN_UNKNOWN", "value": 0},
        {"ctor": "SVGViewSpec", "name": "SVG_ZOOMANDPAN_DISABLE", "value": 1},
        {"ctor": "SVGViewSpec", "name": "SVG_ZOOMANDPAN_MAGNIFY", "value": 2},
        # F-155 残项：WebKit 的 MediaError 多一个 MEDIA_ERR_ENCRYPTED=5
        # （/tmp/f404 实测：构造函数与原型两侧都有、可枚举、不可写）。
        {"ctor": "MediaError", "name": "MEDIA_ERR_ENCRYPTED", "value": 5},
    ] + (
        # CSSPrimitiveValue / SVGPathSeg 的枚举族：真机实测为连续 0..N，
        # 顺序即 WebKit CSSPrimitiveValue.idl / SVGPathSeg.idl 的声明顺序。
        [{"ctor": "CSSPrimitiveValue", "name": n, "value": i}
         for i, n in enumerate([
             "CSS_UNKNOWN", "CSS_NUMBER", "CSS_PERCENTAGE", "CSS_EMS",
             "CSS_EXS", "CSS_PX", "CSS_CM", "CSS_MM", "CSS_IN", "CSS_PT",
             "CSS_PC", "CSS_DEG", "CSS_RAD", "CSS_GRAD", "CSS_MS", "CSS_S",
             "CSS_HZ", "CSS_KHZ", "CSS_DIMENSION", "CSS_STRING", "CSS_URI",
             "CSS_IDENT", "CSS_ATTR", "CSS_COUNTER", "CSS_RECT",
             "CSS_RGBCOLOR"])]
        + [{"ctor": "SVGPathSeg", "name": n, "value": i}
           for i, n in enumerate([
               "PATHSEG_UNKNOWN", "PATHSEG_CLOSEPATH", "PATHSEG_MOVETO_ABS",
               "PATHSEG_MOVETO_REL", "PATHSEG_LINETO_ABS",
               "PATHSEG_LINETO_REL", "PATHSEG_CURVETO_CUBIC_ABS",
               "PATHSEG_CURVETO_CUBIC_REL", "PATHSEG_CURVETO_QUADRATIC_ABS",
               "PATHSEG_CURVETO_QUADRATIC_REL", "PATHSEG_ARC_ABS",
               "PATHSEG_ARC_REL", "PATHSEG_LINETO_HORIZONTAL_ABS",
               "PATHSEG_LINETO_HORIZONTAL_REL", "PATHSEG_LINETO_VERTICAL_ABS",
               "PATHSEG_LINETO_VERTICAL_REL",
               "PATHSEG_CURVETO_CUBIC_SMOOTH_ABS",
               "PATHSEG_CURVETO_CUBIC_SMOOTH_REL",
               "PATHSEG_CURVETO_QUADRATIC_SMOOTH_ABS",
               "PATHSEG_CURVETO_QUADRATIC_SMOOTH_REL"])]
    ),
    # 事件处理属性。真机实测（`/tmp/f77p/hosts.py`，WebKit 27.2 / has_touch）：
    # copy/cut/paste + webkitmouseforce* + ontouchforcechange + ontouch* 在
    # Window / Document / HTMLElement / SVGElement 四处都有；HTMLBodyElement
    # 继承这些成员（不单独补）。
    # 可枚举性：copy/cut/paste 为 enumerable:true，其余九个（Force Touch 五个 +
    # ontouch 四个）为 enumerable:false —— 写成 `enum: False` 的行走后者，
    # 其余沿用默认 true。ontouch* 在内核里由触控仿真原生安装且是可枚举的，
    # 注入器对「已存在但可枚举性不符」的自有访问器只重定义 enumerable。
    "handlers": (
        [{"ctor": c, "name": n, "enum": n in ("oncopy", "oncut", "onpaste")}
         for c in ("window", "Document", "HTMLElement", "SVGElement")
         for n in ("oncopy", "oncut", "onpaste",
                   "onwebkitmouseforcechanged", "onwebkitmouseforcedown",
                   "onwebkitmouseforceup", "onwebkitmouseforcewillbegin",
                   "ontouchforcechange",
                   "ontouchstart", "ontouchmove", "ontouchend", "ontouchcancel")]
        # F-115 / F-117：WebKit 把 onbeforecopy/cut/paste 放在 HTMLElement、
        # onfocusin/onfocusout 放在 Element（WebKit Element.idl 对后者是
        # [NotEnumerable]），Blink 的落点不同或整体缺失。WebKit 26.5 实测这
        # 五名均为 enumerable:false 的访问器。
        # 落点只写 WebKit 真实宿主：Element 是 HTMLElement 的父类，补在父类
        # 会让 document.documentElement 也命中，反向制造判据。
        + [{"ctor": "HTMLElement", "name": n, "enum": False}
           for n in ("onbeforecopy", "onbeforecut", "onbeforepaste")]
        + [{"ctor": "Element", "name": n, "enum": False}
           for n in ("onfocusin", "onfocusout")]
        # F-118：WebKit 的 Element.idl 里另有 5 名非标准处理器，全部
        # [NotEnumerable]（WebKit 26.5 实测 enumerable:false）。HTMLBodyElement
        # / SVGElement / MathMLElement 按继承得到，不重复补。
        # 条件项（onwebkitneedkey 依赖 LegacyEncryptedMediaAPIEnabled、
        # presentationmode 依赖 VIDEO_PRESENTATION_MODE、AirPlay 两项依赖
        # WIRELESS_PLAYBACK_TARGET）在 WebKit 26.5 运行期全部暴露。
        + [{"ctor": "Element", "name": n, "enum": False}
           for n in ("onbeforeload", "onwebkitneedkey",
                     "onwebkitpresentationmodechanged",
                     "onwebkitcurrentplaybacktargetiswirelesschanged",
                     "onwebkitplaybacktargetavailabilitychanged")]
    ),
    # F-29 / F-30：表单控件默认样式。真机 iOS WebKit 的 UA 样式表把控件
    # 字体定为 system-ui（macOS 宿主 Chromium 是 Arial），控件高度
    # button 18 / input 21 / select 20 / textarea 32（实测
    # /tmp/wkprobe/forms.py）。只作用于作者未显式设过的控件。
    # F-29 / F-30：表单控件默认样式（真机 iOS WebKit 实测 /tmp/wkprobe/forms2.py）。
    # 整段放进 @layer：层内规则一律低于任何未分层作者规则，所以站点自己写的
    # `button{...}` 仍然覆盖，只有「作者没设过」的属性才落到这里的真机口径。
    # 不用 !important —— 那会连作者的普通规则一起压掉，反而暴露。
    # line-height:13px 是控件高度的真正来源：WebKit 的 button 高度 = 4 border
    # + 1 padding + 13 行高 = 18px。Chromium 默认行高按字体族解析（CJK 文本
    # 会到 16px 而撑成 21px），钉死行高后 button/textarea 与真机逐像素一致。
    "uaFormCss": (
        "@layer wkua{"
        "button,input,select,textarea{font-family:system-ui;font-size:11px;"
        "line-height:13px}"
        # F-46：Blink UA sheet 的两处 iOS 穿帮面。真机 WebKit 实测
        # （/tmp/f40wk/probe5.py）：input overflow=visible（Blink 是 clip）、
        # select font-family=system-ui（Blink 落到宿主 Arial）；textarea 的
        # monospace 由上面的 font-family 规则覆盖。
        "input{overflow:visible}"
        # F-47：真机 iOS Safari 正文默认不可选（长按不弹选择菜单），computed
        # `-webkit-user-select` 为 none；内核宿主是 auto。放 @layer 里，站点
        # 自己写的 user-select 仍然优先。表单控件显式回 text（真机控件内可选中）。
        "body{-webkit-user-select:none;user-select:none}"
        "input,textarea{-webkit-user-select:text;user-select:text}"
        "button,input[type=button],input[type=submit],input[type=reset]{"
        "padding:0 6px 1px;border:2px outset rgb(192,192,192);"
        "border-radius:0;background-color:rgb(192,192,192);"
        "color:rgba(0,0,0,0.847)}"
        "input:not([type=button]):not([type=submit]):not([type=reset])"
        ":not([type=checkbox]):not([type=radio]):not([type=range])"
        ":not([type=color]):not([type=file]){padding:2px 5px;"
        "border:2px inset rgb(128,128,128);border-radius:0;"
        "background-color:rgb(255,255,255);color:rgb(0,0,0)}"
        "select{border:1px solid rgba(0,0,0,0.847);border-radius:5px;"
        "background-color:rgb(255,255,255);color:rgba(0,0,0,0.847);height:20px}"
        "textarea{border:1px solid rgb(0,0,0);border-radius:0;"
        "background-color:rgb(255,255,255);color:rgb(0,0,0);padding:2px 5px}"
        "}"
    ),
    # 真机控件高度（实测），供验证脚本比对。
    "uaHeights": {"button": 18, "input": 21, "select": 20, "textarea": 32},
    # 全局项（WebKit 27.2 实测 /tmp/f77p/shape2.py、odb5.py）：
    #   webkitIndexedDB —— 只读 getter（无 setter）、enumerable true、
    #     SameObject（=== indexedDB）；
    #   openDatabase —— get/set 访问器、enumerable false；未赋值读作
    #     "undefined"（真机 getter 返回 JSC 的内部函数对象，typeof 亦为
    #     "undefined"；V8 无法制造该对象，退化为真 undefined）；
    #   safari —— 可写数据属性（w/e/c 全 true），值 {pushNotification}，
    #     其 pushNotification 自带 toString → "[object SafariRemoteNotification]"。
    "globals": {
        "webkitIndexedDB": {"alias": "indexedDB"},
        "openDatabase": {"accessor": "undefined"},
        "safari": {"object": {"pushNotification": {
            "toStringTag": "SafariRemoteNotification"}}},
    },
    # 接口对象：construct='ok' 表示真机可直接 new（其余抛 Illegal constructor），
    # length 为构造器参数个数；members 为原型成员（'!' 前缀 = 方法，'=' 前缀
    # = 可写属性，其余为只读 getter）；extends 为真机原型链父接口（实测
    # /tmp/wkprobe/f25chain.py）；init 为实例初值，argInit 为「构造参数 →
    # 成员」的位置映射（'' = 该位不映射）。
    "interfaces": {
        # CSSOM derived declaration interfaces (Safari 26+).  Chromium keeps
        # CSSStyleDeclaration as the public constructor and does not expose
        # these WebKit/CSSOM globals.  They are non-constructible in WebKit
        # and inherit the declaration members from CSSStyleDeclaration; the
        # injector supplies the same prototype chain and native-shaped
        # Illegal constructor semantics.
        "CSSStyleProperties": {"construct": "illegal", "length": 0,
                                "extends": "CSSStyleDeclaration",
                                "members": []},
        "CSSFontFaceDescriptors": {"construct": "illegal", "length": 0,
                                    "extends": "CSSStyleDeclaration",
                                    "members": []},
        "CSSPageDescriptors": {"construct": "illegal", "length": 0,
                                "extends": "CSSStyleDeclaration",
                                "members": []},
        "AudioSession": {"construct": "illegal", "length": 0,
                         "extends": "EventTarget",
                         "members": ["=type"]},
        "MediaController": {"construct": "ok", "length": 0,
                            "extends": "EventTarget",
                            # 真机 new MediaController() 的初值（实测）。
                            "init": {"buffered": "@TimeRanges",
                                     "seekable": "@TimeRanges",
                                     "played": "@TimeRanges",
                                     "duration": 0, "currentTime": 0,
                                     "paused": False,
                                     "playbackState": "waiting",
                                     "defaultPlaybackRate": 1,
                                     "playbackRate": 1, "volume": 1,
                                     "muted": False},
                            "members": ["buffered", "seekable", "duration",
                                        "=currentTime", "paused", "played",
                                        "playbackState", "=defaultPlaybackRate",
                                        "=playbackRate", "=volume", "=muted",
                                        "!play", "!pause", "!unpause"]},
        "XMLHttpRequestProgressEvent": {
            "construct": "illegal", "length": 0,
            "extends": "ProgressEvent",
            "members": ["position", "totalSize"]},
        "SVGViewSpec": {"construct": "illegal", "length": 0,
                        "members": ["transform", "viewBoxString",
                                    "preserveAspectRatioString",
                                    "transformString", "viewBox",
                                    "preserveAspectRatio", "=zoomAndPan"]},
        "CSSValue": {"construct": "illegal", "length": 0,
                     "members": ["=cssText", "cssValueType"]},
        "CSSPrimitiveValue": {
            "construct": "illegal", "length": 0, "extends": "CSSValue",
            "members": ["primitiveType", "!setFloatValue", "!getFloatValue",
                        "!setStringValue", "!getStringValue", "!getCounterValue",
                        "!getRectValue", "!getRGBColorValue"]},
        "CSSValueList": {"construct": "illegal", "length": 0,
                         "extends": "CSSValue",
                         "members": ["length", "!item"]},
        "RGBColor": {"construct": "illegal", "length": 0,
                     "members": ["red", "green", "blue", "alpha"]},
        "Rect": {"construct": "illegal", "length": 0,
                 "members": ["top", "right", "bottom", "left"]},
        "Counter": {"construct": "illegal", "length": 0,
                    "members": ["identifier", "listStyle", "separator"]},
        "SVGPathSeg": {"construct": "illegal", "length": 0,
                       "members": ["pathSegType", "pathSegTypeAsLetter"]},
        "MathMLMathElement": {"construct": "illegal", "length": 0,
                              "extends": "MathMLElement", "members": []},
        # 真机 type 取第 4 个参数（实测 new DataCue(5, 9, 'V', 'text/plain')
        # → value 'V'、type 'text/plain'；缺省为 ''）。
        "DataCue": {"construct": "args", "length": 3,
                    "extends": "TextTrackCue",
                    "init": {"value": "", "type": "", "data": None},
                    "argInit": ["", "", "value", "type"],
                    "members": ["=value", "type", "=data"]},
        "VTTRegion": {"construct": "ok", "length": 0,
                      # 真机 new VTTRegion() 的初值（实测）。
                      "init": {"id": "", "width": 100, "lines": 3,
                               "regionAnchorX": 0, "regionAnchorY": 100,
                               "viewportAnchorX": 0, "viewportAnchorY": 100,
                               "scroll": ""},
                      "members": ["=id", "=width", "=lines", "=regionAnchorX",
                                  "=regionAnchorY", "=viewportAnchorX",
                                  "=viewportAnchorY", "=scroll"]},
        "ManagedMediaSource": {"construct": "ok", "length": 0,
                               "extends": "MediaSource",
                               # 实测：quality 'high'、streaming false。
                               # 构造参数 MediaSourceInit 在 WebKit 里被完全忽略
                               # （new ManagedMediaSource({quality:'low'}) 仍读
                               # 'high'），故 init 只作成员默认值，不进 seed。
                               "init": {"quality": "high", "streaming": False},
                               # F-152：实例必须建在真实 MediaSource 上，
                               # 否则继承来的 readyState/duration/sourceBuffers
                               # 全是 Illegal invocation，URL.createObjectURL
                               # 也会因过不了 Blink 内部类型检查而失败。
                               "realBase": True,
                               # F-152：字典参数遇到非对象 → TypeError('Type error')。
                               "dictArg": True,
                               "members": ["quality", "=onqualitychange",
                                           "streaming", "=onstartstreaming",
                                           "=onendstreaming"],
                               # F-130：WebKit 里 ManagedMediaSource 继承
                               # MediaSource，静态 isTypeSupported 必须存在
                               # （主世界表此前漏了这一项，Worker 表有）。
                               "statics": ["!isTypeSupported"]},
        # F-157 残项：Blink 的原生构造器文案带 `Failed to construct 'X': …`
        # 前缀，WebKit 是裸文案。两者原型面已完全对齐（/tmp/f512：成员
        # 顺序、描述符、toString、原型链、length 全同），只需 repair 构造
        # 语义，故 members 留空。
        #   SourceBuffer / SourceBufferList：new 与不带 new 都是
        #     `Illegal constructor`；
        #   MediaSource：new 正常，不带 new 是
        #     `Constructor requires 'new' operator`（construct='args' 且
        #     length=0，缺参判定因此永远不触发）。
        "SourceBuffer": {"construct": "illegal", "length": 0, "repair": True},
        "SourceBufferList": {"construct": "illegal", "length": 0,
                             "repair": True},
        "MediaSource": {"construct": "args", "length": 0, "repair": True},
        "ManagedSourceBuffer": {"construct": "illegal", "length": 0,
                                "extends": "SourceBuffer",
                                "members": ["=onbufferedchange"]},
        "BufferedChangeEvent": {"construct": "args", "length": 2,
                                "extends": "Event",
                                "members": ["addedRanges", "removedRanges"]},
        "VideoTrack": {"construct": "illegal", "length": 0,
                       "members": ["id", "kind", "label", "language",
                                   "=selected", "configuration", "sourceBuffer"]},
        "AudioTrack": {"construct": "illegal", "length": 0,
                       "members": ["id", "kind", "label", "language",
                                   "=enabled", "configuration", "sourceBuffer"]},
        "VideoTrackList": {"construct": "illegal", "length": 0,
                           "extends": "EventTarget",
                           "members": ["length", "selectedIndex", "=onchange",
                                       "=onaddtrack", "=onremovetrack", "!item",
                                       "!getTrackById"]},
        "AudioTrackList": {"construct": "illegal", "length": 0,
                           "extends": "EventTarget",
                           "members": ["length", "=onchange", "=onaddtrack",
                                       "=onremovetrack", "!item",
                                       "!getTrackById"]},
        # F-154：Blink 的 TextTrackList / SourceBufferList 缺 item()，
        # 且成员的接收者校验走内部槽（真机 Object.create(原型) 不算实例）。
        # 这里只补缺的成员，既有成员由页面脚本的 __wkListSetup 统一接管。
        "TextTrackList": {"construct": "illegal", "length": 0,
                          "repair": True, "extends": "EventTarget",
                          "members": ["length", "=onaddtrack", "=onchange",
                                      "=onremovetrack", "!item",
                                      "!getTrackById"]},
        "SourceBufferList": {"construct": "illegal", "length": 0,
                             "repair": True, "extends": "EventTarget",
                             "members": ["length", "=onaddsourcebuffer",
                                         "=onremovesourcebuffer", "!item"]},
        # F-155 残项：真机 MediaError 比 Blink 多 MEDIA_ERR_ENCRYPTED（=5），
        # 构造文案是裸 Illegal constructor，接收者校验也走真机文案
        # （/tmp/f404）。常量走 constants 表；原型成员顺序因 Blink 的
        # 常量不可配置而无法重排，属既有 own-name 顺序残差。
        "MediaError": {"construct": "illegal", "length": 0,
                       "repair": True,
                       "members": ["code", "message"]},
        "AudioTrackConfiguration": {
            "construct": "illegal", "length": 0,
            "members": ["codec", "sampleRate", "numberOfChannels", "bitrate"]},
        "MediaRecorderErrorEvent": {"construct": "args", "length": 2,
                                    "extends": "Event",
                                    "members": ["error"]},
        "OverconstrainedErrorEvent": {"construct": "args", "length": 1,
                                      "extends": "Event",
                                      "members": ["error"]},
        "FileSystemEntry": {"construct": "illegal", "length": 0,
                            "members": ["isFile", "isDirectory", "name",
                                        "fullPath", "filesystem", "!getParent"]},
        "FileSystemDirectoryEntry": {
            "construct": "illegal", "length": 0,
            "extends": "FileSystemEntry",
            "members": ["!createReader", "!getFile", "!getDirectory"]},
        "FileSystemFileEntry": {"construct": "illegal", "length": 0,
                                "extends": "FileSystemEntry",
                                "members": ["!file"]},
        "FileSystemDirectoryReader": {"construct": "illegal", "length": 0,
                                      "members": ["!readEntries"]},
        "Report": {"construct": "illegal", "length": 0,
                   "members": ["type", "url", "body", "!toJSON"]},
        # 混合体：不带 new 是 args 文案，带 new 是裸 Illegal constructor
        # （实测 /tmp/wkprobe/f25nonew.py）。
        # repair=True：内核 152 已原生暴露 window.FontFaceSet（内核 148 没有，
        # 旧注释「裸 fp-chromium 没有这个全局」已过时），其构造语义是 Blink 的
        # `Failed to construct 'FontFaceSet': Illegal constructor`、length 0。
        # 不加 repair 会被 __addIfaceObj 的 `existing && !repair` 直接跳过，
        # 两处 Blink 文案原样漏给检测站（verify iOS「接口对象形态」即在此失败）。
        # 真机 WebKit 26.6 实测：length 1、new → Illegal constructor、
        # 无 new → Constructor requires 'new' operator（/tmp/fb04/wkffs.py）。
        "FontFaceSet": {"construct": "illegal", "length": 1,
                        "noNew": "args", "repair": True,
                        "extends": "EventTarget",
                        "members": ["size", "=onloading", "=onloadingdone",
                                    "=onloadingerror", "ready", "status",
                                    "!has", "!add", "!delete", "!clear",
                                    "!load", "!check", "!entries", "!keys",
                                    "!values", "!forEach"]},
        # repair=True：Chromium 已有同名接口，但构造语义是 Blink 文案
        # （`Failed to construct 'X': …`），需要改成 WebKit 的裸文案；
        # 且缺 entries/keys/values/forEach 四个迭代方法。
        "StylePropertyMapReadOnly": {
            "construct": "illegal", "length": 0, "repair": True,
            "members": ["size", "!get", "!getAll", "!has", "!entries",
                        "!keys", "!values", "!forEach"]},
        "CSSMathValue": {"construct": "illegal", "length": 0, "repair": True,
                         "extends": "CSSNumericValue",
                         "members": ["operator"]},
        "CSSNumericValue": {"construct": "illegal", "length": 0, "repair": True,
                            "extends": "CSSStyleValue",
                            "members": ["!add", "!sub", "!mul", "!div", "!min",
                                        "!max", "!equals", "!to", "!toSum",
                                        "!type"]},
        "CSSUnitValue": {"construct": "args", "length": 2, "repair": True,
                         "extends": "CSSNumericValue",
                         "members": ["=value", "unit"]},
        "CSSKeywordValue": {"construct": "args", "length": 1, "repair": True,
                            "extends": "CSSStyleValue",
                            "members": ["=value"]},
        "CSSImageValue": {"construct": "illegal", "length": 0, "repair": True,
                          "extends": "CSSStyleValue",
                          "members": []},
        "CSSTransformComponent": {"construct": "illegal", "length": 0,
                                  "repair": True,
                                  "members": ["=is2D", "!toMatrix",
                                              "!toString"]},
        "CSSStyleValue": {"construct": "illegal", "length": 0, "repair": True,
                          "members": ["!toString"]},
        "SpeechRecognitionResultList": {
            "construct": "illegal", "length": 0,
            "members": ["length", "!item"]},
        # F-77 D3：WebKit 27.2 实测（/tmp/f77p/shape2.py）三者的形态——
        # 均为 Illegal constructor、length 0、原型直挂 Object.prototype。
        # SpeechRecognitionResult.item 是 writable 数据属性（length 1），
        # 其余成员是只读 getter（enumerable true）。
        "SpeechRecognitionResult": {
            "construct": "illegal", "length": 0,
            "members": ["length", "isFinal", "!item"]},
        "SpeechRecognitionAlternative": {
            "construct": "illegal", "length": 0,
            "members": ["transcript", "confidence"]},
        # Apple Pay JS：ApplePayError 可 new（length 1，缺参抛 Not enough
        # arguments），四个成员均为 get/set（enumerable true）。真机还校验
        # code / contactField / domain 的枚举值，补丁只查参数个数——检测站
        # 极少构造 ApplePayError，该差异可接受（清单 §28 D3 备忘）。
        # 1 参构造的初值实测 {contactField:null, message:"", domain:null}。
        "ApplePayError": {
            "construct": "args", "length": 1,
            "init": {"contactField": None, "message": "", "domain": None},
            "argInit": ["code", "contactField", "message", "domain"],
            "members": ["=code", "=contactField", "=message", "=domain"]},
        # 旧 WebKit 文件系统入口（与 FileSystemEntry 族的 File and Directory
        # Entries 是不同 API）：Illegal constructor，原型 name / root 只读。
        "FileSystem": {"construct": "illegal", "length": 0,
                       "members": ["name", "root"]},
        # 真机 TextTrack 比 Chromium 多 inBandMetadataTrackDispatchType 与
        # sourceBuffer 两个属性。
        "TextTrack": {"construct": "illegal", "length": 0, "repair": True,
                      "extends": "EventTarget",
                      "members": ["kind", "label", "language", "id",
                                  "inBandMetadataTrackDispatchType", "=mode",
                                  "cues", "activeCues", "=oncuechange",
                                  "sourceBuffer", "!addCue", "!removeCue"]},
        # 真机是可 new 的（length 3，缺参抛 Not enough arguments），
        # Chromium 里 TextTrackCue 是 Illegal constructor 且 length 0。
        "TextTrackCue": {"construct": "args", "length": 3, "repair": True,
                         "extends": "EventTarget",
                         "members": ["track", "=id", "=startTime", "=endTime",
                                     "=pauseOnExit", "=onenter", "=onexit",
                                     "!getCueAsHTML"]},
    },
}


def _worker_add_members(target_os: str) -> Dict[str, Any]:
    """Worker 作用域的补成员清单（F-37 / F-38）；非 iOS 返回空表。

    Worker 是独立 JS 全局，页面的 addMembers 到不了（见
    mobile_cdp_injector.build_worker_patch）。表结构与页面版本一致，但
    extends 指向 Worker 作用域内的同名接口——同一份表在页面里会解析到
    window，在 Worker 里解析到 self。
    """
    if target_os != "ios":
        return {}
    return {
        "interfaces": {k: dict(v) for k, v in _IOS_WORKER_ADD_INTERFACES.items()},
        "globalDrops": list(_IOS_WORKER_GLOBAL_DROPS),
        "navProto": list(_IOS_WORKER_NAV_PROTO),
        # Worker 独有的全局项：真机 WebKit Worker 的 ServiceWorkerContainer
        # 是规范内接口（Chromium 的 Worker 全局没有它，见 navProto 的
        # serviceWorker 成员），必须在这里补建，否则 navProto 里的
        # serviceWorker getter 读不到容器对象。
        "globals": dict(_IOS_WORKER_ADD_GLOBALS),
        # F-92：Worker 侧 TextMetrics 的字体度量表。Worker 是独立全局，页面
        # addMembers 到不了，故与页面同源再下发一份（注入器在 Worker 模板里
        # 重装 measureText / TextMetrics getter）。
        "addMembers": {
            "emRatio": {k: list(v) for k, v in _WK_EM_RATIO.items()},
            "emAdjust": [list(t) for t in _WK_EM_ADJ],
        },
    }


def _parse_os_version(value: Any) -> Optional[tuple]:
    """'18.6' / '16.4.1' / '17' / 缺省 → 版本元组；无法解析返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    parts = []
    for chunk in text.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


# F-119：WebKit 把 WindowEventHandlers 转发到 body/frameset，BCD Window
# 事件记 Safari 18.2，故按 profile 版本门控；低于 18.2 不补。
_PAGESWAP_MIN = (18, 2)


def _ios_handlers(version: Optional[tuple]) -> List[Dict[str, Any]]:
    rows = [dict(r) for r in _IOS_ADD_SPEC["handlers"]]
    if version is None or version >= _PAGESWAP_MIN:
        rows += [{"ctor": c, "name": n}
                 for c in ("HTMLBodyElement", "HTMLFrameSetElement")
                 for n in ("onpageswap", "onpagereveal")]
    return rows


def _add_members(target_os: str, os_version: Any = None) -> Dict[str, Any]:
    if target_os != "ios":
        return {}
    version = _parse_os_version(os_version)
    return {
        "protos": [dict(r) for r in _IOS_ADD_SPEC["protos"]],
        # F-92：TextMetrics.emHeight 的字体度量表（注入器按 ctx.font 逐实例求值）
        "emRatio": {k: list(v) for k, v in _WK_EM_RATIO.items()},
        "emAdjust": [list(t) for t in _WK_EM_ADJ],
        "statics": [dict(r) for r in _IOS_ADD_SPEC["statics"]],
        "constants": [dict(r) for r in _IOS_ADD_SPEC["constants"]],
        "handlers": _ios_handlers(version),
        "globals": dict(_IOS_ADD_SPEC["globals"]),
        "uaFormCss": _IOS_ADD_SPEC["uaFormCss"],
        "uaHeights": dict(_IOS_ADD_SPEC["uaHeights"]),
        "interfaces": {k: {"construct": v["construct"], "length": v["length"],
                           "noNew": v.get("noNew"),
                           "repair": bool(v.get("repair")),
                           "extends": v.get("extends"),
                           "realBase": bool(v.get("realBase")),
                           "dictArg": bool(v.get("dictArg")),
                           "init": dict(v.get("init") or {}),
                           "argInit": list(v.get("argInit") or []),
                           "statics": list(v.get("statics") or []),
                           "members": list(v.get("members") or [])}
                       for k, v in _IOS_ADD_SPEC["interfaces"].items()},
    }



# ------------------------------------------------------------------ 指纹生成
def generate_mobile_fingerprint(
    target_os: str,
    manufacturer: Optional[str] = None,
    mode: str = "generate",
    browser: Optional[str] = None,
    device_model: Optional[str] = None,
) -> Dict[str, Any]:
    """生成一套移动端指纹（基于真实机型库）。

    Parameters:
        target_os: "android" or "ios"
        manufacturer: 可选品牌（Samsung/Google/Xiaomi/OnePlus/Honor/Huawei/Apple 等），
                      无匹配时回退为该平台全库，实际结果见返回的 manufacturer
        device_model: 可选具体机型名（如 Pixel 8 Pro）或型号代码（如 SM-S928B），
                      指定后不再按权重随机
        mode: "generate"（版本随机）/ "preset"（版本取机型当前主流组合）
        browser: 浏览器口径。Android 仅 "chrome"（默认）；iOS 可选
                 "safari"（默认）/"chrome_ios"（Chrome iOS，CriOS）。
    """
    if target_os not in ("android", "ios"):
        raise ValueError(f"不支持的移动端目标 OS: {target_os}")
    mode = "preset" if mode == "preset" else "generate"
    browser_choice = _normalize_browser(target_os, browser)

    # 保留旧版测试/扩展对两参数 _pick_device 的替换能力。
    device = (_pick_device(target_os, manufacturer, device_model)
              if device_model else _pick_device(target_os, manufacturer))
    screen_meta = device["screen"]
    css_w, css_h = int(screen_meta["css_w"]), int(screen_meta["css_h"])
    viewport_h = int(screen_meta["viewport_h"])
    dpr = float(screen_meta["dpr"])

    os_version = _pick_os_version(device, mode)
    is_tablet = device["type"] == "tablet"
    max_touch_points = int(device.get("max_touch_points") or 5)
    memory_class = _memory_class(int(device.get("ram_gb") or 4))
    # F-99：显示面板色域独立于 GPU。Android 只在有面板证据时下发 P3；
    # iPhone/iPad profile 按既有全系 Display-P3 口径固定为 p3。
    display_gamut = (_android_display_gamut(device)
                     if target_os == "android" else "p3")

    if target_os == "android":
        android_browser = _pick_android_browser(mode)
        major = str(android_browser["major"])
        mobile_token = "" if is_tablet else "Mobile "
        user_agent = (
            f"Mozilla/5.0 ({ANDROID_REDUCED_UA_PLATFORM}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 "
            f"{mobile_token}Safari/537.36"
        )
        # 机型与系统版本不进 UA，只走 UA-CH 两条高熵头（见上方常量注释）。
        nav_platform = ANDROID_REDUCED_NAV_PLATFORM
        vendor = "Google Inc."
        ua_ch: Optional[Dict[str, Any]] = _android_ua_ch(device, os_version, android_browser)
        browser_version = str(android_browser["full"])
        platform_version_ch = f"{os_version}.0.0"
        battery: Optional[Dict[str, Any]] = _generate_battery_api()
        device_memory: Optional[int] = memory_class
        remove_props: List[str] = list(_ANDROID_REMOVE_NAV_PROPS)
        # Android 构建无内置 PDF 插件（Chromium 构建开关 enable_pdf=!is_android）：
        # 真机 Chrome Android 的 plugins/mimeTypes 为空、pdfViewerEnabled 恒 false。
        # 宿主桌面内核是 5 个 PDF 插件 + true，是「桌面装手机」的单行级证据。
        empty_plugins = True
        # NetworkInformation.type：仅 Android 实现（MDN BCD chrome_android=38；
        # 桌面 chrome 仅 ChromeOS 有）——桌面内核 'type' in navigator.connection
        # 为 false，同样是单行级证据。真机取值 wifi/cellular/ethernet/…（枚举）。
        # 环境经代理出网、无蜂窝信息，取最常见且稳定的 'wifi'。
        connection_type: Optional[str] = "wifi"
        # downlinkMax 与 type 同属 NetInfoDownlinkMax 开关，仅 Android 实现；
        # 取值见 _ANDROID_DOWNLINK_MAX_MBPS（内核子类型表的 Wi-Fi 值域）。
        downlink_max: Optional[float] = _ANDROID_DOWNLINK_MAX_MBPS
    else:
        os_token = os_version.replace(".", "_")
        safari_version = ".".join(os_version.split(".")[:2])  # iOS 27.0.1 → Safari 27.0
        if browser_choice == "chrome_ios":
            # Chrome iOS（CriOS）：WKWebView 壳 + 系统 WebKit。UA 为 reduced
            # 形态 CriOS/<major>.0.0.0（与 Android 侧 Chrome/<major>.0.0.0
            # 同法），版本随内核实测引擎；无 UA-CH / Battery（WKWebView 无
            # 这些 API）。页面世界检测面差异（__gCrWeb / 改写痕迹 /
            # webkit.messageHandlers / 无 window.chrome）由运行期注入层
            # （mobile_cdp_injector 的 iosChrome 段）补齐。
            ios_browser = _pick_ios_browser()
            crios_major = str(ios_browser["major"])
            browser_version = str(ios_browser["full"])
            ua_segment = f"CriOS/{crios_major}.0.0.0"
        else:
            browser_version = safari_version
            ua_segment = f"Version/{safari_version}"
        if is_tablet:
            user_agent = (
                f"Mozilla/5.0 (iPad; CPU OS {os_token} like Mac OS X) "
                f"AppleWebKit/605.1.15 (KHTML, like Gecko) {ua_segment} "
                f"Mobile/15E148 Safari/604.1"
            )
            nav_platform = "iPad"
        else:
            user_agent = (
                f"Mozilla/5.0 (iPhone; CPU iPhone OS {os_token} like Mac OS X) "
                f"AppleWebKit/605.1.15 (KHTML, like Gecko) {ua_segment} "
                f"Mobile/15E148 Safari/604.1"
            )
            nav_platform = "iPhone"
        vendor = "Apple Computer, Inc."
        ua_ch = None  # Safari / CriOS 均不暴露 UA-CH
        platform_version_ch = os_version if os_version.count(".") >= 2 else f"{os_version}.0"
        battery = None  # WKWebView 无 Battery API
        device_memory = None
        remove_props = list(_IOS_REMOVE_NAV_PROPS)
        empty_plugins = True
        connection_type = None  # Safari / CriOS 均无 navigator.connection（含 type）
        downlink_max = None

    app_version = user_agent.split("Mozilla/", 1)[-1]

    screen = {
        "availHeight": int(screen_meta["h"]),
        "availWidth": int(screen_meta["w"]),
        "availTop": 0,
        "availLeft": 0,
        "colorDepth": 24,
        "height": int(screen_meta["h"]),
        "pixelDepth": 24,
        "width": int(screen_meta["w"]),
        "devicePixelRatio": dpr,
        # 浏览器可见的 CSS 尺寸（物理 / DPR）与首屏视口高度（扣除浏览器 UI）
        "cssWidth": css_w,
        "cssHeight": css_h,
        "cssViewportHeight": viewport_h,
    }

    navigator = {
        "userAgent": user_agent,
        "userAgentData": ua_ch,
        "doNotTrack": None,
        "appCodeName": "Mozilla",
        "appName": "Netscape",
        "appVersion": app_version,
        "oscpu": None,  # Chrome/Safari 均不暴露 oscpu（Firefox 专有属性）
        "webdriver": False,
        "language": "zh-CN",
        "languages": ["zh-CN", "zh", "en-US", "en"],
        "platform": nav_platform,
        "hardwareConcurrency": int(device.get("cpu_cores") or 8),
        "deviceMemory": device_memory,
        "product": "Gecko",
        "productSub": "20030107",
        "vendor": vendor,
        "vendorSub": "",
        "maxTouchPoints": max_touch_points,
        "extraProperties": {},
        "offline": False,
        "cookieEnabled": True,
    }

    video_card = {
        "vendor": device["gpu"]["vendor"],
        "renderer": device["gpu"]["renderer"],
    }

    audio_codecs = {
        "aac": "audio/mp4a-enc;codecs=aac",
        "flac": "audio/flac;codecs=flac",
        "mp3": "audio/mpeg;codecs=mp3",
        "opus": "audio/ogg;codecs=opus",
        "vorbis": "audio/ogg;codecs=vorbis",
    }

    video_codecs = {
        "av1": "video/mp4;codecs=av01.0.05M.08",
        "h264": "video/mp4;codecs=avc1.42E01E",
        "vp8": "video/webm;codecs=vp8",
        "vp9": "video/webm;codecs=vp09.00.10.08",
    }

    plugins_data = {
        "pdfViewer": "application/pdf",
        "widevine": "video/mp4",
        "nacl": "application/x-nacl",
    }

    multimedia_devices = [
        "microphone",
        "speaker",
        "camera-rear",
        "camera-front",
        "video-output",
    ]

    fonts = _generate_mobile_font_subset(target_os, full=(mode == "preset"))

    fingerprint = {
        "screen": screen,
        "navigator": navigator,
        "headers": {},  # 由 HeadersGenerator 填充
        "videoCodecs": video_codecs,
        "audioCodecs": audio_codecs,
        "pluginsData": plugins_data,
        "battery": battery,
        "videoCard": video_card,
        "multimediaDevices": multimedia_devices,
        "fonts": fonts,
        "mockWebRTC": False,
        "slim": False,
        # 实际配对结果（供 create_mobile_fingerprint / 摘要使用）
        "manufacturer": device["brand"],
        "deviceModel": device["market_name"],
        # 移动端特有扩展：运行期注入层（mobile_cdp_injector）据此落地
        "mobileExtra": {
            "model": str(device["model"]),
            "marketName": device["market_name"],
            "deviceType": device["type"],
            "released": device.get("released"),
            "osVersion": os_version,
            "browser": browser_choice,
            "browserVersion": browser_version,
            "platformVersionCh": platform_version_ch,
            "memoryClass": memory_class,
            "hasUaCh": ua_ch is not None,
            "hasBattery": battery is not None,
            "displayGamut": display_gamut,
            "emptyPlugins": empty_plugins,
            "connectionType": connection_type,
            "downlinkMax": downlink_max,
            # F-123：机型能力位随档案落库（与 derive_spec 同口径），否则 A17
            # Pro 档的导出/API 面表里 av01 仍是 ''。
            "codecCapability": _codec_capability(
                target_os,
                av1=_ios_av1_capable(str(device["model"]), device["market_name"])),
            "webglProfile": _webgl_profile(target_os, video_card["renderer"]),
            "removeNavigatorProps": remove_props,
            "uaCh": ua_ch,
            "maxTouchPoints": max_touch_points,
            "geolocation": _MOBILE_GEOLOCATION,
            "camera": _generate_camera_api(),
            "webrtc": _MOBILE_WEBRTC,
        },
    }

    return fingerprint


def seeds_to_config(fingerprint_data: dict) -> dict:
    """把固定噪声种子转成 camoufox launch_options 的 config 参数。"""
    seeds = fingerprint_data.get("seeds", {})
    return {
        "canvas:seed": int(seeds.get("canvas", 1)),
        "audio:seed": int(seeds.get("audio", 1)),
        "fonts:spacing_seed": int(seeds.get("font_spacing", 1)),
    }


def create_mobile_fingerprint(
    target_os: str,
    mode: str = "generate",
    manufacturer: Optional[str] = None,
    browser: Optional[str] = None,
    device_model: Optional[str] = None,
) -> dict:
    """创建移动端环境指纹（与桌面端 create_fingerprint 接口一致）。

    - mode=preset:  真实机型预设（版本组合取机型当前主流值）
    - mode=generate: 基于真实机型库的合成（版本随机）
    - browser: Android 恒为 chrome；iOS 可选 safari（默认）/ chrome_ios
    - device_model: 可选具体机型名或型号代码；指定后固定该机型
    """
    if target_os not in ("android", "ios"):
        raise ValueError(f"不支持的移动端目标 OS: {target_os}")
    mode = "preset" if mode == "preset" else "generate"

    data = {
        "mode": mode,
        "fingerprint": None,
        "preset": None,
        "seeds": {
            "canvas": random.randint(1, _SEED_MAX),
            "audio": random.randint(1, _SEED_MAX),
            "font_spacing": random.randint(1, _SEED_MAX),
        },
        "fonts": [],
        "voices": None,  # 移动端通常没有 TTS
        "health": {"score": 100, "warnings": []},
        "target_os": target_os,
        "manufacturer": manufacturer,
        "device_model": None,
    }

    raw_fp = generate_mobile_fingerprint(
        target_os, manufacturer, mode, browser, device_model=device_model)
    data["fingerprint"] = raw_fp

    # 厂商 / 机型以实际机型库配对结果为准（未指定 manufacturer 时为按权重抽取结果）
    data["manufacturer"] = raw_fp.get("manufacturer") or manufacturer
    data["device_model"] = raw_fp.get("deviceModel")

    # 字体子集直接取用指纹内的同一份结果，避免两处随机不一致
    data["fonts"] = list(raw_fp.get("fonts") or [])

    from app.fingerprint_engine import check_health
    data["health"] = check_health(data, target_os)

    return data


# ------------------------------------------------------------------ 库查询（API/UI）
def list_brands(target_os: Optional[str] = None) -> Dict[str, List[str]]:
    """返回各平台可用品牌（用于面板品牌选择器）。"""
    result: Dict[str, List[str]] = {"android": [], "ios": []}
    for dev in _DEVICES:
        if target_os and dev["platform"] != target_os:
            continue
        brand = dev["brand"]
        if brand not in result.setdefault(dev["platform"], []):
            result[dev["platform"]].append(brand)
    return {k: sorted(v) for k, v in result.items()}


def library_devices(target_os: Optional[str] = None) -> List[Dict[str, Any]]:
    """机型清单（摘要字段，供 API/UI 展示）。"""
    out = []
    for dev in _DEVICES:
        if target_os and dev["platform"] != target_os:
            continue
        out.append({
            "brand": dev["brand"],
            "platform": dev["platform"],
            "type": dev["type"],
            "model": dev["model"],
            "market_name": dev["market_name"],
            "released": dev.get("released"),
            "screen": f"{dev['screen']['css_w']}x{dev['screen']['css_h']}@{dev['screen']['dpr']}",
            "gpu": dev["gpu"]["renderer"],
            "weight": dev["weight"],
        })
    return out


def library_info() -> Dict[str, Any]:
    return {
        "version": _LIBRARY.get("version"),
        "updated": _LIBRARY.get("updated"),
        "notes": _LIBRARY.get("notes"),
        "count": len(_DEVICES),
        "brands": list_brands(),
    }


if __name__ == "__main__":
    print("=== 机型库 ===")
    print(json.dumps(library_info(), ensure_ascii=False, indent=1))

    print("\n=== Android preset ===")
    android_fp = create_mobile_fingerprint("android", "preset")
    fp = android_fp["fingerprint"]
    print(f"UA: {fp['navigator']['userAgent']}")
    print(f"机型: {android_fp['manufacturer']} {android_fp['device_model']}")
    print(f"Screen: {fp['screen']['width']}x{fp['screen']['height']} @{fp['screen']['devicePixelRatio']} "
          f"(css {fp['screen']['cssWidth']}x{fp['screen']['cssHeight']}, vp {fp['screen']['cssViewportHeight']})")
    print(f"GPU: {fp['videoCard']['vendor']} / {fp['videoCard']['renderer']}")
    print(f"UA-CH model: {(fp['mobileExtra'].get('uaCh') or {}).get('model')}")
    print(f"health: {android_fp['health']}")

    print("\n=== iOS generate ===")
    ios_fp = create_mobile_fingerprint("ios", "generate")
    fp = ios_fp["fingerprint"]
    print(f"UA: {fp['navigator']['userAgent']}")
    print(f"机型: {ios_fp['manufacturer']} {ios_fp['device_model']}")
    print(f"Screen: {fp['screen']['width']}x{fp['screen']['height']} @{fp['screen']['devicePixelRatio']} "
          f"(css {fp['screen']['cssWidth']}x{fp['screen']['cssHeight']}, vp {fp['screen']['cssViewportHeight']})")
    print(f"GPU: {fp['videoCard']['vendor']} / {fp['videoCard']['renderer']}")
    print(f"remove props: {fp['mobileExtra']['removeNavigatorProps']}")
    print(f"health: {ios_fp['health']}")
