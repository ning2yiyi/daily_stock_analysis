#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
盘前选股 + 分析（前瞻验证 Phase 1）

流程：
  1. 执行 A 股扫描（Scanner）→ 选出 Top 10
  2. 对每只股票运行分析（Pipeline）
  3. 将选股+分析结果写入 backtest_records 表

用法：
  python scripts/forward_test_premarket.py
  python scripts/forward_test_premarket.py --market cn --top 10
  python scripts/forward_test_premarket.py --date 2026-04-02
"""

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime

# 项目根目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

# 代理
if os.getenv("GITHUB_ACTIONS") != "true" and os.getenv("USE_PROXY", "false").lower() == "true":
    proxy_host = os.getenv("PROXY_HOST", "127.0.0.1")
    proxy_port = os.getenv("PROXY_PORT", "10809")
    proxy_url = f"http://{proxy_host}:{proxy_port}"
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url

from src.config import setup_env, get_config, Config
setup_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("forward_test.premarket")


def parse_args():
    parser = argparse.ArgumentParser(description="前瞻验证 - 盘前选股+分析")
    parser.add_argument("--market", default="cn", choices=["cn", "us"], help="扫描市场")
    parser.add_argument("--top", type=int, default=10, help="最终选股数量")
    parser.add_argument("--date", default=None, help="交易日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--no-analysis", action="store_true", help="跳过分析，仅选股")
    parser.add_argument("--notify", action="store_true", help="发送选股结果通知")
    return parser.parse_args()


def wait_for_scan(scanner, task_id: str, timeout: int = 600) -> bool:
    """同步等待扫描任务完成。"""
    start = time.time()
    while True:
        task = scanner.get_task(task_id)
        if not task:
            logger.error("任务 %s 不存在", task_id)
            return False
        if task.status == "done":
            logger.info("扫描完成: 筛选 %d / 总计 %d", task.screened, task.total)
            return True
        if task.status == "error":
            logger.error("扫描失败: %s", task.error)
            return False
        if time.time() - start > timeout:
            logger.error("扫描超时 (%ds)", timeout)
            return False
        time.sleep(3)


def main():
    args = parse_args()

    # 重置单例以读取最新 .env
    Config._instance = None
    config = get_config()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    logger.info("===== 前瞻验证 - 盘前选股 (date=%s, market=%s) =====", date_str, args.market)

    # Phase 1: 扫描
    from src.services.stock_scanner_service import StockScannerService
    scanner = StockScannerService()
    task_id = scanner.start_scan(market=args.market, top_final=args.top)
    logger.info("扫描任务已启动: %s", task_id)

    if not wait_for_scan(scanner, task_id):
        logger.error("扫描失败，退出")
        return 1

    candidates = scanner.get_candidates(task_id, llm_only=True)
    if not candidates:
        candidates = scanner.get_candidates(task_id, llm_only=False)

    if not candidates:
        logger.error("无选股结果，退出")
        return 1

    logger.info("选股结果 (%d 只):", len(candidates))
    for c in candidates:
        logger.info(
            "  %s (%s) | 评分=%.0f | 排名=%s | 理由=%s",
            c.code, c.name, c.quant_score,
            c.llm_rank or "-", (c.llm_reason or "-")[:40],
        )

    # Phase 2: 分析
    analysis_items = []
    if not args.no_analysis:
        from src.core.pipeline import StockAnalysisPipeline
        from src.enums import ReportType

        query_id = uuid.uuid4().hex
        pipeline = StockAnalysisPipeline(
            config=config, query_id=query_id, query_source="forward_test",
        )

        stock_codes = [c.code for c in candidates]
        logger.info("开始分析 %d 只股票...", len(stock_codes))
        results = pipeline.run(
            stock_codes=stock_codes,
            send_notification=False,
        )

        # 建立 code → result 映射
        result_map = {r.code: r for r in results if r}

        for c in candidates:
            r = result_map.get(c.code)
            sniper = r.get_sniper_points() if r and hasattr(r, "get_sniper_points") else {}
            analysis_items.append({
                "code": c.code,
                "name": c.name,
                "quant_score": c.quant_score,
                "llm_rank": c.llm_rank,
                "query_id": r.query_id if r else None,
                "operation_advice": r.operation_advice if r else None,
                "sentiment_score": r.sentiment_score if r else None,
                "ideal_buy": sniper.get("ideal_buy"),
                "stop_loss": sniper.get("stop_loss"),
            })
    else:
        for c in candidates:
            analysis_items.append({
                "code": c.code,
                "name": c.name,
                "quant_score": c.quant_score,
                "llm_rank": c.llm_rank,
            })

    # Phase 3: 写入 BacktestRecord
    from src.services.forward_test_service import ForwardTestService
    fts = ForwardTestService()
    saved = fts.record_predictions(date_str, task_id, analysis_items, market=args.market)
    logger.info("写入前瞻记录 %d 条", saved)

    # Phase 4: 通知（可选）
    if args.notify:
        try:
            from src.notification import NotificationService
            notifier = NotificationService()
            if notifier.is_available():
                lines = [f"## 🔍 前瞻选股 ({date_str})\n"]
                for item in analysis_items:
                    advice = item.get("operation_advice", "-")
                    score = item.get("sentiment_score", "?")
                    lines.append(f"- **{item['code']}** ({item.get('name', '-')}) | {advice} | 评分 {score}")
                notifier.send("\n".join(lines))
                logger.info("选股结果已推送")
        except Exception as e:
            logger.warning("推送失败: %s", e)

    logger.info("===== 盘前选股完成 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
