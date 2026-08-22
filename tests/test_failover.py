"""HTTP-free unit tests for the failover transport.

The fake session replays scripted outcomes (statuses or exceptions); the
recording sleeper and stubbed rng make retry pacing deterministic. No
sockets, no Flask, no real sleeps.
"""

import http.cookiejar

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


def stub_rng(lo, hi):
    return hi / 4  # deterministic quarter of the jitter band


def make_transport(rotator, script, count=2, *, max_retries=3, timeout=25.0,
                   backoff_base=0.5, backoff_max=8.0, use_real_session=False):
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
    )
    return transport, ledger, sleeper, nodes, session or transport.session


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
