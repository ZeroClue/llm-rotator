"""Process-local telemetry for the operator dashboard (spec §4, issue #61).

Owns the request ring and the per-node reason buckets under one RLock.
Fed by the view layer; the transport never imports this module. No
persistence: everything dies with the process, by design.

The clock is injectable (deterministic tests); it only needs to be
monotonic-ish and divisible into 60-second minutes — time.monotonic and
time.time both qualify.
"""
import threading
import time
from collections import Counter, deque

# Per-attempt outcome taxonomy (spec §4.1). Transport-level: a completed
# non-retryable status (401 etc.) is "ok" — the status code rides along
# for display.
REASONS = ("ok", "rate_limited", "server_error", "timeout", "connection",
           "error")

MINUTE = 60


class Telemetry:
    """Request ring + per-node reason buckets + lifetime counters."""

    def __init__(self, *, ring_maxlen=200, window_minutes=60,
                 clock=time.monotonic, wall_clock=time.time):
        self._clock = clock
        self._wall_clock = wall_clock
        self._window_minutes = window_minutes
        self._ring = deque(maxlen=ring_maxlen)
        # node_id -> {minute: Counter({reason: n}), "tokens": int}
        self._buckets = {}
        # node_id -> {"reason": str, "ts": clock} — last failure, kept even
        # after later successes ("failed 4m ago" must survive recovery).
        self._last_error = {}
        # node_id -> Counter over reasons, lifetime (Prometheus counters).
        self._lifetime = {}
        self._tokens_lifetime = 0
        self._lock = threading.RLock()

    # ── request ring ─────────────────────────────────────────────────────
    def record_request(self, *, request_id, method, path, node_id, outcome,
                       status_code=None, attempts=None):
        """Append a ring entry and return it. Streamed requests: the view
        keeps the reference and calls complete_request() when the stream
        ends (update-in-place, spec §4.3)."""
        entry = {
            "ts": self._clock(),
            # Wall-clock twin for UI display ("absolute on hover") — the
            # monotonic ts orders the ring but can't render a date.
            "wall_ts": self._wall_clock(),
            "request_id": request_id,
            "method": method,
            "path": path,
            "node_id": node_id,
            "outcome": outcome,
            "status_code": status_code,
            "ttfb_ms": None,
            "total_ms": None,
            "tokens": None,
            "attempts": list(attempts or []),
        }
        with self._lock:
            self._ring.append(entry)
        return entry

    def complete_request(self, entry, *, ttfb_ms=None, total_ms=None,
                         tokens=None):
        """Fill in timing/tokens on a previously recorded entry. Token
        totals also land in the current minute bucket and the lifetime
        counter (the only place tokens enter telemetry)."""
        with self._lock:
            if ttfb_ms is not None:
                entry["ttfb_ms"] = ttfb_ms
            if total_ms is not None:
                entry["total_ms"] = total_ms
            if tokens:
                entry["tokens"] = dict(tokens)
                self._tokens_lifetime += int(tokens.get("total", 0))
                node_id = entry["node_id"]
                if node_id is not None:
                    bucket = self._minute_bucket(node_id)
                    bucket["tokens"] += int(tokens.get("total", 0))

    def ring_snapshot(self):
        """Newest-first copy of the ring, taken under one lock."""
        with self._lock:
            return [dict(entry, attempts=list(entry["attempts"]))
                    for entry in reversed(self._ring)]

    # ── reason buckets ───────────────────────────────────────────────────
    def record_outcome(self, node_id, reason, *, tokens=0):
        """Count one attempt outcome into the current minute bucket and the
        lifetime counters. Non-ok reasons also update last-error."""
        if reason not in REASONS:
            raise ValueError(f"Unknown reason: {reason!r}")
        now = self._clock()
        with self._lock:
            bucket = self._minute_bucket(node_id, now=now)
            bucket[reason] += 1
            bucket["tokens"] += int(tokens or 0)
            self._lifetime.setdefault(node_id, Counter())[reason] += 1
            if reason != "ok":
                self._last_error[node_id] = {"reason": reason, "ts": now}

    def _minute_bucket(self, node_id, now=None):
        """Current-minute bucket for a node; prunes windows older than the
        retention period. Caller holds the lock."""
        if now is None:
            now = self._clock()
        minute = int(now // MINUTE)
        node_buckets = self._buckets.setdefault(node_id, {})
        for stale in [m for m in node_buckets if m < minute - self._window_minutes + 1]:
            del node_buckets[stale]
        return node_buckets.setdefault(minute, Counter())

    def node_window(self, node_id, now=None):
        """60-slot per-minute history for a node's sparkline: oldest first,
        empty minutes zero-filled — [{minute_ts, ok, fail, tokens}] x N."""
        if now is None:
            now = self._clock()
        current = int(now // MINUTE)
        with self._lock:
            node_buckets = self._buckets.get(node_id, {})
            slots = []
            for offset in range(self._window_minutes - 1, -1, -1):
                minute = current - offset
                counts = node_buckets.get(minute, Counter())
                ok = counts.get("ok", 0)
                total = sum(counts.get(r, 0) for r in REASONS)
                slots.append({
                    "minute_ts": minute * MINUTE,
                    "ok": ok,
                    "fail": total - ok,
                    "tokens": counts.get("tokens", 0),
                })
            return slots

    def last_error(self, node_id):
        with self._lock:
            error = self._last_error.get(node_id)
            return dict(error) if error else None

    # ── aggregates & counters ────────────────────────────────────────────
    def aggregates(self, now=None):
        """Fleet-level numbers for the chrome aggregate line: tokens last
        hour (bucket sums) / lifetime, requests last hour."""
        if now is None:
            now = self._clock()
        current = int(now // MINUTE)
        oldest = current - self._window_minutes + 1
        tokens_hour = requests_hour = 0
        with self._lock:
            for node_buckets in self._buckets.values():
                for minute, counts in node_buckets.items():
                    if minute >= oldest:
                        total = sum(counts.get(r, 0) for r in REASONS)
                        requests_hour += total
                        tokens_hour += counts.get("tokens", 0)
            return {
                "tokens_last_hour": tokens_hour,
                "tokens_lifetime": self._tokens_lifetime,
                "requests_last_hour": requests_hour,
            }

    def lifetime_attempts(self):
        """{node_id: Counter({reason: n})} lifetime, for /metrics."""
        with self._lock:
            return {nid: dict(counter) for nid, counter in self._lifetime.items()}
