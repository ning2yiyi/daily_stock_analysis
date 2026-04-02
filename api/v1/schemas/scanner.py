# -*- coding: utf-8 -*-
"""Scanner API schemas."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ScannerRunRequest(BaseModel):
    market: str = Field("us", description="market to scan: us / cn")
    bias_threshold: Optional[float] = Field(None, ge=1.0, le=20.0, description="乖离率上限(%)")
    gain_min: Optional[float] = Field(None, ge=0.0, description="近20日涨幅下限(%)")
    gain_max: Optional[float] = Field(None, ge=5.0, description="近20日涨幅上限(%)")
    volume_ratio_min: Optional[float] = Field(None, ge=0.5, description="量比下限")
    score_threshold: Optional[int] = Field(None, ge=10, le=100, description="进LLM的最低得分")
    top_quant: Optional[int] = Field(None, ge=5, le=100, description="量化初筛后进LLM的数量")
    top_final: Optional[int] = Field(None, ge=1, le=50, description="LLM精选最终输出数量")


class ScannerRunResponse(BaseModel):
    task_id: str = Field(..., description="异步任务ID")


class ScannerStatusResponse(BaseModel):
    task_id: str
    status: str = Field(..., description="pending/scanning/llm/done/error")
    progress: int = Field(0, description="已处理数量")
    total: int = Field(0, description="总数量")
    screened: int = Field(0, description="通过初筛数量")
    message: str = Field("", description="当前状态描述")
    error: Optional[str] = Field(None, description="错误信息")


class ScannerCandidateItem(BaseModel):
    code: str
    name: str
    market: str = "us"
    quant_score: float = Field(0, description="量化总分(0-100)")
    ma_score: float = 0
    bias_score: float = 0
    volume_score: float = 0
    gain_score: float = 0
    current_price: Optional[float] = None
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    bias_ma5: Optional[float] = None
    volume_ratio: Optional[float] = None
    gain_20d: Optional[float] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    llm_rank: Optional[int] = None
    llm_reason: Optional[str] = None
    llm_selected: bool = False
    confirmed: bool = False


class ScannerCandidatesResponse(BaseModel):
    task_id: Optional[str] = None
    candidates: List[ScannerCandidateItem] = Field(default_factory=list)
    total: int = 0


class ScannerConfirmRequest(BaseModel):
    task_id: str = Field(..., description="任务ID")
    codes: List[str] = Field(..., min_length=1, description="确认加入自选的股票代码列表")


class ScannerConfirmResponse(BaseModel):
    confirmed: int = Field(0, description="确认数量")
    added: List[str] = Field(default_factory=list, description="实际新增的股票代码")
    stock_list: List[str] = Field(default_factory=list, description="更新后的完整STOCK_LIST")
