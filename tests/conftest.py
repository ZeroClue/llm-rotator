import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rotator as rotator_module

from mock_upstream import MockUpstream


@pytest.fixture(scope="session")
def mock():
    m = MockUpstream()
    m.start()
    yield m
    m.stop()


@pytest.fixture(scope="session")
def rotator(mock):
    # Importing rotator builds nothing; the first get_app() below constructs
    # settings -> services -> app from this process's environment.
    os.environ["PROXY_1_URL"] = "http://127.0.0.1:1"
    os.environ["API_KEY_1"] = "node-one-key"
    os.environ["PROXY_2_URL"] = f"http://127.0.0.1:{mock.port}"
    os.environ["API_KEY_2"] = "node-two-key"
    os.environ["LLM_PROVIDER_URL"] = mock.url("/v1")
    os.environ["MAX_RETRIES"] = "6"
    rotator_module.get_app()
    return rotator_module


@pytest.fixture(autouse=True)
def clean_state(rotator):
    rotator.health_ledger.reset_all()
    yield
    rotator.health_ledger.reset_all()


@pytest.fixture()
def client(rotator):
    return rotator.app.test_client()


def build_optimizer(rotator, **overrides):
    """Fresh TokenOptimizer from OPTIMIZATION_CONFIG with field overrides —
    the single builder both the fixture and pipeline tests share."""
    import dataclasses

    cfg = (
        dataclasses.replace(rotator.OPTIMIZATION_CONFIG, **overrides)
        if overrides
        else rotator.OPTIMIZATION_CONFIG
    )
    return rotator.TokenOptimizer(config=cfg, model_name=rotator.settings.default_model)


@pytest.fixture()
def make_optimizer(rotator, monkeypatch):
    """Build a fresh TokenOptimizer with explicit OptimizationConfig overrides
    and swap it in as the module-level optimizer the view resolves."""
    def _make(**overrides):
        opt = build_optimizer(rotator, **overrides)
        monkeypatch.setattr(rotator, "token_optimizer", opt)
        return opt

    return _make


@pytest.fixture()
def chat_captures(mock):
    return mock.chat_posts
