# -*- coding: utf-8 -*-
"""
===================================
股票扫描选股服务
===================================

职责：
1. 动态获取美股/A股全量股票池
2. 分批量化初筛（MA多头/乖离率/量比/涨幅）
3. 调用 LLM 精选 Top N
4. 持久化候选结果
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.config import get_config, Config
from src.repositories.scanner_repo import ScannerRepository
from src.storage import DatabaseManager, ScannerCandidate
from data_provider.base import DataFetcherManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class QuantScoreCard:
    """量化初筛得分卡"""
    code: str
    name: str
    market: str = "us"

    # 分项得分 (满分 100)
    ma_score: float = 0.0       # 30
    bias_score: float = 0.0     # 25
    volume_score: float = 0.0   # 25
    gain_score: float = 0.0     # 20
    total_score: float = 0.0

    # 关键指标
    current_price: float = 0.0
    ma5: float = 0.0
    ma10: float = 0.0
    ma20: float = 0.0
    bias_ma5: float = 0.0
    volume_ratio: float = 0.0
    gain_20d: float = 0.0
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None


@dataclass
class ScanTask:
    """扫描任务状态"""
    task_id: str
    market: str
    status: str = "pending"     # pending / scanning / llm / done / error
    progress: int = 0
    total: int = 0
    screened: int = 0
    message: str = ""
    candidates: List[QuantScoreCard] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Universe providers
# ---------------------------------------------------------------------------

def fetch_us_universe() -> List[Tuple[str, str]]:
    """
    从网络动态获取标普500 + 纳斯达克100成分股列表。

    Returns:
        [(ticker, name), ...] 去重后约 550 只
    """
    seen = set()
    result: List[Tuple[str, str]] = []

    # S&P 500 via yfinance/wikipedia
    try:
        import yfinance as yf
        sp500 = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"},
        )[0]
        for _, row in sp500.iterrows():
            ticker = str(row.get("Symbol", "")).strip().replace(".", "-")
            name = str(row.get("Security", ticker)).strip()
            if ticker and ticker not in seen:
                seen.add(ticker)
                result.append((ticker, name))
        logger.info("S&P 500: fetched %d tickers from Wikipedia", len(result))
    except Exception as exc:
        logger.warning("Failed to fetch S&P 500 list: %s", exc)

    # Nasdaq-100
    try:
        ndx = pd.read_html(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            attrs={"id": "constituents"},
        )[0]
        added = 0
        for _, row in ndx.iterrows():
            ticker = str(row.get("Ticker", "")).strip().replace(".", "-")
            name = str(row.get("Company", ticker)).strip()
            if ticker and ticker not in seen:
                seen.add(ticker)
                result.append((ticker, name))
                added += 1
        logger.info("Nasdaq-100: added %d new tickers", added)
    except Exception as exc:
        logger.warning("Failed to fetch Nasdaq-100 list: %s", exc)

    if not result:
        raise RuntimeError("Failed to fetch any US stock universe; check network")

    return result


# ---------------------------------------------------------------------------
# Scanner Service
# ---------------------------------------------------------------------------

class StockScannerService:
    """股票扫描选股服务"""

    _tasks: Dict[str, ScanTask] = {}
    _tasks_lock = threading.Lock()

    def __init__(
        self,
        config: Optional[Config] = None,
        db_manager: Optional[DatabaseManager] = None,
    ):
        self.config = config or get_config()
        self.repo = ScannerRepository(db_manager)
        self.fetcher_manager = DataFetcherManager()

    # ----- task management -----

    def _set_task(self, task: ScanTask) -> None:
        with self._tasks_lock:
            self._tasks[task.task_id] = task

    def get_task(self, task_id: str) -> Optional[ScanTask]:
        with self._tasks_lock:
            return self._tasks.get(task_id)

    # ----- public API -----

    def start_scan(
        self,
        market: str = "us",
        *,
        bias_threshold: Optional[float] = None,
        gain_min: Optional[float] = None,
        gain_max: Optional[float] = None,
        volume_ratio_min: Optional[float] = None,
        score_threshold: Optional[int] = None,
        top_quant: Optional[int] = None,
        top_final: Optional[int] = None,
    ) -> str:
        """
        启动异步扫描任务。

        Returns:
            task_id
        """
        task_id = f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        task = ScanTask(task_id=task_id, market=market, status="pending")
        self._set_task(task)

        params = {
            "bias_threshold": bias_threshold or self.config.scan_bias_threshold,
            "gain_min": gain_min or self.config.scan_gain_min,
            "gain_max": gain_max or self.config.scan_gain_max,
            "volume_ratio_min": volume_ratio_min or self.config.scan_volume_ratio_min,
            "score_threshold": score_threshold or self.config.scan_score_threshold,
            "top_quant": top_quant or self.config.scan_top_quant,
            "top_final": top_final or self.config.scan_top_final,
        }

        thread = threading.Thread(
            target=self._run_scan, args=(task_id, market, params), daemon=True
        )
        thread.start()
        return task_id

    def get_candidates(self, task_id: Optional[str] = None, *, llm_only: bool = True):
        if task_id:
            return self.repo.get_candidates_by_task(task_id, llm_only=llm_only)
        return self.repo.get_latest_candidates(llm_only=llm_only)

    def confirm_candidates(self, task_id: str, codes: List[str]) -> Dict[str, Any]:
        """
        用户确认选股结果，追加写入 STOCK_LIST。
        """
        from src.core.config_manager import ConfigManager

        count = self.repo.mark_confirmed(task_id, codes)
        mgr = ConfigManager()
        current = self.config.stock_list[:]
        added = []
        for code in codes:
            if code not in current:
                current.append(code)
                added.append(code)
        if added:
            mgr.update({"STOCK_LIST": ",".join(current)})
            self.config.refresh_stock_list()
        return {"confirmed": count, "added": added, "stock_list": self.config.stock_list[:]}

    # ----- internal pipeline -----

    def _run_scan(self, task_id: str, market: str, params: Dict[str, Any]) -> None:
        task = self.get_task(task_id)
        if not task:
            return
        try:
            # Phase 1: get universe
            task.status = "scanning"
            task.message = "正在获取股票池..."
            self._set_task(task)

            if market == "us":
                universe = fetch_us_universe()
            else:
                raise ValueError(f"Market '{market}' not yet supported")

            task.total = len(universe)
            task.message = f"股票池 {task.total} 只，开始量化初筛..."
            self._set_task(task)

            # Phase 2: quantitative screening in batches
            batch_size = self.config.scan_batch_size
            batch_interval = self.config.scan_batch_interval
            history_days = self.config.scan_history_days
            all_cards: List[QuantScoreCard] = []

            for i in range(0, len(universe), batch_size):
                batch = universe[i : i + batch_size]
                for ticker, name in batch:
                    try:
                        card = self._score_one(ticker, name, market, history_days, params)
                        if card and card.total_score >= params["score_threshold"]:
                            all_cards.append(card)
                    except Exception as exc:
                        logger.debug("Score %s failed: %s", ticker, exc)
                    task.progress = min(i + batch_size, len(universe))
                    self._set_task(task)

                if i + batch_size < len(universe):
                    time.sleep(batch_interval)

            task.screened = len(all_cards)

            # Sort and keep top_quant
            all_cards.sort(key=lambda c: c.total_score, reverse=True)
            top_cards = all_cards[: params["top_quant"]]
            task.candidates = top_cards
            task.message = f"量化初筛完成，{task.screened} 只达标，取 Top {len(top_cards)} 进入 LLM 精选"
            self._set_task(task)

            # Phase 3: persist quant results
            db_candidates = [self._card_to_model(task_id, c) for c in top_cards]
            self.repo.save_candidates_batch(db_candidates)

            # Phase 4: LLM refinement
            task.status = "llm"
            task.message = "LLM 精选中..."
            self._set_task(task)

            try:
                self._llm_refine(task_id, top_cards, params["top_final"])
            except Exception as exc:
                logger.warning("LLM refinement failed, using quant-only results: %s", exc)
                # Fallback: mark top N by quant score as selected
                top_codes = [c.code for c in top_cards[: params["top_final"]]]
                self._fallback_select(task_id, top_codes)

            task.status = "done"
            task.message = "选股完成"
            self._set_task(task)

        except Exception as exc:
            logger.error("Scan task %s failed: %s", task_id, exc, exc_info=True)
            task.status = "error"
            task.error = str(exc)
            task.message = f"扫描失败: {exc}"
            self._set_task(task)

    def _score_one(
        self,
        code: str,
        name: str,
        market: str,
        history_days: int,
        params: Dict[str, Any],
    ) -> Optional[QuantScoreCard]:
        """计算单只股票的量化得分"""
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=history_days + 30)).strftime("%Y%m%d")

        df = self.fetcher_manager.get_stock_data(code, start_date, end_date)
        if df is None or len(df) < 25:
            return None

        df = df.sort_values("date").reset_index(drop=True)
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)

        # MA
        ma5 = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()

        latest = close.iloc[-1]
        latest_ma5 = ma5.iloc[-1]
        latest_ma10 = ma10.iloc[-1]
        latest_ma20 = ma20.iloc[-1]

        if any(math.isnan(v) for v in [latest_ma5, latest_ma10, latest_ma20]):
            return None

        # --- MA score (30) ---
        ma_score = 0.0
        if latest_ma5 > latest_ma10 > latest_ma20:
            ma_score = 30.0
        elif latest_ma5 > latest_ma10:
            ma_score = 15.0

        # --- Bias score (25) ---
        bias_ma5 = (latest - latest_ma5) / latest_ma5 * 100 if latest_ma5 else 0
        bias_threshold = params["bias_threshold"]
        if abs(bias_ma5) < 2:
            bias_score = 25.0
        elif abs(bias_ma5) < bias_threshold:
            bias_score = 15.0
        else:
            bias_score = 0.0

        # --- Volume score (25) ---
        vol_5 = volume.iloc[-5:].mean()
        vol_20 = volume.iloc[-20:].mean()
        volume_ratio = vol_5 / vol_20 if vol_20 > 0 else 0

        if volume_ratio > 1.5:
            volume_score = 25.0
        elif volume_ratio > params["volume_ratio_min"]:
            volume_score = 15.0
        else:
            volume_score = 0.0

        # --- Gain score (20) ---
        if len(close) >= 20:
            price_20d_ago = close.iloc[-20]
            gain_20d = (latest - price_20d_ago) / price_20d_ago * 100 if price_20d_ago > 0 else 0
        else:
            gain_20d = 0

        gain_min = params["gain_min"]
        gain_max = params["gain_max"]
        if gain_min <= gain_20d <= gain_max:
            gain_score = 20.0
        elif 0 <= gain_20d < gain_min:
            gain_score = 10.0
        else:
            gain_score = 0.0

        total = ma_score + bias_score + volume_score + gain_score

        card = QuantScoreCard(
            code=code,
            name=name,
            market=market,
            ma_score=ma_score,
            bias_score=bias_score,
            volume_score=volume_score,
            gain_score=gain_score,
            total_score=total,
            current_price=round(latest, 2),
            ma5=round(latest_ma5, 2),
            ma10=round(latest_ma10, 2),
            ma20=round(latest_ma20, 2),
            bias_ma5=round(bias_ma5, 2),
            volume_ratio=round(volume_ratio, 2),
            gain_20d=round(gain_20d, 2),
        )
        return card

    def _card_to_model(self, task_id: str, card: QuantScoreCard) -> ScannerCandidate:
        return ScannerCandidate(
            task_id=task_id,
            code=card.code,
            name=card.name,
            market=card.market,
            quant_score=card.total_score,
            ma_score=card.ma_score,
            bias_score=card.bias_score,
            volume_score=card.volume_score,
            gain_score=card.gain_score,
            current_price=card.current_price,
            ma5=card.ma5,
            ma10=card.ma10,
            ma20=card.ma20,
            bias_ma5=card.bias_ma5,
            volume_ratio=card.volume_ratio,
            gain_20d=card.gain_20d,
            pe_ratio=card.pe_ratio,
            pb_ratio=card.pb_ratio,
        )

    # ----- LLM refinement -----

    def _llm_refine(
        self, task_id: str, cards: List[QuantScoreCard], top_final: int
    ) -> None:
        """
        调用 LLM 对量化初筛结果进行精选排序。
        """
        from src.analyzer import GeminiAnalyzer
        from data_provider.fundamental_adapter import AkshareFundamentalAdapter

        # Build summary for LLM
        stock_summaries = []
        for i, card in enumerate(cards, 1):
            summary = (
                f"{i}. {card.code} ({card.name}) | "
                f"评分 {card.total_score:.0f} | "
                f"价格 ${card.current_price} | "
                f"MA5={card.ma5} MA10={card.ma10} MA20={card.ma20} | "
                f"乖离率 {card.bias_ma5:.1f}% | "
                f"量比 {card.volume_ratio:.1f} | "
                f"20日涨幅 {card.gain_20d:.1f}%"
            )
            # Try fetching PE/PB
            try:
                import yfinance as yf
                info = yf.Ticker(card.code).info
                pe = info.get("trailingPE")
                pb = info.get("priceToBook")
                mcap = info.get("marketCap")
                if pe:
                    summary += f" | PE={pe:.1f}"
                    card.pe_ratio = round(pe, 2)
                if pb:
                    summary += f" | PB={pb:.1f}"
                    card.pb_ratio = round(pb, 2)
                if mcap:
                    summary += f" | 市值=${mcap / 1e9:.1f}B"
            except Exception:
                pass
            stock_summaries.append(summary)

        prompt = f"""你是一位专业的美股投资分析师。以下是通过量化初筛选出的 {len(cards)} 只候选股票：

{chr(10).join(stock_summaries)}

请从中精选出最值得关注的 {top_final} 只，按推荐优先级排序。

评估标准：
1. 趋势健康度：多头排列稳固，不是已进入加速赶顶阶段
2. 位置合理性：不追高，距离均线位置合理
3. 量价配合度：放量上涨/缩量回调优于缩量上涨
4. 基本面支撑：PE/PB合理，非纯炒作标的
5. 涨幅空间：近期涨幅不宜过大，避免高位接盘

请严格按以下 JSON 格式返回，不要包含其他内容：
```json
[
  {{"code": "AAPL", "rank": 1, "reason": "推荐理由（30字以内）"}},
  ...
]
```"""

        analyzer = GeminiAnalyzer(config=self.config)
        response_text = analyzer.generate_text(prompt, max_tokens=4096, temperature=0.3)

        if not response_text:
            logger.warning("LLM returned empty response, falling back")
            top_codes = [c.code for c in cards[:top_final]]
            self._fallback_select(task_id, top_codes)
            return

        # Parse JSON from response
        selections = self._parse_llm_selections(response_text)

        if not selections:
            logger.warning("LLM returned no valid selections, falling back")
            top_codes = [c.code for c in cards[:top_final]]
            self._fallback_select(task_id, top_codes)
            return

        # Update database
        with self.repo.db.get_session() as session:
            for sel in selections[:top_final]:
                code = sel.get("code", "")
                rank = sel.get("rank", 0)
                reason = sel.get("reason", "")
                from sqlalchemy import update as sa_update
                session.execute(
                    sa_update(ScannerCandidate)
                    .where(
                        ScannerCandidate.task_id == task_id,
                        ScannerCandidate.code == code,
                    )
                    .values(llm_rank=rank, llm_reason=reason, llm_selected=True)
                )
            session.commit()

    def _parse_llm_selections(self, text: str) -> List[Dict[str, Any]]:
        """从 LLM 响应中解析 JSON 数组"""
        try:
            # Try to find JSON array in response
            start = text.find("[")
            end = text.rfind("]")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse LLM selection JSON: %s", exc)
        return []

    def _fallback_select(self, task_id: str, codes: List[str]) -> None:
        """Fallback: 按量化得分直接选择 Top N"""
        with self.repo.db.get_session() as session:
            for i, code in enumerate(codes, 1):
                from sqlalchemy import update as sa_update
                session.execute(
                    sa_update(ScannerCandidate)
                    .where(
                        ScannerCandidate.task_id == task_id,
                        ScannerCandidate.code == code,
                    )
                    .values(
                        llm_rank=i,
                        llm_reason="量化得分排序（LLM不可用）",
                        llm_selected=True,
                    )
                )
            session.commit()
