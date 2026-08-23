import json
import logging
import sys
import os
from dataclasses import replace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rotator import HealthLedger, Node
from failover import AllNodesFailed


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
        monkeypatch.setattr(rotator, "settings",
                            replace(rotator.settings, auth_token="sekrit"))
        resp = client.post("/v1/chat/completions", json={},
                           headers={"X-Request-Id": "corr-42"})
        assert resp.status_code == 401
        assert resp.headers.get("X-Request-Id") == "corr-42"
        assert json.loads(resp.data)["request_id"] == "corr-42"

    def test_gateway_error_body_carries_request_id(self, rotator, client, monkeypatch):
        """All-nodes-exhausted 502 names the request id in its JSON body."""
        import rotator as rotator_module

        class ExhaustedTransport:
            def send(self, *args, **kwargs):
                return AllNodesFailed(last_error="boom", attempt_count=4, attempts=[])

        monkeypatch.setattr(rotator_module, "transport", ExhaustedTransport())
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Request-Id": "corr-502"},
        )
        assert resp.status_code == 502
        assert json.loads(resp.data)["request_id"] == "corr-502"
        assert resp.headers.get("X-Request-Id") == "corr-502"


class TestStructuredLifecycleLogs:
    def test_attempt_logs_carry_node_attempt_and_request_id(self, caplog):
        """Scripted fake session, house style from test_failover: a 429 then
        success must leave structured fields on the attempt log records."""
        from failover import FailoverTransport

        class FakeResp:
            def __init__(self, status):
                self.status_code = status
                self.headers = {}

            def close(self):
                pass

        class FakeSession:
            def __init__(self, statuses):
                self._statuses = list(statuses)

            def request(self, **kwargs):
                return FakeResp(self._statuses.pop(0))

        class FakeSelector:
            def __init__(self, nodes):
                self.nodes = nodes

            def select(self):
                return self.nodes[0]

        class FakeLedger:
            def record_success(self, node):
                pass

            def record_failure(self, node):
                pass

        nodes = [Node(node_id=1, proxy="socks5h://10.0.0.1:1055", api_key="k")]
        transport = FailoverTransport(
            selector=FakeSelector(nodes), ledger=FakeLedger(),
            session=FakeSession([429, 200]), sleep=lambda s: None,
            max_retries=3,
        )
        with caplog.at_level(logging.INFO, logger="failover"):
            result = transport.send(
                "POST", "http://upstream/v1/chat/completions", headers={},
                payload=b"{}", request_id="corr-99",
            )
        assert not isinstance(result, AllNodesFailed)
        attempt_events = [
            r for r in caplog.records if getattr(r, "event", None) == "upstream_attempt"
        ]
        assert len(attempt_events) == 2
        first = attempt_events[0]
        assert first.node_id == 1 and first.attempt == 1
        assert first.request_id == "corr-99"
        failures = [
            r for r in caplog.records if getattr(r, "event", None) == "upstream_failure"
        ]
        assert failures and failures[0].status_code == 429

    def test_proxy_request_log_carries_request_id(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="rotator"):
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
                headers={"X-Request-Id": "trace-7"},
            )
        assert resp.status_code == 200
        entries = [r for r in caplog.records if getattr(r, "event", None) == "proxy_request"]
        assert entries and entries[0].request_id == "trace-7"
        assert entries[0].path == "chat/completions"


class TestJsonLogging:
    def test_settings_default_text(self, monkeypatch):
        from rotator import Settings

        monkeypatch.delenv("LOG_FORMAT", raising=False)
        assert Settings.from_env().log_format == "text"

    def test_settings_json_env(self, monkeypatch):
        from rotator import Settings

        monkeypatch.setenv("LOG_FORMAT", "json")
        assert Settings.from_env().log_format == "json"

    def test_json_formatter_envelope_and_extras(self):
        from rotator import JsonFormatter

        record = logging.LogRecord(
            "failover", logging.INFO, __file__, 1,
            "Attempt 1/4: Routing via Node 1", (), None,
        )
        record.event = "upstream_attempt"
        record.node_id = 1
        record.attempt = 1
        record.request_id = "corr-1"
        out = json.loads(JsonFormatter().format(record))
        assert out["message"] == "Attempt 1/4: Routing via Node 1"
        assert out["level"] == "INFO"
        assert out["logger"] == "failover"
        assert out["event"] == "upstream_attempt"
        assert out["node_id"] == 1 and out["attempt"] == 1
        assert out["request_id"] == "corr-1"

    def test_json_formatter_ts_is_iso8601(self):
        from datetime import datetime

        from rotator import JsonFormatter

        record = logging.LogRecord("rotator", logging.WARNING, __file__, 1, "w", (), None)
        out = json.loads(JsonFormatter().format(record))
        datetime.fromisoformat(out["ts"])

    def test_configure_logging_installs_json_formatter(self):
        from rotator import Settings, configure_logging

        configure_logging(replace(Settings.from_env(), log_format="json"))
        root = logging.getLogger()
        assert any(getattr(h, "formatter", None).__class__.__name__ == "JsonFormatter"
                   for h in root.handlers)

    def test_configure_logging_text_keeps_default_formatter(self, monkeypatch):
        from rotator import Settings, configure_logging

        cfg = Settings.from_env()
        configure_logging(replace(cfg, log_format="text"))
        root = logging.getLogger()
        assert any(getattr(h, "formatter", None).__class__.__name__ != "JsonFormatter"
                   for h in root.handlers)


class TestMetrics:
    def test_metrics_lines_match_ledger_state(self, rotator, client):
        """Delta-based: counters are process-lifetime and deliberately
        survive reset_all(), so compare against a baseline scrape."""
        import rotator as rotator_module

        def counters(body):
            out = {}
            for line in body.splitlines():
                if line.startswith("llm_rotator_node_requests_total{"):
                    labels, value = line.split("} ", 1)
                    nid = labels.split('node_id="')[1].split('"')[0]
                    outcome = labels.split('outcome="')[1].split('"')[0]
                    out[(int(nid), outcome)] = int(value)
            return out

        before = counters(client.get("/metrics").data.decode())
        ledger = rotator_module.health_ledger
        nodes = {n.node_id: n for n in rotator_module.NODE_POOL}
        ledger.record_success(nodes[1])
        ledger.record_success(nodes[1])
        ledger.record_failure(nodes[2])

        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["Content-Type"]
        body = resp.data.decode()
        assert "# TYPE llm_rotator_node_requests_total counter" in body
        after = counters(body)
        assert after[(1, "success")] == before.get((1, "success"), 0) + 2
        assert after[(2, "failure")] == before.get((2, "failure"), 0) + 1

    def test_metrics_gauge_counts_usable_nodes(self, rotator, client):
        resp = client.get("/metrics")
        body = resp.data.decode()
        line = next(l for l in body.splitlines() if l.startswith("llm_rotator_nodes_available "))
        value = int(line.rsplit(" ", 1)[1])
        assert 0 <= value <= len(rotator.NODE_POOL)

    def test_metrics_stays_open_with_auth_enabled(self, rotator, monkeypatch, client):
        monkeypatch.setattr(rotator, "settings",
                            replace(rotator.settings, auth_token="sekrit"))
        assert client.get("/metrics").status_code == 200


class TestReviewFixes:
    def test_inbound_request_id_never_reaches_upstream(self, mock, client):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Request-Id": "client-trace-1"},
        )
        assert resp.status_code == 200
        captured_headers = mock.chat_posts()[-1]["headers"]
        assert not any(k.lower() == "x-request-id" for k in captured_headers)

    def test_all_nodes_failed_log_carries_attempts(self, rotator, client, caplog, monkeypatch):
        import rotator as rotator_module

        class ExhaustedTransport:
            def send(self, *args, **kwargs):
                return AllNodesFailed(last_error="boom", attempt_count=4, attempts=[])

        monkeypatch.setattr(rotator_module, "transport", ExhaustedTransport())
        with caplog.at_level(logging.CRITICAL, logger="rotator"):
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
                headers={"X-Request-Id": "corr-x"},
            )
        assert resp.status_code == 502
        events = [r for r in caplog.records if getattr(r, "event", None) == "all_nodes_failed"]
        assert events and events[0].attempts == 4  # log extra keeps the count name
        assert events[0].request_id == "corr-x"

    def test_token_usage_log_carries_counts(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="rotator"):
            client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            )
        usage_events = [
            r for r in caplog.records if getattr(r, "event", None) == "token_usage"
        ]
        assert usage_events
        assert usage_events[0].total_tokens == 10

    def test_unhandled_exception_log_carries_request_id(self, rotator, client, caplog, monkeypatch):
        import rotator as rotator_module

        class ExplodingTransport:
            def send(self, *args, **kwargs):
                raise RuntimeError("kaboom")

        monkeypatch.setattr(rotator_module, "transport", ExplodingTransport())
        with caplog.at_level(logging.ERROR, logger="rotator"):
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
                headers={"X-Request-Id": "corr-boom"},
            )
        assert resp.status_code == 500
        records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert records and getattr(records[0], "request_id", "") == "corr-boom"
