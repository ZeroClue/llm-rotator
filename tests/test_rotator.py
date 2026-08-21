import json
import logging
import os
import subprocess
import sys

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
        assert node["api_key"] not in text


def test_default_config_does_not_inject_cache_control(client, chat_captures):
    resp = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": BASIC_MESSAGES})
    assert resp.status_code == 200
    sent = jload(chat_captures()[-1]["body"])
    assert "cache_control" not in json.dumps(sent)
    assert isinstance(sent["messages"][0]["content"], str)


def test_explicit_prompt_caching_adds_valid_markers(client, chat_captures, rotator, monkeypatch):
    monkeypatch.setattr(rotator, "ENABLE_PROMPT_CACHING", True)
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


def test_cache_hit_respects_client_max_tokens(client, chat_captures, caplog):
    import logging
    payload = {"model": "gpt-4o", "messages": BASIC_MESSAGES}
    with caplog.at_level(logging.INFO, logger="rotator"):
        r1 = client.post("/v1/chat/completions", json={**payload, "max_tokens": 123})
        r2 = client.post("/v1/chat/completions", json={**payload, "max_tokens": 456})
    assert r1.status_code == r2.status_code == 200
    posts = chat_captures()
    assert jload(posts[-2]["body"])["max_tokens"] == 123
    assert jload(posts[-1]["body"])["max_tokens"] == 456
    assert any("Cache hit" in r.getMessage() for r in caplog.records)


def test_context_cache_is_lru_bounded(rotator, monkeypatch):
    from collections import OrderedDict
    monkeypatch.setattr(rotator, "CONTEXT_CACHE_SIZE", 2)
    opt = rotator.token_optimizer
    assert isinstance(opt.context_cache, OrderedDict)
    opt._cache_result("k1", {"messages": [], "tokens_saved": 0})
    opt._cache_result("k2", {"messages": [], "tokens_saved": 0})
    assert opt._get_cached("k1") is not None
    opt._cache_result("k3", {"messages": [], "tokens_saved": 0})
    assert opt._get_cached("k1") is not None
    assert opt._get_cached("k2") is None
    assert opt._get_cached("k3") is not None
    assert len(opt.context_cache) <= 2


def test_importance_filter_preserves_system_and_alternation(rotator):
    msgs = (
        [{"role": "system", "content": "Be helpful."}]
        + [{"role": "user", "content": f"q{i}"} for i in range(1)]
        + [{"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"},
           {"role": "assistant", "content": "a2"}, {"role": "user", "content": "q3"},
           {"role": "assistant", "content": "a3"}, {"role": "user", "content": "q4"},
           {"role": "assistant", "content": "a4"}]
        + [{"role": "user", "content": "final question"}]
    )
    out = rotator.token_optimizer._filter_by_importance(msgs, 0.25)
    roles = [m["role"] for m in out]
    assert roles[0] == "system"
    assert roles[-1] == "user"
    assert all(a != b for a, b in zip(roles, roles[1:]))


def test_importance_scoring_end_to_end_keeps_system(client, chat_captures, rotator, monkeypatch):
    monkeypatch.setattr(rotator, "ENABLE_IMPORTANCE_SCORING", True)
    monkeypatch.setattr(rotator, "MIN_MESSAGE_IMPORTANCE", 0.25)
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


def test_semantic_compression_uses_llmlingua_api(client, chat_captures, rotator, monkeypatch):
    fake = FakeCompressor()
    monkeypatch.setattr(rotator.token_optimizer, "compressor", fake)
    monkeypatch.setattr(rotator, "ENABLE_SEMANTIC_COMPRESSION", True)
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


def test_compute_backoff_bounds(rotator, monkeypatch):
    monkeypatch.setattr(rotator, "RETRY_BACKOFF_BASE", 0.5)
    monkeypatch.setattr(rotator, "RETRY_BACKOFF_MAX", 8.0)
    ra = rotator.compute_backoff(0, 2.0)
    assert ra == 2.0
    assert rotator.compute_backoff(0, 100.0) == 8.0
    for attempt in range(6):
        d = rotator.compute_backoff(attempt, None)
        assert 0.5 <= d <= 8.0
    assert rotator.compute_backoff(3, None) >= rotator.compute_backoff(0, None)


def test_compute_backoff_jitter_is_injectable(rotator, monkeypatch):
    monkeypatch.setattr(rotator, "RETRY_BACKOFF_BASE", 0.5)
    monkeypatch.setattr(rotator, "RETRY_BACKOFF_MAX", 8.0)

    def stub(lo, hi):
        return hi / 4  # deterministic quarter of the jitter band

    assert rotator.compute_backoff(0, None, rng=stub) == 0.625  # 0.5·2⁰ + 0.125
    assert rotator.compute_backoff(1, None, rng=stub) == 1.125  # 0.5·2¹ + 0.125
    assert rotator.compute_backoff(5, None, rng=stub) == 8.0  # capped
    assert rotator.compute_backoff(0, 2.0, rng=stub) == 2.0  # Retry-After ignores jitter


def test_cooldown_skips_recently_failed_node(rotator, monkeypatch):
    monkeypatch.setattr(rotator, "NODE_COOLDOWN_BASE", 30)
    monkeypatch.setattr(rotator, "NODE_COOLDOWN_MAX", 300)
    clock = FakeClock(start=100.0)

    it = rotator.ThreadSafeIterator([
        {"proxy": "p1", "api_key": "k1", "node_id": 1},
        {"proxy": "p2", "api_key": "k2", "node_id": 2},
    ])
    first = it.get_next(now=clock.now)
    assert first["node_id"] == 1
    it.report_failure(first, now=clock.now)
    assert [it.get_next(now=clock.now)["node_id"] for _ in range(3)] == [2, 2, 2]
    clock.advance(29)  # cooldown deadline is t=130; still cooling at t=129
    assert it.get_next(now=clock.now)["node_id"] == 2
    clock.advance(2)  # t=131, past the deadline
    assert it.get_next(now=clock.now)["node_id"] == 1


def test_never_starves_when_all_nodes_are_cooling(rotator, monkeypatch):
    monkeypatch.setattr(rotator, "NODE_COOLDOWN_BASE", 30)
    monkeypatch.setattr(rotator, "NODE_COOLDOWN_MAX", 300)
    clock = FakeClock(start=100.0)

    it = rotator.ThreadSafeIterator([
        {"proxy": "p1", "api_key": "k1", "node_id": 1},
        {"proxy": "p2", "api_key": "k2", "node_id": 2},
    ])
    node1 = it.get_next(now=clock.now)
    it.report_failure(node1, now=clock.now)
    node2 = it.get_next(now=clock.now)
    it.report_failure(node2, now=clock.now)
    # Both nodes cooling: the never-starve rule serves the cursor node anyway.
    assert [it.get_next(now=clock.now)["node_id"] for _ in range(2)] == [1, 2]


def test_report_success_clears_cooldown(rotator, monkeypatch):
    monkeypatch.setattr(rotator, "NODE_COOLDOWN_BASE", 60)
    monkeypatch.setattr(rotator, "NODE_COOLDOWN_MAX", 60)
    it = rotator.ThreadSafeIterator([
        {"proxy": "p1", "api_key": "k1", "node_id": 1},
        {"proxy": "p2", "api_key": "k2", "node_id": 2},
    ])
    node = it.get_next()
    it.report_failure(node)
    assert it.get_next()["node_id"] == 2
    it.report_success(node)
    assert it.get_next()["node_id"] == 1


def test_snapshot_reports_remaining_cooldown(rotator, monkeypatch):
    monkeypatch.setattr(rotator, "NODE_COOLDOWN_BASE", 30)
    monkeypatch.setattr(rotator, "NODE_COOLDOWN_MAX", 300)
    clock = FakeClock(start=100.0)

    it = rotator.ThreadSafeIterator([
        {"proxy": "p1", "api_key": "k1", "node_id": 1},
        {"proxy": "p2", "api_key": "k2", "node_id": 2},
    ])
    node = it.get_next(now=clock.now)
    it.report_failure(node, now=clock.now)
    clock.advance(15)
    snap = {e["node_id"]: e for e in it.snapshot(now=clock.now)}
    assert snap[1]["consecutive_failures"] == 1
    assert snap[1]["cooldown_seconds"] == 15.0  # 30s cooldown, 15s elapsed
    assert snap[2]["cooldown_seconds"] == 0.0


def test_auth_token_rejects_missing_header(rotator, monkeypatch, client, chat_captures):
    monkeypatch.setattr(rotator, "PROXY_AUTH_TOKEN", "sekrit")
    before = len(chat_captures())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": BASIC_MESSAGES},
    )
    assert resp.status_code == 401
    assert len(chat_captures()) == before  # never reached upstream


def test_auth_token_gates_wrong_and_admits_correct(rotator, monkeypatch, client, chat_captures):
    monkeypatch.setattr(rotator, "PROXY_AUTH_TOKEN", "sekrit")
    wrong = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": BASIC_MESSAGES},
        headers={"Authorization": "Bearer nope"},
    )
    assert wrong.status_code == 401
    before = len(chat_captures())
    right = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": BASIC_MESSAGES},
        headers={"Authorization": "Bearer sekrit"},
    )
    assert right.status_code == 200
    assert len(chat_captures()) == before + 1


def test_health_stays_open_with_auth_enabled(rotator, monkeypatch, client):
    monkeypatch.setattr(rotator, "PROXY_AUTH_TOKEN", "sekrit")
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "healthy"


def test_auth_disabled_by_default(rotator, monkeypatch, client):
    monkeypatch.setattr(rotator, "PROXY_AUTH_TOKEN", "")
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": BASIC_MESSAGES},
    )
    assert resp.status_code == 200


def test_models_endpoint_requires_token_too(rotator, monkeypatch, client):
    monkeypatch.setattr(rotator, "PROXY_AUTH_TOKEN", "sekrit")
    assert client.get("/v1/models").status_code == 401


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


def test_log_level_env_var_is_effective():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def debug_enabled(level):
        env = os.environ.copy()
        env.update({
            "LOG_LEVEL": level,
            "PROXY_1_URL": "socks5h://127.0.0.1:9",
            "API_KEY_1": "dummy",
        })
        code = "import logging, rotator; print(logging.getLogger('rotator').isEnabledFor(logging.DEBUG))"
        out = subprocess.run(
            [sys.executable, "-c", code],
            env=env, cwd=repo_root, capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    assert debug_enabled("DEBUG") == "True"
    assert debug_enabled("INFO") == "False"
