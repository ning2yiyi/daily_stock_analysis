# -*- coding: utf-8 -*-
"""
完整功能测试脚本 - 覆盖配置、数据源、搜索（MX_APIKEY）、LLM 分析
输出写入 full_test_result.txt，避免终端编码问题
"""
import sys
import os
import time
import traceback

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# 重定向输出到文件
OUT = open("full_test_result.txt", "w", encoding="utf-8")

def p(msg=""):
    print(msg)
    OUT.write(msg + "\n")
    OUT.flush()

def section(title):
    p()
    p("=" * 60)
    p(f"  {title}")
    p("=" * 60)

def sub(title):
    p()
    p(f"--- {title} ---")

def ok(msg): p(f"  [OK] {msg}")
def fail(msg): p(f"  [FAIL] {msg}")
def info(msg): p(f"  [INFO] {msg}")
def warn(msg): p(f"  [WARN] {msg}")

# =========================================================
# 加载环境
# =========================================================
from dotenv import load_dotenv
load_dotenv(override=True)
import logging
logging.disable(logging.CRITICAL)

# =========================================================
# 1. 配置加载
# =========================================================
section("1. 配置加载与验证")
try:
    from src.config import get_config
    config = get_config()
    sub("基础配置")
    info(f"股票列表: {config.stock_list}")
    info(f"LLM 模型: {config.litellm_model}")
    info(f"OpenAI Base URL: {config.openai_base_url}")
    info(f"OpenAI Key: {'已配置(' + config.openai_api_key[:8] + '...)' if config.openai_api_key else '未配置'}")

    sub("搜索引擎配置")
    info(f"MX_APIKEY: {'已配置(' + config.mx_api_keys[0][:12] + '...)' if config.mx_api_keys else '未配置'}")
    info(f"Bocha Keys: {len(config.bocha_api_keys)} 个")
    info(f"Tavily Keys: {len(config.tavily_api_keys)} 个")
    info(f"搜索能力启用: {config.has_search_capability_enabled()}")

    sub("配置验证结果")
    issues = config.validate_structured()
    for issue in issues:
        prefix = "  [ERROR]" if issue.severity == "error" else ("  [WARN]" if issue.severity == "warning" else "  [INFO]")
        p(f"{prefix} {issue.message}")
    if not any(i.severity in ("error", "warning") for i in issues):
        ok("配置验证通过，无错误/警告")
    ok("配置模块正常")
except Exception as e:
    fail(f"配置加载失败: {e}")
    traceback.print_exc(file=OUT)

# =========================================================
# 2. 数据源测试
# =========================================================
section("2. 数据源测试")
try:
    from data_provider import DataFetcherManager
    mgr = DataFetcherManager()
    sub("已初始化数据源")
    for f in mgr._fetchers:
        info(f"P{f.priority} {f.name}")

    sub("A股日线 (600519贵州茅台, 5日)")
    t0 = time.time()
    df, src = mgr.get_daily_data('600519', days=5)
    elapsed = time.time() - t0
    ok(f"来源={src}, 记录数={len(df)}, 耗时={elapsed:.1f}s")
    cols = [c for c in ['date','open','close','pct_chg','volume'] if c in df.columns]
    p(df[cols].tail(3).to_string(index=False))

    sub("美股日线 (AAPL, 3日)")
    t0 = time.time()
    df2, src2 = mgr.get_daily_data('AAPL', days=3)
    elapsed2 = time.time() - t0
    ok(f"来源={src2}, 记录数={len(df2)}, 耗时={elapsed2:.1f}s")
    cols2 = [c for c in ['date','close','pct_chg'] if c in df2.columns]
    p(df2[cols2].tail(3).to_string(index=False))
except Exception as e:
    fail(f"数据源测试失败: {e}")
    traceback.print_exc(file=OUT)

# =========================================================
# 3. 妙想搜索测试（真实 API Key）
# =========================================================
section("3. 东方财富妙想搜索测试（真实 API）")
try:
    from src.search_service import MiaoXiangSearchProvider, SearchService

    if not config.mx_api_keys:
        warn("MX_APIKEY 未配置，跳过")
    else:
        provider = MiaoXiangSearchProvider(config.mx_api_keys)
        sub("测试1: 个股资讯 (贵州茅台最新研报)")
        t0 = time.time()
        resp = provider.search("贵州茅台最新研报机构观点", max_results=3)
        elapsed = time.time() - t0
        if resp.success:
            ok(f"搜索成功，耗时={elapsed:.1f}s，返回{len(resp.results)}条")
            for i, r in enumerate(resp.results, 1):
                p(f"  [{i}] 标题: {r.title}")
                p(f"      摘要: {r.snippet[:200]}...")
        else:
            fail(f"搜索失败: {resp.error_message}")

        sub("测试2: 板块/政策 (新能源政策最新动向)")
        t0 = time.time()
        resp2 = provider.search("新能源板块近期政策动向", max_results=3)
        elapsed2 = time.time() - t0
        if resp2.success:
            ok(f"搜索成功，耗时={elapsed2:.1f}s，返回{len(resp2.results)}条")
            if resp2.results:
                p(f"  标题: {resp2.results[0].title}")
                p(f"  摘要: {resp2.results[0].snippet[:200]}...")
        else:
            fail(f"搜索失败: {resp2.error_message}")

        sub("测试3: SearchService优先级（mx_keys注册为P0）")
        svc = SearchService(mx_keys=config.mx_api_keys)
        first_provider = svc._providers[0] if svc._providers else None
        if first_provider and first_provider.name == "东方财富妙想":
            ok(f"妙想已注册为最高优先级, providers={[p_.name for p_ in svc._providers]}")
        else:
            fail(f"Provider顺序异常: {[p_.name for p_ in svc._providers]}")

        sub("测试4: SearchService.search_stock_news 调用")
        t0 = time.time()
        resp3 = svc.search_stock_news("002594", "比亚迪", max_results=3)
        elapsed3 = time.time() - t0
        if resp3.success:
            ok(f"search_stock_news成功，来源={resp3.provider}，耗时={elapsed3:.1f}s")
            if resp3.results:
                p(f"  标题: {resp3.results[0].title}")
                p(f"  摘要: {resp3.results[0].snippet[:200]}...")
        else:
            fail(f"search_stock_news失败: {resp3.error_message}")

except Exception as e:
    fail(f"妙想搜索测试失败: {e}")
    traceback.print_exc(file=OUT)

# =========================================================
# 4. LLM 分析测试
# =========================================================
section("4. LLM 分析测试 (Qwen via DashScope)")
try:
    from src.analyzer import GeminiAnalyzer
    analyzer = GeminiAnalyzer()
    if not analyzer.is_available():
        warn("LLM 分析器不可用，跳过")
    else:
        ok(f"LLM 分析器已就绪，模型: {config.litellm_model}")

        sub("简单文本生成测试")
        t0 = time.time()
        result = analyzer.generate_text("请用一句话介绍贵州茅台股票")
        elapsed = time.time() - t0
        ok(f"LLM 响应成功，耗时={elapsed:.1f}s")
        p(f"  回答: {result[:200]}")
except Exception as e:
    fail(f"LLM 测试失败: {e}")
    traceback.print_exc(file=OUT)

# =========================================================
# 5. 完整单股分析流程（含搜索增强）
# =========================================================
section("5. 完整单股分析流程 (600519, dry-run模式)")
try:
    from src.search_service import SearchService as SS
    svc_full = SS(mx_keys=config.mx_api_keys) if config.mx_api_keys else SS()
    sub("搜索增强新闻获取")
    t0 = time.time()
    news_resp = svc_full.search_stock_news(
        stock_code="600519",
        stock_name="贵州茅台",
        max_results=3,
    )
    elapsed = time.time() - t0
    if news_resp.success:
        ok(f"新闻搜索成功，来源={news_resp.provider}，条数={len(news_resp.results)}，耗时={elapsed:.1f}s")
        for i, r in enumerate(news_resp.results[:2], 1):
            p(f"  [{i}] {r.title}")
    else:
        warn(f"新闻搜索返回：{news_resp.error_message}")
except AttributeError:
    # search_stock_news 方法名可能不同
    try:
        from src.search_service import SearchService as SS2
        svc2 = SS2(mx_keys=config.mx_api_keys) if config.mx_api_keys else SS2()
        ok(f"SearchService is_available={svc2.is_available}")
        # 尝试通过 search 代替
        resp_news = svc2.search(f"贵州茅台 600519 今日新闻", max_results=3, days=3)
        ok(f"search方法: success={resp_news.success}, provider={resp_news.provider}")
    except Exception as e2:
        fail(str(e2))
except Exception as e:
    fail(f"完整流程测试失败: {e}")
    traceback.print_exc(file=OUT)

# =========================================================
# 6. 单元测试套件
# =========================================================
section("6. 单元测试套件 (非网络)")
try:
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_search_miaoxiang_provider.py",
         "tests/test_config_validate_structured.py",
         "-v", "--tb=short", "-q"],
        capture_output=True, text=True, encoding='utf-8',
        cwd=os.getcwd()
    )
    p(result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout)
    if result.returncode == 0:
        ok("单元测试全部通过")
    else:
        fail(f"单元测试失败，退出码={result.returncode}")
        if result.stderr:
            p(result.stderr[-1000:])
except Exception as e:
    fail(f"运行单元测试失败: {e}")
    traceback.print_exc(file=OUT)

# =========================================================
# 汇总
# =========================================================
section("测试汇总")
p("输出已保存至 full_test_result.txt")
p("如需测试通知渠道，请运行: python test_env.py --notify")
p("如需测试完整分析流程，请运行: python main.py --stocks 600519 --dry-run")

OUT.close()
print("测试完成，结果已写入 full_test_result.txt")
