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
"""

import os
import re
import json
import logging
import threading
from flask import Flask, request, Response, jsonify

# Configure logging first (before any imports that might use it)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
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
ENABLE_PROMPT_CACHING = os.getenv("ENABLE_PROMPT_CACHING", "false").lower() == "true"
PROMPT_CACHE_TTL = int(os.getenv("PROMPT_CACHE_TTL", "300"))  # Cache TTL in seconds (Anthropic/OpenAI)
ENABLE_IMPORTANCE_SCORING = os.getenv("ENABLE_IMPORTANCE_SCORING", "false").lower() == "true"
MIN_MESSAGE_IMPORTANCE = float(os.getenv("MIN_MESSAGE_IMPORTANCE", "0.3"))  # Drop messages below this score
ENABLE_RECURSIVE_SUMMARIZATION = os.getenv("ENABLE_RECURSIVE_SUMMARIZATION", "false").lower() == "true"
SUMMARIZATION_MODEL = os.getenv("SUMMARIZATION_MODEL", "gpt-4o-mini")  # Model for summarization

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-4o")

# Logging configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper()),
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

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
            "node_id": node_index
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


class ThreadSafeIterator:
    """
    Thread-safe index counter for cycling the proxy pool concurrently.
    Ensures atomic access to the current node index across multiple threads.
    """
    
    def __init__(self, pool):
        self.pool = pool
        self.index = 0
        self.lock = threading.Lock()

    def get_next(self):
        """Atomically retrieve the next node and advance the cursor."""
        with self.lock:
            node = self.pool[self.index].copy()  # Return a copy to avoid mutation
            self.index = (self.index + 1) % len(self.pool)
            return node
    
    def get_current_index(self):
        """Get the current node index (for health checks)."""
        with self.lock:
            return self.index


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
    
    def optimize_context(self, payload: dict) -> dict:
        """
        Main optimization pipeline - executes enabled stages sequentially.
        """
        if not ENABLE_CONTEXT_COMPRESSION:
            return payload
        
        messages = payload.get("messages", [])
        if not messages:
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
        
        final_token_count = self.count_message_tokens(messages)
        savings = original_token_count - final_token_count
        savings_pct = (savings / original_token_count * 100) if original_token_count > 0 else 0
        
        if savings > 0:
            logger.info(
                f"Token optimization saved {savings:,} tokens ({savings_pct:.1f}% reduction). "
                f"Final count: {final_token_count:,} / {available_tokens:,}"
            )
        
        payload["messages"] = messages
        payload["max_tokens"] = min(
            payload.get("max_tokens", RESERVED_RESPONSE_TOKENS),
            MAX_CONTEXT_TOKENS - final_token_count
        )
        
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
        Based on research from Microsoft's LLMLingua project.
        """
        if not self.compressor or not messages:
            return messages
        
        try:
            # Extract text content from messages
            texts = []
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    texts.append(content)
            
            if not texts:
                return messages
            
            # Compress using llmlingua
            compressed = self.compressor.compress(
                texts,
                ratio=target_ratio,
                instructions="Preserve key information, code snippets, and technical details."
            )
            
            # Reconstruct messages with compressed content
            compressed_messages = []
            text_idx = 0
            for msg in messages:
                new_msg = msg.copy()
                content = msg.get("content", "")
                
                if isinstance(content, str) and content.strip() and text_idx < len(compressed):
                    new_msg["content"] = compressed[text_idx]
                    text_idx += 1
                
                compressed_messages.append(new_msg)
            
            logger.info(f"Semantic compression applied with ratio {target_ratio}")
            return compressed_messages
            
        except Exception as e:
            logger.error(f"Semantic compression failed: {e}. Falling back to original messages.")
            return messages
    
    def _add_prompt_caching(self, messages: list) -> list:
        """
        Add prompt caching metadata for supported providers.
        OpenAI: cache_control object
        Anthropic: cache_control with ephemeral type
        """
        if not messages:
            return messages
        
        # Mark system message and early context for caching
        for i, msg in enumerate(messages):
            if msg.get("role") == "system" or (i < len(messages) // 3):
                content = msg.get("content", "")
                if isinstance(content, str):
                    # Convert to structured content with cache control
                    msg["content"] = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral", "ttl_seconds": PROMPT_CACHE_TTL}
                        }
                    ]
        
        logger.debug(f"Prompt caching enabled with TTL={PROMPT_CACHE_TTL}s")
        return messages
    
    def _filter_by_importance(self, messages: list, min_importance: float) -> list:
        """
        Caveman-inspired importance scoring for message prioritization.
        Scores messages based on:
        - Recency (newer = more important)
        - Content length (longer = potentially more important)
        - Role (user queries often more important than assistant filler)
        - Keyword density (technical terms, questions, action items)
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
            
            scored_messages.append((score, msg))
        
        # Filter out low-importance messages (except recent ones)
        filtered = []
        for i, (score, msg) in enumerate(scored_messages):
            is_recent = i > len(scored_messages) - 3  # Keep last 3 messages
            if score >= min_importance or is_recent:
                filtered.append(msg)
            else:
                logger.debug(f"Filtered low-importance {msg.get('role')} message (score: {score:.2f})")
        
        return filtered
    
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
    if payload and request.is_json:
        try:
            parsed_payload = request.get_json(force=True)
            
            # Apply token optimization for chat completions
            if path.endswith("chat/completions") and ENABLE_CONTEXT_COMPRESSION:
                logger.info("Applying token optimization to request...")
                parsed_payload = token_optimizer.optimize_context(parsed_payload)
                payload = json.dumps(parsed_payload).encode('utf-8')
                
        except Exception as e:
            logger.warning(f"Could not parse/optimize payload: {e}")
    
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

        try:
            response = session.request(
                method=method,
                url=url,
                headers=headers,
                data=payload,
                cookies=cookies,
                proxies=proxies,
                timeout=REQUEST_TIMEOUT,
                stream=True if path.endswith("chat/completions") else False
            )

            # Trigger failover on rate limits or server errors
            if response.status_code in [429, 500, 502, 503, 504]:
                logger.warning(
                    f"Node {node['node_id']} returned HTTP {response.status_code}. "
                    f"Retrying with next node..."
                )
                last_error = f"Upstream error: {response.status_code}"
                continue
            
            # Log token usage if available
            if response.status_code == 200 and parsed_payload:
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
            
            # Successful response - stream or return normally
            if path.endswith("chat/completions") and request.args.get("stream") == "true":
                def generate():
                    for chunk in response.iter_content(chunk_size=8192):
                        yield chunk
                return Response(generate(), response.status_code, response.headers.items())
            else:
                return Response(response.content, response.status_code, response.headers.items())

        except Timeout as e:
            logger.error(f"Timeout on Node {node['node_id']}: {str(e)}. Retrying...")
            last_error = f"Timeout: {str(e)}"
            continue
            
        except ConnectionError as e:
            logger.error(f"Connection error on Node {node['node_id']}: {str(e)}. Retrying...")
            last_error = f"Connection error: {str(e)}"
            continue
            
        except RequestException as e:
            logger.error(f"Request failed on Node {node['node_id']}: {str(e)}. Retrying...")
            last_error = f"Request error: {str(e)}"
            continue

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


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for monitoring."""
    return jsonify({
        "status": "healthy",
        "nodes_configured": len(NODE_POOL),
        "current_node_index": node_iterator.get_current_index(),
        "token_optimization_enabled": ENABLE_CONTEXT_COMPRESSION,
        "max_context_tokens": MAX_CONTEXT_TOKENS,
        "reserved_response_tokens": RESERVED_RESPONSE_TOKENS
    }), 200


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
    """Global exception handler."""
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
