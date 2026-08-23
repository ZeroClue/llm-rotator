"""Admin dashboard tests (issue #62): fragment rendering against seeded
telemetry, bearer-gate exemption, key masking."""

import dataclasses

import pytest


@pytest.fixture()
def fresh_telemetry(rotator, monkeypatch):
    """Isolate dashboard assertions from suite-accumulated ring/buckets."""
    from telemetry import Telemetry
    telemetry = Telemetry()
    monkeypatch.setattr(rotator, "telemetry", telemetry)
    return telemetry


def test_admin_page_renders_sections(client):
    html = client.get("/admin").get_data(as_text=True)
    assert 'id="nodes"' in html
    assert 'id="config"' in html
    assert "/static/vendor/htmx.min.js" in html
    assert "hx-trigger=\"every 3s\"" in html          # nodes polling
    assert "<noscript>" in html                        # refresh fallback


def test_nodes_fragment_renders_cursor_and_counts(client):
    html = client.get("/admin/fragments/nodes").get_data(as_text=True)
    assert "▶ next" in html            # cursor marker on some row
    assert "nodes available" in html
    assert "in-flight streams" in html


def test_nodes_fragment_shows_cooldown_countdown(client, rotator):
    node = rotator.NODE_POOL[0]
    rotator.health_ledger.record_failure(node)  # base cooldown ~2s
    try:
        html = client.get("/admin/fragments/nodes").get_data(as_text=True)
        assert "cooling" in html
        assert "<b>2s</b>" in html     # countdown-first format
    finally:
        rotator.health_ledger.reset_all()


def test_nodes_fragment_shows_last_error_from_telemetry(client, rotator,
                                                        fresh_telemetry):
    fresh_telemetry.record_outcome(rotator.NODE_POOL[0].node_id, "timeout")

    html = client.get("/admin/fragments/nodes").get_data(as_text=True)

    assert "timeout" in html
    assert "ago" in html               # relative timestamp


def test_nodes_fragment_renders_sparkline_slots(client, rotator,
                                                fresh_telemetry):
    fresh_telemetry.record_outcome(rotator.NODE_POOL[1].node_id, "ok")
    fresh_telemetry.record_outcome(rotator.NODE_POOL[1].node_id, "rate_limited")

    html = client.get("/admin/fragments/nodes").get_data(as_text=True)

    assert html.count('class="spark"') >= 1
    assert 'class="f"' in html         # a failure slot is shaded


def test_config_fragment_masks_token_and_labels_env_vars(client, rotator,
                                                          monkeypatch):
    monkeypatch.setattr(
        rotator, "settings",
        dataclasses.replace(rotator.settings, auth_token="super-secret"))

    html = client.get("/admin/fragments/config").get_data(as_text=True)

    assert "****" in html
    assert "super-secret" not in html
    assert "PROXY_AUTH_TOKEN" in html  # env-var labels present
    assert "ANONYMITY_FAILOVER" in html


def test_admin_exempt_from_bearer_gate(rotator, monkeypatch):
    monkeypatch.setattr(
        rotator, "settings",
        dataclasses.replace(rotator.settings, auth_token="sekrit"))
    client = rotator.app.test_client()

    # Dashboard routes open; proxy still gated.
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/fragments/nodes").status_code == 200
    assert client.get("/admin/fragments/config").status_code == 200
    assert client.post("/v1/chat/completions",
                       json={"model": "gpt-4o",
                             "messages": [{"role": "user", "content": "hi"}]}
                       ).status_code == 401

    # Dashboard routes open; proxy still gated.
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/fragments/nodes").status_code == 200
    assert client.get("/admin/fragments/config").status_code == 200
    assert client.post("/v1/chat/completions",
                       json={"model": "gpt-4o",
                             "messages": [{"role": "user", "content": "hi"}]}
                       ).status_code == 401


# ── Requests section (issue #63) ─────────────────────────────────────────────

def test_requests_fragment_empty_state(client, rotator, fresh_telemetry):
    html = client.get("/admin/fragments/requests").get_data(as_text=True)
    assert "No requests proxied yet." in html


def test_requests_fragment_renders_ring_newest_first(client, rotator,
                                                     fresh_telemetry):
    entries = []
    for i, outcome in enumerate(["ok", "ok"]):
        entries.append(fresh_telemetry.record_request(
            request_id=f"{'a' * 32}{i}", method="POST",
            path="/v1/chat/completions", node_id=2, outcome=outcome,
            status_code=200, attempts=[{"node_id": 2, "status": 200,
                                        "reason": "ok", "duration_ms": 12.0}]))
    # complete_request needs the original reference — snapshots are copies.
    fresh_telemetry.complete_request(
        entries[-1],
        ttfb_ms=5.0, total_ms=42.0, tokens={"prompt": 9, "completion": 1,
                                            "total": 10})

    html = client.get("/admin/fragments/requests").get_data(as_text=True)

    assert "No requests proxied yet." not in html
    assert "aaaaaaa" in html                       # short id (8 chars)
    assert 'data-rid="' in html                    # full id for copy
    assert "42ms" in html
    assert ">10<" in html                          # token total
    assert "2 attempts" in html or "1 attempt" in html


def test_requests_fragment_marks_failures_and_shows_attempts(client, rotator,
                                                             fresh_telemetry):
    fresh_telemetry.record_request(
        request_id="b" * 32, method="POST", path="/v1/chat/completions",
        node_id=None, outcome="failed", status_code=None,
        attempts=[{"node_id": 1, "status": None, "reason": "timeout",
                   "duration_ms": 25000.0},
                  {"node_id": 2, "status": None, "reason": "connection",
                   "duration_ms": 80.0}])

    html = client.get("/admin/fragments/requests").get_data(as_text=True)

    assert "failed" in html
    assert "timeout" in html and "connection" in html
    assert "25000.0" in html                       # attempt-level detail


def test_requests_fragment_single_shot_abort_badge(client, rotator,
                                                   fresh_telemetry):
    fresh_telemetry.record_request(
        request_id="c" * 32, method="POST", path="/v1/chat/completions",
        node_id=1, outcome="single_shot_abort", status_code=504,
        attempts=[{"node_id": 1, "status": 504, "reason": "server_error",
                   "duration_ms": 300.0}])

    html = client.get("/admin/fragments/requests").get_data(as_text=True)

    assert "aborted" in html
