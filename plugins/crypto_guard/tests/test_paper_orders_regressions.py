"""Paper order, position, broker, and BTC#9 regression tests."""

import pytest

from plugins.crypto_guard.tests._smoke_suite import (
    PendingOrderManagerTest,
    TradeGateRegressionChainTest,
)

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

__all__ = ["PendingOrderManagerTest", "TradeGateRegressionChainTest"]
