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
- LRU caching for repeated context blocks
- Provider-specific optimization profiles
"""

import os
import re
import hmac
import json
import time
import random
import logging
import threading
from collections import OrderedDict
from functools import lru_cache
from flask import Flask, request, Response, jsonify, stream_with_context
from werkzeug.exceptions import HTTPException

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

import requests
from requests import Session
from requests.exceptions import RequestException, Timeout, ConnectionError

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

_HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
    "date", "server", "content-encoding", "content-length",
})


def parse_retry_after(value):
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def compute_backoff(attempt, retry_after=None, rng=random.uniform):
    if retry_after is not None:
        return min(retry_after, RETRY_BACKOFF_MAX)
    delay = RETRY_BACKOFF_BASE * (2 ** attempt) + rng(0, RETRY_BACKOFF_BASE)
    return min(delay, RETRY_BACKOFF_MAX)

# Token optimization settings
MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "120000"))  # Reserve tokens for response
RESERVED_RESPONSE_TOKENS = int(os.getenv("RESERVED_RESPONSE_TOKENS", "4000"))
ENABLE_CONTEXT_COMPRESSION = os.getenv("ENABLE_CONTEXT_COMPRESSION", "true").lower() == "true"
COMPRESSION_THRESHOLD = float(os.getenv("COMPRESSION_THRESHOLD", "0.85"))  # Compress when >85% full
STRIP_WHITESPACE = os.getenv("STRIP_WHITESPACE", "true").lower() == "true"
REMOVE_DUPLICATE_MESSAGES = os.getenv("REMOVE_DUPLICATE_MESSAGES", "true").lower() == "true"

# Advanced compression settings (Caveman-inspired & llmlingua)
ENABLE_SEMANTIC_COMPRESSION = os.getenv("ENABLE_SEMANTIC_COMPRESSION", "false").lower() == "true"
SEMANTIC_COMPRESSION_RATIO = float(os.getenv("SEMANTIC_COMPRESSION_RATIO", "0.5"))  # Target 50% compression
_ENABLE_PROMPT_CACHING_RAW = os.getenv("ENABLE_PROMPT_CACHING")
ENABLE_PROMPT_CACHING = (_ENABLE_PROMPT_CACHING_RAW or "false").strip().lower() == "true"
ENABLE_IMPORTANCE_SCORING = os.getenv("ENABLE_IMPORTANCE_SCORING", "false").lower() == "true"
MIN_MESSAGE_IMPORTANCE = float(os.getenv("MIN_MESSAGE_IMPORTANCE", "0.3"))  # Drop messages below this score
ENABLE_RECURSIVE_SUMMARIZATION = os.getenv("ENABLE_RECURSIVE_SUMMARIZATION", "false").lower() == "true"
SUMMARIZATION_MODEL = os.getenv("SUMMARIZATION_MODEL", "gpt-4o-mini")  # Model for summarization

# Streaming and performance settings
ENABLE_STREAMING_FASTPATH = os.getenv("ENABLE_STREAMING_FASTPATH", "true").lower() == "true"
ENABLE_CONTEXT_CACHE = os.getenv("ENABLE_CONTEXT_CACHE", "true").lower() == "true"
CONTEXT_CACHE_SIZE = int(os.getenv("CONTEXT_CACHE_SIZE", "128"))  # LRU cache size

# Provider-specific profiles
PROVIDER_PROFILE = os.getenv("PROVIDER_PROFILE", "openai")  # openai, anthropic, groq
PROVIDER_PROFILES = {
    "openai": {
        "max_context": 128000,
        "reserved_tokens": 4096
    },
    "anthropic": {
        "max_context": 200000,
        "reserved_tokens": 8192
    },
    "groq": {
        "max_context": 32000,
        "reserved_tokens": 2048
    }
}

# Apply provider profile
if PROVIDER_PROFILE in PROVIDER_PROFILES:
    _profile = PROVIDER_PROFILES[PROVIDER_PROFILE]
    MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", str(_profile["max_context"])))
    RESERVED_RESPONSE_TOKENS = int(os.getenv("RESERVED_RESPONSE_TOKENS", str(_profile["reserved_tokens"])))
    logger.info(f"Applied provider profile: {PROVIDER_PROFILE} (max_context={MAX_CONTEXT_TOKENS})")
else:
    logger.warning(f"Unknown provider profile '{PROVIDER_PROFILE}', using defaults")

# Prompt-caching marker injection is Anthropic-style and breaks OpenAI-format
# payloads, so it never defaults on: only an explicit ENABLE_PROMPT_CACHING=true
# enables it, regardless of profile.
ENABLE_PROMPT_CACHING = (_ENABLE_PROMPT_CACHING_RAW or "false").strip().lower() == "true"

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-4o")

# ─────────────────────────────────────────────────────────────────────────────
# Build Node Pool from Environment Variables
# ─────────────────────────────────────────────────────────────────────────────
def build_node_pool():
    """Build node pool from environment variables."""
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
        
        pool.append({
            "proxy": proxy_url,
            "api_key": api_key,
            "node_id": node_index,
            "consecutive_failures": 0,
            "fail_until": 0.0
        })
        logger.info(f"Loaded node {node_index}: {proxy_url}")
        node_index += 1
    
    if len(pool) < 1:
        raise ValueError("No nodes configured in environment variables")
    
    logger.info(f"Node pool initialized with {len(pool)} nodes")
    return pool

try:
    NODE_POOL = build_node_pool()
except Exception as e:
    logger.critical(f"Failed to initialize node pool: {e}")
    raise SystemExit(1)


# Initialize Flask app and session
app = Flask(__name__)
session = Session()

# Optional bearer-token gate. Empty disables auth entirely (current behavior
# for existing deployments). /health stays open either way so orchestrators
# can probe the process without holding the token.
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


class ThreadSafeIterator:
    """
    Thread-safe round-robin cursor over the node pool with per-node
    failure cooldowns. Nodes that recently failed are skipped until their
    cooldown expires; if every node is cooling down, the cursor node is
    used anyway so requests never starve.
    """

    def __init__(self, pool):
        self.pool = pool
        self.index = 0
        self.lock = threading.Lock()
        for entry in self.pool:
            entry.setdefault("consecutive_failures", 0)
            entry.setdefault("fail_until", 0.0)

    def get_next(self, now=None):
        """Atomically select the next usable node and advance the cursor."""
        if now is None:
            now = time.monotonic()
        with self.lock:
            n = len(self.pool)
            chosen = None
            for offset in range(n):
                i = (self.index + offset) % n
                if self.pool[i]["fail_until"] <= now:
                    chosen = i
                    break
            if chosen is None:
                chosen = self.index % n
            node = self.pool[chosen]
            self.index = (chosen + 1) % n
            return {
                "proxy": node["proxy"],
                "api_key": node["api_key"],
                "node_id": node["node_id"],
            }

    def get_current_index(self):
        """Get the current node index (for health checks)."""
        with self.lock:
            return self.index

    def report_success(self, node):
        with self.lock:
            entry = self._find(node)
            if entry is not None:
                entry["consecutive_failures"] = 0
                entry["fail_until"] = 0.0

    def report_failure(self, node, now=None):
        with self.lock:
            entry = self._find(node)
            if entry is None:
                return
            entry["consecutive_failures"] += 1
            cooldown = min(
                NODE_COOLDOWN_MAX,
                NODE_COOLDOWN_BASE * (2 ** (entry["consecutive_failures"] - 1)),
            )
            if now is None:
                now = time.monotonic()
            entry["fail_until"] = now + cooldown

    def clear_failures(self):
        with self.lock:
            for entry in self.pool:
                entry["consecutive_failures"] = 0
                entry["fail_until"] = 0.0

    def snapshot(self, now=None):
        """Public view of node state for health checks (never includes keys)."""
        if now is None:
            now = time.monotonic()
        with self.lock:
            return [{
                "node_id": e["node_id"],
                "proxy": e["proxy"],
                "consecutive_failures": e["consecutive_failures"],
                "cooldown_seconds": round(max(0.0, e["fail_until"] - now), 3),
            } for e in self.pool]

    def _find(self, node):
        target = node.get("node_id") if isinstance(node, dict) else None
        if target is None:
            return None
        for entry in self.pool:
            if entry["node_id"] == target:
                return entry
        return None


# Initialize the thread-safe node iterator
node_iterator = ThreadSafeIterator(NODE_POOL)


# ─────────────────────────────────────────────────────────────────────────────
# Token Management & Context Optimization Engine
# ─────────────────────────────────────────────────────────────────────────────
class TokenOptimizer:
    """
    Advanced modular token management with pluggable compression strategies.
    
    Pipeline Stages (all independently toggleable):
    1. Structural Hygiene - Remove duplicates, strip whitespace (native Python)
    2. Semantic Compression - LLMLingua for aggressive context compression
    3. Prompt Caching - Cache control headers for Anthropic/OpenAI
    4. Importance Scoring - Caveman-inspired message prioritization
    5. Recursive Summarization - Summarize old context when needed
    6. Smart Truncation - Fallback token-based message dropping
    
    Improvements:
    - Post-optimization verification to prevent double-counting
    - LRU caching for repeated context blocks
    - Streaming fast-path bypass for real-time responses
    - Provider-specific token calculations
    """
    
    def __init__(self, model_name="gpt-4o"):
        self.model_name = model_name
        self.summarization_model = SUMMARIZATION_MODEL
        
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
        if LLMLINGUA_AVAILABLE and ENABLE_SEMANTIC_COMPRESSION:
            try:
                self.compressor = PromptCompressor()
                logger.info("LLMLingua semantic compression enabled")
            except Exception as e:
                logger.warning(f"Failed to initialize llmlingua: {e}")
                self.compressor = None
        
        # LRU cache for compressed contexts (content hash -> compressed result)
        self.context_cache = OrderedDict()
        self.cache_lock = threading.Lock()
        if ENABLE_CONTEXT_CACHE:
            logger.info(f"Context caching enabled (max size: {CONTEXT_CACHE_SIZE})")
    
    def _hash_content(self, messages: list) -> str:
        """Generate a hash key for message content caching."""
        import hashlib
        content_str = json.dumps(messages, sort_keys=True)
        return hashlib.md5(content_str.encode()).hexdigest()
    
    def _get_cached(self, hash_key: str) -> dict | None:
        """Retrieve cached optimization result if available, refreshing recency."""
        if not ENABLE_CONTEXT_CACHE:
            return None
        with self.cache_lock:
            result = self.context_cache.get(hash_key)
            if result is not None:
                self.context_cache.move_to_end(hash_key)
            return result

    def _cache_result(self, hash_key: str, result: dict):
        """Cache optimization result with LRU eviction."""
        if not ENABLE_CONTEXT_CACHE:
            return
        with self.cache_lock:
            if len(self.context_cache) >= CONTEXT_CACHE_SIZE:
                self.context_cache.popitem(last=False)
            self.context_cache[hash_key] = result
    
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
    
    def optimize_context(self, payload: dict, is_streaming: bool = False) -> dict:
        """
        Main optimization pipeline - executes enabled stages sequentially.
        
        Args:
            payload: The request payload containing messages
            is_streaming: If True, skip expensive optimizations for low-latency streaming
        
        Returns:
            Optimized payload with reduced token count
        """
        if not ENABLE_CONTEXT_COMPRESSION:
            return payload
        
        messages = payload.get("messages", [])
        if not messages:
            return payload
        
        # Fast path for streaming: skip expensive optimizations
        if is_streaming and ENABLE_STREAMING_FASTPATH:
            logger.debug("Streaming fast-path: minimal optimization applied")
            if REMOVE_DUPLICATE_MESSAGES:
                messages = self._remove_duplicates(messages)
            if STRIP_WHITESPACE:
                messages = self._strip_whitespace(messages)
            payload["messages"] = messages
            return payload
        
        # Check cache for identical context
        content_hash = self._hash_content(messages)
        cached_result = self._get_cached(content_hash)
        if cached_result:
            logger.info(f"Cache hit: reusing optimized context (saved {cached_result.get('tokens_saved', 0)} tokens)")
            payload["messages"] = cached_result["messages"]
            return payload
        
        original_token_count = self.count_message_tokens(messages)
        logger.debug(f"Original message token count: {original_token_count}")
        
        # Stage 1: Structural Hygiene (always runs first if enabled)
        if REMOVE_DUPLICATE_MESSAGES:
            messages = self._remove_duplicates(messages)
        
        if STRIP_WHITESPACE:
            messages = self._strip_whitespace(messages)
        
        current_token_count = self.count_message_tokens(messages)
        available_tokens = MAX_CONTEXT_TOKENS - RESERVED_RESPONSE_TOKENS
        
        logger.debug(
            f"After structural hygiene: {current_token_count} tokens "
            f"(available: {available_tokens})"
        )
        
        # Stage 2: Semantic Compression with LLMLingua
        if ENABLE_SEMANTIC_COMPRESSION and self.compressor:
            messages = self._apply_semantic_compression(messages, SEMANTIC_COMPRESSION_RATIO)
            current_token_count = self.count_message_tokens(messages)
            logger.debug(f"After semantic compression: {current_token_count} tokens")
        
        # Stage 3: Prompt Caching (add cache control metadata)
        if ENABLE_PROMPT_CACHING:
            messages = self._add_prompt_caching(messages)
        
        # Stage 4: Importance Scoring (Caveman-inspired)
        if ENABLE_IMPORTANCE_SCORING:
            messages = self._filter_by_importance(messages, MIN_MESSAGE_IMPORTANCE)
            current_token_count = self.count_message_tokens(messages)
            logger.debug(f"After importance filtering: {current_token_count} tokens")
        
        # Stage 5: Recursive Summarization
        if ENABLE_RECURSIVE_SUMMARIZATION and current_token_count > available_tokens * COMPRESSION_THRESHOLD:
            messages = self._recursive_summarization(messages, available_tokens)
            current_token_count = self.count_message_tokens(messages)
            logger.debug(f"After summarization: {current_token_count} tokens")
        
        # Stage 6: Smart Truncation (fallback)
        if current_token_count > available_tokens * COMPRESSION_THRESHOLD:
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
        
        payload["messages"] = messages
        payload["max_tokens"] = max(
            1,
            min(
                payload.get("max_tokens", RESERVED_RESPONSE_TOKENS),
                MAX_CONTEXT_TOKENS - final_token_count
            )
        )

        # Cache the result (client-specific fields like max_tokens are
        # intentionally excluded so cache hits never override request values)
        cache_data = {
            "messages": messages,
            "tokens_saved": savings
        }
        self._cache_result(content_hash, cache_data)
        
        return payload
    
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
token_optimizer = TokenOptimizer(model_name=DEFAULT_MODEL)


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
    cookies = request.cookies

    # Parse JSON payload for token optimization (if applicable)
    parsed_payload = None
    is_streaming = False
    if payload and request.is_json:
        try:
            parsed_payload = request.get_json(force=True)

            # Detect streaming requests from the body (OpenAI convention)
            if isinstance(parsed_payload, dict):
                is_streaming = bool(parsed_payload.get("stream", False))

            # Apply token optimization for chat completions
            if isinstance(parsed_payload, dict) and path.endswith("chat/completions") and ENABLE_CONTEXT_COMPRESSION:
                logger.info(f"Applying token optimization to request... (streaming={is_streaming})")
                parsed_payload = token_optimizer.optimize_context(parsed_payload, is_streaming=is_streaming)
                payload = json.dumps(parsed_payload).encode('utf-8')

        except Exception as e:
            logger.warning(f"Could not parse/optimize payload: {e}")

    # Legacy clients may signal streaming via query param instead of body.
    # Normalize the upstream body so providers actually stream back.
    wants_stream = is_streaming or request.args.get("stream") == "true"
    stream_upstream = wants_stream and path.endswith("chat/completions")
    if stream_upstream and isinstance(parsed_payload, dict) and not parsed_payload.get("stream"):
        parsed_payload["stream"] = True
        payload = json.dumps(parsed_payload).encode('utf-8')

    # Filter incoming host headers to prevent proxy conflicts
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ('host', 'content-length')}

    # Construct the full target URL
    url = f"{TARGET_PROVIDER_URL}/{path}"
    last_error = None

    for attempt in range(MAX_RETRIES):
        node = node_iterator.get_next()

        # Inject target node's API key
        headers["Authorization"] = f"Bearer {node['api_key']}"

        # Configure SOCKS5H proxy (ensures DNS resolution happens on remote node)
        proxies = {
            "http": node["proxy"],
            "https": node["proxy"]
        }

        logger.info(
            f"Attempt {attempt + 1}/{MAX_RETRIES}: Routing via Node {node['node_id']} "
            f"({node['proxy'].split('://')[1].split(':')[0]})"
        )

        retry_after = None
        try:
            response = session.request(
                method=method,
                url=url,
                headers=headers,
                data=payload,
                cookies=cookies,
                proxies=proxies,
                timeout=REQUEST_TIMEOUT,
                stream=stream_upstream
            )

            # Trigger failover on rate limits or server errors
            if response.status_code in [429, 500, 502, 503, 504]:
                logger.warning(
                    f"Node {node['node_id']} returned HTTP {response.status_code}. "
                    f"Retrying with next node..."
                )
                last_error = f"Upstream error: {response.status_code}"
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                response.close()
                node_iterator.report_failure(node)
                continue

            node_iterator.report_success(node)

            # Log token usage only for buffered JSON bodies; calling .json()
            # on a streamed SSE response would consume the whole stream.
            if response.status_code == 200 and parsed_payload and not stream_upstream:
                try:
                    resp_json = response.json()
                    usage = resp_json.get("usage", {})
                    if usage:
                        logger.info(
                            f"Token usage - Prompt: {usage.get('prompt_tokens', 'N/A')}, "
                            f"Completion: {usage.get('completion_tokens', 'N/A')}, "
                            f"Total: {usage.get('total_tokens', 'N/A')}"
                        )
                except Exception:
                    pass

            # Strip hop-by-hop and framing headers; requests has already
            # decoded content-encoding and werkzeug owns connection framing.
            forward_headers = [
                (k, v) for k, v in response.headers.items()
                if k.lower() not in _HOP_BY_HOP_HEADERS
            ]

            if stream_upstream:
                def generate():
                    try:
                        # chunk_size=1: larger sizes do exact blocking reads and
                        # buffer small SSE events until EOF, killing latency.
                        for chunk in response.iter_content(chunk_size=1):
                            yield chunk
                    finally:
                        response.close()
                return Response(generate(), response.status_code, forward_headers)

            body = response.content
            response.close()
            return Response(body, response.status_code, forward_headers)

        except Timeout as e:
            logger.error(f"Timeout on Node {node['node_id']}: {str(e)}. Retrying...")
            last_error = f"Timeout: {str(e)}"
            node_iterator.report_failure(node)
        except ConnectionError as e:
            logger.error(f"Connection error on Node {node['node_id']}: {str(e)}. Retrying...")
            last_error = f"Connection error: {str(e)}"
            node_iterator.report_failure(node)
        except RequestException as e:
            logger.error(f"Request failed on Node {node['node_id']}: {str(e)}. Retrying...")
            last_error = f"Request error: {str(e)}"
            node_iterator.report_failure(node)

        if attempt < MAX_RETRIES - 1:
            time.sleep(compute_backoff(attempt, retry_after))

    # All retries exhausted
    logger.critical(f"All {MAX_RETRIES} proxy nodes failed. Last error: {last_error}")
    return Response(
        json.dumps({
            "error": {
                "message": "Proxy Gateway Error: All backend nodes exhausted or rate-limited",
                "type": "gateway_error",
                "last_error": last_error
            }
        }),
        502,
        {"Content-Type": "application/json"}
    )


def nodes_available_count():
    """Nodes not currently in cooldown, per the iterator's own clock."""
    return sum(1 for e in node_iterator.snapshot() if e["cooldown_seconds"] <= 0)


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for monitoring."""
    return jsonify({
        "status": "healthy",
        "nodes_configured": len(NODE_POOL),
        "nodes_available": nodes_available_count(),
        "current_node_index": node_iterator.get_current_index(),
        "nodes": node_iterator.snapshot(),
        "token_optimization_enabled": ENABLE_CONTEXT_COMPRESSION,
        "max_context_tokens": MAX_CONTEXT_TOKENS,
        "reserved_response_tokens": RESERVED_RESPONSE_TOKENS
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
    """Proxy model listing endpoint."""
    node = node_iterator.get_next()
    headers = {"Authorization": f"Bearer {node['api_key']}"}
    proxies = {"http": node["proxy"], "https": node["proxy"]}
    
    try:
        response = session.get(
            url=f"{TARGET_PROVIDER_URL}/models",
            headers=headers,
            proxies=proxies,
            timeout=10
        )
        return Response(response.content, response.status_code, response.headers.items())
    except RequestException as e:
        logger.error(f"Failed to fetch models: {e}")
        return jsonify({"error": "Failed to fetch models from upstream"}), 502


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
    logger.info(f"Token Optimization: {'ENABLED' if ENABLE_CONTEXT_COMPRESSION else 'DISABLED'}")
    logger.info(f"Max Context Tokens: {MAX_CONTEXT_TOKENS:,}")
    logger.info(f"Reserved Response Tokens: {RESERVED_RESPONSE_TOKENS:,}")
    logger.info(f"Active Nodes: {len(NODE_POOL)}")
    logger.info("=" * 70)
    
    app.run(host=BIND_HOST, port=BIND_PORT, threaded=True)
