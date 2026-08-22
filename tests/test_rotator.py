import json
import logging
import os
import subprocess
import sys

import dataclasses

import pytest


def jload(text):
    return json.loads(text)

BASIC_MESSAGES = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hello"},
]


class FakeClock:
    """Stepped virtual clock: time moves only when advance() is called."""

    def __init__(self, start=0.0):
        self.now = float(start)

    def advance(self, seconds):
        self.now += float(seconds)


def test_health_reports_node_state(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "healthy"
    assert body["nodes_configured"] >= 1
    assert isinstance(body["nodes"], list) and body["nodes"]
    entry = body["nodes"][0]
    assert {"node_id", "proxy", "consecutive_failures", "cooldown_seconds"} <= set(entry)


def test_health_never_leaks_api_keys(client, rotator):
    resp = client.get("/health")
    text = resp.get_data(as_text=True)
    assert "api_key" not in text.lower().replace("api_keys_configured", "")
    for node in rotator.NODE_POOL:
        assert node.api_key not in text


def test_default_config_does_not_inject_cache_control(client, chat_captures):
    resp = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": BASIC_MESSAGES})
    assert resp.status_code == 200
    sent = jload(chat_captures()[-1]["body"])
    assert "cache_control" not in json.dumps(sent)
    assert isinstance(sent["messages"][0]["content"], str)


def test_explicit_prompt_caching_adds_valid_markers(client, chat_captures, make_optimizer):
    make_optimizer(enable_prompt_caching=True)
    resp = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": BASIC_MESSAGES})
    assert resp.status_code == 200
    sent = jload(chat_captures()[-1]["body"])
    dumped = json.dumps(sent)
    assert "cache_control" in dumped
    assert {"type": "ephemeral"} in [p.get("cache_control") for m in sent["messages"]
                                     if isinstance(m["content"], list) for p in m["content"]]
    assert "ttl_seconds" not in dumped


def test_usage_logged_for_buffered_responses(client, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="rotator"):
        resp = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": BASIC_MESSAGES})
    assert resp.status_code == 200
    assert any("Token usage" in r.getMessage() for r in caplog.records)


def test_importance_scoring_end_to_end_keeps_system(client, chat_captures, make_optimizer):
    make_optimizer(enable_importance_scoring=True, min_message_importance=0.25)
    msgs = [{"role": "system", "content": "Be helpful."}] + [
        {"role": r, "content": c}
        for r, c in [("user", "q1"), ("assistant", "a1"), ("user", "q2"),
                     ("assistant", "a2"), ("user", "q3"), ("assistant", "a3"),
                     ("user", "q4"), ("assistant", "a4")]
    ] + [{"role": "user", "content": "final question"}]
    resp = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": msgs})
    assert resp.status_code == 200
    sent = jload(chat_captures()[-1]["body"])["messages"]
    roles = [m["role"] for m in sent]
    assert roles[0] == "system"
    assert roles[-1] == "user"
    assert all(a != b for a, b in zip(roles, roles[1:]))


class FakeCompressor:
    def __init__(self):
        self.calls = []

    def compress_prompt(self, context, instruction="", ratio=None, **kwargs):
        self.calls.append({"count": len(context), "instruction": instruction, "ratio": ratio})
        return {"compressed_prompt": "ZZ" + context[0]}


def test_semantic_compression_uses_llmlingua_api(client, chat_captures, make_optimizer):
    opt = make_optimizer(enable_semantic_compression=True)
    fake = FakeCompressor()
    opt.compressor = fake
    msgs = [
        {"role": "system", "content": "system text here"},
        {"role": "user", "content": "user text here"},
    ]
    resp = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": msgs})
    assert resp.status_code == 200
    assert len(fake.calls) == 2
    assert fake.calls[0]["instruction"]
    sent = jload(chat_captures()[-1]["body"])["messages"]
    assert [m["content"] for m in sent] == ["ZZsystem text here", "ZZuser text here"]


def test_failover_reaches_healthy_node_with_correct_key(client, chat_captures):
    resp = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": BASIC_MESSAGES})
    assert resp.status_code == 200
    assert chat_captures()[-1]["auth"] == "Bearer node-two-key"


def test_retry_after_header_is_honored(mock, client):
    mock.script = [(429, {"Retry-After": "0"}, {"error": {"message": "slow down"}})]
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": BASIC_MESSAGES},
    )
    assert resp.status_code == 200
    assert len(mock.chat_posts()) >= 2


def make_nodes(rotator, count=2):
    return [
        rotator.Node(node_id=i, proxy=f"p{i}", api_key=f"k{i}")
        for i in range(1, count + 1)
    ]


def test_node_rejects_unknown_fields(rotator):
    with pytest.raises(TypeError):
        rotator.Node(node_id=1, proxy="p", api_key="k", typo="x")


def test_node_repr_hides_api_key(rotator):
    node = rotator.Node(node_id=1, proxy="p", api_key="sekrit")
    assert "sekrit" not in repr(node)


def make_selector(rotator, cooldown_base=30.0, cooldown_max=300.0):
    nodes = make_nodes(rotator)
    ledger = rotator.HealthLedger(nodes, cooldown_base, cooldown_max)
    return rotator.NodeSelector(nodes, ledger), ledger


def test_ledger_rejects_out_of_pool_nodes(rotator):
    _, ledger = make_selector(rotator)
    stranger = rotator.Node(node_id=99, proxy="px", api_key="kx")
    with pytest.raises(ValueError):
        ledger.record_failure(stranger)
    with pytest.raises(ValueError):
        ledger.record_success(stranger)
    with pytest.raises(ValueError):
        ledger.usable(stranger)


def test_cooldown_skips_recently_failed_node(rotator):
    clock = FakeClock(start=100.0)
    selector, ledger = make_selector(rotator)

    first = selector.select(now=clock.now)
    assert first.node_id == 1
    ledger.record_failure(first, now=clock.now)
    assert [selector.select(now=clock.now).node_id for _ in range(3)] == [2, 2, 2]
    clock.advance(29)  # cooldown deadline is t=130; still cooling at t=129
    assert selector.select(now=clock.now).node_id == 2
    clock.advance(2)  # t=131, past the deadline
    assert selector.select(now=clock.now).node_id == 1


def test_never_starves_when_all_nodes_are_cooling(rotator):
    clock = FakeClock(start=100.0)
    selector, ledger = make_selector(rotator)

    node1 = selector.select(now=clock.now)
    ledger.record_failure(node1, now=clock.now)
    node2 = selector.select(now=clock.now)
    ledger.record_failure(node2, now=clock.now)
    # Both nodes cooling: the never-starve rule serves the cursor node anyway.
    assert [selector.select(now=clock.now).node_id for _ in range(2)] == [1, 2]


def test_record_success_clears_cooldown(rotator):
    selector, ledger = make_selector(rotator, cooldown_base=60.0, cooldown_max=60.0)

    node = selector.select()
    ledger.record_failure(node)
    assert selector.select().node_id == 2
    ledger.record_success(node)
    assert selector.select().node_id == 1


def test_cooldown_doubles_and_caps_through_public_interface(rotator):
    clock = FakeClock(start=0.0)
    _, ledger = make_selector(rotator, cooldown_base=2.0, cooldown_max=5.0)
    node = rotator.Node(node_id=1, proxy="p1", api_key="k1")

    ledger.record_failure(node, now=clock.now)
    assert ledger.cooldown_remaining(node, now=1.9) == pytest.approx(0.1)  # base: 2s
    ledger.record_failure(node, now=2.0)
    assert ledger.cooldown_remaining(node, now=2.0) == pytest.approx(4.0)  # doubled
    ledger.record_failure(node, now=6.0)
    assert ledger.cooldown_remaining(node, now=6.0) == 5.0  # capped
    ledger.record_failure(node, now=11.0)
    assert ledger.cooldown_remaining(node, now=11.0) == 5.0  # stays capped
    assert ledger.failure_count(node) == 4
    ledger.record_success(node)
    assert ledger.cooldown_remaining(node, now=11.0) == 0.0
    assert ledger.failure_count(node) == 0


def test_snapshot_reports_remaining_cooldown(rotator):
    clock = FakeClock(start=100.0)
    nodes = make_nodes(rotator)
    ledger = rotator.HealthLedger(nodes, 30.0, 300.0)

    ledger.record_failure(nodes[0], now=clock.now)
    clock.advance(15)
    snap = {e["node_id"]: e for e in rotator.node_health_snapshot(nodes, ledger, now=clock.now)}
    assert snap[1]["consecutive_failures"] == 1
    assert snap[1]["cooldown_seconds"] == 15.0  # 30s cooldown, 15s elapsed
    assert snap[2]["cooldown_seconds"] == 0.0


def test_auth_token_rejects_missing_header(rotator, monkeypatch, client, chat_captures):
    monkeypatch.setattr(rotator, "settings", dataclasses.replace(rotator.settings, auth_token="sekrit"))
    before = len(chat_captures())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": BASIC_MESSAGES},
    )
    assert resp.status_code == 401
    assert len(chat_captures()) == before  # never reached upstream


def test_auth_token_gates_wrong_and_admits_correct(rotator, monkeypatch, client, chat_captures):
    monkeypatch.setattr(rotator, "settings", dataclasses.replace(rotator.settings, auth_token="sekrit"))
    before = len(chat_captures())
    wrong = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": BASIC_MESSAGES},
        headers={"Authorization": "Bearer nope"},
    )
    assert wrong.status_code == 401
    assert len(chat_captures()) == before  # wrong token never reached upstream
    right = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": BASIC_MESSAGES},
        headers={"Authorization": "Bearer sekrit"},
    )
    assert right.status_code == 200
    assert len(chat_captures()) == before + 1


def test_auth_token_non_ascii_header_gets_401_not_500(rotator, monkeypatch, client):
    monkeypatch.setattr(rotator, "settings", dataclasses.replace(rotator.settings, auth_token="sekrit"))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": BASIC_MESSAGES},
        headers={"Authorization": "Bearer tøken"},
    )
    assert resp.status_code == 401


def test_health_stays_open_with_auth_enabled(rotator, monkeypatch, client):
    monkeypatch.setattr(rotator, "settings", dataclasses.replace(rotator.settings, auth_token="sekrit"))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "healthy"


def test_auth_disabled_by_default(rotator, monkeypatch, client):
    monkeypatch.setattr(rotator, "settings", dataclasses.replace(rotator.settings, auth_token=""))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": BASIC_MESSAGES},
    )
    assert resp.status_code == 200


def test_models_endpoint_requires_token_too(rotator, monkeypatch, client):
    monkeypatch.setattr(rotator, "settings", dataclasses.replace(rotator.settings, auth_token="sekrit"))
    assert client.get("/v1/models").status_code == 401


def test_models_endpoint_fails_over_to_healthy_node(mock, client):
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.get_json()["data"][0]["id"] == "gpt-4o"
    gets = [r for r in mock.captured() if r["method"] == "GET"]
    assert gets and gets[-1]["path"].endswith("/models")
    assert gets[-1]["auth"] == "Bearer node-two-key"


def test_health_reports_available_node_count(client):
    resp = client.get("/health")
    body = resp.get_json()
    assert body["nodes_available"] == body["nodes_configured"]


def test_ready_flips_503_when_all_nodes_cooling(rotator, client):
    assert client.get("/ready").status_code == 200
    for node in rotator.NODE_POOL:
        rotator.health_ledger.record_failure(node)
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.get_json()["nodes_available"] == 0
    health = client.get("/health").get_json()
    assert health["nodes_available"] == 0
    assert health["status"] == "healthy"  # /health shape unchanged otherwise


def test_ready_stays_open_with_auth_enabled(rotator, monkeypatch, client):
    monkeypatch.setattr(rotator, "settings", dataclasses.replace(rotator.settings, auth_token="sekrit"))
    assert client.get("/ready").status_code == 200


def test_unknown_path_returns_404(client):
    resp = client.get("/definitely/not/here")
    assert resp.status_code == 404


def test_streamed_response_headers_are_sanitized(client):
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "stream": True, "messages": BASIC_MESSAGES},
    )
    assert resp.status_code == 200
    headers = resp.headers
    assert len(headers.getlist("Date")) <= 1
    assert len(headers.getlist("Connection")) <= 1
    if headers.get("Connection"):
        assert "," not in headers["Connection"]
    assert "MockUpstream" not in (headers.get("Server") or "")
    assert "MockUpstream" not in headers.get("Content-Type", "")
    body = resp.get_data(as_text=True)
    assert "part4" in body and "[DONE]" in body


def test_log_level_env_var_is_effective(rotator):
    import logging

    debug_settings = dataclasses.replace(rotator.settings, log_level="DEBUG")
    info_settings = dataclasses.replace(rotator.settings, log_level="INFO")

    try:
        rotator.configure_logging(debug_settings)
        assert logging.getLogger().isEnabledFor(logging.DEBUG) is True

        rotator.configure_logging(info_settings)
        assert logging.getLogger().isEnabledFor(logging.DEBUG) is False
    finally:
        # configure_logging mutates the root logger globally; restore the
        # process's real configuration so later tests aren't affected.
        rotator.configure_logging(rotator.settings)
