import json
import logging
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rotator import HealthLedger, Node


def make_ledger(count=2):
    nodes = [Node(node_id=i + 1, proxy=f"socks5h://10.0.0.{i+1}:1055", api_key="k") for i in range(count)]
    return nodes, HealthLedger(nodes, cooldown_base=30.0, cooldown_max=300.0)


class TestLedgerCounters:
    def test_record_success_counts(self):
        nodes, ledger = make_ledger()
        ledger.record_success(nodes[0])
        ledger.record_success(nodes[0])
        assert ledger.health_state()[1]["total_successes"] == 2

    def test_record_failure_counts(self):
        nodes, ledger = make_ledger()
        ledger.record_failure(nodes[1])
        ledger.record_failure(nodes[1])
        ledger.record_failure(nodes[1])
        assert ledger.health_state()[2]["total_failures"] == 3

    def test_counters_start_at_zero_and_are_per_node(self):
        _, ledger = make_ledger()
        state = ledger.health_state()
        assert all(e["total_successes"] == 0 and e["total_failures"] == 0 for e in state.values())

    def test_reset_all_keeps_counters(self):
        """Counters are observability, not health state: reset_all clears
        cooldowns but lifetime outcomes must survive."""
        nodes, ledger = make_ledger()
        ledger.record_failure(nodes[0])
        ledger.record_success(nodes[1])
        ledger.reset_all()
        state = ledger.health_state()
        assert state[1]["total_failures"] == 1
        assert state[2]["total_successes"] == 1
        assert state[1]["cooldown_seconds"] == 0.0


class TestSnapshotCounterFields:
    def test_snapshot_rows_carry_totals(self):
        from rotator import node_health_snapshot

        nodes, ledger = make_ledger()
        ledger.record_failure(nodes[0])
        rows = node_health_snapshot(nodes, ledger)
        by_id = {r["node_id"]: r for r in rows}
        assert by_id[1]["total_failures"] == 1
        assert by_id[1]["total_successes"] == 0
        assert by_id[2]["total_failures"] == 0


class TestRequestId:
    def test_response_carries_generated_hex_id(self, client):
        resp = client.get("/health")
        rid = resp.headers.get("X-Request-Id")
        assert rid is not None
        assert len(rid) == 32 and int(rid, 16) >= 0

    def test_inbound_id_is_honored(self, client):
        resp = client.get("/health", headers={"X-Request-Id": "my-trace-123"})
        assert resp.headers.get("X-Request-Id") == "my-trace-123"

    def test_inbound_id_whitespace_stripped(self, client):
        resp = client.get("/health", headers={"X-Request-Id": "  abc  "})
        assert resp.headers.get("X-Request-Id") == "abc"

    def test_unsafe_inbound_id_replaced_not_echoed(self, client):
        evil = "x" * 200
        resp = client.get("/health", headers={"X-Request-Id": evil})
        rid = resp.headers.get("X-Request-Id")
        assert rid != evil and len(rid) == 32

    def test_auth_error_carries_id_in_header_and_body(self, rotator, monkeypatch, client):
        import dataclasses

        monkeypatch.setattr(rotator, "settings",
                            dataclasses.replace(rotator.settings, auth_token="sekrit"))
        resp = client.post("/v1/chat/completions", json={},
                           headers={"X-Request-Id": "corr-42"})
        assert resp.status_code == 401
        assert resp.headers.get("X-Request-Id") == "corr-42"
        assert json.loads(resp.data)["request_id"] == "corr-42"

    def test_gateway_error_body_carries_request_id(self, rotator, client, monkeypatch):
        """All-nodes-exhausted 502 names the request id in its JSON body."""
        from failover import AllNodesFailed
        import rotator as rotator_module

        class ExhaustedTransport:
            def send(self, *args, **kwargs):
                return AllNodesFailed(last_error="boom", attempts=4)

        monkeypatch.setattr(rotator_module, "transport", ExhaustedTransport())
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Request-Id": "corr-502"},
        )
        assert resp.status_code == 502
        assert json.loads(resp.data)["request_id"] == "corr-502"
        assert resp.headers.get("X-Request-Id") == "corr-502"
