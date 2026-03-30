# -*- coding: utf-8 -*-
"""Scanner repository.

Provides database access helpers for stock scanner candidate tables.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import and_, delete, desc, select, update

from src.storage import ScannerCandidate, DatabaseManager

logger = logging.getLogger(__name__)


class ScannerRepository:
    """DB access layer for stock scanner candidates."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def save_candidates_batch(self, candidates: List[ScannerCandidate]) -> int:
        if not candidates:
            return 0
        with self.db.get_session() as session:
            session.add_all(candidates)
            session.commit()
            return len(candidates)

    def get_candidates_by_task(
        self, task_id: str, *, llm_only: bool = False
    ) -> List[ScannerCandidate]:
        with self.db.get_session() as session:
            conditions = [ScannerCandidate.task_id == task_id]
            if llm_only:
                conditions.append(ScannerCandidate.llm_selected == True)  # noqa: E712
            query = (
                select(ScannerCandidate)
                .where(and_(*conditions))
                .order_by(desc(ScannerCandidate.quant_score))
            )
            rows = session.execute(query).scalars().all()
            return list(rows)

    def get_latest_candidates(self, *, llm_only: bool = True) -> List[ScannerCandidate]:
        with self.db.get_session() as session:
            latest_task = (
                select(ScannerCandidate.task_id)
                .order_by(desc(ScannerCandidate.created_at))
                .limit(1)
            )
            result = session.execute(latest_task).scalar_one_or_none()
            if not result:
                return []
            return self.get_candidates_by_task(result, llm_only=llm_only)

    def mark_confirmed(self, task_id: str, codes: List[str]) -> int:
        if not codes:
            return 0
        with self.db.get_session() as session:
            stmt = (
                update(ScannerCandidate)
                .where(
                    and_(
                        ScannerCandidate.task_id == task_id,
                        ScannerCandidate.code.in_(codes),
                    )
                )
                .values(confirmed=True)
            )
            result = session.execute(stmt)
            session.commit()
            return result.rowcount

    def delete_task(self, task_id: str) -> int:
        with self.db.get_session() as session:
            stmt = delete(ScannerCandidate).where(
                ScannerCandidate.task_id == task_id
            )
            result = session.execute(stmt)
            session.commit()
            return result.rowcount
