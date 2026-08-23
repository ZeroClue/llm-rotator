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
import logging
import random
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


@dataclass
class SendResult:
    """One successful upstream response.

    body() returns bytes when sent with stream=False, or a chunk iterator
    when stream=True; either way the underlying response is closed exactly
    once, after the body is consumed. body() is single-shot: consuming a
    streamed result twice re-enters an already-closed response and raises.
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
                 persona_hygiene=False):
        self.selector = selector
        self.ledger = ledger
        # PERSONA_HYGIENE: also strip client telemetry headers (x-stainless-*,
        # x-app, x-title, http-referer) outbound; see drop_outbound_header.
        self.persona_hygiene = persona_hygiene
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

    def send(self, method, url, headers, payload=b"", stream=False,
             request_id=None):
        # request_id is pure log context: it rides every attempt record so
        # proxy-side and upstream-attempt lines correlate in one grep.
        log_extras = {"request_id": request_id}
        last_error = None
        attempts = 0
        single_shot_post = method.upper() == "POST" and not self.retry_posts
        for attempt in range(self.max_retries):
            node = self.selector.select()
            attempts += 1

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
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    data=payload,
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
                else:
                    self.ledger.record_success(node)
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
                self.sleep(compute_backoff(
                    attempt,
                    retry_after,
                    rng=self.rng,
                    backoff_base=self.backoff_base,
                    backoff_max=self.backoff_max,
                ))

        return AllNodesFailed(last_error=last_error, attempts=attempts)
