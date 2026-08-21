import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mock_upstream import MockUpstream


@pytest.fixture(scope="session")
def mock():
    m = MockUpstream()
    m.start()
    yield m
    m.stop()


@pytest.fixture(scope="session")
def rotator(mock):
    os.environ["PROXY_1_URL"] = "http://127.0.0.1:1"
    os.environ["API_KEY_1"] = "node-one-key"
    os.environ["PROXY_2_URL"] = f"http://127.0.0.1:{mock.port}"
    os.environ["API_KEY_2"] = "node-two-key"
    os.environ["LLM_PROVIDER_URL"] = mock.url("/v1")
    os.environ["MAX_RETRIES"] = "6"
    return importlib.import_module("rotator")


@pytest.fixture(autouse=True)
def clean_state(rotator):
    rotator.token_optimizer.context_cache.clear()
    if hasattr(rotator.node_iterator, "clear_failures"):
        rotator.node_iterator.clear_failures()
    yield
    rotator.token_optimizer.context_cache.clear()
    if hasattr(rotator.node_iterator, "clear_failures"):
        rotator.node_iterator.clear_failures()


@pytest.fixture()
def client(rotator):
    return rotator.app.test_client()


@pytest.fixture()
def chat_captures(mock):
    return mock.chat_posts
