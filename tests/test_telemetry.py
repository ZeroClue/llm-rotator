"""Telemetry unit tests (issue #61): reason buckets, request ring, and
lifetime counters on a deterministic clock — no HTTP, no real time."""

import pytest

from telemetry import REASONS, Telemetry


class FakeClock:
    def __init__(self, start=0.0):
        self.now = float(start)

    def __call__(self):
        return self.now


def make_telemetry(start=0.0):
    clock = FakeClock(start)
    return Telemetry(clock=clock), clock


def test_bucket_counts_and_token_sums():
    t, _ = make_telemetry()
    t.record_outcome(1, "ok")
    t.record_outcome(1, "ok")
    t.record_outcome(1, "rate_limited")

    window = t.node_window(1)
    current = window[-1]
    assert current["ok"] == 2
    assert current["fail"] == 1
    assert current["tokens"] == 0


def test_bucket_rolls_and_prunes_past_retention():
    t, clock = make_telemetry(start=0.0)
    t.record_outcome(1, "ok")                    # minute 0
    clock.now = 60 * 59                          # minute 59: minute 0 survives
    t.record_outcome(1, "ok")
    assert len(t.node_window(1)) == 60
    assert t.node_window(1)[0]["ok"] == 1        # oldest slot still counted

    clock.now = 60 * 60                          # minute 60: minute 0 pruned
    t.record_outcome(1, "ok")
    window = t.node_window(1)
    assert len(window) == 60
    assert window[0]["minute_ts"] == 60 * 1      # window slid forward
    assert window[0]["ok"] == 0                  # minute-0 data gone


def test_node_window_zero_fills_empty_minutes():
    t, clock = make_telemetry(start=60 * 10)
    t.record_outcome(1, "ok")
    clock.now = 60 * 12                          # skip minute 11 entirely
    t.record_outcome(1, "ok")

    window = t.node_window(1)
    assert len(window) == 60
    assert window[-1]["ok"] == 1
    assert window[-2]["ok"] == 0                 # the skipped minute
    assert window[-2]["minute_ts"] == 60 * 11


def test_last_error_set_and_kept_after_recovery():
    t, _ = make_telemetry()
    t.record_outcome(1, "timeout")
    t.record_outcome(1, "ok")  # recovery must not erase "failed Nm ago"

    error = t.last_error(1)
    assert error["reason"] == "timeout"
    assert t.last_error(2) is None


def test_request_ring_eviction_and_snapshot_order():
    t, _ = make_telemetry()
    for i in range(250):
        t.record_request(request_id=str(i), method="POST",
                         path="/v1/chat/completions", node_id=1,
                         outcome="ok", status_code=200)

    snapshot = t.ring_snapshot()
    assert len(snapshot) == 200
    assert snapshot[0]["request_id"] == "249"    # newest first
    assert snapshot[-1]["request_id"] == "50"    # oldest evicted


def test_streamed_entry_updated_in_place():
    t, _ = make_telemetry()
    entry = t.record_request(request_id="r1", method="POST",
                             path="/v1/chat/completions", node_id=2,
                             outcome="ok", status_code=200)

    # On send-return: no timings yet.
    assert t.ring_snapshot()[0]["ttfb_ms"] is None

    # First chunk surfaces, then the stream ends.
    t.complete_request(entry, ttfb_ms=12.5)
    t.complete_request(entry, total_ms=90.0, tokens={
        "prompt": 10, "completion": 5, "total": 15})

    stored = t.ring_snapshot()[0]
    assert stored["ttfb_ms"] == 12.5
    assert stored["total_ms"] == 90.0
    assert stored["tokens"] == {"prompt": 10, "completion": 5, "total": 15}


def test_tokens_feed_buckets_and_lifetime():
    t, _ = make_telemetry()
    entry = t.record_request(request_id="r1", method="POST",
                             path="/v1/chat/completions", node_id=3,
                             outcome="ok", status_code=200)
    t.complete_request(entry, tokens={
        "prompt": 100, "completion": 20, "total": 120})
    t.record_outcome(3, "ok")

    assert t.node_window(3)[-1]["tokens"] == 120
    aggregates = t.aggregates()
    assert aggregates["tokens_lifetime"] == 120
    assert aggregates["tokens_last_hour"] == 120
    assert aggregates["requests_last_hour"] == 1


def test_aggregates_only_count_window():
    t, clock = make_telemetry(start=0.0)
    entry = t.record_request(request_id="old", method="POST",
                             path="/v1/chat/completions", node_id=1,
                             outcome="ok", status_code=200)
    t.complete_request(entry, tokens={"prompt": 1, "completion": 1,
                                      "total": 500})
    clock.now = 60 * 61  # an hour later: the old bucket fell out

    aggregates = t.aggregates()
    assert aggregates["tokens_last_hour"] == 0
    assert aggregates["tokens_lifetime"] == 500  # lifetime never expires


def test_unknown_reason_rejected():
    t, _ = make_telemetry()
    with pytest.raises(ValueError, match="Unknown reason"):
        t.record_outcome(1, "kind_of_fine")


def test_taxonomy_covers_all_documented_reasons():
    assert set(REASONS) == {"ok", "rate_limited", "server_error",
                            "timeout", "connection", "error"}
