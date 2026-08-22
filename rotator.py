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
import json
import time
import logging
import threading
from dataclasses import dataclass, field, replace

from flask import Flask, request, Response, jsonify, stream_with_context
from werkzeug.exceptions import HTTPException

from failover import AllNodesFailed, FailoverTransport

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    force=True
)
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
BIND_HOST = os.getenv("PROXY_BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.getenv("PROXY_BIND_PORT", "8080"))
TARGET_PROVIDER_URL = os.getenv("LLM_PROVIDER_URL", "https://api.openai.com/v1")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "25.0"))
RETRY_BACKOFF_BASE = float(os.getenv("RETRY_BACKOFF_BASE", "0.5"))
RETRY_BACKOFF_MAX = float(os.getenv("RETRY_BACKOFF_MAX", "8.0"))
NODE_COOLDOWN_BASE = float(os.getenv("NODE_COOLDOWN_BASE", "2.0"))
NODE_COOLDOWN_MAX = float(os.getenv("NODE_COOLDOWN_MAX", "60.0"))

# Token-optimization settings live in OptimizationConfig.from_env() — parsed
# in exactly one place, next to the pipeline class below.
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-4o")

# ─────────────────────────────────────────────────────────────────────────────
# Build Node Pool from Environment Variables
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Node:
    """One upstream target: an egress paired with the API key spent through it."""
    node_id: int
    proxy: str
    api_key: str = field(repr=False)


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

        pool.append(Node(node_id=node_index, proxy=proxy_url, api_key=api_key))
        logger.info(f"Loaded node {node_index}: {proxy_url}")
        node_index += 1

    logger.info(f"Node pool initialized with {len(pool)} nodes")
    return pool

try:
    NODE_POOL = build_node_pool()
except Exception as e:
    logger.critical(f"Failed to initialize node pool: {e}")
    raise SystemExit(1)


# Initialize Flask app; the upstream HTTP session lives inside the transport.
app = Flask(__name__)

# Optional bearer-token gate. Empty disables auth entirely (current behavior
# for existing deployments). /health and /ready stay open either way so
# orchestrators can probe the process without holding the token.
PROXY_AUTH_TOKEN = os.getenv("PROXY_AUTH_TOKEN", "")


@app.before_request
def require_bearer_token():
    if not PROXY_AUTH_TOKEN or request.path in ("/health", "/ready"):
        return None
    provided = request.headers.get("Authorization", "")
    # Bytes, not str: compare_digest raises TypeError on non-ASCII str, and
    # client-supplied headers can contain anything. utf-8 encoding never does.
    if not hmac.compare_digest(
        provided.encode("utf-8"), f"Bearer {PROXY_AUTH_TOKEN}".encode("utf-8")
    ):
        return Response(
            json.dumps({
                "error": {
                    "message": "Invalid or missing bearer token",
                    "type": "auth_error",
                }
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

    def __init__(self, nodes, cooldown_base, cooldown_max, lock=None):
        self._cooldown_base = cooldown_base
        self._cooldown_max = cooldown_max
        # Reentrant because NodeSelector.select() holds this same shared lock
        # while calling usable(); separate locks would break that atomicity.
        self._lock = lock if lock is not None else threading.RLock()
        self._state = {
            n.node_id: {"consecutive_failures": 0, "fail_until": 0.0}
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
        """All nodes' {consecutive_failures, cooldown_seconds} under one lock
        and a single clock reading, so a report can't tear the pair apart."""
        if now is None:
            now = time.monotonic()
        with self._lock:
            return {
                nid: {
                    "consecutive_failures": e["consecutive_failures"],
                    "cooldown_seconds": round(max(0.0, e["fail_until"] - now), 3),
                }
                for nid, e in self._state.items()
            }

    def record_success(self, node):
        with self._lock:
            entry = self._entry(node)
            entry["consecutive_failures"] = 0
            entry["fail_until"] = 0.0

    def record_failure(self, node, now=None):
        if now is None:
            now = time.monotonic()
        with self._lock:
            entry = self._entry(node)
            entry["consecutive_failures"] += 1
            cooldown = min(
                self._cooldown_max,
                self._cooldown_base * (2 ** (entry["consecutive_failures"] - 1)),
            )
            entry["fail_until"] = now + cooldown

    def reset_all(self):
        with self._lock:
            for entry in self._state.values():
                entry["consecutive_failures"] = 0
                entry["fail_until"] = 0.0


class NodeSelector:
    """
    Round-robin cursor over the node pool. Selects the next usable node and
    advances the cursor past each selection; if every node is cooling down,
    serves the cursor node anyway so requests never starve.
    """

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
        """Atomically pick the next usable node and advance the cursor."""
        if now is None:
            now = time.monotonic()
        with self._lock:
            n = len(self.nodes)
            chosen = None
            for offset in range(n):
                i = (self._index + offset) % n
                if self.ledger.usable(self.nodes[i], now):
                    chosen = i
                    break
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
        "consecutive_failures": state[n.node_id]["consecutive_failures"],
        "cooldown_seconds": state[n.node_id]["cooldown_seconds"],
    } for n in nodes]


# One shared reentrant lock keeps ledger updates atomic with selection,
# matching the previous single-lock behavior of the combined class.
_ledger_lock = threading.RLock()
health_ledger = HealthLedger(NODE_POOL, NODE_COOLDOWN_BASE, NODE_COOLDOWN_MAX, lock=_ledger_lock)
node_selector = NodeSelector(NODE_POOL, health_ledger, lock=_ledger_lock)

# Retry/failover transport: env read once here; session/sleeper/rng default
# to production adapters inside the module.
transport = FailoverTransport(
    selector=node_selector,
    ledger=health_ledger,
    max_retries=MAX_RETRIES,
    timeout=REQUEST_TIMEOUT,
    backoff_base=RETRY_BACKOFF_BASE,
    backoff_max=RETRY_BACKOFF_MAX,
)


# ─────────────────────────────────────────────────────────────────────────────
# Token Management & Context Optimization Engine
# ─────────────────────────────────────────────────────────────────────────────
_PROVIDER_PROFILES = {
    "openai": {"max_context": 128000, "reserved_tokens": 4096},
    "anthropic": {"max_context": 200000, "reserved_tokens": 8192},
    "groq": {"max_context": 32000, "reserved_tokens": 2048},
}


def _env_bool(raw):
    return raw.lower() == "true"


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

    def __init__(self, config=None, model_name="gpt-4o"):
        self.config = config if config is not None else OptimizationConfig()
        self.model_name = model_name
        self.summarization_model = self.config.summarization_model
        
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
        """
        cfg = self.config
        if not cfg.enabled or not path.endswith("chat/completions"):
            return payload

        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list) or not messages:
            return payload

        try:
            logger.info(f"Applying token optimization to request... (streaming={is_streaming})")
            return self._optimize(dict(payload), [dict(m) for m in messages], is_streaming)
        except Exception:
            logger.exception("Context optimization failed; forwarding payload unoptimized")
            return payload

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


# Initialize token optimizer
OPTIMIZATION_CONFIG = OptimizationConfig.from_env()
token_optimizer = TokenOptimizer(config=OPTIMIZATION_CONFIG, model_name=DEFAULT_MODEL)


@app.route('/v1/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
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
    url = f"{TARGET_PROVIDER_URL}/{path}"

    result = transport.send(
        method=method,
        url=url,
        headers=dict(request.headers),
        payload=payload,
        stream=stream_upstream,
    )

    if isinstance(result, AllNodesFailed):
        logger.critical(f"All {MAX_RETRIES} proxy nodes failed. Last error: {result.last_error}")
        return Response(
            json.dumps({
                "error": {
                    "message": "Proxy Gateway Error: All backend nodes exhausted or rate-limited",
                    "type": "gateway_error",
                    "last_error": result.last_error
                }
            }),
            502,
            {"Content-Type": "application/json"}
        )

    if stream_upstream:
        return Response(result.body(), result.status_code, result.header_pairs)

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
                    f"Total: {usage.get('total_tokens', 'N/A')}"
                )
        except Exception:
            pass

    return Response(body, result.status_code, result.header_pairs)


def nodes_available_count():
    """Nodes not currently in cooldown, per the ledger's own clock."""
    state = health_ledger.health_state()
    return sum(1 for e in state.values() if e["cooldown_seconds"] <= 0)


@app.route('/health', methods=['GET'])
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


@app.route('/ready', methods=['GET'])
def ready_check():
    """Readiness for orchestrators: 503 while every node is in cooldown."""
    available = nodes_available_count()
    if available == 0:
        return jsonify({"status": "unavailable", "nodes_available": 0}), 503
    return jsonify({"status": "ready", "nodes_available": available}), 200


@app.route('/v1/models', methods=['GET'])
def list_models():
    """Proxy model listing endpoint (full failover via the transport)."""
    result = transport.send(
        method="GET",
        url=f"{TARGET_PROVIDER_URL}/models",
        headers={},
    )
    if isinstance(result, AllNodesFailed):
        logger.error(f"Failed to fetch models: {result.last_error}")
        return jsonify({"error": "Failed to fetch models from upstream"}), 502
    return Response(result.body(), result.status_code, result.header_pairs)


@app.errorhandler(Exception)
def handle_exception(e):
    """Global exception handler; HTTP errors keep their own status codes."""
    if isinstance(e, HTTPException):
        return e
    logger.exception(f"Unhandled exception: {e}")
    return jsonify({
        "error": {
            "message": "Internal proxy error",
            "type": "internal_error"
        }
    }), 500


if __name__ == '__main__':
    logger.info("=" * 70)
    logger.info("🚀 Secure Tailscale LLM Proxy Rotator Starting...")
    logger.info("=" * 70)
    logger.info(f"Binding to: {BIND_HOST}:{BIND_PORT}")
    logger.info(f"Target Provider: {TARGET_PROVIDER_URL}")
    logger.info(f"Token Optimization: {'ENABLED' if OPTIMIZATION_CONFIG.enabled else 'DISABLED'}")
    logger.info(f"Max Context Tokens: {OPTIMIZATION_CONFIG.max_context_tokens:,}")
    logger.info(f"Reserved Response Tokens: {OPTIMIZATION_CONFIG.reserved_response_tokens:,}")
    logger.info(f"Active Nodes: {len(NODE_POOL)}")
    logger.info("=" * 70)
    
    app.run(host=BIND_HOST, port=BIND_PORT, threaded=True)
