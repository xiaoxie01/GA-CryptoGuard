from __future__ import annotations

from typing import Any

from plugins.crypto_guard.ga_master.decision_schema import GAAnalysisRequest
from plugins.crypto_guard.reasoning.context_envelope import build_context_envelope
from plugins.crypto_guard.reasoning.market_state_builder import build_market_state_snapshot
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.utils import latest_closed_close_time_ms, utc_ms


def _primary_timeframe(request: GAAnalysisRequest, snapshot: dict[str, Any]) -> str:
    """Pick a single timeframe for the context envelope from the request's
    timeframe list, else the snapshot's timeframe_context, else 15m."""
    tfs = request.timeframes
    if tfs and isinstance(tfs, list) and tfs:
        return str(tfs[0])
    ctx = snapshot.get("timeframe_context") or {}
    if isinstance(ctx, dict) and ctx:
        return str(next(iter(ctx)))
    return "15m"


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
        previous_state = self.repo.latest_analysis_state(symbol)
        watches = self.repo.list_active_opportunity_watches_for_symbol(symbol)
        orders = self.repo.list_open_paper_orders_for_symbol(symbol)
        memory = self._skill_feedback_memory(symbol)
        # 08-04 contract C1/F2 (fresh reviewer P1): the versioned context
        # envelope is a PRODUCTION wiring point — attach it to the in-memory
        # snapshot (NOT persisted; the envelope is decision-time context) so
        # the LLM judge and the D6 evidence-grounding gate
        # (``_build_allowed_evidence_ids``) see provenance-tagged evidence with
        # real ``evidence_id`` values instead of an empty ``context_envelope``.
        snapshot["context_envelope"] = build_context_envelope(
            repo=self.repo,
            symbol=symbol,
            timeframe=_primary_timeframe(request, snapshot),
            analysis_time_utc=analysis_time,
            snapshot=snapshot,
            previous_state=previous_state,
            watches=watches,
            orders=orders,
            memory=memory,
        )
        return {
            "request": request,
            "symbol": symbol,
            "analysis_time_utc": analysis_time,
            "decision_type": request.decision_type,
            "snapshot": snapshot,
            "snapshot_id": snapshot_id,
            "previous_analysis_state": previous_state,
            "active_opportunity_watches": watches,
            "open_paper_orders": orders,
            "skill_feedback_memory": memory,
        }

    def _skill_feedback_memory(self, symbol: str) -> list[dict[str, Any]]:
        """08-04 contract C5: symbol/status/recency-filtered memory, no global-50."""
        return self.repo.get_skill_feedback_memory(symbol)
