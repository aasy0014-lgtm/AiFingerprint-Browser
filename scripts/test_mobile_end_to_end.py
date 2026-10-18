#!/usr/bin/env python3
"""移动端环境创建和启动测试

演示完整的移动端环境创建流程：
1. 创建 Android/iOS 环境配置
2. 生成指纹数据
3. 调用 API 创建环境
4. 启动浏览器并注入 CDP 特征
5. 验证移动端特性
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.mobile_fingerprints import create_mobile_fingerprint
from app.fingerprint_engine import build_launch_kwargs, summarize


def test_android_profile():
    """测试 Android 环境配置文件"""
    print("=" * 70)
    print("Android 环境配置测试")
    print("=" * 70)
    
    # 1. 生成指纹
    print("\n[1] 生成 Samsung Galaxy S24 Ultra 指纹...")
    fp_data = create_mobile_fingerprint("android", "generate", "Samsung")
    
    # 2. 摘要展示
    print("\n[2] 指纹摘要:")
    summary = summarize(fp_data)
    for key, value in summary.items():
        print(f"   {key}: {value}")
    
    # 3. 构建启动参数
    print("\n[3] 构建启动参数...")
    kwargs = build_launch_kwargs(fp_data)
    print(f"   Config seeds: {kwargs.get('config')}")
    print(f"   Mobile target OS: {kwargs.get('mobile_target_os')}")
    print(f"   Fonts count: {len(kwargs.get('fonts', []))}")
    
    # 4. 健康检查
    print("\n[4] 健康分检查:")
    health = fp_data.get("health", {})
    print(f"   Score: {health['score']}/100")
    if health.get("warnings"):
        print(f"   Warnings: {health['warnings']}")
    else:
        print(f"   ✅ 无警告")
    
    return fp_data


def test_ios_profile():
    """测试 iOS 环境配置文件"""
    print("\n" + "=" * 70)
    print("iOS 环境配置测试 (iPad)")
    print("=" * 70)
    
    # 1. 生成指纹
    print("\n[1] 生成 iPad Pro 指纹...")
    fp_data = create_mobile_fingerprint("ios", "generate", "Apple")
    
    # 2. 摘要展示
    print("\n[2] 指纹摘要:")
    summary = summarize(fp_data)
    for key, value in summary.items():
        print(f"   {key}: {value}")
    
    return fp_data


def test_environment_consistency():
    """测试跨重启一致性"""
    print("\n" + "=" * 70)
    print("指纹一致性测试（模拟重启）")
    print("=" * 70)
    
    print("\n[1] 第 1 次启动:")
    fp1 = create_mobile_fingerprint("android", "generate", "Google")
    nav1 = fp1["fingerprint"]["navigator"]
    screen1 = fp1["fingerprint"]["screen"]
    print(f"   UA: {nav1['userAgent'][:60]}...")
    print(f"   Screen: {screen1['width']}x{screen1['height']}")
    print(f"   Canvas Seed: {fp1['seeds']['canvas']}")
    
    print("\n[2] 第 2 次启动:")
    fp2 = create_mobile_fingerprint("android", "generate", "Google")
    nav2 = fp2["fingerprint"]["navigator"]
    screen2 = fp2["fingerprint"]["screen"]
    print(f"   UA: {nav2['userAgent'][:60]}...")
    print(f"   Screen: {screen2['width']}x{screen2['height']}")
    print(f"   Canvas Seed: {fp2['seeds']['canvas']}")
    
    print("\n注意:")
    print("- 种子应保持一致（实际场景中从数据库读取固定种子）")
    print("- UA 和屏幕尺寸应在同一种子下相同")
    print("- 当前实现为随机生成，仅演示数据结构完整性")


def test_api_integration():
    """测试与 API 层的集成"""
    print("\n" + "=" * 70)
    print("API 层集成测试")
    print("=" * 70)
    
    # 模拟 ProfileCreate 请求体
    profile_create_example = {
        "name": "Galaxy S24 测试环境",
        "group_name": "移动测试分组",
        "notes": "用于 H5 自动化测试的 Android 环境",
        "kernel": "fp-chromium",  # 移动端只能用 Chromium
        "target_os": "android",
        "fingerprint_mode": "generate",
        "proxy": None,
        "launch": {
            "headless": False,
            "geoip": True,
            "humanize": True,
            "preset": "standard",
        },
    }
    
    print("\n[1] ProfileCreate 请求示例:")
    for key, value in profile_create_example.items():
        print(f"   {key}: {value}")
    
    print("\n[2] 预期响应字段:")
    expected_fields = [
        "id", "name", "target_os", "manufacturer",
        "fingerprint_summary", "launch",
    ]
    for field in expected_fields:
        print(f"   - {field}")
    
    print("\n[3] 启动流程:")
    print("   1. fp_chromium_kernel.start() → 启动 Chrome + CDP")
    print("   2. launcher._setup_mobile_context() → 注入 Viewport+Touch")
    print("   3. inject_mobile_features() → 修改 navigator 字段")
    print("   4. 返回 ws_endpoint 供 Playwright/Selenium 连接")


def main():
    """运行所有测试"""
    try:
        print("\n" + "🚀 移动端环境创建测试开始".center(70, "="))
        
        # 测试 1: Android 配置
        android_fp = test_android_profile()
        
        # 测试 2: iOS 配置
        ios_fp = test_ios_profile()
        
        # 测试 3: 一致性
        test_environment_consistency()
        
        # 测试 4: API 集成
        test_api_integration()
        
        print("\n" + "=" * 70)
        print("✅ 所有测试完成!")
        print("=" * 70)
        print("\n验证入口:")
        print("  - 数据层测试：.venv/bin/python scripts/mobile_test.py")
        print("  - 真机级验证：.venv/bin/python scripts/mobile_launch_verify.py android|ios")
        print("\n文档:")
        print("  - docs/移动端真实指纹库.md（现状说明与扩展指南）")
        print("  - docs/移动端增强实现方案.md（早期设计稿）")
        
    except Exception as e:
        print(f"\n❌ 测试失败：{e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
