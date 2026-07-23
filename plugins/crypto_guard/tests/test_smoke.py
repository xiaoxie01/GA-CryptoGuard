"""Compatibility proxy for the split CryptoGuard regression suite.

The collected tests now live in domain modules. Runtime imports retained by
older regression methods resolve lazily through this module without rebinding
all test classes here (which would collect them twice).
"""

from plugins.crypto_guard.tests import _smoke_suite as _suite


def __getattr__(name: str):
    return getattr(_suite, name)
