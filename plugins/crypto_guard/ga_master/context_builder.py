from __future__ import annotations

from typing import Any

from plugins.crypto_guard.ga_master.decision_schema import GAAnalysisRequest
from plugins.crypto_guard.reasoning.market_state_builder import build_market_state_snapshot
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.utils import latest_closed_close_time_ms, utc_ms


class ContextBuilder:
    def __init__(self, repo: CryptoGuardRepository):
        self.repo = repo

    def build(self, request: GAAnalysisRequest) -> dict[str, Any]:
        analysis_time = int(request.analysis_time_utc or latest_closed_close_time_ms("15m", utc_ms()))
        snapshot = request.snapshot
        snapshot_id = request.snapshot_id
        if snapshot is None:
            snapshot = build_market_state_snapshot(
                self.repo,
                symbol=request.symbol,
                analysis_time_utc=analysis_time,
                mode=request.mode,
                timeframes=request.timeframes,
            )
            snapshot_id = self.repo.save_market_snapshot(snapshot)
        else:
            analysis_time = int(snapshot.get("analysis_time_utc") or analysis_time)
        symbol = str(snapshot.get("symbol") or request.symbol)
        return {
            "request": request,
            "symbol": symbol,
            "analysis_time_utc": analysis_time,
            "decision_type": request.decision_type,
            "snapshot": snapshot,
            "snapshot_id": snapshot_id,
            "previous_analysis_state": self.repo.latest_analysis_state(symbol),
            "active_opportunity_watches": self.repo.list_active_opportunity_watches_for_symbol(symbol),
            "open_paper_orders": self.repo.list_open_paper_orders_for_symbol(symbol),
            "skill_feedback_memory": self._skill_feedback_memory(),
        }

    def _skill_feedback_memory(self) -> list[dict[str, Any]]:
        rows = self.repo.conn.execute(
            """
            SELECT * FROM skill_feedback_memory
            WHERE status IN ('candidate', 'active')
            ORDER BY updated_at DESC, id DESC
            LIMIT 50
            """
        ).fetchall()
        return [dict(r) for r in rows]
