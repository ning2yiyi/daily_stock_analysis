#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略历史回测脚本

用历史日线数据对项目内置的 11 种策略逐一测试胜率。
流程：
  1. 选取 --stocks 指定的股票池（或默认池）
  2. 对每只股票，在历史日线上按 --step 天间隔取若干采样日
  3. 每个采样日，用 StockTrendAnalyzer 生成技术指标快照
  4. 将快照 + 策略 instructions 发给 LLM，让其输出决策仪表盘
  5. 取采样日后 --window 天的前瞻行情，用 BacktestEngine 评估胜负
  6. 汇总每种策略的胜率、方向准确率、平均收益

用法:
  python scripts/strategy_backtest.py
  python scripts/strategy_backtest.py --stocks 600519,000858,601318
  python scripts/strategy_backtest.py --strategies bull_trend,shrink_pullback
  python scripts/strategy_backtest.py --window 10 --samples 20 --step 5
  python scripts/strategy_backtest.py --days 120 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 确保项目根目录在 sys.path 中
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from src.config import get_config
from src.storage import DatabaseManager, StockDaily
from src.repositories.stock_repo import StockRepository
from src.core.backtest_engine import BacktestEngine, EvaluationConfig
from src.agent.skills.base import SkillManager, load_skills_from_directory
from src.stock_analyzer import StockTrendAnalyzer

logger = logging.getLogger(__name__)

# 默认股票池：流动性好的 A 股代表
DEFAULT_STOCKS = [
    "600519",  # 贵州茅台
    "000858",  # 五粮液
    "601318",  # 中国平安
    "600036",  # 招商银行
    "000001",  # 平安银行
    "601888",  # 中国中免
    "600276",  # 恒瑞医药
    "300750",  # 宁德时代
]


# ─────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────

@dataclass
class SamplePoint:
    """一个回测采样点"""
    code: str
    analysis_date: date
    start_price: float
    forward_bars: List[StockDaily]


@dataclass
class StrategyResult:
    """单策略汇总"""
    strategy_name: str
    display_name: str
    total: int = 0
    win: int = 0
    loss: int = 0
    neutral: int = 0
    direction_correct: int = 0
    insufficient: int = 0
    errors: int = 0
    sum_return_pct: float = 0.0
    details: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def evaluated(self) -> int:
        return self.win + self.loss + self.neutral

    @property
    def win_rate(self) -> Optional[float]:
        decided = self.win + self.loss
        return (self.win / decided * 100) if decided > 0 else None

    @property
    def direction_accuracy(self) -> Optional[float]:
        return (self.direction_correct / self.evaluated * 100) if self.evaluated > 0 else None

    @property
    def avg_return(self) -> Optional[float]:
        return (self.sum_return_pct / self.evaluated) if self.evaluated > 0 else None


# ─────────────────────────────────────────────────
# LLM 调用：给定技术指标 + 策略 instructions，输出建议
# ─────────────────────────────────────────────────

def _build_analysis_prompt(
    code: str,
    analysis_date: date,
    trend_snapshot: Dict[str, Any],
    daily_bars: List[Dict[str, Any]],
    skill_instructions: str,
) -> List[Dict[str, str]]:
    """构造 LLM 消息，让 LLM 基于策略分析给出决策。"""
    system_msg = f"""你是一位趋势交易分析师。你需要严格按照以下交易策略来分析股票。

{skill_instructions}

请基于提供的技术指标数据和日线行情，严格按照上述策略的判定标准进行分析。

输出格式（仅输出 JSON，不要输出其他文字）：
```json
{{
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "sentiment_score": 0-100整数,
    "reasoning": "简述判断依据（50字以内）"
}}
```"""

    # 最近 10 根 K 线概要
    recent_bars = daily_bars[-10:] if len(daily_bars) > 10 else daily_bars
    bars_text = "\n".join(
        f"  {b['date']}: O={b.get('open','?')} H={b.get('high','?')} "
        f"L={b.get('low','?')} C={b.get('close','?')} Vol={b.get('volume','?')}"
        for b in recent_bars
    )

    # 精简趋势指标
    trend_keys = [
        "trend_status", "ma_alignment", "ma5", "ma10", "ma20",
        "current_price", "bias_ma5", "bias_ma10", "bias_ma20",
        "volume_status", "volume_ratio_5d", "volume_trend",
        "macd_status", "macd_signal", "rsi_6", "rsi_12",
        "buy_signal", "signal_score",
    ]
    trend_text = "\n".join(
        f"  {k}: {trend_snapshot.get(k, 'N/A')}" for k in trend_keys if k in trend_snapshot
    )

    user_msg = f"""股票代码: {code}
分析日期: {analysis_date}

技术指标:
{trend_text}

最近 K 线数据:
{bars_text}

请根据策略判定标准分析并给出操作建议。"""

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]


def _call_llm(messages: List[Dict[str, str]], config) -> Optional[Dict[str, Any]]:
    """调用 LLM 获取分析结论。"""
    try:
        from src.agent.llm_adapter import LLMToolAdapter

        adapter = LLMToolAdapter(config=config)
        response = adapter.call_completion(
            messages=messages,
            temperature=0.3,
            max_tokens=500,
            timeout=30.0,
        )

        if not response or not response.content:
            return None

        # 提取 JSON
        text = response.content.strip()
        # 尝试从 markdown 代码块中提取
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"LLM 返回解析失败: {e}")
        return None
    except Exception as e:
        logger.warning(f"LLM 调用失败: {e}")
        return None


# ─────────────────────────────────────────────────
# 采样点生成
# ─────────────────────────────────────────────────

def _generate_sample_points(
    stock_repo: StockRepository,
    codes: List[str],
    days_back: int,
    window: int,
    step: int,
    max_samples_per_stock: int,
) -> List[SamplePoint]:
    """为每只股票生成历史采样点。"""
    today = date.today()
    # 需要 analysis_date 之后有 window 天前瞻数据
    latest_analysis_date = today - timedelta(days=window + 5)  # 留余量
    earliest_analysis_date = today - timedelta(days=days_back)

    samples: List[SamplePoint] = []

    for code in codes:
        bars = stock_repo.get_range(code, earliest_analysis_date, today)
        if len(bars) < window + 20:
            logger.warning(f"{code}: 数据不足 ({len(bars)} 条)，跳过。需要补充数据后重试。")
            continue

        # 按 date 排序
        bars_sorted = sorted(bars, key=lambda b: b.date)
        dates_list = [b.date for b in bars_sorted]
        bars_by_date = {b.date: b for b in bars_sorted}

        count = 0
        # 从较早的日期开始，按 step 间隔采样
        idx = 20  # 至少需要 20 天历史来算技术指标
        while idx < len(dates_list) and count < max_samples_per_stock:
            analysis_dt = dates_list[idx]
            if analysis_dt > latest_analysis_date:
                break

            # 获取前瞻 bars
            forward_start_idx = idx + 1
            forward_bars = bars_sorted[forward_start_idx : forward_start_idx + window]
            if len(forward_bars) < window:
                idx += step
                continue

            start_bar = bars_by_date[analysis_dt]
            if start_bar.close and start_bar.close > 0:
                samples.append(SamplePoint(
                    code=code,
                    analysis_date=analysis_dt,
                    start_price=start_bar.close,
                    forward_bars=forward_bars,
                ))
                count += 1

            idx += step

    return samples


def _bars_to_dataframe(bars: List[StockDaily]) -> Any:
    """将 StockDaily 列表转为 pandas DataFrame，供 StockTrendAnalyzer 使用。"""
    import pandas as pd

    records = []
    for b in bars:
        records.append({
            "date": b.date,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "amount": b.amount,
            "pct_chg": b.pct_chg,
        })
    df = pd.DataFrame(records)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
    return df


def _get_history_up_to(
    stock_repo: StockRepository,
    code: str,
    analysis_date: date,
    lookback_days: int = 60,
) -> Tuple[Optional[Any], List[Dict[str, Any]]]:
    """获取 analysis_date 之前（含当天）的日线数据，返回 (DataFrame, bar_dicts)。"""
    start = analysis_date - timedelta(days=lookback_days + 30)  # 日历天多取一些
    bars = stock_repo.get_range(code, start, analysis_date)
    if not bars or len(bars) < 20:
        return None, []

    bars_sorted = sorted(bars, key=lambda b: b.date)
    df = _bars_to_dataframe(bars_sorted)

    bar_dicts = []
    for b in bars_sorted:
        bar_dicts.append({
            "date": str(b.date),
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        })

    return df, bar_dicts


# ─────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────

def run_strategy_backtest(args: argparse.Namespace) -> Dict[str, StrategyResult]:
    """执行策略历史回测。"""
    config = get_config()
    db = DatabaseManager.get_instance()
    stock_repo = StockRepository(db)
    trend_analyzer = StockTrendAnalyzer()

    # 加载策略
    skill_manager = SkillManager()
    skill_manager.load_builtin_skills()
    all_skills = skill_manager.list_skills()
    skill_map = {s.name: s for s in all_skills}

    # 确定要测试的策略
    if args.strategies:
        strategy_ids = [s.strip() for s in args.strategies.split(",")]
        missing = [s for s in strategy_ids if s not in skill_map]
        if missing:
            logger.error(f"未找到策略: {missing}")
            logger.info(f"可用策略: {sorted(skill_map.keys())}")
            sys.exit(1)
    else:
        strategy_ids = sorted(skill_map.keys())

    # 确定股票池
    if args.stocks:
        codes = [c.strip() for c in args.stocks.split(",")]
    else:
        codes = DEFAULT_STOCKS

    logger.info(f"策略数: {len(strategy_ids)}, 股票池: {codes}")
    logger.info(f"参数: days_back={args.days}, window={args.window}, step={args.step}, samples/stock={args.samples}")

    # 第一步：确保有足够的日线数据
    _ensure_daily_data(codes, args.days, stock_repo)

    # 第二步：生成采样点
    sample_points = _generate_sample_points(
        stock_repo=stock_repo,
        codes=codes,
        days_back=args.days,
        window=args.window,
        step=args.step,
        max_samples_per_stock=args.samples,
    )
    logger.info(f"共生成 {len(sample_points)} 个采样点")

    if not sample_points:
        logger.error("没有采样点可用。请检查股票池是否有足够的历史日线数据。")
        logger.info("提示：先运行 `python main.py --stocks 600519,000858` 积累日线数据，再运行回测。")
        sys.exit(1)

    if args.dry_run:
        logger.info("[DRY RUN] 采样点示例:")
        for sp in sample_points[:5]:
            logger.info(f"  {sp.code} @ {sp.analysis_date}, start_price={sp.start_price:.2f}")
        logger.info(f"  ... 共 {len(sample_points)} 个")
        logger.info("[DRY RUN] 完成。去掉 --dry-run 参数实际执行回测。")
        return None

    # 第三步：逐策略 × 逐采样点评估
    eval_config = EvaluationConfig(
        eval_window_days=args.window,
        neutral_band_pct=float(getattr(config, "backtest_neutral_band_pct", 2.0)),
        engine_version="strategy_bt_v1",
    )

    results: Dict[str, StrategyResult] = {}
    total_calls = len(strategy_ids) * len(sample_points)
    call_idx = 0

    for strategy_id in strategy_ids:
        skill = skill_map[strategy_id]
        sr = StrategyResult(strategy_name=strategy_id, display_name=skill.display_name)
        results[strategy_id] = sr

        logger.info(f"\n{'='*60}")
        logger.info(f"策略: {skill.display_name} ({strategy_id})")
        logger.info(f"{'='*60}")

        for sp in sample_points:
            call_idx += 1
            sr.total += 1

            # 获取分析日之前的历史数据
            df, bar_dicts = _get_history_up_to(stock_repo, sp.code, sp.analysis_date)
            if df is None or df.empty:
                sr.errors += 1
                logger.warning(f"  [{call_idx}/{total_calls}] {sp.code}@{sp.analysis_date}: 历史数据不足，跳过")
                continue

            # 技术指标分析
            try:
                trend_result = trend_analyzer.analyze(df, sp.code)
                trend_snapshot = {
                    "trend_status": trend_result.trend_status.value,
                    "ma_alignment": trend_result.ma_alignment,
                    "ma5": trend_result.ma5,
                    "ma10": trend_result.ma10,
                    "ma20": trend_result.ma20,
                    "current_price": trend_result.current_price,
                    "bias_ma5": round(trend_result.bias_ma5, 2),
                    "bias_ma10": round(trend_result.bias_ma10, 2),
                    "bias_ma20": round(trend_result.bias_ma20, 2),
                    "volume_status": trend_result.volume_status.value,
                    "volume_ratio_5d": round(trend_result.volume_ratio_5d, 2),
                    "volume_trend": trend_result.volume_trend,
                    "macd_status": trend_result.macd_status.value,
                    "macd_signal": trend_result.macd_signal,
                    "rsi_6": round(trend_result.rsi_6, 2),
                    "rsi_12": round(trend_result.rsi_12, 2),
                    "buy_signal": trend_result.buy_signal.value,
                    "signal_score": trend_result.signal_score,
                }
            except Exception as e:
                sr.errors += 1
                logger.warning(f"  [{call_idx}/{total_calls}] {sp.code}@{sp.analysis_date}: 技术分析失败: {e}")
                continue

            # 构造 prompt 并调用 LLM
            messages = _build_analysis_prompt(
                code=sp.code,
                analysis_date=sp.analysis_date,
                trend_snapshot=trend_snapshot,
                daily_bars=bar_dicts,
                skill_instructions=skill.instructions,
            )

            llm_result = _call_llm(messages, config)
            if llm_result is None:
                sr.errors += 1
                logger.warning(f"  [{call_idx}/{total_calls}] {sp.code}@{sp.analysis_date}: LLM 返回无效")
                continue

            operation_advice = llm_result.get("operation_advice", "观望")

            # 用 BacktestEngine 评估
            eval_result = BacktestEngine.evaluate_single(
                operation_advice=operation_advice,
                analysis_date=sp.analysis_date,
                start_price=sp.start_price,
                forward_bars=sp.forward_bars,
                stop_loss=None,
                take_profit=None,
                config=eval_config,
            )

            status = eval_result.get("eval_status", "error")
            if status == "insufficient_data":
                sr.insufficient += 1
            elif status == "error":
                sr.errors += 1
            else:
                outcome = eval_result.get("outcome")
                if outcome == "win":
                    sr.win += 1
                elif outcome == "loss":
                    sr.loss += 1
                else:
                    sr.neutral += 1

                if eval_result.get("direction_correct"):
                    sr.direction_correct += 1

                ret = eval_result.get("stock_return_pct")
                if ret is not None:
                    sr.sum_return_pct += ret

            sr.details.append({
                "code": sp.code,
                "date": str(sp.analysis_date),
                "advice": operation_advice,
                "outcome": eval_result.get("outcome"),
                "direction_correct": eval_result.get("direction_correct"),
                "return_pct": eval_result.get("stock_return_pct"),
            })

            # 进度提示
            outcome_str = eval_result.get("outcome", status)
            ret_str = f"{eval_result.get('stock_return_pct', 0):.2f}%" if eval_result.get("stock_return_pct") is not None else "N/A"
            logger.info(
                f"  [{call_idx}/{total_calls}] {sp.code}@{sp.analysis_date} "
                f"advice={operation_advice} outcome={outcome_str} return={ret_str}"
            )

            # 限速：避免 LLM API 限流
            time.sleep(args.delay)

    return results


def _ensure_daily_data(codes: List[str], days_back: int, stock_repo: StockRepository):
    """检查并补充日线数据。"""
    from data_provider import DataFetcherManager

    today = date.today()
    start = today - timedelta(days=days_back + 30)
    manager = DataFetcherManager()

    for code in codes:
        bars = stock_repo.get_range(code, start, today)
        if len(bars) >= days_back * 0.6:
            logger.info(f"{code}: 已有 {len(bars)} 条日线数据，足够。")
            continue

        logger.info(f"{code}: 日线数据不足 ({len(bars)} 条)，正在从数据源补充...")
        try:
            df, source = manager.get_daily_data(code, days=days_back)
            if df is not None and not df.empty:
                saved = stock_repo.save_dataframe(df, code, source)
                logger.info(f"{code}: 从 {source} 补充了 {saved} 条日线数据。")
            else:
                logger.warning(f"{code}: 无法获取日线数据。")
        except Exception as e:
            logger.warning(f"{code}: 补充数据失败: {e}")


def _print_summary(results: Dict[str, StrategyResult]):
    """打印汇总表格。"""
    print("\n" + "=" * 90)
    print(f"{'策略名称':<20} {'样本':<6} {'胜':<5} {'负':<5} {'中性':<5} "
          f"{'胜率':>8} {'方向准确率':>10} {'平均收益':>10} {'错误':<5}")
    print("-" * 90)

    for strategy_id in sorted(results.keys()):
        sr = results[strategy_id]
        wr = f"{sr.win_rate:.1f}%" if sr.win_rate is not None else "N/A"
        da = f"{sr.direction_accuracy:.1f}%" if sr.direction_accuracy is not None else "N/A"
        ar = f"{sr.avg_return:.2f}%" if sr.avg_return is not None else "N/A"

        print(
            f"{sr.display_name:<20} {sr.evaluated:<6} {sr.win:<5} {sr.loss:<5} {sr.neutral:<5} "
            f"{wr:>8} {da:>10} {ar:>10} {sr.errors:<5}"
        )

    print("=" * 90)

    # 按胜率排序
    ranked = sorted(
        results.values(),
        key=lambda x: x.win_rate if x.win_rate is not None else -1,
        reverse=True,
    )

    print("\n胜率排名:")
    for i, sr in enumerate(ranked, 1):
        wr = f"{sr.win_rate:.1f}%" if sr.win_rate is not None else "N/A"
        print(f"  {i}. {sr.display_name} ({sr.strategy_name}): {wr} ({sr.evaluated} 次评估)")


def main():
    parser = argparse.ArgumentParser(
        description="策略历史回测 — 用过去日线数据测试内置策略胜率"
    )
    parser.add_argument(
        "--stocks", type=str, default=None,
        help="逗号分隔的股票代码（默认使用内置股票池）"
    )
    parser.add_argument(
        "--strategies", type=str, default=None,
        help="逗号分隔的策略 ID（默认测试全部 11 种）"
    )
    parser.add_argument(
        "--days", type=int, default=180,
        help="向前回溯天数（日历天，默认 180）"
    )
    parser.add_argument(
        "--window", type=int, default=10,
        help="前瞻评估窗口（交易日数，默认 10）"
    )
    parser.add_argument(
        "--step", type=int, default=10,
        help="采样间隔（交易日数，默认 10）"
    )
    parser.add_argument(
        "--samples", type=int, default=15,
        help="每只股票最大采样数（默认 15）"
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="每次 LLM 调用间的延迟秒数（默认 1.0）"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅生成采样点，不调用 LLM"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="输出详细结果到 JSON 文件"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="输出 DEBUG 级别日志"
    )

    args = parser.parse_args()

    # 日志配置
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # 降低第三方库日志
    for noisy in ("httpx", "httpcore", "litellm", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger.info("策略历史回测启动")

    results = run_strategy_backtest(args)

    if results is None:
        return

    if not results:
        logger.error("未产生任何回测结果。")
        return

    _print_summary(results)

    # 输出 JSON
    if args.output:
        out_data = {}
        for sid, sr in results.items():
            out_data[sid] = {
                "display_name": sr.display_name,
                "total": sr.total,
                "evaluated": sr.evaluated,
                "win": sr.win,
                "loss": sr.loss,
                "neutral": sr.neutral,
                "win_rate": sr.win_rate,
                "direction_accuracy": sr.direction_accuracy,
                "avg_return": sr.avg_return,
                "errors": sr.errors,
                "details": sr.details,
            }
        output_path = Path(args.output)
        output_path.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"详细结果已保存到: {output_path}")


if __name__ == "__main__":
    main()
