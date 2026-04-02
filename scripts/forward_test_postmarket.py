#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
盘后验证（前瞻验证 Phase 2）

流程：
  1. 读取当日未验证的 backtest_records
  2. 拉取收盘价，计算涨跌幅
  3. 判断选股命中（收涨）和方向命中
  4. 生成日报 + 滚动统计
  5. 胜率连续过低时通过已配置渠道告警

用法：
  python scripts/forward_test_postmarket.py
  python scripts/forward_test_postmarket.py --date 2026-04-02
  python scripts/forward_test_postmarket.py --stats-only --days 10
"""

import argparse
import logging
import os
import sys
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
logger = logging.getLogger("forward_test.postmarket")


def parse_args():
    p = argparse.ArgumentParser(description="前瞻验证 - 盘后收盘验证")
    p.add_argument("--date", default=None, help="交易日 YYYY-MM-DD（默认今天）")
    p.add_argument("--days", type=int, default=5, help="滚动统计天数")
    p.add_argument("--stats-only", action="store_true", help="仅输出统计，不做验证")
    p.add_argument("--no-notify", action="store_true", help="不发送通知")
    return p.parse_args()


def main():
    args = parse_args()

    Config._instance = None
    config = get_config()

    from src.services.forward_test_service import ForwardTestService
    fts = ForwardTestService()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    # === 验证当日 ===
    if not args.stats_only:
        logger.info("===== 前瞻验证 - 盘后验证 (date=%s) =====", date_str)
        day_result = fts.validate_day(date_str)

        if day_result["total"] == 0:
            logger.warning("当日无可验证记录 (date=%s)，可能尚未执行盘前选股", date_str)
        else:
            daily_report = fts.build_daily_report(day_result)
            logger.info("\n%s", daily_report)

            # 推送日报
            if not args.no_notify:
                _send_notification(daily_report)

    # === 滚动统计 ===
    logger.info("===== 滚动统计 (最近 %d 天) =====", args.days)
    stats = fts.get_rolling_stats(days=args.days)

    for d in stats["daily"]:
        logger.info(
            "  %s: 命中 %d/%d (%.1f%%) | 方向 %.1f%%",
            d["date"], d["scan_hits"], d["total"],
            d["scan_win_rate"], d["direction_win_rate"],
        )

    logger.info(
        "平均选股胜率: %.1f%% | 平均方向胜率: %.1f%% | 连续低胜率天数: %d",
        stats["avg_scan_win_rate"],
        stats["avg_direction_win_rate"],
        stats.get("consecutive_low_days", 0),
    )

    # === 胜率告警 ===
    if stats["alert"]:
        logger.warning("⚠️ 选股策略胜率连续过低，触发告警！")
        alert_msg = fts.build_alert_message(stats)
        logger.info("\n%s", alert_msg)

        if not args.no_notify:
            _send_notification(alert_msg)

    logger.info("===== 盘后验证完成 =====")
    return 0


def _send_notification(content: str):
    """复用现有通知渠道发送消息。"""
    try:
        from src.notification import NotificationService
        notifier = NotificationService()
        if notifier.is_available():
            notifier.send(content)
            logger.info("通知已发送")
        else:
            logger.info("无可用通知渠道，跳过推送")
    except Exception as e:
        logger.warning("通知发送失败: %s", e)


if __name__ == "__main__":
    sys.exit(main())
