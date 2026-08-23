"""curl_cffi transport adapter tests (issue #47).

resolve_impersonation and the per-persona session map are pure and always
tested. The live adapter smoke (cookie refusal, streaming) needs the
optional curl_cffi package and skips when it is absent.
"""

import pytest

import failover
from failover import SendResult, resolve_impersonation

from test_failover import FakeResponse, RecordingSleeper, stub_rng


def test_resolve_impersonation_mapping():
    assert resolve_impersonation("") == {}
    assert resolve_impersonation(None) == {}
    assert resolve_impersonation("chrome124") == {"impersonate": "chrome124"}
    assert resolve_impersonation("ja3:771,4865-4866-4867,0-23,29-23-24,0") == {
        "ja3": "771,4865-4866-4867,0-23,29-23-24,0"}
    assert resolve_impersonation("akamai:1:65536;m=1,p=0") == {
        "akamai": "1:65536;m=1,p=0"}
    # Curated stack labels are NOT valid impersonate targets — they pass
    # through as names and curl_cffi will reject unknown ones loudly rather
    # than silently impersonating the wrong stack.
    assert resolve_impersonation("okhttp-android") == {"impersonate": "okhttp-android"}


class RecordingFactory:
    def __init__(self):
        self.labels = []

    def __call__(self, fingerprint):
        self.labels.append(fingerprint)
        return FakeSessionFor(fingerprint)


class FakeSessionFor:
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        self.calls = []
        self.script = [FakeResponse(status=200, content=b"ok")] * 50

    def request(self, method, url, headers=None, data=None,
                proxies=None, timeout=None, stream=False):
        self.calls.append({"headers": headers, "data": data})
        return self.script.pop(0)


def test_transport_builds_one_session_per_persona_fingerprint(rotator):
    nodes = [
        rotator.Node(node_id=1, proxy="socks5h://n1:1080", api_key="k1",
                     fingerprint="stack-a"),
        rotator.Node(node_id=2, proxy="socks5h://n2:1080", api_key="k2",
                     fingerprint="stack-a"),  # same persona: shared session
        rotator.Node(node_id=3, proxy="socks5h://n3:1080", api_key="k3",
                     fingerprint="stack-b"),
    ]
    ledger = rotator.HealthLedger(nodes, 30.0, 300.0)
    selector = rotator.NodeSelector(nodes, ledger)
    factory = RecordingFactory()
    transport = failover.FailoverTransport(
        selector, ledger, session_factory=factory,
        sleep=RecordingSleeper(), rng=stub_rng)

    for _ in range(4):
        result = transport.send("GET", "http://up.test/v1/models", headers={})
        assert isinstance(result, SendResult)

    # One session per distinct fingerprint — personas never share a stack.
    assert sorted(factory.labels) == ["stack-a", "stack-b"]
    session_a = transport._sessions["stack-a"]
    session_b = transport._sessions["stack-b"]
    assert session_a is not session_b


def test_default_session_used_without_factory(rotator):
    from test_failover import FakeSession

    nodes = [rotator.Node(node_id=1, proxy="socks5h://n1:1080", api_key="k1",
                          fingerprint="whatever")]
    ledger = rotator.HealthLedger(nodes, 30.0, 300.0)
    selector = rotator.NodeSelector(nodes, ledger)
    session = FakeSession([FakeResponse(status=200, content=b"[]")])
    transport = failover.FailoverTransport(
        selector, ledger, session=session,
        sleep=RecordingSleeper(), rng=stub_rng)
    assert transport.session_factory is None

    result = transport.send("GET", "http://up.test/v1/models", headers={})
    assert isinstance(result, SendResult) and result.status_code == 200
    assert session.call_count == 1  # shared session rode the attempt


try:
    import curl_cffi  # noqa: F401
    CURL_CFFI_INSTALLED = True
except ImportError:
    CURL_CFFI_INSTALLED = False


@pytest.mark.skipif(not CURL_CFFI_INSTALLED,
                    reason="optional dependency curl_cffi not installed")
@pytest.mark.parametrize("fingerprint,expected_kwargs", [
    ("", {}),
    ("chrome124", {"impersonate": "chrome124"}),
    ("ja3:771,4865,0-23,29-23-24,0", {"ja3": "771,4865,0-23,29-23-24,0"}),
])
def test_adapter_session_carries_pinned_fingerprint(fingerprint, expected_kwargs):
    from unittest.mock import patch

    with patch("curl_cffi.requests.Session") as session_cls:
        failover.CurlCffiSessionAdapter(fingerprint)
        assert session_cls.call_count == 1
        kwargs = session_cls.call_args[1]
        for key, value in expected_kwargs.items():
            assert kwargs[key] == value


@pytest.mark.skipif(not CURL_CFFI_INSTALLED,
                    reason="optional dependency curl_cffi not installed")
def test_adapter_streams_incrementally_and_never_stores_cookies(mock):
    """Live adapter smoke against the scriptable mock upstream: SSE chunks
    arrive well before the body completes (TTFB parity with the requests
    transport), and a Set-Cookie on one exchange never rides the next."""
    import time

    mock.sse_parts = 4
    mock.chunk_delay = 0.15
    mock.script = [(200, {"Set-Cookie": "tracker=leak; Path=/"},
                    b'{"error": "unused"}')]

    adapter = failover.CurlCffiSessionAdapter("chrome124")
    # Exchange #1 (POST consumes the scripted Set-Cookie response).
    response = adapter.request(
        "POST", mock.url("/v1/chat/completions"),
        headers={"Content-Type": "application/json"},
        data=b'{"model":"gpt-4o","messages":[]}',
        proxies=None, timeout=10.0, stream=False)

    # Cookie refusal: the Set-Cookie from this exchange must not survive.
    assert not list(getattr(adapter._session.cookies, "values", lambda: [])())

    # Streaming parity: TTFB well before completion, and delivery is
    # incremental (per network read — per SSE event through the mock).
    stream_resp = adapter.request(
        "POST", mock.url("/v1/chat/completions"),
        headers={"Content-Type": "application/json"},
        data=b'{"model":"gpt-4o","stream":true,"messages":[]}',
        proxies=None, timeout=10.0, stream=True)
    ttfb = None
    arrivals = []
    start = time.monotonic()
    for chunk in stream_resp.iter_content(chunk_size=1):
        now = time.monotonic() - start
        if ttfb is None:
            ttfb = now
        arrivals.append((now, len(chunk)))
    total = time.monotonic() - start
    assert ttfb is not None and len(arrivals) >= 4  # per-event granularity
    assert ttfb < total * 0.6  # first bytes arrived well before the tail
