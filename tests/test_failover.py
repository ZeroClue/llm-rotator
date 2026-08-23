"""HTTP-free unit tests for the failover transport.

The fake session replays scripted outcomes (statuses or exceptions); the
recording sleeper and stubbed rng make retry pacing deterministic. No
sockets, no Flask, no real sleeps.
"""

import http.cookiejar
import json

import pytest
import requests

import failover


class FakeResponse:
    def __init__(self, status=200, headers=None, content=b"", chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self._content = content
        self._chunks = chunks
        self.closed = False
        self.iter_chunk_sizes = []

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size):
        self.iter_chunk_sizes.append(chunk_size)
        yield from self._chunks

    @property
    def content(self):
        return self._content


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, headers=None, data=None,
                proxies=None, timeout=None, stream=False):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "data": data,
            "proxies": proxies,
            "timeout": timeout,
            "stream": stream,
        })
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def call_count(self):
        return len(self.calls)


class RecordingSleeper:
    def __init__(self):
        self.sleeps = []

    def __call__(self, seconds):
        self.sleeps.append(seconds)


class FakeGate:
    """Semaphore stand-in: records acquire/release, never blocks."""

    def __init__(self, available=True):
        self.available = available
        self.acquires = 0
        self.releases = 0

    def acquire(self, blocking=False):
        self.acquires += 1
        return self.available

    def release(self):
        self.releases += 1


def stub_rng(lo, hi):
    return hi / 4  # deterministic quarter of the jitter band


def make_transport(rotator, script, count=2, *, max_retries=3, timeout=25.0,
                   backoff_base=0.5, backoff_max=8.0, use_real_session=False,
                   retry_posts=True, persona_hygiene=False,
                   anonymity_failover="cross", failover_max_wait=8.0,
                   wait_gate=None):
    nodes = [
        rotator.Node(node_id=i, proxy=f"socks5h://node{i}.ts.net:1080", api_key=f"key{i}")
        for i in range(1, count + 1)
    ]
    ledger = rotator.HealthLedger(nodes, 30.0, 300.0)
    selector = rotator.NodeSelector(nodes, ledger)
    sleeper = RecordingSleeper()
    session = None if use_real_session else FakeSession(script)
    transport = failover.FailoverTransport(
        selector, ledger,
        session=session,
        sleep=sleeper,
        rng=stub_rng,
        max_retries=max_retries,
        timeout=timeout,
        backoff_base=backoff_base,
        backoff_max=backoff_max,
        retry_posts=retry_posts,
        persona_hygiene=persona_hygiene,
        anonymity_failover=anonymity_failover,
        failover_max_wait=failover_max_wait,
        wait_gate=wait_gate,
    )
    return transport, ledger, sleeper, nodes, session or transport.session


@pytest.mark.parametrize("mode", [
    "502",
    "timeout",
    "connection_error",
    "request_exception",
])
def test_retry_posts_disabled_gives_post_exactly_one_attempt(rotator, mode):
    failure = {
        "502": lambda: FakeResponse(status=502),
        "timeout": lambda: requests.Timeout("stalled"),
        "connection_error": lambda: requests.ConnectionError("down"),
        "request_exception": lambda: requests.RequestException("boom"),
    }[mode]()
    rok_a, rok_b = FakeResponse(status=200), FakeResponse(status=200)
    t, ledger, sleeper, nodes, session = make_transport(
        rotator, [failure, rok_a, rok_b], max_retries=3, retry_posts=False)

    result = t.send("POST", "http://up.test/v1/chat/completions", headers={})

    assert isinstance(result, failover.AllNodesFailed)
    assert result.attempts == 1
    assert session.call_count == 1  # no failover attempt
    assert sleeper.sleeps == []     # no pacing
    assert not ledger.usable(nodes[0])  # outcome still recorded

    # The same failure still fails over for idempotent methods; the cooling
    # first node is skipped, so the GET lands on node 2 directly.
    result = t.send("GET", "http://up.test/v1/models", headers={})
    assert isinstance(result, failover.SendResult)
    assert session.call_count == 2


def test_all_nodes_failed_reports_true_attempt_count(rotator):
    script = [FakeResponse(status=502) for _ in range(3)]
    t, *_ , session = make_transport(rotator, script, max_retries=3)

    result = t.send("GET", "http://up.test/v1", headers={})

    assert isinstance(result, failover.AllNodesFailed)
    assert result.attempts == 3


def test_429_records_cooldown_and_retries_on_next_node(rotator):
    r429 = FakeResponse(status=429, headers={"Retry-After": "3"})
    rok = FakeResponse(status=200, content=b"ok")
    t, ledger, sleeper, nodes, session = make_transport(rotator, [r429, rok])

    result = t.send(
        "POST", "http://up.test/v1/chat/completions",
        headers={"Host": "up.test", "Content-Length": "9", "X-Client": "probe"},
        payload=b"123456789",
    )

    assert isinstance(result, failover.SendResult)
    assert result.status_code == 200
    assert result.node_id == 2
    assert result.body() == b"ok"

    # The 429'd node is cooling; the healthy one is untouched.
    assert not ledger.usable(nodes[0])
    assert ledger.usable(nodes[1])

    # Retry-After was honored as the first pacing sleep.
    assert sleeper.sleeps == [3.0]

    # Outbound hygiene: host/content-length dropped, key injected per node.
    first, second = session.calls[0], session.calls[1]
    assert first["headers"]["Authorization"] == "Bearer key1"
    assert "host" not in {k.lower() for k in first["headers"]}
    assert "content-length" not in {k.lower() for k in first["headers"]}
    assert first["headers"]["X-Client"] == "probe"
    assert second["headers"]["Authorization"] == "Bearer key2"
    assert first["proxies"] == {"http": nodes[0].proxy, "https": nodes[0].proxy}
    assert second["proxies"] == {"http": nodes[1].proxy, "https": nodes[1].proxy}


def test_exhaustion_reports_all_nodes_failed_with_backoff_sequence(rotator):
    script = [FakeResponse(status=502) for _ in range(3)]
    t, ledger, sleeper, nodes, session = make_transport(rotator, script, max_retries=3)

    result = t.send("POST", "http://up.test/v1", headers={})

    assert isinstance(result, failover.AllNodesFailed)
    assert result.last_error == "Upstream error: 502"
    assert session.call_count == 3
    # Pacing: backoff after attempts 0 and 1, none after the final attempt.
    assert sleeper.sleeps == [pytest.approx(0.625), pytest.approx(1.125)]  # 0.5·2ⁿ + 0.125


def test_timeout_and_connection_errors_classify_and_retry(rotator):
    script = [
        requests.Timeout("upstream stalled"),
        requests.ConnectionError("egress down"),
        FakeResponse(status=200, content=b"fine"),
    ]
    t, ledger, sleeper, nodes, session = make_transport(rotator, script)

    result = t.send("GET", "http://up.test/v1", headers={})

    assert isinstance(result, failover.SendResult)
    assert result.node_id == 1  # wrapped around to the first node
    assert not ledger.usable(nodes[1])  # connection error left it cooling
    assert ledger.usable(nodes[0])  # eventual success cleared its earlier failure


def test_non_retry_status_passes_through_without_retrying(rotator):
    r401 = FakeResponse(status=401, headers={"Content-Type": "application/json"})
    t, ledger, sleeper, nodes, session = make_transport(rotator, [r401])

    result = t.send("POST", "http://up.test/v1", headers={})

    assert isinstance(result, failover.SendResult)
    assert result.status_code == 401
    assert session.call_count == 1
    assert sleeper.sleeps == []
    assert ("Content-Type", "application/json") in result.header_pairs


def test_response_hop_by_hop_headers_are_stripped(rotator):
    r200 = FakeResponse(status=200, headers={
        "Content-Type": "application/json",
        "Connection": "close",
        "Transfer-Encoding": "chunked",
        "Server": "unit/1.0",
        "Date": "Tue, 22 Aug 2026 00:00:00 GMT",
    })
    t, *_ , session = make_transport(rotator, [r200])

    result = t.send("GET", "http://up.test/v1", headers={})

    assert isinstance(result, failover.SendResult)
    names = {k.lower() for k, _ in result.header_pairs}

    assert "content-type" in names
    assert not names & {"connection", "transfer-encoding", "server", "date"}


def test_stream_body_yields_chunks_and_closes_underlying(rotator):
    rstream = FakeResponse(status=200, chunks=[b"data: 1\n\n", b"data: 2\n\n"])
    t, *_ , session = make_transport(rotator, [rstream])

    result = t.send("POST", "http://up.test/v1", headers={}, stream=True)

    assert isinstance(result, failover.SendResult)
    chunks = list(result.body())

    assert chunks == [b"data: 1\n\n", b"data: 2\n\n"]
    assert rstream.iter_chunk_sizes == [1]  # exact blocking reads; do not optimize
    assert rstream.closed


def test_buffered_body_returns_bytes_and_closes_underlying(rotator):
    rok = FakeResponse(status=200, content=b"payload")
    t, *_ , session = make_transport(rotator, [rok])

    result = t.send("POST", "http://up.test/v1", headers={})

    assert isinstance(result, failover.SendResult)
    assert result.body() == b"payload"
    assert rok.closed


def test_client_cookies_never_reach_the_upstream(rotator):
    """Neither the cookies= channel (removed) nor a client Cookie header may
    reach the upstream call."""
    rok = FakeResponse(status=200, content=b"ok")
    t, *_ , session = make_transport(rotator, [rok])

    t.send("POST", "http://up.test/v1",
           headers={"Cookie": "session_id=abc; other=x"}, payload=b"{}")

    call = session.calls[0]
    assert call["headers"].get("Cookie") is None
    assert "cookie" not in {k.lower() for k in call["headers"]}


def test_credential_and_org_headers_never_reach_upstream(rotator):
    """x-api-key/api-key would defeat per-node key injection (a client's real
    key riding upstream); openai-organization/openai-project carry account
    identity. Dropped unconditionally — no flags involved."""
    rok = FakeResponse(status=200, content=b"ok")
    t, *_ , session = make_transport(rotator, [rok])

    t.send("POST", "http://up.test/v1/chat/completions",
           headers={
               "x-api-key": "sk-client-real",
               "api-key": "azure-client-key",
               "openai-organization": "org-leak",
               "OpenAI-Project": "proj-leak",
           },
           payload=b"{}")

    sent = {k.lower(): v for k, v in session.calls[0]["headers"].items()}
    assert sent["authorization"] == "Bearer key1"  # per-node key still injected
    for leaked in ("x-api-key", "api-key", "openai-organization", "openai-project"):
        assert leaked not in sent


@pytest.mark.parametrize("persona_hygiene", [True, False])
def test_persona_hygiene_telemetry_headers(rotator, persona_hygiene):
    """PERSONA_HYGIENE extends the drop set with client telemetry that
    fingerprints the automation stack; unrelated headers always pass."""
    rok = FakeResponse(status=200, content=b"ok")
    t, *_ , session = make_transport(rotator, [rok], persona_hygiene=persona_hygiene)

    t.send("POST", "http://up.test/v1", headers={
        "X-Stainless-Lang": "python",
        "x-stainless-runtime": "CPython 3.11",
        "x-app": "coding-agent",
        "X-Title": "agent-title",
        "Http-Referer": "http://localhost:5173",
        "Keep-Me": "yes",
    })

    sent = {k.lower() for k in session.calls[0]["headers"]}
    assert "keep-me" in sent  # unrelated header passes either way
    telemetry = {"x-stainless-lang", "x-stainless-runtime",
                 "x-app", "x-title", "http-referer"}
    if persona_hygiene:
        assert not sent & telemetry
    else:
        assert telemetry <= sent


# ── Failover ladder (issue #46) ──────────────────────────────────────────────

def test_same_mode_short_retry_after_waits_on_same_persona(rotator):
    """429 with Retry-After <= FAILOVER_MAX_WAIT: wait out exactly what the
    provider asked, retry the SAME persona, never touch the selector."""
    r429 = FakeResponse(status=429, headers={"Retry-After": "3"})
    rok = FakeResponse(status=200, content=b"ok")
    gate = FakeGate()
    t, ledger, sleeper, nodes, session = make_transport(
        rotator, [r429, rok], anonymity_failover="same", wait_gate=gate)

    result = t.send("POST", "http://up.test/v1/chat/completions",
                    headers={}, payload=b"{}", canonical={"a": 1, "b": 2})

    assert isinstance(result, failover.SendResult)
    assert sleeper.sleeps == [3.0]          # the ladder wait; no backoff on top
    assert gate.acquires == 1 and gate.releases == 1
    proxies = [call["proxies"]["http"] for call in session.calls]
    assert proxies == [nodes[0].proxy, nodes[0].proxy]  # same persona both tries
    # Serialization jitter: byte-different attempts, canonically identical.
    bodies = [call["data"] for call in session.calls]
    assert len(set(bodies)) == 2
    assert [json.loads(b) for b in bodies] == [{"a": 1, "b": 2}] * 2


def test_same_mode_long_retry_after_redistributes_once(rotator):
    """Retry-After beyond FAILOVER_MAX_WAIT: no parking — one cross-persona
    replay, then normal selector rotation."""
    r429 = FakeResponse(status=429, headers={"Retry-After": "30"})
    rok = FakeResponse(status=200, content=b"ok")
    t, ledger, sleeper, nodes, session = make_transport(
        rotator, [r429, rok], anonymity_failover="same")

    result = t.send("POST", "http://up.test/v1/chat/completions",
                    headers={}, canonical={"k": "v"})

    assert isinstance(result, failover.SendResult)
    proxies = [call["proxies"]["http"] for call in session.calls]
    assert proxies == [nodes[0].proxy, nodes[1].proxy]
    # Pacing stays today's rule: Retry-After honored up to backoff_max.
    assert sleeper.sleeps == [8.0]


def test_same_mode_waiter_cap_skips_wait_and_rotates(rotator):
    """A saturated waiter gate skips the ladder entirely: standard pacing,
    selector picks another node — bounded parking, availability preserved."""
    r429 = FakeResponse(status=429, headers={"Retry-After": "3"})
    rok = FakeResponse(status=200, content=b"ok")
    gate = FakeGate(available=False)
    t, ledger, sleeper, nodes, session = make_transport(
        rotator, [r429, rok], anonymity_failover="same", wait_gate=gate)

    result = t.send("POST", "http://up.test/v1/chat/completions",
                    headers={}, canonical={"k": "v"})

    assert isinstance(result, failover.SendResult)
    assert gate.acquires == 1 and gate.releases == 0  # refused, never held
    proxies = [call["proxies"]["http"] for call in session.calls]
    assert proxies == [nodes[0].proxy, nodes[1].proxy]


def test_cross_mode_default_ignores_ladder_and_jitters_canonical(rotator):
    """Default mode is today's availability-first behavior plus the one
    ratified change: per-attempt serialization jitter when a canonical body
    exists. Ladder never engages; rotation is immediate."""
    r429 = FakeResponse(status=429, headers={"Retry-After": "3"})
    rok = FakeResponse(status=200, content=b"ok")
    t, ledger, sleeper, nodes, session = make_transport(rotator, [r429, rok])

    result = t.send("POST", "http://up.test/v1/chat/completions",
                    headers={}, payload=b"raw-bytes", canonical={"x": 1})

    assert isinstance(result, failover.SendResult)
    bodies = [call["data"] for call in session.calls]
    assert len(set(bodies)) == 2  # jittered
    assert [json.loads(b) for b in bodies] == [{"x": 1}] * 2
    proxies = [call["proxies"]["http"] for call in session.calls]
    assert proxies == [nodes[0].proxy, nodes[1].proxy]
    assert sleeper.sleeps == [3.0]  # RA honored as first pacing sleep, as today


def test_cross_mode_raw_payload_untouched_without_canonical(rotator):
    """Non-JSON / no-canonical bodies pass through byte-for-byte as today."""
    r503 = FakeResponse(status=503)
    rok = FakeResponse(status=200, content=b"ok")
    t, *_ , session = make_transport(rotator, [r503, rok])

    t.send("POST", "http://up.test/v1/chat/completions",
           headers={}, payload=b"\x00raw-opaque")

    assert [call["data"] for call in session.calls] == [b"\x00raw-opaque"] * 2


def test_invalid_anonymity_failover_mode_rejected(rotator):
    with pytest.raises(ValueError, match="anonymity_failover"):
        make_transport(rotator, [], anonymity_failover="yolo")


def test_serialize_attempt_varies_bytes_preserves_value():
    canonical = {"b": 1, "a": [1, 2]}
    variants = {failover.serialize_attempt(canonical, i) for i in range(4)}
    assert len(variants) >= 2
    for i in range(4):
        wire = failover.serialize_attempt(canonical, i)
        assert json.loads(wire) == canonical
    # Deterministic per attempt index.
    assert (failover.serialize_attempt(canonical, 0)
            == failover.serialize_attempt(canonical, 0))


def test_production_session_never_stores_upstream_cookies(rotator):
    """Behavioral: a Set-Cookie fed through a jar running this policy is
    refused, and the production-built session carries the refusing policy."""
    import email.message
    import urllib.request

    msg = email.message.Message()
    msg["Set-Cookie"] = "tracker=leak; Path=/"
    response = type("_R", (), {"info": lambda self: msg})()

    jar = http.cookiejar.CookieJar(policy=failover._NoStoreCookiePolicy())
    jar.extract_cookies(response, urllib.request.Request("http://up.test/"))
    assert list(jar) == []

    t, *_ , session = make_transport(rotator, [], use_real_session=True)
    assert isinstance(session.cookies.get_policy(), failover._NoStoreCookiePolicy)


def test_compute_backoff_bounds(rotator):
    ra = failover.compute_backoff(0, 2.0, backoff_base=0.5, backoff_max=8.0)
    assert ra == 2.0
    assert failover.compute_backoff(0, 100.0, backoff_base=0.5, backoff_max=8.0) == 8.0
    for attempt in range(6):
        d = failover.compute_backoff(attempt, None, backoff_base=0.5, backoff_max=8.0)
        assert 0.5 <= d <= 8.0
    assert failover.compute_backoff(3, None, backoff_base=0.5, backoff_max=8.0) >= \
        failover.compute_backoff(0, None, backoff_base=0.5, backoff_max=8.0)


def test_compute_backoff_jitter_is_injectable(rotator):
    assert failover.compute_backoff(0, None, rng=stub_rng, backoff_base=0.5, backoff_max=8.0) == 0.625
    assert failover.compute_backoff(1, None, rng=stub_rng, backoff_base=0.5, backoff_max=8.0) == 1.125
    assert failover.compute_backoff(5, None, rng=stub_rng, backoff_base=0.5, backoff_max=8.0) == 8.0
    assert failover.compute_backoff(0, 2.0, rng=stub_rng, backoff_base=0.5, backoff_max=8.0) == 2.0


def test_parse_retry_after_variants(rotator):
    assert failover.parse_retry_after("3") == 3.0
    assert failover.parse_retry_after("0") == 0.0
    assert failover.parse_retry_after("-1") is None
    assert failover.parse_retry_after("soon") is None
    assert failover.parse_retry_after(None) is None
