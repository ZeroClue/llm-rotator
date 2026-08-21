# Secure Tailscale LLM Proxy Rotator

An intelligent API gateway for local AI coding agents that distributes LLM requests across multiple Tailscale nodes with automatic failover, key injection, and **state-of-the-art token optimization**.

## 🚀 Features

### Core Capabilities
- **Dynamic Key Injection**: Automatically injects the correct API key matched to the selected egress Tailscale node
- **Resilient Failover**: Seamlessly retries failed requests (429, 5xx, timeouts) on the next available node
- **Environment Configuration**: No hardcoded secrets - all configuration via environment variables
- **DNS Leak Prevention**: Uses `socks5h://` protocol to ensure DNS resolution happens on remote nodes
- **Thread-Safe Rotation**: Atomic round-robin node selection for concurrent requests
- **Streaming Support**: Full support for streaming chat completions with fast-path bypass

### Advanced Token Optimization Pipeline 🔥

A modular 6-stage compression pipeline that maximizes context utilization and minimizes token waste:

1. **Structural Hygiene** - Remove duplicate messages, strip excessive whitespace
2. **Semantic Compression** - LLMLingua-powered meaning preservation (optional)
3. **Prompt Caching** - Leverage OpenAI/Anthropic cache control headers
4. **Importance Scoring** - Keep high-value messages, drop low-value content
5. **Recursive Summarization** - Auto-summarize old context when approaching limits
6. **Smart Truncation** - Aggressive fallback with post-optimization verification

**All stages independently toggleable** via environment variables for granular control.

### Performance Optimizations
- **LRU Context Caching**: 128-entry cache for repeated context blocks (instant reuse)
- **Provider Profiles**: Pre-configured settings for OpenAI (128K), Anthropic (200K), Groq (32K)
- **Streaming Fast-Path**: Bypass expensive optimizations for real-time responses
- **Post-Verification**: Guaranteed token limit compliance after optimization

## 📋 Prerequisites

- Python 3.8+
- Tailscale installed and authenticated
- Four (or more) Tailscale nodes running SOCKS5 proxies
- API keys for each node's LLM provider account

## 🔧 Installation

### Option A: Direct Installation

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables:**
   ```bash
   cp .env.example .env
   # Edit .env with your actual values
   ```

3. **Required environment variables:**
   ```bash
   # Node configuration (add as many as needed)
   PROXY_1_URL=socks5h://100.64.0.1:1055
   API_KEY_1=sk-proj-your-actual-key-1
   
   PROXY_2_URL=socks5h://100.64.0.2:1055
   API_KEY_2=sk-proj-your-actual-key-2
   
   # ... add more nodes as needed
   ```

### Option B: Docker Deployment 🐳

1. **Build and run with docker-compose:**
   ```bash
   cp .env.example .env
   # Edit .env with your actual values
   
   docker-compose up -d --build
   ```

2. **Verify container is running:**
   ```bash
   docker-compose ps
   docker-compose logs -f llm-rotator
   ```

**Note:** Docker uses `network_mode: host` to access Tailscale's 100.64.x.x addresses directly.

### Option C: Manual Docker Build

```bash
docker build -t llm-rotator .
docker run -d \
  --name llm-rotator \
  --network host \
  --env-file .env \
  --restart unless-stopped \
  llm-rotator
```

## 🎯 Token Optimization Configuration

### Quick Start (Recommended Defaults)

```bash
# Enable the full optimization pipeline
ENABLE_CONTEXT_COMPRESSION=true

# Provider-specific presets (automatically configured)
PROVIDER_PROFILE=openai  # Options: openai, anthropic, groq

# Fine-tune individual stages
ENABLE_STRUCTURAL_HYGIENE=true
ENABLE_SEMANTIC_COMPRESSION=false  # Requires llmlingua
ENABLE_PROMPT_CACHING=true
ENABLE_IMPORTANCE_SCORING=true
ENABLE_RECURSIVE_SUMMARIZATION=false
ENABLE_SMART_TRUNCATION=true

# Performance features
ENABLE_STREAMING_FASTPATH=true
ENABLE_CONTEXT_CACHE=true
CONTEXT_CACHE_SIZE=128
```

### Advanced Configuration

```bash
# Token budget management
MAX_CONTEXT_TOKENS=120000        # Adjust based on your model
RESERVED_RESPONSE_TOKENS=4000    # Space for model output
COMPRESSION_THRESHOLD=0.85       # Compress when >85% full

# Structural hygiene settings
REMOVE_DUPLICATE_MESSAGES=true
STRIP_WHITESPACE=true
MIN_MESSAGE_LENGTH=10            # Ignore very short messages

# Semantic compression (requires llmlingua)
LLMLINGUA_TARGET_RATIO=0.5       # Compress to 50% of original
LLMLINGUA_FORCE_TOKENS=200       # Minimum tokens after compression

# Importance scoring
IMPORTANCE_KEYWORDS="error,fix,bug,critical,important"
SYSTEM_PROMPT_WEIGHT=2.0         # Weight for system messages
RECENT_MESSAGES_WEIGHT=1.5       # Weight for recent context

# Recursive summarization
ENABLE_SUMMARIZATION=false       # CPU-intensive, enable with caution
SUMMARIZATION_MODEL=gpt-4o-mini  # Use cheaper model for summarization
MAX_SUMMARY_TOKENS=500           # Max tokens per summary

# Streaming optimization
STREAMING_CHUNK_SIZE=1000        # Process in chunks for large contexts
```

### Provider Profiles (Automatic Configuration)

| Provider | Max Context | Reserved | Cache Support | Profile Name |
|----------|-------------|----------|---------------|--------------|
| OpenAI   | 128,000     | 4,000    | Yes           | `openai`     |
| Anthropic| 200,000     | 8,000    | Yes           | `anthropic`  |
| Groq     | 32,000      | 2,000    | No            | `groq`       |

Set `PROVIDER_PROFILE=<name>` to automatically configure optimal settings.

## 🏃 Running the Proxy

### Direct Execution
```bash
python rotator.py
```

### With systemd (Linux)
```bash
sudo cp llm-rotator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable llm-rotator
sudo systemctl start llm-rotator
sudo systemctl status llm-rotator
```

### With Docker Compose
```bash
docker-compose up -d
docker-compose logs -f
```

### With launchd (macOS)
Create `/Library/LaunchDaemons/com.llmrotator.plist`:
```xml
<key>ProgramArguments</key>
<array>
    <string>/usr/bin/python3</string>
    <string>/path/to/rotator.py</string>
</array>
```

## 📡 Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | POST | Main chat completion endpoint with optimization |
| `/v1/models` | GET | List available models |
| `/health` | GET | Health check with optimization status |
| `/health/detailed` | GET | Detailed health with node status and cache info |

### Health Check Response Example

```json
{
  "status": "healthy",
  "nodes_configured": 4,
  "current_node_index": 2,
  "token_optimization_enabled": true,
  "optimization_stages": {
    "structural_hygiene": true,
    "semantic_compression": false,
    "prompt_caching": true,
    "importance_scoring": true,
    "recursive_summarization": false,
    "smart_truncation": true
  },
  "provider_profile": "openai",
  "max_context_tokens": 120000,
  "reserved_response_tokens": 4000,
  "context_cache_enabled": true,
  "context_cache_size": 128,
  "context_cache_hits": 47,
  "context_cache_misses": 203,
  "dependencies": {
    "tiktoken": true,
    "llmlingua": false
  }
}
```

## 🔒 Security

### Tailscale ACL Configuration

Add this to your Tailscale Admin Console ACL JSON:

```json
{
  "tags": {
    "tag:llm-client": ["admin@yourdomain.com"],
    "tag:proxy-nodes": ["admin@yourdomain.com"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["tag:llm-client"],
      "dst": ["tag:proxy-nodes:1055"]
    }
  ]
}
```

### On Proxy Nodes

```bash
tailscaled --socks5-server=0.0.0.0:1055
```

### Best Practices

1. **Localhost Binding**: The proxy binds to `127.0.0.1` by default (change with `BIND_HOST`)
2. **No Secret Logging**: API keys are never logged (only prefixes shown)
3. **Environment Variables**: Store secrets in `.env` file (gitignored)
4. **Docker Security**: Runs as non-root user inside container

## 📊 Monitoring & Observability

### Health Checks

```bash
# Basic health
curl http://127.0.0.1:8080/health

# Detailed health with cache stats
curl http://127.0.0.1:8080/health/detailed

# Watch logs
docker-compose logs -f llm-rotator
```

### Token Usage Logging

The proxy logs optimization statistics for each request:

```
[INFO] Context optimized: 45,230 → 28,450 tokens (37.1% reduction)
[INFO] Stage results: structural_hygiene=-2,100, semantic=-12,500, truncation=-2,180
[INFO] Cache: HIT (reused compressed context)
```

### Metrics to Watch

- **Cache Hit Rate**: Target >30% for repeated conversations
- **Compression Ratio**: Typical 30-60% reduction with full pipeline
- **Failover Frequency**: Should be rare; indicates rate limiting or node issues
- **Average Latency**: Streaming fast-path should add <50ms overhead

## 🔧 IDE Integration

### Continue.dev / OpenCode

```json
{
  "models": [{
    "title": "Tailscale Rotated LLM Pool",
    "provider": "openai",
    "model": "gpt-4o",
    "apiBase": "http://127.0.0.1:8080/v1",
    "apiKey": "managed-by-local-rotator-daemon"
  }]
}
```

### Cursor

Settings → AI → Custom API Endpoint:
- Base URL: `http://127.0.0.1:8080/v1`
- API Key: `any-value` (injected by proxy)

### VS Code Extensions

Any extension supporting OpenAI-compatible APIs works with:
- Endpoint: `http://127.0.0.1:8080/v1`
- Model: Any supported by your upstream providers

## 📝 Environment Variables Reference

### Core Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BIND_HOST` | `127.0.0.1` | Host to bind the proxy server |
| `BIND_PORT` | `8080` | Port to bind the proxy server |
| `LLM_PROVIDER_URL` | `https://api.openai.com/v1` | Target LLM provider API URL |
| `REQUEST_TIMEOUT` | `25.0` | Request timeout in seconds |
| `MAX_RETRIES` | `4` | Maximum retry attempts across nodes |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

### Node Configuration (Repeat for Each Node)

| Variable | Required | Description |
|----------|----------|-------------|
| `PROXY_N_URL` | ✅ | SOCKS5 proxy URL (e.g., `socks5h://100.64.0.1:1055`) |
| `API_KEY_N` | ✅ | API key for node N's LLM account |

Example:
```bash
PROXY_1_URL=socks5h://100.64.0.1:1055
API_KEY_1=sk-proj-xxxxx

PROXY_2_URL=socks5h://100.64.0.2:1055
API_KEY_2=sk-proj-yyyyy

PROXY_3_URL=socks5h://100.64.0.3:1055
API_KEY_3=sk-proj-zzzzz
```

### Token Optimization Pipeline

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_CONTEXT_COMPRESSION` | `true` | Master switch for all optimizations |
| `PROVIDER_PROFILE` | `openai` | Preset: openai, anthropic, groq |
| `MAX_CONTEXT_TOKENS` | `120000` | Maximum context window size |
| `RESERVED_RESPONSE_TOKENS` | `4000` | Tokens reserved for model response |
| `COMPRESSION_THRESHOLD` | `0.85` | Compress when context >85% full |

#### Stage-Specific Toggles

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_STRUCTURAL_HYGIENE` | `true` | Remove duplicates, strip whitespace |
| `ENABLE_SEMANTIC_COMPRESSION` | `false` | LLMLingua-powered compression |
| `ENABLE_PROMPT_CACHING` | `true` | Use provider cache headers |
| `ENABLE_IMPORTANCE_SCORING` | `true` | Prioritize important messages |
| `ENABLE_RECURSIVE_SUMMARIZATION` | `false` | Auto-summarize old context |
| `ENABLE_SMART_TRUNCATION` | `true` | Drop oldest messages when needed |

#### Performance Features

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_STREAMING_FASTPATH` | `true` | Skip optimization for streaming |
| `ENABLE_CONTEXT_CACHE` | `true` | Cache compressed contexts |
| `CONTEXT_CACHE_SIZE` | `128` | Number of cached contexts |

#### Fine-Tuning Parameters

| Variable | Default | Description |
|----------|---------|-------------|
| `REMOVE_DUPLICATE_MESSAGES` | `true` | Remove consecutive duplicates |
| `STRIP_WHITESPACE` | `true` | Clean excessive whitespace |
| `MIN_MESSAGE_LENGTH` | `10` | Ignore messages shorter than this |
| `LLMLINGUA_TARGET_RATIO` | `0.5` | Compress to X% of original |
| `LLMLINGUA_FORCE_TOKENS` | `200` | Minimum tokens after compression |
| `IMPORTANCE_KEYWORDS` | See code | Comma-separated keywords |
| `SYSTEM_PROMPT_WEIGHT` | `2.0` | Weight multiplier for system messages |
| `RECENT_MESSAGES_WEIGHT` | `1.5` | Weight multiplier for recent messages |
| `ENABLE_SUMMARIZATION` | `false` | Enable recursive summarization |
| `SUMMARIZATION_MODEL` | `gpt-4o-mini` | Model for summarization |
| `MAX_SUMMARY_TOKENS` | `500` | Max tokens per summary |

## ⚠️ Troubleshooting

### All Nodes Failing

```bash
# Verify Tailscale is running
tailscale status

# Check SOCKS5 proxy is listening
netstat -tlnp | grep 1055

# Test connectivity to a node
curl --socks5-hostname 100.64.0.1:1055 https://api.openai.com/v1/models
```

### Token Optimization Not Working

```bash
# Ensure tiktoken is installed
pip install tiktoken

# Check if optimization is enabled
curl http://127.0.0.1:8080/health | jq .token_optimization_enabled

# Review logs for optimization stats
docker-compose logs llm-rotator | grep "Context optimized"
```

### Semantic Compression Issues

```bash
# Install llmlingua (optional but recommended)
pip install llmlingua

# Verify installation
python -c "from llmlingua import PromptCompressor; print('OK')"

# Check status in health endpoint
curl http://127.0.0.1:8080/health | jq .dependencies.llmlingua
```

### High Latency with Optimization

```bash
# Enable streaming fast-path
export ENABLE_STREAMING_FASTPATH=true

# Disable expensive stages
export ENABLE_SEMANTIC_COMPRESSION=false
export ENABLE_RECURSIVE_SUMMARIZATION=false

# Reduce cache size if memory-constrained
export CONTEXT_CACHE_SIZE=64
```

### Cache Miss Rate Too High

- Ensure conversations have consistent structure
- Check if `CONTEXT_CACHE_SIZE` is too small
- Verify content hashing isn't too sensitive (check logs)

## 🧪 Testing

### Unit Tests

```bash
python -m pytest tests/ -v
```

### Integration Tests

```bash
# Test basic rotation
curl http://127.0.0.1:8080/health

# Test chat completion
curl -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Test optimization stats
curl http://127.0.0.1:8080/health/detailed | jq .context_cache
```

## 📚 Additional Resources

- [Tailscale Documentation](https://tailscale.com/docs)
- [LLMLingua GitHub](https://github.com/microsoft/LLMLingua)
- [OpenAI Token Guide](https://platform.openai.com/token-guidelines)
- [Anthropic Prompt Caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching)

## 🤝 Contributing

1. Create a feature branch (`git checkout -b feature/amazing-feature`)
2. Make your changes with tests
3. Run the test suite (`pytest tests/`)
4. Submit a pull request

## 📄 License

MIT License - see LICENSE file for details.

## 🙏 Acknowledgments

- [Caveman](https://github.com/semperos/caveman) for inspiration on context management
- [LLMLingua](https://github.com/microsoft/LLMLingua) for semantic compression
- [Tailscale](https://tailscale.com) for secure networking
- The open-source community for continuous innovation in LLM efficiency

