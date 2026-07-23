"""Shadow virtual-trade and strategy-evolution regression tests."""

import pytest

from plugins.crypto_guard.tests._smoke_suite import ShadowVTLifecycleTest

pytestmark = [pytest.mark.pg, pytest.mark.schema_mutation, pytest.mark.slow]

__all__ = ["ShadowVTLifecycleTest"]
