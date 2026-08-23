"""Failover transport: carries one client request through the node pool
under the failover rule until an upstream response succeeds or every node
is exhausted.

Framework-free: callers hand over request primitives and get back a
SendResult or AllNodesFailed; mapping results to HTTP responses (Flask or
otherwise) is the view's job. Selection, cooldown accounting, and key
ownership stay in the rotator's NodeSelector/HealthLedger — this module
only drives attempts and classifies outcomes.
"""

import http.cookiejar
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field

import requests
from requests.exceptions import ConnectionError, RequestException, Timeout

logger = logging.getLogger(__name__)

_HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
    "date", "server", "content-encoding", "content-length",
})

# Client headers that must never be forwarded upstream; Authorization is
# injected per attempt and anything host/framing/cookie-related is the
# transport's business, not the client's. Cookies especially: they would ride
# whatever egress node the request rotates through. X-Request-Id is a
# proxy-local correlation handle by contract (issue #34) — it never leaks.
# The credential/organization entries are unconditional bug fixes: a client's
# real key arriving via x-api-key/api-key would defeat per-node key injection,
# and openai-organization/openai-project carry account identity upstream.
_OUTBOUND_DROPPED_HEADERS = frozenset({
    "host", "content-length", "cookie", "x-request-id",
    "x-api-key", "api-key", "openai-organization", "openai-project",
})

# Persona-hygiene extras (PERSONA_HYGIENE=true only): client telemetry that
# fingerprints the automation stack behind this proxy. x-stainless-* is the
# OpenAI SDK's hardware/runtime telemetry family.
_PERSONA_HYGIENE_HEADERS = frozenset({"x-app", "x-title", "http-referer"})
_PERSONA_HYGIENE_PREFIXES = ("x-stainless-",)


def drop_outbound_header(name, *, hygiene=False):
    """True when a client header must not reach upstream. The unconditional
    set always drops; persona hygiene extends it while enabled."""
    lowered = name.lower()
    if lowered in _OUTBOUND_DROPPED_HEADERS:
        return True
    return hygiene and (
        lowered in _PERSONA_HYGIENE_HEADERS
        or lowered.startswith(_PERSONA_HYGIENE_PREFIXES)
    )


class _NoStoreCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    """Refuses every Set-Cookie: the session jar is shared across worker
    threads (unsynchronized mutation = race) and stored cookies would leak
    between egress nodes."""

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def parse_retry_after(value):
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def compute_backoff(attempt, retry_after=None, rng=random.uniform, *,
                    backoff_base, backoff_max):
    if retry_after is not None:
        return min(retry_after, backoff_max)
    delay = backoff_base * (2 ** attempt) + rng(0, backoff_base)
    return min(delay, backoff_max)


# Deterministic serialization variants cycled per attempt: semantically
# identical JSON, byte-different wire form. The four forms stay pairwise
# distinct for any realistic payload (compact/spaced/indented/tab-indented ×
# key order), so consecutive attempts never carry identical bytes for a
# provider to hash-link. A provider hashing canonical JSON defeats this
# entirely (accepted residual risk, ADR 0002).
_SERIALIZE_VARIANTS = (
    {"sort_keys": True, "separators": (",", ":")},
    {"sort_keys": False, "separators": (", ", ": ")},
    {"sort_keys": True, "indent": 2},
    {"sort_keys": False, "indent": "\t"},
)


def serialize_attempt(canonical, attempt):
    return json.dumps(
        canonical,
        **_SERIALIZE_VARIANTS[attempt % len(_SERIALIZE_VARIANTS)],
    ).encode("utf-8")


# ── Quota-header parsing (issue #48) ─────────────────────────────────────────
_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$")


def parse_duration_seconds(raw):
    """OpenAI x-ratelimit-reset-* durations: '1s', '6m', '1h2m30s'."""
    if not raw:
        return None
    match = _DURATION_RE.match(raw.strip())
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)


def parse_quota_headers(headers):
    """Headroom from OpenAI-style ratelimit headers: min of the available
    dimensions (requests/tokens) as a 0-100 percentage, plus the longest
    reset window. Anything missing/unparseable simply narrows the picture;
    no headers at all -> None (provider unknown to us)."""
    remaining_pct = []
    reset_seconds = None
    for dim in ("requests", "tokens"):
        limit = headers.get(f"x-ratelimit-limit-{dim}")
        remaining = headers.get(f"x-ratelimit-remaining-{dim}")
        if limit and remaining:
            try:
                limit_v, remaining_v = float(limit), float(remaining)
            except ValueError:
                continue
            if limit_v > 0:
                remaining_pct.append(max(0.0, 100.0 * remaining_v / limit_v))
        reset = parse_duration_seconds(headers.get(f"x-ratelimit-reset-{dim}"))
        if reset is not None:
            reset_seconds = reset if reset_seconds is None else max(reset_seconds, reset)
    if not remaining_pct:
        return None
    return {"remaining_pct": min(remaining_pct),
            "reset_seconds": reset_seconds}


# ── curl_cffi transport adapter (issue #47) ──────────────────────────────────
def resolve_impersonation(fingerprint):
    """Persona fingerprint label -> curl_cffi session kwargs. 'ja3:<spec>'
    and 'akamai:<spec>' pass raw custom fingerprints through; any other
    non-empty value is treated as a curl_cffi impersonate target name
    (e.g. 'chrome124'); empty means plain libcurl — still one consistent
    stack per persona. The curated stack LABELS (okhttp-android etc.) are
    deliberately NOT valid curl_cffi targets: pretending to okhttp with an
    unverified JA3 stands out more than an honest consistent client, so
    real diversity comes from operator-supplied ja3:/akamai: specs."""
    if not fingerprint:
        return {}
    if fingerprint.startswith("ja3:"):
        return {"ja3": fingerprint[4:].strip()}
    if fingerprint.startswith("akamai:"):
        return {"akamai": fingerprint[7:].strip()}
    return {"impersonate": fingerprint}


class _CaseInsensitiveHeaders:
    """Case-insensitive get/items over the upstream header pairs — matches
    requests.CaseInsensitiveDict semantics, which send()'s Retry-After
    lookup and parse_quota_headers' lowercase lookups depend on."""

    def __init__(self, pairs):
        self._pairs = list(pairs)

    def get(self, key, default=None):
        lowered = key.lower()
        for k, v in self._pairs:
            if k.lower() == lowered:
                return v
        return default

    def items(self):
        return list(self._pairs)


class _CurlCffiResponse:
    """The minimal requests.Response surface FailoverTransport.send()
    consumes, backed by a curl_cffi response."""

    def __init__(self, response):
        self._response = response
        self.status_code = response.status_code
        self._headers = _CaseInsensitiveHeaders(response.headers.items())
        self.closed = False

    @property
    def headers(self):
        return self._headers

    @property
    def content(self):
        return self._response.content

    def iter_content(self, chunk_size):
        # chunk_size is deliberately NOT forwarded: curl_cffi warns that it
        # cannot honor it and ignores the value, delivering network-read
        # granularity natively — per-SSE-event, which beats requests'
        # byte-at-a-time reads for streaming latency anyway.
        try:
            for chunk in self._response.iter_content():
                yield chunk
        finally:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self._response.close()


class CurlCffiSessionAdapter:
    """requests.Session-shaped wrapper over curl_cffi, pinned to ONE persona
    fingerprint for its lifetime (issue #47): impersonating per-request would
    churn a persona's TLS identity mid-conversation. One instance per
    fingerprint lives on the transport.

    Cookies: per-persona sessions make cross-persona leakage structurally
    impossible (the original threat behind _NoStoreCookiePolicy), so clearing
    here is hygiene against unbounded jar growth, best-effort around
    curl_cffi's lack of a refuse-all policy hook.

    Exceptions are translated into the requests hierarchy so send()'s
    failure classification (timeout/connection/general) keeps working
    unchanged across transports."""

    def __init__(self, fingerprint=""):
        from curl_cffi import requests as cffi_requests
        self._fingerprint = fingerprint
        self._session = cffi_requests.Session(**resolve_impersonation(fingerprint))

    def request(self, method, url, headers=None, data=None, proxies=None,
                timeout=None, stream=False):
        try:
            response = self._session.request(
                method=method, url=url, headers=headers, data=data,
                proxies=proxies, timeout=timeout, stream=stream,
            )
        except requests.Timeout:
            raise
        except requests.RequestException:
            raise
        except Exception as exc:
            # curl_cffi raises its own hierarchy (CurlError & friends);
            # classify by name so version drift degrades to the generic
            # retryable bucket rather than crashing the attempt.
            name = type(exc).__name__.lower()
            if "timeout" in name:
                raise requests.Timeout(str(exc)) from exc
            if "connection" in name:
                raise requests.ConnectionError(str(exc)) from exc
            raise requests.RequestException(str(exc)) from exc
        try:
            self._session.cookies.clear()
        except Exception as exc:
            logger.debug("curl_cffi cookie clear failed: %s", exc)
        return _CurlCffiResponse(response)

    def close(self):
        try:
            self._session.close()
        except Exception as exc:
            logger.debug("curl_cffi session close failed: %s", exc)


@dataclass
class SendResult:
    """One successful upstream response.

    body() returns bytes when sent with stream=False, or a chunk iterator
    when sent with stream=True; either way the underlying response is closed
    exactly once, after the body is consumed. body() is single-shot:
    consuming a streamed result twice re-enters an already-closed response
    and raises. _response is a requests.Response, or an equivalent shim
    (see _CurlCffiResponse) when a non-requests session factory is wired.
    """
    status_code: int
    header_pairs: list
    node_id: int
    _response: requests.Response = field(repr=False)
    _streamed: bool = field(repr=False)

    def body(self):
        if self._streamed:
            return self._iter_chunks()
        content = self._response.content
        self._response.close()
        return content

    def _iter_chunks(self):
        try:
            # chunk_size=1: larger sizes do exact blocking reads and
            # buffer small SSE events until EOF, killing latency.
            for chunk in self._response.iter_content(chunk_size=1):
                yield chunk
        finally:
            self._response.close()


@dataclass
class AllNodesFailed:
    last_error: str | None
    attempts: int = 0


class FailoverTransport:
    """
    Drives the retry/failover loop: picks nodes from the selector, injects
    each node's API key, routes through its egress, classifies the outcome,
    reports failures/successes to the ledger, and paces retries with
    Retry-After-aware exponential backoff. Session, sleeper, and jitter rng
    are injectable so the whole loop is testable without HTTP or real time.
    """

    def __init__(self, selector, ledger, session=None, sleep=time.sleep,
                 rng=random.uniform, max_retries=4, timeout=25.0,
                 backoff_base=0.5, backoff_max=8.0, retry_posts=True,
                 persona_hygiene=False, anonymity_failover="cross",
                 failover_max_wait=8.0, failover_max_waiters=4,
                 wait_gate=None, redistribution_jitter=True,
                 clock=time.monotonic, session_factory=None):
        self.selector = selector
        self.ledger = ledger
        # PERSONA_HYGIENE: also strip client telemetry headers (x-stainless-*,
        # x-app, x-title, http-referer) outbound; see drop_outbound_header.
        self.persona_hygiene = persona_hygiene
        # Clock seam for quota timestamps (issue #48): ledger windows are set
        # against this clock so tests can drive them deterministically.
        self.clock = clock
        # "cross" = today's availability-first failover (replays bytes across
        # personas). "same" = 429s with a short Retry-After wait out on the
        # SAME persona (what a legitimate single customer does); only then is
        # a cross-persona replay allowed, at most once per request. Byte
        # replays across personas are the cheapest linkage oracle there is.
        if anonymity_failover not in ("same", "cross"):
            raise ValueError(
                f"anonymity_failover must be 'same' or 'cross', got {anonymity_failover!r}")
        self.anonymity_failover = anonymity_failover
        self.failover_max_wait = failover_max_wait
        # Bounds threads concurrently parked in same-persona waits so a burst
        # of 429s cannot pin the whole gthread pool (issue #46).
        self.wait_gate = wait_gate if wait_gate is not None \
            else threading.Semaphore(failover_max_waiters)
        self.redistribution_jitter = redistribution_jitter
        if session is not None:
            self.session = session
        else:
            self.session = requests.Session()
            # Never store upstream cookies (see _NoStoreCookiePolicy).
            self.session.cookies.set_policy(_NoStoreCookiePolicy())
        self.sleep = sleep
        self.rng = rng
        self.max_retries = max_retries
        self.timeout = timeout
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        # POSTs are not idempotent: a 504/timeout may mean the upstream
        # completed, and verbatim retries then double-bill. retry_posts=False
        # gives POSTs exactly one attempt; the ledger still records the
        # outcome so cooldown accounting stays truthful.
        self.retry_posts = retry_posts
        # Optional per-persona session factory (issue #47): given a persona
        # fingerprint label, returns a session-like object. When unset, every
        # attempt rides the single shared session (requests default).
        self.session_factory = session_factory
        self._sessions = {}
        self._sessions_lock = threading.Lock()

    def _session_for(self, node):
        """The session for one attempt: the shared default unless a
        session_factory was provided, in which case one lazily-built session
        per distinct fingerprint — personas never share a stack."""
        if self.session_factory is None:
            return self.session
        key = node.fingerprint or ""
        with self._sessions_lock:
            session = self._sessions.get(key)
            if session is None:
                session = self.session_factory(key)
                self._sessions[key] = session
            return session

    def send(self, method, url, headers, payload=b"", stream=False,
             request_id=None, canonical=None):
        # request_id is pure log context: it rides every attempt record so
        # proxy-side and upstream-attempt lines correlate in one grep.
        # canonical is the parsed JSON body (when the payload is JSON): every
        # attempt re-serializes it with varied framing so cross-persona
        # replays never carry identical bytes.
        log_extras = {"request_id": request_id}
        last_error = None
        attempts = 0
        single_shot_post = method.upper() == "POST" and not self.retry_posts
        sticky_node = None      # same-persona retry target after a short wait
        redistributed = False   # cross-persona replay: at most once per request
        for attempt in range(self.max_retries):
            if sticky_node is not None:
                # Deliberately bypasses the cooldown ledger: we just waited
                # out exactly the Retry-After the provider asked for.
                node, sticky_node = sticky_node, None
            else:
                node = self.selector.select()
            attempts += 1

            wire_payload = serialize_attempt(canonical, attempt) \
                if canonical is not None and self.redistribution_jitter else payload

            dropped = [k for k in headers
                       if drop_outbound_header(k, hygiene=self.persona_hygiene)]
            request_headers = {k: v for k, v in headers.items() if k not in dropped}
            request_headers["Authorization"] = f"Bearer {node.api_key}"
            if node.user_agent:
                # Persona identity: the node's own User-Agent always wins over
                # the client's — a persona is its whole client stack, and a
                # client UA under a persona fingerprint would be incoherent.
                request_headers["User-Agent"] = node.user_agent
            proxies = {"http": node.proxy, "https": node.proxy}

            if dropped:
                # Counts only — never header names or values: client telemetry
                # must not be echoed into the proxy's own logs either.
                logger.info(
                    f"Dropped {len(dropped)} outbound client header(s)",
                    extra={**log_extras, "event": "header_hygiene",
                           "count": len(dropped)},
                )

            logger.info(
                f"Attempt {attempt + 1}/{self.max_retries}: Routing via Node {node.node_id} "
                f"({node.proxy.split('://')[1].split(':')[0]})",
                extra={**log_extras, "event": "upstream_attempt",
                       "node_id": node.node_id, "attempt": attempt + 1},
            )

            retry_after = None
            failed = False
            next_action = ("not retrying: RETRY_POSTS=false" if single_shot_post
                           else "Retrying...")
            failure_extras = lambda reason, **kw: {  # noqa: E731
                **log_extras, "event": "upstream_failure",
                "node_id": node.node_id, "attempt": attempt + 1,
                "reason": reason, **kw,
            }
            try:
                response = self._session_for(node).request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    data=wire_payload,
                    proxies=proxies,
                    timeout=self.timeout,
                    stream=stream,
                )
            except Timeout as e:
                logger.error(
                    f"Timeout on Node {node.node_id}: {str(e)}. {next_action}",
                    extra=failure_extras("timeout"),
                )
                last_error = f"Timeout: {str(e)}"
                self.ledger.record_failure(node)
                failed = True
            except ConnectionError as e:
                logger.error(
                    f"Connection error on Node {node.node_id}: {str(e)}. {next_action}",
                    extra=failure_extras("connection_error"),
                )
                last_error = f"Connection error: {str(e)}"
                self.ledger.record_failure(node)
                failed = True
            except RequestException as e:
                logger.error(
                    f"Request failed on Node {node.node_id}: {str(e)}. {next_action}",
                    extra=failure_extras("request_error"),
                )
                last_error = f"Request error: {str(e)}"
                self.ledger.record_failure(node)
                failed = True
            else:
                if response.status_code in RETRY_STATUSES:
                    logger.warning(
                        f"Node {node.node_id} returned HTTP {response.status_code}. "
                        f"{next_action}",
                        extra=failure_extras("retryable_status", status_code=response.status_code),
                    )
                    last_error = f"Upstream error: {response.status_code}"
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    response.close()
                    self.ledger.record_failure(node)
                    failed = True

                    # A 429 is the one failure provably processed-not: the
                    # provider rejected the request before work, so a replay
                    # cannot double-bill. In same-persona mode, a short
                    # Retry-After is waited out on THIS persona — exactly what
                    # the legitimate single customer the provider throttled
                    # would do. Only a long/no Retry-After justifies the
                    # linkage cost of one cross-persona replay.
                    if (response.status_code == 429
                            and self.anonymity_failover == "same"
                            and not single_shot_post):
                        if (retry_after is not None
                                and retry_after <= self.failover_max_wait):
                            if self.wait_gate.acquire(False):
                                try:
                                    logger.info(
                                        f"429 on Node {node.node_id}: waiting "
                                        f"{retry_after:.1f}s to retry the same persona",
                                        extra={**log_extras,
                                               "event": "failover_same_persona_wait",
                                               "node_id": node.node_id,
                                               "wait_seconds": retry_after},
                                    )
                                    self.sleep(retry_after)
                                    sticky_node = node
                                finally:
                                    self.wait_gate.release()
                            else:
                                # Gate full: this rotation IS the request's
                                # cross-persona hop — consume the budget so a
                                # later above-threshold 429 cannot replay again.
                                redistributed = True
                                logger.warning(
                                    f"429 on Node {node.node_id}: waiter cap hit, "
                                    "rotating to another persona without waiting",
                                    extra={**log_extras,
                                           "event": "failover_redistribute",
                                           "node_id": node.node_id,
                                           "reason": "waiter_cap"},
                                )
                        elif not redistributed:
                            redistributed = True
                            logger.info(
                                f"429 on Node {node.node_id}: redistributing once "
                                "via another persona (Retry-After too long to wait)",
                                extra={**log_extras,
                                       "event": "failover_redistribute",
                                       "node_id": node.node_id,
                                       "reason": "retry_after_too_long"},
                            )
                else:
                    self.ledger.record_success(node)
                    quota = parse_quota_headers(response.headers)
                    if quota:
                        self.ledger.record_quota(node, now=self.clock(), **quota)
                    return SendResult(
                        status_code=response.status_code,
                        header_pairs=[
                            (k, v) for k, v in response.headers.items()
                            if k.lower() not in _HOP_BY_HOP_HEADERS
                        ],
                        node_id=node.node_id,
                        _response=response,
                        _streamed=stream,
                    )

            if single_shot_post and failed:
                return AllNodesFailed(last_error=last_error, attempts=attempts)

            if attempt < self.max_retries - 1:
                if sticky_node is not None:
                    continue  # the ladder already waited; no backoff on top
                self.sleep(compute_backoff(
                    attempt,
                    retry_after,
                    rng=self.rng,
                    backoff_base=self.backoff_base,
                    backoff_max=self.backoff_max,
                ))

        return AllNodesFailed(last_error=last_error, attempts=attempts)
