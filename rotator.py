#!/usr/bin/env python3
"""
Secure Tailscale LLM Proxy Rotator with Advanced Token Optimization
===================================================================
Features:
- Environment variable configuration (no hardcoded secrets)
- Dynamic prompt engineering & context optimization
- Token usage maximization and waste reduction
- Thread-safe round-robin rotation across Tailscale nodes
- Automatic failover on rate limits (429) and server errors (5xx)
- DNS leak prevention via socks5h:// protocol
- Streaming support with fast-path bypass for real-time responses
- Post-optimization token verification to prevent double-counting
- Provider-specific optimization profiles
"""

import os
import re
import hmac
import hashlib
import json
import time
import signal
import logging
import threading
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace

from flask import Flask, g, request, Response, jsonify, stream_with_context
from werkzeug.exceptions import HTTPException

from failover import AllNodesFailed, FailoverTransport
import failover

logger = logging.getLogger(__name__)

# Try to import tiktoken for token counting (optional but recommended)
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logger.warning("tiktoken not installed. Install with: pip install tiktoken")

# Try to import llmlingua for advanced semantic compression
try:
    from llmlingua import PromptCompressor
    LLMLINGUA_AVAILABLE = True
except ImportError:
    LLMLINGUA_AVAILABLE = False
    logger.warning("llmlingua not installed. Advanced semantic compression disabled. Install with: pip install llmlingua")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration via Environment Variables
# ─────────────────────────────────────────────────────────────────────────────
def _env_bool(raw):
    return raw.strip().lower() == "true"


@dataclass(frozen=True)
class Settings:
    """Core/runtime knobs, parsed from the environment in exactly one place
    (from_env) when create_app() runs. Importing this module parses nothing."""

    log_level: str = "INFO"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    target_provider_url: str = "https://api.openai.com/v1"
    max_retries: int = 4
    request_timeout: float = 25.0
    retry_backoff_base: float = 0.5
    retry_backoff_max: float = 8.0
    node_cooldown_base: float = 2.0
    node_cooldown_max: float = 60.0
    # POSTs are retried verbatim on failover; a 504/timeout may mean the upstream
    # completed, so duplicate-averse operators can set RETRY_POSTS=false.
    retry_posts: bool = True
    # Graceful-shutdown drain window (seconds from the signal) for in-flight
    # SSE streams; keep below GUNICORN_GRACEFUL_TIMEOUT so the terminal event
    # flushes before gunicorn's hard kill. 0 cuts streams on the next chunk.
    stream_drain_window: float = 20.0
    default_model: str = "gpt-4o"
    # Optional bearer-token gate. Empty disables auth entirely. /health and
    # /ready stay open either way so orchestrators can probe without the token.
    auth_token: str = ""
    # "text" keeps the classic human format; "json" emits one structured
    # object per line (see JsonFormatter).
    log_format: str = "text"
    # Persona hygiene: additionally strip client telemetry headers
    # (x-stainless-*, x-app, x-title, http-referer) outbound and remove
    # provider identity fields (user/metadata/prompt_cache_key/
    # safety_identifier) from chat payloads. The credential/organization
    # header drops are unconditional and unaffected by this flag.
    persona_hygiene: bool = False
    # Failover ladder (issue #46): "same" waits out a short 429 Retry-After
    # on the failing persona and only then allows one cross-persona replay;
    # "cross" keeps today's availability-first rotation. A byte-identical
    # replay across personas is the cheapest linkage oracle there is.
    anonymity_failover: str = "cross"
    failover_max_wait: float = 8.0
    # Cap on threads concurrently parked in same-persona waits so a burst of
    # 429s cannot pin the whole gthread pool.
    failover_max_waiters: int = 4
    redistribution_jitter: bool = True
    # Outbound transport (issue #47): "curl_cffi" enables the fingerprint-
    # capable libcurl adapter (optional dependency; startup fails loudly if
    # it is not installed). "requests" keeps the historical h1.1 stack, in
    # which only the persona User-Agent half is observable.
    transport: str = "requests"

    @classmethod
    def from_env(cls) -> "Settings":
        anonymity_failover = os.getenv("ANONYMITY_FAILOVER", "cross")
        if anonymity_failover not in ("same", "cross"):
            raise ValueError(
                f"ANONYMITY_FAILOVER must be 'same' or 'cross', "
                f"got {anonymity_failover!r}")
        transport = os.getenv("TRANSPORT", "requests")
        if transport not in ("requests", "curl_cffi"):
            raise ValueError(
                f"TRANSPORT must be 'requests' or 'curl_cffi', got {transport!r}")
        return cls(
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            bind_host=os.getenv("PROXY_BIND_HOST", "127.0.0.1"),
            bind_port=int(os.getenv("PROXY_BIND_PORT", "8080")),
            target_provider_url=os.getenv("LLM_PROVIDER_URL", "https://api.openai.com/v1"),
            max_retries=int(os.getenv("MAX_RETRIES", "4")),
            request_timeout=float(os.getenv("REQUEST_TIMEOUT", "25.0")),
            retry_backoff_base=float(os.getenv("RETRY_BACKOFF_BASE", "0.5")),
            retry_backoff_max=float(os.getenv("RETRY_BACKOFF_MAX", "8.0")),
            node_cooldown_base=float(os.getenv("NODE_COOLDOWN_BASE", "2.0")),
            node_cooldown_max=float(os.getenv("NODE_COOLDOWN_MAX", "60.0")),
            retry_posts=_env_bool(os.getenv("RETRY_POSTS", "true")),
            stream_drain_window=float(os.getenv("STREAM_DRAIN_WINDOW", "20.0")),
            default_model=os.getenv("DEFAULT_MODEL", "gpt-4o"),
            auth_token=os.getenv("PROXY_AUTH_TOKEN", ""),
            log_format=os.getenv("LOG_FORMAT", "text"),
            persona_hygiene=_env_bool(os.getenv("PERSONA_HYGIENE", "false")),
            anonymity_failover=anonymity_failover,
            failover_max_wait=float(os.getenv("FAILOVER_MAX_WAIT", "8.0")),
            failover_max_waiters=int(os.getenv("FAILOVER_MAX_WAITERS", "4")),
            redistribution_jitter=_env_bool(
                os.getenv("REDISTRIBUTION_JITTER", "true")),
            transport=transport,
        )


class JsonFormatter(logging.Formatter):
    """One JSON object per log line: a stable envelope (ts/level/logger/
    message) plus whatever structured extras the call site attached. Prose
    messages are preserved verbatim inside `message`."""

    # LogRecord attributes that belong to the envelope or the logging
    # machinery itself; everything else on the record is an extra.
    _ENVELOPE = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    })

    def format(self, record):
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._ENVELOPE:
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(settings):
    """(Re)apply root logging config; safe to call per app build."""
    if settings.log_format == "json":
        formatter = JsonFormatter()
    else:
        # Explicit so text output stays byte-identical regardless of how
        # handlers are attached below.
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        force=True,
        handlers=[handler],
    )


# Token-optimization settings live in OptimizationConfig.from_env() — parsed
# in exactly one place, next to the pipeline class below.

# ─────────────────────────────────────────────────────────────────────────────
# Build Node Pool from Environment Variables
# ─────────────────────────────────────────────────────────────────────────────
# Curated client personas: realistic API-client stacks, deliberately NOT
# browsers — a browser TLS fingerprint on an API endpoint stands out more
# than it hides. The fingerprint label is what the optional curl_cffi
# transport (issue #47) impersonates; with the default requests transport
# only the User-Agent half is observable. Public: tests and operators may
# enumerate the stacks.
PERSONA_STACKS = (
    {"name": "httpx-py311-linux", "user_agent": "python-httpx/0.27.0"},
    {"name": "undici-node20-linux", "user_agent": "undici/6.19.2"},
    {"name": "okhttp-android", "user_agent": "okhttp/4.12.0"},
    {"name": "reqwest-macos", "user_agent": "reqwest/0.12.5"},
    {"name": "requests-windows", "user_agent": "python-requests/2.32.3"},
)


def resolve_persona(node_id, user_agent_override=None, fingerprint_override=None):
    """A node's persona attributes: explicit PERSONA_N_* overrides win;
    otherwise a stable hash of node_id picks from the curated stacks, so a
    given node presents the same persona across restarts and processes."""
    stack = PERSONA_STACKS[
        hashlib.sha256(str(node_id).encode()).digest()[0] % len(PERSONA_STACKS)
    ]
    return (
        user_agent_override if user_agent_override else stack["user_agent"],
        fingerprint_override if fingerprint_override else stack["name"],
    )


def _persona_override(node_index, kind):
    """Read one PERSONA_N_<KIND> override: None when unset, normalized
    (stripped) value otherwise, loud failure when set but blank."""
    env_name = f"PERSONA_{node_index}_{kind}"
    raw = os.getenv(env_name)
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError(f"{env_name} is set but empty — remove it or give it a value")
    return raw.strip()


@dataclass(frozen=True)
class Node:
    """One upstream target: an egress paired with the API key spent through
    it, presenting one consistent client persona (see docs/adr/0002)."""
    node_id: int
    proxy: str
    api_key: str = field(repr=False)
    user_agent: str = ""
    fingerprint: str = ""


def build_node_pool():
    """Build the ordered node pool from PROXY_N_URL/API_KEY_N pairs."""
    pool = []
    node_index = 1

    while True:
        proxy_url = os.getenv(f"PROXY_{node_index}_URL")
        api_key = os.getenv(f"API_KEY_{node_index}")

        if not proxy_url or not api_key:
            if node_index == 1:
                logger.error("At least one node (PROXY_1_URL and API_KEY_1) must be configured!")
                raise ValueError("Missing required node configuration")
            break

        user_agent = _persona_override(node_index, "USER_AGENT")
        fingerprint = _persona_override(node_index, "FINGERPRINT")
        user_agent, fingerprint = resolve_persona(node_index, user_agent, fingerprint)
        pool.append(Node(
            node_id=node_index,
            proxy=proxy_url,
            api_key=api_key,
            user_agent=user_agent,
            fingerprint=fingerprint,
        ))
        logger.info(f"Loaded node {node_index}: {proxy_url} (persona: {fingerprint})")
        node_index += 1

    logger.info(f"Node pool initialized with {len(pool)} nodes")
    return pool


class ShutdownState:
    """
    Shutdown draining: armed once by the signal handlers when the process is
    asked to stop; streams may finish naturally until the drain window
    elapses, then each is cut with a terminal SSE event.

    One global deadline is fixed at arm time — a stream starting after that
    deadline is already past it and terminates immediately. Arming is
    idempotent: the first signal wins.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._deadline = None
        self._inflight = 0
        self._lock = threading.Lock()

    def arm(self, grace_seconds):
        if self._deadline is None:
            self._deadline = self._clock() + max(0.0, grace_seconds)

    def draining(self):
        return self._deadline is not None and self._clock() >= self._deadline

    def stream_started(self):
        with self._lock:
            self._inflight += 1

    def stream_finished(self):
        with self._lock:
            self._inflight -= 1

    @property
    def inflight(self):
        with self._lock:
            return self._inflight

# Built once by create_app()/get_app() — never at import time. Declared as
# None so attribute access before building is a clear error site, not a crash.
NODE_POOL = None
settings = None
OPTIMIZATION_CONFIG = None
health_ledger = None
node_selector = None
transport = None
token_optimizer = None
shutdown_state = None


def assign_request_id():
    """Honor a sanitized inbound X-Request-Id, else generate one. Registered
    before the auth gate so even rejected requests carry an id; echoed on
    every response (after_response) and bound into error bodies/logs."""
    raw = request.headers.get("X-Request-Id", "")
    candidate = raw.strip()
    if candidate and len(candidate) <= 128 and all(32 <= ord(c) < 127 for c in candidate):
        g.request_id = candidate
    else:
        g.request_id = uuid.uuid4().hex
    return None


def echo_request_id(response):
    response.headers["X-Request-Id"] = g.get("request_id", "")
    return response


def require_bearer_token():
    if not settings.auth_token or request.path in ("/health", "/ready", "/metrics"):
        return None
    provided = request.headers.get("Authorization", "")
    # Bytes, not str: compare_digest raises TypeError on non-ASCII str, and
    # client-supplied headers can contain anything. utf-8 encoding never does.
    if not hmac.compare_digest(
        provided.encode("utf-8"), f"Bearer {settings.auth_token}".encode("utf-8")
    ):
        return Response(
            json.dumps({
                "error": {
                    "message": "Invalid or missing bearer token",
                    "type": "auth_error",
                },
                "request_id": g.get("request_id", ""),
            }),
            401,
            {"Content-Type": "application/json"},
        )
    return None


class HealthLedger:
    """
    Per-node failure and cooldown accounting. Records consecutive failures,
    computes exponentially growing cooldowns capped at a maximum, and answers
    usability queries. Nodes carry no failure state themselves.
    """

    # Headroom opinions from providers that omit reset headers live this
    # long before being treated as refilled (issue #48).
    BUDGET_DEFAULT_TTL = 60.0

    def __init__(self, nodes, cooldown_base, cooldown_max, lock=None,
                 budget_low_pct=25.0):
        self._cooldown_base = cooldown_base
        self._cooldown_max = cooldown_max
        # Below this remaining-percentage a node counts as "low" headroom for
        # budget-aware routing (issue #48); 0 or less is "depleted".
        self._budget_low_pct = budget_low_pct
        # Reentrant because NodeSelector.select() holds this same shared lock
        # while calling usable(); separate locks would break that atomicity.
        self._lock = lock if lock is not None else threading.RLock()
        self._state = {
            n.node_id: {
                "consecutive_failures": 0,
                "fail_until": 0.0,
                # Lifetime attempt outcomes (observability): recorded at the
                # same lock-guarded sites as health state so a report can't
                # tear them apart. reset_all() deliberately preserves these.
                "total_successes": 0,
                "total_failures": 0,
                # Quota headroom from upstream ratelimit headers; None until
                # the provider reports it (issue #48).
                "budget": None,
            }
            for n in nodes
        }

    def _entry(self, node):
        entry = self._state.get(node.node_id)
        if entry is None:
            raise ValueError(f"Unknown node_id: {node.node_id!r}")
        return entry

    def usable(self, node, now=None):
        """True unless the node is inside a cooldown window at time `now`."""
        if now is None:
            now = time.monotonic()
        with self._lock:
            return self._entry(node)["fail_until"] <= now

    def cooldown_remaining(self, node, now=None):
        """Seconds left in the node's cooldown at time `now` (0 when usable)."""
        if now is None:
            now = time.monotonic()
        with self._lock:
            return max(0.0, self._entry(node)["fail_until"] - now)

    def failure_count(self, node):
        with self._lock:
            return self._entry(node)["consecutive_failures"]

    def health_state(self, now=None):
        """All nodes' {consecutive_failures, cooldown_seconds, lifetime
        totals} under one lock and a single clock reading, so a report can't
        tear the pair apart."""
        if now is None:
            now = time.monotonic()
        with self._lock:
            return {
                nid: {
                    "consecutive_failures": e["consecutive_failures"],
                    "cooldown_seconds": round(max(0.0, e["fail_until"] - now), 3),
                    "total_successes": e["total_successes"],
                    "total_failures": e["total_failures"],
                }
                for nid, e in self._state.items()
            }

    def record_success(self, node):
        with self._lock:
            entry = self._entry(node)
            entry["consecutive_failures"] = 0
            entry["fail_until"] = 0.0
            entry["total_successes"] += 1

    def record_failure(self, node, now=None):
        if now is None:
            now = time.monotonic()
        with self._lock:
            entry = self._entry(node)
            entry["consecutive_failures"] += 1
            entry["total_failures"] += 1
            cooldown = min(
                self._cooldown_max,
                self._cooldown_base * (2 ** (entry["consecutive_failures"] - 1)),
            )
            entry["fail_until"] = now + cooldown

    def record_quota(self, node, remaining_pct, reset_seconds=None, now=None):
        """Store upstream-reported headroom (issue #48). reset_seconds starts
        the window after which the budget is considered refilled; providers
        that omit reset headers get a short default TTL so a stale headroom
        opinion can never outlive the traffic that produced it."""
        if now is None:
            now = time.monotonic()
        with self._lock:
            self._entry(node)["budget"] = {
                "pct": max(0.0, min(100.0, float(remaining_pct))),
                "reset_at": now + (self.BUDGET_DEFAULT_TTL if reset_seconds is None
                                   else reset_seconds),
            }

    def _live_budget(self, node, now):
        """The node's budget entry unless its reset window has elapsed
        (expired = refilled = no opinion), for callers holding the lock."""
        budget = self._entry(node)["budget"]
        if budget is None:
            return None
        if budget["reset_at"] is not None and budget["reset_at"] <= now:
            return None
        return budget

    def budget_class(self, node, now=None):
        """"healthy" / "low" / "depleted" headroom class, or None when the
        provider has not reported a quota (or the window expired)."""
        if now is None:
            now = time.monotonic()
        with self._lock:
            budget = self._live_budget(node, now)
        if budget is None:
            return None
        if budget["pct"] <= 0.0:
            return "depleted"
        return "low" if budget["pct"] < self._budget_low_pct else "healthy"

    def budget_remaining_pct(self, node, now=None):
        if now is None:
            now = time.monotonic()
        with self._lock:
            budget = self._live_budget(node, now)
        return None if budget is None else budget["pct"]

    def reset_all(self):
        with self._lock:
            for entry in self._state.values():
                entry["consecutive_failures"] = 0
                entry["fail_until"] = 0.0
                entry["budget"] = None


class NodeSelector:
    """
    Round-robin cursor over the node pool. Selects the next usable node and
    advances the cursor past each selection; if every node is cooling down,
    serves the cursor node anyway so requests never starve.
    """

    # Selection preference by headroom class (issue #48): healthy beats low
    # beats depleted; None (no quota reported) rides with "low" so unknown
    # providers keep their fair share without outranking known-good nodes.
    _BUDGET_RANK = {"healthy": 0, "low": 1, "depleted": 2, None: 1}

    def __init__(self, nodes, ledger, lock=None):
        self.nodes = list(nodes)
        self.ledger = ledger
        self._index = 0
        self._lock = lock if lock is not None else threading.RLock()

    @property
    def current_index(self):
        with self._lock:
            return self._index

    def select(self, now=None):
        """Atomically pick the next usable node and advance the cursor.

        Budget-aware (issue #48): among usable nodes, the first — in cursor
        order — belonging to the best headroom class present wins. With an
        all-healthy or quota-less pool this is exactly the historical
        first-usable scan; a scan that reaches a healthy node can stop
        immediately because nothing outranks it."""
        if now is None:
            now = time.monotonic()
        with self._lock:
            n = len(self.nodes)
            chosen = None
            best_rank = None
            for offset in range(n):
                i = (self._index + offset) % n
                if not self.ledger.usable(self.nodes[i], now):
                    continue
                rank = self._BUDGET_RANK.get(
                    self.ledger.budget_class(self.nodes[i], now), 1)
                if best_rank is None or rank < best_rank:
                    best_rank = rank
                    chosen = i
                if best_rank == 0:
                    break  # healthy: nothing in the pool outranks it
            if chosen is None:
                chosen = self._index % n
            node = self.nodes[chosen]
            self._index = (chosen + 1) % n
            return node


def node_health_snapshot(nodes, ledger, now=None):
    """Public per-node view for health checks (never includes keys)."""
    state = ledger.health_state(now=now)
    return [{
        "node_id": n.node_id,
        "proxy": n.proxy,
        "user_agent": n.user_agent,
        "fingerprint": n.fingerprint,
        "consecutive_failures": state[n.node_id]["consecutive_failures"],
        "cooldown_seconds": state[n.node_id]["cooldown_seconds"],
        "total_successes": state[n.node_id]["total_successes"],
        "total_failures": state[n.node_id]["total_failures"],
        "budget_remaining_pct": ledger.budget_remaining_pct(n, now=now),
    } for n in nodes]


def create_app(cfg=None, optimization_config=None) -> Flask:
    """Build settings -> services -> Flask app from the environment.

    The only place anything is constructed; importing this module runs
    nothing. Both config objects are injectable for tests."""
    cfg = cfg or Settings.from_env()
    opt = optimization_config or OptimizationConfig.from_env()
    configure_logging(cfg)

    nodes = build_node_pool()

    global settings, OPTIMIZATION_CONFIG, NODE_POOL
    global health_ledger, node_selector, transport, token_optimizer, app
    global shutdown_state
    settings = cfg
    OPTIMIZATION_CONFIG = opt
    NODE_POOL = nodes

    # One shared reentrant lock keeps ledger updates atomic with selection.
    _ledger_lock = threading.RLock()
    health_ledger = HealthLedger(nodes, cfg.node_cooldown_base, cfg.node_cooldown_max, lock=_ledger_lock)
    node_selector = NodeSelector(nodes, health_ledger, lock=_ledger_lock)

    # Retry/failover transport: session/sleeper/rng default to production
    # adapters inside the module.
    if cfg.transport == "curl_cffi":
        try:
            import curl_cffi  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "TRANSPORT=curl_cffi requires the optional dependency "
                "curl_cffi (pip install curl_cffi); refusing to silently "
                "degrade the persona fingerprint layer") from exc
        session_factory = failover.CurlCffiSessionAdapter
    else:
        session_factory = None
    transport = FailoverTransport(
        selector=node_selector,
        ledger=health_ledger,
        max_retries=cfg.max_retries,
        timeout=cfg.request_timeout,
        backoff_base=cfg.retry_backoff_base,
        backoff_max=cfg.retry_backoff_max,
        retry_posts=cfg.retry_posts,
        persona_hygiene=cfg.persona_hygiene,
        anonymity_failover=cfg.anonymity_failover,
        failover_max_wait=cfg.failover_max_wait,
        failover_max_waiters=cfg.failover_max_waiters,
        redistribution_jitter=cfg.redistribution_jitter,
        session_factory=session_factory,
    )


    token_optimizer = TokenOptimizer(
        config=opt, model_name=cfg.default_model, persona_hygiene=cfg.persona_hygiene
    )

    # Shutdown draining: armed by the signal handlers installed below; the
    # streamed view consults it between chunks (see guarded_stream).
    shutdown_state = ShutdownState()
    install_shutdown_handlers(shutdown_state, cfg.stream_drain_window)

    application = Flask(__name__)
    application.add_url_rule(
        "/v1/<path:path>", "dynamic_failover_proxy", dynamic_failover_proxy,
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    )
    application.add_url_rule("/health", "health_check", health_check)
    application.add_url_rule("/ready", "ready_check", ready_check)
    application.add_url_rule("/metrics", "metrics", metrics)
    application.add_url_rule("/v1/models", "list_models", list_models)
    application.before_request(assign_request_id)
    application.before_request(require_bearer_token)
    application.after_request(echo_request_id)
    application.register_error_handler(Exception, handle_exception)
    app = application
    return app


# Built lazily on first attribute access so gunicorn's `rotator:app` works
# while a bare `import rotator` constructs nothing. The `app` global comes
# into existence only when something builds it — deliberately NOT declared
# as a placeholder, or `rotator:app` would resolve to None without building.
def get_app() -> Flask:
    """Build the Flask app from the environment once; cached thereafter."""
    if globals().get("app") is None:
        try:
            create_app()
        except Exception as e:
            logger.critical(f"Failed to initialize proxy: {e}")
            raise SystemExit(1)
    return app


def __getattr__(name):
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Token Management & Context Optimization Engine
# ─────────────────────────────────────────────────────────────────────────────
_PROVIDER_PROFILES = {
    "openai": {"max_context": 128000, "reserved_tokens": 4096},
    "anthropic": {"max_context": 200000, "reserved_tokens": 8192},
    "groq": {"max_context": 32000, "reserved_tokens": 2048},
}


def _env_prompt_cache_bool(raw):
    # Prompt-caching parsing historically stripped whitespace; keep exact.
    return raw.strip().lower() == "true"


@dataclass(frozen=True)
class OptimizationConfig:
    """Every knob of the optimization pipeline, parsed from the environment
    in exactly one place (from_env). Tests build explicit instances instead
    of patching globals."""

    enabled: bool = True
    streaming_fastpath: bool = True
    remove_duplicates: bool = True
    strip_whitespace: bool = True
    max_context_tokens: int = 120000
    reserved_response_tokens: int = 4000
    enable_semantic_compression: bool = False
    semantic_compression_ratio: float = 0.5
    # Prompt-caching marker injection is Anthropic-style and breaks
    # OpenAI-format payloads, so it never defaults on.
    enable_prompt_caching: bool = False
    enable_importance_scoring: bool = False
    min_message_importance: float = 0.3
    enable_recursive_summarization: bool = False
    compression_threshold: float = 0.85
    summarization_model: str = "gpt-4o-mini"

    @classmethod
    def from_env(cls) -> "OptimizationConfig":
        """Overlay environment settings onto the dataclass defaults; provider
        profiles only supply the context-budget fallbacks."""
        profile_name = os.getenv("PROVIDER_PROFILE", "openai")
        profile = _PROVIDER_PROFILES.get(profile_name)
        if profile is None:
            logger.warning(f"Unknown provider profile '{profile_name}', using defaults")
            base = cls()
        else:
            logger.info(f"Applied provider profile: {profile_name} (max_context={profile['max_context']})")
            base = cls(
                max_context_tokens=profile["max_context"],
                reserved_response_tokens=profile["reserved_tokens"],
            )

        overrides = {}
        for field_name, env_name, cast in [
            ("enabled", "ENABLE_CONTEXT_COMPRESSION", _env_bool),
            ("streaming_fastpath", "ENABLE_STREAMING_FASTPATH", _env_bool),
            ("remove_duplicates", "REMOVE_DUPLICATE_MESSAGES", _env_bool),
            ("strip_whitespace", "STRIP_WHITESPACE", _env_bool),
            ("enable_semantic_compression", "ENABLE_SEMANTIC_COMPRESSION", _env_bool),
            ("semantic_compression_ratio", "SEMANTIC_COMPRESSION_RATIO", float),
            ("enable_prompt_caching", "ENABLE_PROMPT_CACHING", _env_prompt_cache_bool),
            ("enable_importance_scoring", "ENABLE_IMPORTANCE_SCORING", _env_bool),
            ("min_message_importance", "MIN_MESSAGE_IMPORTANCE", float),
            ("enable_recursive_summarization", "ENABLE_RECURSIVE_SUMMARIZATION", _env_bool),
            ("compression_threshold", "COMPRESSION_THRESHOLD", float),
            ("summarization_model", "SUMMARIZATION_MODEL", str),
        ]:
            raw = os.getenv(env_name)
            if raw is not None:
                overrides[field_name] = cast(raw)
        for field_name, env_name, cast in [
            ("max_context_tokens", "MAX_CONTEXT_TOKENS", int),
            ("reserved_response_tokens", "RESERVED_RESPONSE_TOKENS", int),
        ]:
            raw = os.getenv(env_name)
            if raw is not None:
                overrides[field_name] = cast(raw)

        return replace(base, **overrides)


# Top-level chat-payload fields that exist to carry end-user/account identity
# upstream; removed when PERSONA_HYGIENE is on (see _strip_identity_fields).
_IDENTITY_PAYLOAD_FIELDS = frozenset(
    {"user", "metadata", "prompt_cache_key", "safety_identifier"}
)


class TokenOptimizer:
    """
    Modular token-management pipeline applied to chat/completions payloads.

    Contract (see optimize_context): pure payload-in/payload-out — the input
    is never mutated; the returned payload may share nothing with it. The
    optimizer owns routing (only chat/completions paths are touched) and
    enabling (single gate in OptimizationConfig), and it never raises for
    request data: any internal failure logs and returns the payload
    unoptimized rather than breaking the proxied request.

    Pipeline Stages (all independently toggleable via OptimizationConfig):
    1. Structural Hygiene - Remove duplicates, strip whitespace (native Python)
    2. Semantic Compression - LLMLingua for aggressive context compression
    3. Prompt Caching - Cache control headers for Anthropic/OpenAI
    4. Importance Scoring - Caveman-inspired message prioritization
    5. Recursive Summarization - Summarize old context when needed
    6. Smart Truncation - Fallback token-based message dropping

    Improvements:
    - Post-optimization verification to prevent double-counting
    - Streaming fast-path bypass for real-time responses
    - Provider-specific token calculations
    """

    def __init__(self, config=None, model_name="gpt-4o", persona_hygiene=False):
        self.config = config if config is not None else OptimizationConfig()
        self.model_name = model_name
        self.summarization_model = self.config.summarization_model
        # PERSONA_HYGIENE payload stage: strip provider identity fields
        # (user/metadata/prompt_cache_key/safety_identifier) from chat
        # payloads. Injected from Settings here rather than living on
        # OptimizationConfig so the flag is parsed from the environment in
        # exactly one place and header/payload hygiene can never diverge.
        self.persona_hygiene = persona_hygiene
        
        # Initialize tiktoken encoder
        if TIKTOKEN_AVAILABLE:
            try:
                self.encoding = tiktoken.encoding_for_model(model_name)
            except KeyError:
                logger.warning(f"Model {model_name} not found in tiktoken, using cl100k_base")
                self.encoding = tiktoken.get_encoding("cl100k_base")
        else:
            self.encoding = None
        
        # Initialize llmlingua compressor if available and enabled
        self.compressor = None
        if LLMLINGUA_AVAILABLE and self.config.enable_semantic_compression:
            try:
                self.compressor = PromptCompressor()
                logger.info("LLMLingua semantic compression enabled")
            except Exception as e:
                logger.warning(f"Failed to initialize llmlingua: {e}")
                self.compressor = None

    def count_tokens(self, text: str) -> int:
        """Count tokens in a text string."""
        if not self.encoding:
            return len(text) // 4 if text else 0
        if not text:
            return 0
        return len(self.encoding.encode(text))
    
    def count_message_tokens(self, messages: list) -> int:
        """Count tokens in a message array (OpenAI format)."""
        if not self.encoding:
            total = 0
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, str):
                    total += len(content) // 4
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            total += len(item.get("text", "")) // 4
            return total + len(messages) * 4
        
        total_tokens = 0
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            
            total_tokens += self.count_tokens(role)
            total_tokens += 4
            
            if isinstance(content, str):
                total_tokens += self.count_tokens(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            total_tokens += self.count_tokens(item.get("text", ""))
                        elif item.get("type") == "image_url":
                            total_tokens += 85
        
        total_tokens += 2
        return total_tokens
    
    def optimize_context(self, payload: dict, *, path: str, is_streaming: bool = False) -> dict:
        """
        Main optimization pipeline - executes enabled stages sequentially.

        Owns routing and enabling: payloads are only touched when `path` is
        a chat/completions endpoint and the gate in OptimizationConfig is
        on; anything else comes back untouched (same object).

        Purity: the input payload and its message dicts are never mutated —
        each message is shallow-copied at entry, so top-level keys are
        private to the pipeline; deeply nested values (e.g. list-typed
        content parts) are shared until a stage rewrites them. On any
        internal failure the input is returned unoptimized — optimization
        must never break the proxied request.

        The returned payload's max_tokens may be clamped down to fit the
        remaining context budget (client value wins whenever it fits);
        clamping is logged.

        Persona hygiene (PERSONA_HYGIENE) runs ahead of the enabled gate:
        provider identity fields are stripped even when token compression
        is disabled, because unlinkability is a privacy property, not an
        optimization.
        """
        cfg = self.config
        if not path.endswith("chat/completions") or not isinstance(payload, dict):
            return payload

        payload = self._strip_identity_fields(payload)

        if not cfg.enabled:
            return payload

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return payload

        try:
            logger.info(f"Applying token optimization to request... (streaming={is_streaming})")
            return self._optimize(dict(payload), [dict(m) for m in messages], is_streaming)
        except Exception:
            logger.exception("Context optimization failed; forwarding payload unoptimized")
            return payload

    def _strip_identity_fields(self, payload: dict) -> dict:
        """Persona-hygiene payload stage: remove fields that exist to carry
        end-user/account identity upstream (OpenAI's user/safety_identifier/
        prompt_cache_key, metadata.user_id). Copy-on-write like every stage;
        when disabled or nothing matches, the input object comes back
        untouched."""
        if not self.persona_hygiene:
            return payload
        present = _IDENTITY_PAYLOAD_FIELDS.intersection(payload)
        if not present:
            return payload
        logger.info(
            "Stripped identity fields from chat payload: "
            + ", ".join(sorted(present)),
            extra={"event": "payload_hygiene", "count": len(present),
                   "fields": sorted(present)},
        )
        return {k: v for k, v in payload.items() if k not in _IDENTITY_PAYLOAD_FIELDS}

    def _optimize(self, payload: dict, messages: list, is_streaming: bool) -> dict:
        cfg = self.config

        # Fast path for streaming: skip expensive optimizations
        if is_streaming and cfg.streaming_fastpath:
            logger.debug("Streaming fast-path: minimal optimization applied")
            if cfg.remove_duplicates:
                messages = self._remove_duplicates(messages)
            if cfg.strip_whitespace:
                messages = self._strip_whitespace(messages)
            return {**payload, "messages": messages}

        original_token_count = self.count_message_tokens(messages)
        logger.debug(f"Original message token count: {original_token_count}")

        # Stage 1: Structural Hygiene (always runs first if enabled)
        if cfg.remove_duplicates:
            messages = self._remove_duplicates(messages)

        if cfg.strip_whitespace:
            messages = self._strip_whitespace(messages)

        current_token_count = self.count_message_tokens(messages)
        available_tokens = cfg.max_context_tokens - cfg.reserved_response_tokens

        logger.debug(
            f"After structural hygiene: {current_token_count} tokens "
            f"(available: {available_tokens})"
        )

        # Stage 2: Semantic Compression with LLMLingua
        if cfg.enable_semantic_compression and self.compressor:
            messages = self._apply_semantic_compression(messages, cfg.semantic_compression_ratio)
            current_token_count = self.count_message_tokens(messages)
            logger.debug(f"After semantic compression: {current_token_count} tokens")

        # Stage 3: Prompt Caching (add cache control metadata)
        if cfg.enable_prompt_caching:
            messages = self._add_prompt_caching(messages)

        # Stage 4: Importance Scoring (Caveman-inspired)
        if cfg.enable_importance_scoring:
            messages = self._filter_by_importance(messages, cfg.min_message_importance)
            current_token_count = self.count_message_tokens(messages)
            logger.debug(f"After importance filtering: {current_token_count} tokens")

        # Stage 5: Recursive Summarization
        if cfg.enable_recursive_summarization and current_token_count > available_tokens * cfg.compression_threshold:
            messages = self._recursive_summarization(messages, available_tokens)
            current_token_count = self.count_message_tokens(messages)
            logger.debug(f"After summarization: {current_token_count} tokens")

        # Stage 6: Smart Truncation (fallback)
        if current_token_count > available_tokens * cfg.compression_threshold:
            logger.warning(
                f"Context at {current_token_count/available_tokens:.1%} capacity. "
                f"Applying truncation..."
            )
            messages = self._truncate_messages(messages, available_tokens)

        # POST-OPTIMIZATION VERIFICATION: Prevent double-counting trap
        final_token_count = self.count_message_tokens(messages)
        if final_token_count > available_tokens:
            logger.error(
                f"⚠️  Post-optimization verification FAILED: "
                f"{final_token_count} tokens > {available_tokens} available. "
                f"Forcing aggressive truncation..."
            )
            messages = self._aggressive_truncate(messages, available_tokens)
            final_token_count = self.count_message_tokens(messages)

        savings = original_token_count - final_token_count
        savings_pct = (savings / original_token_count * 100) if original_token_count > 0 else 0

        if savings > 0:
            logger.info(
                f"Token optimization saved {savings:,} tokens ({savings_pct:.1f}% reduction). "
                f"Final count: {final_token_count:,} / {available_tokens:,}"
            )

        requested_max = payload.get("max_tokens", cfg.reserved_response_tokens)
        clamped_max = max(1, min(requested_max, cfg.max_context_tokens - final_token_count))
        if clamped_max != requested_max:
            logger.info(f"Clamped max_tokens {requested_max} -> {clamped_max} to fit the context budget")

        result = {
            **payload,
            "messages": messages,
            "max_tokens": clamped_max,
        }

        return result
    
    def _remove_duplicates(self, messages: list) -> list:
        """Remove consecutive duplicate messages."""
        if len(messages) <= 1:
            return messages
        
        optimized = [messages[0]]
        for i in range(1, len(messages)):
            curr_content = str(messages[i].get("content", ""))
            prev_content = str(optimized[-1].get("content", ""))
            
            if curr_content.strip() != prev_content.strip():
                optimized.append(messages[i])
            else:
                logger.debug(f"Removed duplicate {messages[i].get('role')} message")
        
        return optimized
    
    def _strip_whitespace(self, messages: list) -> list:
        """Strip excessive whitespace from message content."""
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                cleaned = re.sub(r'\n\s*\n', '\n\n', content.strip())
                cleaned = re.sub(r'  +', ' ', cleaned)
                if cleaned != content:
                    msg["content"] = cleaned
        return messages
    
    def _apply_semantic_compression(self, messages: list, target_ratio: float) -> list:
        """
        Apply LLMLingua semantic compression to reduce token count while preserving meaning.
        Uses compress_prompt() per message so per-message boundaries survive compression.
        Based on research from Microsoft's LLMLingua project.
        """
        if not self.compressor or not messages:
            return messages

        try:
            compressed_messages = []
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    result = self.compressor.compress_prompt(
                        [content],
                        instruction="Preserve key information, code snippets, and technical details.",
                        ratio=target_ratio,
                    )
                    compressed_text = (
                        result.get("compressed_prompt", content)
                        if isinstance(result, dict) else str(result)
                    )
                    new_msg = msg.copy()
                    new_msg["content"] = compressed_text
                    compressed_messages.append(new_msg)
                else:
                    compressed_messages.append(msg)

            logger.info(f"Semantic compression applied with ratio {target_ratio}")
            return compressed_messages

        except Exception as e:
            logger.error(f"Semantic compression failed: {e}. Falling back to original messages.")
            return messages
    
    def _add_prompt_caching(self, messages: list) -> list:
        """
        Add Anthropic-style cache_control markers for gateways that honor them.
        OpenAI's Chat Completions API caches automatically and rejects unknown
        content-part fields, so this stage is off unless explicitly enabled.
        """
        if not messages:
            return messages

        # Mark system message and early context for caching
        for i, msg in enumerate(messages):
            if msg.get("role") == "system" or (i < len(messages) // 3):
                content = msg.get("content", "")
                if isinstance(content, str):
                    msg["content"] = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"}
                        }
                    ]

        logger.debug("Prompt caching markers added to system and early context")
        return messages
    
    def _filter_by_importance(self, messages: list, min_importance: float) -> list:
        """
        Caveman-inspired importance scoring for message prioritization.
        Scores messages based on:
        - Recency (newer = more important)
        - Content length (longer = potentially more important)
        - Role (user queries often more important than assistant filler)
        - Keyword density (technical terms, questions, action items)

        System prompts are never dropped, and dropped messages are re-inserted
        when their removal would make two same-role messages adjacent, so
        provider-side role-alternation validation keeps passing.
        """
        if len(messages) <= 2:
            return messages

        scored_messages = []
        for i, msg in enumerate(messages):
            score = 0.0
            content = str(msg.get("content", ""))
            role = msg.get("role", "")

            # Recency score (0.0 to 0.3)
            recency_score = i / max(len(messages) - 1, 1) * 0.3
            score += recency_score

            # Content length score (0.0 to 0.2)
            length_score = min(len(content) / 1000, 1.0) * 0.2
            score += length_score

            # Role bonus (0.0 to 0.2)
            if role == "user":
                score += 0.2  # User queries are important
            elif role == "system":
                score += 0.15  # System prompts are important

            # Keyword density (0.0 to 0.3)
            importance_keywords = ['?', '!', 'TODO', 'FIXME', 'important', 'critical',
                                   'bug', 'error', 'fix', 'implement', 'review']
            keyword_matches = sum(1 for kw in importance_keywords if kw.lower() in content.lower())
            keyword_score = min(keyword_matches / 5, 1.0) * 0.3
            score += keyword_score

            scored_messages.append(score)

        keep = set(range(max(0, len(messages) - 3), len(messages)))
        for i, score in enumerate(scored_messages):
            if score >= min_importance:
                keep.add(i)
        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                keep.add(i)

        filtered = [msg for i, msg in enumerate(messages) if i in keep]
        return self._repair_role_collisions(messages, keep, filtered)

    def _repair_role_collisions(self, messages: list, keep: set, filtered: list) -> list:
        """Re-insert dropped messages where filtering made roles collide."""
        if len(filtered) < 2:
            return filtered

        orig_index = [i for i in range(len(messages)) if i in keep]
        result = list(filtered)
        inserted = set()
        j = 1
        while j < len(result):
            prev_orig = orig_index[j - 1]
            curr_orig = orig_index[j]
            if result[j - 1].get("role") == result[j].get("role") and curr_orig - prev_orig > 1:
                gap = [i for i in range(prev_orig + 1, curr_orig)
                       if i not in keep and i not in inserted]
                if gap:
                    pick = gap[0]
                    orig_index.insert(j, pick)
                    result.insert(j, messages[pick])
                    inserted.add(pick)
                j += 1
            else:
                j += 1
        return result
    
    def _recursive_summarization(self, messages: list, max_tokens: int) -> list:
        """
        Recursively summarize older context when approaching token limits.
        Inspired by techniques from Caveman and context compression research.
        """
        if len(messages) <= 3:
            return messages
        
        # Separate system message, old context, and recent context
        system_msg = None
        old_context = []
        recent_context = []
        
        cutoff = max(3, len(messages) // 2)  # Keep second half as recent
        
        for i, msg in enumerate(messages):
            if msg.get("role") == "system" and system_msg is None:
                system_msg = msg
            elif i < cutoff:
                old_context.append(msg)
            else:
                recent_context.append(msg)
        
        # Check if we need summarization
        test_messages = ([system_msg] if system_msg else []) + old_context + recent_context
        if self.count_message_tokens(test_messages) <= max_tokens:
            return messages
        
        # Summarize old context
        if old_context:
            try:
                old_text = "\n\n".join([str(m.get("content", "")) for m in old_context])
                
                # Create summarization prompt
                summary_prompt = (
                    f"Summarize the following conversation history concisely, "
                    f"preserving key facts, decisions, and technical details. "
                    f"Keep it under 500 tokens:\n\n{old_text}"
                )
                
                # In production, this would call the LLM API for summarization
                # For now, we use a simple truncation as placeholder
                summary = f"[Summary of {len(old_context)} previous messages: {old_text[:500]}...]"
                
                summary_msg = {
                    "role": "system",
                    "content": summary,
                    "metadata": {"summarized_from": len(old_context), "type": "context_summary"}
                }
                
                logger.info(f"Summarized {len(old_context)} old messages into summary")
                old_context = [summary_msg]
                
            except Exception as e:
                logger.error(f"Summarization failed: {e}. Using truncation fallback.")
                old_context = old_context[-2:]  # Keep only last 2 old messages
        
        return ([system_msg] if system_msg else []) + old_context + recent_context
    
    def _truncate_messages(self, messages: list, max_tokens: int) -> list:
        """Truncate oldest messages to fit within token limit (fallback strategy)."""
        system_msg = None
        conversation = []
        
        for msg in messages:
            if msg.get("role") == "system" and system_msg is None:
                system_msg = msg
            else:
                conversation.append(msg)
        
        while len(conversation) > 0:
            test_messages = ([system_msg] if system_msg else []) + conversation
            token_count = self.count_message_tokens(test_messages)
            
            if token_count <= max_tokens:
                break
            
            removed = conversation.pop(0)
            logger.debug(f"Truncated {removed.get('role')} message to reduce context size")
        
        if conversation:
            last_user_idx = None
            for i in range(len(conversation) - 1, -1, -1):
                if conversation[i].get("role") == "user":
                    last_user_idx = i
                    break
            
            if last_user_idx is not None:
                content = conversation[last_user_idx].get("content", "")
                if isinstance(content, str):
                    while self.count_message_tokens(
                        ([system_msg] if system_msg else []) + conversation
                    ) > max_tokens and len(content) > 100:
                        mid_point = len(content) // 2
                        truncate_size = min(500, len(content) // 10)
                        content = (
                            content[:mid_point - truncate_size // 2] +
                            "\n\n[...truncated...]\n\n" +
                            content[mid_point + truncate_size // 2:]
                        )
                        conversation[last_user_idx]["content"] = content
        
        return ([system_msg] if system_msg else []) + conversation
    
    def _aggressive_truncate(self, messages: list, max_tokens: int) -> list:
        """
        Aggressive truncation fallback when post-optimization verification fails.
        Keeps only the most recent messages and the system prompt.
        """
        system_msg = None
        conversation = []
        
        for msg in messages:
            if msg.get("role") == "system":
                if system_msg is None:
                    system_msg = msg
            else:
                conversation.append(msg)
        
        # Keep only the last N message pairs (user+assistant = 2 messages)
        keep_count = max(2, len(conversation) // 4)  # Keep 25% or minimum 2
        conversation = conversation[-keep_count:]
        
        # If still over limit, aggressively truncate content
        while len(conversation) > 0:
            test_messages = ([system_msg] if system_msg else []) + conversation
            token_count = self.count_message_tokens(test_messages)
            
            if token_count <= max_tokens:
                break
            
            # Remove oldest message
            conversation.pop(0)
        
        # Final resort: truncate the last user message heavily
        if conversation and self.count_message_tokens(([system_msg] if system_msg else []) + conversation) > max_tokens:
            last_user_idx = None
            for i in range(len(conversation) - 1, -1, -1):
                if conversation[i].get("role") == "user":
                    last_user_idx = i
                    break
            
            if last_user_idx is not None:
                content = conversation[last_user_idx].get("content", "")
                if isinstance(content, str):
                    # Keep only first and last 200 characters
                    if len(content) > 400:
                        conversation[last_user_idx]["content"] = (
                            f"{content[:200]}\n\n[...heavily truncated...]\n\n{content[-200:]}"
                        )
        
        result = ([system_msg] if system_msg else []) + conversation
        logger.warning(f"Aggressive truncation applied: {len(messages)} → {len(result)} messages")
        return result


# Emitted when draining cuts an in-flight SSE stream: an OpenAI-style error
# object (so SDK clients surface a failure, not a silent truncation) followed
# by [DONE] (so their parser terminates cleanly).
TERMINAL_SSE_EVENT = (
    b'data: {"error": {"message": "proxy shutting down; completion interrupted", '
    b'"type": "proxy_shutdown"}}\n\ndata: [DONE]\n\n'
)


def guarded_stream(chunks, *, state, request_id=""):
    """Wrap a streamed upstream body with shutdown awareness.

    Passes chunks through verbatim while the process is not draining; once
    ShutdownState.draining() flips, yields the terminal SSE event and stops,
    closing the inner iterable so the upstream connection is released. The
    finally also closes inner when this generator itself is closed early
    (client disconnect mid-stream).
    """
    try:
        state.stream_started()
        try:
            for chunk in chunks:
                if state.draining():
                    logger.warning(
                        "Draining: cutting in-flight SSE stream with terminal event",
                        extra={"event": "stream_drain_cut", "request_id": request_id},
                    )
                    yield TERMINAL_SSE_EVENT
                    return
                yield chunk
        finally:
            state.stream_finished()
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()


def _drain_and_exit(state, exit_fn, sleeper):
    """Dev-server shutdown path: nobody above us orchestrates a graceful
    stop, so hold the process until in-flight streams drain or the window
    elapses, then exit. Runs on the main thread inside the signal handler;
    werkzeug's worker threads are daemons, so exiting ends them."""
    logger.info(
        f"Shutdown signal: draining {state.inflight} in-flight stream(s) "
        f"(STREAM_DRAIN_WINDOW elapsed means immediate exit)"
    )
    while not state.draining() and state.inflight > 0:
        sleeper(0.05)
    if state.inflight > 0:
        # Window elapsed mid-drain: guards only observe draining() between
        # chunks, so give them one settle beat to flush the terminal event
        # before the process goes away.
        sleeper(0.25)
    exit_fn(0)


def _is_bare_server_handler(prev):
    """SIG_DFL/SIG_IGN/default_int_handler mean no supervisor owns graceful
    shutdown — we must orchestrate the exit ourselves."""
    return prev is None or prev in (signal.SIG_DFL, signal.SIG_IGN, signal.default_int_handler)


def install_shutdown_handlers(state, grace_seconds, *,
                              get_signal=signal.getsignal,
                              set_signal=signal.signal,
                              exit_fn=os._exit,
                              sleeper=time.sleep):
    """Arm draining on SIGTERM/SIGINT.

    Under gunicorn the previous handler owns the graceful-stop machinery
    (gthread stops accepting, waits out graceful_timeout): arm first, then
    delegate to it. Under the bare werkzeug dev server there is no such
    machinery (previous handler is the default), so drain in-process and
    exit once streams finish or the window elapses.

    Idempotent: installing twice keeps the first handlers. Must run on the
    main thread (create_app qualifies under every entrypoint).
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        prev = get_signal(sig)
        if getattr(prev, "_rotator_shutdown_chain", False):
            return

        def handler(signum, frame, _prev=prev):
            state.arm(grace_seconds)
            if _is_bare_server_handler(_prev):
                _drain_and_exit(state, exit_fn, sleeper)
            else:
                _prev(signum, frame)

        handler._rotator_shutdown_chain = True
        set_signal(sig, handler)


def dynamic_failover_proxy(path):
    """
    Handle incoming LLM API requests with automatic node rotation and failover.
    
    Args:
        path: The API endpoint path (e.g., 'chat/completions')
    
    Returns:
        Response from the LLM provider or 502 if all nodes fail
    """
    payload = request.get_data()
    method = request.method

    # Parse JSON payload (if applicable)
    parsed_payload = None
    is_streaming = False
    if payload and request.is_json:
        try:
            parsed_payload = request.get_json(force=True)

            # Detect streaming requests from the body (OpenAI convention)
            if isinstance(parsed_payload, dict):
                is_streaming = bool(parsed_payload.get("stream", False))

        except Exception as e:
            logger.warning(f"Could not parse JSON payload: {e}")
            parsed_payload = None

    # Optimization owns routing and enabling; identity change tells us
    # whether anything was rewritten.
    if isinstance(parsed_payload, dict):
        optimized = token_optimizer.optimize_context(
            parsed_payload, path=path, is_streaming=is_streaming
        )
        if optimized is not parsed_payload:
            parsed_payload = optimized
            payload = json.dumps(parsed_payload).encode('utf-8')

    # Legacy clients may signal streaming via query param instead of body.
    # Normalize the upstream body so providers actually stream back.
    wants_stream = is_streaming or request.args.get("stream") == "true"
    stream_upstream = wants_stream and path.endswith("chat/completions")
    if stream_upstream and isinstance(parsed_payload, dict) and not parsed_payload.get("stream"):
        parsed_payload["stream"] = True
        payload = json.dumps(parsed_payload).encode('utf-8')

    # Construct the full target URL
    url = f"{settings.target_provider_url}/{path}"
    request_id = g.get("request_id", "")
    logger.info(
        f"Proxying {method} /v1/{path} (streaming={stream_upstream})",
        extra={"event": "proxy_request", "request_id": request_id,
               "method": method, "path": path, "streaming": stream_upstream},
    )

    result = transport.send(
        method=method,
        url=url,
        headers=dict(request.headers),
        payload=payload,
        stream=stream_upstream,
        request_id=request_id,
        canonical=parsed_payload if isinstance(parsed_payload, dict) else None,
    )

    if isinstance(result, AllNodesFailed):
        logger.critical(
            f"All retry attempts exhausted ({result.attempts} attempt(s), "
            f"MAX_RETRIES={settings.max_retries}). Last error: {result.last_error}",
            extra={"event": "all_nodes_failed", "request_id": request_id,
                   "attempts": result.attempts, "max_retries": settings.max_retries},
        )
        return Response(
            json.dumps({
                "error": {
                    "message": "Proxy Gateway Error: All backend nodes exhausted or rate-limited",
                    "type": "gateway_error",
                    "last_error": result.last_error
                },
                "request_id": g.get("request_id", ""),
            }),
            502,
            {"Content-Type": "application/json"}
        )

    if stream_upstream:
        return Response(
            guarded_stream(result.body(), state=shutdown_state,
                           request_id=request_id),
            result.status_code, result.header_pairs,
        )

    body = result.body()
    # Log token usage only for buffered JSON bodies; streamed SSE responses
    # returned above never buffer here.
    if result.status_code == 200 and parsed_payload and isinstance(body, bytes):
        try:
            usage = json.loads(body).get("usage", {})
            if usage:
                logger.info(
                    f"Token usage - Prompt: {usage.get('prompt_tokens', 'N/A')}, "
                    f"Completion: {usage.get('completion_tokens', 'N/A')}, "
                    f"Total: {usage.get('total_tokens', 'N/A')}",
                    extra={"event": "token_usage", "request_id": request_id,
                           "node_id": result.node_id,
                           "prompt_tokens": usage.get("prompt_tokens"),
                           "completion_tokens": usage.get("completion_tokens"),
                           "total_tokens": usage.get("total_tokens")},
                )
        except Exception:
            pass

    return Response(body, result.status_code, result.header_pairs)


def nodes_available_count():
    """Nodes not currently in cooldown, per the ledger's own clock."""
    state = health_ledger.health_state()
    return sum(1 for e in state.values() if e["cooldown_seconds"] <= 0)


def metrics():
    """Prometheus text exposition of the per-node lifetime counters and a
    usable-node gauge. Hand-rolled on purpose: the format for two counters
    and one gauge is ~15 lines, not a dependency."""
    state = health_ledger.health_state()
    # Recompute from the same `state` snapshot rather than calling
    # nodes_available_count(): one lock/clock read keeps gauge and counters
    # from tearing apart.
    available = sum(1 for e in state.values() if e["cooldown_seconds"] <= 0)
    lines = [
        "# HELP llm_rotator_node_requests_total Lifetime upstream attempt outcomes per node.",
        "# TYPE llm_rotator_node_requests_total counter",
    ]
    for nid in sorted(state):
        lines.append(
            f'llm_rotator_node_requests_total{{node_id="{nid}",outcome="success"}} '
            f'{state[nid]["total_successes"]}'
        )
        lines.append(
            f'llm_rotator_node_requests_total{{node_id="{nid}",outcome="failure"}} '
            f'{state[nid]["total_failures"]}'
        )
    lines.extend([
        "# HELP llm_rotator_nodes_available Nodes not currently in cooldown.",
        "# TYPE llm_rotator_nodes_available gauge",
        f"llm_rotator_nodes_available {available}",
    ])
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


def health_check():
    """Health check endpoint for monitoring."""
    nodes = node_health_snapshot(NODE_POOL, health_ledger)
    return jsonify({
        "status": "healthy",
        "nodes_configured": len(NODE_POOL),
        "nodes_available": sum(1 for e in nodes if e["cooldown_seconds"] <= 0),
        "current_node_index": node_selector.current_index,
        "nodes": nodes,
        "token_optimization_enabled": OPTIMIZATION_CONFIG.enabled,
        "max_context_tokens": OPTIMIZATION_CONFIG.max_context_tokens,
        "reserved_response_tokens": OPTIMIZATION_CONFIG.reserved_response_tokens
    }), 200


def ready_check():
    """Readiness for orchestrators: 503 while every node is in cooldown."""
    available = nodes_available_count()
    if available == 0:
        return jsonify({"status": "unavailable", "nodes_available": 0}), 503
    return jsonify({"status": "ready", "nodes_available": available}), 200


def list_models():
    """Proxy model listing endpoint (full failover via the transport)."""
    result = transport.send(
        method="GET",
        url=f"{settings.target_provider_url}/models",
        headers={},
    )
    if isinstance(result, AllNodesFailed):
        logger.error(f"Failed to fetch models: {result.last_error}")
        return jsonify({
            "error": "Failed to fetch models from upstream",
            "request_id": g.get("request_id", ""),
        }), 502
    return Response(result.body(), result.status_code, result.header_pairs)


def handle_exception(e):
    """Global exception handler; HTTP errors keep their own status codes."""
    if isinstance(e, HTTPException):
        return e
    logger.exception(
        f"Unhandled exception: {e}",
        extra={"event": "unhandled_exception",
               "request_id": g.get("request_id", "")},
    )
    return jsonify({
        "error": {
            "message": "Internal proxy error",
            "type": "internal_error"
        },
        "request_id": g.get("request_id", ""),
    }), 500


if __name__ == '__main__':
    try:
        application = create_app()
    except Exception as e:
        logger.critical(f"Failed to initialize proxy: {e}")
        raise SystemExit(1)
    logger.info("=" * 70)
    logger.info("🚀 Secure Tailscale LLM Proxy Rotator Starting...")
    logger.info("=" * 70)
    logger.info(f"Binding to: {settings.bind_host}:{settings.bind_port}")
    logger.info(f"Target Provider: {settings.target_provider_url}")
    logger.info(f"Token Optimization: {'ENABLED' if OPTIMIZATION_CONFIG.enabled else 'DISABLED'}")
    logger.info(f"Max Context Tokens: {OPTIMIZATION_CONFIG.max_context_tokens:,}")
    logger.info(f"Reserved Response Tokens: {OPTIMIZATION_CONFIG.reserved_response_tokens:,}")
    logger.info(f"Active Nodes: {len(NODE_POOL)}")
    logger.info("=" * 70)

    application.run(host=settings.bind_host, port=settings.bind_port, threaded=True)
