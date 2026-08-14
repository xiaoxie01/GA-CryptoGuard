"""08-10 LLM risk-governance diagnostics (design.md §12, prd.md P2-2).

Seven detection checks over the risk-advisory envelopes persisted inside
``ga_decisions.raw_decision_json`` (``entry_confirmation_lifecycle`` /
``llm_risk_proposal`` / ``risk_adjustment_verification`` / ``risk_advisory``),
plus the fail-closed marker-missing checks for the four 08-10 contract
markers:

  - ``entry_confirmation_lifecycle_contract_v1``     (lifecycle)
  - ``llm_risk_proposal_contract_v1``                (proposal)
  - ``risk_adjustment_verifier_contract_v1``         (verifier)
  - ``llm_risk_context_isolation_contract_v1``       (context)

Marker gating (fail closed, mirrors the 08-02 execution-funnel pattern):

  - A MISSING marker emits ``{name}_contract_marker_missing`` (error,
    details.issue="marker_absent"); an unparseable ``applied_at`` emits the
    same code with details.issue="marker_corrupt". Never silent green.
  - Every detection check runs ONLY when ITS OWN marker is present AND
    parseable; the marker's ``applied_at`` is the SQL lower bound
    ``created_at >= %s::timestamptz`` (exclude-only — pre-marker rows never
    enter current issues). Absent/corrupt marker -> the gated check SELF-SKIPS
    (an undeployed contract must not be evaluated as current).

Detection checks (all ``error`` unless noted; per-check marker in brackets):

  - ``carried_confirmation_without_provenance`` [lifecycle] — lifecycle
    origin ``carried_forward`` but no ``source_decision_id``.
  - ``confirmation_survived_expiry`` [lifecycle] — lifecycle status ``valid``
    with ``age_bars > ttl_bars``.
  - ``llm_proposal_immutable_change`` [proposal] — proposal ``adjustments``
    carry an immutable key (symbol/side/candidate_fingerprint/
    entry_trigger_confirmation/order_id/database_action/notification_action/
    ttl_bars/quantity/leverage/risk_check).
  - ``llm_proposal_unknown_evidence`` [proposal] — ``evidence_refs`` not
    contained in the decision's stable ``evidence_ids`` set (empty stable set
    with refs FAILS CLOSED to unknown).
  - ``accepted_adjustment_increases_monetary_risk`` [verifier] — verification
    ``accepted`` with ``monetary_risk_delta > 0``.
  - ``order_without_final_verifier_success`` [verifier] — a paper order whose
    linked decision carries a ``risk_advisory`` envelope in shadow /
    paper_bounded but NOT (``verification_ok`` AND ``final_risk_check_ok``);
    the legacy ``off`` path (and decisions with no advisory envelope) are out
    of scope. 08-12 P2-2: also fires when the ORDER-side
    ``paper_orders.risk_advisory_mode`` says governance ran but the decision
    lost the audit row (persist-loss false negative).
  - ``llm_risk_review_starvation`` (warning) [proposal] — >= 3 system
    ``failed`` proposals in the window; legitimate ``reject``/``wait``
    verdicts never count.

Every issue mirrors ``report_diagnostics._issue`` shape
``{type, severity, layer, details, suggested_action}`` so the existing
aggregator can consume this module unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from plugins.crypto_guard.diagnostics.report_diagnostics import _issue
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

# The four 08-10 contract markers (verbatim from design.md §12). ``name`` is
# the suffix used for the fail-closed ``{name}_contract_marker_missing`` code;
# ``key`` is the ``_migration_state`` row written by initialize_database.
MARKERS: dict[str, str] = {
    "lifecycle": "entry_confirmation_lifecycle_contract_v1",
    "proposal": "llm_risk_proposal_contract_v1",
    "verifier": "risk_adjustment_verifier_contract_v1",
    "context": "llm_risk_context_isolation_contract_v1",
}

# Keys a risk-adjustment proposal must NEVER mutate (compiled from design §12).
# Any of these inside ``adjustments`` is a contract breach regardless of the
# verifier's verdict.
IMMUTABLE_KEYS: set[str] = {
    "symbol", "side", "candidate_fingerprint", "entry_trigger_confirmation",
    "order_id", "database_action", "notification_action", "ttl_bars",
    "quantity", "leverage", "risk_check",
}

CARRIED_CONFIRMATION_WITHOUT_PROVENANCE = "carried_confirmation_without_provenance"
CONFIRMATION_SURVIVED_EXPIRY = "confirmation_survived_expiry"
LLM_PROPOSAL_IMMUTABLE_CHANGE = "llm_proposal_immutable_change"
LLM_PROPOSAL_UNKNOWN_EVIDENCE = "llm_proposal_unknown_evidence"
ACCEPTED_ADJUSTMENT_INCREASES_MONETARY_RISK = "accepted_adjustment_increases_monetary_risk"
ORDER_WITHOUT_FINAL_VERIFIER_SUCCESS = "order_without_final_verifier_success"
LLM_RISK_REVIEW_STARVATION = "llm_risk_review_starvation"

# System-failure threshold for ``llm_risk_review_starvation`` (design §12).
_LLM_RISK_REVIEW_STARVATION_THRESHOLD = 3


def _safe_json(raw: Any) -> Any:
    """JSONB pass-through: psycopg3 decodes JSONB columns to dict/list already
    (``json.loads(dict)`` would raise TypeError). Accept dict/list as-is; only
    parse str. Mirrors report_diagnostics._safe_json / state_consistency."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


def _parse_marker_applied_at(value: Any) -> datetime | None:
    """Parse a marker ``applied_at`` into an aware ``datetime``.

    Returns ``None`` on a missing value or ANY parse failure (fail closed —
    a corrupt marker means nothing is provably post-marker, mirroring the
    08-08 ``marker_corrupt`` defensive branch).
    """
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError, OSError):
        return None


def _marker_ts(repo: CryptoGuardRepository, key: str) -> datetime | None:
    """The marker's parsed ``applied_at``, or None when absent/corrupt."""
    row = repo.conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key=%s", (key,),
    ).fetchone()
    return _parse_marker_applied_at(row["applied_at"] if row else None)


def _check_llm_risk_contract_marker_missing(
    repo: CryptoGuardRepository, name: str, key: str,
) -> list[dict[str, Any]]:
    """Fail-closed marker-missing check for ONE 08-10 marker.

    Runs for every marker so a missing contract is explicitly surfaced even
    when the other checks would pass (or skip). A PRESENT but CORRUPT marker
    is surfaced too (``issue=marker_corrupt``) — never silent green.
    """
    issues: list[dict[str, Any]] = []
    row = repo.conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key=%s", (key,),
    ).fetchone()
    if not row or not row["applied_at"]:
        issues.append(_issue(
            f"{name}_contract_marker_missing", "error",
            {
                "marker_key": key,
                "contract": name,
                "issue": "marker_absent",
            },
            f"{name} 契约 marker 未部署。运行 initialize_database() 写入 {key}；"
            "marker 缺失时该契约的检测项被 SKIP（未部署契约不得评估为当前错误），"
            "避免历史行重复成当前错误。",
        ))
        return issues
    if _parse_marker_applied_at(row["applied_at"]) is None:
        issues.append(_issue(
            f"{name}_contract_marker_missing", "error",
            {
                "marker_key": key,
                "contract": name,
                "issue": "marker_corrupt",
                "applied_at": str(row["applied_at"]),
            },
            f"{name} 契约 marker 值损坏（不可解析为时间戳）。运行 "
            "initialize_database() 重写正确的 applied_at；损坏 marker 下该契约"
            "的检测项 lower bound 均 fail-closed（不评估任何行，无静默 fail-open）。",
        ))
    return issues


def _window_rows(repo: CryptoGuardRepository, marker_ts: datetime | None) -> list[Any]:
    """Post-marker decision rows (``created_at >= marker.applied_at``), or []
    when the marker is absent/corrupt (SELF-SKIP — no partial window)."""
    if marker_ts is None:
        return []
    return repo.conn.execute(
        "SELECT id, symbol, created_at, raw_decision_json "
        "FROM ga_decisions "
        "WHERE created_at >= %s::timestamptz AND raw_decision_json IS NOT NULL",
        (marker_ts,),
    ).fetchall()


def _check_carried_confirmation_without_provenance(
    repo: CryptoGuardRepository, marker_ts: datetime | None,
) -> list[dict[str, Any]]:
    """[lifecycle] A carried-forward confirmation must prove its source."""
    if marker_ts is None:
        return []
    issues: list[dict[str, Any]] = []
    for row in _window_rows(repo, marker_ts):
        raw = _safe_json(row["raw_decision_json"])
        if not isinstance(raw, dict):
            continue
        lifecycle = raw.get("entry_confirmation_lifecycle")
        if not isinstance(lifecycle, dict):
            continue
        if (lifecycle.get("origin") == "carried_forward"
                and not lifecycle.get("source_decision_id")):
            issues.append(_issue(
                CARRIED_CONFIRMATION_WITHOUT_PROVENANCE, "error",
                {
                    "decision_id": row["id"],
                    "symbol": row["symbol"],
                    "source_decision_id": lifecycle.get("source_decision_id"),
                },
                "carried_forward 入场确认缺少 source_decision_id（来源决策）。"
                "延续的确认必须指向其来源决策以便审计；缺失来源说明生命周期"
                "解析器未记录来源。检查确认事件落库与生命周期解析链路。",
            ))
    return issues


def _check_confirmation_survived_expiry(
    repo: CryptoGuardRepository, marker_ts: datetime | None,
) -> list[dict[str, Any]]:
    """[lifecycle] A ``valid`` confirmation with ``age_bars > ttl_bars``."""
    if marker_ts is None:
        return []
    issues: list[dict[str, Any]] = []
    for row in _window_rows(repo, marker_ts):
        raw = _safe_json(row["raw_decision_json"])
        if not isinstance(raw, dict):
            continue
        lifecycle = raw.get("entry_confirmation_lifecycle")
        if not isinstance(lifecycle, dict):
            continue
        if lifecycle.get("status") != "valid":
            continue
        age_bars = lifecycle.get("age_bars") or 0
        ttl_bars = lifecycle.get("ttl_bars")
        if ttl_bars is not None and age_bars > ttl_bars:
            issues.append(_issue(
                CONFIRMATION_SURVIVED_EXPIRY, "error",
                {
                    "decision_id": row["id"],
                    "symbol": row["symbol"],
                    "age_bars": age_bars,
                    "ttl_bars": ttl_bars,
                },
                "确认已超过 TTL 仍标记为 valid（age_bars > ttl_bars）。"
                "生命周期解析器必须在 ttl 到期时翻转为 expired，过期确认不得"
                "继续作为入场依据。检查确认事件老化逻辑。",
            ))
    return issues


def _check_llm_proposal_immutable_change(
    repo: CryptoGuardRepository, marker_ts: datetime | None,
) -> list[dict[str, Any]]:
    """[proposal] An adjustment that mutates an immutable field."""
    if marker_ts is None:
        return []
    issues: list[dict[str, Any]] = []
    for row in _window_rows(repo, marker_ts):
        raw = _safe_json(row["raw_decision_json"])
        if not isinstance(raw, dict):
            continue
        proposal = raw.get("llm_risk_proposal")
        if not isinstance(proposal, dict):
            continue
        adjustments = proposal.get("adjustments")
        if not isinstance(adjustments, dict):
            continue
        changed = sorted(k for k in adjustments if k in IMMUTABLE_KEYS)
        if changed:
            issues.append(_issue(
                LLM_PROPOSAL_IMMUTABLE_CHANGE, "error",
                {
                    "decision_id": row["id"],
                    "symbol": row["symbol"],
                    "immutable_keys": changed,
                },
                "LLM 风险建议调整了不可变字段（symbol/side/fingerprint/确认/"
                "订单/数量/杠杆/risk_check 等）。这些字段在建议之后锁定，任何"
                "调整都是契约违约。检查风险建议生成是否把不可变字段当作可调整项。",
            ))
    return issues


def _check_llm_proposal_unknown_evidence(
    repo: CryptoGuardRepository, marker_ts: datetime | None,
) -> list[dict[str, Any]]:
    """[proposal] ``evidence_refs`` referencing evidence outside the stable
    set. An empty stable ``evidence_ids`` with refs FAILS CLOSED to unknown."""
    if marker_ts is None:
        return []
    issues: list[dict[str, Any]] = []
    for row in _window_rows(repo, marker_ts):
        raw = _safe_json(row["raw_decision_json"])
        if not isinstance(raw, dict):
            continue
        proposal = raw.get("llm_risk_proposal")
        if not isinstance(proposal, dict):
            continue
        refs = proposal.get("evidence_refs") or []
        if not refs:
            continue
        evidence_ids = raw.get("evidence_ids") or []
        unknown = sorted(set(refs) - set(evidence_ids))
        if unknown:
            issues.append(_issue(
                LLM_PROPOSAL_UNKNOWN_EVIDENCE, "error",
                {
                    "decision_id": row["id"],
                    "symbol": row["symbol"],
                    "unknown_refs": unknown,
                    "evidence_ids": sorted(set(evidence_ids)),
                },
                "LLM 风险建议引用了不在该决策稳定证据集（evidence_ids）中的证据。"
                "建议只能引用本轮决策实际收集的证据；未知引用说明上下文泄漏或"
                "证据 id 拼接错误。检查证据收集与建议生成之间的引用契约。",
            ))
    return issues


def _check_accepted_adjustment_increases_monetary_risk(
    repo: CryptoGuardRepository, marker_ts: datetime | None,
) -> list[dict[str, Any]]:
    """[verifier] An ACCEPTED adjustment that INCREASES monetary risk."""
    if marker_ts is None:
        return []
    issues: list[dict[str, Any]] = []
    for row in _window_rows(repo, marker_ts):
        raw = _safe_json(row["raw_decision_json"])
        if not isinstance(raw, dict):
            continue
        verification = raw.get("risk_adjustment_verification")
        if not isinstance(verification, dict):
            continue
        delta = verification.get("monetary_risk_delta", 0.0)
        if verification.get("accepted") is True and delta > 0:
            issues.append(_issue(
                ACCEPTED_ADJUSTMENT_INCREASES_MONETARY_RISK, "error",
                {
                    "decision_id": row["id"],
                    "symbol": row["symbol"],
                    "monetary_risk_delta": delta,
                },
                "验证器接受了增加货币风险敞口的调整（monetary_risk_delta > 0）。"
                "调整验证器的职责是拒绝风险扩大；接受正 delta 说明验证逻辑被绕过。"
                "检查确定性调整验证器。",
            ))
    return issues


def _check_order_without_final_verifier_success(
    repo: CryptoGuardRepository, marker_ts: datetime | None,
) -> list[dict[str, Any]]:
    """[verifier] A paper order whose linked decision has a shadow/paper_bounded
    risk-advisory envelope but NOT full verifier success.

    The legacy ``off`` path (and decisions with no advisory envelope) are out
    of scope. Filtering happens on the DECISION-side ``created_at`` because a
    paper order's own ``created_at`` is the fill time (NOW()), unrelated to the
    decision's marker window.

    08-12 P2-2 (fresh reviewer): also flags the persist-loss false negative --
    an order whose ``risk_advisory_mode`` says governance ran (NOT NULL and not
    'off') but whose linked decision lost the audit row (no dict
    ``raw_decision_json.risk_advisory``). The producer stamps the system-only
    envelope on the in-memory dict BEFORE the persist attempt and swallows a
    persist exception (always-stamp), so an order created from that in-memory
    dict carries the mode while the decision row never recorded the envelope --
    invisible to the old decision-side-only join.
    """
    if marker_ts is None:
        return []
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT d.id AS decision_id, d.symbol, o.id AS order_id,
               o.risk_advisory_mode, d.raw_decision_json
        FROM paper_orders o
        JOIN ga_decisions d ON d.id = o.ga_decision_id
        WHERE d.created_at >= %s::timestamptz
          AND d.raw_decision_json IS NOT NULL
        """,
        (marker_ts,),
    ).fetchall()
    for row in rows:
        order_mode = row["risk_advisory_mode"]
        raw = _safe_json(row["raw_decision_json"])
        risk_advisory = raw.get("risk_advisory") if isinstance(raw, dict) else None
        if isinstance(risk_advisory, dict):
            mode = risk_advisory.get("mode")
            if mode in (None, "off"):
                # Legacy deterministic path — no verifier by design.
                continue
            verification_ok = bool(risk_advisory.get("verification_ok"))
            final_risk_check_ok = bool(risk_advisory.get("final_risk_check_ok"))
            if verification_ok and final_risk_check_ok:
                continue
            issues.append(_issue(
                ORDER_WITHOUT_FINAL_VERIFIER_SUCCESS, "error",
                {
                    "paper_order_id": row["order_id"],
                    "decision_id": row["decision_id"],
                    "symbol": row["symbol"],
                    "mode": mode,
                    "verification_ok": verification_ok,
                    "final_risk_check_ok": final_risk_check_ok,
                },
                "存在 paper order，但其关联决策的风险建议（risk_advisory）未同时"
                "通过 verification_ok 与 final_risk_check_ok。最终风控未通过时不得"
                "生成订单——检查订单创建是否绕过确定性调整验证器。",
            ))
            continue
        # Decision-side envelope absent. A governance-created order (order-side
        # mode NOT NULL and not 'off') with a lost audit row is the persist-loss
        # case (always-stamp persist-swallow in ``_attach_risk_governance``).
        # Legacy orders (``risk_advisory_mode IS NULL``) are out of scope.
        if order_mode is not None and order_mode != "off":
            issues.append(_issue(
                ORDER_WITHOUT_FINAL_VERIFIER_SUCCESS, "error",
                {
                    "paper_order_id": row["order_id"],
                    "decision_id": row["decision_id"],
                    "symbol": row["symbol"],
                    "mode": order_mode,
                    "verification_ok": None,
                    "final_risk_check_ok": None,
                },
                "存在 paper order，其 risk_advisory_mode 表明风控治理已运行，但"
                "关联决策丢失了 risk_advisory 审计行（持久化失败被吞掉）。"
                "检查审计写入失败时订单创建是否绕过确定性调整验证器。",
            ))
    return issues


def _check_llm_risk_review_starvation(
    repo: CryptoGuardRepository, marker_ts: datetime | None,
) -> list[dict[str, Any]]:
    """[proposal] warning — >= 3 system ``failed`` proposals in the window.

    Legitimate ``reject``/``wait`` verdicts are user-visible outcomes, never
    starvation; only ``proposal_status == "failed"`` (LLM/provider failure)
    counts toward the threshold.
    """
    if marker_ts is None:
        return []
    failed_count = 0
    for row in _window_rows(repo, marker_ts):
        raw = _safe_json(row["raw_decision_json"])
        if not isinstance(raw, dict):
            continue
        proposal = raw.get("llm_risk_proposal")
        if isinstance(proposal, dict) and proposal.get("proposal_status") == "failed":
            failed_count += 1
    if failed_count >= _LLM_RISK_REVIEW_STARVATION_THRESHOLD:
        return [_issue(
            LLM_RISK_REVIEW_STARVATION, "warning",
            {"failed_count": failed_count,
             "threshold": _LLM_RISK_REVIEW_STARVATION_THRESHOLD},
            f"窗口内 {failed_count} 次风险建议系统失败（proposal_status=failed），"
            f"超过阈值 {_LLM_RISK_REVIEW_STARVATION_THRESHOLD}。风险委员会因"
            "LLM/provider 故障持续无法给出建议，可能掩盖真实风险。检查 LLM 风险"
            "建议的故障率与重试/熔断配置。",
        )]
    return []


def diagnose_llm_risk_governance(
    repo: CryptoGuardRepository, *, batch_id: str | None = None,
) -> dict[str, Any]:
    """Run the eight 08-10 risk-governance checks.

    Returns the same envelope as ``report_diagnostics.diagnose_report_accuracy``
    (``ok / issues / summary / total_issues / error_count / warning_count /
    legacy_info_count / layer_counts``) so existing renderers can consume it
    unchanged. ``batch_id`` is accepted for signature symmetry with the other
    diagnose_* entry points; the checks are windowed by marker cutoff, not by
    batch.
    """
    issues: list[dict[str, Any]] = []
    marker_ts: dict[str, datetime | None] = {}
    for name, key in MARKERS.items():
        issues.extend(_check_llm_risk_contract_marker_missing(repo, name, key))
        marker_ts[name] = _marker_ts(repo, key)

    # Per-check marker gating: each detection check runs only when ITS OWN
    # marker is present+parseable; absent/corrupt -> the check self-skips.
    issues.extend(_check_carried_confirmation_without_provenance(
        repo, marker_ts["lifecycle"]))
    issues.extend(_check_confirmation_survived_expiry(
        repo, marker_ts["lifecycle"]))
    issues.extend(_check_llm_proposal_immutable_change(
        repo, marker_ts["proposal"]))
    issues.extend(_check_llm_proposal_unknown_evidence(
        repo, marker_ts["proposal"]))
    issues.extend(_check_accepted_adjustment_increases_monetary_risk(
        repo, marker_ts["verifier"]))
    issues.extend(_check_order_without_final_verifier_success(
        repo, marker_ts["verifier"]))
    issues.extend(_check_llm_risk_review_starvation(
        repo, marker_ts["proposal"]))

    error_count = sum(1 for i in issues if i["severity"] == "error")
    warning_count = sum(1 for i in issues if i["severity"] == "warning")
    legacy_info_count = sum(1 for i in issues if i["severity"] == "legacy_info")
    layer_counts = {
        "current": sum(1 for i in issues if i.get("layer") == "current"),
        "warning": sum(1 for i in issues if i.get("layer") == "warning"),
        "legacy_audit": sum(1 for i in issues if i.get("layer") == "legacy_audit"),
    }
    summary: dict[str, Any] = {}
    for i in issues:
        summary[i["type"]] = summary.get(i["type"], 0) + 1
    summary["error_count"] = error_count
    summary["warning_count"] = warning_count
    summary["legacy_info_count"] = legacy_info_count
    return {
        "ok": error_count == 0,
        "issues": issues,
        "summary": summary,
        "total_issues": len(issues),
        "error_count": error_count,
        "warning_count": warning_count,
        "legacy_info_count": legacy_info_count,
        "layer_counts": layer_counts,
    }
