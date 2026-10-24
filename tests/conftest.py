from datetime import UTC, datetime

import pytest

from toss_trading.config import Settings
from toss_trading.domain import Session
from toss_trading.persistence import Store
from toss_trading.trading_config import TradingConfig


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in Settings.model_fields:
        monkeypatch.delenv(key.upper(), raising=False)


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        toss_client_id="test-client",
        toss_client_secret="test-secret",
        toss_account_seq=7,
        operations_api_key="test-operations-key-32-characters-long",
    )


@pytest.fixture
def config():
    return TradingConfig()


@pytest.fixture
def session():
    return Session(
        day="2026-10-19",
        open=datetime(2026, 10, 19, 13, 30, tzinfo=UTC),
        close=datetime(2026, 10, 19, 20, tzinfo=UTC),
    )


@pytest.fixture
def store(tmp_path, config):
    value = Store(f"sqlite:///{tmp_path}/paper.db", config)
    yield value
    value.close()
