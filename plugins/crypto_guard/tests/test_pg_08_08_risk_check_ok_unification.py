# -*- coding: utf-8 -*-
"""08-08 P0-1 (PRD): risk-pass field unification to ``risk_check.ok`` with
STRICT identity.

Production shape is ``risk_check={"ok": bool}``. The gate
``_recheck_order_gate`` must read ``risk.get("ok") is True`` — NOT
``bool(risk.get("ok"))`` (which wrongly passes ``bool("yes")==True``) and NOT
the old wrong field ``risk_ok``. Missing / string / number / ``None`` /
``False`` risk values all fail-closed (reject). The only two
``{"risk_ok": True}`` fake test shapes (08-04 bridge b / 08-06 once-ever) are
replaced with the real ``{"ok": True}`` controller shape.

RED-first + revert-fail: the current gate reads ``risk.get("risk_ok")``, so it
ACCEPTS the wrong ``{"risk_ok": True}`` shape and REJECTS the real
``{"ok": True}`` shape — the "accept real shape" test fails (RED), and the
"reject old shape" test passes for the wrong reason. After the fix the real
shape passes and every non-``True`` value fails-closed.

No production DB mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

from plugins.crypto_guard.tests.pg_fixtures import make_repo

_SYMBOL = "BTCUSDT"


def _gate_clearing_decision(**overrides: dict) -> dict:
    """A decision dict that clears ``_recheck_order_gate`` (unless overridden).

    ``risk_check`` defaults to the REAL controller/adapter production shape
    ``{"ok": True}``. ``overrides`` are merged shallowly; nested keys (e.g.
    ``trade_plan``, ``risk_check``) are replaced wholesale.
    """
    decision = {
        "symbol": _SYMBOL,
        "signal_id": None,
        "ga_decision_id": 12_345,
        "plan_execution_state": "confirmed",
        "plan_origin": "llm_confirmed",
        "llm_status": "ok",
        "effective_signal_grade": "A",
        "signal_grade": "A",
        "risk_check": {"ok": True},
        "trade_plan": {
            "side": "LONG",
            "entry_type": "market",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profits": [{"price": 108.0}],
            "quantity": 0.5,
            "reason": "watch recheck",
        },
    }
    decision.update(overrides)
    return decision


def _watch() -> dict:
    return {"symbol": _SYMBOL, "direction": "LONG"}


class TestGateAcceptsRealControllerShape:
    def test_real_shape_ok_true_passes(self) -> None:
        """P0-1: the real controller/adapter shape ``{"ok": True}`` passes the
        gate. RED: the current gate reads ``risk_ok`` so it rejects this."""
        from plugins.crypto_guard.run_ga_workers import _recheck_order_gate

        handle = make_repo()
        try:
            ok, reason = _recheck_order_gate(handle.repo, _watch(), _gate_clearing_decision())
            assert ok is True, f"real controller shape {{'ok': True}} must pass; {reason}"
            assert reason == "ok"
        finally:
            handle.close()

    @pytest.mark.parametrize(
        "label,risk_check",
        [
            ("non_bool_truthy_string", {"ok": "yes"}),
            ("non_bool_number", {"ok": 1}),
            ("non_bool_false", {"ok": False}),
            ("non_bool_none", {"ok": None}),
            ("wrong_field_old_shape", {"risk_ok": True}),
        ],
    )
    def test_fail_closed_rejects_non_true_risk(self, label: str, risk_check: dict) -> None:
        """P0-1: every non-``True`` risk value fails-closed (reject). This
        includes the truthy string ``"yes"`` (which ``bool()`` would wrongly
        accept) and the old wrong ``{"risk_ok": True}`` shape."""
        from plugins.crypto_guard.run_ga_workers import _recheck_order_gate

        handle = make_repo()
        try:
            ok, reason = _recheck_order_gate(
                handle.repo, _watch(), _gate_clearing_decision(risk_check=risk_check)
            )
            assert ok is False, f"{label}: must fail-closed (got ok={ok!r}, {reason})"
            assert reason == "risk_ok=false"
        finally:
            handle.close()

    def test_fail_closed_rejects_missing_risk_check(self) -> None:
        """P0-1: a decision with NO ``risk_check`` key fails-closed."""
        from plugins.crypto_guard.run_ga_workers import _recheck_order_gate

        handle = make_repo()
        try:
            decision = _gate_clearing_decision()
            del decision["risk_check"]
            ok, reason = _recheck_order_gate(handle.repo, _watch(), decision)
            assert ok is False, f"missing risk_check must fail-closed; {reason}"
        finally:
            handle.close()


def test_no_risk_ok_data_shape_fixture_anywhere_but_the_deliberate_negative() -> None:
    """P0-1 grep-guard (implement.md Step 1): no ``{"risk_ok": ...}``
    DATA-SHAPE dict key may exist anywhere in the test tree except the single
    deliberate fail-closed negative above (``wrong_field_old_shape`` in
    ``test_fail_closed_rejects_non_true_risk``).

    The guard walks every ``*.py`` file under ``tests/``, AST-parses it, and
    flags any ``dict`` literal whose key is the string constant ``"risk_ok"``
    OUTSIDE that one parametrized negative (keyed by class + function scope).
    Bare ``risk_ok`` identifiers (helper parameters, ``deterministic_risk_ok=``
    kwargs, ``risk_ok=`` decision-builder kwargs) and the gate's
    ``"risk_ok=false"`` reason-code string are NOT dict-literal keys and remain
    legal. Re-introducing a wrong-shape fixture (e.g. a future
    ``{"risk_ok": True}`` in a new test) fails here.

    Revert-fail: deleting the deliberate negative removes the only allowed
    ``{"risk_ok": ...}`` shape, and the ``allowed_count >= 1`` assertion fails —
    the wrong-shape fail-closed case must not silently disappear.
    """
    import ast as _ast
    from pathlib import Path as _Path

    tests_dir = _Path(__file__).resolve().parent
    allowed_file = "test_pg_08_08_risk_check_ok_unification.py"
    allowed_class = "TestGateAcceptsRealControllerShape"
    allowed_func = "test_fail_closed_rejects_non_true_risk"
    violations: list[str] = []
    allowed_count = 0

    def _scan(node: _ast.AST, rel: str, cls: str | None, func: str | None) -> None:
        nonlocal allowed_count
        if isinstance(node, _ast.Dict):
            for key in node.keys:
                if isinstance(key, _ast.Constant) and key.value == "risk_ok":
                    if (
                        rel == allowed_file
                        and cls == allowed_class
                        and func == allowed_func
                    ):
                        allowed_count += 1
                    else:
                        violations.append(
                            f"{rel}:{getattr(node, 'lineno', '?')} "
                            f"{{'{key.value}': ...}} (class={cls!r} func={func!r})"
                        )
        for child in _ast.iter_child_nodes(node):
            child_cls = cls
            child_func = func
            if isinstance(child, _ast.ClassDef):
                child_cls = child.name
                child_func = None
            elif isinstance(child, _ast.FunctionDef) or isinstance(
                child, _ast.AsyncFunctionDef
            ):
                child_func = child.name
            _scan(child, rel, child_cls, child_func)

    for py in sorted(tests_dir.rglob("*.py")):
        if "__pycache__" in str(py):
            continue
        try:
            tree = _ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        _scan(tree, py.name, None, None)

    assert allowed_count >= 1, (
        "P0-1 grep-guard: the deliberate fail-closed negative "
        f"(\"{allowed_func}\", {{\"risk_ok\": True}}) must exist"
    )
    assert violations == [], (
        "P0-1 grep-guard: test tree must not seed a {\"risk_ok\": ...} data "
        f"shape; found {len(violations)}: {violations}"
    )
