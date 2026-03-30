# -*- coding: utf-8 -*-
"""Scanner endpoints — stock screening and selection."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_database_manager
from api.v1.schemas.scanner import (
    ScannerRunRequest,
    ScannerRunResponse,
    ScannerStatusResponse,
    ScannerCandidateItem,
    ScannerCandidatesResponse,
    ScannerConfirmRequest,
    ScannerConfirmResponse,
)
from api.v1.schemas.common import ErrorResponse
from src.services.stock_scanner_service import StockScannerService
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

router = APIRouter()

# Singleton service (lazily created)
_scanner_service: StockScannerService | None = None


def _get_scanner(db_manager: DatabaseManager = Depends(get_database_manager)) -> StockScannerService:
    global _scanner_service
    if _scanner_service is None:
        _scanner_service = StockScannerService(db_manager=db_manager)
    return _scanner_service


@router.post(
    "/run",
    response_model=ScannerRunResponse,
    responses={
        200: {"description": "扫描任务已启动"},
        400: {"description": "参数错误", "model": ErrorResponse},
    },
    summary="启动选股扫描",
    description="启动异步选股扫描任务，量化初筛后调用LLM精选",
)
def run_scan(
    request: ScannerRunRequest,
    scanner: StockScannerService = Depends(_get_scanner),
) -> ScannerRunResponse:
    if request.market not in ("us",):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_market", "message": f"Market '{request.market}' not yet supported. Use 'us'."},
        )
    task_id = scanner.start_scan(
        market=request.market,
        bias_threshold=request.bias_threshold,
        gain_min=request.gain_min,
        gain_max=request.gain_max,
        volume_ratio_min=request.volume_ratio_min,
        score_threshold=request.score_threshold,
        top_quant=request.top_quant,
        top_final=request.top_final,
    )
    return ScannerRunResponse(task_id=task_id)


@router.get(
    "/status/{task_id}",
    response_model=ScannerStatusResponse,
    responses={
        200: {"description": "任务状态"},
        404: {"description": "任务不存在", "model": ErrorResponse},
    },
    summary="查询扫描任务状态",
)
def get_scan_status(
    task_id: str,
    scanner: StockScannerService = Depends(_get_scanner),
) -> ScannerStatusResponse:
    task = scanner.get_task(task_id)
    if not task:
        raise HTTPException(
            status_code=404,
            detail={"error": "task_not_found", "message": f"Task '{task_id}' not found"},
        )
    return ScannerStatusResponse(
        task_id=task.task_id,
        status=task.status,
        progress=task.progress,
        total=task.total,
        screened=task.screened,
        message=task.message,
        error=task.error,
    )


@router.get(
    "/candidates",
    response_model=ScannerCandidatesResponse,
    summary="获取选股候选结果",
    description="获取指定任务或最近一次扫描的候选结果",
)
def get_candidates(
    task_id: str | None = None,
    llm_only: bool = True,
    scanner: StockScannerService = Depends(_get_scanner),
) -> ScannerCandidatesResponse:
    rows = scanner.get_candidates(task_id, llm_only=llm_only)
    items = [
        ScannerCandidateItem(
            code=r.code,
            name=r.name,
            market=r.market,
            quant_score=r.quant_score,
            ma_score=r.ma_score or 0,
            bias_score=r.bias_score or 0,
            volume_score=r.volume_score or 0,
            gain_score=r.gain_score or 0,
            current_price=r.current_price,
            ma5=r.ma5,
            ma10=r.ma10,
            ma20=r.ma20,
            bias_ma5=r.bias_ma5,
            volume_ratio=r.volume_ratio,
            gain_20d=r.gain_20d,
            pe_ratio=r.pe_ratio,
            pb_ratio=r.pb_ratio,
            llm_rank=r.llm_rank,
            llm_reason=r.llm_reason,
            llm_selected=r.llm_selected or False,
            confirmed=r.confirmed or False,
        )
        for r in rows
    ]
    actual_task_id = rows[0].task_id if rows else task_id
    return ScannerCandidatesResponse(
        task_id=actual_task_id,
        candidates=items,
        total=len(items),
    )


@router.post(
    "/confirm",
    response_model=ScannerConfirmResponse,
    responses={
        200: {"description": "确认成功"},
        400: {"description": "参数错误", "model": ErrorResponse},
    },
    summary="确认选股并写入自选",
    description="用户确认候选池中的股票，追加到 STOCK_LIST",
)
def confirm_candidates(
    request: ScannerConfirmRequest,
    scanner: StockScannerService = Depends(_get_scanner),
) -> ScannerConfirmResponse:
    result = scanner.confirm_candidates(request.task_id, request.codes)
    return ScannerConfirmResponse(**result)
