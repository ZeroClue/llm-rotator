#!/usr/bin/env python3
"""
Secure LLM Proxy Rotator with Tailscale Integration
====================================================
An intelligent API gateway that distributes LLM requests across multiple
Tailscale nodes with automatic failover, key injection, and rate-limit handling.

Features:
- Dynamic API key injection per node
- Thread-safe round-robin rotation
- Automatic failover on 429/5xx errors or timeouts
- SOCKS5H proxy routing (DNS resolution on remote nodes)
- Localhost-only binding for security
"""

import logging
import threading
import requests
from flask import Flask, request, Response

# Configure logging for proxy rotation tracking
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

app = Flask(__name__)

# ==================== System Configurations ====================
TARGET_PROVIDER_URL = "https://api.openai.com/v1"  # Base URL for OpenAI API
BIND_PORT = 8080
MAX_RETRIES = 4  # Matches pool size to prevent infinite loops
REQUEST_TIMEOUT = 25  # Seconds - short enough to trigger failover quickly

# ==================== Node Pool Configuration ====================
# Map each Tailscale node IP to its respective API key
# Replace placeholder keys with your actual API keys
NODE_POOL = [
    {"proxy": "socks5h://100.64.0.1:1055", "api_key": "sk-proj-Node1-xxxxxx"},
    {"proxy": "socks5h://100.64.0.2:1055", "api_key": "sk-proj-Node2-xxxxxx"},
    {"proxy": "socks5h://100.64.0.3:1055", "api_key": "sk-proj-Node3-xxxxxx"},
    {"proxy": "socks5h://100.64.0.4:1055", "api_key": "sk-proj-Node4-xxxxxx"}
]


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


# Initialize the thread-safe node iterator
node_iterator = ThreadSafeIterator(NODE_POOL)


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
    
    # Filter incoming host headers to prevent proxy conflicts
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ('host', 'content-length')}
    
    # Construct the full target URL
    url = f"{TARGET_PROVIDER_URL}/{path}"

    for attempt in range(MAX_RETRIES):
        node = node_iterator.get_next()
        
        # Inject target node's API key
        headers["Authorization"] = f"Bearer {node['api_key']}"
        
        # Configure SOCKS5H proxy (ensures DNS resolution happens on remote node)
        proxies = {
            "http": node["proxy"],
            "https": node["proxy"]
        }
        
        logging.info(
            f"Attempt {attempt + 1}/{MAX_RETRIES}: Routing via {node['proxy']} "
            f"(key prefix: {node['api_key'][:12]}...)"
        )

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                data=payload,
                cookies=cookies,
                proxies=proxies,
                timeout=REQUEST_TIMEOUT
            )

            # Trigger failover on rate limits (429) or transient backend failures (5xx)
            if response.status_code in (429, 500, 502, 503, 504):
                logging.warning(
                    f"Node {node['proxy']} returned status {response.status_code}. "
                    f"Retrying with next node..."
                )
                continue
            
            # Successful response - stream back to client
            logging.info(f"Success via {node['proxy']} with status {response.status_code}")
            return Response(
                response.content,
                status=response.status_code,
                headers=response.headers.items()
            )

        except requests.exceptions.Timeout:
            logging.error(
                f"Timeout on node {node['proxy']} after {REQUEST_TIMEOUT}s. "
                f"Retrying next node..."
            )
            continue
            
        except requests.exceptions.ConnectionError as e:
            logging.error(
                f"Connection error on node {node['proxy']}: {str(e)}. "
                f"Retrying next node..."
            )
            continue
            
        except requests.exceptions.RequestException as e:
            logging.error(
                f"Network error on node {node['proxy']}: {str(e)}. "
                f"Retrying next node..."
            )
            continue

    # All retries exhausted
    logging.critical(
        f"All {MAX_RETRIES} proxy nodes failed to process the request to {url}"
    )
    return Response(
        "Proxy Gateway Error: All available backend pools exhausted or rate-limited.",
        status=502,
        content_type='text/plain'
    )


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for monitoring."""
    return Response("OK", status=200, content_type='text/plain')


@app.route('/status', methods=['GET'])
def status():
    """Return current pool status and configuration."""
    return Response(
        f"Active nodes: {len(NODE_POOL)}\n"
        f"Max retries: {MAX_RETRIES}\n"
        f"Target: {TARGET_PROVIDER_URL}\n"
        f"Port: {BIND_PORT}",
        status=200,
        content_type='text/plain'
    )


if __name__ == '__main__':
    # Bound explicitly to localhost to prevent local network exposure
    logging.info(f"Starting Secure LLM Proxy Gateway on port {BIND_PORT}...")
    logging.info(f"Target provider: {TARGET_PROVIDER_URL}")
    logging.info(f"Node pool size: {len(NODE_POOL)}")
    
    app.run(host='127.0.0.1', port=BIND_PORT, threaded=True)
