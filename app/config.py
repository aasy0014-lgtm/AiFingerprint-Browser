"""全局配置：路径、端口、常量。

支持环境变量 FPWB_HOME（或 run.py --home）重定位数据目录，
用于多实例部署（如同步服务器/测试实例）。
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

FPWB_HOME = Path(os.environ.get("FPWB_HOME") or BASE_DIR)
DATA_DIR = FPWB_HOME / "data"
PROFILE_ROOT = FPWB_HOME / "profiles"
DB_PATH = DATA_DIR / "fpworkbench.db"

APP_NAME = "指纹浏览器工作台"
API_VERSION = "v1"
VERSION = "0.4.0"
PHASE = 4

DEFAULT_HOST = "127.0.0.1"
# 仿 AdsPower Local API 风格；默认端口 50325 在部分 Windows 上被
# Hyper-V 端口保留范围占用（winerror 10013），故默认使用 18080
DEFAULT_PORT = 18080

# 代理刷新接口允许的目标主机。刷新地址包含供应商凭据，只允许已知供应商
# 通过管理端保存和请求，避免把这个管理能力变成任意 SSRF 代理。
PROXY_REFRESH_ALLOWED_HOSTS = frozenset(
    host.strip().lower().rstrip(".")
    for host in os.environ.get("FPWB_PROXY_REFRESH_HOSTS", "us.udealproxy.com").split(",")
    if host.strip()
)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)


# 内置自检链接（管理界面一键打开）
DETECT_LINKS = {
    "CreepJS": "https://abrahamjuliot.github.io/creepjs/",
    "CreepJS Checker": "https://creepjs.org/checker",
    "BrowserLeaks": "https://browserleaks.com/javascript",
    "Canvas 指纹": "https://browserleaks.com/canvas",
    "WebGL 指纹": "https://browserleaks.com/webgl",
    "字体指纹": "https://browserleaks.com/fonts",
    "Client Hints": "https://browserleaks.com/client-hints",
    "WebRTC 检测": "https://browserleaks.com/webrtc",
    "Pixelscan": "https://pixelscan.net/",
    "IPHey": "https://iphey.com/",
    "AmIUnique": "https://amiunique.org/fingerprint",
    "设备能力": "https://deviceinfo.me/",
    "跟踪保护": "https://coveryourtracks.eff.org/",
    "JA3/TLS 检测": "https://scrapfly.io/web-scraping-tools/ja3-fingerprint",
    # 独立实现的综合页，避免只依赖 BrowserLeaks/CreepJS 同源的判定逻辑。
    "FingerprintJS": "https://fingerprintjs.github.io/fingerprintjs/",
    "Fingerprint Pro Demo": "https://fingerprint.com/demo/",
    "CHA.LA": "https://www.cha.la/en/environment/fingerprint",
    "ILSERBY": "https://ilserby.net/fingerprint",
    "ProxyLabs": "https://proxylabs.app/fingerprint",
    "JustBrowser": "https://justbrowser.app/fingerprint-checker",
    "Sannysoft Bot Test": "https://bot.sannysoft.com/",
    # 额外的独立实现：重点覆盖 IP/时区/语言一致性、CDP/自动化探针以及
    # 指纹抗性测试，和上面的站点判定逻辑不完全重叠。
    "BrowserScan 综合": "https://www.browserscan.net/",
    "BrowserScan Bot Test": "https://www.browserscan.net/bot-detection",
    "CleanIP 指纹": "https://cleanip.io/en-US/fingerprint",
    "PrivacyTests": "https://privacytests.org/me.html",
    "Fingerprint Bot Demo": "https://demo.fingerprint.com/web-scraping?disableBotDetection=1",
    # 第二批独立实现：补充 IP/时区、WebRTC、Canvas/音频/字体与综合一致性对拍。
    "ReadMyBrowser 综合": "https://readmybrowser.com/",
    "XDLogin IP/WebRTC": "https://xdlogin.com/check",
    "BrowserFingerprint.org": "https://browserfingerprint.org/",
    "Panopticlick 指纹": "https://panopticlick.org/",
    "BrowserScan.one 综合": "https://browserscan.one/",
    "VoriTools 指纹": "https://voritools.com/browserfingerprint/",
    "SpeedIQ 指纹": "https://www.speediq.org/browser-fingerprint-test/",
    "BellCrier 指纹": "https://bellcrier.com/fingerprint-detector/",
    "Dual Login 指纹": "https://duallogin.com/fingerprints",
    "Pixelscan.dev": "https://pixelscan.dev/",
    "BrowserLeaks.io": "https://browserleaks.io/",
}

# 成员角色
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
