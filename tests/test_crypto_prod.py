from __future__ import annotations

import os

import pytest

from crossing import crypto


def test_prod_refuses_missing_seed(monkeypatch):
    crypto.reset_for_tests()
    monkeypatch.delenv("CROSSING_ED25519_SEED", raising=False)
    monkeypatch.setenv("CROSSING_ALLOW_DEV", "0")
    with pytest.raises(crypto.ProductionConfigError):
        crypto.signing_key()
    monkeypatch.setenv("CROSSING_ALLOW_DEV", "1")
    crypto.reset_for_tests()
    crypto.signing_key()
