# -*- coding: utf-8 -*-
"""
前瞻验证服务 (Forward Test)

职责：
1. 盘前：选股 + 分析 → 写入 BacktestRecord
2. 盘后：拉取收盘价 → 验证命中率 → 告警
3. 统计：滚动胜率、连续低胜率检测

与既有 backtest_service（事后回测）不同，本模块面向
「今天选 → 今天验」的前瞻闭环场景。
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, desc, func, select

from src.config import get_config, Config
from src.storage import BacktestRecord, DatabaseManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  辅助
# ---------------------------------------------------------------------------

def _direction_from_advice(advice: Optional[str]) -> str:
    """从 operation_advice 推断预测方向。"""
    if not advice:
        return "neutral"
    buy_words = ("买入", "加仓", "buy")
    sell_words = ("卖出", "减仓", "sell")
    for w in buy_words:
        if w in advice.lower():
            return "up"
    for w in sell_words:
        if w in advice.lower():
            return "down"
    return "neutral"


def _actual_direction(change_pct: float) -> str:
    if change_pct > 0.0:
        return "up"
    elif change_pct < 0.0:
        return "down"
    return "flat"


# ---------------------------------------------------------------------------
#  Service
# ---------------------------------------------------------------------------

class ForwardTestService:
    """盘前选股 → 盘后验证 的前瞻闭环服务。"""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ===== 盘前：记录预测 ====================================================

    def record_predictions(
        self,
        date_str: str,
        scan_task_id: str,
        analysis_results: List[Dict[str, Any]],
        market: str = "cn",
    ) -> int:
        """
        将选股 + 分析结果写入 backtest_records 表。

        Args:
            date_str: 交易日 YYYY-MM-DD
            scan_task_id: scanner task_id
            analysis_results: 列表，每项包含:
                code, name, quant_score, llm_rank,
                query_id, operation_advice, sentiment_score,
                ideal_buy, stop_loss
        Returns:
            写入行数
        """
        if not analysis_results:
            return 0

        saved = 0
        with self.db.get_session() as session:
            for item in analysis_results:
                advice = item.get("operation_advice")
                record = BacktestRecord(
                    date=date_str,
                    code=item["code"],
                    name=item.get("name"),
                    market=market,
                    scan_task_id=scan_task_id,
                    query_id=item.get("query_id"),
                    quant_score=item.get("quant_score"),
                    llm_rank=item.get("llm_rank"),
                    operation_advice=advice,
                    sentiment_score=item.get("sentiment_score"),
                    predicted_direction=_direction_from_advice(advice),
                    ideal_buy=item.get("ideal_buy"),
                    stop_loss=item.get("stop_loss"),
                )
                session.merge(record)  # UPSERT by (date, code)
                saved += 1
            session.commit()
        logger.info("前瞻记录写入 %d 条 (date=%s)", saved, date_str)
        return saved

    # ===== 盘后：验证 ======================================================

    def validate_day(self, date_str: str) -> Dict[str, Any]:
        """
        拉取收盘价，补全 backtest_records 并计算当日胜率。

        Returns:
            {
                "date": "2026-04-02",
                "total": 10,
                "scan_hits": 7,
                "direction_hits": 6,
                "scan_win_rate": 70.0,
                "direction_win_rate": 60.0,
                "details": [...]
            }
        """
        from data_provider.base import DataFetcherManager

        records = self._get_unvalidated(date_str)
        if not records:
            logger.warning("无待验证记录 (date=%s)", date_str)
            return {"date": date_str, "total": 0}

        fetcher = DataFetcherManager()
        details: List[Dict[str, Any]] = []

        with self.db.get_session() as session:
            for rec in records:
                try:
                    quote = fetcher.get_realtime_quote(rec.code)
                    if quote is None:
                        logger.warning("[%s] 收盘行情获取失败，跳过", rec.code)
                        continue

                    close = float(quote.price) if quote.price else None
                    pre_close = float(quote.pre_close) if getattr(quote, "pre_close", None) else None
                    change = float(quote.change_pct) if getattr(quote, "change_pct", None) else None

                    if close is None:
                        continue

                    # 计算涨跌幅 (如果 quote 没提供)
                    if change is None and pre_close and pre_close > 0:
                        change = (close - pre_close) / pre_close * 100

                    if change is None:
                        continue

                    actual_dir = _actual_direction(change)
                    scan_hit = change > 0  # 选股命中 = 收涨
                    dir_hit = (rec.predicted_direction == actual_dir) if rec.predicted_direction != "neutral" else None

                    # 更新记录
                    session.execute(
                        BacktestRecord.__table__.update()
                        .where(BacktestRecord.id == rec.id)
                        .values(
                            close_price=close,
                            open_price=float(quote.open) if getattr(quote, "open", None) else None,
                            pre_close=pre_close,
                            change_pct=round(change, 2),
                            actual_direction=actual_dir,
                            scan_hit=scan_hit,
                            direction_hit=dir_hit,
                            validated=True,
                            validated_at=datetime.now(),
                        )
                    )

                    details.append({
                        "code": rec.code,
                        "name": rec.name,
                        "advice": rec.operation_advice,
                        "predicted": rec.predicted_direction,
                        "actual": actual_dir,
                        "change_pct": round(change, 2),
                        "scan_hit": scan_hit,
                        "direction_hit": dir_hit,
                    })
                except Exception as exc:
                    logger.error("[%s] 验证异常: %s", rec.code, exc)

            session.commit()

        total = len(details)
        scan_hits = sum(1 for d in details if d["scan_hit"])
        dir_hits = sum(1 for d in details if d.get("direction_hit") is True)
        dir_total = sum(1 for d in details if d.get("direction_hit") is not None)

        result = {
            "date": date_str,
            "total": total,
            "scan_hits": scan_hits,
            "direction_hits": dir_hits,
            "scan_win_rate": round(scan_hits / total * 100, 1) if total else 0,
            "direction_win_rate": round(dir_hits / dir_total * 100, 1) if dir_total else 0,
            "details": details,
        }
        logger.info(
            "验证完成 date=%s: 选股命中 %d/%d (%.1f%%), 方向命中 %d/%d (%.1f%%)",
            date_str, scan_hits, total, result["scan_win_rate"],
            dir_hits, dir_total, result["direction_win_rate"],
        )
        return result

    # ===== 滚动统计 & 告警 =================================================

    def get_rolling_stats(self, days: int = 5) -> Dict[str, Any]:
        """
        获取最近 N 个交易日的滚动胜率。

        Returns:
            {
                "days": 5,
                "daily": [{"date": ..., "scan_win_rate": ..., ...}, ...],
                "avg_scan_win_rate": 45.0,
                "avg_direction_win_rate": 40.0,
                "alert": True,
            }
        """
        with self.db.get_session() as session:
            # 获取最近 N 天有验证记录的日期
            dates_q = (
                select(BacktestRecord.date)
                .where(BacktestRecord.validated == True)  # noqa: E712
                .group_by(BacktestRecord.date)
                .order_by(desc(BacktestRecord.date))
                .limit(days)
            )
            dates = [row[0] for row in session.execute(dates_q).fetchall()]

        if not dates:
            return {"days": days, "daily": [], "avg_scan_win_rate": 0, "alert": False}

        daily_stats: List[Dict[str, Any]] = []
        for d in sorted(dates):
            stats = self._day_stats(d)
            if stats:
                daily_stats.append(stats)

        scan_rates = [s["scan_win_rate"] for s in daily_stats if s["total"] > 0]
        avg_scan = round(sum(scan_rates) / len(scan_rates), 1) if scan_rates else 0

        dir_rates = [s["direction_win_rate"] for s in daily_stats if s.get("direction_win_rate")]
        avg_dir = round(sum(dir_rates) / len(dir_rates), 1) if dir_rates else 0

        config = get_config()
        threshold = getattr(config, "forward_test_alert_threshold", 50.0)
        alert_days = getattr(config, "forward_test_alert_days", 5)

        # 告警条件：连续 alert_days 天选股胜率 < threshold
        consecutive_low = 0
        for s in reversed(daily_stats):
            if s["total"] > 0 and s["scan_win_rate"] < threshold:
                consecutive_low += 1
            else:
                break

        alert = consecutive_low >= alert_days

        return {
            "days": days,
            "daily": daily_stats,
            "avg_scan_win_rate": avg_scan,
            "avg_direction_win_rate": avg_dir,
            "consecutive_low_days": consecutive_low,
            "alert": alert,
        }

    def build_alert_message(self, stats: Dict[str, Any]) -> str:
        """构建告警通知消息。"""
        lines = [
            "## ⚠️ 选股策略胜率告警\n",
            f"连续 **{stats['consecutive_low_days']}** 天选股胜率低于阈值\n",
            f"- 平均选股胜率: **{stats['avg_scan_win_rate']}%**",
            f"- 平均方向胜率: **{stats['avg_direction_win_rate']}%**\n",
            "### 每日详情\n",
            "| 日期 | 总数 | 选股命中 | 胜率 | 方向命中 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for d in stats["daily"]:
            lines.append(
                f"| {d['date']} | {d['total']} | {d['scan_hits']}/{d['total']} "
                f"| {d['scan_win_rate']}% | {d['direction_win_rate']}% |"
            )
        lines.append("\n> 建议检查选股评分逻辑和 LLM 精选 prompt 是否需要调整。")
        return "\n".join(lines)

    def build_daily_report(self, result: Dict[str, Any]) -> str:
        """构建每日验证报告。"""
        lines = [
            f"## 📊 前瞻验证日报 ({result['date']})\n",
            f"- 验证股票: {result['total']} 只",
            f"- 选股命中（收涨）: {result['scan_hits']}/{result['total']} "
            f"(**{result['scan_win_rate']}%**)",
            f"- 方向命中: {result['direction_hits']}/{result['total']} "
            f"(**{result['direction_win_rate']}%**)\n",
            "### 明细\n",
            "| 代码 | 名称 | 建议 | 预测 | 实际 | 涨跌% | 命中 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for d in result.get("details", []):
            hit_icon = "✅" if d["scan_hit"] else "❌"
            lines.append(
                f"| {d['code']} | {d['name'] or '-'} | {d['advice'] or '-'} "
                f"| {d['predicted']} | {d['actual']} | {d['change_pct']}% | {hit_icon} |"
            )
        return "\n".join(lines)

    # ===== 内部 =============================================================

    def _get_unvalidated(self, date_str: str) -> List[BacktestRecord]:
        with self.db.get_session() as session:
            q = (
                select(BacktestRecord)
                .where(
                    and_(
                        BacktestRecord.date == date_str,
                        BacktestRecord.validated == False,  # noqa: E712
                    )
                )
            )
            return list(session.execute(q).scalars().all())

    def _day_stats(self, date_str: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            q = (
                select(BacktestRecord)
                .where(
                    and_(
                        BacktestRecord.date == date_str,
                        BacktestRecord.validated == True,  # noqa: E712
                    )
                )
            )
            records = list(session.execute(q).scalars().all())

        if not records:
            return None

        total = len(records)
        scan_hits = sum(1 for r in records if r.scan_hit)
        dir_total = sum(1 for r in records if r.direction_hit is not None)
        dir_hits = sum(1 for r in records if r.direction_hit is True)

        return {
            "date": date_str,
            "total": total,
            "scan_hits": scan_hits,
            "scan_win_rate": round(scan_hits / total * 100, 1) if total else 0,
            "direction_hits": dir_hits,
            "direction_win_rate": round(dir_hits / dir_total * 100, 1) if dir_total else 0,
        }
