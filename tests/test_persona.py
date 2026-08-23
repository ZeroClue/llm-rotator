"""Persona-model tests: derivation determinism, override precedence,
loud failure on malformed overrides, per-node consistency under failover,
and health exposure. All through public interfaces; no HTTP."""

import os

import pytest

import failover

from test_failover import FakeResponse, FakeSession, RecordingSleeper, stub_rng


POOL_ENV = {
    "PROXY_1_URL": "socks5h://node1.ts.net:1080", "API_KEY_1": "key1",
    "PROXY_2_URL": "socks5h://node2.ts.net:1080", "API_KEY_2": "key2",
    "PROXY_3_URL": "socks5h://node3.ts.net:1080", "API_KEY_3": "key3",
}


@pytest.fixture()
def pool_env(monkeypatch):
    for k, v in POOL_ENV.items():
        monkeypatch.setenv(k, v)
    for k in list(os.environ):
        if k.startswith("PERSONA_"):
            monkeypatch.delenv(k)


def test_persona_derivation_is_deterministic(rotator, pool_env):
    first = rotator.build_node_pool()
    second = rotator.build_node_pool()

    assert [(n.node_id, n.user_agent, n.fingerprint) for n in first] == \
           [(n.node_id, n.user_agent, n.fingerprint) for n in second]
    # Derived values come from the curated stacks, never empty.
    stack_names = {s["name"] for s in rotator.PERSONA_STACKS}
    for node in first:
        assert node.user_agent
        assert node.fingerprint in stack_names


def test_persona_assignment_spreads_across_stacks(rotator):
    seen = {rotator.resolve_persona(node_id)[1]
            for node_id in range(1, 4 * len(rotator.PERSONA_STACKS) + 1)}
    assert len(seen) >= 2  # hash assignment is not degenerate


def test_persona_overrides_win_and_independence(rotator, pool_env, monkeypatch):
    monkeypatch.setenv("PERSONA_2_USER_AGENT", "custom-agent/9.9")
    nodes = rotator.build_node_pool()

    by_id = {n.node_id: n for n in nodes}
    assert by_id[2].user_agent == "custom-agent/9.9"
    assert by_id[2].fingerprint  # untouched half falls back to derived default
    assert by_id[1].user_agent != "custom-agent/9.9"


def test_blank_persona_override_fails_loudly(rotator, pool_env, monkeypatch):
    monkeypatch.setenv("PERSONA_3_FINGERPRINT", "   ")
    with pytest.raises(ValueError, match="PERSONA_3_FINGERPRINT"):
        rotator.build_node_pool()


def make_persona_transport(rotator, nodes, script):
    ledger = rotator.HealthLedger(nodes, 30.0, 300.0)
    selector = rotator.NodeSelector(nodes, ledger)
    sleeper = RecordingSleeper()
    session = FakeSession(script)
    transport = failover.FailoverTransport(
        selector, ledger, session=session, sleep=sleeper, rng=stub_rng,
        max_retries=10,
    )
    return transport, session


def test_every_attempt_carries_its_own_nodes_persona(rotator):
    """The core invariant: attributes move together — an attempt through
    node k presents exactly persona k's User-Agent, whatever the client sent."""
    nodes = [
        rotator.Node(node_id=1, proxy="socks5h://n1:1080", api_key="k1",
                     user_agent="persona-one/1.0"),
        rotator.Node(node_id=2, proxy="socks5h://n2:1080", api_key="k2",
                     user_agent="persona-two/2.0"),
    ]
    ok = [FakeResponse(status=200, content=b"ok")] * 6
    t, session = make_persona_transport(rotator, nodes, ok)

    client_headers = {"User-Agent": "leaky-client/0.1"}
    for _ in range(6):
        result = t.send("POST", "http://up.test/v1/chat/completions",
                        headers=dict(client_headers))
        assert isinstance(result, failover.SendResult)

    by_proxy = {n.proxy: n for n in nodes}
    for call in session.calls:
        node = by_proxy[call["proxies"]["http"]]
        assert call["headers"]["User-Agent"] == node.user_agent
        assert call["headers"]["User-Agent"] != "leaky-client/0.1"


def test_health_exposes_persona_fields(client):
    resp = client.get("/health")
    entry = resp.get_json()["nodes"][0]
    assert entry["user_agent"]
    assert entry["fingerprint"]
