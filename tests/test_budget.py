"""Budget-aware routing tests (issue #48): quota-header parsing, ledger
budget state with deterministic clocks, headroom-class selection ordering,
and /health exposure — no real time, no HTTP."""

import pytest

import failover

from test_failover import FakeResponse, FakeSession, RecordingSleeper, stub_rng


def test_parse_duration_seconds_variants():
    assert failover.parse_duration_seconds("1s") == 1.0
    assert failover.parse_duration_seconds("6m0s") == 360.0
    assert failover.parse_duration_seconds("1h2m3s") == 3723.0
    assert failover.parse_duration_seconds("2.5s") == 2.5
    assert failover.parse_duration_seconds(None) is None
    assert failover.parse_duration_seconds("") is None
    assert failover.parse_duration_seconds("soon") is None
    assert failover.parse_duration_seconds("2026-08-23T00:00:00Z") is None


def test_parse_quota_headers_min_of_dimensions():
    headers = {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "50",
        "x-ratelimit-reset-requests": "1s",
        "x-ratelimit-limit-tokens": "200",
        "x-ratelimit-remaining-tokens": "10",   # 5%: the binding dimension
        "x-ratelimit-reset-tokens": "30s",
    }
    quota = failover.parse_quota_headers(headers)
    assert quota["remaining_pct"] == pytest.approx(5.0)
    assert quota["reset_seconds"] == 30.0  # conservative: longest window

    requests_only = {k: v for k, v in headers.items() if "tokens" not in k}
    assert failover.parse_quota_headers(requests_only)["remaining_pct"] == 50.0
    assert failover.parse_quota_headers({"x-ratelimit-limit-requests": "100"}) is None
    assert failover.parse_quota_headers({}) is None
    junk = {"x-ratelimit-limit-requests": "abc", "x-ratelimit-remaining-requests": "1"}
    assert failover.parse_quota_headers(junk) is None


def make_budget_transport(rotator, nodes, script, clock=None):
    ledger = rotator.HealthLedger(nodes, 30.0, 300.0)
    selector = rotator.NodeSelector(nodes, ledger)
    session = FakeSession(script)
    transport = failover.FailoverTransport(
        selector, ledger, session=session, sleep=RecordingSleeper(),
        rng=stub_rng, clock=clock or (lambda: 100.0),
    )
    return transport, ledger, selector, session


def test_successful_response_records_headroom(rotator):
    node = rotator.Node(node_id=1, proxy="socks5h://n1:1080", api_key="k")
    ok = FakeResponse(status=200, content=b"ok", headers={
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "10",
        "x-ratelimit-reset-requests": "6m0s",
    })
    t, ledger, _, _ = make_budget_transport(rotator, [node], [ok])

    t.send("POST", "http://up.test/v1/chat/completions", headers={})

    # Recorded against the injected clock (~100); reset lands at ~460.
    assert ledger.budget_remaining_pct(node, now=200.0) == pytest.approx(10.0)
    assert ledger.budget_class(node, now=200.0) == "low"
    # Window refills after the reset duration.
    assert ledger.budget_class(node, now=500.0) is None


def test_budget_survives_headerless_success_but_expires_without_reset(rotator):
    """A success without quota headers keeps the previous opinion; an
    opinion from a provider that omits reset headers expires on the default
    TTL instead of living forever (a starved node must be able to recover)."""
    node = rotator.Node(node_id=1, proxy="socks5h://n1:1080", api_key="k")
    with_headers = FakeResponse(status=200, content=b"ok", headers={
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "0",   # depleted
        # no reset header -> default TTL
    })
    plain = FakeResponse(status=200, content=b"ok")
    t, ledger, _, _ = make_budget_transport(
        rotator, [node], [with_headers, plain])

    t.send("POST", "http://up.test/v1/chat/completions", headers={})
    t.send("POST", "http://up.test/v1/chat/completions", headers={})

    assert ledger.budget_class(node, now=130.0) == "depleted"  # survived
    assert ledger.budget_class(node, now=200.0) is None        # TTL expired


def test_selector_prefers_headroom_but_keeps_cursor_order_within_class(rotator):
    nodes = [
        rotator.Node(node_id=1, proxy="socks5h://n1:1080", api_key="k1"),
        rotator.Node(node_id=2, proxy="socks5h://n2:1080", api_key="k2"),
        rotator.Node(node_id=3, proxy="socks5h://n3:1080", api_key="k3"),
    ]
    ledger = rotator.HealthLedger(nodes, 30.0, 300.0)
    selector = rotator.NodeSelector(nodes, ledger)

    # Cursor starts at node 1. Node 1 reported healthy; node 2 low: the scan
    # passes node 2 to find the healthy one.
    ledger.record_quota(nodes[1], remaining_pct=80, reset_seconds=600, now=100.0)
    ledger.record_quota(nodes[2], remaining_pct=10, reset_seconds=600, now=100.0)
    assert selector.select(now=100.0) is nodes[1]
    # Depleted never wins while anything else is usable...
    ledger.record_quota(nodes[2], remaining_pct=0, reset_seconds=600, now=100.0)
    selector._index = 0
    assert selector.select(now=100.0) is nodes[1]
    # ...but within the same class the cursor order is preserved: after the
    # healthy pick the cursor sits past it, so the low node (id 2) precedes
    # the unreported one (id 3, same rank).
    ledger.record_quota(nodes[1], remaining_pct=10, reset_seconds=600, now=100.0)
    selector._index = 0
    first = selector.select(now=100.0)   # healthy wins despite cursor order
    second = selector.select(now=100.0)  # cursor now at id2
    assert (first, second) == (nodes[0], nodes[1])


def test_all_healthy_pool_matches_historical_round_robin(rotator):
    """No quota reports anywhere -> selection identical to plain round-robin."""
    nodes = [
        rotator.Node(node_id=i, proxy=f"socks5h://n{i}:1080", api_key=f"k{i}")
        for i in range(1, 4)
    ]
    ledger = rotator.HealthLedger(nodes, 30.0, 300.0)
    selector = rotator.NodeSelector(nodes, ledger)
    picked = [selector.select() for _ in range(6)]
    assert [n.node_id for n in picked] == [1, 2, 3, 1, 2, 3]


def test_health_exposes_budget_pct(client, mock):
    """End-to-end: a scripted upstream response carrying quota headers shows
    up in /health for that node."""
    mock.script = [(200, {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "40",
    }, {"id": "cmpl-x", "choices": [], "usage": {}})]
    resp = client.post("/v1/chat/completions",
                       json={"model": "gpt-4o",
                             "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200

    health = client.get("/health").get_json()
    entry = next(n for n in health["nodes"] if n["node_id"] == 2)
    assert entry["budget_remaining_pct"] == pytest.approx(40.0)
    others = [n for n in health["nodes"] if n["node_id"] != 2]
    assert all(n["budget_remaining_pct"] is None for n in others)
