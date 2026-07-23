"""Core CryptoGuard production-path regression tests."""

import pytest

from plugins.crypto_guard.tests._smoke_suite import CryptoGuardSmokeTest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

__all__ = ["CryptoGuardSmokeTest"]
