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
        assert "2s" in html            # countdown-first format
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


def test_config_fragment_masks_token_and_labels_env_vars(client, rotator):
    masked = dataclasses.replace(rotator.settings, auth_token="super-secret")
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(rotator, "settings", masked)
    try:
        html = client.get("/admin/fragments/config").get_data(as_text=True)
    finally:
        monkeypatched.undo()

    assert "****" in html
    assert "super-secret" not in html
    assert "PROXY_AUTH_TOKEN" in html  # env-var labels present
    assert "ANONYMITY_FAILOVER" in html


def test_admin_exempt_from_bearer_gate(rotator, monkeypatch):
    import dataclasses
    gated = dataclasses.replace(rotator.settings, auth_token="sekrit")
    monkeypatch.setattr(rotator, "settings", gated)
    client = rotator.app.test_client()

    # Dashboard routes open; proxy still gated.
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/fragments/nodes").status_code == 200
    assert client.get("/admin/fragments/config").status_code == 200
    assert client.post("/v1/chat/completions",
                       json={"model": "gpt-4o",
                             "messages": [{"role": "user", "content": "hi"}]}
                       ).status_code == 401
